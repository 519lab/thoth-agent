"""Parser LLM extraction — slices → ``ParserResult``.

Routes through Thoth's existing auxiliary-client chain
(``agent/auxiliary_client.py`` ``get_async_text_auxiliary_client("parser")``)
so any configured provider works — OpenRouter, Nous, OpenAI, Anthropic, or
any OpenAI-compatible local endpoint (Ollama, vLLM, llama.cpp…).

Not every endpoint exposes the same JSON-forcing machinery, so
:func:`_call_with_structured_output` walks three tiers and memoises the
highest one that worked per model (Phase D spec §4.6):

  1. JSON-Schema response_format
  2. tool/function calling
  3. plain prompt + parse (with a corrective retry)

Validation is intentionally hand-rolled (no hard pydantic dependency): a
malformed payload raises :class:`ParseError`, which the Parser maps to the
``parse_error`` outcome and still consolidates the source slices (so the
same bad input isn't re-processed forever).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from substrate.l1.schema import (
    ParsedEntity,
    ParsedRelationship,
    ParserResult,
    normalise_entity_type,
)


class ParseError(Exception):
    """Raised when the LLM response cannot be coerced into a ParserResult."""


@dataclass(frozen=True)
class SliceText:
    """One slice handed to the Parser: its id, the stream it came from, and
    the (already-decoded) text the LLM reads."""

    slice_id: UUID
    stream_name: str
    text: str


# JSON schema describing the extraction object — used verbatim for tier 1
# (response_format) and tier 2 (tool parameters), and documented in the
# tier-3 prompt.
_PARSER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "entity_type": {
                        "type": "string",
                        "enum": [
                            "person", "project", "file", "concept",
                            "place", "org", "other",
                        ],
                    },
                    "summary": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "source_slice_ids": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                },
                "required": ["name", "entity_type"],
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject_name": {"type": "string"},
                    "subject_type": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object_name": {"type": "string"},
                    "object_type": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_slice_ids": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                },
                "required": ["subject_name", "predicate", "object_name"],
            },
        },
    },
    "required": ["entities", "relationships"],
}

_TOOL_NAME = "emit_extraction"

# Memoised highest working tier per model slug. First call probes 1→2→3;
# subsequent calls skip to the memoised tier. Cleared by reset_tier_cache()
# in tests.
_tier_cache: dict[str, int] = {}


def reset_tier_cache() -> None:
    _tier_cache.clear()


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _build_prompt(slices: list[SliceText]) -> str:
    """The extraction prompt. Slices are shown most-recent-first with a
    short id so the model can cite them in ``source_slice_ids``."""
    lines = []
    for s in slices:
        short = str(s.slice_id)[:8]
        text = (s.text or "").replace("\n", " ")[:400]
        lines.append(f'[{short}] ({s.stream_name}) "{text}"')
    slice_block = "\n".join(lines)
    return (
        f"You are reading {len(slices)} messages from a Thoth Agent "
        "conversation. Identify named entities (people, projects, files, "
        "concepts, places, organizations) and the relationships between them "
        "that are EXPLICITLY mentioned. Do not infer.\n\n"
        "Messages (most-recent first):\n"
        f"{slice_block}\n\n"
        "Return a JSON object with two arrays, `entities` and "
        "`relationships`, matching this schema:\n"
        f"{json.dumps(_PARSER_JSON_SCHEMA)}\n\n"
        "Rules: use the 8-char ids above in source_slice_ids; for each "
        "relationship both subject and object must also appear in entities; "
        "if nothing is extractable, return empty arrays."
    )


def _short_id_map(slices: list[SliceText]) -> dict[str, UUID]:
    return {str(s.slice_id)[:8]: s.slice_id for s in slices}


# ---------------------------------------------------------------------------
# 3-tier structured-output call
# ---------------------------------------------------------------------------


async def _create(client, *, substrate=None, **kwargs):
    """Single chat-completion call. Isolated so tests can assert kwargs.

    Routes through ``substrate.cost.acreate_and_record`` so per-agent token
    usage is recorded (no-op recording when ``substrate`` is None)."""
    from substrate import cost

    return await cost.acreate_and_record(
        client, substrate=substrate, agent="parser", **kwargs
    )


async def _tier1(client, model, prompt, substrate=None) -> str:
    resp = await _create(
        client,
        substrate=substrate,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": _PARSER_JSON_SCHEMA},
        },
        temperature=float(os.environ.get("PARSER_TEMPERATURE", "0.2") or "0.2"),
    )
    return resp.choices[0].message.content or ""


async def _tier2(client, model, prompt, substrate=None) -> str:
    resp = await _create(
        client,
        substrate=substrate,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "type": "function",
            "function": {"name": _TOOL_NAME, "parameters": _PARSER_JSON_SCHEMA},
        }],
        tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
        temperature=float(os.environ.get("PARSER_TEMPERATURE", "0.2") or "0.2"),
    )
    tool_calls = resp.choices[0].message.tool_calls or []
    if not tool_calls:
        raise ParseError("tier-2: model returned no tool call")
    return tool_calls[0].function.arguments or ""


async def _tier3(client, model, prompt, substrate=None) -> str:
    retries = _env_int("PARSER_JSON_RETRIES", 2)
    msg = prompt + (
        "\n\nRespond with ONLY a JSON object matching the schema above. "
        "No prose, no markdown fences."
    )
    last_raw = ""
    for attempt in range(retries + 1):
        resp = await _create(
            client,
            substrate=substrate,
            model=model,
            messages=[{"role": "user", "content": msg}],
            temperature=float(os.environ.get("PARSER_TEMPERATURE", "0.2") or "0.2"),
        )
        last_raw = resp.choices[0].message.content or ""
        try:
            json.loads(_strip_fences(last_raw))
            return _strip_fences(last_raw)
        except (json.JSONDecodeError, ValueError):
            msg = (
                prompt + "\n\nYour previous response was not valid JSON. "
                "Return only a JSON object, no prose."
            )
    return _strip_fences(last_raw)  # let the caller's json.loads raise ParseError


def _strip_fences(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        # drop a leading "json" language tag line if present
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


_TIERS = {1: _tier1, 2: _tier2, 3: _tier3}


async def _call_with_structured_output(
    client, model: str, prompt: str, substrate=None
) -> str:
    """Walk tiers 1→2→3, memoising the first that works for *model*.

    An explicit ``PARSER_STRUCTURED_OUTPUT_TIER`` (1–3) pins the tier and
    skips probing — useful for local models that mis-advertise tool support.
    """
    pinned = _env_int("PARSER_STRUCTURED_OUTPUT_TIER", 0)
    if pinned in (1, 2, 3):
        return await _TIERS[pinned](client, model, prompt, substrate)

    start = _tier_cache.get(model, 1)
    last_exc: Optional[Exception] = None
    for tier in (t for t in (1, 2, 3) if t >= start):
        try:
            raw = await _TIERS[tier](client, model, prompt, substrate)
            _tier_cache[model] = tier
            return raw
        except Exception as exc:  # demote to the next tier
            last_exc = exc
            continue
    raise ParseError(f"all structured-output tiers failed for {model}: {last_exc}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _coerce_result(data: Any, id_map: dict[str, UUID]) -> ParserResult:
    if not isinstance(data, dict):
        raise ParseError("extraction is not a JSON object")
    raw_entities = data.get("entities") or []
    raw_rels = data.get("relationships") or []
    if not isinstance(raw_entities, list) or not isinstance(raw_rels, list):
        raise ParseError("entities/relationships must be arrays")

    def _map_ids(short_ids) -> list[UUID]:
        out = []
        for sid in short_ids or []:
            full = id_map.get(str(sid)[:8])
            if full is not None:
                out.append(full)
        return out

    entities = []
    for e in raw_entities:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        entities.append(
            ParsedEntity(
                name=str(e["name"]).strip(),
                entity_type=normalise_entity_type(e.get("entity_type")),
                summary=str(e.get("summary") or "")[:200],
                aliases=[str(a) for a in (e.get("aliases") or []) if a],
                source_slice_ids=_map_ids(e.get("source_slice_ids")),
                quote=str(e.get("quote") or "")[:500],
            )
        )

    relationships = []
    for r in raw_rels:
        if not isinstance(r, dict) or not (r.get("subject_name") and r.get("object_name") and r.get("predicate")):
            continue
        try:
            conf = float(r.get("confidence", 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        relationships.append(
            ParsedRelationship(
                subject_name=str(r["subject_name"]).strip(),
                subject_type=normalise_entity_type(r.get("subject_type")),
                predicate=str(r["predicate"]).strip().lower()[:80],
                object_name=str(r["object_name"]).strip(),
                object_type=normalise_entity_type(r.get("object_type")),
                confidence=max(0.0, min(1.0, conf)),
                source_slice_ids=_map_ids(r.get("source_slice_ids")),
                quote=str(r.get("quote") or "")[:500],
            )
        )

    return ParserResult(entities=entities, relationships=relationships)


def resolve_parser_client():
    """Resolve ``(async_client, model)`` for the Parser task via Thoth's
    auxiliary-client chain. Returns ``(None, None)`` when no provider is
    configured. Kept as a thin seam so the Parser can log the model and
    tests can inject a dummy client without monkeypatching the whole
    auxiliary-client module."""
    from agent.auxiliary_client import get_async_text_auxiliary_client

    return get_async_text_auxiliary_client("parser")


async def call_parser_llm(
    slices: list[SliceText], *, client=None, model: Optional[str] = None,
    substrate=None,
) -> ParserResult:
    """Extract entities + relationships from *slices*. Raises ParseError on
    malformed output; raises other exceptions on transport failures (the
    Parser maps those to ``llm_error``).

    ``substrate`` (optional) threads through to the create site so per-agent
    token usage is recorded; ``None`` skips recording (back-compatible)."""
    if not slices:
        return ParserResult()
    if client is None:
        from agent.auxiliary_client import get_async_text_auxiliary_client

        client, model = get_async_text_auxiliary_client("parser")
        if client is None:
            raise RuntimeError("no parser auxiliary client configured")
    prompt = _build_prompt(slices)
    raw = await _call_with_structured_output(client, model or "", prompt, substrate)
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ParseError(f"invalid JSON from parser model: {exc}") from exc
    return _coerce_result(data, _short_id_map(slices))


__all__ = [
    "ParseError",
    "SliceText",
    "call_parser_llm",
    "resolve_parser_client",
    "reset_tier_cache",
    "_call_with_structured_output",
    "_build_prompt",
    "_coerce_result",
]
