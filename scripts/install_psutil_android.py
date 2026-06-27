#!/usr/bin/env python3
"""Install psutil on Termux/Android by patching upstream platform detection.

psutil's setup currently gates Linux sources behind
``sys.platform.startswith('linux')``. On Termux, Python reports
``sys.platform == 'android'``, so ``pip install psutil`` aborts with
"platform android is not supported" — even though psutil compiles fine
when the Linux source path is reused.

This script downloads the official psutil sdist, applies a one-line
patch (``LINUX = sys.platform.startswith(("linux", "android"))``), and
installs the patched tree with ``pip install --no-build-isolation``.

Usage:
    python scripts/install_psutil_android.py [--pip "/path/to/pip"] [--uv]

When neither flag is given, the script auto-detects ``uv`` on PATH and
falls back to ``<sys.executable> -m pip``.

This is a stopgap. Remove once psutil upstream merges
https://github.com/giampaolo/psutil/pull/2762 and ships a release.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Pin a version we know patches cleanly. Update when a newer psutil
# changes the marker line shape and we need to follow upstream. The download
# URL is resolved from the PyPI JSON API at runtime so we don't carry a
# hardcoded files.pythonhosted.org hash path that rots on re-upload.
PSUTIL_VERSION = "7.2.2"
PYPI_JSON_URL = f"https://pypi.org/pypi/psutil/{PSUTIL_VERSION}/json"

MARKER = 'LINUX = sys.platform.startswith("linux")'
REPLACEMENT = 'LINUX = sys.platform.startswith(("linux", "android"))'


def _resolve_sdist_url() -> str:
    """Resolve the psutil sdist (.tar.gz) download URL via the PyPI JSON API.

    A hardcoded files.pythonhosted.org path breaks whenever PyPI changes the
    hashed upload location; the JSON API always returns the current URL for the
    pinned version.
    """
    try:
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:
        sys.exit(f"Failed to query PyPI for psutil {PSUTIL_VERSION}: {exc}")
    for entry in data.get("urls", []):
        url = entry.get("url", "")
        if entry.get("packagetype") == "sdist" and url.endswith(".tar.gz"):
            return url
    sys.exit(f"No sdist (.tar.gz) found on PyPI for psutil {PSUTIL_VERSION}")


def _resolve_install_cmd(pip_arg: str | None, prefer_uv: bool) -> list[str]:
    if pip_arg:
        return pip_arg.split()
    if prefer_uv:
        uv = shutil.which("uv")
        if not uv:
            sys.exit("--uv requested but no uv on PATH")
        return [uv, "pip"]
    auto_uv = shutil.which("uv")
    if auto_uv:
        return [auto_uv, "pip"]
    return [sys.executable, "-m", "pip"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pip",
        help="Explicit installer command (e.g. '/usr/bin/uv pip' or 'python -m pip')",
    )
    parser.add_argument(
        "--uv",
        action="store_true",
        help="Force using uv (errors out if uv is not on PATH)",
    )
    args = parser.parse_args()

    install_cmd_prefix = _resolve_install_cmd(args.pip, args.uv)

    print(
        "→ Termux/Android: prebuilding psutil with Linux source path "
        "compatibility shim (see psutil#2762)..."
    )

    sdist_url = _resolve_sdist_url()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "psutil.tar.gz"
        try:
            urllib.request.urlretrieve(sdist_url, archive)
        except Exception as exc:
            sys.exit(f"Failed to download psutil sdist from {sdist_url}: {exc}")
        with tarfile.open(archive) as tar:
            # filter="data" rejects unsafe members (absolute paths, traversal,
            # special files) — the secure default landing in Python 3.14.
            tar.extractall(tmp_path, filter="data")

        try:
            src_root = next(
                p for p in tmp_path.iterdir()
                if p.is_dir() and p.name.startswith("psutil-")
            )
        except StopIteration:
            sys.exit("psutil sdist did not contain a psutil-* directory")

        common_py = src_root / "psutil" / "_common.py"
        content = common_py.read_text(encoding="utf-8")
        if MARKER not in content:
            sys.exit(
                "psutil Android compatibility patch marker not found — "
                "upstream may have changed the LINUX detection line. "
                "Update MARKER/REPLACEMENT in this script."
            )
        common_py.write_text(content.replace(MARKER, REPLACEMENT), encoding="utf-8")

        cmd = install_cmd_prefix + ["install", "--no-build-isolation", str(src_root)]
        print(f"  $ {' '.join(cmd)}")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            # Fatal on Termux/Android: psutil backs the agent's process/memory
            # tooling, so a silent skip would leave a broken install. Surface a
            # clear message and propagate the failing exit code.
            sys.exit(
                "✗ psutil install failed on Termux/Android (exit code "
                f"{result.returncode}). psutil is required for the agent's "
                "process and memory tooling — the install cannot continue "
                "without it."
            )

    print("✓ psutil installed via Android compatibility shim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
