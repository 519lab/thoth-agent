"""compose_projection — Phase C Task 6 / spec §9.4.

Pure-function tests against the composer. No DB, no async. Uses
tiktoken (already pinned for Thoth's context_compressor).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from substrate.recall.composer import compose_projection
from substrate.recall.projection import RecallCandidate
from substrate.storage.types import Address


def _candidate(payload, *, stream: str = "test.stream") -> RecallCandidate:
    now = datetime.now(timezone.utc)
    return RecallCandidate(
        slice_id=uuid4(),
        address=Address(uuid4(), now, now),
        stream_name=stream,
        payload=payload,
        event_time_world=now,
        salience_score=0.5,
        trust_score=None,
        metadata={},
        embedding=None,
    )


def test_compose_projection_greedy_fill_to_budget():
    """3 candidates totalling more than budget → composes a prefix that
    fits and stops; later candidates skipped."""
    # Each "block" is the header + body. "x " * 100 is ~100 tokens with
    # cl100k_base.
    cands = [
        _candidate(payload=("foo " * 100)),
        _candidate(payload=("bar " * 100)),
        _candidate(payload=("baz " * 100)),
    ]
    text, composed, tokens = compose_projection(cands, token_budget=200)
    assert len(composed) >= 1
    assert tokens <= 200
    # Body of the composed candidates appears.
    assert "foo" in text


def test_compose_projection_truncates_oversized_first_candidate():
    """If the very first candidate is bigger than the entire budget,
    truncate at the last newline and mark ``[truncated]``."""
    big = _candidate(payload="\n".join(["line"] * 500))  # very large
    text, composed, tokens = compose_projection([big], token_budget=80)
    assert text.endswith("[truncated]")
    assert tokens <= 80


def test_compose_projection_sanitizes_inline_memory_fences():
    """Candidate payload containing a <memory-context> span should have
    that span stripped before token counting (no double-wrap)."""
    c = _candidate(
        payload="prefix\n<memory-context>\nSECRET\n</memory-context>\nsuffix"
    )
    text, composed, tokens = compose_projection([c], token_budget=200)
    assert "SECRET" not in text
    assert "<memory-context>" not in text
    assert "prefix" in text
    assert "suffix" in text


def test_compose_projection_includes_stream_attribution():
    """Header line ``[from <stream> at <iso>]`` is present in each block."""
    c = _candidate(payload="hello", stream="hermes.test.attrib")
    text, composed, tokens = compose_projection([c], token_budget=200)
    assert "[from hermes.test.attrib at" in text
    assert "hello" in text


def test_compose_projection_zero_budget_returns_empty():
    """token_budget == 0 → empty text + empty composed list."""
    c = _candidate(payload="anything")
    text, composed, tokens = compose_projection([c], token_budget=0)
    assert text == ""
    assert composed == []
    assert tokens == 0


def test_compose_projection_empty_input_returns_empty():
    """No candidates → empty composition, no exceptions."""
    text, composed, tokens = compose_projection([], token_budget=1000)
    assert text == ""
    assert composed == []
    assert tokens == 0


def test_compose_projection_multiple_blocks_separated_by_blank_line():
    """When two small candidates both fit, the composed text contains
    them separated by a blank line (``\\n\\n``)."""
    a = _candidate(payload="alpha")
    b = _candidate(payload="beta")
    text, composed, _ = compose_projection([a, b], token_budget=500)
    assert len(composed) == 2
    assert "alpha" in text
    assert "beta" in text
    # Blank line between them.
    assert "\n\n" in text
