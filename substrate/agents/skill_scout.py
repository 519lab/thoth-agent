"""SkillScout — drafts new skills from recurring/important needs in memory.

Self-improvement Tier 1
(``docs/plans/2026-05-28-substrate-self-improvement-forge.md``). The Curator
keeps the substrate's *knowledge* healthy; the SkillScout extends that loop to
*capability*: it watches the upper layers (L3 patterns) for a recurring,
high-salience need, drafts a skill for it via the auxiliary model, stages it as
a **pending proposal**, and messages the user to review it in chat. It NEVER
installs a skill — the human approval (via the ``skill_proposal`` tool) is the
gate. The pending proposal is the Tier-1 quarantine.

Gated by ``THOTH_SUBSTRATE_SKILL_SCOUT`` (default OFF — opt-in like the Parser):
registers + heartbeats, tick no-ops until enabled. Change-gated like the
PatternFinder so it only works when L3 actually changed, and capped at
``SKILL_SCOUT_MAX_PENDING`` open proposals so it never floods the user.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Optional

from substrate.agents.base import Level, SubAgent

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_choice(name: str, choices: tuple[str, ...], default: str) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw if raw in choices else default


class SkillScout(SubAgent):
    """L3 need → drafted skill proposal. Floor intensity LOW (deep-cycle work)."""

    name = "skill-scout"
    is_sentinel = False

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        self._level = Level.LOW
        # Change-gating (mirrors PatternFinder): interval throttle + a watermark
        # of the newest L3 pattern seen at the last run.
        self._last_run_mono: float = 0.0
        self._last_l3_max_seen = None
        # Needs the drafter declined this process-lifetime — don't re-spend the
        # auxiliary model on them every interval. (A declined need produces no
        # proposal, so the proposal-table dedup can't cover it.)
        self._declined: set[str] = set()

    async def tick(self) -> None:
        if not _env_bool("THOTH_SUBSTRATE_SKILL_SCOUT", default=False):
            return
        if self._level is Level.OFF:
            return
        if not await self._should_run():
            return

        # Don't flood: cap concurrent pending proposals awaiting the user.
        from substrate.skill_proposals import store

        max_pending = _env_int("SKILL_SCOUT_MAX_PENDING", 3)
        if await store.count_pending() >= max_pending:
            return

        candidate = await self._pick_candidate()
        if candidate is None:
            return

        # Already covered by an existing skill? (keyword overlap is enough to
        # avoid the obvious duplicate; the drafter is the finer judge.)
        if self._already_covered(candidate["need_text"]):
            return

        from substrate.skill_proposals import author

        started = time.monotonic()
        timeout_s = _env_int("SKILL_SCOUT_TIMEOUT_S", 40)
        try:
            drafted = await asyncio.wait_for(
                author.draft_skill(candidate["need_text"], substrate=self._substrate),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, Exception):
            self._log.debug("skill_scout.draft.degraded", exc_info=True)
            return

        if drafted is None:
            # Model declined or output unusable — remember so we don't retry it
            # every interval this process lifetime.
            self._declined.add(candidate["key"])
            return

        # Phase 2 — frontier-model evaluator (advisory by default). Judge the
        # draft before the user sees it; defense-in-depth, never auto-approves.
        # `None` (no model configured / call failed) → un-vetted, Phase-1 path.
        mode = _env_choice("SKILL_EVALUATOR_MODE", ("off", "advisory", "gate"), "advisory")
        verdict = None
        if mode != "off":
            verdict = await self._evaluate(drafted, candidate["need_text"], timeout_s)

        ev_kwargs = (
            {} if verdict is None
            else dict(
                eval_verdict=verdict.verdict,
                eval_reasons=verdict.reasons,
                eval_model=verdict.model,
            )
        )

        # Slug already proposed/decided? insert is a no-op then (unique slug).
        proposal_id = await store.insert_proposal(
            slug=drafted.slug,
            title=drafted.title,
            draft_content=drafted.skill_md,
            rationale=drafted.rationale,
            source_l3_ids=candidate["l3_ids"],
            source_l4_ids=candidate["l4_ids"],
            salience=candidate["salience"],
            **ev_kwargs,
        )
        if proposal_id is None:
            self._declined.add(candidate["key"])  # covered already; stop re-picking
            return

        await self._emit_proposed(drafted, candidate, time.monotonic() - started, verdict)

        # gate mode: a `reject` verdict auto-rejects the proposal and stays
        # silent — the user only hears about pass/flag drafts. The row is kept
        # (flipped to rejected) for audit + dedup.
        if mode == "gate" and verdict is not None and verdict.verdict == "reject":
            await store.set_status(drafted.slug, "rejected", by="evaluator")
            await self._emit_eval_rejected(drafted, verdict)
            self._declined.add(candidate["key"])
            return

        await self._notify(drafted, verdict)

    async def _evaluate(self, drafted, need_text, timeout_s):
        """Run the evaluator, timeout-guarded; ``None`` on any failure."""
        from substrate.skill_proposals import evaluator

        try:
            return await asyncio.wait_for(
                evaluator.evaluate_skill(
                    drafted.skill_md, need_text, substrate=self._substrate
                ),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, Exception):
            self._log.debug("skill_scout.evaluate.degraded", exc_info=True)
            return None

    async def _should_run(self) -> bool:
        """Interval throttle AND a check that L3 gained/updated patterns since
        the last run — on a static L3 there's no new need to mine."""
        import thoth_db

        interval = _env_int("SKILL_SCOUT_INTERVAL_S", 3600)
        now_mono = asyncio.get_event_loop().time()
        if self._last_run_mono and (now_mono - self._last_run_mono) < interval:
            return False
        async with thoth_db.connection() as conn:
            l3_max = await conn.fetchval("SELECT max(last_seen_at) FROM l3_patterns")
        if (
            l3_max is not None
            and self._last_l3_max_seen is not None
            and l3_max <= self._last_l3_max_seen
        ):
            self._last_run_mono = now_mono  # honour the throttle on the next check
            return False
        self._last_run_mono = now_mono
        self._last_l3_max_seen = l3_max
        return True

    async def _pick_candidate(self) -> Optional[dict]:
        """Highest-salience recurring/thematic L3 pattern not already declined.

        Provenance is precise: the candidate is one pattern plus any sibling
        patterns that share a cited entity (a small need-cluster), so a reviewer
        can trace exactly what triggered the proposal. (L4 self-observations are
        a future enrichment — the ``source_l4_ids`` column is already wired.)
        """
        import thoth_db

        floor = _env_float("SKILL_SCOUT_SALIENCE_FLOOR", 0.7)
        limit = _env_int("SKILL_SCOUT_CANDIDATES", 10)
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT id, statement, kind, salience_score, cites
                  FROM l3_patterns
                 WHERE salience_score >= $1
                   AND kind IN ('recurring_structure', 'theme')
                 ORDER BY salience_score DESC, last_seen_at DESC
                 LIMIT $2
                """,
                floor,
                limit,
            )
            for r in rows:
                key = str(r["id"])
                if key in self._declined:
                    continue
                cites = list(r["cites"] or [])
                # Sibling patterns citing any of the same entities → the cluster.
                siblings = []
                if cites:
                    siblings = await conn.fetch(
                        """
                        SELECT id, statement FROM l3_patterns
                         WHERE id <> $1
                           AND cites ?| $2::text[]
                         ORDER BY salience_score DESC
                         LIMIT 5
                        """,
                        r["id"],
                        [str(c) for c in cites],
                    )
                lines = [f"- {r['statement']} (kind: {r['kind']}, "
                         f"salience: {r['salience_score']:.2f})"]
                for s in siblings:
                    lines.append(f"- {s['statement']}")
                l3_ids = [str(r["id"])] + [str(s["id"]) for s in siblings]
                return {
                    "key": key,
                    "need_text": "Recurring pattern(s) observed:\n" + "\n".join(lines),
                    "l3_ids": l3_ids,
                    "l4_ids": [],
                    "salience": float(r["salience_score"]),
                }
        return None

    def _already_covered(self, need_text: str) -> bool:
        try:
            from substrate.skills_match import suggest_skills

            min_overlap = _env_int("SKILL_SCOUT_DEDUP_MIN_OVERLAP", 3)
            matches = suggest_skills(need_text, limit=1, min_overlap=min_overlap)
            return bool(matches)
        except Exception:
            self._log.debug("skill_scout.dedup.unavailable", exc_info=True)
            return False

    async def _emit_proposed(self, drafted, candidate, elapsed_s, verdict=None) -> None:
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="skill-scout",
                event="skill_scout.proposed",
                payload={
                    "slug": drafted.slug,
                    "title": drafted.title,
                    "salience": candidate["salience"],
                    "source_l3_ids": candidate["l3_ids"],
                    "latency_ms": int(elapsed_s * 1000),
                    "eval_verdict": verdict.verdict if verdict else None,
                    "eval_model": verdict.model if verdict else None,
                },
            )
        except Exception:
            self._log.debug("skill_scout.telemetry.emit_failed", exc_info=True)

    async def _emit_eval_rejected(self, drafted, verdict) -> None:
        """Telemetry-only audit trail for a gate-mode auto-reject (no user notify)."""
        from substrate.telemetry import write as telemetry_write

        try:
            await telemetry_write(
                self._substrate,
                agent="skill-scout",
                event="skill_scout.eval_rejected",
                payload={
                    "slug": drafted.slug,
                    "reasons": verdict.reasons,
                    "model": verdict.model,
                },
            )
        except Exception:
            self._log.debug("skill_scout.telemetry.emit_failed", exc_info=True)

    async def _notify(self, drafted, verdict=None) -> None:
        from substrate.notify import notify_user

        verdict_line = ""
        if verdict is not None:
            if verdict.verdict == "pass":
                verdict_line = "\n✓ evaluator: pass"
            else:  # flag (reject in advisory mode still surfaces for your call)
                why = f" — {verdict.reasons[0]}" if verdict.reasons else ""
                verdict_line = f"\n⚠️ evaluator: {verdict.verdict}{why}"

        msg = (
            f"💡 I drafted a skill from a recurring need in my memory: "
            f"*{drafted.title}* (`{drafted.slug}`).\n\n"
            f"Why: {drafted.rationale}{verdict_line}\n\n"
            f"Review it with `skill_proposal show {drafted.slug}`, then "
            f"`approve {drafted.slug}` to install or `reject {drafted.slug}` to discard."
        )
        try:
            errors = await notify_user(msg)
            if errors:
                # Non-fatal: the proposal is persisted and reviewable later.
                self._log.info("skill_scout.notify.partial errors=%s", errors)
        except Exception:
            self._log.debug("skill_scout.notify.failed", exc_info=True)


__all__ = ["SkillScout"]
