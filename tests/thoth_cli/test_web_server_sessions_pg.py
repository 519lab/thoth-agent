"""Tests for the dashboard session endpoints' Postgres-backend bridging.

When ``SessionDB`` is backed by Postgres (``_AsyncSessionDB``, no ``_conn``)
the dashboard has to bridge two backend-shape gaps that don't exist on SQLite:

  1. asyncpg pools are loop-bound, so DB coroutines awaited on the dashboard's
     uvicorn loop must be routed to thoth_db's pool loop
     (``_route_session_db``). On SQLite the connection is thread-bound and must
     NOT hop loops.
  2. PG returns ``timestamptz`` columns as ``datetime``; the SQLite contract
     and the frontend expect epoch floats (``_epochify``).

Regression coverage for the bug where every /api/sessions* endpoint 500'd on a
PG-backed install (``got Future attached to a different loop`` →
``float - datetime`` TypeError).
"""

import datetime as dt

import pytest

from thoth_cli.web_server import _epochify, _route_session_db


class TestEpochify:
    def test_datetime_becomes_epoch_float(self):
        d = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
        out = _epochify(d)
        assert isinstance(out, float)
        assert out == d.timestamp()

    def test_normalizes_datetimes_inside_dict(self):
        started = dt.datetime(2026, 6, 9, tzinfo=dt.timezone.utc)
        row = {"id": "abc", "started_at": started, "title": "x", "ended_at": None}
        out = _epochify(row)
        assert out["started_at"] == started.timestamp()
        assert out["id"] == "abc"
        assert out["title"] == "x"
        assert out["ended_at"] is None

    def test_normalizes_list_of_rows(self):
        ts = dt.datetime(2026, 6, 9, 12, tzinfo=dt.timezone.utc)
        rows = [{"timestamp": ts}, {"timestamp": ts, "role": "user"}]
        out = _epochify(rows)
        assert all(r["timestamp"] == ts.timestamp() for r in out)
        assert out[1]["role"] == "user"

    def test_nested_response_shape(self):
        ts = dt.datetime(2026, 6, 9, tzinfo=dt.timezone.utc)
        payload = {"session_id": "s1", "messages": [{"id": 1, "timestamp": ts}]}
        out = _epochify(payload)
        assert out["messages"][0]["timestamp"] == ts.timestamp()

    def test_leaves_scalars_and_jsonb_untouched(self):
        payload = {
            "n": 5,
            "f": 1.5,
            "s": "text",
            "b": True,
            "none": None,
            "model_config": {"max_tokens": None, "nested": {"a": 1}},
        }
        out = _epochify(payload)
        assert out == payload


@pytest.mark.asyncio
class TestRouteSessionDB:
    async def test_sqlite_awaits_directly(self, monkeypatch):
        """On SQLite (db has ``_conn``) the coroutine is awaited on the caller's
        loop and the pool-loop bridge is never invoked."""
        import thoth_db

        called = {"routed": False}

        async def _boom(_coro):
            called["routed"] = True
            return "routed"

        monkeypatch.setattr(thoth_db, "run_on_pool_loop", _boom, raising=False)

        class _SqliteDB:
            _conn = object()

        async def _query():
            return "direct"

        result = await _route_session_db(_SqliteDB(), _query())
        assert result == "direct"
        assert called["routed"] is False

    async def test_postgres_routes_to_pool_loop(self, monkeypatch):
        """On PG (no ``_conn``) the coroutine is handed to
        ``thoth_db.run_on_pool_loop``."""
        import thoth_db

        seen = {}

        async def _fake_route(coro):
            seen["coro"] = coro
            return await coro

        monkeypatch.setattr(thoth_db, "run_on_pool_loop", _fake_route, raising=False)

        class _PgDB:
            pass  # no _conn attribute

        async def _query():
            return 42

        result = await _route_session_db(_PgDB(), _query())
        assert result == 42
        assert "coro" in seen


def test_startup_event_initializes_db_pool(monkeypatch):
    """The dashboard's startup event must initialize the thoth_db pool, or every
    PG-backed /api/sessions and /api/logs call 500s with 'thoth_db.init() not
    called'. Regression for the standalone-dashboard pool-init bug."""
    from starlette.testclient import TestClient

    import thoth_db
    from thoth_cli.web_server import app

    called = {}

    async def _fake_init(dsn, **kwargs):
        called["dsn"] = dsn

    async def _fake_run_on_pool_loop(coro):
        return await coro

    monkeypatch.setattr(thoth_db, "init", _fake_init, raising=False)
    monkeypatch.setattr(thoth_db, "run_on_pool_loop", _fake_run_on_pool_loop, raising=False)
    monkeypatch.setenv("THOTH_PG_DSN", "postgresql://u:p@localhost:5432/thoth")

    # Entering the TestClient context fires ASGI lifespan startup.
    with TestClient(app):
        pass

    assert called.get("dsn") == "postgresql://u:p@localhost:5432/thoth"


def test_startup_event_noops_without_dsn(monkeypatch):
    """No THOTH_PG_DSN → the startup init is a warning no-op, never a crash."""
    from starlette.testclient import TestClient

    import thoth_db
    from thoth_cli.web_server import app

    called = {"init": False}

    async def _fake_init(dsn, **kwargs):
        called["init"] = True

    monkeypatch.setattr(thoth_db, "init", _fake_init, raising=False)
    monkeypatch.delenv("THOTH_PG_DSN", raising=False)

    with TestClient(app):
        pass

    assert called["init"] is False
