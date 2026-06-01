"""Skill suggestion — match the substrate's current context to bundled skills.

Suggest bundled skills relevant to the substrate's current context —
useful when working in a domain with established procedures. Skills are
self-describing (``skills/**/SKILL.md`` with ``name`` / ``description`` /
``metadata.hermes.tags`` frontmatter); this scans them once (cached) and
ranks them against a context string (the recall query + whatever entities/
patterns the caller folds in) by keyword overlap.

Pure + dependency-light: filesystem scan + token overlap, no LLM. Powers
``thoth substrate skills <query>`` and an opt-in recall section.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Optional

from substrate.recall.projection import _tokenise


def _skills_root() -> Path:
    """The bundled-skills directory. ``HERMES_SKILLS_ROOT`` overrides (used
    by tests + non-standard installs); default is the repo's ``skills/``."""
    env = (os.environ.get("HERMES_SKILLS_ROOT") or "").strip()
    if env:
        return Path(env)
    # substrate/skills_match.py → substrate/ → repo root → repo/skills
    return Path(__file__).resolve().parent.parent / "skills"


def _parse_frontmatter(path: Path) -> dict:
    """Extract name / description / tags from a SKILL.md YAML frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    try:
        import yaml

        data = yaml.safe_load(block) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    tags = []
    meta = data.get("metadata")
    if isinstance(meta, dict):
        hermes = meta.get("hermes")
        if isinstance(hermes, dict) and isinstance(hermes.get("tags"), list):
            tags = [str(t) for t in hermes["tags"]]
    return {
        "name": str(data.get("name") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "tags": tags,
    }


@functools.lru_cache(maxsize=8)
def scan_skills(root_str: str) -> tuple:
    """Return a tuple of skill dicts (name, description, tags, path, tokens)
    for every ``SKILL.md`` under ``root_str``. Cached per root; tuple so the
    result is hashable/immutable. Returns ``()`` if the root is absent."""
    root = Path(root_str)
    if not root.is_dir():
        return ()
    skills = []
    for skill_md in sorted(root.glob("**/SKILL.md")):
        meta = _parse_frontmatter(skill_md)
        if not meta.get("name"):
            continue
        blob = " ".join(
            [meta["name"], meta["description"], " ".join(meta["tags"])]
        )
        skills.append(
            {
                "name": meta["name"],
                "description": meta["description"],
                "tags": meta["tags"],
                "path": str(skill_md.parent),
                "_tokens": frozenset(_tokenise(blob)),
            }
        )
    return tuple(skills)


def suggest_skills(
    context: str,
    *,
    root: Optional[str] = None,
    limit: int = 3,
    min_overlap: int = 2,
) -> list[dict]:
    """Rank bundled skills against *context* by keyword overlap. Returns up
    to ``limit`` skills (name, description, tags, path, overlap) whose token
    overlap with the context is at least ``min_overlap``. Empty when nothing
    clears the bar — so a callsite can simply skip an empty result."""
    ctx_tokens = _tokenise(context or "")
    if not ctx_tokens:
        return []
    catalog = scan_skills(root or str(_skills_root()))
    scored = []
    for s in catalog:
        overlap = len(ctx_tokens & s["_tokens"])
        if overlap >= min_overlap:
            scored.append((overlap, s))
    scored.sort(key=lambda t: (-t[0], t[1]["name"]))
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "tags": s["tags"],
            "path": s["path"],
            "overlap": overlap,
        }
        for overlap, s in scored[:limit]
    ]


__all__ = ["suggest_skills", "scan_skills"]
