"""Static structural regressions for scripts/install.ps1 (the Windows
installer), covering the install-audit fixes #217, #220, #225, #227.

PowerShell can't be executed on the Linux CI host, so — exactly like
``tests/test_install_sh_update_safety.py`` does for the bash installer — these
tests pin the *shape* of install.ps1 so regressions surface in seconds rather
than during a user's Windows install.

The four issues:

* #217 — install.ps1 must actually provision PostgreSQL (mirror install.sh:
  pin the compose project, ``docker compose up -d postgres``, wait-for-healthy,
  write the real bound port into .env's THOTH_PG_DSN, ``alembic upgrade head``),
  and when Docker is absent print a clear "memory disabled" block instead of
  baking a dead localhost DSN.
* #220 — the update path must back up local changes + ``git reset --hard
  origin/$Branch`` + ``git clean -fd -e venv -e node_modules`` (no stash / no
  pull-replay), matching install.sh #210.
* #225 — user-facing output must not print ``~/.hermes`` (the real dir is
  %LOCALAPPDATA%\\thoth == $HermesHome).
* #227 — stop pre-creating legacy image_cache/audio_cache dirs; ensure the
  workspace/ dir is created.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def _read() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def _extract_function_body(name: str) -> str:
    """Return the body (between the outermost braces) of a PowerShell
    ``function <name> { ... }`` via brace matching."""
    text = _read()
    match = re.search(rf"function\s+{re.escape(name)}\b", text)
    assert match is not None, f"function {name} not found in scripts/install.ps1"
    brace_start = text.index("{", match.end())
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : i]
    raise AssertionError(f"unbalanced braces in function {name}")


# ---------------------------------------------------------------------------
# #217 — PostgreSQL provisioning parity
# ---------------------------------------------------------------------------


def test_postgres_provisioning_function_exists() -> None:
    text = _read()
    assert "function Install-Postgres" in text, (
        "install.ps1 must define Install-Postgres — without it the Windows "
        "agent provisions no database and the substrate silently fails (#217)."
    )


def test_postgres_stage_registered_before_config_templates() -> None:
    """The provisioning stage must run before config-templates so the
    resolved DSN can be written into .env."""
    text = _read()
    assert 'Worker = "Stage-Postgres"' in text, (
        "a 'postgres' stage must be registered in $InstallStages."
    )
    pg_pos = text.index('Worker = "Stage-Postgres"')
    cfg_pos = text.index('Worker = "Stage-ConfigTemplates"')
    dep_pos = text.index('Worker = "Stage-Dependencies"')
    assert dep_pos < pg_pos < cfg_pos, (
        "the postgres stage must run after dependencies (needs the venv for "
        "alembic) and before config-templates (which writes THOTH_PG_DSN)."
    )


def test_compose_project_pinned_not_derived_from_dir() -> None:
    body = _extract_function_body("Resolve-ComposeProject")
    assert "_hermes_pg_data" in body, (
        "Resolve-ComposeProject must detect an existing *_hermes_pg_data "
        "volume and reuse its project so an upgrade re-attaches real data."
    )
    assert '"thoth"' in body or "'thoth'" in body, (
        "fresh installs must use the stable 'thoth' compose project."
    )
    assert "COMPOSE_PROJECT_NAME" in body, (
        "Resolve-ComposeProject must set COMPOSE_PROJECT_NAME so every "
        "docker compose call agrees on the volume/container names."
    )


def test_postgres_compose_up_wait_healthy_and_migrate() -> None:
    body = _extract_function_body("Install-Postgres")
    assert "up" in body and "postgres" in body, "must `docker compose up postgres`."
    assert "healthy" in body, (
        "Install-Postgres must wait for the container to report healthy "
        "before declaring PG ready / running migrations."
    )
    # Migrations run via the dedicated helper.
    assert "Invoke-AlembicUpgrade" in body, (
        "Install-Postgres must run Alembic migrations after the DB is up."
    )
    alembic = _extract_function_body("Invoke-AlembicUpgrade")
    assert "upgrade head" in alembic, (
        "Invoke-AlembicUpgrade must run `alembic ... upgrade head`."
    )


def test_postgres_absent_prints_disabled_block_not_dead_dsn() -> None:
    """When Docker is missing the installer must say memory is disabled and
    must NOT leave a resolved DSN that would bake localhost:5432 into .env."""
    body = _extract_function_body("Install-Postgres")
    assert re.search(r"(?i)disabled", body), (
        "Install-Postgres must print a clear memory/substrate DISABLED block "
        "when Docker is unavailable."
    )
    assert "$script:ResolvedPgDsn = $null" in body, (
        "On the Docker-absent path Install-Postgres must clear ResolvedPgDsn "
        "so config-templates does not write a dead localhost DSN over .env."
    )


def test_dsn_written_from_resolved_port_and_preserves_custom() -> None:
    """copy/config-templates must rewrite THOTH_PG_DSN to the provisioned
    DSN, but only when the existing value is installer-managed (localhost)."""
    cfg = _extract_function_body("Copy-ConfigTemplates")
    assert "Update-EnvPgDsn" in cfg and "Resolve-RunningPgDsn" in cfg, (
        "Copy-ConfigTemplates must call Update-EnvPgDsn with the resolved DSN."
    )
    upd = _extract_function_body("Update-EnvPgDsn")
    assert "THOTH_PG_DSN=" in upd, "Update-EnvPgDsn must write the THOTH_PG_DSN line."
    assert "localhost" in upd and "127" in upd, (
        "Update-EnvPgDsn must detect localhost-style DSNs so it preserves "
        "user-customized / remote DSNs and only rewrites installer-managed ones."
    )
    assert "Backup-EnvFile" in upd, (
        "Update-EnvPgDsn must back up .env before rewriting it."
    )


# ---------------------------------------------------------------------------
# #220 — update path is conflict-proof (hard reset, no stash/pull-replay)
# ---------------------------------------------------------------------------


def test_update_uses_hard_reset_not_pull_replay() -> None:
    body = _extract_function_body("Install-Repository")
    assert 'reset --hard "origin/$Branch"' in body, (
        "update path must `git reset --hard origin/$Branch` — the code dir is "
        "managed/disposable so a clean reset is conflict-proof (#220)."
    )
    assert "clean -fd -e venv -e node_modules" in body, (
        "update path must `git clean -fd -e venv -e node_modules` to drop "
        "stray files while preserving gitignored build artifacts."
    )
    # The fragile pull-replay must be gone.
    assert "pull origin $Branch" not in body, (
        "update path must not `git pull origin $Branch` (replay) — that wedges "
        "on a drifted/force-pushed checkout."
    )
    assert "git stash" not in body, "stash/replay must not be used."
    # The branch checkout itself must be force (-f) or a locally-modified file
    # that also changed upstream makes `checkout -B` abort (exit 1) and throw
    # BEFORE the reset --hard runs — the very wedge #220 fixes.
    assert "checkout -f -B $Branch" in body, (
        "branch checkout must be `git checkout -f -B $Branch` so a drifted "
        "working tree can't abort the switch before reset --hard (#220)."
    )


def test_update_backs_up_local_changes_first() -> None:
    repo = _extract_function_body("Install-Repository")
    assert "Backup-CodeDirChanges" in repo, (
        "update path must back up local code-dir changes before the reset."
    )
    backup = _extract_function_body("Backup-CodeDirChanges")
    assert ".install-backup" in backup and "diff HEAD" in backup, (
        "Backup-CodeDirChanges must save `git diff HEAD` under "
        "$HermesHome\\.install-backup before the hard reset."
    )


# ---------------------------------------------------------------------------
# #225 — no ~/.hermes in user-facing output
# ---------------------------------------------------------------------------


def test_no_hermes_path_in_user_facing_output() -> None:
    """No Write-Success / Write-Info / Write-Host line may print the wrong
    ~/.hermes path. Comments (#...) are exempt — they're not user-facing."""
    offenders = []
    for lineno, line in enumerate(_read().splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "~/.hermes" not in line:
            continue
        if re.search(r"Write-(Success|Info|Host|Warn|Err)\b", line):
            offenders.append((lineno, line.strip()))
    assert not offenders, (
        "user-facing output must use $HermesHome / ~/.thoth, not ~/.hermes: "
        + "; ".join(f"L{n}: {t}" for n, t in offenders)
    )


# ---------------------------------------------------------------------------
# #227 — no legacy cache dirs; workspace/ created
# ---------------------------------------------------------------------------


def test_no_legacy_cache_dirs_created() -> None:
    body = _extract_function_body("Copy-ConfigTemplates")
    assert "New-Item" in body  # sanity: we're looking at the dir-creation fn
    assert 'Path "$HermesHome\\image_cache"' not in body, (
        "must not pre-create the legacy image_cache dir (#227)."
    )
    assert 'Path "$HermesHome\\audio_cache"' not in body, (
        "must not pre-create the legacy audio_cache dir (#227)."
    )


def test_workspace_dir_created() -> None:
    body = _extract_function_body("Copy-ConfigTemplates")
    assert 'Path "$HermesHome\\workspace"' in body, (
        "Copy-ConfigTemplates must create the agent workspace/ dir (#227)."
    )
