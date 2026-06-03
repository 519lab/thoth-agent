"""Embedding client wrapper — Phase C spec §5.7 (real) + §9.6b (mock).

Two paths:

  * **Real**: OpenAI-compatible ``text-embedding-3-small`` endpoint.
    Routing order (first match wins):

      1. ``auxiliary.embedding.provider`` config — explicit provider
         (``openai`` | ``openrouter`` | ``custom``) with optional
         ``base_url`` / ``api_key`` / ``model``.
      2. ``OPENAI_API_KEY`` env → OpenAI direct.
      3. ``OPENROUTER_API_KEY`` env → OpenRouter proxying
         ``openai/text-embedding-3-small``.
      4. None of the above → return None for the whole batch (recall
         falls back to keyword Jaccard). Logged ONCE at WARN level so
         operators can see what's missing.

    The client is constructed lazily and cached. Cache invalidates via
    ``reset_client_cache()`` (test seam).

  * **Mock**: enabled via ``HERMES_RECALL_EMBEDDING_MOCK=1``. Uses
    SHA-256(input) to seed a deterministic pseudo-random 1536-d
    vector, then normalises to unit length. Two identical inputs
    produce identical vectors so ranker tests are stable. No
    network. Free.

The real path is constructed lazily — importing this module does NOT
require ``openai`` to be installed (the module-level imports stay clean).
The mock path is always available.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
from typing import Optional

from substrate import config as _substrate_config  # noqa: F401  (forward use)


# Dimension was originally pinned at 1536 (Phase C / Alembic 0006).
# Phase C-cleanup migration 0009 makes it configurable via the
# ``HERMES_EMBEDDING_DIM`` env var, with 1536 retained as the default
# for back-compat. The actual column dimension is read from PG at
# first use via ``_get_schema_dim`` — env var only matters at
# migration time. ``EMBEDDING_DIM`` is preserved here as a fallback
# (for code paths and tests that read it before any embed() call) and
# for the mock path which generates fixed-size deterministic vectors.
EMBEDDING_DIM = 1536
MOCK_ENV_VAR = "HERMES_RECALL_EMBEDDING_MOCK"
API_KEY_ENV_VAR = "OPENAI_API_KEY"

_log = logging.getLogger("substrate.recall.embeddings")

_client = None  # OpenAI-compatible client cache — created lazily.
_unconfigured_warned = False  # one-shot operator warning
_schema_dim_cache: Optional[int] = None  # cached PG column dim, see _get_schema_dim


async def _get_schema_dim() -> int:
    """Read the live ``substrate_slices.embedding`` column dimension.

    Cached after first read — the dim doesn't change without a
    migration + restart. Falls back to the module-level
    ``EMBEDDING_DIM`` constant if PG is unreachable or the column
    introspection fails (defensive — the dim guard in embed() then
    uses the constant).
    """
    global _schema_dim_cache
    if _schema_dim_cache is not None:
        return _schema_dim_cache
    try:
        import hermes_db
        async with hermes_db.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT format_type(atttypid, atttypmod) AS coltype
                  FROM pg_attribute
                 WHERE attrelid = 'substrate_slices'::regclass
                   AND attname  = 'embedding'
                   AND NOT attisdropped
                """
            )
        if row and row["coltype"] and row["coltype"].startswith("vector("):
            _schema_dim_cache = int(row["coltype"][len("vector("):-1])
            return _schema_dim_cache
    except Exception:
        # PG unreachable, asyncpg not initialised, column missing —
        # fall through to the module constant.
        pass
    _schema_dim_cache = EMBEDDING_DIM
    return _schema_dim_cache


def reset_schema_dim_cache() -> None:
    """Test seam — re-read schema dim on next embed()."""
    global _schema_dim_cache
    _schema_dim_cache = None


