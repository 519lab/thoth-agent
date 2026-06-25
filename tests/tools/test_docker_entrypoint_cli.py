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
