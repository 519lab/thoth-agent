"""Regression for #21454: re-running install.sh on a symlinked prior install.

Older versions of ``install.sh`` created ``$link_dir/$CLI_NAME`` as a symlink
to the pip-generated entry point at ``$HERMES_BIN`` (i.e. ``venv/bin/hermes``).
When ``setup_path()`` later switched to writing a bash shim with
``cat > "$link_dir/$CLI_NAME" <<EOF``, the redirect followed the existing
symlink and overwrote the pip entry point with the shim. The shim's
``exec "$HERMES_BIN" "$@"`` then self-recursed and the CLI hung on every
invocation.

These tests pin the fix: ``setup_path()`` must remove ``$link_dir/$CLI_NAME``
before writing through the redirect, so the shim is created as a regular file
in ``link_dir`` and the venv entry point is left intact.

(The substrate-edition rewrite renamed ``command_link_dir`` → ``link_dir`` and
made the CLI name configurable via ``$CLI_NAME``; both before and after the
rewrite the regression class is identical, so this module pins the fix
through whichever variable names the script currently uses.)
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _extract_setup_path_shim_block() -> str:
    """Return the install.sh shim-write block used by setup_path().

    Anchored on the ``mkdir -p "$link_dir"`` … ``chmod +x "$link_dir/$CLI_NAME"``
    span so it keeps working regardless of intermediate refactors.
    """
    text = INSTALL_SH.read_text()
    match = re.search(
        r"(?P<block>mkdir -p \"\$link_dir\".*?chmod \+x \"\$link_dir/\$CLI_NAME\")",
        text,
        re.DOTALL,
    )
    assert match is not None, (
        "Could not locate the setup_path shim-write block in scripts/install.sh "
        "(looked for `mkdir -p \"$link_dir\"` … `chmod +x \"$link_dir/$CLI_NAME\"`)."
    )
    return match["block"]


def test_setup_path_shim_block_removes_old_link_before_writing() -> None:
    """Static guard: the rm must precede the cat heredoc, not follow it."""
    block = _extract_setup_path_shim_block()
    rm_idx = block.find('rm -f "$link_dir/$CLI_NAME"')
    cat_idx = block.find('cat > "$link_dir/$CLI_NAME" <<EOF')
    assert rm_idx != -1, (
        "setup_path() must `rm -f` $link_dir/$CLI_NAME before the "
        "`cat >` heredoc, otherwise an existing symlink (left by older "
        "installs) will be followed and the pip entry point overwritten. "
        "See #21454."
    )
    assert cat_idx != -1, "expected `cat >` heredoc still present"
    assert rm_idx < cat_idx, (
        "`rm -f` must come *before* the `cat >` heredoc, not after."
    )


def test_re_running_setup_path_block_preserves_pip_entry_point(tmp_path: Path) -> None:
    """Behavioral repro: simulate prior-install symlink + new-install heredoc.

    Layout mirrors a real install:

        tmp/
          venv/bin/hermes        <- pip entry point (the one we must preserve)
          local_bin/hermes       <- symlink → ../venv/bin/hermes  (old install)

    Then we run the exact shim-write block from setup_path() with
    ``HERMES_BIN``, ``link_dir``, and ``CLI_NAME`` pointed at this fixture.
    The fix requires that, after the run:

      * ``venv/bin/hermes`` still contains its original pip-script body
      * ``local_bin/hermes`` is a regular file (not a symlink) holding the shim
    """
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    pip_entry = venv_bin / "hermes"
    pip_marker = "#!/usr/bin/env python\n# pip-generated entry point — must not be overwritten\n"
    pip_entry.write_text(pip_marker)
    pip_entry.chmod(pip_entry.stat().st_mode | stat.S_IXUSR)

    link_dir = tmp_path / "local_bin"
    link_dir.mkdir()
    cli_name = "hermes"
    shim_path = link_dir / cli_name
    # Reproduce the prior-install state: shim path is a symlink to the
    # pip-generated entry point.
    shim_path.symlink_to(pip_entry)
    assert shim_path.is_symlink()

    block = _extract_setup_path_shim_block()
    # Drive the block with the real env vars setup_path() sets. The extracted
    # block now contains a `local pg_dsn=...` line between the rm and the cat
    # (substrate-edition added HERMES_PG_DSN injection to the launcher), so
    # wrap the block in a function — `local` is illegal at script scope.
    script = (
        "set -e\n"
        f"HERMES_BIN={pip_entry!s}\n"
        f"HERMES_HOME={tmp_path!s}/thoth_home\n"
        "_run_shim_block() {\n"
        f"  local link_dir={link_dir!s}\n"
        f"  local CLI_NAME={cli_name}\n"
        f"{block}\n"
        "}\n"
        "_run_shim_block\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, (
        f"shim-write block failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # The pip entry point must still be the original pip script — not a
    # re-written self-recursing bash shim.
    assert pip_entry.read_text() == pip_marker, (
        "venv/bin/hermes was overwritten by setup_path() — symlink-stomp "
        "regression (#21454)."
    )

    # The shim path itself must now be a regular file holding the launcher.
    assert shim_path.exists()
    assert not shim_path.is_symlink(), (
        "link_dir/<CLI_NAME> must be replaced with a regular file, not "
        "left as a symlink — otherwise the next install will stomp again."
    )
    shim_text = shim_path.read_text()
    assert "unset PYTHONPATH" in shim_text
    assert "unset PYTHONHOME" in shim_text
    assert f'exec "{pip_entry}"' in shim_text
    shim_mode = shim_path.stat().st_mode
    assert shim_mode & stat.S_IXUSR, "shim must be user-executable"
