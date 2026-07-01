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

    enabled = os.environ.get("THOTH_SUBSTRATE_RECALL", "1")
    print("Provider status:")
    print(f"  THOTH_SUBSTRATE_RECALL = {enabled}")


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
    print(f"  THOTH_SUBSTRATE_RECALL (enable)      = {_cfg.THOTH_SUBSTRATE_RECALL_ENABLED}")


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
            "THOTH_SUBSTRATE_RECALL=0 — the foreground is NOT using substrate "
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
    enabled = os.environ.get("THOTH_SUBSTRATE_RECALL", "1")
    print(
        f"  THOTH_SUBSTRATE_RECALL = {enabled}  "
        f"(default {'ON' if _cfg.THOTH_SUBSTRATE_RECALL_ENABLED else 'OFF'})"
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


# ---------------------------------------------------------------------------
# Recall-replay report (innovation #1) — REPORT ONLY.
#
# Runs the offline replay sweep over the labelled recall log and prints the
# weight grid ranked by the v1 kept-vs-dropped outcome-separation metric. This
# command NEVER applies the winning weights — it's an A/B oracle the operator
# reads. Applying is `tune --apply`'s job (below), behind its guardrails.
# ---------------------------------------------------------------------------


async def replay(
    conn: "asyncpg.Connection",
    *,
    since: "datetime | None" = None,
    limit: "int | None" = None,
    grid: str = "default",
) -> None:
    """Print the recall-replay sweep over labelled recall rows."""
    from substrate.recall import replay as _replay

    corpus = await _replay.load_labeled_recalls(conn, since=since, limit=limit)
    if not corpus:
        print(
            "(no labelled recalls with per-candidate records to replay; "
            "outcome labelling needs THOTH_RECALL_OUTCOME_LABEL on and a few "
            "completed turns)"
        )
        return

    n_pos = sum(1 for r in corpus if r.outcome_score >= 0.5)
    print(
        f"Recall replay over {len(corpus)} labelled recall(s) "
        f"({n_pos} good-outcome, {len(corpus) - n_pos} poor-outcome)."
    )
    if since is not None:
        print(f"  since: {since.isoformat()}")
    print()
    print(
        "v1 metric = kept-vs-dropped outcome separation (NOT NDCG — no "
        "per-slice graded labels yet). Higher separation = the weight "
        "vector's top pick better predicts a good turn. REPORT ONLY: this "
        "command never applies weights."
    )
    print()

    base = _replay.baseline_weights()
    if grid != "default":
        print(f"(unknown grid {grid!r}; using the default grid)")
    entries = _replay.sweep(corpus, _replay.default_grid(base))

    print(
        f"{'sep':>7}  {'kept µ':>7}  {'drop µ':>7}  "
        f"{'n_kept':>6}  {'n_drop':>6}  weights"
    )
    print("-" * 72)
    base_label = base.label()
    for e in entries:
        marker = " *" if e.weights.label() == base_label else "  "
        print(
            f"{e.separation:>7.3f}  {e.mean_outcome_kept:>7.3f}  "
            f"{e.mean_outcome_dropped:>7.3f}  {e.n_kept:>6}  "
            f"{e.n_dropped:>6}  {e.weights.label()}{marker}"
        )
    print()
    print("  (* = current live baseline from substrate/config.py)")


# ---------------------------------------------------------------------------
# Recall-weight tuner (learned-recall-weights innovation) — the applying
# sibling of `replay`.
#
# Fits a weight vector on the labelled recall log (coordinate descent on the
# train split of a time-ordered train/holdout split), prints the verdict, and
# — only with --apply AND every guardrail green — promotes it to the active
# row in substrate_recall_weights. The live recall path picks the promotion
# up within RECALL_TUNED_WEIGHTS_TTL_S, no restart needed.
# ---------------------------------------------------------------------------


def _fmt_entry(label: str, entry) -> str:
    return (
        f"  {label:<10} sep={entry.separation:+.4f}  "
        f"kept µ={entry.mean_outcome_kept:.3f} (n={entry.n_kept})  "
        f"drop µ={entry.mean_outcome_dropped:.3f} (n={entry.n_dropped})"
    )


async def tune(
    conn: "asyncpg.Connection",
    *,
    since: "datetime | None" = None,
    limit: "int | None" = None,
    holdout: float = 0.3,
    apply: bool = False,
) -> None:
    """Fit recall weights on the labelled log; optionally promote them."""
    from substrate.recall import replay as _replay
    from substrate.recall import tuner as _tuner
    from substrate.recall import weights_store as _store

    corpus = await _replay.load_labeled_recalls(conn, since=since, limit=limit)
    if not corpus:
        print(
            "(no labelled recalls with per-candidate records to tune on; "
            "outcome labelling needs THOTH_RECALL_OUTCOME_LABEL on and a few "
            "completed turns)"
        )
        return

    result = _tuner.fit(corpus, holdout_fraction=holdout)

    print(
        f"Recall weight tune over {result.corpus_size} labelled recall(s) "
        f"(train {result.train_size} / holdout {result.holdout_size}, "
        f"time-ordered split)."
    )
    print()
    print(f"baseline: {result.baseline.label()}")
    print(_fmt_entry("train", result.baseline_train))
    print(_fmt_entry("holdout", result.baseline_holdout))
    print(f"fitted:   {result.best.label()}")
    print(_fmt_entry("train", result.best_train))
    print(_fmt_entry("holdout", result.best_holdout))
    print()
    print(f"holdout improvement: {result.holdout_improvement:+.4f}")

    if result.guardrails:
        print()
        print("Guardrails NOT met — these weights should not be applied:")
        for reason in result.guardrails:
            print(f"  - {reason}")
        if apply:
            print()
            print("--apply refused (guardrails above).")
        return

    if not result.recommend:
        print()
        print(
            "Fit did not move off the baseline — nothing to apply (the live "
            "weights already sit at a local optimum for this corpus)."
        )
        return

    if not apply:
        print()
        print(
            "Guardrails met. Re-run with --apply to promote these weights; "
            "the live recall path picks them up within "
            "THOTH_RECALL_TUNED_WEIGHTS_TTL_S (default 300s)."
        )
        return

    row_id = await _store.save(
        conn,
        weights=result.best,
        source="cli",
        corpus_size=result.corpus_size,
        train_metric=result.best_train.separation,
        holdout_metric=result.best_holdout.separation,
        baseline_holdout_metric=result.baseline_holdout.separation,
        activate=True,
    )
    print()
    print(
        f"Applied: weight set {row_id} is now active. Revert any time with "
        "`thoth substrate recall weights --revert`."
    )


async def weights(
    conn: "asyncpg.Connection",
    *,
    limit: int = 10,
    revert: bool = False,
) -> None:
    """Show the tuned-weight audit trail, or revert to the config baseline."""
    from substrate.recall import weights_store as _store

    if revert:
        demoted = await _store.deactivate_all(conn)
        if demoted:
            print(
                "Reverted: no tuned weight set is active; recall runs on the "
                "config/env baseline (live within the cache TTL)."
            )
        else:
            print("(nothing to revert — no tuned weight set was active)")
        return

    rows = await _store.history(conn, limit=limit)
    if not rows:
        print(
            "(no tuned weight sets stored yet — run "
            "`thoth substrate recall tune` once the recall log has labels)"
        )
        return

    from substrate.recall.replay import baseline_weights

    print(f"config baseline: {baseline_weights().label()}")
    print()
    for r in rows:
        marker = "ACTIVE" if r["active"] else "      "
        w = r["weights"]
        label = w.label() if w is not None else "(corrupt row)"
        hold = (
            f"{r['holdout_metric']:+.4f}"
            if r["holdout_metric"] is not None
            else "n/a"
        )
        base = (
            f"{r['baseline_holdout_metric']:+.4f}"
            if r["baseline_holdout_metric"] is not None
            else "n/a"
        )
        print(
            f"{marker}  {r['created_at']:%Y-%m-%d %H:%M}  {label}\n"
            f"        id={r['id']}  source={r['source']}  "
            f"corpus={r['corpus_size']}  holdout sep {hold} vs baseline {base}"
        )


__all__ = [
    "print_summary",
    "print_recent",
    "print_sample",
    "print_config",
    "validate",
    "replay",
    "tune",
    "weights",
]
