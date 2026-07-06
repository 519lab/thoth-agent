"""``thoth embed`` — embedding admin commands.

Surface:

    thoth embed reshape <DIM>     # reshape pgvector column + re-embed

Distinct from ``thoth substrate`` (read-only inspection): these commands
mutate the substrate's embedding state. Lives at the top level rather
than under ``thoth substrate`` because embedding is its own user-visible
concern (config, model choice, dim, cost) — not just substrate internals.

Future expansion (not in this PR):
    thoth embed status            # coverage, cost since last reset, last error
    thoth embed backfill          # force a re-embed pass over the NULL queue
    thoth embed test              # 1-call probe of the configured provider

Wired into Thoth's top-level argparse via :func:`register_subparser`
called from ``thoth_cli/main.py``.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


# ---------------------------------------------------------------------------
# Subparser registration — called from thoth_cli/main.py.
# ---------------------------------------------------------------------------


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``thoth embed`` subcommand tree to ``subparsers``."""
    embed_parser = subparsers.add_parser(
        "embed",
        help="Embedding admin (reshape pgvector column, re-embed)",
        description="Admin commands for the substrate's embedding column. "
        "Inspection lives under ``thoth substrate recall``; this namespace "
        "is for state-mutating operations.",
    )
    embed_sub = embed_parser.add_subparsers(dest="embed_command")

    reshape_p = embed_sub.add_parser(
        "reshape",
        help="Reshape ALL embedding columns to a new dimension + re-embed",
        description=(
            "Change every substrate embedding column (substrate_slices and the "
            "L3/L4 curation columns) from its current vector(N) shape to "
            "vector(<DIM>) — they're kept in lockstep since the Curator embeds "
            "all layers with one model. Existing embeddings are NOT convertible "
            "across dims and are cleared; slices are re-embedded inline using "
            "the configured provider (see auxiliary.embedding.* in config.yaml), "
            "and L3/L4 by the Curator's curation pass. Interactive y/N prompt "
            "before any destructive work — pass --yes to skip."
        ),
    )
    reshape_p.add_argument(
        "dim",
        type=int,
        help="Target embedding dimension (1-16000, pgvector cap). Must match "
        "the configured model's native output dim — see the dim guard in "
        "substrate/recall/embeddings.py.",
    )
    reshape_p.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the y/N confirmation prompt.",
    )
    reshape_p.add_argument(
        "--no-reembed",
        action="store_true",
        help="Reshape the column only; don't re-embed inline. The Curator's "
        "background backfill will re-populate on its normal cadence (slower; "
        "useful for non-interactive setups).",
    )
    reshape_p.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Slices per embedding-API call during the re-embed pass "
        "(default 50; lower if hitting provider rate limits).",
    )
    reshape_p.set_defaults(func=_cmd_embed_reshape)

    reembed_p = embed_sub.add_parser(
        "reembed",
        help="Re-embed ALL slices in place with the configured provider "
        "(same dimension — for a model/provider change)",
        description=(
            "Clear every existing embedding and re-embed inline using the "
            "configured provider (auxiliary.embedding.* in config.yaml). Unlike "
            "``reshape``, this does NOT change the column dimension — use it "
            "when the MODEL changed but the dim is the same (e.g. swapping "
            "text-embedding-3-small for qwen3-embedding:8b @ dimensions=1536). "
            "``reshape`` short-circuits at an unchanged dim and would leave the "
            "old vectors in place; this forces the re-embed. Existing vectors "
            "are cleared and repopulated; recall degrades to keyword-only until "
            "the pass completes. Interactive y/N prompt (shows the target DB) "
            "before any destructive work — pass --yes to skip."
        ),
    )
    reembed_p.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the y/N confirmation prompt.",
    )
    reembed_p.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Slices per embedding-API call (default 64; lower for rate limits).",
    )
    reembed_p.set_defaults(func=_cmd_embed_reembed)

    retry_p = embed_sub.add_parser(
        "retry-failed",
        help="Un-park slices marked embedding_failed so they get re-embedded",
        description=(
            "Clear the ``embedding_failed`` marker on parked slices so they "
            "re-enter the Curator's embedding queue. Slices that exhaust their "
            "retry budget are parked and excluded forever (so a broken provider "
            "isn't hammered) — but nothing un-parks them once you fix the cause "
            "(dim mismatch, unreachable endpoint, wrong model name). Run this "
            "after fixing the config for immediate recovery. The Curator also "
            "auto-heals a small batch per long interval "
            "(THOTH_RECALL_EMBEDDING_RETRY_FAILED_INTERVAL_S), so this is the "
            "manual fast-path."
        ),
    )
    retry_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max parked slices to un-park (newest-first). Default: all.",
    )
    retry_p.set_defaults(func=_cmd_embed_retry_failed)

    embed_parser.set_defaults(func=_cmd_embed_help)


