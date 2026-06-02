"""Skill drafting — turn a discovered need into a proposed SKILL.md.

The SkillScout finds a recurring/important need in the upper-layer memory and
asks the auxiliary chat model to (a) decide whether it genuinely warrants a
reusable skill and (b) if so, draft a full ``SKILL.md``. The model is told it
MAY DECLINE — most memory is facts, not repeatable tasks — so a low-value
signal yields ``None`` rather than a junk skill. Mirrors
``substrate.l3.extract`` (auxiliary async client + JSON-schema response).

Final validation + a security scan happen later, at approval time, via
``skill_manage(action="create")``; this module only needs to produce
well-formed, plausible drafts (a malformed draft is dropped, not staged).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from substrate.l1.extract import _strip_fences  # shared fence/JSON cleanup

_MAX_NAME_LENGTH = 64
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class DraftedSkill:
    slug: str
    title: str
    rationale: str
    skill_md: str  # full SKILL.md (frontmatter + body)


_AUTHOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "skill_worthy": {"type": "boolean"},
        "slug": {"type": "string"},
        "title": {"type": "string"},
        "rationale": {"type": "string"},
        "skill_md": {"type": "string"},
    },
    "required": ["skill_worthy"],
}


def _slugify(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    return s[:_MAX_NAME_LENGTH]


def _build_prompt(need_context: str) -> str:
    return (
        "You are the skill-authoring component of a Hermes Agent's self-improvement "
        "system. The agent has noticed a RECURRING or IMPORTANT need in its own "
        "long-term memory (shown below). Your job is to decide whether this need "
        "warrants a reusable *skill* — a repeatable PROCEDURE the agent can follow "
        "with the tools it already has — and if so, to draft it.\n\n"
        "Be conservative. Most memory is facts or one-off context, NOT repeatable "
        "tasks. Only propose a skill for a genuinely repeated, important, or clearly "
        "useful TASK. If the need is just knowledge, a single past event, or too "
        "vague to act on, set \"skill_worthy\": false and return nothing else.\n\n"
        "If it IS skill-worthy, write a complete SKILL.md as `skill_md`:\n"
        "  - It MUST start with YAML frontmatter delimited by `---` lines, "
        "including at least `name:` (lowercase-kebab, matches your `slug`) and "
        "`description:` (one line, <1024 chars). You may add "
        "`metadata:\\n  hermes:\\n    tags: [...]`.\n"
        "  - After the closing `---`, write clear step-by-step instructions.\n"
        "  - The procedure must use only safe, ordinary capabilities. Do NOT "
        "include destructive actions, secret exfiltration, or anything that "
        "weakens the agent's own safety. A human will review before it installs.\n\n"
        "Also return a short kebab-case `slug`, a human `title`, and a one-sentence "
        "`rationale` explaining why this recurring need justifies a skill.\n\n"
        f"Discovered need (from the agent's memory):\n{need_context}\n\n"
        "Return ONLY a JSON object matching this schema:\n"
        f"{json.dumps(_AUTHOR_SCHEMA)}"
    )


def resolve_author_client():
    """Resolve ``(async_client, model)`` for the skill-author task."""
    from agent.auxiliary_client import get_async_text_auxiliary_client

    return get_async_text_auxiliary_client("skill_author")


def _looks_like_skill_md(content: str) -> bool:
    """Cheap pre-stage sanity check (the authoritative validation is at
    approval via ``skill_manage create``): frontmatter opens/closes and has a
    name + description, with a non-empty body."""
    if not content or not content.startswith("---"):
        return False
    end = re.search(r"\n---\s*\n", content[3:])
    if not end:
        return False
    fm = content[3 : end.start() + 3]
    body = content[end.end() + 3 :].strip()
    return ("name:" in fm) and ("description:" in fm) and bool(body)


def _coerce(data) -> Optional[DraftedSkill]:
    if not isinstance(data, dict):
        return None
    if not data.get("skill_worthy"):
        return None
    skill_md = str(data.get("skill_md") or "").strip()
    slug = _slugify(str(data.get("slug") or ""))
    if not slug or not _VALID_NAME_RE.match(slug):
        return None
    if not _looks_like_skill_md(skill_md):
        return None
    title = str(data.get("title") or slug).strip()[:200]
    rationale = str(data.get("rationale") or "").strip()[:1000]
    return DraftedSkill(slug=slug, title=title, rationale=rationale, skill_md=skill_md)


async def draft_skill(
    need_context: str, *, client=None, model: Optional[str] = None, substrate=None
) -> Optional[DraftedSkill]:
    """Draft a skill for ``need_context``, or ``None`` if the model declines or
    the output is unusable. Never raises on a model/parse error (returns None).

    ``substrate`` (optional) threads through to the create site so per-agent
    token usage is recorded; ``None`` skips recording (back-compatible)."""
    from substrate import cost

    if not (need_context or "").strip():
        return None
    if client is None:
        client, model = resolve_author_client()
        if client is None:
            return None
    prompt = _build_prompt(need_context)
    temp = float(os.environ.get("SKILL_SCOUT_TEMPERATURE", "0.4") or "0.4")

    raw = ""
    try:
        resp = await cost.acreate_and_record(
            client,
            substrate=substrate,
            agent="skill_author",
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "skill_proposal", "schema": _AUTHOR_SCHEMA},
            },
            temperature=temp,
        )
        raw = resp.choices[0].message.content or ""
    except Exception:
        try:
            resp = await cost.acreate_and_record(
                client,
                substrate=substrate,
                agent="skill_author",
                model=model,
                messages=[{
                    "role": "user",
                    "content": prompt + "\n\nReturn ONLY the JSON object, no prose.",
                }],
                temperature=temp,
            )
            raw = resp.choices[0].message.content or ""
        except Exception:
            return None

    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    return _coerce(data)


__all__ = ["DraftedSkill", "draft_skill", "resolve_author_client", "_slugify"]
