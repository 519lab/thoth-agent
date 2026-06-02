"""Regression tests for install.sh browser setup.

Browser automation is optional. The installer should not leave Thoth
half-installed just because Playwright's managed Chromium download hangs on an
unsupported distribution.
"""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_install_script_skips_playwright_download_when_system_browser_exists() -> None:
    text = INSTALL_SH.read_text()

    assert "find_system_browser()" in text
    assert "google-chrome google-chrome-stable chromium chromium-browser chrome" in text
    # When a system Chrome/Chromium is found, the installer must not also kick
    # off Playwright's managed Chromium download. The log message phrasing has
    # changed across rewrites; pin behaviour via the "skipping Chromium
    # download" phrase rather than the exact log line.
    assert re.search(r"skipping Chromium download", text), (
        "Expected install.sh to announce skipping the Chromium download when "
        "a system browser is detected."
    )


def test_install_script_persists_system_browser_for_agent_browser() -> None:
    text = INSTALL_SH.read_text()

    assert "configure_browser_env_from_system_browser()" in text
    # The detected browser path must be persisted to .env as
    # AGENT_BROWSER_EXECUTABLE_PATH. The exact write mechanism (raw redirect
    # vs printf format string) is incidental — assert the variable name is
    # written with the browser_path value.
    assert re.search(r"AGENT_BROWSER_EXECUTABLE_PATH=(\$browser_path|%s)", text), (
        "Expected install.sh to persist AGENT_BROWSER_EXECUTABLE_PATH using "
        "$browser_path (raw) or %s (printf format)."
    )


def test_playwright_installs_are_timeout_guarded() -> None:
    text = INSTALL_SH.read_text()

    assert "run_browser_install_with_timeout()" in text
    assert "run_browser_install_with_timeout 600 npx playwright install chromium" in text
    # --with-deps is still invoked on apt-based systems, but only when sudo
    # is available non-interactively (root or passwordless sudo). Non-sudo
    # service users fall back to the browser-only install — see
    # install_node_deps() in install.sh.
    assert "run_browser_install_with_timeout 600 npx playwright install --with-deps chromium" in text


def test_install_script_supports_skip_browser_flag() -> None:
    """--skip-browser (and --no-playwright alias) skips the Playwright install."""
    text = INSTALL_SH.read_text()

    assert "--skip-browser|--no-playwright)" in text
    assert "SKIP_BROWSER=true" in text
    assert 'if [ "$SKIP_BROWSER" = true ]; then' in text
    # Help text formatting (spacing between flag and description) is
    # incidental — match the flag and its purpose with a regex.
    assert re.search(r"--skip-browser\s+Skip Playwright/Chromium install", text), (
        "Expected --skip-browser to be documented in the help text."
    )


def test_install_script_skips_with_deps_when_no_sudo() -> None:
    """Non-sudo users on apt distros must not block on an interactive sudo prompt."""
    text = INSTALL_SH.read_text()

    # The apt branch must gate --with-deps behind a sudo capability check
    # (root or non-interactive sudo), otherwise the installer hangs for
    # service-user installs (systemd accounts, operator users, etc.).
    assert 'if [ "$(id -u)" -eq 0 ] || (command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null); then' in text
    assert "sudo npx playwright install-deps chromium" in text