def _cmd_embed_help(args: argparse.Namespace) -> int:
    """Default for ``thoth embed`` with no subcommand."""
    print(
        "usage: thoth embed {reshape <DIM> | reembed | retry-failed}",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# reembed command — re-embed all slices in place at the SAME dim (model swap).
# reshape short-circuits when the dim is unchanged, so a provider/model change
# at a fixed dim (e.g. text-embedding-3-small -> qwen3-embedding:8b @1536) needs
# this to force the clear-and-repopulate.
# ---------------------------------------------------------------------------


def _dsn_target_str() -> str:
    """Human-readable ``host:port/dbname`` from ``THOTH_PG_DSN`` (password
    stripped). This is the value that selects the instance — 5432 (live) vs
    5433 (test) — so it is the safety-critical line to eyeball before a
    destructive re-embed. Returns ``<THOTH_PG_DSN unset>`` if absent and a
    coarse fallback if the DSN doesn't parse."""
    import os
    from urllib.parse import urlparse

    dsn = os.environ.get("THOTH_PG_DSN")
    if not dsn:
        return "<THOTH_PG_DSN unset>"
    try:
        u = urlparse(dsn)
        host = u.hostname or "local-socket"
        port = u.port or 5432
        dbname = (u.path or "").lstrip("/") or "?"
        return f"{host}:{port}/{dbname}"
    except Exception:
        return "<unparseable DSN>"


def _cmd_embed_reembed(args: argparse.Namespace) -> int:
    """Validate connection, confirm (showing the target DB), then re-embed."""
    import thoth_db

    if not thoth_db.ensure_pool_sync():
        print(
            "error: THOTH_PG_DSN not set; cannot connect to substrate PG.",
            file=sys.stderr,
        )
        return 1
    return thoth_db.run_sync(
        _reembed_async(interactive=not args.yes, batch_size=args.batch_size)
    )


async def _reembed_async(*, interactive: bool, batch_size: int) -> int:
    """Clear every embedding column then re-embed slices inline.

    Same dim as the current schema (no ALTER) — only the model changed. L3/L4
    columns are cleared too and re-populate via the Curator's curation pass,
    keeping every layer on one model (see ``_reshape_async``)."""
    import thoth_db

    current = await _current_schema_dim()
    if current is None:
        print(
            "error: substrate_slices.embedding column not found. Run "
            "``alembic upgrade head`` first.",
            file=sys.stderr,
        )
        return 1

    async with thoth_db.connection() as conn:
        db = await conn.fetchval("SELECT current_database()")
        total = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices"
        ) or 0
        dims = {t: await _table_vector_dim(conn, t) for t in _EMBEDDING_TABLES}

    tables = [t for t, d in dims.items() if d is not None]
    # Show the DSN's own host:port/db — the value that actually selects the
    # instance (5432 live vs 5433 test) — NOT the server-internal
    # inet_server_port(), which reports the container-internal 5432 for BOTH
    # the live and test containers and so masks which one you're hitting.
    print(f"About to RE-EMBED all slices at vector({current}) (no dim change):")
    print(f"  target: {_dsn_target_str()}  (server reports current_database={db})")
    print(f"  columns cleared + repopulated: {', '.join(tables)}")
    print(f"  substrate_slices to re-embed: {total:,} (inline)")
    print("  provider: configured auxiliary.embedding.* (recall degrades to "
          "keyword-only until this completes)")

    if interactive:
        try:
            ans = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in {"y", "yes"}:
            print("Aborted.")
            return 1

    print(f"Clearing embeddings on {len(tables)} column(s) ...")
    async with thoth_db.transaction() as conn:
        for t in tables:
            await conn.execute(f"UPDATE {t} SET embedding = NULL")
    # Drop the cached schema dim so embed() re-reads it (unchanged here, but
    # keeps behaviour identical to reshape's post-alter reset).
    try:
        from substrate.recall import embeddings as _embed_mod
        _embed_mod.reset_schema_dim_cache()
    except Exception:
        pass

    return await _backfill_inline(total=total, batch_size=batch_size)


