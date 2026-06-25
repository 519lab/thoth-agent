"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _thoth_home_path() -> Path:
    """Resolve the active THOTH_HOME (profile-aware) without circular imports."""
    try:
        from thoth_constants import get_thoth_home  # local import to avoid cycles
        return get_thoth_home()
    except Exception:
        return Path(os.path.expanduser("~/.thoth"))


def _thoth_root_path() -> Path:
    """Resolve the Thoth root dir (always the parent of any profile, never per-profile)."""
    try:
        from thoth_constants import get_default_thoth_root  # local import to avoid cycles
        return get_default_thoth_root()
    except Exception:
        return Path(os.path.expanduser("~/.thoth"))


_CWD_PLACEHOLDERS = {"", ".", "auto", "cwd"}


def resolve_active_root(
    explicit_cwd: Optional[str],
    launch_cwd: Optional[str],
    is_gateway: bool,
) -> Path:
    """Resolve the agent's *active root* — the directory it may freely operate in.

    Fixed and session-immutable (distinct from ``TERMINAL_CWD``, which drifts as
    the agent ``cd``s around). Resolution order:

    1. An explicit, non-placeholder working dir (``terminal.cwd`` / ``MESSAGING_CWD``
       — the user chose it) wins.
    2. Interactive CLI launch (``not is_gateway``): if the launch dir is the user's
       ``$HOME`` or inside THOTH_HOME ("no project intent"), fall through to the
       default workspace; otherwise the launch dir is the active root (the user
       intentionally ``cd``'d into a project before running ``thoth``).
    3. Default: ``{THOTH_HOME}/workspace`` (auto-created).
    """
    def _real(p: str) -> str:
        return os.path.realpath(os.path.expanduser(p))

    # 1. Explicit working dir.
    if explicit_cwd and explicit_cwd not in _CWD_PLACEHOLDERS:
        try:
            return Path(_real(explicit_cwd))
        except Exception:
            pass  # fall through to default

    # 2. Interactive CLI launched intentionally inside a project dir.
    if not is_gateway and launch_cwd:
        try:
            launch_real = _real(launch_cwd)
            home_real = _real("~")
            thoth_real = os.path.realpath(_thoth_home_path())
            inside_thoth = (
                launch_real == thoth_real or launch_real.startswith(thoth_real + os.sep)
            )
            if launch_real != home_real and not inside_thoth:
                return Path(launch_real)
        except Exception:
            pass

    # 3. Default workspace.
    try:
        from thoth_constants import ensure_workspace_dir  # local import to avoid cycles
        return ensure_workspace_dir()
    except Exception:
        return _thoth_home_path() / "workspace"


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    thoth_home = _thoth_home_path()
    thoth_root = _thoth_root_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            # Active profile .env (or top-level .env when not in profile mode).
            str(thoth_home / ".env"),
            # Top-level .env, even when running under a profile — overwriting it
            # leaks credentials across every profile that inherits from root (#15981).
            str(thoth_root / ".env"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        ]
    ]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved THOTH_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("THOTH_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    # Thoth control-plane files: block both the ACTIVE profile's view
    # (thoth_home) AND the global root view. Without the root pass, a
    # profile-mode session leaves <root>/auth.json + <root>/config.yaml
    # writable — letting a prompt-injected write_file overwrite the global
    # files that every profile inherits from (same shape as #15981).
    control_file_names = ("auth.json", "config.yaml", "webhook_subscriptions.json")
    mcp_tokens_dir_name = "mcp-tokens"

    thoth_dirs = []
    for base in (_thoth_home_path(), _thoth_root_path()):
        try:
            real = os.path.realpath(base)
            if real not in thoth_dirs:
                thoth_dirs.append(real)
        except Exception:
            continue

    for base_real in thoth_dirs:
        for name in control_file_names:
            try:
                if resolved == os.path.realpath(os.path.join(base_real, name)):
                    return True
            except Exception:
                continue
        try:
            mcp_real = os.path.realpath(os.path.join(base_real, mcp_tokens_dir_name))
            if resolved == mcp_real or resolved.startswith(mcp_real + os.sep):
                return True
        except Exception:
            pass

    safe_root = get_safe_write_root()
    if safe_root and not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):
        return True

    return False


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets internal Thoth cache files."""
    resolved = Path(path).expanduser().resolve()
    thoth_home = _thoth_home_path().resolve()
    blocked_dirs = [
        thoth_home / "skills" / ".hub" / "index-cache",
        thoth_home / "skills" / ".hub",
    ]
    for blocked in blocked_dirs:
        try:
            resolved.relative_to(blocked)
        except ValueError:
            continue
        return (
            f"Access denied: {path} is an internal Thoth cache file "
            "and cannot be read directly to prevent prompt injection. "
            "Use the skills_list or skill_view tools instead."
        )
    return None
