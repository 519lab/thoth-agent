"""Regression guard: no stray ``hermes`` references in tracked files.

The project was renamed Hermes Agent → Thoth Agent across several "de-Hermes"
waves. Residue kept surviving each wave because nothing *prevented* new
occurrences. This test is that backstop: it scans every tracked text file for
``hermes`` (case-insensitive) and fails on anything not on the allowlist.

The allowlist is deliberately *concept-scoped*, not blanket — the guard still
catches the regression shapes that matter (``class HermesAgent``,
``import hermes``, ``HERMES_GATEWAY``, "Welcome to Hermes Agent" prose, …).
Legitimately-kept "hermes" falls into a handful of buckets:

  * **Historical changelogs** — ``docs/releases/**`` shipped under the Hermes
    name; rewriting shipped history (and the upstream NousResearch PR links it
    cites) is wrong.
  * **External names we don't control** — Meta's Hermes JS engine
    (``hermes-parser`` / ``hermes-estree`` / ``hermes-engine``); the Nous
    **Hermes** model family (``NousResearch``, ``nous/hermes-*``, ``Hermes 4``,
    ``hermes@nousresearch.com``); third-party repos/tools (``hermes-lcm``,
    ``rtk-hermes``, ``hermes-mod``, ``hermes-achievements``, ``grok-hermes``,
    ``sample-hermes-agent-on-aws``, ``nix-hermes-agent``, the GSD ``--hermes``
    flag, OpenClaw's ``migrate-hermes``, …).
  * **Legacy migrators** — code/tests whose job is to read a user's pre-rename
    ``~/.hermes`` install (env-var translator, sqlite→PG migrator, toolset-name
    migrator, uninstaller, backup excluder, …). These must name the old refs to
    function.
  * **Upstream attribution prose** — "a fork of Hermes by Nous Research",
    "the original Hermes copyright", "Upstream Hermes …".
  * **Historical contributor maps** — release/contributor tooling that maps
    long-since-merged commit emails to GitHub handles.

If you're adding a legitimate new exception, extend ``ALLOWED_LINE_RES`` or
``ALLOWED_PATHS`` with a comment explaining why — don't just silence it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Whole files (by path prefix) that may legitimately contain "hermes".
ALLOWED_PATH_PREFIXES = (
    "docs/releases/",  # historical changelogs — frozen (incl. upstream PR links)
    "skills/red-teaming/godmode/",  # Nous Hermes model family (jailbreak refs)
)

# Whole files (matched by exact relative path via endswith) that are allowlisted
# in their entirety because every "hermes" in them is a legitimate keep.
ALLOWED_PATH_SUFFIXES = (
    # lockfiles — transitive package names we don't control
    "package-lock.json",
    "uv.lock",
    # this guard names "hermes" by necessity
    "tests/test_no_hermes_residue.py",
    # generated aggregate of all docs (mirrors external/Nous refs from sources)
    "website/static/llms-full.txt",
    # rendered Nous-Hermes red-team docs (model-family refs)
    "website/docs/user-guide/skills/godmode.md",
    "website/docs/user-guide/skills/bundled/red-teaming/red-teaming-godmode.md",
    # Chinese attribution prose (Nous Research / Hermes credit)
    "README.zh-CN.md",
    # legacy-migrator source: read/translate a pre-rename ~/.hermes install
    "thoth_cli/env_translator.py",      # HERMES_* -> THOTH_* .env translator
    "thoth_cli/db_commands.py",         # sqlite ~/.hermes + hermes.* stream migrator
    "thoth_cli/uninstall.py",           # cleans legacy hermes install layout
    "thoth_cli/stdio.py",               # resolves legacy hermes code/git dirs
    "thoth_cli/backup.py",              # excludes legacy hermes-agent code dir
    "thoth_cli/banner.py",              # locates legacy hermes-agent code dir
    "thoth_cli/profiles.py",            # excludes legacy hermes-agent code dir
    "thoth_cli/model_switch.py",        # Nous Hermes 3/4 non-agentic warning
    "thoth_cli/config.py",              # legacy hermes-* toolset preset rename map
    # historical contributor / release tooling (long-merged commit emails)
    "scripts/release.py",
    "scripts/contributor_audit.py",
    # negative-assertion / migration tests
    "tests/test_thoth_home.py",
    "tests/test_install_ps1_audit.py",
    "tests/thoth_cli/test_env_translator.py",
    "tests/thoth_cli/test_toolset_name_migration.py",
    "tests/thoth_cli/test_nous_hermes_non_agentic.py",
    "tests/tools/test_docker_entrypoint_cli.py",
    "tests/thoth_cli/test_gateway_service.py",
)

# Lines matching any of these are legitimate (external names / Nous model /
# legacy-migrator literals / attribution prose).
ALLOWED_LINE_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # --- Meta's Hermes JS engine (metro/babel dependency) ---
        r"hermes-parser",
        r"hermes-estree",
        r"hermes-engine",
        # --- Nous Hermes model family ---
        r"nousresearch",
        r"nous[\s_/-]*hermes",       # nous/hermes-*, nous-hermes
        r"hermes[\s_.\-]*\d",        # Hermes 3 / Hermes 4 405B / Hermes_4.5 / hermes-4
        r"hermes model family",
        r"hermes models",            # "Skip Hermes models", "Hermes models are uncensored"
        r'"hermes" in ',             # model-id substring checks
        r"mimo,\s*hermes",           # Nous Portal provider list (DeepSeek, Kimi, MiMo, Hermes)
        # --- third-party repos / plugins / tools (not our product) ---
        r"hermes-lcm",
        r"rtk-hermes",
        r"hermes-mod",
        r"hermes-achievements",
        r"hermes-plugin",
        r"hermes-permission",        # external Linear doc-slug example
        r"grok-hermes",
        r"migrate-hermes",           # OpenClaw extension path
        r"--hermes",                 # GSD (get-shit-done) external CLI flag
        r"hermes-agent",             # legacy code-dir name + external repos
                                     # (sample-hermes-agent-on-aws, nix-hermes-agent,
                                     #  *-comfyui-helper) + upstream layout attribution
        # --- legacy-migrator literals / migration prose ---
        r"\.hermes\b",               # ~/.hermes legacy home path
        r"de-hermes",                # "de-Hermes rename" migration prose
        r"hermes\.service",          # legacy systemd unit detection
        r"hermes -p",                # doctor.py legacy claude-config probe
        r"ai\.hermes\.gateway",      # legacy launchd label (negative assertions)
        # --- upstream attribution prose ---
        r"shaped hermes",
        r"hermes copyright",
        r"fork of hermes",
        r"upstream hermes",
        r"hermes by nous",
    )
]

_TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md",
    ".sh", ".bash", ".zsh", ".nix", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".txt", ".html", ".css", ".env", ".ps1", ".plist", ".service", "",
}


def _tracked_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "ls-files"], cwd=REPO_ROOT, text=True
    )
    return out.splitlines()


def test_no_hermes_residue() -> None:
    violations: list[str] = []
    for rel in _tracked_files():
        if rel.startswith(ALLOWED_PATH_PREFIXES):
            continue
        if rel.endswith(ALLOWED_PATH_SUFFIXES):
            continue
        p = REPO_ROOT / rel
        if p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "hermes" not in line.lower():
                continue
            if any(rx.search(line) for rx in ALLOWED_LINE_RES):
                continue
            violations.append(f"{rel}:{i}: {line.strip()[:140]}")

    assert not violations, (
        f"Found {len(violations)} stray 'hermes' reference(s) — the project is "
        "Thoth now. Rename to thoth, or add a justified allowlist entry in "
        "tests/test_no_hermes_residue.py:\n" + "\n".join(violations)
    )
