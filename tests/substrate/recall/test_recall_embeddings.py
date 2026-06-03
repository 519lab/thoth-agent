"""Embedding client tests — Phase C Task 6b / spec §9.6b."""

from __future__ import annotations

import asyncio
import math
import os

import pytest

from substrate.recall import embeddings


@pytest.fixture(autouse=True)
def _enable_mock(monkeypatch):
    """Default every test to the mock path; tests that need real may opt out."""
    monkeypatch.setenv(embeddings.MOCK_ENV_VAR, "1")
    embeddings.reset_client_cache()


@pytest.mark.asyncio
async def test_embed_mock_path_deterministic():
    """Two calls with the same input return identical vectors."""
    a = await embeddings.embed(["hello"])
    b = await embeddings.embed(["hello"])
    assert a == b
    assert len(a) == 1
    assert a[0] is not None


@pytest.mark.asyncio
async def test_embed_mock_vector_dim():
    """Mock vector length matches the pinned dim (1536)."""
    out = await embeddings.embed(["x"])
    assert out[0] is not None
    assert len(out[0]) == embeddings.EMBEDDING_DIM


@pytest.mark.asyncio
async def test_embed_mock_unit_norm():
    """Mock vector has L2 norm ≈ 1.0 (so cosine == dot product)."""
    out = await embeddings.embed(["unit test"])
    vec = out[0]
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_embed_batch_returns_list_of_vectors():
    """embed(['a', 'b']) → 2 vectors, preserves order."""
    out = await embeddings.embed(["a", "b"])
    assert len(out) == 2
    a_solo = (await embeddings.embed(["a"]))[0]
    b_solo = (await embeddings.embed(["b"]))[0]
    assert out[0] == a_solo
    assert out[1] == b_solo


@pytest.mark.asyncio
async def test_embed_empty_input_returns_empty_list():
    """No inputs → no outputs (no exception)."""
    out = await embeddings.embed([])
    assert out == []


@pytest.mark.asyncio
async def test_embed_query_returns_single_vector():
    """embed_query is the single-string convenience wrapper."""
    vec = await embeddings.embed_query("hello world")
    assert vec is not None
    assert len(vec) == embeddings.EMBEDDING_DIM


@pytest.mark.asyncio
async def test_embed_query_empty_returns_none():
    """Empty query → None (caller falls back to keyword path)."""
    vec = await embeddings.embed_query("")
    assert vec is None


@pytest.mark.asyncio
async def test_embed_different_inputs_produce_different_vectors():
    """Two distinct inputs SHOULD have distinct mock vectors (otherwise
    the mock degenerates to a single representation)."""
    a = await embeddings.embed_query("the user asked about coffee")
    b = await embeddings.embed_query("entirely different content")
    assert a != b


