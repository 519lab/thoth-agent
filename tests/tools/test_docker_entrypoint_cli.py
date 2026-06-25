"""Contract tests for the container entrypoint's CLI invocation (#213).

The de-hermes rename dropped the ``hermes`` console script (pyproject ships
only ``thoth``/``thoth-agent``/``thoth-acp``). The entrypoint must therefore
``exec thoth`` — not ``hermes`` — or every container deploy exits 127
("hermes: command not found") and crash-loops. And the app venv's bin must be
on PATH or even ``thoth`` won't resolve.

These assert the *CLI invocations* only; the container's ``hermes`` OS user and
the ``/opt/hermes`` install path are intentionally left alone (deferred).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"


@pytest.fixture(scope="module")
def entrypoint_text() -> str:
    if not ENTRYPOINT.exists():
        pytest.skip("docker/entrypoint.sh not present in this checkout")
    return ENTRYPOINT.read_text()


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    if not DOCKERFILE.exists():
        pytest.skip("Dockerfile not present in this checkout")
    return DOCKERFILE.read_text()


def test_entrypoint_execs_thoth_not_hermes(entrypoint_text):
    assert 'exec thoth "$@"' in entrypoint_text, (
        "entrypoint must `exec thoth \"$@\"` — the `hermes` console script "
        "no longer exists, so `exec hermes` exits 127 and crash-loops (#213)."
    )
    # No `hermes` CLI invocations (the OS-user `hermes` / `/opt/hermes` path
    # are fine — they don't run the deleted console script).
    assert "exec hermes" not in entrypoint_text
    assert "hermes dashboard" not in entrypoint_text
    assert "hermes setup" not in entrypoint_text
    assert "thoth dashboard" in entrypoint_text


def test_dockerfile_puts_app_venv_on_path(dockerfile_text):
    """`thoth` lives in /opt/hermes/.venv/bin; that must be on PATH or the
    entrypoint's bare `exec thoth` can't resolve it (#213)."""
    path_lines = [
        ln.strip()
        for ln in dockerfile_text.splitlines()
        if ln.strip().startswith("ENV PATH") or ln.strip().startswith('ENV PATH=')
    ]
    assert path_lines, "Dockerfile must set PATH"
    assert any("/opt/hermes/.venv/bin" in ln for ln in path_lines), (
        "the app venv bin (/opt/hermes/.venv/bin) must be on PATH so the "
        "entrypoint's `exec thoth` resolves the console script (#213)."
    )


@pytest.fixture(scope="module")
def compose_text() -> str:
    p = REPO_ROOT / "docker-compose.yml"
    if not p.exists():
        pytest.skip("docker-compose.yml not present")
    return p.read_text()


def test_entrypoint_neutralizes_seeded_pg_dsn(entrypoint_text):
    # #216: in a container the DB is reached by compose service name; the seeded
    # localhost DSN must be neutralized so it can't override the compose DSN
    # (env_loader loads .env with override=True).
    assert "s/^THOTH_PG_DSN=/#THOTH_PG_DSN=/" in entrypoint_text, (
        "entrypoint must comment out the seeded localhost THOTH_PG_DSN so the "
        "compose-provided @postgres:5432 DSN wins (#216)."
    )


def test_compose_reaches_postgres_by_service_name(compose_text):
    # #216: no host networking (silently ignored on Docker Desktop), and the
    # gateway/dashboard reach Postgres by its service name, not localhost.
    active_host_net = [
        ln for ln in compose_text.splitlines()
        if ln.split("#", 1)[0].strip().startswith("network_mode:")
        and "host" in ln.split("#", 1)[0]
    ]
    assert not active_host_net, (
        "no active `network_mode: host` directive — it's ignored on Docker "
        "Desktop and breaks gateway<->DB (#216). (Comments are fine.)"
    )
    assert "@postgres:5432" in compose_text, (
        "gateway/dashboard THOTH_PG_DSN must target the `postgres` service host (#216)."
    )
    assert "127.0.0.1:9119:9119" in compose_text
