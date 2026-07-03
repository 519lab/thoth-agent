"""Token-budgeted greedy composer — Phase C spec §5.3.

Turns the ranked list of :class:`RecallCandidate` into the text body
that :class:`SubstrateMemoryProvider` returns. Sanitises any inline
``<memory-context>`` fences using Thoth's existing helper, counts
tokens via tiktoken, and stops when adding the next candidate would
exceed the budget. A single oversized first candidate is truncated at
the last newline before the budget edge and marked ``[truncated]``.

Output is NOT fence-wrapped — the caller passes the text to Thoth's
``build_memory_context_block`` for the standard wrapper.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from substrate.recall.projection import RecallCandidate


# Module-level encoder cache. tiktoken's ``encoding_for_model`` /
# ``get_encoding`` does a one-off file read; caching avoids repeating
# that on every recall call.
_ENCODER_CACHE: dict[str, "object"] = {}

# Phase 2d: the context-engine eviction pointer stream. Its structured payload
# already carries a self-describing, actionable ``text`` one-liner (the tool
# name, the gist, and the exact ``context_expand(...)`` call). Rendering the
# whole payload dict would spend projection tokens on scaffolding
# (``kind`` / ``handle`` / ``orig_len``) the model doesn't need — so the block
# renders ONLY that ``text`` line. Kept as a literal (matching the SQL carve-out
# in ``SliceRepo.recall_window``) rather than imported from the agent package so
# the recall composer stays free of an agent-side dependency.
_CONTEXT_EVICTED_STREAM = "thoth.self_state.context_evicted"


class _HeuristicEncoder:
    """Tiktoken-shaped fallback when tiktoken isn't installed.

    Implements just enough of the ``Encoding`` interface for
    ``compose_projection``: ``encode(text) -> list[int]`` (where the
    list length is the heuristic token count) and ``decode(tokens) ->
    str`` (round-trip identity by tracking the source text).

    The heuristic: ~4 chars per token (matches OpenAI's published
    rule-of-thumb for cl100k_base on English prose). Length-only
    accuracy is sufficient for budget enforcement; truncation accuracy
    is preserved via a chars-to-token-edge lookup.
    """

    _CHARS_PER_TOKEN = 4

    def encode(self, text: str) -> list[int]:
        # Return one fake token id per ~4 chars (rounded up). The IDs
        # themselves don't matter — the composer only cares about
        # ``len(encoded)``.
        n = max(0, math.ceil(len(text) / self._CHARS_PER_TOKEN))
        return [0] * n

    def decode(self, tokens: list[int]) -> str:
        # This is the only tricky method — the composer calls
        # ``encoder.decode(token_ids[:available])`` after slicing the
        # encoded list. We can't reconstruct the original text from
        # the placeholder IDs; instead the composer's truncation path
        # uses ``len(tokens) * CHARS_PER_TOKEN`` characters of the
        # original text. To keep the interface symmetric we just
        # return the original (passed as a separate argument is not
        # available here, so we return a string of the right token
        # count). Practically the composer's truncation uses character
        # slicing under the heuristic encoder; see _truncate_to_budget.
        return " " * (len(tokens) * self._CHARS_PER_TOKEN)


def _get_encoder(encoder_name: str):
    """Look up a cached tiktoken encoder. Lazy-imports tiktoken so the
    recall package can be imported without tiktoken installed.

    If tiktoken is unavailable, returns a heuristic encoder (~4 chars
    per token) that satisfies the same length-counting contract.
    Truncation behaviour is approximate under the heuristic — see
    ``_truncate_to_budget`` for the character-aligned fallback path.
    """
    enc = _ENCODER_CACHE.get(encoder_name)
    if enc is not None:
        return enc
    try:
        import tiktoken
    except ImportError:
        enc = _HeuristicEncoder()
    else:
        enc = tiktoken.get_encoding(encoder_name)
    _ENCODER_CACHE[encoder_name] = enc
    return enc


def _payload_text(payload) -> str:
    """Format a candidate's payload as text for inclusion in the body.

    Text-modality payloads are already unwrapped to bare strings by
    ``recall_window``. Structured-event payloads (dicts) are dumped via
    ``json.dumps`` with compact separators — the composer doesn't try
    to be pretty, just deterministic.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return str(payload)


