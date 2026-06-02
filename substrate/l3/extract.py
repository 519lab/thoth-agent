"""Pattern-finder LLM extraction — L1 context → ``PatternResult``.

Mirrors the Parser's shape (auxiliary chat client, structured output) but
for higher-order patterns. Two tiers — JSON-schema ``response_format`` then
plain-prompt+parse — are enough here; the full 3-tier tool-calling dance
lives in the Parser and isn't duplicated. Offline-testable by mocking
:func:`call_pattern_llm`.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from substrate.l1.extract import _strip_fences  # shared fence/JSON cleanup
from substrate.l3.schema import ParsedPattern, PatternResult, normalise_kind


class PatternError(Exception):
    """Malformed Pattern-finder output."""


_PATTERN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "statement": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["generalization", "theme", "recurring_structure", "other"],
                    },
                    "entity_names": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["statement", "kind"],
            },
        }
    },
    "required": ["patterns"],
}


def _build_prompt(context: str) -> str:
    return (
        "You are reviewing structured knowledge a Hermes Agent has accumulated "
        "about its world — named entities and the relationships between them. "
        "Identify higher-order PATTERNS: generalizations (a recurring trait of "
        "an entity), themes (a recurring topic across entities), or recurring "
        "structures (a repeated relational shape). Only state patterns the "
        "evidence supports; do not invent.\n\n"
        f"Knowledge:\n{context}\n\n"
        "Return a JSON object with a `patterns` array matching this schema:\n"
        f"{json.dumps(_PATTERN_SCHEMA)}\n\n"
        "Each pattern: a one-sentence `statement`, a `kind`, the `entity_names` "
        "it generalizes from (drawn from the knowledge above), and a "
        "`confidence` in [0,1]. If nothing generalizes, return an empty array."
    )


def resolve_pattern_client():
    """Resolve ``(async_client, model)`` for the pattern-finder task."""
    from agent.auxiliary_client import get_async_text_auxiliary_client

    return get_async_text_auxiliary_client("pattern_finder")


def _coerce(data, ) -> PatternResult:
    if not isinstance(data, dict):
        raise PatternError("pattern output is not a JSON object")
    raw = data.get("patterns")
    if not isinstance(raw, list):
        raise PatternError("`patterns` must be an array")
    out = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("statement"):
            continue
        try:
            conf = float(p.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out.append(
            ParsedPattern(
                statement=str(p["statement"]).strip()[:500],
                kind=normalise_kind(p.get("kind")),
                entity_names=[str(n) for n in (p.get("entity_names") or []) if n],
                confidence=max(0.0, min(1.0, conf)),
            )
        )
    return PatternResult(patterns=out)


async def call_pattern_llm(
    context: str, *, client=None, model: Optional[str] = None, substrate=None
) -> PatternResult:
    from substrate import cost

    if not (context or "").strip():
        return PatternResult()
    if client is None:
        client, model = resolve_pattern_client()
        if client is None:
            raise RuntimeError("no pattern-finder auxiliary client configured")
    prompt = _build_prompt(context)
    temp = float(os.environ.get("PATTERNFINDER_TEMPERATURE", "0.3") or "0.3")

    # Tier 1: JSON-schema response_format. Tier fallback: plain prompt.
    raw = ""
    try:
        resp = await cost.acreate_and_record(
            client,
            substrate=substrate,
            agent="pattern-finder",
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "patterns", "schema": _PATTERN_SCHEMA},
            },
            temperature=temp,
        )
        raw = resp.choices[0].message.content or ""
    except Exception:
        resp = await cost.acreate_and_record(
            client,
            substrate=substrate,
            agent="pattern-finder",
            model=model,
            messages=[{
                "role": "user",
                "content": prompt + "\n\nReturn ONLY the JSON object, no prose.",
            }],
            temperature=temp,
        )
        raw = resp.choices[0].message.content or ""

    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise PatternError(f"invalid JSON from pattern model: {exc}") from exc
    return _coerce(data)


__all__ = ["PatternError", "call_pattern_llm", "resolve_pattern_client", "_coerce"]
