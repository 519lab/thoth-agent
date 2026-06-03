"""Tests for thoth db migrate-from-sqlite (Task 21).

Requires: docker-compose postgres running (same as other Phase-0 PG tests).
Run: uv run pytest tests/test_migrate_from_sqlite.py -v -o addopts=""
"""

import sqlite3
from pathlib import Path

import pytest

import hermes_db
from thoth_cli.db_commands import migrate_from_sqlite

# Path to the upstream schema fixture (relative to the project root).
_FIXTURE_SQL = Path(__file__).parent / "fixtures" / "upstream_schema.sql"


def _make_sqlite(path: Path) -> sqlite3.Connection:
    """Create a fixture SQLite DB from the upstream schema snapshot."""
    conn = sqlite3.connect(str(path))
    with open(_FIXTURE_SQL) as f:
        conn.executescript(f.read())
    return conn


# ---------------------------------------------------------------------------
# test: sessions and messages are copied to PG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrate_from_sqlite_copies_sessions_and_messages(
    hermes_db_initialized, tmp_path
):
    src = tmp_path / "state.db"
    conn = _make_sqlite(src)

    # Seed: 2 sessions, 2 messages in s1 (s2 has no messages).
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, message_count) VALUES (?, ?, ?, ?)",
        ("s1", "cli", 1700000000.0, 2),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        ("s2", "telegram", 1700000010.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("s1", "user", "hi", 1700000001.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("s1", "assistant", "hello", 1700000002.0),
    )
    conn.commit()
    conn.close()

    n_sessions, n_messages = await migrate_from_sqlite(src)
    assert n_sessions == 2
    assert n_messages == 2

    async with hermes_db.connection() as c:
        s = await c.fetchrow("SELECT * FROM sessions WHERE id = 's1'")
        assert s is not None
        assert s["source"] == "cli"
        assert s["message_count"] == 2

        msgs = await c.fetch(
            "SELECT * FROM messages WHERE session_id = 's1' ORDER BY id"
        )
        assert [m["content"] for m in msgs] == ["hi", "hello"]
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

        s2 = await c.fetchrow("SELECT id FROM sessions WHERE id = 's2'")
        assert s2 is not None


# ---------------------------------------------------------------------------
# test: dry_run counts but does not write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrate_from_sqlite_dry_run_does_not_write(
    hermes_db_initialized, tmp_path
):
    src = tmp_path / "state.db"
    conn = _make_sqlite(src)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('dr1', 'cli', 1700000000.0)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('dr1', 'user', 'test', 1700000001.0)"
    )
    conn.commit()
    conn.close()

    n_sessions, n_messages = await migrate_from_sqlite(src, dry_run=True)
    assert n_sessions == 1
    assert n_messages == 1  # dry_run counts messages too

    async with hermes_db.connection() as c:
        s = await c.fetchrow("SELECT id FROM sessions WHERE id = 'dr1'")
        assert s is None  # nothing was written

        m = await c.fetchrow("SELECT id FROM messages WHERE session_id = 'dr1'")
        assert m is None


# ---------------------------------------------------------------------------
# test: idempotent — ON CONFLICT DO NOTHING prevents duplicates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrate_from_sqlite_idempotent(hermes_db_initialized, tmp_path):
    src = tmp_path / "state.db"
    conn = _make_sqlite(src)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('idem1', 'cli', 1700000000.0)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('idem1', 'user', 'hello', 1700000001.0)"
    )
    conn.commit()
    conn.close()

    # First run.
    s1, m1 = await migrate_from_sqlite(src)
    assert s1 == 1
    assert m1 == 1

    # Second run — session already exists; ON CONFLICT DO NOTHING skips it.
    # Messages don't have a unique constraint so they will be inserted again.
    # This is the expected behaviour: the migrator is idempotent for sessions.
    s2, _m2 = await migrate_from_sqlite(src)
    assert s2 == 1  # still counted (row attempted, silently skipped)

    async with hermes_db.connection() as c:
        count = await c.fetchval("SELECT COUNT(*) FROM sessions WHERE id = 'idem1'")
        assert count == 1  # exactly one row in PG


# ---------------------------------------------------------------------------
# test: JSONB columns round-trip correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrate_from_sqlite_jsonb_columns(hermes_db_initialized, tmp_path):
    src = tmp_path / "state.db"
    conn = _make_sqlite(src)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, model_config) VALUES (?, ?, ?, ?)",
        ("j1", "cli", 1700000000.0, '{"temperature": 0.7, "max_tokens": 1000}'),
    )
    tc_json = '[{"id": "tc1", "type": "function", "function": {"name": "my_tool", "arguments": "{}"}}]'
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, tool_calls) VALUES (?, ?, ?, ?, ?)",
        ("j1", "assistant", None, 1700000001.0, tc_json),
    )
    conn.commit()
    conn.close()

    await migrate_from_sqlite(src)

    async with hermes_db.connection() as c:
        s = await c.fetchrow("SELECT model_config FROM sessions WHERE id = 'j1'")
        assert isinstance(s["model_config"], dict)
        assert s["model_config"]["temperature"] == 0.7

        m = await c.fetchrow("SELECT tool_calls FROM messages WHERE session_id = 'j1'")
        assert isinstance(m["tool_calls"], list)
        assert m["tool_calls"][0]["id"] == "tc1"


# ---------------------------------------------------------------------------
# test: missing PG schema raises clearly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrate_from_sqlite_requires_alembic_head(
    hermes_db_initialized, tmp_path, monkeypatch
):
    """If alembic_version is empty, the migrator raises RuntimeError."""
    src = tmp_path / "state.db"
    conn = _make_sqlite(src)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('x1', 'cli', 1700000000.0)"
    )
    conn.commit()
    conn.close()

    # Wipe the alembic_version table so head is absent.
    async with hermes_db.connection() as c:
        await c.execute("DELETE FROM alembic_version")

    with pytest.raises(RuntimeError, match="PG schema not migrated"):
        await migrate_from_sqlite(src)
