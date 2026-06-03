"""PostgreSQL port of kanban_db tests (Phase 0 Task 20).

Tests use `hermes_db_initialized_sync` from tests/conftest.py which:
  - Creates a fresh per-test PG database via pytest-postgresql
  - Runs Alembic upgrade head (including the kanban schema migration)
  - Initialises the thoth_db pool
  - Yields the DSN

The kanban_db module detects HERMES_PG_DSN and uses PG automatically.
"""

from __future__ import annotations

import os
import time

import pytest

import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _set_pg_dsn(hermes_db_initialized_sync, monkeypatch):
    """Make kanban_db use PG for the duration of each test."""
    monkeypatch.setenv("THOTH_PG_DSN", hermes_db_initialized_sync)
    monkeypatch.setenv("HERMES_PG_DSN", hermes_db_initialized_sync)


@pytest.fixture
def kb(monkeypatch):
    """Import kanban_db after PG env is set."""
    from thoth_cli import kanban_db
    return kanban_db


@pytest.fixture
def default_board(kb):
    """Ensure the default board exists (idempotent)."""
    kb.init_db(board="default")
    return "default"


@pytest.fixture
def named_board(kb):
    """Create a named board for multi-board isolation tests."""
    kb.create_board("test-alpha")
    return "test-alpha"


# ---------------------------------------------------------------------------
# Board management
# ---------------------------------------------------------------------------

def test_init_db_creates_default_board(kb, hermes_db_initialized_sync):
    """init_db in PG mode inserts the board slug into kanban_boards."""
    import thoth_db
    kb.init_db(board="default")
    async def _check():
        async with thoth_db.connection() as conn:
            row = await conn.fetchrow("SELECT slug FROM kanban_boards WHERE slug = 'default'")
            return row
    row = thoth_db.run_sync(_check())
    assert row is not None
    assert row["slug"] == "default"


def test_create_board_is_idempotent(kb):
    """create_board can be called multiple times without error."""
    kb.create_board("idempotent-board")
    kb.create_board("idempotent-board")  # second call must not raise
    assert kb.board_exists("idempotent-board")


def test_board_exists_default_always_true(kb):
    """The 'default' board always exists in PG mode."""
    assert kb.board_exists("default") is True


def test_board_exists_named(kb, named_board):
    assert kb.board_exists(named_board) is True


def test_board_exists_missing(kb):
    assert kb.board_exists("nonexistent-board-xyz") is False


# ---------------------------------------------------------------------------
# Task CRUD - default board
# ---------------------------------------------------------------------------

def test_create_and_get_task_default_board(kb, default_board):
    with kb.connect(board=default_board) as conn:
        task_id = kb.create_task(conn, title="Hello PG", board=default_board)
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.id == task_id
    assert task.title == "Hello PG"
    assert task.status in ("ready", "running")  # no parents → ready
    assert task.board_slug == default_board if hasattr(task, "board_slug") else True


def test_create_task_with_body_and_assignee(kb, default_board):
    with kb.connect(board=default_board) as conn:
        task_id = kb.create_task(
            conn,
            title="Detailed task",
            body="Some body text",
            assignee="alice",
            priority=5,
            board=default_board,
        )
        task = kb.get_task(conn, task_id)

    assert task.title == "Detailed task"
    assert task.body == "Some body text"
    assert task.assignee == "alice"
    assert task.priority == 5


def test_get_task_returns_none_for_missing(kb, default_board):
    with kb.connect(board=default_board) as conn:
        result = kb.get_task(conn, "t_doesnotexist")
    assert result is None


# ---------------------------------------------------------------------------
# list_tasks with status filter
# ---------------------------------------------------------------------------

def test_list_tasks_all(kb, default_board):
    with kb.connect(board=default_board) as conn:
        kb.create_task(conn, title="Task A", board=default_board)
        kb.create_task(conn, title="Task B", board=default_board)
        tasks = kb.list_tasks(conn)

    assert len(tasks) >= 2
    titles = {t.title for t in tasks}
    assert "Task A" in titles
    assert "Task B" in titles


def test_list_tasks_filter_by_status(kb, default_board):
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Ready task", board=default_board)
        # Mark one task done so we can filter
        kb.complete_task(conn, tid)
        tasks_ready = kb.list_tasks(conn, status="ready")
        tasks_done = kb.list_tasks(conn, status="done", include_archived=True)

    assert all(t.status == "ready" for t in tasks_ready)
    assert any(t.status == "done" for t in tasks_done)


def test_list_tasks_excludes_archived_by_default(kb, default_board):
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Archived", board=default_board)
        kb.complete_task(conn, tid)
        # Archive by completing and then archiving
        kb.archive_task(conn, tid)
        tasks = kb.list_tasks(conn)

    ids = {t.id for t in tasks}
    assert tid not in ids


# ---------------------------------------------------------------------------
# Multi-board isolation
# ---------------------------------------------------------------------------

def test_tasks_isolated_between_boards(kb, default_board, named_board):
    """Tasks created on one board are not visible on another board."""
    with kb.connect(board=default_board) as conn:
        tid_default = kb.create_task(conn, title="Default task", board=default_board)

    with kb.connect(board=named_board) as conn:
        tid_alpha = kb.create_task(conn, title="Alpha task", board=named_board)

    # Verify isolation
    with kb.connect(board=default_board) as conn:
        default_tasks = kb.list_tasks(conn)
        default_ids = {t.id for t in default_tasks}

    with kb.connect(board=named_board) as conn:
        alpha_tasks = kb.list_tasks(conn)
        alpha_ids = {t.id for t in alpha_tasks}

    assert tid_default in default_ids
    assert tid_alpha not in default_ids

    assert tid_alpha in alpha_ids
    assert tid_default not in alpha_ids