# ---------------------------------------------------------------------------
# retry-failed command — clear the embedding_failed marker so parked slices
# get another embedding attempt.
# ---------------------------------------------------------------------------


def _cmd_embed_retry_failed(args: argparse.Namespace) -> int:
    import thoth_db

    limit = args.limit
    if limit is not None and limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return 2
    if not thoth_db.ensure_pool_sync():
        print(
            "error: THOTH_PG_DSN not set; cannot connect to substrate PG.",
            file=sys.stderr,
        )
        return 1
    # Drive via thoth_db.run_sync so the coro runs on the pool's bound loop
    # (same cross-loop constraint reshape documents).
    return thoth_db.run_sync(_retry_failed_async(limit=limit))


async def _retry_failed_async(*, limit: "int | None") -> int:
    import thoth_db

    async with thoth_db.connection() as conn:
        parked = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices "
            "WHERE (metadata->>'embedding_failed') = 'true'"
        ) or 0
        if parked == 0:
            print("No slices are parked as embedding_failed — nothing to do.")
            return 0
        if limit is None:
            cleared = await conn.fetchval(
                """
                WITH cleared AS (
                    UPDATE substrate_slices
                       SET metadata = metadata - 'embedding_failed'
                     WHERE (metadata->>'embedding_failed') = 'true'
                    RETURNING 1
                )
                SELECT count(*) FROM cleared
                """
            )
        else:
            cleared = await conn.fetchval(
                """
                WITH targets AS (
                    SELECT slice_id FROM substrate_slices
                     WHERE (metadata->>'embedding_failed') = 'true'
                     ORDER BY ingest_time_world DESC
                     LIMIT $1
                ), cleared AS (
                    UPDATE substrate_slices s
                       SET metadata = s.metadata - 'embedding_failed'
                      FROM targets t
                     WHERE s.slice_id = t.slice_id
                    RETURNING 1
                )
                SELECT count(*) FROM cleared
                """,
                limit,
            )
    cleared = cleared or 0
    remaining = parked - cleared
    print(
        f"Un-parked {cleared:,} of {parked:,} embedding_failed slice(s)."
        + (f" {remaining:,} still parked (raise --limit)." if remaining else "")
    )
    print(
        "The Curator re-embeds them on its next backfill tick. If they fail "
        "again, re-check auxiliary.embedding.* (model, base_url, dimensions) "
        "vs the schema dim — see `thoth embed reshape`."
    )
    return 0


# ---------------------------------------------------------------------------
# reshape command — sync wrapper that bridges to the async implementation.
# ---------------------------------------------------------------------------


def _cmd_embed_reshape(args: argparse.Namespace) -> int:
    """Validate args, prompt for confirmation, then drive the reshape."""
    import thoth_db

    target = args.dim
    if target < 1 or target > 16000:
        print(
            f"error: dim must be between 1 and 16000 (got {target})",
            file=sys.stderr,
        )
        return 2

    if not thoth_db.ensure_pool_sync():
        print(
            "error: THOTH_PG_DSN not set; cannot connect to substrate PG.",
            file=sys.stderr,
        )
        return 1

    # MUST drive via thoth_db.run_sync, not asyncio.get_event_loop() / asyncio.run:
    # ensure_pool_sync() bound the asyncpg pool to thoth_db's persistent
    # ``_sync_loop``. Running the coro on any other loop hits asyncpg's
    # "another operation is in progress" / "attached to a different loop"
    # cross-loop error (same failure class as the 2026-05-26 incident).
    return thoth_db.run_sync(
        _reshape_async(
            target=target,
            interactive=not args.yes,
            reembed=not args.no_reembed,
            batch_size=args.batch_size,
        )
    )


