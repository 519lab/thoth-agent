"""Tier-B batched MAIN-model gist upgrade (Round 3).

Tier B replaces the structural (Tier-A) gist of large/prose evicted items with a
task-relevant summary from the MAIN model — ONE batched call per pass (numbered
sections in, numbered summaries out). The economics: on the target install the
main model is a fast LOCAL server (sub-second prefill for a modest prompt), while
the auxiliary chain is a slow cloud call, so we force the main model and batch
the whole pass into a single request. Every failure mode (no endpoint, disabled,
malformed, partial, exception) must degrade to the Tier-A gist and never raise.

These tests drive :meth:`_apply_tier_b_gists` directly with a fake ``call_llm``
so no PG or network is needed — the eviction/cooling suites already cover the
loop that stages the records.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from agent.context_compressor import EVICTION_STUB_PREFIX
from agent.context_engine_substrate import (
    SubstrateContextEngine,
    _parse_numbered_summaries,
)


@contextmanager
def _no_ctx_probe(context_length: int = 200000):
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=context_length,
    ):
        yield


def _make_engine(with_endpoint: bool = True) -> SubstrateContextEngine:
    with _no_ctx_probe():
        eng = SubstrateContextEngine(model="test/model", quiet_mode=True)
    if with_endpoint:
        # Give the engine a resolvable main endpoint so the Tier-B gate opens
        # (a local server is exactly base_url-without-provider).
        eng._compressor.base_url = "http://localhost:8080/v1"
    return eng


class _Resp:
    def __init__(self, content):
        msg = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": msg})()]


# Body size clears the round-4 Tier-B eligibility floor (finding C: 4k → 8k),
# so these still exercise the Tier-B upgrade path.
def _big(tag: str, n: int = 9000) -> str:
    return f"{tag}\n" + (tag[0] * n)


def _records_and_result(n: int = 3):
    """n eligible records (>= AUX_MIN_CHARS) + a result list of matching stubs."""
    records = []
    result = []
    for i in range(n):
        orig = _big(f"BODY{i}", 9000)
        handle = f"sid:s#m:{i}"
        stub = f'{EVICTION_STUB_PREFIX}{handle} — terminal ({len(orig)} chars). Gist: TIER_A_{i}. Retrieve exact: context_expand("{handle}")]'
        result.append({"role": "tool", "tool_call_id": f"c{i}", "content": stub})
        records.append({
            "handle": handle,
            "tool_name": "terminal",
            "gist": f"TIER_A_{i}",
            "orig_len": len(orig),
            "_idx": i,
            "_orig": orig,
        })
    return records, result


class TestParseNumberedSummaries:
    def test_bracket_form_one_line_each(self):
        out = _parse_numbered_summaries("[1] first\n[2] second\n[3] third", 3)
        assert out == {1: "first", 2: "second", 3: "third"}

    def test_multiline_sections(self):
        text = "[1] alpha\nmore alpha\n[2] beta"
        out = _parse_numbered_summaries(text, 2)
        assert out[1] == "alpha\nmore alpha"
        assert out[2] == "beta"

    def test_dotted_fallback_form(self):
        out = _parse_numbered_summaries("1. one\n2. two", 2)
        assert out == {1: "one", 2: "two"}

    def test_out_of_range_dropped(self):
        out = _parse_numbered_summaries("[1] ok\n[9] nope", 3)
        assert out == {1: "ok"}

    def test_garbage_returns_empty(self):
        assert _parse_numbered_summaries("no markers here at all", 3) == {}
        assert _parse_numbered_summaries("", 3) == {}


class TestTierBApply:
    def test_batched_call_applies_summaries_per_item(self):
        engine = _make_engine()
        records, result = _records_and_result(3)
        fake = patch(
            "agent.auxiliary_client.call_llm",
            return_value=_Resp("[1] gist ONE\n[2] gist TWO\n[3] gist THREE"),
        )
        with fake as m:
            engine._apply_tier_b_gists(result, records)
        # Exactly ONE batched call for all three items.
        assert m.call_count == 1
        # Each record's gist upgraded and its stub rewritten with the new gist.
        assert records[0]["gist"] == "gist ONE"
        assert records[2]["gist"] == "gist THREE"
        assert "gist ONE" in result[0]["content"]
        assert "gist THREE" in result[2]["content"]
        # Handle grammar preserved through the rewrite.
        assert result[0]["content"].startswith(EVICTION_STUB_PREFIX)
        assert 'context_expand("sid:s#m:0")' in result[0]["content"]

    def test_forces_main_model_via_auto_and_main_runtime(self):
        engine = _make_engine()
        records, result = _records_and_result(2)
        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=_Resp("[1] a\n[2] b"),
        ) as m:
            engine._apply_tier_b_gists(result, records)
        _, kwargs = m.call_args
        assert kwargs["provider"] == "auto"  # auto → main Step-1 path
        assert kwargs["main_runtime"]["model"] == "test/model"
        assert kwargs["main_runtime"]["base_url"] == "http://localhost:8080/v1"

    def test_single_call_for_many_items(self):
        engine = _make_engine()
        records, result = _records_and_result(5)
        body = "\n".join(f"[{i}] g{i}" for i in range(1, 6))
        with patch("agent.auxiliary_client.call_llm", return_value=_Resp(body)) as m:
            engine._apply_tier_b_gists(result, records)
        assert m.call_count == 1  # batching: N items → 1 call

    def test_malformed_reply_falls_back_to_tier_a(self):
        engine = _make_engine()
        records, result = _records_and_result(2)
        before = [r["gist"] for r in records]
        stubs = [msg["content"] for msg in result]
        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=_Resp("sorry, I cannot help with that"),
        ):
            engine._apply_tier_b_gists(result, records)
        assert [r["gist"] for r in records] == before  # Tier A untouched
        assert [msg["content"] for msg in result] == stubs

    def test_partial_reply_upgrades_only_present_sections(self):
        engine = _make_engine()
        records, result = _records_and_result(3)
        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=_Resp("[1] upgraded one\n[3] upgraded three"),
        ):
            engine._apply_tier_b_gists(result, records)
        assert records[0]["gist"] == "upgraded one"
        assert records[1]["gist"] == "TIER_A_1"       # missing section → Tier A
        assert records[2]["gist"] == "upgraded three"

    def test_call_exception_never_raises_keeps_tier_a(self):
        engine = _make_engine()
        records, result = _records_and_result(2)
        before = [r["gist"] for r in records]
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("no provider configured"),
        ):
            engine._apply_tier_b_gists(result, records)  # must not raise
        assert [r["gist"] for r in records] == before

    def test_env_disabled_makes_no_call(self):
        engine = _make_engine()
        records, result = _records_and_result(2)
        with patch("agent.auxiliary_client.call_llm") as m:
            with patch.dict("os.environ", {"CONTEXT_GIST_LLM": "0"}):
                engine._apply_tier_b_gists(result, records)
        m.assert_not_called()

    def test_no_main_endpoint_makes_no_call(self):
        engine = _make_engine(with_endpoint=False)  # no provider, no base_url
        records, result = _records_and_result(2)
        with patch("agent.auxiliary_client.call_llm") as m:
            engine._apply_tier_b_gists(result, records)
        m.assert_not_called()

    def test_small_items_are_ineligible_no_call(self):
        engine = _make_engine()
        records, result = _records_and_result(2)
        # Shrink both bodies below the 8000-char eligibility floor (finding C).
        for r in records:
            r["_orig"] = "tiny"
        with patch("agent.auxiliary_client.call_llm") as m:
            engine._apply_tier_b_gists(result, records)
        m.assert_not_called()

    def test_transient_fields_stripped_after_apply(self):
        engine = _make_engine()
        records, result = _records_and_result(2)
        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=_Resp("[1] a\n[2] b"),
        ):
            engine._apply_tier_b_gists(result, records)
        for r in records:
            assert "_idx" not in r and "_orig" not in r

    def test_secret_in_summary_is_redacted(self):
        engine = _make_engine()
        records, result = _records_and_result(1)
        secret = "ghp_1234567890abcdefABCDEFghijklmnopqrst"
        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=_Resp(f"[1] leaked {secret} value"),
        ):
            engine._apply_tier_b_gists(result, records)
        assert secret not in records[0]["gist"]
