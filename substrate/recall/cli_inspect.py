"""``thoth substrate recall`` printers — Phase C Task 12.

Implementation lives next to the recall API for cohesion; the parent
``substrate/cli/inspect.py`` registers the subparser and delegates the
print to the functions here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


async def print_summary(conn: "asyncpg.Connection") -> None:
    """Default ``recall`` subcommand output (spec §8.2)."""
    from substrate.storage.slices import SliceRepo

    # recall_stats only uses the passed-in conn — pool can be None for
    # the throwaway repo used here. The real repo lives on the booted
    # Substrate; the inspect CLI deliberately doesn't boot the substrate
    # (it's a read-only debug surface).
    repo = SliceRepo(pool=None)
    stats = await repo.recall_stats(conn, window=timedelta(hours=1))
    now = datetime.now(timezone.utc)
    print(f"Recall state @ {now.isoformat()}")
    print()

    total = int(stats.get("total_calls", 0) or 0)
    non_empty = int(stats.get("non_empty_calls", 0) or 0)
    timed_out = int(stats.get("timed_out_calls", 0) or 0)
    errors = int(stats.get("error_calls", 0) or 0)
    pct = (100 * non_empty / total) if total else 0
    print("Last 1 hour:")
    print(f"  calls           {total}")
    print(f"  non-empty       {non_empty} ({pct:.0f}%)")
    print(f"  timed-out       {timed_out}")
    print(f"  errors          {errors}")
    print(f"  avg duration   {int(stats.get('avg_duration_ms', 0) or 0)} ms")
    print(f"  avg tokens     {int(stats.get('avg_tokens', 0) or 0)}")
    print(
        f"  avg candidates {int(stats.get('avg_candidates', 0) or 0)} / call, "
        f"{int(stats.get('avg_composed', 0) or 0)} composed"
    )
    print()

    total_slices = int(stats.get("total_slices", 0) or 0)
    embedded = int(stats.get("embedded_slices", 0) or 0)
    backlog = int(stats.get("unembedded_backlog", 0) or 0)
    coverage = (100 * embedded / total_slices) if total_slices else 0
    print("Embedding coverage:")
    print(f"  total slices              {total_slices}")
    print(f"  with embedding            {embedded} ({coverage:.1f}%)")
    print(f"  unembedded (backlog)      {backlog}")

    semantic = int(stats.get("semantic_path_count", 0) or 0)
    keyword = int(stats.get("keyword_path_count", 0) or 0)
    mixed = int(stats.get("mixed_path_count", 0) or 0)
    path_total = semantic + keyword + mixed
    if path_total:
        sem_pct = 100 * semantic / path_total
        kw_pct = 100 * keyword / path_total
        mx_pct = 100 * mixed / path_total
        print(
            f"  ranking path (last hour)  semantic {sem_pct:.0f}%, "
            f"keyword {kw_pct:.0f}%, mixed {mx_pct:.0f}%"
        )
    print()

    import os

    enabled = os.environ.get("HERMES_SUBSTRATE_RECALL", "1")
    print("Provider status:")
    print(f"  HERMES_SUBSTRATE_RECALL = {enabled}")


async def print_recent(conn: "asyncpg.Connection", *, limit: int) -> None:
    """Recent recall calls — table view from substrate_recall_log."""
    rows = await conn.fetch(
        """
        SELECT log_id, requested_at, session_id, query_excerpt,
               candidates_count, composed_count, tokens_used,
               duration_ms, timed_out, error_text,
               metadata->>'embedding_path' AS embedding_path
          FROM substrate_recall_log
         ORDER BY requested_at DESC
         LIMIT $1
        """,
        limit,
    )
    if not rows:
        print("(no recall log rows)")
        return
    # Header
    print(
        f"{'id':>6}  {'when':<25}  {'sess':<14}  {'cand':>4}  "
        f"{'comp':>4}  {'toks':>5}  {'ms':>4}  {'path':<8}  query"
    )
    for r in rows:
        when = r["requested_at"].isoformat(timespec="seconds")
        sess = (r["session_id"] or "")[:14]
        path = (r["embedding_path"] or "-")[:8]
        excerpt = (r["query_excerpt"] or "")[:50]
        flag = "T" if r["timed_out"] else "-"
        print(
            f"{r['log_id']:>6}  {when:<25}  {sess:<14}  "
            f"{r['candidates_count']:>4}  {r['composed_count']:>4}  "
            f"{r['tokens_used']:>5}  {r['duration_ms']:>4}  "
            f"{path:<8}  {flag} {excerpt}"
        )


async def print_sample(conn: "asyncpg.Connection", *, session_id: str) -> None:
    """Last log row for a session — useful for the operator to verify a
    given session is actually producing recall output."""
    row = await conn.fetchrow(
        """
        SELECT *
          FROM substrate_recall_log
         WHERE session_id = $1
         ORDER BY requested_at DESC
         LIMIT 1
        """,
        session_id,
    )
    if row is None:
        print(f"(no recall log rows for session_id={session_id!r})")
        return
    print(f"session_id:     {row['session_id']}")
    print(f"requested_at:   {row['requested_at'].isoformat()}")
    print(f"query_excerpt:  {row['query_excerpt']}")
    print(f"candidates:     {row['candidates_count']}")
    print(f"composed:       {row['composed_count']}")
    print(f"tokens_used:    {row['tokens_used']}")
    print(f"duration_ms:    {row['duration_ms']}")
    print(f"timed_out:      {row['timed_out']}")
    print(f"error_text:     {row['error_text'] or '-'}")
    print(f"metadata:       {row['metadata']}")


async def print_config(conn: "asyncpg.Connection") -> None:
    """Dump the current RECALL_* config knobs."""
    from substrate import config as _cfg

    print("Recall config:")
    print(f"  RECALL_TOKEN_BUDGET                   = {_cfg.RECALL_TOKEN_BUDGET}")
    print(f"  RECALL_TIME_WINDOW_HOURS              = {_cfg.RECALL_TIME_WINDOW_HOURS}")
    print(f"  RECALL_TIMEOUT_MS                     = {_cfg.RECALL_TIMEOUT_MS}")
    print(f"  RECALL_MIN_SALIENCE                   = {_cfg.RECALL_MIN_SALIENCE}")
    print(f"  RECALL_CANDIDATE_LIMIT                = {_cfg.RECALL_CANDIDATE_LIMIT}")
    print(f"  RECALL_MIN_RELEVANCE                  = {_cfg.RECALL_MIN_RELEVANCE}")
    print(f"  RECALL_RELATIVE_FLOOR                 = {_cfg.RECALL_RELATIVE_FLOOR}")
    print(f"  RECALL_DEDUP_THRESHOLD                = {_cfg.RECALL_DEDUP_THRESHOLD}")
    print(f"  RECALL_SHOW_PROVENANCE                = {_cfg.RECALL_SHOW_PROVENANCE}")
    print(f"  RECALL_SIMILARITY_WEIGHT              = {_cfg.RECALL_SIMILARITY_WEIGHT}")
    print(f"  RECALL_KEYWORD_WEIGHT                 = {_cfg.RECALL_KEYWORD_WEIGHT}")
    print(f"  RECALL_SALIENCE_WEIGHT                = {_cfg.RECALL_SALIENCE_WEIGHT}")
    print(f"  RECALL_RECENCY_WEIGHT                 = {_cfg.RECALL_RECENCY_WEIGHT}")
    print(f"  RECALL_RECENCY_HALF_LIFE_HOURS        = {_cfg.RECALL_RECENCY_HALF_LIFE_HOURS}")
    print(f"  RECALL_REINFORCE_RATE_LIMIT_PER_MIN   = {_cfg.RECALL_REINFORCE_RATE_LIMIT_PER_MIN}")
    print(f"  RECALL_LOG_QUEUE_DEPTH                = {_cfg.RECALL_LOG_QUEUE_DEPTH}")
    print(f"  RECALL_EMBEDDING_MODEL                = {_cfg.RECALL_EMBEDDING_MODEL!r}")
    print(f"  RECALL_EMBEDDING_DIM                  = {_cfg.RECALL_EMBEDDING_DIM}")
    print(f"  RECALL_EMBEDDING_TIMEOUT_MS           = {_cfg.RECALL_EMBEDDING_TIMEOUT_MS}")
    print(f"  RECALL_EMBEDDING_QUEUE_DEPTH          = {_cfg.RECALL_EMBEDDING_QUEUE_DEPTH}")
    print(f"  RECALL_EMBEDDING_BATCH_SIZE           = {_cfg.RECALL_EMBEDDING_BATCH_SIZE}")
    print(f"  RECALL_EMBEDDING_BACKFILL_INTERVAL_S  = {_cfg.RECALL_EMBEDDING_BACKFILL_INTERVAL_S}")
    print(f"  RECALL_EMBEDDING_BACKFILL_MAX_RETRIES = {_cfg.RECALL_EMBEDDING_BACKFILL_MAX_RETRIES}")
    print(f"  HERMES_SUBSTRATE_RECALL (enable)      = {_cfg.HERMES_SUBSTRATE_RECALL_ENABLED}")


# ---------------------------------------------------------------------------
# Recall validation — the operator-facing go/no-go probe.
#
# Phase C's ADR deferred acceptance criteria #10/#13/#14 ("coherent memory
# block", "embedding coverage ≥95%", ">80% semantic path") to a manual
# smoke test before flipping the default. The default is now ON (PR #61),
# so this command turns that one-off smoke into a repeatable health check:
# it runs a REAL recall against the current L0 and prints the composed
# <memory-context> block plus a readiness verdict. Especially useful after
# a worker outage (embeddings stop backfilling → recall silently degrades
# to keyword-only or empty blocks).
# ---------------------------------------------------------------------------


async def _reachable_candidates(conn: "asyncpg.Connection", window_hours: float) -> int:
    """Count passed slices in the default recall streams within the recall
    window — i.e. how much perception recall even has to work with."""
    from substrate import config as _cfg

    return int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
              FROM substrate_slices sl
              JOIN substrate_streams st ON st.stream_id = sl.stream_id
             WHERE sl.sentinel_state = 'passed'
               AND st.name = ANY($1::text[])
               AND sl.event_time_world > now() - ($2 || ' hours')::interval
            """,
            list(_cfg.DEFAULT_RECALL_STREAMS),
            str(window_hours),
        )
        or 0
    )


