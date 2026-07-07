"""Phase B Curator — continuous decay, release, self-state emission.

Replaces the Phase A absence. The Curator manages the substrate's
salience landscape: slices fade exponentially per their decay profile's
half-life, get released below their ``min_salience_to_retain`` threshold
per the profile's tombstone policy, and emit per-decision self-state
slices so future Reflector/Critic (Phase E) develop calibration about
Curator behaviour.

The three sub-tasks per tick (decay → release → alarm) run in their own
transactions so a partial failure leaves the others intact. Audit
emissions run **after** the relevant transaction commits — a slow audit
emit doesn't extend the lock window on ``substrate_slices``.

**Phase C extension** (spec §5.7): the Curator also emits embeddings
for unembedded passed slices once per cycle. This is the backfill path
that keeps ``substrate_slices.embedding`` coverage climbing toward
100% — recall against missing-embedding slices falls back to keyword
Jaccard, so embedding-emit is an eventually-consistent optimisation,
not a correctness gate. Failures (API down, mis-encoded payload) are
logged and the slice retried up to ``RECALL_EMBEDDING_BACKFILL_MAX_RETRIES``
times before being persistently marked failed.

See [Phase B spec](https://github.com/ggrace519/llm-cognitive-thought/blob/main/docs/superpowers/specs/2026-05-25-phase-b-curator.md)
§4 (Curator's loop), §6 (release), §7 (self-state emission), and
[Phase C spec](https://github.com/ggrace519/llm-cognitive-thought/blob/main/docs/superpowers/specs/2026-05-25-phase-c-recall.md)
§5.7 (embedding-emit pipeline).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from substrate.agents.base import Level, SubAgent
# Module-level import so tests can monkeypatch
# substrate.agents.curator.embed for the embedding-emit failure cases.
from substrate.recall.embeddings import embed
from substrate.l3 import store as l3
from substrate.l4 import store as l4

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate
    from substrate.storage.slices import ReleaseRecord


# Per-tick limits — keep ticks short so other sub-agents see a fair
# share of pool connections. Numbers from Phase B spec §4.1.
_DECAY_MIN_INTERVAL_SECONDS = 1.0
_RELEASE_BATCH_LIMIT = 200
_ALARM_BATCH_LIMIT = 100

# Don't re-alarm a slice we already touched in the last hour. Before
# this, every tick re-found the same overdue slices and bumped them
# again, producing 900+ alarm audit slices/hour at saturation and
# defeating the decay loop entirely. The cooldown means an alarmed
# slice gets ONE alarm per hour until something else (Parser
# consolidation in Phase D, recall reinforcement, manual operator
# intervention) changes its state.
# Exposed as a class attribute on ``Curator`` (overridable) for tests
# that need to fire the alarm against freshly-seeded data without
# waiting an hour for the cooldown to expire.
_ALARM_COOLDOWN_SECONDS = 3600

# Stranded-slice drain horizon (issue #287). The Parser only fetches
# pending slices younger than its 7-day horizon, so a passed +
# unconsolidated slice older than this can NEVER consolidate — and with
# ``release_after_consolidation`` profiles it can never be released
# either: an immortal slice the alarm re-finds every cooldown, forever
# (observed live: 1,602 alarms over 100 slices in ~2.5 weeks, which also
# held the Critic's ``alarms_1h`` coherence penalty permanently at -0.2).
# Same bug class PR #199 fixed for the Conductor backlog COUNT; this is
# the alarm/consolidation side. Slices aging past the horizon get ONE
# ``curator.slice_stranded`` telemetry event and are tombstoned via the
# Parser's give-up idiom (``mark_slices_consolidated(ids, [])``), which
# makes them release-eligible so natural decay retires them.
# Must stay >= the Parser's fetch horizon (7 days) or slices would be
# drained while still parseable.
_STRANDED_DRAIN_SECONDS = 7 * 86400
_STRANDED_BATCH_LIMIT = 200

# --- Upper-layer (L3/L4) curation — the Curator now curates patterns +
# observations too, not just L0 slices. These were unbounded append-only
# with exact-text-only dedup; the Curator semantically merges near-dupes
# and decays→releases stale ones. Run on a slow interval (deep-cycle work).
_CURATE_UPPER_INTERVAL_S = 3600.0       # hourly; far slower than the main tick
# within-kind merge distance. 0.18 (sim >= 0.82) — looser than the original
# 0.12 because a stronger embedder (e.g. Qwen3-Embedding) separates distinct
# facts well, so it's safe to fold more paraphrases. Env-tunable.
_UPPER_MERGE_MAX_DISTANCE = 0.18
# cross-kind merge distance — TIGHTER (near-identical only): the same fact
# stored as generalization + theme + recurring_structure won't collapse
# within-kind, so fold it across kinds when the text is nearly identical.
# 0 disables cross-kind merging. Env-tunable.
_UPPER_MERGE_CROSS_KIND_MAX_DISTANCE = 0.06
_UPPER_MERGE_SEEDS_PER_PASS = 50        # bounded work per pass (converges over passes)
_UPPER_MERGE_NEIGHBORS = 25             # near-dups examined per seed
_L3_HALF_LIFE_SECONDS = 7 * 86400.0     # patterns fade over ~a week unless reinforced
_L4_HALF_LIFE_SECONDS = 3 * 86400.0     # self-notes fade faster
_UPPER_RELEASE_FLOOR = 0.15             # release below this salience…
_UPPER_STALE_SECONDS = 7 * 86400.0      # …if also not re-found within a week


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


class Curator(SubAgent):
    """Real Phase B Curator. Tick body runs decay → release → alarm.

    Floor intensity = LOW (not FULL — Curator is not a Sentinel-class
    primitive). Operator can dial OFF to disable; intensity between OFF
    and LOW (no such enum value today, but future enum extensions are
    forward-compatible) is silently demoted to LOW.
    """

    name = "curator"
    is_sentinel = False

    DECAY_MIN_INTERVAL_SECONDS = _DECAY_MIN_INTERVAL_SECONDS
    RELEASE_BATCH_LIMIT = _RELEASE_BATCH_LIMIT
    ALARM_COOLDOWN_SECONDS = _ALARM_COOLDOWN_SECONDS
    ALARM_BATCH_LIMIT = _ALARM_BATCH_LIMIT
    STRANDED_DRAIN_SECONDS = _STRANDED_DRAIN_SECONDS
    STRANDED_BATCH_LIMIT = _STRANDED_BATCH_LIMIT
    # Upper-layer curation knobs (class attrs so tests can override).
    CURATE_UPPER_INTERVAL_S = _CURATE_UPPER_INTERVAL_S
    UPPER_MERGE_MAX_DISTANCE = _UPPER_MERGE_MAX_DISTANCE
    UPPER_MERGE_CROSS_KIND_MAX_DISTANCE = _UPPER_MERGE_CROSS_KIND_MAX_DISTANCE
    UPPER_MERGE_SEEDS_PER_PASS = _UPPER_MERGE_SEEDS_PER_PASS
    UPPER_MERGE_NEIGHBORS = _UPPER_MERGE_NEIGHBORS
    L3_HALF_LIFE_SECONDS = _L3_HALF_LIFE_SECONDS
    L4_HALF_LIFE_SECONDS = _L4_HALF_LIFE_SECONDS
    UPPER_RELEASE_FLOOR = _UPPER_RELEASE_FLOOR
    UPPER_STALE_SECONDS = _UPPER_STALE_SECONDS

    def __init__(self, substrate: "Substrate") -> None:
        super().__init__(substrate)
        # Pin floor at LOW. Base class default for non-sentinel is also
        # LOW; the assertive assignment here is forward-defensive against
        # future base-class default changes.
        self._level = Level.LOW
        # Phase C: per-slice retry counter for the embedding-emit loop.
        # In-process dict keyed by slice_id; bounded only by the
        # max-retries cap (failed slices get persisted into metadata
        # and then dropped from list_unembedded, so the dict naturally
        # caps).
        self._embed_failure_counts: dict[UUID, int] = {}
        # Track wall-clock of the last embedding backfill cycle so we
        # respect RECALL_EMBEDDING_BACKFILL_INTERVAL_S regardless of
        # how fast the Curator's main tick cadence is.
        self._last_embed_backfill_at: float = 0.0
        # Last auto-heal pass that un-parks ``embedding_failed`` slices, on
        # its own (long) interval so a fixed embedding config self-heals.
        self._last_retry_failed_at: float = 0.0
        # Last L3/L4 curation pass (its own slow interval).
        self._last_curate_upper_at: float = 0.0
        # Per-tick watchdog override (innovation #4): the Curator's tick runs
        # the hourly L3/L4 backfill in-line, which can far exceed the LOW-level
        # interval-derived ceiling. Give it a window sized to the upper-curation
        # cadence so a legitimately long backfill tick is not false-tripped as
        # wedged. Env-tunable via the same flag the backfill cadence reads.
        self.tick_timeout_s = _env_float(
            "THOTH_SUBSTRATE_CURATOR_TICK_TIMEOUT_S",
            _CURATE_UPPER_INTERVAL_S,
        )
        # Merge thresholds are env-tunable (operators dial the merge
        # aggressiveness without a redeploy; the accelerator script picks the
        # same env up). Instance attrs so tests can still override directly.
        self.UPPER_MERGE_MAX_DISTANCE = _env_float(
            "THOTH_SUBSTRATE_MERGE_MAX_DISTANCE", _UPPER_MERGE_MAX_DISTANCE
        )
        self.UPPER_MERGE_CROSS_KIND_MAX_DISTANCE = _env_float(
            "THOTH_SUBSTRATE_MERGE_CROSS_KIND_DISTANCE",
            _UPPER_MERGE_CROSS_KIND_MAX_DISTANCE,
        )

    # ------------------------------------------------------------------
    # Intensity floor — Phase B spec §8.3.
    # ------------------------------------------------------------------

    def set_intensity(self, level: Level) -> None:
        """Curator-specific floor: anything strictly between OFF and LOW
        is demoted to LOW. OFF is honoured verbatim (operator opt-out).

        The OFF→LOW gap is deliberate: OFF is a deliberate operator
        gesture ("halt this sub-agent"); LOW is "minimum useful work".
        A bug-y caller passing MODERATE-1 (no such enum value today)
        should get LOW, not OFF.
        """
        # Future-proofing: today the enum has OFF, LOW, MODERATE, HIGH,
        # FULL with no values between OFF and LOW. The demotion below is
        # a no-op for those five values; it only kicks in if the enum
        # ever grows a new value between OFF and LOW.
        if level is not Level.OFF and self._level_rank(level) < self._level_rank(Level.LOW):
            self._log.debug(
                "curator: demoting set_intensity(%s) to LOW (floor)",
                level.value,
            )
            level = Level.LOW
        self._level = level

    @staticmethod
    def _level_rank(level: Level) -> int:
        """Rank levels so the floor comparison is well-defined.
        OFF=0, LOW=1, MODERATE=2, HIGH=3, FULL=4. Anything new in the
        enum landing between OFF and LOW gets a rank between 0 and 1 —
        triggering the floor.
        """
        return {
            Level.OFF: 0,
            Level.LOW: 1,
            Level.MODERATE: 2,
            Level.HIGH: 3,
            Level.FULL: 4,
        }.get(level, 1)

    # ------------------------------------------------------------------
    # Tick body — decay → release → alarm. Each sub-task is its own
    # transaction (Phase B spec §4.3). Audit emissions run AFTER commit.
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        # Each stage is ISOLATED: a failure in one (e.g. a transient lock /
        # connection error on the bulk decay UPDATE while the DB is under
        # heavy write load) must not skip the stages after it. The sub-tasks
        # already run in their own transactions for *data* isolation, but
        # before this they ran as a bare sequence — so a raising decay stage
        # propagated out of tick() and the base loop swallowed the *whole*
        # tick, silently starving release / alarm / embedding for that cycle
        # (observed 2026-06: a ~2min window of decay errors during an
        # embedding storm also blocked release + embed). Per-stage try/except
        # keeps the rest of the tick running and logs each failure distinctly.
        await self._run_stage("decay", self._apply_natural_decay)
        await self._run_stage("release", self._release_stage)
        await self._run_stage("alarm", self._alarm_stage)
        # Stranded-slice drain (issue #287): tombstone passed+unconsolidated
        # slices the Parser can no longer reach so they stop alarming and
        # become release-eligible.
        await self._run_stage("strand", self._stranded_stage)
        # Phase C: embedding backfill — guarded by its own interval so
        # the Curator's main tick can run faster without hammering the
        # embedding API.
        await self._run_stage("embed", self._maybe_emit_embeddings)
        # Auto-heal: periodically un-park a batch of ``embedding_failed``
        # slices so a fixed config recovers without operator intervention.
        await self._run_stage("retry_failed", self._maybe_retry_failed_embeddings)
        # Upper-layer (L3/L4) curation — its own slow interval. Embed →
        # semantic-merge near-dupes → decay → release. The Curator curates
        # all memory layers, not just L0.
        await self._run_stage("curate_upper", self._maybe_curate_upper_layers)

    async def _run_stage(self, name: str, stage) -> None:
        """Run one tick stage, isolating its failure from the others.

        The base loop already logs+swallows whole-tick errors and continues;
        this narrows that to per-stage so one failing stage can't starve the
        rest of the tick. Distinct ``curator.stage.error stage=<name>`` log
        lines make it obvious which stage failed.
        """
        try:
            await stage()
        except Exception:
            self._log.exception("curator.stage.error stage=%s", name)

    async def _release_stage(self) -> None:
        released = await self._evaluate_releases()
        await self._emit_release_audit(released)

    async def _alarm_stage(self) -> None:
        alarmed = await self._alarm_pathological()
        await self._emit_alarm_audit(alarmed)

    async def _stranded_stage(self) -> None:
        stranded = await self._drain_stranded()
        await self._emit_stranded_audit(stranded)

    # ------------------------------------------------------------------
    # Decay — Phase B spec §4 + archived plan Task 5.2.
    # ------------------------------------------------------------------

    async def _apply_natural_decay(self) -> None:
        """Single UPDATE applying exponential decay to all eligible slices.

        Formula: ``salience *= POWER(0.5, dt / half_life)``
        where ``dt = now() - salience_updated_at`` and
        ``half_life = dp.natural_half_life``.

        Skips:
        * Slices with ``salience_updated_at`` within ``_DECAY_MIN_INTERVAL_SECONDS``
          (decay against tiny dt is mathematically noise, not signal).
        * ``sentinel_state != 'passed'`` (pending + quarantined are Sentinel territory).
        * ``consolidation_state = 'released'`` (already at 0; no-op).
        """
        import thoth_db

        async with thoth_db.transaction() as conn:
            await conn.execute(
                """
                UPDATE substrate_slices sl
                   SET salience_score = sl.salience_score *
                       POWER(
                           0.5,
                           -- Cap the exponent at 60 half-lives.  A slice whose
                           -- salience_updated_at is very old relative to its
                           -- half-life would otherwise drive POWER(0.5, huge)
                           -- below the smallest double and raise
                           -- NumericValueOutOfRangeError (underflow), aborting
                           -- the whole batched UPDATE so NO slice decays and
                           -- salience_updated_at never advances — an every-tick
                           -- crash loop. 2^-60 (~9e-19) is already far below any
                           -- retention floor, so the slice releases normally.
                           LEAST(
                               EXTRACT(EPOCH FROM (now() - sl.salience_updated_at))
                               / GREATEST(EXTRACT(EPOCH FROM dp.natural_half_life), 0.001),
                               60.0
                           )
                       ),
                       salience_updated_at = now()
                  FROM substrate_streams st
                  JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
                 WHERE sl.stream_id           = st.stream_id
                   AND sl.sentinel_state      = 'passed'
                   AND sl.consolidation_state <> 'released'
                   AND NOT sl.pinned
                   AND now() - sl.salience_updated_at > interval '1 second'
                """
            )

    # ------------------------------------------------------------------
    # Release — Phase B spec §6 + archived plan Tasks 5.4–5.5.
    # ------------------------------------------------------------------

    async def _evaluate_releases(self) -> list["ReleaseRecord"]:
        """Read + release up to ``RELEASE_BATCH_LIMIT`` eligible slices.

        Eligibility is the SliceRepo.release_eligible CTE: passed,
        not-released, below the per-profile salience floor, and either
        the profile does not require consolidation before release OR the
        slice is consolidated.
        """
        import thoth_db

        async with thoth_db.transaction() as conn:
            return await self._substrate.slices.release_eligible(
                conn, limit=self.RELEASE_BATCH_LIMIT
            )

    async def _emit_release_audit(self, released: list["ReleaseRecord"]) -> None:
        """Record one ``curator.release`` telemetry row per released slice.
        Bounded by the LIMIT in evaluate_releases. Runs after the release
        UPDATE has already committed.

        Operational telemetry (``substrate_telemetry``), not a perceptual
        slice — emitting these to ``substrate.self_state`` is what fed the
        L0 feedback loop.
        """
        if not released:
            return

        from substrate.telemetry import write as telemetry_write

        for r in released:
            await telemetry_write(
                self._substrate,
                agent="curator",
                event="curator.release",
                payload={
                    "slice_id": str(r.slice_id),
                    "stream_id": str(r.stream_id),
                    "tombstone_policy": r.tombstone_policy,
                    "salience_at_release": float(r.salience_at_release),
                },
            )

    # ------------------------------------------------------------------
    # Pathological-forgetting alarm — Phase B spec §7 + archived plan
    # Task 5.7. Slices past their profile's consolidation_window still
    # unconsolidated get bumped + emit an alarm self-state slice.
    # ------------------------------------------------------------------

    async def _alarm_pathological(self) -> list[dict]:
        """Find + report up to ``ALARM_BATCH_LIMIT`` overdue slices.

        Returns the per-alarm dicts so ``_emit_alarm_audit`` can write
        them without re-querying.

        Three changes from the original Phase B spec implementation
        (observed in production to be in a feedback loop, see
        commit message):

        1. No salience bump. The alarm is observational ("this slice
           rotted past its consolidation_window without an upstream
           consumer"). Bumping salience to 1.0 every tick defeats the
           decay mechanism — once promoted, the slice never decays
           again. We just record the rot in the audit slice and let
           natural decay continue.

        2. Per-slice cooldown via ``salience_updated_at``. After we
           alarm a slice once, suppress re-alarms for the next hour
           so an unconsolidated slice produces ONE alarm/hour instead
           of one per tick. ``salience_updated_at`` is touched here so
           subsequent ticks see it inside the cooldown window. Recall
           reinforcement also touches this column, which is fine —
           a slice the foreground keeps re-contacting doesn't need
           pathological-forgetting alarms either.

        3. Exclude every ``substrate.*`` stream — these carry the
           substrate's own operational telemetry (and historically the
           alarm/release audit slices themselves), which is non-perceptual
           and must never be alarm-eligible. Without the exclusion those
           slices age past their own consolidation_window and become
           alarm-eligible, a feedback loop that saturated the curator at
           900+ alarms/hour in production. (Operational events now go to
           ``substrate_telemetry``; this guard remains for the historical
           ``substrate.self_state`` rows and any future ``substrate.*``
           stream — see ``substrate.storage.streams.is_perceptual``.)

        4. Bounded to the stranded-drain horizon (issue #287). A slice
           older than ``STRANDED_DRAIN_SECONDS`` is beyond the Parser's
           fetch horizon and can never consolidate — alarming on it every
           cooldown forever is pure noise (and held the Critic's
           ``alarms_1h`` coherence penalty at -0.2 permanently). Those
           slices are handled by :meth:`_drain_stranded` instead: one
           ``curator.slice_stranded`` event, then tombstoned.
        """
        import thoth_db

        alarmed: list[dict] = []
        async with thoth_db.transaction() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.slice_id, sl.stream_id, sl.ingest_time_world,
                       sl.salience_score,
                       EXTRACT(EPOCH FROM (now() - sl.ingest_time_world))::bigint AS age_seconds,
                       EXTRACT(EPOCH FROM dp.consolidation_window)::bigint AS window_seconds
                  FROM substrate_slices         sl
                  JOIN substrate_streams        st ON st.stream_id  = sl.stream_id
                  JOIN substrate_decay_profiles dp ON dp.profile_id = st.decay_profile_id
                 WHERE sl.sentinel_state      = 'passed'
                   AND sl.consolidation_state = 'unconsolidated'
                   AND sl.ingest_time_world + dp.consolidation_window < now()
                   AND sl.ingest_time_world > now() - make_interval(secs => $3)
                   AND sl.salience_updated_at < now() - make_interval(secs => $2)
                   AND st.name NOT LIKE 'substrate.%'
                 ORDER BY sl.ingest_time_world ASC
                 LIMIT $1
                 FOR UPDATE OF sl SKIP LOCKED
                """,
                self.ALARM_BATCH_LIMIT,
                self.ALARM_COOLDOWN_SECONDS,
                self.STRANDED_DRAIN_SECONDS,
            )
            for r in rows:
                # Touch salience_updated_at so the cooldown predicate
                # excludes this slice from the next ALARM_COOLDOWN_SECONDS
                # of ticks. Salience itself is left alone — see docstring.
                await conn.execute(
                    """
                    UPDATE substrate_slices
                       SET salience_updated_at = now()
                     WHERE slice_id = $1 AND ingest_time_world = $2
                    """,
                    r["slice_id"],
                    r["ingest_time_world"],
                )
                alarmed.append(
                    {
                        "slice_id": r["slice_id"],
                        "stream_id": r["stream_id"],
                        "age_seconds": int(r["age_seconds"]),
                        "window_seconds": int(r["window_seconds"]),
                        # Kept in the audit payload for back-compat with
                        # existing emit code; reflects current (un-bumped)
                        # salience now rather than a post-bump value.
                        "bumped_to": float(r["salience_score"]),
                    }
                )
        return alarmed

    async def _drain_stranded(self) -> list[dict]:
        """Tombstone passed+unconsolidated slices older than the Parser's
        reach (issue #287).

        The Parser fetches only pending slices younger than its 7-day
        horizon, in per-session batches of ``PARSER_MIN_PENDING_SLICES``+.
        A slice that ages past the horizon unconsolidated is stranded: it
        can never consolidate, and (with ``release_after_consolidation``
        profiles) never release — immortal, and re-alarmed every cooldown.

        Drain = the Parser's own give-up idiom: ``mark_slices_consolidated``
        with an empty extraction (the parse-error tombstone, spec §4.4).
        Consolidated-empty slices leave the alarm set and become
        release-eligible, so natural decay retires them. One-shot by
        construction — a drained slice can never match this predicate again.

        The ``substrate.%`` exclusion mirrors the alarm's: historical
        non-perceptual rows stay untouched (they're excluded from every
        awareness-loop query anyway; rewriting history there buys nothing).
        """
        import thoth_db

        from substrate.l1 import store as l1_store

        stranded: list[dict] = []
        async with thoth_db.transaction() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.slice_id, sl.stream_id, sl.ingest_time_world,
                       EXTRACT(EPOCH FROM (now() - sl.ingest_time_world))::bigint AS age_seconds,
                       sl.metadata->>'session_id' AS session_id
                  FROM substrate_slices  sl
                  JOIN substrate_streams st ON st.stream_id = sl.stream_id
                 WHERE sl.sentinel_state      = 'passed'
                   AND sl.consolidation_state = 'unconsolidated'
                   AND sl.ingest_time_world < now() - make_interval(secs => $2)
                   AND st.name NOT LIKE 'substrate.%'
                 ORDER BY sl.ingest_time_world ASC
                 LIMIT $1
                 FOR UPDATE OF sl SKIP LOCKED
                """,
                self.STRANDED_BATCH_LIMIT,
                self.STRANDED_DRAIN_SECONDS,
            )
            if rows:
                await l1_store.mark_slices_consolidated(
                    [r["slice_id"] for r in rows], [], conn=conn
                )
            stranded = [
                {
                    "slice_id": r["slice_id"],
                    "stream_id": r["stream_id"],
                    "age_seconds": int(r["age_seconds"]),
                    "session_id": r["session_id"],
                }
                for r in rows
            ]
        if stranded:
            self._log.info(
                "curator.stranded_drain: tombstoned %d slice(s) older than %ds",
                len(stranded),
                self.STRANDED_DRAIN_SECONDS,
            )
        return stranded

    # ------------------------------------------------------------------
    # Phase C: embedding emit (spec §5.7).
    # ------------------------------------------------------------------

    async def _maybe_emit_embeddings(self) -> None:
        """Run the embedding-backfill batch if enough wall-clock time
        has passed since the last cycle. ``RECALL_EMBEDDING_BACKFILL_INTERVAL_S``
        is the gate; the Curator's main tick cadence may be faster."""
        from substrate import config as _cfg

        now = time.monotonic()
        if (now - self._last_embed_backfill_at) < _cfg.RECALL_EMBEDDING_BACKFILL_INTERVAL_S:
            return
        self._last_embed_backfill_at = now
        await self._emit_embeddings_for_unembedded()

    async def _emit_embeddings_for_unembedded(self) -> None:
        """One backfill pass: read up to ``RECALL_EMBEDDING_BACKFILL_BATCH_SIZE``
        unembedded passed slices, batch-call the embedding client,
        persist each result via ``SliceRepo.set_embedding``.

        Per-slice failures (None vector returned, or set_embedding
        raised) increment the in-process retry counter; once a slice
        hits ``RECALL_EMBEDDING_BACKFILL_MAX_RETRIES`` consecutive
        failures it's marked ``embedding_failed=true`` in metadata and
        the next ``list_unembedded`` excludes it.
        """
        from substrate import config as _cfg

        import thoth_db

        async with thoth_db.connection() as conn:
            rows = await self._substrate.slices.list_unembedded(
                conn, limit=_cfg.RECALL_EMBEDDING_BATCH_SIZE
            )
        if not rows:
            return

        texts = [_extract_text_for_embedding(r["payload"]) for r in rows]
        # ``RECALL_EMBEDDING_MODEL`` is an OVERRIDE knob (see
        # ``substrate/config.py``). When unset (the default) we pass
        # ``model=None`` so ``embed()`` reads ``auxiliary.embedding.model``
        # from the operator's config.yaml — without that the Curator
        # would silently force the OpenAI model name on Ollama / Voyage /
        # any non-OpenAI provider, and every embed call would 404.
        embed_kwargs = {"timeout_ms": _cfg.RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS}
        if _cfg.RECALL_EMBEDDING_MODEL is not None:
            embed_kwargs["model"] = _cfg.RECALL_EMBEDDING_MODEL
        try:
            vectors = await embed(texts, **embed_kwargs)
        except Exception as exc:
            self._log.warning("curator embed batch raised: %s", exc)
            # Whole-batch failure: bump each slice's retry counter.
            for r in rows:
                self._record_embed_failure(r["slice_id"])
            await self._persist_failures_if_maxed(rows)
            return

        async with thoth_db.connection() as conn:
            async with conn.transaction():
                for row, vec in zip(rows, vectors):
                    if vec is None:
                        self._record_embed_failure(row["slice_id"])
                        continue
                    try:
                        await self._substrate.slices.set_embedding(
                            conn, row["slice_id"], vec
                        )
                        self._reset_embed_failure(row["slice_id"])
                    except Exception as exc:
                        self._log.warning(
                            "curator set_embedding for %s failed: %s",
                            row["slice_id"],
                            exc,
                        )
                        self._record_embed_failure(row["slice_id"])
            # Persist failures outside the embedding transaction so a
            # bad slice can't block the rest of the batch from landing.
            await self._persist_failures_if_maxed(rows)

    async def _maybe_retry_failed_embeddings(self) -> None:
        """Auto-heal: un-park a small batch of ``embedding_failed`` slices so a
        fixed embedding config recovers on its own.

        A slice that exhausts its retry budget is parked (``embedding_failed``)
        and excluded from ``list_unembedded`` forever — that's deliberate, so a
        broken provider isn't hammered. But nothing un-parks it once the
        operator fixes the cause, so the whole backlog can sit at 0% coverage
        indefinitely. This clears a bounded batch on a long interval: the next
        ``_emit_embeddings_for_unembedded`` re-attempts them. If the config is
        now healthy they embed and stay embedded; if it's still broken they
        re-park (one small probe batch per interval — negligible load).

        Only probes when the fresh backlog is empty, so it never competes with
        normal first-time embedding. Interval of 0 disables it.
        """
        from substrate import config as _cfg

        interval = _cfg.RECALL_EMBEDDING_RETRY_FAILED_INTERVAL_S
        if interval <= 0:
            return
        now = time.monotonic()
        if (now - self._last_retry_failed_at) < interval:
            return
        self._last_retry_failed_at = now

        import thoth_db

        async with thoth_db.connection() as conn:
            # Don't compete with first-time embedding — only probe when the
            # normal queue is drained.
            fresh = await self._substrate.slices.list_unembedded(conn, limit=1)
            if fresh:
                return
            cleared = await self._substrate.slices.reset_embedding_failed(
                conn, limit=_cfg.RECALL_EMBEDDING_BATCH_SIZE
            )
        if cleared:
            self._log.info(
                "curator embedding auto-heal: un-parked %d failed slice(s) "
                "for re-embedding",
                cleared,
            )

    # ------------------------------------------------------------------
    # Upper-layer (L3/L4) curation — embed → merge near-dupes → decay →
    # release. The same decay/release lifecycle the Curator runs for L0,
    # extended to patterns + self-model observations, plus semantic merge
    # to collapse the LLM's reworded near-duplicates that exact-text dedup
    # can't catch.
    # ------------------------------------------------------------------

    async def _maybe_curate_upper_layers(self) -> None:
        now = time.monotonic()
        if (now - self._last_curate_upper_at) < self.CURATE_UPPER_INTERVAL_S:
            return
        self._last_curate_upper_at = now
        try:
            await self._embed_backfill_upper(l3, "l3")
            await self._embed_backfill_upper(l4, "l4")
            await self._merge_l3()
            await self._merge_l4()
            await l3.decay(half_life_seconds=self.L3_HALF_LIFE_SECONDS)
            await l4.decay(half_life_seconds=self.L4_HALF_LIFE_SECONDS)
            await l3.release_stale(
                floor=self.UPPER_RELEASE_FLOOR, stale_seconds=self.UPPER_STALE_SECONDS
            )
            await l4.release_stale(
                floor=self.UPPER_RELEASE_FLOOR, stale_seconds=self.UPPER_STALE_SECONDS
            )
        except Exception:
            # Best-effort enrichment — never crash the Curator tick.
            self._log.debug("curator.curate_upper.degraded", exc_info=True)

    async def _embed_backfill_upper(self, store, label: str) -> None:
        """Embed unembedded L3/L4 statements so the merge pass can compare
        them. Mirrors the L0 slice backfill; failures just retry next pass."""
        from substrate import config as _cfg

        rows = await store.list_unembedded(limit=_cfg.RECALL_EMBEDDING_BATCH_SIZE)
        if not rows:
            return
        texts = [(r["statement"] or "") for r in rows]
        embed_kwargs = {"timeout_ms": _cfg.RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS}
        if _cfg.RECALL_EMBEDDING_MODEL is not None:
            embed_kwargs["model"] = _cfg.RECALL_EMBEDDING_MODEL
        try:
            vectors = await embed(texts, **embed_kwargs)
        except Exception as exc:
            self._log.warning("curator %s embed batch raised: %s", label, exc)
            return
        for r, vec in zip(rows, vectors):
            if vec is None:
                continue
            try:
                await store.set_embedding(r["id"], vec)
            except Exception:
                self._log.debug(
                    "curator %s set_embedding failed", label, exc_info=True
                )

    @staticmethod
    def _pick_canonical(cluster: list[dict]) -> dict:
        """Recency-weighted canonical: the most recently re-found entry wins
        (so an updated value supersedes a stale near-duplicate), tie-broken by
        salience."""
        return max(cluster, key=lambda r: (r["last_seen_at"], r["salience_score"]))

    async def _merge_l3(self) -> None:
        seeds = await l3.list_merge_seeds(limit=self.UPPER_MERGE_SEEDS_PER_PASS)
        absorbed: set = set()
        for seed in seeds:
            if seed["id"] in absorbed:
                continue
            # Within-kind at the (looser) main threshold…
            dups = await l3.find_near_duplicates(
                seed["id"],
                max_distance=self.UPPER_MERGE_MAX_DISTANCE,
                limit=self.UPPER_MERGE_NEIGHBORS,
            )
            # …plus near-identical matches ACROSS kinds (the same fact stored
            # as generalization + theme + recurring_structure), at a tighter
            # distance. 0 disables cross-kind merging.
            if self.UPPER_MERGE_CROSS_KIND_MAX_DISTANCE > 0:
                seen = {d["id"] for d in dups}
                cross = await l3.find_near_duplicates(
                    seed["id"],
                    max_distance=self.UPPER_MERGE_CROSS_KIND_MAX_DISTANCE,
                    limit=self.UPPER_MERGE_NEIGHBORS,
                    same_kind=False,
                )
                dups += [d for d in cross if d["id"] not in seen]
            dups = [d for d in dups if d["id"] not in absorbed]
            if not dups:
                continue
            cluster = [seed] + dups
            canonical = self._pick_canonical(cluster)
            victims = [r for r in cluster if r["id"] != canonical["id"]]
            cites = {str(c) for r in cluster for c in (r.get("cites") or [])}
            salience = min(1.0, max(r["salience_score"] for r in cluster) + 0.05)
            confidence = max(float(r.get("confidence") or 0.5) for r in cluster)
            await l3.apply_merge(
                canonical["id"], cites=sorted(cites),
                salience=salience, confidence=confidence,
            )
            await l3.delete_patterns([v["id"] for v in victims])
            absorbed.update(v["id"] for v in victims)
            absorbed.add(canonical["id"])

    async def _merge_l4(self) -> None:
        seeds = await l4.list_merge_seeds(limit=self.UPPER_MERGE_SEEDS_PER_PASS)
        absorbed: set = set()
        for seed in seeds:
            if seed["id"] in absorbed:
                continue
            dups = await l4.find_near_duplicates(
                seed["id"],
                max_distance=self.UPPER_MERGE_MAX_DISTANCE,
                limit=self.UPPER_MERGE_NEIGHBORS,
            )
            dups = [d for d in dups if d["id"] not in absorbed]
            if not dups:
                continue
            cluster = [seed] + dups
            canonical = self._pick_canonical(cluster)
            victims = [r for r in cluster if r["id"] != canonical["id"]]
            salience = min(1.0, max(r["salience_score"] for r in cluster) + 0.05)
            await l4.apply_merge(
                canonical["id"], salience=salience, score=canonical.get("score"),
            )
            await l4.delete_observations([v["id"] for v in victims])
            absorbed.update(v["id"] for v in victims)
            absorbed.add(canonical["id"])

    def _record_embed_failure(self, slice_id: UUID) -> None:
        self._embed_failure_counts[slice_id] = (
            self._embed_failure_counts.get(slice_id, 0) + 1
        )

    def _reset_embed_failure(self, slice_id: UUID) -> None:
        self._embed_failure_counts.pop(slice_id, None)

    async def _persist_failures_if_maxed(self, rows: list[dict]) -> None:
        """For each slice whose failure count has reached the cap,
        persist metadata.embedding_failed=true and drop the in-process
        counter."""
        from substrate import config as _cfg

        import thoth_db

        to_persist: list[UUID] = []
        cap = _cfg.RECALL_EMBEDDING_BACKFILL_MAX_RETRIES
        for r in rows:
            sid = r["slice_id"]
            count = self._embed_failure_counts.get(sid, 0)
            if count >= cap:
                to_persist.append(sid)
        if not to_persist:
            return
        async with thoth_db.transaction() as conn:
            for sid in to_persist:
                try:
                    await self._substrate.slices.mark_embedding_failed(conn, sid)
                except Exception as exc:
                    self._log.warning(
                        "mark_embedding_failed for %s raised: %s", sid, exc
                    )
                # Drop the counter regardless — the DB marker is now
                # authoritative for whether this slice gets retried.
                self._embed_failure_counts.pop(sid, None)

    async def _emit_alarm_audit(self, alarmed: list[dict]) -> None:
        """Record one ``curator.pathological_forgetting_alarm`` telemetry
        row per alarmed slice. Operational telemetry (``substrate_telemetry``),
        not a perceptual slice."""
        if not alarmed:
            return

        from substrate.telemetry import write as telemetry_write

        for a in alarmed:
            await telemetry_write(
                self._substrate,
                agent="curator",
                event="curator.pathological_forgetting_alarm",
                payload={
                    "slice_id": str(a["slice_id"]),
                    "stream_id": str(a["stream_id"]),
                    "age_seconds": a["age_seconds"],
                    "consolidation_window_seconds": a["window_seconds"],
                    "bumped_to": a["bumped_to"],
                },
            )

    async def _emit_stranded_audit(self, stranded: list[dict]) -> None:
        """Record one ``curator.slice_stranded`` telemetry row per drained
        slice (issue #287). One-shot by construction: the drain flips the
        slice to ``consolidated``, so it can never be re-selected."""
        if not stranded:
            return

        from substrate.telemetry import write as telemetry_write

        for s in stranded:
            await telemetry_write(
                self._substrate,
                agent="curator",
                event="curator.slice_stranded",
                payload={
                    "slice_id": str(s["slice_id"]),
                    "stream_id": str(s["stream_id"]),
                    "age_seconds": s["age_seconds"],
                    "session_id": s["session_id"],
                    "action": "tombstoned_empty_consolidation",
                },
            )


