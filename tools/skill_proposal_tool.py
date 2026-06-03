"""``skill_proposal`` tool — the in-chat review gate for self-authored skills.

The substrate's SkillScout drafts skills from recurring needs in memory and
stages them as ``pending`` proposals (see
``docs/plans/2026-05-28-substrate-self-improvement-forge.md``). This tool is
how the user reviews and decides them in chat:

    skill_proposal list                 — pending (and recent) proposals
    skill_proposal show   <slug>        — the staged SKILL.md + provenance
    skill_proposal approve <slug>       — install it (the human gate)
    skill_proposal reject  <slug>       — discard it (won't be re-proposed)

Approval is the ONLY path from a draft to an installed skill. It goes through
``skill_manage(action="create")`` so the draft gets the same frontmatter
validation + security scan + collision check as any hand-written skill, then
marks it agent-created so the existing skills Curator (``agent/curator.py``)
maintains its lifecycle.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# --- async DB bridge -------------------------------------------------------

def _run(coro):
    """Drive an async store coro from this sync tool. Ensures the asyncpg pool
    is bootstrapped (``run_sync`` deliberately doesn't lazy-init it)."""
    import thoth_db

    thoth_db.ensure_pool_sync()
    return thoth_db.run_sync(coro)


async def _emit_telemetry(event: str, payload: dict) -> None:
    """Append a row to ``substrate_telemetry`` directly (the tool has no
    Substrate handle, so we don't go through ``substrate.telemetry.write``)."""
    import thoth_db

    async with thoth_db.connection() as conn:
        await conn.execute(
            "INSERT INTO substrate_telemetry (agent, event, payload, at) "
            "VALUES ($1, $2, $3, now())",
            "skill-scout",
            event,
            payload or {},
        )


def _provenance_lines(p) -> str:
    parts = [
        f"status: {p.status}",
        f"salience: {p.salience:.2f}",
    ]
    if p.source_l3_ids:
        parts.append(f"from {len(p.source_l3_ids)} L3 pattern(s): "
                     + ", ".join(p.source_l3_ids[:5]))
    if p.source_l4_ids:
        parts.append(f"from {len(p.source_l4_ids)} L4 observation(s)")
    if p.decided_at:
        parts.append(f"decided: {p.decided_at:%Y-%m-%d %H:%M} by {p.decided_by or '?'}")
    return " | ".join(parts)


# --- actions ---------------------------------------------------------------

def _do_list() -> str:
    from substrate.skill_proposals import store

    proposals = _run(store.list_proposals(limit=50))
    items = [
        {
            "slug": p.slug,
            "title": p.title,
            "status": p.status,
            "salience": round(p.salience, 2),
            "rationale": p.rationale,
            "eval_verdict": p.eval_verdict,  # pass | flag | reject | null
        }
        for p in proposals
    ]
    pending = sum(1 for p in proposals if p.status == "pending")
    return json.dumps(
        {"success": True, "pending": pending, "count": len(items), "proposals": items},
        ensure_ascii=False,
    )


def _do_show(slug: str) -> str:
    from substrate.skill_proposals import store

    p = _run(store.get_proposal(slug))
    if p is None:
        return json.dumps({"success": False, "error": f"No proposal '{slug}'."})
    out = {
        "success": True,
        "slug": p.slug,
        "title": p.title,
        "rationale": p.rationale,
        "provenance": _provenance_lines(p),
        "status": p.status,
        "skill_md": p.draft_content,
    }
    if p.eval_verdict:
        # The evaluator's second-opinion verdict (advisory). Surface it so the
        # user can weigh it before approving — but it never blocks approval.
        out["evaluation"] = {
            "verdict": p.eval_verdict,           # pass | flag | reject
            "reasons": p.eval_reasons or [],
            "model": p.eval_model,
        }
    return json.dumps(out, ensure_ascii=False)


def _do_approve(slug: str) -> str:
    from substrate.skill_proposals import store

    p = _run(store.get_proposal(slug))
    if p is None:
        return json.dumps({"success": False, "error": f"No proposal '{slug}'."})
    if p.status != "pending":
        return json.dumps(
            {"success": False, "error": f"Proposal '{slug}' is already {p.status}."}
        )

    # The human gate: promote the draft via skill_manage, which validates the
    # frontmatter, security-scans the skill, and refuses on name collision.
    from tools.skill_manager_tool import skill_manage

    raw = skill_manage(action="create", name=p.slug, content=p.draft_content)
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        result = {"success": False, "error": "skill_manage returned malformed output"}
    if not result.get("success"):
        # Do NOT flip status — leave it pending so it can be fixed/retried.
        return json.dumps(
            {"success": False, "error": f"Install failed: {result.get('error')}"}
        )

    # Mark agent-created so the existing skills Curator adopts its lifecycle.
    try:
        from tools.skill_usage import mark_agent_created

        mark_agent_created(p.slug)
    except Exception:
        logger.debug("skill_proposal: mark_agent_created failed", exc_info=True)

    _run(store.set_status(slug, "approved", by="user"))

    # Best-effort: refresh the slash-command map so /<slug> works immediately
    # (skill_manage already cleared the system-prompt skills cache).
    try:
        from agent.skill_commands import reload_skills

        reload_skills()
    except Exception:
        logger.debug("skill_proposal: reload_skills failed", exc_info=True)

    try:
        _run(_emit_telemetry("skill_scout.approved", {"slug": p.slug}))
    except Exception:
        logger.debug("skill_proposal: telemetry failed", exc_info=True)

    out = {
        "success": True,
        "message": f"Approved and installed skill '{p.slug}'. Use /{p.slug}.",
        "path": result.get("path"),
    }
    # The evaluator is defense-in-depth, not a hard gate: a flagged/rejected
    # verdict doesn't block a human-approved install, but we surface it so the
    # agent can confirm the override was intentional.
    if p.eval_verdict in {"flag", "reject"}:
        why = f" ({'; '.join(p.eval_reasons)})" if p.eval_reasons else ""
        out["warning"] = (
            f"The evaluator returned '{p.eval_verdict}'{why}. Installed anyway "
            f"per your approval — double-check this was intended."
        )
    return json.dumps(out, ensure_ascii=False)


def _do_reject(slug: str) -> str:
    from substrate.skill_proposals import store

    p = _run(store.get_proposal(slug))
    if p is None:
        return json.dumps({"success": False, "error": f"No proposal '{slug}'."})
    if p.status != "pending":
        return json.dumps(
            {"success": False, "error": f"Proposal '{slug}' is already {p.status}."}
        )
    _run(store.set_status(slug, "rejected", by="user"))
    try:
        _run(_emit_telemetry("skill_scout.rejected", {"slug": p.slug}))
    except Exception:
        logger.debug("skill_proposal: telemetry failed", exc_info=True)
    return json.dumps(
        {"success": True, "message": f"Rejected proposal '{slug}'. It won't be re-proposed."}
    )


def skill_proposal(action: str, slug: Optional[str] = None) -> str:
    """Review/decide self-authored skill proposals. Returns a JSON string."""
    action = (action or "").strip().lower()
    if action == "list":
        return _do_list()
    if action in {"show", "approve", "reject"}:
        if not (slug or "").strip():
            return json.dumps(
                {"success": False, "error": f"'{action}' requires a slug."}
            )
        slug = slug.strip()
        if action == "show":
            return _do_show(slug)
        if action == "approve":
            return _do_approve(slug)
        return _do_reject(slug)
    return json.dumps(
        {"success": False, "error": f"Unknown action '{action}'. "
         "Use: list, show, approve, reject."}
    )


# --- OpenAI Function-Calling Schema ----------------------------------------

SKILL_PROPOSAL_SCHEMA = {
    "name": "skill_proposal",
    "description": (
        "Review and decide self-authored skill proposals. The substrate's "
        "SkillScout drafts new skills from recurring needs it finds in long-term "
        "memory and stages them for your approval — it never installs on its own. "
        "Use this tool when the user wants to see, inspect, approve, or reject a "
        "drafted skill (e.g. after a 'I drafted a skill …' notification).\n\n"
        "Actions:\n"
        "  list — show pending (and recent) proposals.\n"
        "  show <slug> — print the staged SKILL.md plus its provenance (which "
        "memory triggered it) so you/the user can judge it before approving.\n"
        "  approve <slug> — install the skill (validated + security-scanned via "
        "the normal skill-create path). This is the human gate; only do it on "
        "the user's say-so.\n"
        "  reject <slug> — discard the proposal; it won't be re-proposed.\n\n"
        "Always show a proposal and get the user's explicit go-ahead before "
        "approving."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "show", "approve", "reject"],
                "description": "The action to perform.",
            },
            "slug": {
                "type": "string",
                "description": "Proposal slug — required for show/approve/reject.",
            },
        },
        "required": ["action"],
    },
}


# --- Registry --------------------------------------------------------------
from tools.registry import registry

registry.register(
    name="skill_proposal",
    toolset="skills",
    schema=SKILL_PROPOSAL_SCHEMA,
    handler=lambda args, **kw: skill_proposal(
        action=args.get("action", ""),
        slug=args.get("slug"),
    ),
    emoji="💡",
)


__all__ = ["skill_proposal", "SKILL_PROPOSAL_SCHEMA"]
