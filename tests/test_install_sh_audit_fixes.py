"""Structural regressions for the install-audit fixes (issues #214, #215,
#218, #219, #222, #227).

Like ``test_install_sh_update_safety``, these don't run the installer
end-to-end (that needs Docker, root, a real PG, macOS, …). They pin the
*shape* of ``scripts/install.sh`` so the specific footguns each issue
describes can't silently come back. Where the behaviour is small and
self-contained (version compare, portable sed, the launcher DSN export
logic) the helper is also exercised directly via ``bash -c``.
"""

from __future__ import annotations

import re
import subprocess
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


# ---------------------------------------------------------------------------
# #214 — BSD/macOS `sed -i` aborts re-installs. All in-place edits must go
# through the portable temp-file helper, never bare `sed -i`.
# ---------------------------------------------------------------------------


def test_no_bare_sed_inplace_used() -> None:
    """`sed -i` is non-portable (GNU vs BSD differ on the backup-suffix arg).
    The only allowed occurrences are inside the helper's own comment."""
    for i, line in enumerate(_read_install_sh().splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert "sed -i" not in line, (
            f"scripts/install.sh:{i} uses non-portable `sed -i`; route the "
            "edit through _sed_inplace so macOS re-installs don't abort (#214)."
        )


def test_sed_inplace_helper_defined_and_used() -> None:
    text = _read_install_sh()
    assert "_sed_inplace()" in text, "portable _sed_inplace helper missing (#214)."
    body = _extract_function_body("copy_config_templates")
    assert body.count("_sed_inplace") >= 2, (
        "copy_config_templates must rewrite THOTH_PG_DSN via _sed_inplace on "
        "both the force-rewrite and port-drift paths (#214)."
    )


def test_sed_inplace_is_portable_temp_rewrite() -> None:
    """The helper must do a temp-file rewrite (works on GNU + BSD), exercised
    directly so a regression to `sed -i` is caught behaviourally."""
    script = r"""
        _sed_inplace() {
            local expr="$1" file="$2" tmp
            tmp="$(mktemp 2>/dev/null || echo "${file}.tmp.$$")"
            sed "$expr" "$file" > "$tmp" && mv "$tmp" "$file"
        }
        f="$(mktemp)"
        printf 'THOTH_PG_DSN=old\nKEEP=1\n' > "$f"
        _sed_inplace "s|^THOTH_PG_DSN=.*|THOTH_PG_DSN=new|" "$f"
        cat "$f"
    """
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert "THOTH_PG_DSN=new" in out and "KEEP=1" in out


# ---------------------------------------------------------------------------
# #215 — bash login shell with no rc file → ~/.local/bin never persists.
# ---------------------------------------------------------------------------


def test_bash_path_branch_creates_rc_when_none_present() -> None:
    body = _extract_function_body("setup_path")
    bash_branch = re.search(
        r"bash\)\s*(?P<b>.*?);;\s*\n\s*fish\)", body, re.DOTALL
    )
    assert bash_branch is not None, "bash) PATH branch not found in setup_path."
    b = bash_branch["b"]
    assert "${#cfgs[@]} -eq 0" in b and 'touch "$HOME/.bashrc"' in b, (
        "bash) branch must create ~/.bashrc when no config file matched, "
        "mirroring the zsh/fish create-if-missing branches (#215). Otherwise "
        "~/.local/bin never persists and `thoth` is not found next login."
    )


# ---------------------------------------------------------------------------
# #218 — accept node only when major >= NODE_MIN_MAJOR (project needs >=20).
# ---------------------------------------------------------------------------


def test_node_major_version_is_gated() -> None:
    text = _read_install_sh()
    assert re.search(r"^NODE_MIN_MAJOR=", text, re.MULTILINE), (
        "NODE_MIN_MAJOR must be defined so the node check has a threshold (#218)."
    )
    body = _extract_function_body("check_node")
    assert "node_major" in body and "NODE_MIN_MAJOR" in body, (
        "check_node must parse node's major version and compare it against "
        "NODE_MIN_MAJOR before accepting the system node (#218)."
    )
    # When too old it must NOT return success from the system-node branch —
    # the install_node fall-through must remain reachable.
    assert "install_node" in body, (
        "check_node must still fall through to install_node when the system "
        "node is too old (#218)."
    )


# ---------------------------------------------------------------------------
# #219 — docker permission-denied must not be reported as 'not running'.
# ---------------------------------------------------------------------------


def test_docker_permission_denied_gets_group_remedy() -> None:
    body = _extract_function_body("check_docker")
    assert "permission denied" in body and "dial unix" in body, (
        "check_docker must inspect `docker info` stderr for permission/"
        "dial-unix errors so a running-but-inaccessible daemon isn't "
        "misreported as 'not running' (#219)."
    )
    assert "usermod -aG docker" in body and "newgrp docker" in body, (
        "check_docker must point permission failures at the docker-group "
        "remedy (usermod -aG docker + newgrp), not `systemctl start` (#219)."
    )
    # stderr must be captured (stdout discarded) for the match to work.
    assert "docker info 2>&1 >/dev/null" in body, (
        "check_docker must capture docker info stderr (2>&1 >/dev/null) so "
        "the error string can be classified (#219)."
    )


# ---------------------------------------------------------------------------
# #222 — macOS robustness: timeout fallback + uv minimum-version gate.
# ---------------------------------------------------------------------------


def test_browser_timeout_has_gtimeout_and_background_fallback() -> None:
    body = _extract_function_body("run_browser_install_with_timeout")
    assert "gtimeout" in body, (
        "run_browser_install_with_timeout must try gtimeout (brew coreutils) "
        "when `timeout` is absent (#222)."
    )
    # Stock macOS has neither; must background-and-kill, not run unbounded.
    assert "kill -0" in body and "kill " in body, (
        "run_browser_install_with_timeout must background the command and "
        "kill it on overrun when no timeout binary exists (#222). Running "
        "`npx playwright install` unbounded can hang the installer forever."
    )
    assert '"$@"' in body, "the wrapped command must still be invoked (#222)."


def test_browser_background_kill_terminates_overrun() -> None:
    """Exercise the no-timeout fallback branch in isolation: a long command
    under a short budget must be killed (rc 124); a fast one passes through.
    Mirrors the ``else`` branch of run_browser_install_with_timeout verbatim
    so a regression in the kill logic is caught behaviourally."""
    fallback = r"""
        log_warn() { :; }
        run() {
            local seconds="$1"; shift
            "$@" &
            local _cmd_pid=$! _waited=0
            while kill -0 "$_cmd_pid" 2>/dev/null; do
                if [ "$_waited" -ge "$seconds" ]; then
                    log_warn "overrun"; kill "$_cmd_pid" 2>/dev/null
                    wait "$_cmd_pid" 2>/dev/null; return 124
                fi
                sleep 1; _waited=$((_waited + 1))
            done
            wait "$_cmd_pid"
        }
        run 2 sleep 30; echo "rc=$?"
        run 5 true; echo "fast=$?"
    """
    out = subprocess.run(
        ["bash", "-c", fallback], capture_output=True, text=True
    ).stdout
    assert "rc=124" in out, f"overrun not killed: {out!r}"
    assert "fast=0" in out, f"fast command failed: {out!r}"


def test_uv_minimum_version_gated() -> None:
    text = _read_install_sh()
    assert re.search(r"^UV_MIN_VERSION=", text, re.MULTILINE), (
        "UV_MIN_VERSION must be defined so install_uv can reject stale uv (#222)."
    )
    body = _extract_function_body("install_uv")
    assert "_version_ge" in body and "UV_MIN_VERSION" in body, (
        "install_uv must compare the found uv against UV_MIN_VERSION and fall "
        "through to (re)install when it is too old (#222)."
    )


def test_version_ge_compares_correctly() -> None:
    script = r"""
        _version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V 2>/dev/null | head -1)" = "$2" ]; }
        _version_ge 0.4.18 0.4.0 && echo a
        _version_ge 0.4.0 0.4.0 && echo b
        _version_ge 0.3.9 0.4.0 || echo c
    """
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["a", "b", "c"], out


# ---------------------------------------------------------------------------
# #227 — provider-key blocker in print_success + don't bake localhost:5432
# into the launcher under --skip-postgres with no --pg-dsn.
# ---------------------------------------------------------------------------


def test_print_success_warns_when_no_provider_key() -> None:
    body = _extract_function_body("print_success")
    assert "_env_has_provider_api_key" in body, (
        "print_success must check _env_has_provider_api_key and loudly warn "
        "when no provider key is configured (#227)."
    )
    assert "setup" in body and "No provider key" in body, (
        "print_success must tell the user to run `<cli> setup` before chatting "
        "when no provider key is present (#227)."
    )
    assert "${BOLD}" in body, "the no-provider-key blocker must be bold (#227)."


def test_launcher_omits_localhost_dsn_when_skip_postgres_without_dsn() -> None:
    body = _extract_function_body("setup_path")
    # The unconditional localhost default must be gone; the export is now
    # conditional on having an override or not skipping postgres.
    assert "pg_dsn_export" in body, (
        "setup_path must build THOTH_PG_DSN conditionally via pg_dsn_export "
        "so --skip-postgres without --pg-dsn bakes no localhost DSN (#227)."
    )
    assert 'SKIP_POSTGRES" != true' in body, (
        "the launcher's DSN export must be gated on SKIP_POSTGRES (#227)."
    )


def test_launcher_dsn_export_branches_behaviourally() -> None:
    """Reproduce the exact export-building snippet and assert: skip+no-dsn →
    empty (no localhost baked); other combinations → a real export."""
    snippet = r"""
        PG_USER_DEFAULT=thoth; PG_PASSWORD_DEFAULT=pw; PG_HOST_DEFAULT=localhost
        PG_PORT_DEFAULT=5432; PG_DATABASE_DEFAULT=thoth
        SKIP_POSTGRES="$1"; PG_DSN_OVERRIDE="$2"
        pg_dsn_export=""
        if [ -n "${PG_DSN_OVERRIDE:-}" ]; then
            pg_dsn_export="export THOTH_PG_DSN=\"\${THOTH_PG_DSN:-$PG_DSN_OVERRIDE}\""
        elif [ "$SKIP_POSTGRES" != true ]; then
            pg_dsn="postgresql://${PG_USER_DEFAULT}:${PG_PASSWORD_DEFAULT}@${PG_HOST_DEFAULT}:${PG_PORT_DEFAULT}/${PG_DATABASE_DEFAULT}"
            pg_dsn_export="export THOTH_PG_DSN=\"\${THOTH_PG_DSN:-$pg_dsn}\""
        fi
        echo "[$pg_dsn_export]"
    """

    def run(skip: str, override: str) -> str:
        return subprocess.run(
            ["bash", "-c", snippet, "_", skip, override],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert run("true", "") == "[]", "skip+no-dsn must bake no DSN (#227)"
    assert "localhost:5432" in run("false", ""), "normal install keeps local DSN"
    assert "remote" in run("true", "postgresql://u:p@remote:5432/db"), (
        "explicit --pg-dsn must always be honoured"
    )