def test_get_task_across_boards(kb, default_board, named_board):
    """get_task on wrong board returns None."""
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Default only", board=default_board)

    with kb.connect(board=named_board) as conn:
        result = kb.get_task(conn, tid)

    assert result is None


# ---------------------------------------------------------------------------
# CAS-based claim_lock
# ---------------------------------------------------------------------------

def test_claim_task_cas(kb, default_board):
    """claim_task uses CAS: second claim on same task returns None."""
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Claimable", board=default_board)
        # Promote to ready (no parents, should already be ready)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"

        # First claim succeeds
        claimed = kb.claim_task(conn, tid, claimer="worker-1")
        assert claimed is not None
        assert claimed.claim_lock is not None

        # Second claim on same task fails (CAS)
        second = kb.claim_task(conn, tid, claimer="worker-2")
        assert second is None


def test_claim_and_complete_task(kb, default_board):
    """Full claim → complete cycle."""
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Claimable", board=default_board)
        claimed = kb.claim_task(conn, tid, claimer="worker-1")
        assert claimed is not None

        ok = kb.complete_task(conn, tid, result="done!", summary="It worked")
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.result == "done!"


# ---------------------------------------------------------------------------
# task_events insertion
# ---------------------------------------------------------------------------

def test_task_events_created_on_create(kb, default_board):
    """Creating a task emits a 'created' event."""
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Evented task", board=default_board)
        events = kb.list_events(conn, tid)

    assert any(e.kind == "created" for e in events)


def test_task_events_claim_and_complete(kb, default_board):
    """Claiming and completing emit 'claimed' and 'completed' events."""
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Event chain", board=default_board)
        kb.claim_task(conn, tid, claimer="worker-x")
        kb.complete_task(conn, tid, result="ok")
        events = kb.list_events(conn, tid)

    kinds = {e.kind for e in events}
    assert "claimed" in kinds
    assert "completed" in kinds


def test_add_comment_and_list_comments(kb, default_board):
    """add_comment inserts a row; list_comments returns it."""
    with kb.connect(board=default_board) as conn:
        tid = kb.create_task(conn, title="Commented", board=default_board)
        comment_id = kb.add_comment(conn, tid, author="alice", body="Hello!")
        comments = kb.list_comments(conn, tid)

    assert comment_id > 0
    assert len(comments) == 1
    assert comments[0].author == "alice"
    assert comments[0].body == "Hello!"


# ---------------------------------------------------------------------------
# Cascade delete when board deleted
# ---------------------------------------------------------------------------

def test_cascade_delete_on_board_deletion(kb, hermes_db_initialized_sync):
    """Deleting a board cascades to kanban_tasks, task_events, etc."""
    import thoth_db

    kb.create_board("ephemeral-board")
    with kb.connect(board="ephemeral-board") as conn:
        tid = kb.create_task(conn, title="Will be deleted", board="ephemeral-board")
        kb.add_comment(conn, tid, author="bot", body="Comment")

    # Delete board via direct SQL (remove_board is filesystem-based in SQLite mode)
    async def _delete():
        async with thoth_db.connection() as conn:
            await conn.execute("DELETE FROM kanban_boards WHERE slug = 'ephemeral-board'")
    thoth_db.run_sync(_delete())

    # Tasks and comments should be gone (CASCADE)
    async def _verify():
        async with thoth_db.connection() as conn:
            task_count = await conn.fetchval(
                "SELECT COUNT(*) FROM kanban_tasks WHERE board_slug = 'ephemeral-board'"
            )
            comment_count = await conn.fetchval(
                "SELECT COUNT(*) FROM kanban_task_comments WHERE board_slug = 'ephemeral-board'"
            )
            return task_count, comment_count
    task_count, comment_count = thoth_db.run_sync(_verify())
    assert task_count == 0
    assert comment_count == 0


# ---------------------------------------------------------------------------
# Task links (parent/child dependencies)
# ---------------------------------------------------------------------------

def test_task_links_and_dependency_resolution(kb, default_board):
    """Child task starts as 'todo' when parent is not done; promotes to 'ready' after."""
    with kb.connect(board=default_board) as conn:
        parent_id = kb.create_task(conn, title="Parent", board=default_board)
        child_id = kb.create_task(
            conn,
            title="Child",
            parents=[parent_id],
            board=default_board,
        )

        child = kb.get_task(conn, child_id)
        assert child.status == "todo"  # blocked by parent

        # Complete parent → child should promote to ready
        kb.claim_task(conn, parent_id, claimer="w")
        kb.complete_task(conn, parent_id)

        child_after = kb.get_task(conn, child_id)
        assert child_after.status == "ready"


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def test_idempotency_key_deduplicates(kb, default_board):
    """create_task with same idempotency_key returns existing id."""
    with kb.connect(board=default_board) as conn:
        id1 = kb.create_task(
            conn,
            title="First",
            idempotency_key="idem-001",
            board=default_board,
        )
        id2 = kb.create_task(
            conn,
            title="Duplicate",
            idempotency_key="idem-001",
            board=default_board,
        )

    assert id1 == id2
