"""thoth hermes — import settings/config from a legacy Hermes Agent install.

Thoth began as a fork of Hermes Agent and now stands on its own. A fresh
Thoth install can pull an existing ``~/.hermes`` home — config, env, memories,
skills, auth/state — into ``~/.thoth``. This is a *same-lineage* port: a
straight copy of user data, not a config transform (contrast ``thoth claw``,
which migrates a foreign tool). Code, venvs, caches, logs and runtime
lock/pid files are never copied.

    thoth hermes migrate              # preview, then migrate (asks to confirm)
    thoth hermes migrate --dry-run    # preview only, no changes
    thoth hermes migrate --yes        # skip the confirmation prompt
    thoth hermes migrate --source /path/to/.hermes
    thoth hermes migrate --overwrite  # replace items that already exist in ~/.thoth
    thoth hermes cleanup              # archive ~/.hermes -> ~/.hermes.pre-migration
    thoth hermes cleanup --dry-run
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from thoth_cli.cli_name import cli_name
from thoth_cli.config import get_thoth_home
from thoth_cli.setup import (
    print_header,
    print_info,
    print_success,
    print_error,
    prompt_yes_no,
)

logger = logging.getLogger(__name__)

# Default legacy Hermes home. ``~/.thoth`` may currently be a symlink to this
# on in-place installs; the importer resolves both and refuses to copy a
# directory onto itself (see _resolve_paths).
_DEFAULT_HERMES_HOME = Path.home() / ".hermes"

# Allowlist of user-data items to port (relative to the Hermes home). An
# allowlist — not an exclusion list — so we never accidentally drag over the
# vendored code tree (``hermes-agent/``), the venv, caches, logs, sandboxes,
# or runtime ``*.lock`` / ``*.pid`` / ``processes.json`` files.
_PORTABLE_ITEMS: tuple[str, ...] = (
    "config.yaml",
    ".env",
    "SOUL.md",
    "memories",
    "skills",
    "auth.json",
    "channel_directory.json",
    "gateway_state.json",
    "pairing",
    "cron",
    "hooks",
)


def _resolve_paths(source: Optional[str]) -> tuple[Path, Path]:
    """Return (hermes_home, thoth_home) as resolved absolute paths."""
    hermes = Path(source).expanduser() if source else _DEFAULT_HERMES_HOME
    return hermes.resolve(), get_thoth_home().resolve()


def detect_hermes_residue(home: Optional[Path] = None) -> bool:
    """True if a legacy ``~/.hermes`` home is present and is NOT the same
    directory Thoth already uses (covers the symlink-to-self case)."""
    base = home or Path.home()
    hermes = (base / ".hermes")
    if not hermes.is_dir():
        return False
    try:
        return hermes.resolve() != get_thoth_home().resolve()
    except Exception:
        return True


def hermes_residue_hint_cli() -> str:
    """Banner shown on first run when a portable ``~/.hermes`` is detected."""
    return (
        "A legacy Hermes Agent home was detected at ~/.hermes/.\n"
        f"To import its config, memories and skills into Thoth, run "
        f"`{cli_name()} hermes migrate`.\n"
        f"After importing, archive the old home with "
        f"`{cli_name()} hermes cleanup`."
    )


def _plan(hermes: Path, thoth: Path, overwrite: bool) -> list[tuple[str, str]]:
    """Build a (item, action) plan. action ∈ copy / overwrite / skip-exists / absent."""
    plan: list[tuple[str, str]] = []
    for name in _PORTABLE_ITEMS:
        src = hermes / name
        if not src.exists():
            plan.append((name, "absent"))
            continue
        dst = thoth / name
        if dst.exists():
            plan.append((name, "overwrite" if overwrite else "skip-exists"))
        else:
            plan.append((name, "copy"))
    return plan


def _copy_item(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _cmd_migrate(args) -> int:
    hermes, thoth = _resolve_paths(getattr(args, "source", None))
    dry_run = getattr(args, "dry_run", False)
    overwrite = getattr(args, "overwrite", False)
    auto_yes = getattr(args, "yes", False)

    print_header("Hermes → Thoth import")
    if not hermes.is_dir():
        print_error(f"No Hermes home found at {hermes}")
        print_info(f"Specify one with: {cli_name()} hermes migrate --source /path/to/.hermes")
        return 1
    if hermes == thoth:
        print_error(
            f"Hermes home and Thoth home are the same directory ({thoth}).\n"
            "Nothing to import — this install already uses it directly."
        )
        return 1

    plan = _plan(hermes, thoth, overwrite)
    to_apply = [(n, a) for n, a in plan if a in ("copy", "overwrite")]

    print_info(f"Source: {hermes}")
    print_info(f"Target: {thoth}")
    print()
    for name, action in plan:
        symbol = {"copy": "+", "overwrite": "~", "skip-exists": "=", "absent": " "}[action]
        note = {
            "copy": "import",
            "overwrite": "replace existing",
            "skip-exists": "exists — skip (use --overwrite)",
            "absent": "not in source",
        }[action]
        print(f"  [{symbol}] {name:<22} {note}")
    print()

    if not to_apply:
        print_info("Nothing to import (everything is absent or already present).")
        return 0
    if dry_run:
        print_info(f"Dry run — no changes. Re-run with `{cli_name()} hermes migrate --yes` to apply.")
        return 0
    if not auto_yes and not prompt_yes_no(f"Import {len(to_apply)} item(s) into {thoth}?"):
        print_info("Aborted.")
        return 0

    # Pre-migration backup of the Thoth home (best-effort).
    try:
        from thoth_cli.backup import create_pre_migration_backup

        archive = create_pre_migration_backup()
        if archive:
            print_info(f"Backed up current Thoth home to: {archive}")
    except Exception:
        logger.debug("pre-migration backup failed", exc_info=True)

    thoth.mkdir(parents=True, exist_ok=True)
    done = 0
    for name, action in to_apply:
        try:
            _copy_item(hermes / name, thoth / name)
            done += 1
            print_success(f"imported {name}")
        except Exception as e:
            print_error(f"failed to import {name}: {e}")
            logger.debug("hermes import item failed: %s", name, exc_info=True)

    print()
    print_success(f"Imported {done}/{len(to_apply)} item(s) into {thoth}.")
    print_info(
        f"The Hermes home was left untouched. Archive it with "
        f"`{cli_name()} hermes cleanup` once you've confirmed Thoth works."
    )
    return 0


def _cmd_cleanup(args) -> int:
    hermes, thoth = _resolve_paths(getattr(args, "source", None))
    dry_run = getattr(args, "dry_run", False)
    auto_yes = getattr(args, "yes", False)

    print_header("Hermes cleanup")
    if not hermes.is_dir() or hermes.is_symlink():
        print_info(f"No legacy Hermes directory to archive at {hermes}.")
        return 0
    if hermes == thoth:
        print_error("Refusing to archive — that path is the active Thoth home.")
        return 1

    archive = hermes.with_name(hermes.name + ".pre-migration")
    print_info(f"Will archive: {hermes}  ->  {archive}")
    if dry_run:
        print_info("Dry run — no changes.")
        return 0
    if archive.exists():
        print_error(f"Archive target already exists: {archive}")
        return 1
    if not auto_yes and not prompt_yes_no(
        "Archive the legacy Hermes home? (Hermes Agent will stop working after this.)"
    ):
        print_info("Aborted.")
        return 0
    try:
        hermes.rename(archive)
    except Exception as e:
        print_error(f"Archive failed: {e}")
        return 1
    print_success(f"Archived to {archive}.")
    return 0


def hermes_command(args) -> int:
    """Route ``thoth hermes`` subcommands."""
    action = getattr(args, "hermes_action", None)
    if action == "migrate":
        return _cmd_migrate(args)
    if action in {"cleanup", "clean"}:
        return _cmd_cleanup(args)
    print(f"Usage: {cli_name()} hermes <command> [options]")
    print()
    print("Commands:")
    print("  migrate          Import settings/config/memories from a legacy ~/.hermes")
    print("  cleanup          Archive the legacy ~/.hermes after importing")
    print()
    print(f"Run '{cli_name()} hermes <command> --help' for options.")
    return 0


__all__ = [
    "hermes_command",
    "detect_hermes_residue",
    "hermes_residue_hint_cli",
]