async def _derive_probe_query(conn: "asyncpg.Connection") -> str:
    """Use the most-recent user-message slice's text as a realistic probe,
    so the validation exercises recall against content that actually
    exists. Falls back to a generic prompt when L0 is empty."""
    row = await conn.fetchval(
        """
        SELECT sl.payload
          FROM substrate_slices sl
          JOIN substrate_streams st ON st.stream_id = sl.stream_id
         WHERE st.name LIKE 'thoth.world.user_message.%'
           AND sl.sentinel_state = 'passed'
         ORDER BY sl.event_time_world DESC
         LIMIT 1
        """
    )
    if row is None:
        return "recent conversation topics"
    text = row.get("text") if isinstance(row, dict) else str(row)
    text = (text or "").strip()
    return (text[:200] or "recent conversation topics")


def _validate_verdict(
    *, enabled: str, total_slices: int, reachable: int, coverage: float, proj
) -> tuple[str, list[str]]:
    """Return ``(verdict, notes)``. Verdict is READY / DEGRADED / NOT READY."""
    notes: list[str] = []
    if enabled == "0":
        notes.append(
            "HERMES_SUBSTRATE_RECALL=0 — the foreground is NOT using substrate "
            "recall; set it to 1 (the default) to enable."
        )
    if total_slices == 0 or reachable == 0:
        notes.append(
            "No perception in the recall window — recall returns empty blocks. "
            "Check the worker is running (`thoth substrate agents`) and that "
            "sessions are flowing into L0."
        )
        return ("NOT READY", notes)
    if proj.empty_reason == "no_candidates":
        notes.append(
            "recall found no candidates for the probe query despite slices in "
            "the window — widen the window or check stream filters."
        )
    if total_slices and coverage < 50.0:
        notes.append(
            f"Embedding coverage is low ({coverage:.0f}%) — recall is leaning "
            "on keyword ranking. Confirm the worker is backfilling embeddings "
            "(`thoth substrate curator`) and the embedding provider is reachable."
        )
    if proj.text:
        notes.append(
            f"recall composed a {proj.tokens_used}-token block from "
            f"{len(proj.composed)} slice(s)."
        )
        # A block composed, but semantic ranking needs embeddings: low
        # coverage means recall is leaning on keyword Jaccard, which is
        # functional but lower-quality — surface that as DEGRADED.
        verdict = "READY" if coverage >= 50.0 else "DEGRADED"
        return (verdict, notes)
    notes.append(f"recall returned an empty block (reason={proj.empty_reason}).")
    return ("DEGRADED", notes)