async def _reshape_async(
    *,
    target: int,
    interactive: bool,
    reembed: bool,
    batch_size: int,
) -> int:
    """Reshape ALL substrate embedding columns to ``target``, optionally
    re-embed slices inline.

    Every layer that carries an embedding (``substrate_slices`` + the L3/L4
    curation columns) is moved together — the Curator embeds them all with one
    model, so a split dim would stall the L3/L4 backfill. Slices are re-embedded
    inline here; L3/L4 are re-embedded by the Curator's curation pass (or
    ``scripts/curate_upper_now.py``)."""
    import thoth_db

    # 1. Read current per-table dims (slices is required; L3/L4 may be absent
    #    on a pre-0020 DB → skipped).
    current = await _current_schema_dim()
    if current is None:
        print(
            "error: substrate_slices.embedding column not found. Run "
            "``alembic upgrade head`` first.",
            file=sys.stderr,
        )
        return 1

    async with thoth_db.connection() as conn:
        dims = {t: await _table_vector_dim(conn, t) for t in _EMBEDDING_TABLES}
        embedded = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE embedding IS NOT NULL"
        ) or 0
        unembedded = await conn.fetchval(
            "SELECT count(*) FROM substrate_slices WHERE embedding IS NULL"
        ) or 0

    to_reshape = [t for t, d in dims.items() if d is not None and d != target]
    if not to_reshape:
        print(f"All embedding columns are already vector({target}); nothing to do.")
        if unembedded > 0 and reembed:
            print(
                f"Note: {unembedded} slice(s) still have NULL embeddings — "
                "wait for the Curator's backfill or re-run with re-embed on."
            )
        return 0

    # 2. Confirm.
    total = embedded + unembedded
    print(f"About to reshape embedding columns to vector({target}):")
    for t in to_reshape:
        print(f"  {t}: vector({dims[t]}) -> vector({target})  (embeddings CLEARED)")
    print(f"  substrate_slices to re-embed: {total:,} "
          f"({'inline' if reembed else 'Curator backfill'})")
    if any(t != "substrate_slices" for t in to_reshape):
        print(
            "  L3/L4 embeddings re-populate via the Curator's curation pass "
            "(or scripts/curate_upper_now.py --execute)."
        )
    print(
        "  Cost: re-embed cost depends on your provider (free for local; "
        "metered for cloud)."
    )

    if interactive:
        try:
            ans = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in {"y", "yes"}:
            print("Aborted.")
            return 1

    # 3. Reshape each table (drop index, NULL embeddings, ALTER, recreate index).
    print(f"Reshaping {len(to_reshape)} column(s) to vector({target}) ...")
    async with thoth_db.transaction() as conn:
        for t in to_reshape:
            await _reshape_table(conn, t, target)
            print(f"  {t}: vector({dims[t]}) -> vector({target}), index rebuilt.")

    # Drop the embeddings module's cached dim so the next embed() call
    # picks up the new shape.
    try:
        from substrate.recall import embeddings as _embed_mod
        _embed_mod.reset_schema_dim_cache()
    except Exception:
        pass

    if not reembed:
        print(
            f"Reshape complete. {total:,} slice(s) marked for backfill — the "
            "Curator re-embeds them on its next tick. L3/L4 re-embed via the "
            "Curator's curation pass."
        )
        return 0

    # 4. Inline re-embed pass over slices (L3/L4 left to the Curator).
    return await _backfill_inline(total=total, batch_size=batch_size)


# All substrate tables carrying an embedding column. Kept in lockstep — the
# Curator embeds every layer with one model, so their dims must match.
_EMBEDDING_TABLES = ("substrate_slices", "l3_patterns", "l4_observations")


async def _table_vector_dim(conn, table: str) -> "int | None":
    """Live vector(N) dim of ``<table>.embedding``, or None if the column is
    absent / not a vector. ``table`` is an internal constant (no injection)."""
    assert table in _EMBEDDING_TABLES, f"unexpected table {table!r}"
    row = await conn.fetchrow(
        "SELECT format_type(atttypid, atttypmod) AS coltype FROM pg_attribute "
        f"WHERE attrelid = '{table}'::regclass AND attname = 'embedding' "
        "AND NOT attisdropped"
    )
    if row is None:
        return None
    coltype = row["coltype"] or ""
    if not coltype.startswith("vector("):
        return None
    try:
        return int(coltype[len("vector("):-1])
    except (ValueError, IndexError):
        return None