def _is_mock_enabled() -> bool:
    raw = os.environ.get(MOCK_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mock_embed_one(text: str) -> list[float]:
    """SHA-256-seeded deterministic mock. Unit-norm 1536-d vector.

    The hash digest is 32 bytes; we use a counter-mode expansion to
    derive ``EMBEDDING_DIM * 4`` bytes of entropy, interpret each
    4-byte block as a float in (-1, 1) via int-to-float mapping, then
    normalise.
    """
    base = hashlib.sha256(text.encode("utf-8")).digest()
    # Derive enough entropy: SHA-256 chained with counter index.
    needed = EMBEDDING_DIM * 4
    chunks: list[bytes] = []
    counter = 0
    while sum(len(c) for c in chunks) < needed:
        chunks.append(hashlib.sha256(base + counter.to_bytes(4, "big")).digest())
        counter += 1
    raw = b"".join(chunks)[:needed]
    # Map each 4-byte block to a float in (-1, 1).
    vec: list[float] = []
    for i in range(0, needed, 4):
        n = int.from_bytes(raw[i : i + 4], "big", signed=False)
        # Scale to (-1, 1). u32 range is [0, 2^32 - 1].
        vec.append((n / (2**32 - 1)) * 2.0 - 1.0)
    # Normalise to unit length.
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        # Defensive — astronomically unlikely with SHA-256.
        return [1.0 / math.sqrt(EMBEDDING_DIM)] * EMBEDDING_DIM
    return [x / norm for x in vec]


def _resolve_embedding_provider() -> Optional[dict]:
    """Pick an OpenAI-compatible embedding endpoint from config + env.

    Returns a dict with ``api_key`` and (optional) ``base_url`` keys, or
    ``None`` if no embedding-capable provider is configured. Routing
    order (first match wins):

      1. ``auxiliary.embedding.provider`` config in ``config.yaml``
      2. ``OPENAI_API_KEY`` env → OpenAI direct
      3. ``OPENROUTER_API_KEY`` env → OpenRouter
         (proxies ``openai/text-embedding-3-small``)

    Provider names map to defaults if the config doesn't override:
      - ``openai``     → no base_url (SDK default), key=OPENAI_API_KEY
      - ``openrouter`` → https://openrouter.ai/api/v1, key=OPENROUTER_API_KEY
      - ``custom``     → config-provided base_url + api_key (e.g. Ollama,
                         vLLM, LM Studio on localhost)
    """
    # 1. Config-driven.
    cfg_block = {}
    try:
        from thoth_cli.config import load_config
        cfg = load_config() or {}
        cfg_block = (cfg.get("auxiliary") or {}).get("embedding") or {}
    except Exception:
        cfg_block = {}

    provider = (cfg_block.get("provider") or "").strip().lower() or None
    cfg_base_url = (cfg_block.get("base_url") or "").strip() or None
    cfg_api_key = (cfg_block.get("api_key") or "").strip() or None

    if provider == "openai":
        key = cfg_api_key or os.environ.get(API_KEY_ENV_VAR)
        if key:
            return {"api_key": key, "base_url": cfg_base_url}
    elif provider == "openrouter":
        key = cfg_api_key or os.environ.get("OPENROUTER_API_KEY")
        if key:
            return {
                "api_key": key,
                "base_url": cfg_base_url or "https://openrouter.ai/api/v1",
            }
    elif provider == "custom":
        # Custom endpoint: base_url is required; api_key may be a dummy
        # for local servers that ignore it (Ollama).
        if cfg_base_url:
            return {
                "api_key": cfg_api_key or "not-needed",
                "base_url": cfg_base_url,
            }

    # 2-3. Env-var auto-detection (no explicit config).
    if os.environ.get(API_KEY_ENV_VAR):
        return {"api_key": os.environ[API_KEY_ENV_VAR], "base_url": None}
    if os.environ.get("OPENROUTER_API_KEY"):
        return {
            "api_key": os.environ["OPENROUTER_API_KEY"],
            "base_url": "https://openrouter.ai/api/v1",
        }

    return None


def _resolve_default_model() -> str:
    """Read ``auxiliary.embedding.model`` from config, defaulting to
    ``text-embedding-3-small``. Models that don't return 1536-d vectors
    will trip the dim guard at first call — change schema first."""
    try:
        from thoth_cli.config import load_config
        cfg = load_config() or {}
        block = (cfg.get("auxiliary") or {}).get("embedding") or {}
        m = (block.get("model") or "").strip()
        if m:
            return m
    except Exception:
        pass
    # OpenRouter proxies OpenAI's embeddings under the ``openai/`` prefix.
    if os.environ.get("OPENROUTER_API_KEY") and not os.environ.get(API_KEY_ENV_VAR):
        return "openai/text-embedding-3-small"
    return "text-embedding-3-small"


def _resolve_dimensions() -> Optional[int]:
    """Read ``auxiliary.embedding.dimensions`` from config — the output dim to
    request from the provider (Matryoshka truncation for models that support
    it, e.g. Qwen3-Embedding). ``None`` → let the model return its native dim.

    Useful when a model's native dim exceeds pgvector's 2000-d index cap
    (Qwen3-Embedding-4B is 2560): set ``dimensions: 1024`` to keep the column
    indexable. The provider must honor the OpenAI ``dimensions`` param."""
    try:
        from thoth_cli.config import load_config
        cfg = load_config() or {}
        block = (cfg.get("auxiliary") or {}).get("embedding") or {}
        raw = block.get("dimensions")
        if raw is not None:
            d = int(raw)
            if d > 0:
                return d
    except Exception:
        pass
    return None


def _ensure_client():
    """Create the OpenAI-compatible AsyncClient on first use. Raises
    ``RuntimeError`` if no embedding provider is configured (the caller
    catches this and falls back to keyword ranking)."""
    global _client, _unconfigured_warned
    if _client is not None:
        return _client

    resolved = _resolve_embedding_provider()
    if resolved is None:
        if not _unconfigured_warned:
            _unconfigured_warned = True
            _log.warning(
                "substrate.embeddings.unconfigured — no embedding provider "
                "found. Recall will use keyword Jaccard only (no semantic "
                "ranking). To enable: set OPENAI_API_KEY in env, OR set "
                "auxiliary.embedding.{provider,model,api_key,base_url} in "
                "config.yaml (provider=openai|openrouter|custom). For a "
                "local Ollama: provider=custom, base_url=http://localhost:"
                "11434/v1, model=nomic-embed-text (note: non-1536-d models "
                "require an Alembic schema change before they work)."
            )
        raise RuntimeError("no embedding provider configured")

    # Late import — keeps recall package importable without openai if
    # only the mock path is in use.
    import openai

    kwargs = {"api_key": resolved["api_key"]}
    if resolved.get("base_url"):
        kwargs["base_url"] = resolved["base_url"]
    _client = openai.AsyncOpenAI(**kwargs)
    return _client


def reset_client_cache() -> None:
    """Test seam — drop the cached client so a subsequent ``embed`` call
    re-reads the env vars and re-constructs the client."""
    global _client
    _client = None


async def embed(
    texts: list[str],
    *,
    model: Optional[str] = None,
    timeout_ms: int = 800,
    dimensions: Optional[int] = None,
) -> list[Optional[list[float]]]:
    """Return one embedding vector per input (model's dim, or the configured
    ``dimensions``). None per-item on failure.

    Mock path: uses ``_mock_embed_one`` for each input. No network.
    Real path: single batch ``client.embeddings.create(...)`` call against
    the resolved OpenAI-compatible endpoint (see ``_resolve_embedding_provider``).

    ``model`` defaults to ``auxiliary.embedding.model`` config or
    ``text-embedding-3-small`` (or ``openai/text-embedding-3-small`` when
    routing through OpenRouter without an OpenAI key).

    On batch-level failure (network error, API error, timeout, no provider
    configured) returns a list of ``None`` of the right length so the
    caller can substitute keyword Jaccard for every item without
    special-casing.
    """
    if not texts:
        return []

    if _is_mock_enabled():
        return [_mock_embed_one(t) for t in texts]

    try:
        client = _ensure_client()
    except RuntimeError:
        # No provider configured → keyword fallback for the whole batch.
        return [None] * len(texts)

    if model is None:
        model = _resolve_default_model()
    if dimensions is None:
        dimensions = _resolve_dimensions()

    # Only pass ``dimensions`` when set — providers/models that don't support
    # MRL truncation reject the param, so omitting it keeps the default path.
    create_kwargs: dict = {"model": model, "input": texts}
    if dimensions is not None:
        create_kwargs["dimensions"] = dimensions

    try:
        resp = await asyncio.wait_for(
            client.embeddings.create(**create_kwargs),
            timeout=timeout_ms / 1000.0,
        )
    except (asyncio.TimeoutError, Exception):
        # Any embedding-side error: caller falls back to keyword.
        return [None] * len(texts)

    # Dim guard: model output must match the PG column's vector(N) shape
    # exactly. Read the schema's actual dim (configurable via
    # HERMES_EMBEDDING_DIM at migration time) instead of the module
    # constant so users who reshape their schema for a 768-d local model
    # don't get false-positive mismatch errors.
    schema_dim = await _get_schema_dim()
    out: list[Optional[list[float]]] = []
    for d in resp.data:
        vec = list(d.embedding)
        if len(vec) != schema_dim:
            raise RuntimeError(
                f"embedding dim mismatch: got {len(vec)}, schema expects "
                f"{schema_dim} (model={model!r}). Either pick a model whose "
                f"output dim matches the schema, OR set HERMES_EMBEDDING_DIM="
                f"{len(vec)} in env + re-run migrations to reshape the column."
            )
        out.append(vec)
    # Pad with None if the API returned fewer items than requested
    # (shouldn't happen but defensive).
    while len(out) < len(texts):
        out.append(None)
    return out


async def embed_query(
    query: str,
    *,
    model: Optional[str] = None,
    timeout_ms: int = 800,
) -> Optional[list[float]]:
    """Convenience wrapper for the recall pipeline. Returns the single
    embedding vector or None on failure (timeout / error / no provider
    configured). ``model`` defaults via ``_resolve_default_model``."""
    if not query:
        return None
    out = await embed([query], model=model, timeout_ms=timeout_ms)
    if not out:
        return None
    return out[0]


__all__ = [
    "EMBEDDING_DIM",
    "MOCK_ENV_VAR",
    "API_KEY_ENV_VAR",
    "embed",
    "embed_query",
    "reset_client_cache",
]