def _eviction_body(candidate: "RecallCandidate") -> "Optional[str]":
    """Compact body for a ``context_evicted`` pointer slice, else ``None``.

    Phase 2d (plan §4): eviction pointers on
    ``thoth.self_state.context_evicted`` carry a ready-made, self-describing
    ``text`` line in their structured payload. Return just that line so the
    composed block stays lean; return ``None`` for every other stream (and for
    a malformed payload) so the caller falls back to the default full-payload
    rendering — this special case is strictly stream-name-gated.
    """
    if candidate.stream_name != _CONTEXT_EVICTED_STREAM:
        return None
    payload = candidate.payload
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _format_block(candidate: "RecallCandidate", *, provenance: "Optional[str]" = None) -> str:
    """Render one candidate as a recall-block string.

    ``provenance`` (e.g. ``"0.42 semantic"``) is appended as a ``· why:``
    tag when supplied — opt-in via ``RECALL_SHOW_PROVENANCE`` so the
    default block stays clean."""
    header = (
        f"[from {candidate.stream_name} at "
        f"{candidate.event_time_world.isoformat()}]"
    )
    if provenance:
        header += f"  · why: {provenance}"
    # Eviction pointers render only their actionable ``text`` line; every
    # other stream renders the full payload (unchanged).
    body = _eviction_body(candidate)
    if body is None:
        body = _payload_text(candidate.payload)
    return f"{header}\n{body}"


def render_l1_header(entities: list[dict]) -> str:
    """Render the Phase D ``## Known entities`` block (spec §7).

    Pure: takes already-fetched entity dicts (``name``, ``entity_type``,
    ``summary``, ``cites`` — a list of short slice-id strings) so the
    composer stays DB-free. Returns "" for an empty list so the caller can
    skip prepending.
    """
    if not entities:
        return ""
    lines = [f"## Known entities ({len(entities)})"]
    for e in entities:
        name = e.get("name", "?")
        etype = e.get("entity_type", "other")
        summary = (e.get("summary") or "").strip()[:200]
        cites = e.get("cites") or []
        cite_str = f" (cites: {', '.join(cites)})" if cites else ""
        body = f" — {summary}" if summary else ""
        lines.append(f"- {name} ({etype}){body}{cite_str}")
    return "\n".join(lines)


def render_l3_header(patterns: list[dict]) -> str:
    """Render the ``## Patterns`` block from already-fetched L3 pattern dicts
    (``kind``, ``statement``, ``cites`` — a list of short slice-id strings).

    Pure (DB-free), mirroring :func:`render_l1_header`. Returns "" for an empty
    list so the caller can skip prepending. These are the substrate's
    generalisations / recurring themes — dense and high-value, so the recall
    limit that feeds this is small."""
    if not patterns:
        return ""
    lines = [f"## Patterns ({len(patterns)})"]
    for p in patterns:
        kind = p.get("kind", "other")
        statement = (p.get("statement") or "").strip()
        if not statement:
            continue
        cites = p.get("cites") or []
        cite_str = f" (cites: {', '.join(cites)})" if cites else ""
        lines.append(f"- [{kind}] {statement}{cite_str}")
    # All statements empty → nothing worth a header.
    return "\n".join(lines) if len(lines) > 1 else ""


def render_l4_header(observations: list[dict]) -> str:
    """Render the ``## Self-model`` block from already-fetched L4 observation
    dicts (``kind``, ``subject``, ``statement``).

    Pure (DB-free), mirroring :func:`render_l1_header`. Returns "" for an empty
    list. These are the substrate's calibration / bias / self-model notes —
    what it has observed about itself — surfaced so recall can reach beyond raw
    episodes into the higher-order self-knowledge."""
    if not observations:
        return ""
    lines = [f"## Self-model ({len(observations)})"]
    for o in observations:
        kind = o.get("kind", "other")
        subject = (o.get("subject") or "").strip()
        statement = (o.get("statement") or "").strip()
        if not statement:
            continue
        subj = f"{subject}: " if subject else ""
        lines.append(f"- [{kind}] {subj}{statement}")
    return "\n".join(lines) if len(lines) > 1 else ""