async def _reshape_table(conn, table: str, target: int) -> None:
    """Drop the cosine index, clear embeddings, alter the column dim, recreate
    the index. ``table`` is an internal constant."""
    assert table in _EMBEDDING_TABLES, f"unexpected table {table!r}"
    await conn.execute(f"DROP INDEX IF EXISTS {table}_embedding_cosine_idx")
    await conn.execute(f"UPDATE {table} SET embedding = NULL")
    await conn.execute(
        f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({target})"
    )
    await conn.execute(
        f"CREATE INDEX {table}_embedding_cosine_idx "
        f"ON {table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


async def _current_schema_dim() -> int | None:
    """Read the live vector(N) dim from pg_catalog. Returns None if the
    column is missing or isn't a vector type."""
    import thoth_db

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT format_type(atttypid, atttypmod) AS coltype
              FROM pg_attribute
             WHERE attrelid = 'substrate_slices'::regclass
               AND attname  = 'embedding'
               AND NOT attisdropped
            """
        )
    if row is None:
        return None
    coltype = (row["coltype"] or "")
    if not coltype.startswith("vector("):
        return None
    try:
        return int(coltype[len("vector("):-1])
    except (ValueError, IndexError):
        return None


async def _backfill_inline(*, total: int, batch_size: int) -> int:
    """Re-embed every NULL-embedding slice. Print progress per batch.

    Returns 0 on full success, 1 if the provider failed at any point
    (the partial state is fine; Curator backfill picks up the rest).
    """
    import thoth_db

    # Late import — embed() needs the schema-dim cache cleared above.
    from substrate.recall.embeddings import embed
    # Reuse the same text extractor the Curator uses so re-embedded
    # vectors compare cleanly to fresh Curator-emitted vectors.
    from substrate.agents.curator import _extract_text_for_embedding
    from substrate import config as _cfg

    # Background backfill uses the generous backfill timeout, NOT the 800ms
    # interactive recall-query timeout — a slow local model would otherwise
    # time out on every batch.
    timeout_ms = _cfg.RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS

    if total == 0:
        print("No slices to embed.")
        return 0

    print(f"Re-embedding {total:,} slice(s) in batches of {batch_size} ...")
    done = 0
    failed = 0

    while True:
        # Pull a batch of NULL-embedding slice rows. Ordered for
        # deterministic resume-after-interrupt behaviour.
        async with thoth_db.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT slice_id, ingest_time_world, payload
                  FROM substrate_slices
                 WHERE embedding IS NULL
                 ORDER BY ingest_time_world
                 LIMIT $1
                """,
                batch_size,
            )
        if not rows:
            break

        texts = [_extract_text_for_embedding(r["payload"]) for r in rows]
        try:
            vectors = await embed(texts, timeout_ms=timeout_ms)
        except Exception as exc:
            print(
                f"  embed() raised: {exc}. Aborting; "
                f"{done:,}/{total:,} re-embedded before failure.",
                file=sys.stderr,
            )
            return 1

        # Write back per row. Skip rows where embed() returned None
        # (provider failure for that item only).
        wrote = 0
        async with thoth_db.transaction() as conn:
            for r, vec in zip(rows, vectors):
                if vec is None:
                    failed += 1
                    continue
                await conn.execute(
                    """
                    UPDATE substrate_slices
                       SET embedding = $1
                     WHERE slice_id = $2
                       AND ingest_time_world = $3
                    """,
                    vec, r["slice_id"], r["ingest_time_world"],
                )
                wrote += 1
        done += wrote
        pct = (done / total * 100.0) if total else 100.0
        print(
            f"  Re-embedded {done:,}/{total:,} ({pct:.1f}%)"
            + (f", {failed} per-item failures" if failed else ""),
            flush=True,
        )

        # No slice in this batch embedded → the same NULL rows will be
        # re-fetched next iteration forever. Bail with a diagnostic instead
        # of spinning (the 2026-06 reshape runaway: every call timed out at
        # 800ms vs a ~15s provider, and the loop sailed past 100%).
        if wrote == 0:
            print(
                f"error: 0 of {len(rows)} slices embedded this batch — the "
                "provider is failing every call (timeout, dim mismatch, or "
                "unreachable endpoint). Aborting to avoid an infinite retry "
                "loop. Check auxiliary.embedding.* (model, base_url, "
                "dimensions vs schema dim) and raise "
                "THOTH_RECALL_EMBEDDING_BACKFILL_TIMEOUT_MS for slow models, "
                "then re-run. The Curator's background backfill resumes the "
                "rest once the cause is fixed.",
                file=sys.stderr,
            )
            return 1

        # Tight provider call — small natural pause keeps us under any
        # provider's per-second rate cap without explicit throttling.
        # No sleep needed here; HTTP round-trip provides backoff.

    if failed:
        print(
            f"Done. {done - failed:,}/{total:,} re-embedded successfully; "
            f"{failed} slice(s) failed and remain NULL — the Curator's "
            "backfill loop will retry them.",
        )
        return 0 if failed < total else 1
    print(f"Done. {done:,}/{total:,} re-embedded successfully.")
    return 0
