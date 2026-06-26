"""Fixtures shared across tests/tui_gateway/.

Two Phase-0 patches both apply to every test in this subdirectory:

1.  ``tui_gateway.server`` wraps every SessionDB method with
    ``_thoth_db.run_sync(db.<method>(...))`` because
    ``_AsyncSessionDB`` methods are coroutines. The ``FakeDB`` classes
    used by ``test_protocol.py`` and friends are sync — their methods
    return plain values, not coroutines — so the real ``run_sync``
    crashes with ``TypeError: An asyncio.Future, a coroutine or an
    awaitable is required``. ``_pass_through_run_sync_for_sync_mocks``
    monkeypatches ``run_sync`` to pass non-awaitable values through;
    real coroutines (from production code paths under test) still go
    through the real ``run_sync``.

2.  ``thoth_cli.goals.GoalManager`` persists state via
    ``SessionDB.set_meta`` / ``get_meta`` — both async, both run
    through ``thoth_db.run_sync``. The ``test_goal_command.py`` tests
    rely on those writes landing for follow-up assertions, but the
    GoalManager swallows DB errors (intentionally — it must not crash
    the agent if the DB is down), so without a migrated per-test PG
    database the assertions silently see ``mgr.state is None``.
    ``_ensure_goal_pg_db`` requests ``thoth_db_initialized_sync`` for
    the goal tests so Alembic upgrade head has run and the asyncpg
    pool is bound to a real schema before any ``/goal`` command fires.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.fixture(autouse=True)
def _pass_through_run_sync_for_sync_mocks(monkeypatch):
    """See module docstring (issue 1)."""
    import thoth_db

    real_run_sync = thoth_db.run_sync

    def _passthrough(value):
        if inspect.iscoroutine(value) or inspect.isawaitable(value):
            return real_run_sync(value)
        return value

    monkeypatch.setattr(thoth_db, "run_sync", _passthrough)


@pytest.fixture(autouse=True)
def _ensure_goal_pg_db(request, thoth_db_initialized_sync):
    """See module docstring (issue 2)."""
    return thoth_db_initialized_sync