async def embed_backfill_batch(texts):
    """Embed a batch of slice texts with the backfill timeout + optional
    model override — the single embedding-call path shared by the Curator's
    async emit loop (``_emit_embeddings_for_unembedded``) and the standalone
    ``substrate.recall.backfill.backfill_unembedded_slices`` primitive.

    Kept as one function so the grading harness (which drives the standalone
    backfill inline between turns) embeds via byte-identical logic to
    production's async Curator: same ``embed()`` entry point, same timeout,
    same model-override semantics.

    ``RECALL_EMBEDDING_MODEL`` is an OVERRIDE knob (see ``substrate/config.py``).
    When unset (the default) we pass ``model=None`` so ``embed()`` reads
    ``auxiliary.embedding.model`` from the operator's config.yaml — without
    that the Curator would silently force the OpenAI model name on Ollama /
    Voyage / any non-OpenAI provider, and every embed call would 404.

    Uses the generous backfill timeout, NOT the interactive recall-query
    timeout — a slow local model would otherwise time out on every batch.
    Returns one vector-or-None per input (``embed`` never raises on provider
    failure; it returns ``[None, ...]``).
    """
    from substrate import config as _cfg

    embed_kwargs = {"timeout_ms": _cfg.RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS}
    if _cfg.RECALL_EMBEDDING_MODEL is not None:
        embed_kwargs["model"] = _cfg.RECALL_EMBEDDING_MODEL
    return await embed(texts, **embed_kwargs)


def _extract_text_for_embedding(payload) -> str:
    """Best-effort text extraction for the embedding API.

    * ``str`` (already-unwrapped text-modality payload) passes through.
    * ``{"text": "..."}`` (text-modality JSONB envelope from the L0
      writer) unwraps to bare string.
    * Other dicts (structured events) are JSON-serialised with
      deterministic key ordering so retries on the same payload
      embed identical text.
    * Anything else is str()'d as a fallback.

    Empty / whitespace-only output is allowed — the embedding API
    handles short strings; the result will be a degenerate but unit
    vector. The recall pipeline degrades gracefully (cosine of a
    degenerate vector against any query is just whatever the model
    produces; the ranker handles it).
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        text_field = payload.get("text")
        if isinstance(text_field, str):
            return text_field
        import json

        try:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except Exception:
            return str(payload)
    return str(payload)


__all__ = ["Curator"]