@pytest.mark.asyncio
async def test_embed_real_path_no_provider_returns_none_per_item(monkeypatch):
    """When mock is disabled AND no provider is configured (no
    OPENAI_API_KEY, no OPENROUTER_API_KEY, no custom config), ``embed``
    returns a list of None (signal for keyword fallback)."""
    monkeypatch.delenv(embeddings.MOCK_ENV_VAR, raising=False)
    monkeypatch.delenv(embeddings.API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Bypass any config file that might pin a provider during the test run.
    monkeypatch.setattr(embeddings, "_resolve_embedding_provider", lambda: None)
    embeddings.reset_client_cache()
    out = await embeddings.embed(["x", "y"])
    assert out == [None, None]


@pytest.mark.asyncio
async def test_resolve_provider_openai_env_only(monkeypatch):
    """OPENAI_API_KEY alone → openai routing (no base_url)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(embeddings.API_KEY_ENV_VAR, "sk-test-123")
    monkeypatch.setattr(
        "thoth_cli.config.load_config",
        lambda: {},
    )
    r = embeddings._resolve_embedding_provider()
    assert r is not None
    assert r["api_key"] == "sk-test-123"
    assert r["base_url"] is None


@pytest.mark.asyncio
async def test_resolve_provider_openrouter_env_fallback(monkeypatch):
    """OPENROUTER_API_KEY but no OPENAI key → openrouter routing."""
    monkeypatch.delenv(embeddings.API_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-456")
    monkeypatch.setattr(
        "thoth_cli.config.load_config",
        lambda: {},
    )
    r = embeddings._resolve_embedding_provider()
    assert r is not None
    assert r["api_key"] == "or-test-456"
    assert r["base_url"] == "https://openrouter.ai/api/v1"
    # And the default model auto-flips to the openai/ prefix.
    assert embeddings._resolve_default_model() == "openai/text-embedding-3-small"


@pytest.mark.asyncio
async def test_resolve_provider_custom_config(monkeypatch):
    """Explicit custom config (e.g. Ollama) → custom routing."""
    monkeypatch.setattr(
        "thoth_cli.config.load_config",
        lambda: {
            "auxiliary": {
                "embedding": {
                    "provider": "custom",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "ollama",
                    "model": "nomic-embed-text",
                }
            }
        },
    )
    r = embeddings._resolve_embedding_provider()
    assert r is not None
    assert r["api_key"] == "ollama"
    assert r["base_url"] == "http://localhost:11434/v1"
    assert embeddings._resolve_default_model() == "nomic-embed-text"


@pytest.mark.asyncio
async def test_get_schema_dim_fallback_when_pg_unreachable(monkeypatch):
    """When PG introspection fails, _get_schema_dim falls back to the
    module-level EMBEDDING_DIM constant instead of raising. Recall keeps
    working with the (possibly stale) constant value.

    Implementation: patch the whole introspection helper to raise so
    we exercise the except-branch without juggling async context
    managers in the mock. The real PG-roundtrip path is exercised in
    integration tests that have a live pool.
    """
    embeddings.reset_schema_dim_cache()
    # Force the inner ``import hermes_db`` to fail by removing the
    # entry from sys.modules and shadowing the path. Easier: monkeypatch
    # the hermes_db module to a stub whose ``connection`` access raises.
    import hermes_db
    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("simulated PG down")
    monkeypatch.setattr("substrate.recall.embeddings.hermes_db", _Boom(), raising=False)
    # The function uses a late ``import hermes_db`` inside its body.
    # Easier than fighting that import path: just rig hermes_db.connection
    # to be a no-op context manager that yields a conn whose fetchrow raises.
    import contextlib

    @contextlib.asynccontextmanager
    async def _broken_connection():
        class _C:
            async def fetchrow(self, *_, **__):
                raise RuntimeError("simulated PG down")
        yield _C()

    monkeypatch.setattr(hermes_db, "connection", _broken_connection, raising=True)
    dim = await embeddings._get_schema_dim()
    assert dim == embeddings.EMBEDDING_DIM


def test_schema_dim_cache_resets():
    """reset_schema_dim_cache() drops the cached value so the next
    _get_schema_dim() call re-reads PG. No async; just verifies the
    test seam itself works."""
    embeddings._schema_dim_cache = 999
    embeddings.reset_schema_dim_cache()
    assert embeddings._schema_dim_cache is None


def test_resolve_dimensions_reads_config(monkeypatch):
    """auxiliary.embedding.dimensions → MRL truncation request; absent → None."""
    monkeypatch.setattr(
        "thoth_cli.config.load_config",
        lambda: {"auxiliary": {"embedding": {"dimensions": 1024}}},
    )
    assert embeddings._resolve_dimensions() == 1024
    monkeypatch.setattr("thoth_cli.config.load_config", lambda: {})
    assert embeddings._resolve_dimensions() is None


@pytest.mark.asyncio
async def test_embed_forwards_dimensions(monkeypatch):
    """embed(dimensions=N) passes dimensions=N to the provider and returns
    N-d vectors (MRL truncation for models like Qwen3-Embedding)."""
    monkeypatch.delenv(embeddings.MOCK_ENV_VAR, raising=False)
    captured: dict = {}

    class _Emb:
        async def create(self, **kw):
            captured.update(kw)
            n = kw.get("dimensions", 1536)
            return type("R", (), {
                "data": [type("D", (), {"embedding": [0.0] * n})() for _ in kw["input"]]
            })()

    monkeypatch.setattr(embeddings, "_ensure_client",
                        lambda: type("C", (), {"embeddings": _Emb()})())

    async def _dim():
        return 1024
    monkeypatch.setattr(embeddings, "_get_schema_dim", _dim)

    out = await embeddings.embed(["a", "b"], model="m", dimensions=1024)
    assert captured.get("dimensions") == 1024
    assert len(out) == 2 and all(v is not None and len(v) == 1024 for v in out)


@pytest.mark.asyncio
async def test_embed_omits_dimensions_when_unconfigured(monkeypatch):
    """No configured/passed dimensions → the param is not sent (so providers
    that don't support MRL truncation aren't broken)."""
    monkeypatch.delenv(embeddings.MOCK_ENV_VAR, raising=False)
    monkeypatch.setattr("thoth_cli.config.load_config", lambda: {})
    captured: dict = {}

    class _Emb:
        async def create(self, **kw):
            captured.update(kw)
            return type("R", (), {
                "data": [type("D", (), {"embedding": [0.0] * 1536})() for _ in kw["input"]]
            })()

    monkeypatch.setattr(embeddings, "_ensure_client",
                        lambda: type("C", (), {"embeddings": _Emb()})())

    async def _dim():
        return 1536
    monkeypatch.setattr(embeddings, "_get_schema_dim", _dim)

    await embeddings.embed(["x"], model="m")
    assert "dimensions" not in captured


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get(embeddings.API_KEY_ENV_VAR),
    reason=f"{embeddings.API_KEY_ENV_VAR} not set",
)
async def test_embed_real_path_when_key_present(monkeypatch):
    """When the real API key is available, the real path returns
    1536-d vectors. Skipped by default; lights up in CI/local with the key."""
    monkeypatch.delenv(embeddings.MOCK_ENV_VAR, raising=False)
    embeddings.reset_client_cache()
    out = await embeddings.embed(["hello world"])
    assert len(out) == 1
    assert out[0] is not None
    assert len(out[0]) == embeddings.EMBEDDING_DIM