def compose_projection(
    ranked: "list[RecallCandidate]",
    *,
    token_budget: int,
    encoder_name: str = "cl100k_base",
    dedup_threshold: float = 0.0,
    provenance: "Optional[dict]" = None,
    show_provenance: bool = False,
) -> tuple[str, "list[RecallCandidate]", int]:
    """Greedy fill ranked candidates into a token-budget-bounded text.

    Returns ``(composed_text, composed_candidates, tokens_used)``.

    Behaviour:
      * ``token_budget == 0`` → returns ``("", [], 0)``.
      * Empty input → returns ``("", [], 0)``.
      * Each candidate is sanitised (strips any embedded
        ``<memory-context>`` fences) before token counting — prevents
        double-wrapping when a slice's payload happens to contain
        substrate self-state.
      * If the FIRST candidate alone exceeds the budget, it's truncated
        at the last newline before the budget edge and marked
        ``[truncated]``. (Spec §5.3.) Subsequent oversized candidates
        are skipped, not truncated, on the rationale that the first
        candidate is the highest-ranked and most valuable.
      * Candidates are separated by a blank line (``\\n\\n``).

    Side-effect-free; safe to call from any thread.
    """
    # Late imports keep this module light at import time.
    from agent.memory_manager import sanitize_context
    from substrate.recall.projection import _jaccard, _tokenise

    if token_budget <= 0 or not ranked:
        return "", [], 0

    encoder = _get_encoder(encoder_name)
    provenance = provenance or {}
    composed: list = []
    composed_blocks: list[str] = []
    composed_token_sets: list[set] = []  # for MMR-style dedup
    tokens_used = 0
    separator_tokens_per_block = len(encoder.encode("\n\n"))

    for idx, candidate in enumerate(ranked):
        # MMR-style diversity: skip a near-duplicate of something already
        # composed (kills repeated excerpts). The first candidate never
        # dedups against an empty set.
        if dedup_threshold > 0.0 and composed_token_sets:
            cand_tokens = _tokenise(_payload_text(candidate.payload))
            if cand_tokens and any(
                _jaccard(cand_tokens, prev) >= dedup_threshold
                for prev in composed_token_sets
            ):
                continue

        prov = provenance.get(getattr(candidate, "slice_id", None)) if show_provenance else None
        block_raw = _format_block(candidate, provenance=prov)
        # Sanitise BEFORE counting tokens so the strip count matches
        # the on-wire content. sanitize_context removes already-wrapped
        # <memory-context> spans + the system-note line.
        block = sanitize_context(block_raw)
        block_tokens = len(encoder.encode(block))
        # Budget accounting: the separator before this block costs
        # tokens only when there's already content.
        prefix_cost = separator_tokens_per_block if composed_blocks else 0

        if tokens_used + prefix_cost + block_tokens <= token_budget:
            composed_blocks.append(block)
            composed.append(candidate)
            if dedup_threshold > 0.0:
                composed_token_sets.append(_tokenise(_payload_text(candidate.payload)))
            tokens_used += prefix_cost + block_tokens
            continue

        # Doesn't fit. If this is the first candidate AND the budget
        # is large enough to be meaningful, truncate at the last
        # newline before the budget edge and mark with [truncated].
        if idx == 0:
            truncated = _truncate_to_budget(block, encoder, token_budget)
            if truncated:
                composed_blocks.append(truncated)
                composed.append(candidate)
                tokens_used = len(encoder.encode(truncated))
            # Stop after the first-candidate truncation regardless of
            # success — subsequent candidates can't fit either.
            break
        # Not the first candidate: skip it, continue trying later
        # candidates (a smaller one may still fit). This matches the
        # spec's greedy-fill behaviour.

    return "\n\n".join(composed_blocks), composed, tokens_used


def _truncate_to_budget(text: str, encoder, token_budget: int) -> str:
    """Truncate ``text`` at the last newline before the budget edge and
    append ``[truncated]``. Returns the truncated text, or empty if
    even the truncation marker won't fit.

    Works with both the tiktoken encoder and the heuristic fallback:
    we shrink the source text by character until its token count fits.
    """
    marker = "\n[truncated]"
    marker_tokens = len(encoder.encode(marker))
    if token_budget <= marker_tokens:
        return ""
    available = token_budget - marker_tokens
    token_ids = encoder.encode(text)
    if len(token_ids) <= available:
        # Doesn't need truncation — caller can use the full text.
        return text + marker  # caller still flags as truncated

    # Iteratively shrink by characters until the encoded length fits.
    # Start with a chars-per-token estimate so we converge in a few
    # passes for very large texts.
    estimate_chars = max(1, int(available * (len(text) / max(1, len(token_ids)))))
    candidate = text[:estimate_chars]
    while len(encoder.encode(candidate)) > available and candidate:
        # Drop ~5% of remaining length each iteration.
        new_len = max(0, len(candidate) - max(1, len(candidate) // 20))
        candidate = candidate[:new_len]
    # Prefer to cut at the last newline for readability.
    if candidate:
        last_nl = candidate.rfind("\n")
        if last_nl > 0:
            candidate = candidate[:last_nl]
    return candidate + marker


__all__ = [
    "compose_projection",
    "render_l1_header",
    "render_l3_header",
    "render_l4_header",
]
