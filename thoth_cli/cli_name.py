"""Resolve the name the user actually invoked this CLI with.

A Hermes Substrate fork can be installed side-by-side with an upstream
``hermes`` under a different launcher name (e.g. ``hermes-substrate``).
User-facing hints like ``<cli> --resume <id>`` must echo back whatever
launcher the user actually typed, otherwise the suggestion points at the
wrong install (or a command that doesn't exist).

Resolution order (see ``cli_name``):
    1. ``$HERMES_CLI_NAME`` — set by the launcher shim to the exact command
       the user invoked. This is authoritative: the shim execs the venv
       console script (itself named ``hermes``), so by the time Python runs,
       ``sys.argv[0]`` reflects the venv entry point's name, NOT the shim the
       user typed. The env var is inherited across self-relaunches, so it
       stays correct through ``sessions browse`` / post-setup re-execs.
    2. ``sys.argv[0]`` basename — for invocations that don't go through the
       shim (a renamed standalone executable, a dev checkout). Skipped for
       module/interpreter launches (``python -m hermes_cli.main`` →
       ``__main__.py``) which don't reflect a user-facing command name.
    3. ``os.path.basename(sys.executable)`` — last-resort interpreter name.
    4. ``"hermes"`` — historical default.
"""

import os
import sys

_DEFAULT_NAME = "hermes"

# argv[0] basenames that don't represent a user-facing launcher command.
_NON_LAUNCHER_BASENAMES = {
    "__main__.py",
    "__main__",
    "main.py",
    "-c",
    "",
}


def _looks_like_interpreter(base: str) -> bool:
    """True for python/pytest/uv-style interpreter basenames."""
    low = base.lower()
    return (
        low.startswith("python")
        or low.startswith("pypy")
        or low in {"uv", "uvx", "pytest", "py.test"}
    )


def _sanitize(base: str) -> str | None:
    """Return a usable launcher name from a basename, or None to skip it."""
    if not base or base in _NON_LAUNCHER_BASENAMES:
        return None
    # Module launches surface the script filename, not a command name.
    if base.endswith((".py", ".pyc")):
        return None
    if _looks_like_interpreter(base):
        return None
    # Windows console-script shims carry a .exe suffix the user never types.
    if base.lower().endswith(".exe"):
        base = base[: -len(".exe")]
    return base or None


def cli_name() -> str:
    """Best-effort name the user invoked this CLI with (e.g. ``hermes``)."""
    env_name = os.environ.get("HERMES_CLI_NAME", "").strip()
    if env_name:
        sanitized = _sanitize(os.path.basename(env_name))
        if sanitized:
            return sanitized

    argv0 = sys.argv[0] if sys.argv else ""
    name = _sanitize(os.path.basename(argv0))
    if name:
        return name

    exe = _sanitize(os.path.basename(sys.executable or ""))
    if exe:
        return exe

    return _DEFAULT_NAME
