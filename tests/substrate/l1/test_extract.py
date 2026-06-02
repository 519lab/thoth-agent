"""Parser LLM extraction — 3-tier structured output + result coercion.

No network: a fake AsyncOpenAI-shaped client is injected, configured to
fail at a chosen tier so the fallback chain (§4.6) is exercised.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from substrate.l1 import extract
from substrate.l1.extract import SliceText


# ---------------------------------------------------------------------------
# Fake client plumbing
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _ToolCall:
    def __init__(self, arguments):
        self.function = type("F", (), {"arguments": arguments})()


class _Resp:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


class _FakeCompletions:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return self._handler(kwargs)


class _FakeClient:
    """Drives ``client.chat.completions.create(**kwargs)`` through a handler
    that inspects kwargs and returns/raises to simulate a tier."""

    def __init__(self, handler):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(handler)})()


_VALID = json.dumps(
    {
        "entities": [
            {"name": "Greg", "entity_type": "person", "summary": "maintainer",
             "source_slice_ids": ["aaaaaaaa"], "quote": "Greg"}
        ],
        "relationships": [],
    }
)


@pytest.fixture(autouse=True)
def _clear_tier_cache():
    extract.reset_tier_cache()
    yield
    extract.reset_tier_cache()


def _prompt():
    return "prompt"


@pytest.mark.asyncio
async def test_tier1_json_schema_works():
    def handler(kwargs):
        assert "response_format" in kwargs
        return _Resp(_Msg(content=_VALID))

    raw = await extract._call_with_structured_output(_FakeClient(handler), "m1", _prompt())
    assert json.loads(raw)["entities"][0]["name"] == "Greg"
    assert extract._tier_cache["m1"] == 1


@pytest.mark.asyncio
async def test_tier1_fails_demotes_to_tier2():
    def handler(kwargs):
        if "response_format" in kwargs:
            raise RuntimeError("400: response_format unsupported")
        assert "tools" in kwargs
        return _Resp(_Msg(tool_calls=[_ToolCall(_VALID)]))

    raw = await extract._call_with_structured_output(_FakeClient(handler), "m2", _prompt())
    assert json.loads(raw)["entities"][0]["name"] == "Greg"
    assert extract._tier_cache["m2"] == 2


@pytest.mark.asyncio
async def test_tier2_fails_demotes_to_tier3():
    def handler(kwargs):
        if "response_format" in kwargs:
            raise RuntimeError("no response_format")
        if "tools" in kwargs:
            raise RuntimeError("no tools")
        return _Resp(_Msg(content=_VALID))

    raw = await extract._call_with_structured_output(_FakeClient(handler), "m3", _prompt())
    assert json.loads(raw)["entities"][0]["name"] == "Greg"
    assert extract._tier_cache["m3"] == 3


@pytest.mark.asyncio
async def test_tier3_invalid_json_retries(monkeypatch):
    monkeypatch.setenv("PARSER_JSON_RETRIES", "2")
    calls = {"n": 0}

    def handler(kwargs):
        # Force tier 3 by failing 1 and 2.
        if "response_format" in kwargs or "tools" in kwargs:
            raise RuntimeError("unsupported")
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(_Msg(content="here you go: not json"))
        return _Resp(_Msg(content=_VALID))

    raw = await extract._call_with_structured_output(_FakeClient(handler), "m4", _prompt())
    assert json.loads(raw)["entities"][0]["name"] == "Greg"
    assert calls["n"] == 2  # retried once


@pytest.mark.asyncio
async def test_explicit_tier_pin_skips_probing(monkeypatch):
    monkeypatch.setenv("PARSER_STRUCTURED_OUTPUT_TIER", "3")
    seen = {"response_format": False, "tools": False}

    def handler(kwargs):
        if "response_format" in kwargs:
            seen["response_format"] = True
        if "tools" in kwargs:
            seen["tools"] = True
        return _Resp(_Msg(content=_VALID))

    await extract._call_with_structured_output(_FakeClient(handler), "m5", _prompt())
    assert seen["response_format"] is False and seen["tools"] is False  # tiers 1/2 skipped


@pytest.mark.asyncio
async def test_tier3_strips_markdown_fences():
    fenced = "```json\n" + _VALID + "\n```"

    def handler(kwargs):
        if "response_format" in kwargs or "tools" in kwargs:
            raise RuntimeError("unsupported")
        return _Resp(_Msg(content=fenced))

    raw = await extract._call_with_structured_output(_FakeClient(handler), "m6", _prompt())
    assert json.loads(raw)["entities"][0]["name"] == "Greg"


def test_coerce_result_maps_ids_and_normalises():
    sid = uuid4()
    slices = [SliceText(slice_id=sid, stream_name="s", text="t")]
    id_map = {str(sid)[:8]: sid}
    data = {
        "entities": [
            {"name": "Greg", "entity_type": "Person",
             "source_slice_ids": [str(sid)[:8]], "quote": "Greg"},
            {"entity_type": "person"},  # missing name → dropped
        ],
        "relationships": [
            {"subject_name": "Greg", "subject_type": "person", "predicate": "USES",
             "object_name": "Thoth", "object_type": "spaceship",  # unknown → other
             "confidence": 5, "source_slice_ids": [str(sid)[:8]]},
            {"subject_name": "x"},  # incomplete → dropped
        ],
    }
    result = extract._coerce_result(data, id_map)
    assert len(result.entities) == 1
    assert result.entities[0].entity_type == "person"  # normalised
    assert result.entities[0].source_slice_ids == [sid]
    assert len(result.relationships) == 1
    assert result.relationships[0].predicate == "uses"  # lowercased
    assert result.relationships[0].object_type == "other"  # normalised
    assert result.relationships[0].confidence == 1.0  # clamped


def test_coerce_result_rejects_non_object():
    with pytest.raises(extract.ParseError):
        extract._coerce_result([1, 2, 3], {})


@pytest.mark.asyncio
async def test_call_parser_llm_empty_slices_returns_empty():
    assert (await extract.call_parser_llm([])).is_empty