async def validate(
    conn: "asyncpg.Connection",
    *,
    query: "str | None" = None,
    token_budget: "int | None" = None,
) -> None:
    """Run a real recall and print the composed block + a readiness verdict.

    Read-mostly: it performs the same ``recall()`` the foreground would,
    which includes the normal salience reinforcement of any composed slices
    (a small, realistic side effect). It changes no configuration.
    """
    import os

    import thoth_db
    from substrate import Substrate, config as _cfg
    from substrate.recall.api import recall
    from substrate.storage.slices import SliceRepo

    now = datetime.now(timezone.utc)
    print(f"Recall validation @ {now.isoformat()}")
    enabled = os.environ.get("HERMES_SUBSTRATE_RECALL", "1")
    print(
        f"  HERMES_SUBSTRATE_RECALL = {enabled}  "
        f"(default {'ON' if _cfg.HERMES_SUBSTRATE_RECALL_ENABLED else 'OFF'})"
    )
    print()

    repo = SliceRepo(pool=None)
    stats = await repo.recall_stats(conn, window=timedelta(hours=1))
    total_slices = int(stats.get("total_slices", 0) or 0)
    embedded = int(stats.get("embedded_slices", 0) or 0)
    backlog = int(stats.get("unembedded_backlog", 0) or 0)
    coverage = (100 * embedded / total_slices) if total_slices else 0.0
    print("Embedding coverage:")
    print(f"  total passed slices   {total_slices}")
    print(f"  with embedding        {embedded} ({coverage:.1f}%)")
    print(f"  unembedded backlog    {backlog}")
    print()

    window_h = _cfg.RECALL_TIME_WINDOW_HOURS
    reachable = await _reachable_candidates(conn, window_h)
    print(f"Candidate slices in recall window (last {window_h}h): {reachable}")
    print()

    if not query:
        query = await _derive_probe_query(conn)
    print(f"Probe query: {query!r}")

    sub = Substrate.from_pool(thoth_db.pool())
    proj = await recall(
        sub, query, session_id="recall-validate", token_budget=token_budget
    )
    print(f"  candidates_seen = {proj.candidates_seen}")
    print(f"  composed slices = {len(proj.composed)}")
    print(f"  tokens_used     = {proj.tokens_used}")
    print(f"  timed_out       = {proj.timed_out}")
    if proj.empty_reason:
        print(f"  empty_reason    = {proj.empty_reason}")
    print()
    print("Composed <memory-context> block:")
    print("-" * 64)
    print(proj.text if proj.text else "(empty)")
    print("-" * 64)
    print()

    verdict, notes = _validate_verdict(
        enabled=enabled,
        total_slices=total_slices,
        reachable=reachable,
        coverage=coverage,
        proj=proj,
    )
    print(f"Verdict: {verdict}")
    for note in notes:
        print(f"  - {note}")


__all__ = [
    "print_summary",
    "print_recent",
    "print_sample",
    "print_config",
    "validate",
]
