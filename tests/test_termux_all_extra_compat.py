"""Regression coverage for the Termux broad install profile."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_pyproject_defines_termux_all_without_known_blockers() -> None:
    text = PYPROJECT.read_text()
    assert "termux-all = [" in text
    assert '"thoth-agent[termux]"' in text
    assert '"thoth-agent[matrix]"' not in text.split("termux-all = [", 1)[1].split("]", 1)[0]
    assert '"thoth-agent[voice]"' not in text.split("termux-all = [", 1)[1].split("]", 1)[0]


def test_install_script_prefers_termux_all_then_fallbacks() -> None:
    """Termux pip install must try [termux-all] first, then [termux], then base.

    The substrate-edition rewrite condensed the verbose log lines that used
    to announce each step (``Termux broad profile failed…``); the fallback
    chain itself is the load-bearing behaviour, so pin the three pip-install
    invocations in order.
    """
    text = INSTALL_SH.read_text()
    assert "pip install -e '.[termux-all]' -c constraints-termux.txt" in text
    assert "pip install -e '.[termux]' -c constraints-termux.txt" in text
    # Base install is the final fallback (.).
    assert re.search(
        r"pip install -e '\.' -c constraints-termux\.txt", text
    ), "Expected base install (`.`) as the final Termux pip fallback."

    # Order matters: [termux-all] → [termux] → base. The script may use any
    # control-flow style (if/elif, ||-chain) so locate each invocation and
    # assert they appear in this sequence.
    idx_all = text.index("pip install -e '.[termux-all]' -c constraints-termux.txt")
    idx_termux = text.index("pip install -e '.[termux]' -c constraints-termux.txt")
    idx_base = text.index("pip install -e '.' -c constraints-termux.txt")
    assert idx_all < idx_termux < idx_base, (
        "Termux pip fallback chain must be [termux-all] → [termux] → base; "
        f"got order ({idx_all}, {idx_termux}, {idx_base})."
    )
