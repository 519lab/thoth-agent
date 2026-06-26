from __future__ import annotations

import threading
from pathlib import Path

import pytest

from thoth_cli import kanban_db as kb


@pytest.mark.skip(
    reason="Phase 0 (sqlite→PG): _INITIALIZED_PATHS was a per-file schema-init "
    "cache for sqlite kanban.db files. In PG mode the schema lives in "
    "Alembic-managed migrations on a single database — no per-board init "
    "concurrency to test. The assertion ``conn.execute('PRAGMA table_info(tasks)')`` "
    "is also sqlite-specific; the PG _PgConnection raises on unknown PRAGMA."
)
def test_connect_initialization_is_thread_safe(tmp_path, monkeypatch):
    home = tmp_path / ".thoth"
    home.mkdir()
    monkeypatch.setenv("THOTH_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            conn = kb.connect(board="default")
            conn.close()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with kb.connect(board="default") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_retries" in cols
