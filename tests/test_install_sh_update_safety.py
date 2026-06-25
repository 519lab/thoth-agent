"""Regression for install.sh update-safety: re-running the installer against
an existing $THOTH_HOME must not clobber user-customized config.

Concretely, the installer can be re-run for two distinct purposes:

  1. **Fresh install** — first time on this machine. No $THOTH_HOME state
     exists; the installer creates ``.env``, ``config.yaml``, ``SOUL.md``,
     writes ``THOTH_PG_DSN`` based on the docker-compose port it picked,
     and runs the interactive setup wizard so the user can configure a
     provider/model/API key.

  2. **Update** — second-or-later run, typically to pull new code into the
     existing checkout (handled by ``clone_repo`` via ``git pull``). The
     user already configured everything; rewriting their ``THOTH_PG_DSN``
     (which may point at a remote PG cluster they bring) or re-running the
     wizard from scratch is hostile.

These tests pin the static invariants that protect update mode by
inspecting ``scripts/install.sh``. They don't run the installer end-to-end
(that needs Docker, root, a real PG, etc.) — they verify the script's
shape so regressions surface in seconds rather than during user upgrades.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _read_install_sh() -> str:
    return INSTALL_SH.read_text()


def _extract_function_body(name: str) -> str:
    text = _read_install_sh()
    match = re.search(
        rf"^{re.escape(name)}\(\)\s*\{{\s*\n(?P<body>.*?)^\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"{name}() not found in scripts/install.sh"
    return match["body"]


def test_detect_install_mode_function_exists() -> None:
    """The installer must call ``detect_install_mode`` early in ``main``
    so downstream functions can branch on ``IS_UPDATE``."""
    text = _read_install_sh()
    assert "detect_install_mode()" in text, (
        "detect_install_mode helper missing — without it the installer "
        "can't distinguish fresh installs from updates and will rewrite "
        "user config every time."
    )
    body = _extract_function_body("main")
    # detect_install_mode must run after resolve_install_layout (which
    # sets $INSTALL_DIR) and before any function that touches user state.
    assert "resolve_install_layout" in body
    assert "detect_install_mode" in body
    resolve_pos = body.index("resolve_install_layout")
    detect_pos = body.index("detect_install_mode")
    assert detect_pos > resolve_pos, (
        "detect_install_mode must run after resolve_install_layout so "
        "$INSTALL_DIR is set before we look for $INSTALL_DIR/.git."
    )


def test_update_uses_hard_reset_not_stash_replay() -> None:
    """The update path must hard-reset the (disposable) code dir to upstream
    rather than stash/replay. Stash-apply on a diverged checkout produces
    catastrophic modify/delete conflicts that leave the tree broken (#209)."""
    body = _extract_function_body("clone_repo")
    assert 'git reset --hard "origin/$BRANCH"' in body, (
        "update path must `git reset --hard origin/$BRANCH` — the code dir is "
        "managed/disposable, so a clean reset is conflict-proof."
    )
    # The fragile stash/replay strategy must be gone.
    assert "git stash apply" not in body, (
        "git stash apply must not be used — it caused #209 (modify/delete "
        "conflict storm on a diverged checkout)."
    )
    # Local changes must be backed up before the reset, not silently dropped.
    assert ".install-backup" in body and "git diff HEAD" in body, (
        "update path must back up local code-dir changes to "
        "THOTH_HOME/.install-backup before resetting."
    )


def test_compose_project_name_is_pinned_not_derived_from_code_dir() -> None:
    """The DB's docker-compose project (→ volume + container names) must be
    pinned and decoupled from the code-dir basename. Otherwise renaming the
    code dir (hermes-agent → app) flips the compose project and orphans the
    real database behind a new empty volume."""
    text = _read_install_sh()
    assert "resolve_compose_project()" in text, (
        "resolve_compose_project() missing — without a pinned "
        "COMPOSE_PROJECT_NAME the DB volume name follows the install dir and "
        "renaming the code dir orphans the database."
    )
    body = _extract_function_body("resolve_compose_project")
    # Must export the project name so every `docker compose` call is consistent.
    assert "export COMPOSE_PROJECT_NAME" in body
    # Fresh installs get the stable Thoth name.
    assert 'COMPOSE_PROJECT_NAME="thoth"' in body, (
        "fresh installs must use the stable 'thoth' compose project."
    )
    # Upgraders reuse the project of an existing *_hermes_pg_data volume so
    # their real data re-attaches.
    assert "_hermes_pg_data" in body and "existing_vol" in body, (
        "must detect an existing *_hermes_pg_data volume and reuse its project."
    )
    # setup_postgres must pin the project BEFORE choosing the port / touching
    # any container, so detection + compose-up + reset all agree.
    pg_body = _extract_function_body("setup_postgres")
    assert "resolve_compose_project" in pg_body
    assert pg_body.index("resolve_compose_project") < pg_body.index("choose_pg_port"), (
        "resolve_compose_project must run before choose_pg_port."
    )


def test_run_setup_wizard_skips_when_provider_already_configured() -> None:
    """Update mode: don't re-prompt the wizard if .env already has an
    API key for one of the supported providers."""
    body = _extract_function_body("run_setup_wizard")
    assert "IS_UPDATE" in body, (
        "run_setup_wizard must consult $IS_UPDATE so it can skip on "
        "re-installs when the user already finished setup."
    )
    assert "_env_has_provider_api_key" in body, (
        "run_setup_wizard must call _env_has_provider_api_key (or "
        "equivalent) so updates with a configured provider skip the "
        "wizard. Otherwise every update re-prompts the user."
    )


def test_pg_dsn_rewrite_preserves_user_customized_dsns() -> None:
    """copy_config_templates must not rewrite THOTH_PG_DSN when the
    existing value points at a non-local cluster (the user almost
    certainly customized it to point at remote PG / Neon / Supabase /
    a host-network PG with custom creds)."""
    body = _extract_function_body("copy_config_templates")
    # Local-host detection: required so we can tell installer-managed
    # DSNs (safe to rewrite on port drift) from user-customized ones.
    assert "@localhost:" in body or "@127.0.0.1:" in body, (
        "copy_config_templates must detect localhost-style DSNs as a "
        "prerequisite to deciding whether THOTH_PG_DSN is installer-"
        "managed (safe to rewrite) or user-customized (preserve)."
    )
    # Escape hatch: --force-rewrite-config must be honoured so power
    # users can still flip back to installer-default DSN.
    assert "FORCE_REWRITE_CONFIG" in body, (
        "copy_config_templates must honour --force-rewrite-config "
        "(FORCE_REWRITE_CONFIG var) so users have an escape hatch when "
        "they intentionally want THOTH_PG_DSN reset to install default."
    )


def test_env_mutation_is_backed_up_first() -> None:
    """Any in-place sed against $THOTH_HOME/.env must be preceded by a
    backup. The grep is intentionally loose — we just want the backup
    helper to be called from copy_config_templates so a regression
    can't silently re-introduce blind rewrites."""
    body = _extract_function_body("copy_config_templates")
    # The single sed -i call against .env in this function rewrites
    # THOTH_PG_DSN. It must be preceded by a backup invocation.
    assert "_backup_env_file" in body, (
        "copy_config_templates uses ``sed -i`` against $THOTH_HOME/.env. "
        "It must call _backup_env_file first so users can recover from a "
        "bad rewrite. Without this, an installer bug silently destroys "
        "the user's .env."
    )


def test_force_rewrite_flag_documented_in_help() -> None:
    """The --force-rewrite-config escape hatch must be discoverable
    from ``install.sh --help`` — otherwise users hit the preserve-
    user-config path and have no obvious way to opt out."""
    text = _read_install_sh()
    assert "--force-rewrite-config" in text
    # Ensure it shows up in the help block (HELP_EOF), not just in the
    # arg parser switch.
    help_match = re.search(r"HELP_EOF(?P<help>.*?)HELP_EOF", text, re.DOTALL)
    assert help_match is not None, "install.sh --help block not found"
    assert "--force-rewrite-config" in help_match["help"], (
        "--force-rewrite-config must appear in the help block so users "
        "can discover the escape hatch via --help."
    )


# ---------------------------------------------------------------------------
# Substrate worker systemd unit — install.sh must install + enable it so
# new installs and upgrades both end up with a running worker without
# the operator having to know it exists. The worker is what runs the
# Sentinel/Curator tick loops; without it the substrate is inert. See
# scripts/install.sh::setup_substrate_worker_service and the writer/
# worker boot-split commit.
# ---------------------------------------------------------------------------


def test_setup_substrate_worker_service_function_exists() -> None:
    """install.sh ships a function that installs the substrate-worker
    systemd unit. Without it, fresh installs come up with sub-agents
    that aren't ticking."""
    text = _read_install_sh()
    assert "setup_substrate_worker_service()" in text, (
        "install.sh must define setup_substrate_worker_service; without "
        "it the substrate worker subprocess is not installed and "
        "Sentinel/Curator don't tick after install."
    )


def test_setup_substrate_worker_service_called_from_main() -> None:
    """The function must be wired into main() — defining it without
    calling it would be a silent regression."""
    body = _extract_function_body("main")
    assert "setup_substrate_worker_service" in body, (
        "setup_substrate_worker_service defined but not called from "
        "main(). The unit will never be installed."
    )


def test_substrate_worker_unit_uses_install_paths() -> None:
    """The rendered unit must reference the install's actual paths
    (INSTALL_DIR for ExecStart, THOTH_HOME for EnvironmentFile) so
    operators with custom --hermes-home / --cli-name don't get a broken
    unit pointing at someone else's home directory."""
    body = _extract_function_body("setup_substrate_worker_service")
    # ExecStart resolves to the actual venv python in this install.
    assert "$INSTALL_DIR/venv/bin/python" in body or \
           "$python_path" in body, (
        "Unit's ExecStart must use $INSTALL_DIR-derived python path; "
        "hardcoding ~/.hermes/hermes-agent breaks custom-dir installs."
    )
    # EnvironmentFile points at $THOTH_HOME/.env, not %h/.hermes/.env.
    assert "EnvironmentFile=$env_file" in body or \
           "EnvironmentFile=$THOTH_HOME" in body, (
        "Unit's EnvironmentFile must use $THOTH_HOME-derived path so "
        "operators with custom --hermes-home get a working unit."
    )


def test_substrate_worker_update_path_restarts_if_active() -> None:
    """On update, if the worker unit was already active, restart it
    (to pick up new code + unit changes). Don't enable a unit the
    operator chose to disable, and don't start one they chose to stop."""
    body = _extract_function_body("setup_substrate_worker_service")
    assert "was_active" in body, (
        "setup_substrate_worker_service must capture prior is-active "
        "state before mutating the unit; otherwise the update path "
        "can't distinguish 'restart this' from 'leave alone'."
    )
    assert "was_enabled" in body, (
        "setup_substrate_worker_service must capture prior is-enabled "
        "state so operator-disabled units stay disabled across updates."
    )
    assert "systemctl $scope restart" in body or \
           "systemctl $scope enable --now" in body, (
        "setup_substrate_worker_service must restart-if-active and "
        "enable-now-on-fresh; neither path can be missing."
    )


def test_pg_port_detection_handles_dual_stack_bindings() -> None:
    """Regression: docker's port-mapping inspect format emits ONE entry
    per host binding. A container with both IPv4 (0.0.0.0:5433) and
    IPv6 ([::]:5433) bindings of the same container port produces two
    ``HostPort`` entries. A naive ``{{range $conf}}{{.HostPort}}{{end}}``
    concatenates them into ``"54335433"``, which install.sh then tries
    to bind as a host port (and fails). Inject whitespace + take the
    first field so single-port and dual-stack containers both yield a
    valid port number.

    Hit on 2026-05-26 when re-running install.sh against an existing
    install: ``54335433`` was passed as POSTGRES_PORT and docker
    compose refused with ``invalid hostPort: 54335433``."""
    body = _extract_function_body("choose_pg_port")
    assert "{{.HostPort}} {{end}}" in body, (
        "PG container port-inspect template must separate multiple "
        "HostPort values with whitespace; without the space, IPv4+IPv6 "
        "dual-stack containers produce concatenated bogus ports like "
        "54335433."
    )
    assert "awk '{print $1}'" in body, (
        "After space-separating HostPort values, the result must be "
        "narrowed to a single field with ``awk '{print $1}'`` (or "
        "equivalent), otherwise the trailing space makes "
        "downstream PG_PORT_DEFAULT comparisons spurious."
    )


# ---------------------------------------------------------------------------
# Installer robustness: (1) never silently reuse a stale postgres data volume,
# and (2) fail loudly when the dependency sync left a half-built venv. See
# scripts/install.sh::_warn_or_reset_pg_volume / choose_pg_port / verify_core_deps.
#
# Background: a "fresh" install on a new machine inherited a leftover postgres
# named volume (hermes_pg_data). choose_pg_port removed the OLD container to
# reclaim its port, but the named VOLUME persisted and the new compose-up
# re-attached it — so the install inherited a stale alembic_version whose
# schema didn't match this checkout, exploding mid-migration with a cryptic
# UndefinedTableError. Separately, an interrupted `uv sync` left a venv missing
# core deps (alembic/sqlalchemy/asyncpg are BASE deps, not extras) yet the
# install proceeded to the migration step and failed obscurely.
# ---------------------------------------------------------------------------


def test_reset_db_flag_is_parsed() -> None:
    """``--reset-db`` must be wired into the arg-parsing switch and set a
    dedicated flag variable; otherwise the operator has no way to opt into
    a clean database."""
    text = _read_install_sh()
    assert "--reset-db)" in text, (
        "--reset-db must appear as a case in the argument-parsing switch."
    )
    assert "RESET_DB=true" in text, (
        "--reset-db must set RESET_DB=true so downstream volume handling "
        "can branch on it."
    )
    # The flag must default to false — we must NEVER drop data unless asked.
    assert re.search(r"^RESET_DB=false", text, re.MULTILINE), (
        "RESET_DB must default to false so the installer never destroys a "
        "data volume unless --reset-db is explicitly passed."
    )


def test_reset_db_flag_documented_in_help() -> None:
    """``--reset-db`` must be discoverable from ``install.sh --help`` and the
    help text must flag it as destructive."""
    text = _read_install_sh()
    help_match = re.search(r"HELP_EOF(?P<help>.*?)HELP_EOF", text, re.DOTALL)
    assert help_match is not None, "install.sh --help block not found"
    help_text = help_match["help"]
    assert "--reset-db" in help_text, (
        "--reset-db must appear in the --help block so it is discoverable."
    )
    assert "DESTRUCTIVE" in help_text or "destroy" in help_text.lower(), (
        "--reset-db help text must warn that it is destructive (it wipes "
        "the postgres data volume)."
    )


def test_volume_reuse_warns_loudly() -> None:
    """When an existing postgres container/volume is reused, the installer
    must LOG A WARNING that existing DATA is being reused. Without this, a
    'fresh' install silently inherits a stale schema."""
    body = _extract_function_body("_warn_or_reset_pg_volume")
    assert "log_warn" in body, (
        "_warn_or_reset_pg_volume must call log_warn on the reuse path so "
        "operators see that existing PostgreSQL data is being inherited."
    )
    # The warning must specifically mention reusing data / the volume so the
    # message is actionable, not a generic info line.
    assert "REUSING" in body or "reus" in body.lower(), (
        "The reuse warning must explicitly say existing data is being reused."
    )
    # Best-effort surfacing of the inherited alembic_version helps the
    # operator diagnose schema-mismatch failures.
    assert "alembic_version" in body, (
        "_warn_or_reset_pg_volume should surface the inherited "
        "alembic_version (best-effort) so schema drift is visible."
    )


def test_choose_pg_port_invokes_volume_handler_on_reuse() -> None:
    """choose_pg_port must route the existing-container reuse path through
    _warn_or_reset_pg_volume so the warn/reset logic actually runs."""
    body = _extract_function_body("choose_pg_port")
    assert "_warn_or_reset_pg_volume" in body, (
        "choose_pg_port must call _warn_or_reset_pg_volume on the reuse "
        "path; otherwise the loud warning / --reset-db drop never fires."
    )


def test_volume_drop_only_under_reset_db() -> None:
    """The volume must be destroyed ONLY when --reset-db is set. The
    `down -v` / `docker volume rm` calls must be guarded by RESET_DB so a
    plain re-install never wipes the operator's database."""
    body = _extract_function_body("_warn_or_reset_pg_volume")
    # Both destructive primitives must live inside a RESET_DB=true guard.
    reset_guard = re.search(
        r'if \[ "\$RESET_DB" = true \];.*?\n(?P<guarded>.*?)\n\s*return 0',
        body,
        re.DOTALL,
    )
    assert reset_guard is not None, (
        "_warn_or_reset_pg_volume must have an `if [ \"$RESET_DB\" = true ]` "
        "branch that performs the destructive drop and returns early."
    )
    guarded = reset_guard["guarded"]
    assert "down -v" in guarded or "volume rm" in guarded, (
        "The destructive volume drop (down -v / docker volume rm) must live "
        "inside the RESET_DB guard."
    )
    # Outside the guard there must be NO unconditional destructive call.
    outside = body[: reset_guard.start()] + body[reset_guard.end():]
    assert "down -v" not in outside and "volume rm" not in outside, (
        "No destructive volume operation may exist outside the RESET_DB "
        "guard — a plain re-install must never wipe data."
    )


def test_verify_core_deps_function_exists_and_checks_base_deps() -> None:
    """install.sh must verify the venv can import the BASE deps the very next
    step (alembic upgrade) needs, and abort loudly if not."""
    text = _read_install_sh()
    assert "verify_core_deps()" in text, (
        "verify_core_deps must be defined to validate the venv before "
        "migrations run."
    )
    body = _extract_function_body("verify_core_deps")
    # Import check via the venv python (uv-managed: no pip available).
    assert "import alembic, sqlalchemy, asyncpg" in body, (
        "verify_core_deps must import alembic, sqlalchemy AND asyncpg via the "
        "venv python — all three are base deps the migration step needs."
    )
    assert "venv/bin/python" in body, (
        "verify_core_deps must use the venv's python (./venv/bin/python) for "
        "the import check, not pip (the venv is uv-managed)."
    )
    # Must abort, not warn-and-continue, on failure.
    assert "exit 1" in body, (
        "verify_core_deps must `exit 1` when imports fail — continuing to "
        "the migration step with a half-built venv is exactly the bug."
    )
    assert "log_error" in body, (
        "verify_core_deps must log_error explaining the venv is incomplete."
    )


def test_verify_core_deps_runs_before_migrations() -> None:
    """verify_core_deps must run after install_deps and before
    setup_postgres/run_migrations in main(), so a half-built venv aborts the
    install before the cryptic migration failure."""
    body = _extract_function_body("main")
    assert "verify_core_deps" in body, (
        "verify_core_deps must be wired into main()."
    )
    install_pos = body.index("install_deps")
    verify_pos = body.index("verify_core_deps")
    migrate_pos = body.index("run_migrations")
    assert install_pos < verify_pos < migrate_pos, (
        "verify_core_deps must run after install_deps and before "
        "run_migrations so the dep check gates the migration step."
    )


def test_substrate_worker_skips_cleanly_without_systemd() -> None:
    """Termux / non-Linux / no-systemctl environments must not error
    out — install.sh should print manual steps and continue."""
    body = _extract_function_body("setup_substrate_worker_service")
    assert 'DISTRO" = "termux"' in body, (
        "Termux skip-path missing; Termux has no systemd."
    )
    assert "command -v systemctl" in body, (
        "systemctl presence check missing; some Linux containers "
        "(distroless, scratch) ship without systemctl and the install "
        "must still succeed there."
    )
