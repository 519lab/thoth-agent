"""Regression for #16746: install.sh /dev/tty gates must actually open /dev/tty.

In a Docker build, ``/dev/tty`` exists as a device node (so a bare ``-e``
existence test returns true) but opening it fails with ``ENXIO: No such
device or address``. Under the old gates the script proceeded past the "no
terminal available" skip and then crashed on the ``< /dev/tty`` redirect a
few lines later, aborting the entire image build. The fix replaces every
existence-based check that guards a subsequent ``< /dev/tty`` redirect with
an open-based probe so the skip kicks in correctly.

In the substrate-edition rewrite the gateway install moved out of
``install.sh`` (now driven by ``thoth setup``) and ``install_system_packages``
no longer reads ``/dev/tty`` directly — it delegates to ``prompt_yes_no``,
which gates on readability before redirecting. So the only function that
still both opens ``/dev/tty`` itself *and* needs the gate is
``run_setup_wizard()``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

# Functions in scripts/install.sh that read ``/dev/tty`` directly and so must
# gate on an open-based probe (not a bare ``-e`` existence test). In the
# upstream tree this list also covered ``install_system_packages`` and
# ``maybe_start_gateway``; both were refactored out of the direct-read path
# in the substrate-edition rewrite (see module docstring).
GATED_FUNCTIONS = ("run_setup_wizard",)


def _extract_function_body(name: str) -> str:
    """Return the body of ``<name>()`` as a single string.

    Anchored to ``<name>()`` and a top-of-line ``}`` so the helper keeps
    working if neighbouring functions are renamed.
    """
    text = INSTALL_SH.read_text()
    match = re.search(
        rf"^{re.escape(name)}\(\)\s*\{{\s*\n(?P<body>.*?)^\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"{name}() not found in scripts/install.sh"
    return match["body"]


@pytest.mark.parametrize("fn_name", GATED_FUNCTIONS)
def test_tty_gate_does_not_use_existence_only_check(fn_name: str) -> None:
    """The bare ``-e`` test is the bug — no spelling of it should remain."""
    body = _extract_function_body(fn_name)
    # Cover ``[ -e /dev/tty ]``, ``[ -e "/dev/tty" ]``, ``test -e /dev/tty``
    # and friends, with arbitrary surrounding whitespace.
    pattern = re.compile(
        r"""(
            \[\s*-e\s+["']?/dev/tty["']?\s*\]
            |
            \btest\s+-e\s+["']?/dev/tty["']?
        )""",
        re.VERBOSE,
    )
    match = pattern.search(body)
    assert match is None, (
        f"{fn_name} contains an existence-only check on /dev/tty "
        f"({match.group(0)!r}). Bare `-e` tests pass in Docker builds "
        "where the device node is in the mount namespace but cannot be "
        "opened (ENXIO). Use an open-based probe (e.g. "
        "`(: </dev/tty) 2>/dev/null` or `exec 3</dev/tty`) so the skip "
        "kicks in before the function tries to read from /dev/tty. "
        "See #16746."
    )


@pytest.mark.parametrize("fn_name", GATED_FUNCTIONS)
def test_tty_gate_uses_open_based_probe(fn_name: str) -> None:
    """The gate must actually attempt to open ``/dev/tty``.

    Any ``if``/``if !``/``elif`` whose condition opens ``/dev/tty`` for
    input counts: ``(: </dev/tty)``, ``exec 3</dev/tty``,
    ``{ exec 3</dev/tty; }``, etc. Asserting the higher-level invariant
    rather than a specific spelling so equivalent refactors stay green.
    """
    body = _extract_function_body(fn_name)
    gate = re.compile(
        r"^\s*(?:if|elif)\s+!?\s*[^\n]*<\s*/dev/tty[^\n]*;\s*then",
        re.MULTILINE,
    )
    assert gate.search(body), (
        f"{fn_name} must gate on an open-based probe of /dev/tty "
        "(an `if`/`if !`/`elif` whose test redirects stdin from /dev/tty), "
        "not a mere existence check. See #16746."
    )
