"""Tests for the /update slash command in the classic CLI.

Verifies that ``ThothCLI._handle_update_command`` correctly:
- Refuses to run under a managed install (Homebrew, Docker, etc.)
- Sets ``_pending_relaunch`` and returns ``True`` on confirmation
- Cancels cleanly on a "no"-shaped answer or unrecognized input
- Cancels cleanly when ``_prompt_text_input_modal`` returns None (timeout /
  modal dismissed)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli import ThothCLI

import thoth_cli.main as hmain


def _bound(fn, instance):
    """Bind an unbound method to a stand-in instance."""
    return fn.__get__(instance, type(instance))


def _make_self(modal_response):
    """Build a minimal stand-in 'self' for ``_handle_update_command``.

    Uses the same SimpleNamespace pattern as ``test_destructive_slash_confirm``
    so we don't need a full ``ThothCLI`` construction.
    ``_prompt_text_input_modal`` is stubbed to return *modal_response*
    directly so tests can drive the entire confirmation branch without
    touching stdin or prompt_toolkit internals.
    """
    self_ = SimpleNamespace(
        _app=None,
        _pending_relaunch=None,
        _prompt_text_input_modal=lambda **_kw: modal_response,
    )
    self_._normalize_slash_confirm_choice = _bound(
        ThothCLI._normalize_slash_confirm_choice, self_
    )
    return self_


def _call(self_):
    """Invoke the real ``_handle_update_command`` on the stub."""
    return ThothCLI._handle_update_command(self_)


# ---------------------------------------------------------------------------
# Managed-install guard
# ---------------------------------------------------------------------------


def test_managed_install_refuses_and_does_not_set_pending_relaunch(capsys):
    """Under a managed install (brew/docker), /update prints a hint and
    returns without setting ``_pending_relaunch``."""
    self_ = SimpleNamespace(
        _app=None,
        _pending_relaunch=None,
        # Use pytest.fail so any unexpected modal invocation surfaces as a failure.
        _prompt_text_input_modal=lambda **_kw: pytest.fail("Modal should not be called"),
    )
    self_._normalize_slash_confirm_choice = _bound(
        ThothCLI._normalize_slash_confirm_choice, self_
    )
    with (
        patch("thoth_cli.config.is_managed", return_value=True),
        patch(
            "thoth_cli.config.format_managed_message",
            return_value="Use `brew upgrade thoth-agent` to update.",
        ),
    ):
        result = _call(self_)

    out = capsys.readouterr().out
    assert "brew upgrade thoth-agent" in out
    assert self_._pending_relaunch is None
    assert not result


# ---------------------------------------------------------------------------
# Confirmation proceeds only on recognised affirmative responses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "1", "ok"])
def test_affirmative_answer_sets_pending_relaunch_and_returns_true(answer, capsys):
    """Recognised affirmative answers ("y", "yes", "1", "ok") set
    ``_pending_relaunch = ["update"]`` and return ``True`` so the caller
    (process_command) can trigger the main-thread app-exit path."""
    self_ = _make_self(modal_response=answer)
    with patch("thoth_cli.config.is_managed", return_value=False):
        result = _call(self_)

    assert self_._pending_relaunch == ["update"]
    assert result is True
    assert "Launching update" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Cancellation paths — _pending_relaunch must stay None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["n", "N", "no", "NO", " no "])
def test_negative_answer_cancels(answer, capsys):
    """Any "no"-shaped answer cancels without setting ``_pending_relaunch``."""
    self_ = _make_self(modal_response=answer)
    with patch("thoth_cli.config.is_managed", return_value=False):
        result = _call(self_)

    assert self_._pending_relaunch is None
    assert not result
    assert "Launching update" not in capsys.readouterr().out


def test_none_response_cancels(capsys):
    """``None`` from the modal (timeout or dismiss) cancels cleanly."""
    self_ = _make_self(modal_response=None)
    with patch("thoth_cli.config.is_managed", return_value=False):
        result = _call(self_)

    assert self_._pending_relaunch is None
    assert not result


@pytest.mark.parametrize("answer", ["nope", "cancel", "sure", "2", "3", "abort", ""])
def test_unrecognized_or_cancel_input_cancels(answer, capsys):
    """Unrecognised input and explicit "cancel" do not proceed.

    Previously the implementation treated any non-"n/no" answer as approval,
    which meant typos like "nope" or "cancel" would launch the update.
    Now only confirmed affirmative aliases ("y", "yes", "1", "ok") proceed;
    everything else (including empty string, "cancel", typos) cancels.
    """
    self_ = _make_self(modal_response=answer)
    with patch("thoth_cli.config.is_managed", return_value=False):
        result = _call(self_)

    assert self_._pending_relaunch is None
    assert not result


# ===========================================================================
# Substrate worker discovery + restart on update
# ===========================================================================
#
# ``thoth update`` must restart thoth-substrate* units (notably
# thoth-substrate-worker.service) so substrate sub-agents pick up new
# code. These tests MOCK every ``systemctl`` subprocess call — they never shell
# out to a real service manager.


def _systemctl_runner(active_units):
    """Build a fake ``subprocess.run`` for systemctl that, for the *user*
    scope, lists the given units and reports them active, and for the system
    scope lists nothing.  ``active_units`` is a set of unit base names
    (without ``.service``) considered active.

    Records every invoked argv on ``.calls`` for assertions.
    """
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(list(cmd))
        is_user = "--user" in cmd

        def out(s):
            return SimpleNamespace(returncode=0, stdout=s, stderr="")

        if "list-units" in cmd:
            if is_user:
                lines = "\n".join(
                    f"{u}.service loaded active running {u}" for u in active_units
                )
                return out(lines)
            return out("")  # system scope: nothing
        if "is-active" in cmd:
            svc = cmd[-1]
            return out("active" if svc in active_units else "inactive")
        if "reset-failed" in cmd:
            return out("")
        if "restart" in cmd:
            return out("")
        return out("")

    _run.calls = calls
    return _run


class TestRestartSubstrateWorkers:
    """``_restart_substrate_workers`` discovers + restarts thoth-substrate*
    units."""

    def test_discovers_and_restarts_active_worker(self):
        runner = _systemctl_runner({"thoth-substrate-worker"})
        with (
            patch(
                "thoth_cli.gateway.supports_systemd_services", return_value=True
            ),
            patch("thoth_cli.gateway._ensure_user_systemd_env", lambda: None),
            patch.object(hmain.subprocess, "run", runner),
        ):
            restarted = hmain._restart_substrate_workers()

        assert restarted == ["thoth-substrate-worker"]
        # The list-units discovery globs the canonical thoth-substrate* pattern.
        assert any(
            "list-units" in c
            and "thoth-substrate*" in c
            for c in runner.calls
        )
        # An actual restart of the worker was issued.
        assert any(
            "restart" in c and "thoth-substrate-worker" in c for c in runner.calls
        )

    def test_handles_both_user_and_system_scope(self):
        runner = _systemctl_runner({"thoth-substrate-worker"})
        with (
            patch(
                "thoth_cli.gateway.supports_systemd_services", return_value=True
            ),
            patch("thoth_cli.gateway._ensure_user_systemd_env", lambda: None),
            patch.object(hmain.subprocess, "run", runner),
        ):
            hmain._restart_substrate_workers()

        list_unit_scopes = [
            ("--user" in c) for c in runner.calls if "list-units" in c
        ]
        # Both a --user and a system (no --user) list-units call were made.
        assert True in list_unit_scopes
        assert False in list_unit_scopes

    def test_noop_when_systemd_unsupported(self):
        called = MagicMock()
        with (
            patch(
                "thoth_cli.gateway.supports_systemd_services", return_value=False
            ),
            patch.object(hmain.subprocess, "run", called),
        ):
            restarted = hmain._restart_substrate_workers()
        assert restarted == []
        called.assert_not_called()

    def test_inactive_worker_not_restarted(self):
        calls = []

        def _run(cmd, *a, **kw):
            calls.append(list(cmd))

            def out(s):
                return SimpleNamespace(returncode=0, stdout=s, stderr="")

            if "list-units" in cmd and "--user" in cmd:
                return out("thoth-substrate-worker.service loaded active running x")
            if "list-units" in cmd:
                return out("")
            if "is-active" in cmd:
                return out("inactive")
            return out("")

        with (
            patch(
                "thoth_cli.gateway.supports_systemd_services", return_value=True
            ),
            patch("thoth_cli.gateway._ensure_user_systemd_env", lambda: None),
            patch.object(hmain.subprocess, "run", _run),
        ):
            restarted = hmain._restart_substrate_workers()

        assert restarted == []
        assert not any("restart" in c for c in calls)

    def test_restart_failure_is_best_effort(self, capsys):
        def _run(cmd, *a, **kw):
            def out(s, rc=0, err=""):
                return SimpleNamespace(returncode=rc, stdout=s, stderr=err)

            if "list-units" in cmd and "--user" in cmd:
                return out("thoth-substrate-worker.service loaded active running x")
            if "list-units" in cmd:
                return out("")
            if "is-active" in cmd:
                return out("active")
            if "restart" in cmd:
                return out("", rc=1, err="Job failed")
            return out("")

        with (
            patch(
                "thoth_cli.gateway.supports_systemd_services", return_value=True
            ),
            patch("thoth_cli.gateway._ensure_user_systemd_env", lambda: None),
            patch.object(hmain.subprocess, "run", _run),
        ):
            restarted = hmain._restart_substrate_workers()

        assert restarted == []  # failed restart not counted as restarted
        out = capsys.readouterr().out
        assert "Failed to restart thoth-substrate-worker" in out


# ===========================================================================
# alembic upgrade head on update
# ===========================================================================
#
# These tests MOCK the alembic subprocess and the read-only consistency
# check — they never run alembic against a real database.


class TestRunDbMigrationOnUpdate:
    def test_noop_without_pg_dsn(self, monkeypatch):
        monkeypatch.delenv("THOTH_PG_DSN", raising=False)
        run = MagicMock()
        with patch.object(hmain.subprocess, "run", run):
            hmain._run_db_migration_on_update()
        run.assert_not_called()

    def test_runs_alembic_upgrade_head(self, monkeypatch, capsys):
        monkeypatch.setenv("THOTH_PG_DSN", "postgresql://x/y")
        run = MagicMock(
            return_value=SimpleNamespace(
                returncode=0, stdout="Running upgrade 0022 -> 0023", stderr=""
            )
        )
        with (
            patch.object(
                hmain,
                "_check_alembic_consistency",
                return_value={"status": "ok", "recorded": "0023", "missing_table": None},
            ),
            patch.object(hmain, "_venv_python", return_value="/venv/bin/python"),
            patch.object(hmain.subprocess, "run", run),
        ):
            hmain._run_db_migration_on_update()

        run.assert_called_once()
        argv = run.call_args[0][0]
        # Invokes the venv python via ``-m alembic ... upgrade head``.
        assert argv[0] == "/venv/bin/python"
        assert argv[1:3] == ["-m", "alembic"]
        assert "upgrade" in argv and "head" in argv
        assert "-c" in argv and "migrations/alembic.ini" in argv
        assert "Database schema migrated" in capsys.readouterr().out

    def test_drift_detected_prints_recovery_and_skips_upgrade(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("THOTH_PG_DSN", "postgresql://x/y")
        run = MagicMock()
        with (
            patch.object(
                hmain,
                "_check_alembic_consistency",
                return_value={
                    "status": "drift",
                    "recorded": "20260528_0022",
                    "missing_table": "substrate_skill_proposals",
                },
            ),
            patch.object(hmain, "_venv_python", return_value="/venv/bin/python"),
            patch.object(hmain.subprocess, "run", run),
        ):
            hmain._run_db_migration_on_update()

        # Upgrade must NOT be attempted on drift.
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "schema drift detected" in out
        assert "20260528_0022" in out
        assert "substrate_skill_proposals" in out
        # Prints the recovery commands (downgrade then upgrade).
        assert "downgrade -1" in out
        assert "upgrade head" in out

    def test_migration_failure_is_actionable_not_fatal(self, monkeypatch, capsys):
        monkeypatch.setenv("THOTH_PG_DSN", "postgresql://x/y")
        run = MagicMock(
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="psycopg.OperationalError: boom"
            )
        )
        with (
            patch.object(
                hmain,
                "_check_alembic_consistency",
                return_value={"status": "ok", "recorded": "0023", "missing_table": None},
            ),
            patch.object(hmain, "_venv_python", return_value="/venv/bin/python"),
            patch.object(hmain.subprocess, "run", run),
        ):
            # Must not raise — the rest of the update continues.
            hmain._run_db_migration_on_update()

        out = capsys.readouterr().out
        assert "Database migration failed" in out
        assert "boom" in out


class TestCheckAlembicConsistency:
    """The read-only drift check builds the right verdict from DB state.

    asyncpg.connect is mocked so no real PG is touched.
    """

    def _patch_conn(self, fetchval_map):
        """Patch asyncpg.connect to a fake connection whose fetchval returns
        values keyed by a substring of the query (or its arg)."""
        conn = MagicMock()

        async def _fetchval(query, *args):
            for key, val in fetchval_map.items():
                if key in query or (args and key in str(args[0])):
                    return val
            return None

        async def _close():
            return None

        conn.fetchval = _fetchval
        conn.close = _close

        async def _connect(*a, **kw):
            return conn

        return patch("asyncpg.connect", _connect)

    def test_fresh_db_when_no_alembic_version_table(self):
        with self._patch_conn({"alembic_version": None}):
            res = hmain._check_alembic_consistency("postgresql://x/y")
        assert res["status"] == "fresh"

    def test_ok_when_required_table_present(self):
        with self._patch_conn(
            {
                "to_regclass('public.alembic_version')": "alembic_version",
                "version_num": "20260528_0022",
                "substrate_skill_proposals": "substrate_skill_proposals",
            }
        ):
            res = hmain._check_alembic_consistency("postgresql://x/y")
        assert res["status"] == "ok"
        assert res["recorded"] == "20260528_0022"

    def test_drift_when_recorded_version_table_missing(self):
        # alembic_version table exists, records 0022, but the table that
        # revision should have created is absent → drift.
        conn = MagicMock()

        async def _fetchval(query, *args):
            if "to_regclass('public.alembic_version')" in query:
                return "alembic_version"
            if "version_num" in query:
                return "20260528_0022"
            if args and "substrate_skill_proposals" in str(args[0]):
                return None  # table missing
            return None

        async def _close():
            return None

        conn.fetchval = _fetchval
        conn.close = _close

        async def _connect(*a, **kw):
            return conn

        with patch("asyncpg.connect", _connect):
            res = hmain._check_alembic_consistency("postgresql://x/y")
        assert res["status"] == "drift"
        assert res["recorded"] == "20260528_0022"
        assert res["missing_table"] == "substrate_skill_proposals"

    def test_unknown_on_connection_error(self):
        async def _connect(*a, **kw):
            raise OSError("connection refused")

        with patch("asyncpg.connect", _connect):
            res = hmain._check_alembic_consistency("postgresql://x/y")
        assert res["status"] == "unknown"
