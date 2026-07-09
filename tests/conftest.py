"""Shared fixtures for the thoth-agent test suite.

Hermetic-test invariants enforced here (see CLAUDE.md "Commands → Tests" for rationale):

1. **No credential env vars.** All provider/credential-shaped env vars
   (ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD, _CREDENTIALS, etc.)
   are unset before every test. Local developer keys cannot leak in.
2. **Isolated THOTH_HOME.** THOTH_HOME points to a per-test tempdir so
   code reading ``~/.thoth/*`` via ``get_thoth_home()`` can't see the
   real one. (We do NOT also redirect HOME — that broke subprocesses in
   CI. Code using ``Path.home() / ".thoth"`` instead of the canonical
   ``get_thoth_home()`` is a bug to fix at the callsite.)
3. **Deterministic runtime.** TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0.
4. **No THOTH_SESSION_* inheritance** — the agent's current gateway
   session must not leak into tests.

These invariants make the local test run match CI closely. Gaps that
remain (CPU count, xdist worker count) are addressed by the canonical
test runner at ``scripts/run_tests.sh``.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

logger = logging.getLogger(__name__)

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Per-file process isolation ──────────────────────────────────────────────
# Tests run via ``scripts/run_tests_parallel.py``, which spawns a fresh
# ``python -m pytest <file>`` subprocess per test file. Cross-file state
# leakage (module-level dicts, ContextVars, caches) is impossible: each
# file gets a clean Python interpreter. Intra-file ordering is the test
# author's responsibility — if test A in foo.py mutates state that test B
# in foo.py reads, that's a real bug to fix in the file (it would also
# bite anyone running ``pytest tests/foo.py`` directly).
#
# This replaces the historic _reset_module_state autouse fixture (manual
# state clearing) and the brief experiment with subprocess-per-test
# isolation (too slow at ~17k tests).
#
# See ``scripts/run_tests_parallel.py`` for the runner.


# ── Credential env-var filter ──────────────────────────────────────────────
#
# Any env var in the current process matching ONE of these patterns is
# unset for every test. Developers' local keys cannot leak into assertions
# about "auto-detect provider when key present".

_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_SECRET_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_OAUTH_TOKEN",
    "_WEBHOOK_SECRET",
    "_ENCRYPT_KEY",
    "_APP_SECRET",
    "_CLIENT_SECRET",
    "_CORP_SECRET",
    "_AES_KEY",
)

# Explicit names (for ones that don't fit the suffix pattern)
_CREDENTIAL_NAMES = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_TOKEN",
    "FAL_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "NOUS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "MINIMAX_API_KEY",
    "OLLAMA_API_KEY",
    "OPENVIKING_API_KEY",
    "COPILOT_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "BROWSERBASE_API_KEY",
    "FIRECRAWL_API_KEY",
    "PARALLEL_API_KEY",
    "EXA_API_KEY",
    "TAVILY_API_KEY",
    "WANDB_API_KEY",
    "ELEVENLABS_API_KEY",
    "HONCHO_API_KEY",
    "MEM0_API_KEY",
    "SUPERMEMORY_API_KEY",
    "RETAINDB_API_KEY",
    "HINDSIGHT_API_KEY",
    "HINDSIGHT_LLM_API_KEY",
    "DAYTONA_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "MATTERMOST_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_PASSWORD",
    "MATRIX_RECOVERY_KEY",
    "HASS_TOKEN",
    "EMAIL_PASSWORD",
    "BLUEBUBBLES_PASSWORD",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "TERMINAL_SSH_KEY",
    "SUDO_PASSWORD",
    "GATEWAY_PROXY_KEY",
    "API_SERVER_KEY",
    "TOOL_GATEWAY_USER_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_SECRET",
    "AI_GATEWAY_API_KEY",
    "VOICE_TOOLS_OPENAI_KEY",
    "BROWSER_USE_API_KEY",
    "CUSTOM_API_KEY",
    "GATEWAY_PROXY_URL",
    "GEMINI_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "OLLAMA_BASE_URL",
    "GROQ_BASE_URL",
    "XAI_BASE_URL",
    "AI_GATEWAY_BASE_URL",
    "ANTHROPIC_BASE_URL",
})


def _looks_like_credential(name: str) -> bool:
    """True if env var name matches a credential-shaped pattern."""
    if name in _CREDENTIAL_NAMES:
        return True
    return any(name.endswith(suf) for suf in _CREDENTIAL_SUFFIXES)


# THOTH_* vars that change test behavior by being set. Unset all of these
# unconditionally — individual tests that need them set do so explicitly.
_THOTH_BEHAVIORAL_VARS = frozenset({
    "THOTH_YOLO_MODE",
    "THOTH_INTERACTIVE",
    "THOTH_QUIET",
    "THOTH_TOOL_PROGRESS",
    "THOTH_TOOL_PROGRESS_MODE",
    "THOTH_MAX_ITERATIONS",
    "THOTH_SESSION_PLATFORM",
    "THOTH_SESSION_CHAT_ID",
    "THOTH_SESSION_CHAT_NAME",
    "THOTH_SESSION_THREAD_ID",
    "THOTH_SESSION_SOURCE",
    "THOTH_SESSION_KEY",
    "THOTH_GATEWAY_SESSION",
    "THOTH_PLATFORM",
    "THOTH_MODEL",
    "THOTH_INFERENCE_MODEL",
    "THOTH_INFERENCE_PROVIDER",
    "THOTH_TUI_PROVIDER",
    "THOTH_MANAGED",
    "THOTH_DEV",
    "THOTH_CONTAINER",
    "THOTH_EPHEMERAL_SYSTEM_PROMPT",
    "THOTH_TIMEZONE",
    "THOTH_REDACT_SECRETS",
    "THOTH_BACKGROUND_NOTIFICATIONS",
    "THOTH_EXEC_ASK",
    "THOTH_HOME_MODE",
    "THOTH_AGENT_USE_LEGACY_SESSION_KEYS",
    # Set by the launcher shim to the invoked command name (e.g.
    # thoth-substrate). Cleared so cli_name() deterministically returns the
    # "thoth" default and the many tests asserting literal "thoth …" hints
    # don't depend on the ambient launcher name.
    "THOTH_CLI_NAME",
    # Kanban path/board pins must never leak from a developer shell or
    # dispatched worker into tests; otherwise tests can write fake tasks to
    # the real ~/.thoth/kanban.db instead of the per-test THOTH_HOME.
    "THOTH_KANBAN_DB",
    "THOTH_KANBAN_BOARD",
    "THOTH_KANBAN_HOME",
    "THOTH_KANBAN_WORKSPACES_ROOT",
    "THOTH_KANBAN_LOGS_ROOT",
    "THOTH_KANBAN_TASK",
    "THOTH_KANBAN_WORKSPACE",
    "THOTH_KANBAN_RUN_ID",
    "THOTH_KANBAN_CLAIM_LOCK",
    "THOTH_KANBAN_DISPATCH_IN_GATEWAY",
    "THOTH_TENANT",
    "TERMINAL_CWD",
    "TERMINAL_ENV",
    "TERMINAL_VERCEL_RUNTIME",
    "TERMINAL_CONTAINER_CPU",
    "TERMINAL_CONTAINER_DISK",
    "TERMINAL_CONTAINER_MEMORY",
    "TERMINAL_CONTAINER_PERSISTENT",
    "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "BROWSER_CDP_URL",
    "CAMOFOX_URL",
    # Platform allowlists — not credentials, but if set from any source
    # (user shell, earlier leaky test, CI env), they change gateway auth
    # behavior and flake button-authorization tests.
    "TELEGRAM_ALLOWED_USERS",
    "DISCORD_ALLOWED_USERS",
    "WHATSAPP_ALLOWED_USERS",
    "SLACK_ALLOWED_USERS",
    "SIGNAL_ALLOWED_USERS",
    "SIGNAL_GROUP_ALLOWED_USERS",
    "EMAIL_ALLOWED_USERS",
    "SMS_ALLOWED_USERS",
    "MATTERMOST_ALLOWED_USERS",
    "MATRIX_ALLOWED_USERS",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "DISCORD_ALLOW_ALL_USERS",
    "WHATSAPP_ALLOW_ALL_USERS",
    "SLACK_ALLOW_ALL_USERS",
    "SIGNAL_ALLOW_ALL_USERS",
    "EMAIL_ALLOW_ALL_USERS",
    "SMS_ALLOW_ALL_USERS",
    # Gateway home channels are set by /sethome in real profiles. Tests that
    # exercise dashboard notification toggles must opt in explicitly or they
    # can accidentally subscribe against a developer's real home channel.
    "TELEGRAM_HOME_CHANNEL",
    "TELEGRAM_HOME_CHANNEL_THREAD_ID",
    "TELEGRAM_HOME_CHANNEL_NAME",
    "TELEGRAM_CRON_THREAD_ID",
    "DISCORD_HOME_CHANNEL",
    "DISCORD_HOME_CHANNEL_THREAD_ID",
    "DISCORD_HOME_CHANNEL_NAME",
    "SLACK_HOME_CHANNEL",
    "SLACK_HOME_CHANNEL_THREAD_ID",
    "SLACK_HOME_CHANNEL_NAME",
    "WHATSAPP_HOME_CHANNEL",
    "WHATSAPP_HOME_CHANNEL_THREAD_ID",
    "WHATSAPP_HOME_CHANNEL_NAME",
    "SIGNAL_HOME_CHANNEL",
    "SIGNAL_HOME_CHANNEL_THREAD_ID",
    "SIGNAL_HOME_CHANNEL_NAME",
    "EMAIL_HOME_CHANNEL",
    "EMAIL_HOME_CHANNEL_THREAD_ID",
    "EMAIL_HOME_CHANNEL_NAME",
    "SMS_HOME_CHANNEL",
    "SMS_HOME_CHANNEL_THREAD_ID",
    "SMS_HOME_CHANNEL_NAME",
    "MATTERMOST_HOME_CHANNEL",
    "MATTERMOST_HOME_CHANNEL_THREAD_ID",
    "MATTERMOST_HOME_CHANNEL_NAME",
    "MATRIX_HOME_CHANNEL",
    "MATRIX_HOME_CHANNEL_THREAD_ID",
    "MATRIX_HOME_CHANNEL_NAME",
    # Platform gating — set by load_gateway_config() as a side effect when
    # a config.yaml is present, so individual test bodies that call the
    # loader leak these values into later tests in the same process.
    # Force-clear on every test setup so the leak can't happen.
    "SLACK_REQUIRE_MENTION",
    "SLACK_STRICT_MENTION",
    "SLACK_FREE_RESPONSE_CHANNELS",
    "SLACK_ALLOW_BOTS",
    "SLACK_REACTIONS",
    "DISCORD_REQUIRE_MENTION",
    "DISCORD_FREE_RESPONSE_CHANNELS",
    "TELEGRAM_REQUIRE_MENTION",
    "WHATSAPP_REQUIRE_MENTION",
    "MATRIX_REQUIRE_MENTION",
})


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path, monkeypatch):
    """Blank out all credential/behavioral env vars so local and CI match.

    Also redirects HOME and THOTH_HOME to per-test tempdirs so code that
    reads ``~/.thoth/*`` can't touch the real one, and pins TZ/LANG so
    datetime/locale-sensitive tests are deterministic.
    """
    # 1. Blank every credential-shaped env var that's currently set.
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    # 2. Blank behavioral THOTH_* vars that could change test semantics.
    for name in _THOTH_BEHAVIORAL_VARS:
        monkeypatch.delenv(name, raising=False)

    # 2b. Clear any THOTH_* left in the real os.environ from an earlier test
    #     (monkeypatch won't revert vars written directly into os.environ), so
    #     the per-test THOTH_HOME below is authoritative — EXCEPT the DB infra
    #     vars the CI/job env legitimately provides (THOTH_PG_DSN etc.). A
    #     blanket sweep would wipe the CI-provided DSN and break any test
    #     relying on it, so keep the THOTH_PG_/THOTH_TEST_ families.
    _KEEP = ("THOTH_PG_", "THOTH_TEST_")
    for name in list(os.environ.keys()):
        if name.startswith("THOTH_") and not name.startswith(_KEEP):
            monkeypatch.delenv(name, raising=False)

    # 3. Redirect THOTH_HOME to a per-test tempdir. Code that reads
    #    ``~/.thoth/*`` via ``get_thoth_home()`` now gets the tempdir.
    #
    #    NOTE: We do NOT also redirect HOME. Doing so broke CI because
    #    some tests (and their transitive deps) spawn subprocesses that
    #    inherit HOME and expect it to be stable. If a test genuinely
    #    needs HOME isolated, it should set it explicitly in its own
    #    fixture. Any code in the codebase reading ``~/.thoth/*`` via
    #    ``Path.home() / ".thoth"`` instead of ``get_thoth_home()``
    #    is a bug to fix at the callsite.
    fake_thoth_home = tmp_path / "thoth_test"
    fake_thoth_home.mkdir()
    (fake_thoth_home / "sessions").mkdir()
    (fake_thoth_home / "cron").mkdir()
    (fake_thoth_home / "memories").mkdir()
    (fake_thoth_home / "skills").mkdir()
    monkeypatch.setenv("THOTH_HOME", str(fake_thoth_home))

    # 4. Deterministic locale / timezone / hashseed. CI runs in UTC with
    #    C.UTF-8 locale; local dev often doesn't. Pin everything.
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    # 4b. Disable AWS IMDS lookups. Without this, any test that ends up
    #     calling has_aws_credentials() / resolve_aws_auth_env_var()
    #     (e.g. provider auto-detect, status command, cron run_job) burns
    #     ~2s waiting for the metadata service at 169.254.169.254 to time
    #     out. Tests don't run on EC2 — IMDS is always unreachable here.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")

    # 5. Reset plugin singleton so tests don't leak plugins from
    #    ~/.thoth/plugins/ (which, per step 3, is now empty — but the
    #    singleton might still be cached from a previous test).
    try:
        import thoth_cli.plugins as _plugins_mod
        monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
    except Exception:
        pass
    # Explicitly clear provider-specific base URL overrides that don't match
    # the generic credential-shaped env-var filter above.
    monkeypatch.delenv("GMI_API_KEY", raising=False)
    monkeypatch.delenv("GMI_BASE_URL", raising=False)


# Backward-compat alias — old tests reference this fixture name. Keep it
# as a no-op wrapper so imports don't break.
@pytest.fixture(autouse=True)
def _isolate_thoth_home(_hermetic_environment):
    """Alias preserved for any test that yields this name explicitly."""
    return None


# ── Module-level state reset — replaced by per-file process isolation ──────
#
# Each test FILE runs in a freshly-spawned ``python -m pytest <file>``
# subprocess via ``scripts/run_tests_parallel.py``, so module-level dicts /
# sets / ContextVars from tests in one file cannot leak into tests in
# another file. No manual per-module clearing needed.
#
# Within a single file, ordering is the author's responsibility. If your
# tests in the same file share mutable state, either reset it explicitly
# in a fixture or split them across files.
#
# The skill ``test-suite-cascade-diagnosis`` documents the cascade patterns
# this replaces; the running example was ``test_command_guards`` failing
# 12/15 CI runs because ``tools.approval._session_approved`` carried
# approvals from one test's session into another's.


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal thoth config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Per-test timeout — handled by the isolation plugin ─────────────────────
#
# The subprocess-per-test plugin enforces the configured ``isolate_timeout``
# ini key by terminating the child if it overruns. The old SIGALRM-based
# fixture (POSIX-only, didn't work on Windows) is gone.


@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.

    On Python 3.12+, ``asyncio.get_event_loop_policy().get_event_loop()`` with no
    *running* loop emits DeprecationWarning; skip that path and install a fresh
    loop via ``new_event_loop()`` instead.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    if loop is None and sys.version_info < (3, 12):
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


# ── Live-system guard ──────────────────────────────────────────────────────
#
# Several test files exercise the gateway-restart / kill code paths
# (``cmd_update``, ``kill_gateway_processes``, ``stop_profile_gateway``).
# When a single test forgets to mock either ``os.kill`` or the global
# ``find_gateway_pids`` helper, the real call leaks out of the hermetic
# environment and finds the developer's live ``thoth-gateway`` process
# via ``psutil`` — sending it SIGTERM mid-test. The shutdown forensics in
# PR #23285 caught this happening 5+ times in 3 days, every time
# correlated with a ``tests/thoth_cli/`` pytest run starting up.
#
# This fixture makes the leak impossible by intercepting the two
# primitives that actually do damage:
#
#  • ``os.kill`` rejects any PID outside the test process subtree with
#    a hard ``RuntimeError`` so the offending test gets a stack trace
#    instead of silently murdering the real gateway.
#  • ``subprocess.run`` / ``subprocess.Popen`` / ``call`` / ``check_call`` /
#    ``check_output`` reject any ``systemctl ... <verb> thoth-gateway``
#    invocation that would mutate the live unit. Read-only systemctl
#    calls (``status``, ``show``, ``list-units``) still pass through.
#
# We intentionally do NOT stub ``find_gateway_pids`` / ``_scan_gateway_pids``
# here — tests of those functions themselves need the real implementation.
# Even if a test gets the live gateway PID back from a real scan, the
# ``os.kill`` guard above catches the actual signal call, and the
# ``systemctl`` guard catches the systemd path. Discovery without
# delivery is harmless.

_LIVE_SYSTEM_GUARD_BYPASS_MARK = "live_system_guard_bypass"


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register markers used by hermetic conftest."""
    config.addinivalue_line(
        "markers",
        f"{_LIVE_SYSTEM_GUARD_BYPASS_MARK}: bypass the live-system guard "
        "(only for tests that genuinely need real os.kill / subprocess "
        "behaviour — e.g. PTY tests that signal their own child).",
    )


@pytest.fixture(autouse=True)
def _live_system_guard(request, monkeypatch):
    """Block real os.kill / systemctl / gateway-pid scans during tests.

    See block comment above for the why. Tests that genuinely need
    real signal delivery (e.g. PTY tests that SIGINT their own child)
    can opt out with ``@pytest.mark.live_system_guard_bypass``.

    Coverage (every primitive that can deliver a signal to or otherwise
    terminate a foreign process):
      • os.kill, os.killpg (POSIX)
      • subprocess.run / Popen / call / check_call / check_output
      • subprocess.getoutput / getstatusoutput
      • os.system / os.popen
      • pty.spawn
      • asyncio.create_subprocess_exec / create_subprocess_shell
    Subprocess inspection looks at the WHOLE command string (not just
    tokens[0]), so ``bash -c "systemctl restart thoth-gateway"``,
    ``sudo systemctl ...``, ``env systemctl ...``, ``setsid systemctl ...``
    are all caught. ``pkill``/``killall``/``taskkill`` invocations
    targeting thoth/python patterns are also blocked.
    """
    if request.node.get_closest_marker(_LIVE_SYSTEM_GUARD_BYPASS_MARK):
        yield
        return

    import os as _os
    import shlex as _shlex
    import subprocess as _subprocess

    test_pid = _os.getpid()
    # Capture the test process's existing children at fixture start —
    # any *new* children spawned by the test are also allowlisted via
    # the live psutil walk below. Static set keeps the fast path cheap.
    try:
        import psutil as _psutil
        _initial_children = {
            c.pid for c in _psutil.Process(test_pid).children(recursive=True)
        }
    except Exception:
        _psutil = None
        _initial_children = set()

    def _is_own_subtree(pid: int) -> bool:
        # PID 0 means "our own process group"; -1 means "every process we
        # can signal". Both are dangerous when paired with SIGTERM/SIGKILL,
        # but pid 0 is technically scoped to our group so allow it; pid -1
        # is treated as foreign (refuse).
        if pid == 0:
            return True
        if pid < 0:
            return False
        if pid == test_pid or pid in _initial_children:
            return True
        if _psutil is None:
            return False
        try:
            walker = _psutil.Process(pid)
        except Exception:
            # Stale PID — kill would be a no-op anyway, allow it.
            return True
        try:
            for parent in walker.parents():
                if parent.pid == test_pid:
                    return True
        except Exception:
            return False
        return False

    real_kill = _os.kill

    def _guarded_kill(pid, sig, *args, **kwargs):
        if _is_own_subtree(int(pid)):
            return real_kill(pid, sig, *args, **kwargs)
        raise RuntimeError(
            f"tests/conftest.py live-system guard: blocked os.kill("
            f"{pid}, {sig}) — PID is outside the test process subtree. "
            "If this fired in CI it means the test reached a real "
            "kill_gateway_processes / stop_profile_gateway / cmd_update "
            "code path without mocking find_gateway_pids and os.kill. "
            "Mock both, or mark the test with "
            "@pytest.mark.live_system_guard_bypass if real signal "
            "delivery is genuinely required."
        )

    monkeypatch.setattr(_os, "kill", _guarded_kill)

    # ``os.killpg`` is the same risk class — sends a signal to every
    # process in a group. The gateway is a session leader (its own
    # PGID == its PID), so killpg(gateway_pid, SIGTERM) is a one-shot
    # kill of the live process. Allow it only when the target PGID is
    # the test process's own group.
    if hasattr(_os, "killpg"):
        real_killpg = _os.killpg
        own_pgid = _os.getpgrp()

        def _guarded_killpg(pgid, sig, *args, **kwargs):
            if int(pgid) == own_pgid or _is_own_subtree(int(pgid)):
                return real_killpg(pgid, sig, *args, **kwargs)
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"os.killpg({pgid}, {sig}) — PGID is outside the test "
                "process group. See _live_system_guard for the why."
            )

        monkeypatch.setattr(_os, "killpg", _guarded_killpg)

    # ── Subprocess command-string inspection (whole-line) ──────────
    _THOTH_TOKENS = (
        "thoth-gateway",
        "thoth.service",
        "thoth_cli.main gateway",
        "thoth_cli/main.py gateway",
        "gateway/run.py",
        "thoth gateway",
    )
    _MUTATING_VERBS = (
        "restart", "start", "stop", "kill", "reload",
        "reset-failed", "enable", "disable", "mask", "unmask",
        "daemon-reload", "try-restart", "reload-or-restart",
    )
    _PROCESS_KILLERS = ("pkill", "killall", "taskkill", "skill", "fuser")

    def _cmd_to_string(cmd) -> str:
        if cmd is None:
            return ""
        if isinstance(cmd, (bytes, bytearray)):
            try:
                return bytes(cmd).decode(errors="replace")
            except Exception:
                return ""
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, (list, tuple)):
            try:
                return " ".join(str(t) for t in cmd)
            except Exception:
                return ""
        return str(cmd)

    def _matches_thoth_gateway(cmd_str: str) -> bool:
        low = cmd_str.lower()
        return any(tok in low for tok in _THOTH_TOKENS)

    def _is_blocked_systemctl(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        if "systemctl" not in cmd_str:
            return False
        if not _matches_thoth_gateway(cmd_str):
            return False
        try:
            tokens = _shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        return any(verb in tokens for verb in _MUTATING_VERBS)

    def _is_process_killer(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        try:
            tokens = _shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        if not tokens:
            return False
        for tok in tokens:
            head = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if head in _PROCESS_KILLERS:
                low = cmd_str.lower()
                # pkill -f pattern: catch thoth-themed patterns + a
                # plain "python" -f which would catch the live gateway
                # whose cmdline contains "python -m thoth_cli.main".
                if (
                    "thoth" in low
                    or "gateway" in low
                    or ("python" in low and "-f" in tokens)
                ):
                    return True
        return False

    def _check_subprocess_cmd(name, cmd):
        if _is_blocked_systemctl(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — would mutate the "
                "live thoth-gateway systemd unit. Mock "
                "subprocess.run / _run_systemctl in the test, or "
                "mark with @pytest.mark.live_system_guard_bypass."
            )
        if _is_process_killer(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — process-killer command "
                "targeting thoth/python could hit the live gateway. "
                "Mark with @pytest.mark.live_system_guard_bypass if "
                "intentional."
            )

    def _wrap_subprocess(name, real):
        def _guarded(cmd, *args, **kwargs):
            _check_subprocess_cmd(name, cmd)
            return real(cmd, *args, **kwargs)
        _guarded.__name__ = f"_guarded_{name}"
        # Make the wrapper subscriptable like the wrapped callable when
        # the wrapped object is. ``subprocess.Popen[bytes]`` is used as
        # a type annotation in third-party packages (mcp, etc.); replacing
        # ``Popen`` with a plain function breaks ``Popen[bytes]`` at
        # import time. Defer ``__class_getitem__`` to the original.
        if hasattr(real, "__class_getitem__"):
            _guarded.__class_getitem__ = real.__class_getitem__
        return _guarded

    def _wrap_popen():
        """Subclass Popen so isinstance checks AND Popen[bytes] still work."""
        real = _subprocess.Popen

        class _GuardedPopen(real):  # type: ignore[misc, valid-type]
            def __init__(self, cmd, *args, **kwargs):
                _check_subprocess_cmd("Popen", cmd)
                super().__init__(cmd, *args, **kwargs)

        _GuardedPopen.__name__ = "Popen"
        _GuardedPopen.__qualname__ = "Popen"
        return _GuardedPopen

    real_run = _subprocess.run
    real_popen = _subprocess.Popen
    real_call = _subprocess.call
    real_check_call = _subprocess.check_call
    real_check_output = _subprocess.check_output
    real_getoutput = _subprocess.getoutput
    real_getstatusoutput = _subprocess.getstatusoutput

    monkeypatch.setattr(_subprocess, "run", _wrap_subprocess("run", real_run))
    monkeypatch.setattr(_subprocess, "Popen", _wrap_popen())
    monkeypatch.setattr(_subprocess, "call", _wrap_subprocess("call", real_call))
    monkeypatch.setattr(
        _subprocess, "check_call", _wrap_subprocess("check_call", real_check_call)
    )
    monkeypatch.setattr(
        _subprocess,
        "check_output",
        _wrap_subprocess("check_output", real_check_output),
    )
    monkeypatch.setattr(
        _subprocess, "getoutput", _wrap_subprocess("getoutput", real_getoutput)
    )
    monkeypatch.setattr(
        _subprocess,
        "getstatusoutput",
        _wrap_subprocess("getstatusoutput", real_getstatusoutput),
    )

    # os.system / os.popen — same risk class, completely unwrapped before.
    real_os_system = _os.system
    real_os_popen = _os.popen

    def _guarded_os_system(command):
        _check_subprocess_cmd("os.system", command)
        return real_os_system(command)

    def _guarded_os_popen(cmd, *args, **kwargs):
        _check_subprocess_cmd("os.popen", cmd)
        return real_os_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(_os, "system", _guarded_os_system)
    monkeypatch.setattr(_os, "popen", _guarded_os_popen)

    # pty.spawn — POSIX-only.
    try:
        import pty as _pty
        if hasattr(_pty, "spawn"):
            real_pty_spawn = _pty.spawn

            def _guarded_pty_spawn(argv, *args, **kwargs):
                _check_subprocess_cmd("pty.spawn", argv)
                return real_pty_spawn(argv, *args, **kwargs)

            monkeypatch.setattr(_pty, "spawn", _guarded_pty_spawn)
    except Exception:
        pass

    # asyncio.create_subprocess_* — bypasses subprocess module entirely.
    try:
        import asyncio as _asyncio
        real_async_exec = _asyncio.create_subprocess_exec
        real_async_shell = _asyncio.create_subprocess_shell

        async def _guarded_async_exec(program, *args, **kwargs):
            _check_subprocess_cmd(
                "asyncio.create_subprocess_exec", [program, *args]
            )
            return await real_async_exec(program, *args, **kwargs)

        async def _guarded_async_shell(cmd, *args, **kwargs):
            _check_subprocess_cmd("asyncio.create_subprocess_shell", cmd)
            return await real_async_shell(cmd, *args, **kwargs)

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _guarded_async_exec)
        monkeypatch.setattr(
            _asyncio, "create_subprocess_shell", _guarded_async_shell
        )
    except Exception:
        pass

    yield


# ── Phase 0: PostgreSQL test fixtures ────────────────────────────────────────
#
# Uses the docker-compose postgres (noproc variant) so no pg_ctl.exe is
# required on Windows. Bring it up with: docker compose up -d postgres
#
# Connection defaults match docker-compose.yml:
#   POSTGRES_USER=thoth  POSTGRES_PASSWORD=thoth  POSTGRES_DB=thoth
# Override via env vars POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD.

import pytest_asyncio
import pytest_postgresql.factories as pg_factories
from alembic import command
from alembic.config import Config

# ── Pre-session template DB cleanup ──────────────────────────────────────────
#
# pytest-postgresql 8.1 creates a template DB (``thoth_tmpl`` here, derived
# from ``dbname="thoth"`` below) the first time the ``postgresql`` fixture
# is requested, then clones it for each per-test DB. The template is meant
# to survive across pytest sessions for speed.
#
# An earlier revision of this conftest dropped the template at session start
# to work around a ``DuplicateDatabase: thoth_tmpl already exists`` error
# on local reruns against the same Docker cluster — but that ALTERed an empty
# (or absent) DB which then either raced with pytest-postgresql's own
# create-template path (CI) or left a hole pytest-postgresql never refilled
# (caching its template-exists state from the previous session).
#
# Current approach: don't pre-emptively drop. CI starts from a fresh
# postgres container so the template doesn't exist. Local dev with a
# persistent container handles re-runs because pytest-postgresql 8.1's
# template creation is idempotent (CREATE DATABASE IF NOT EXISTS-style
# via DatabaseJanitor at the fixture level). If a stale template is
# blocking your local run, drop it once by hand:
#     psql -U thoth -h localhost -c "DROP DATABASE IF EXISTS thoth_tmpl"


# Default to the dedicated `postgres-test` docker-compose service
# (port 5433), NOT the real `postgres` service the developer's Thoth
# install runs against (port 5432). The override env var honours
# THOTH_TEST_POSTGRES_PORT first (matches the compose variable name),
# then the THOTH_TEST_POSTGRES_PORT legacy fallback, then POSTGRES_PORT,
# then falls back to 5433. Overriding to 5432 is only safe in CI where
# there is no real install to collide with.
_TEST_PG_PORT = int(
    os.environ.get("THOTH_TEST_POSTGRES_PORT")
    or os.environ.get("THOTH_TEST_POSTGRES_PORT")
    or os.environ.get("POSTGRES_PORT")
    or "5433"
)
# Default host is ``localhost`` for local-host runs (pytest invoked
# directly on the developer's machine). Inside the docker-compose
# test-runner container ``localhost`` is the container itself, so the
# override env vars route to the ``postgres-test`` compose service.
_TEST_PG_HOST = (
    os.environ.get("THOTH_TEST_POSTGRES_HOST")
    or os.environ.get("THOTH_TEST_POSTGRES_HOST")
    or os.environ.get("POSTGRES_HOST")
    or "localhost"
)
postgresql_noproc = pg_factories.postgresql_noproc(
    host=_TEST_PG_HOST,
    port=_TEST_PG_PORT,
    user=os.environ.get("POSTGRES_USER", "thoth"),
    password=os.environ.get("POSTGRES_PASSWORD", "thoth"),
    dbname="thoth",
)
# Note: we deliberately don't pass ``dbname="thoth_test"`` here. The
# ``postgresql`` factory uses ``proc_fixture.dbname`` when ``dbname`` is
# absent, and that name has already been xdistified by pytest-postgresql
# to include the per-subprocess worker id (e.g. ``thothrun_42``). Hard-
# coding ``thoth_test`` would route every concurrent subprocess onto
# the same per-test DB name and surface as
# ``DuplicateDatabase: database "thoth_test" already exists`` when two
# subprocesses race to create it. Letting the factory inherit the
# xdistified name keeps each subprocess on its own per-test DB.
postgresql = pg_factories.postgresql("postgresql_noproc")


@pytest.fixture
def thoth_db_dsn(postgresql):
    """A freshly-migrated PG DSN for one test.

    Runs Alembic upgrade head against a per-test database created against
    the docker-compose postgres cluster. Each test gets a clean schema;
    the cluster keeps running between tests for speed.
    """
    info = postgresql.info
    password_part = f":{info.password}@" if info.password else "@"
    dsn = f"postgresql://{info.user}{password_part}{info.host}:{info.port}/{info.dbname}"
    cfg = Config("migrations/alembic.ini")
    # Runtime (thoth_db sync-pool, substrate) + alembic env.py read THOTH_PG_DSN
    # (canonical, post-cutover) with THOTH_PG_DSN legacy fallback. The P2 env
    # bridge only mirrors at startup, and this fixture sets the DSN at test time
    # (after bootstrap), so set BOTH spellings explicitly.
    _prev = {k: os.environ.get(k) for k in ("THOTH_PG_DSN", "THOTH_PG_DSN")}
    os.environ["THOTH_PG_DSN"] = dsn
    os.environ["THOTH_PG_DSN"] = dsn
    try:
        command.upgrade(cfg, "head")
        yield dsn
    finally:
        for _k, _v in _prev.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v


@pytest_asyncio.fixture
async def thoth_db_initialized(thoth_db_dsn):
    """Pool initialised on pytest-asyncio's per-test event loop.

    Use this in ``@pytest.mark.asyncio`` tests that ``await`` against
    ``thoth_db.pool()`` / ``connection()`` / ``transaction()`` directly.
    Don't use it from sync test bodies that bridge via
    ``thoth_db.run_sync`` — the pool would be bound to pytest-asyncio's
    loop but ``run_sync`` uses the persistent sync loop, surfacing as
    ``InterfaceError: cannot perform operation: another operation is
    in progress``. Sync tests should use :func:`thoth_db_initialized_sync`
    below.

    Defensive loop-binding check: a prior test (or a ``run_sync`` call
    that lazy-bootstrapped the pool on ``thoth_db._sync_loop``) may
    have left ``thoth_db._pool`` bound to a loop other than this
    test's pytest-asyncio loop. ``init()`` is idempotent — it would
    silently return without rebinding — so the async test body would
    inherit the stale pool and explode on teardown with a cross-loop
    error from ``PoolConnectionHolder.close()``. Detect that mismatch
    here and close the stale pool on its own binding loop first.
    """
    import asyncio
    import thoth_db
    if thoth_db._pool is not None:
        current = asyncio.get_running_loop()
        pool_loop = getattr(thoth_db._pool, "_loop", None)
        if pool_loop is not current:
            # Pool is bound to a different loop (typically the
            # persistent _sync_loop from a sibling sync test). Close
            # via run_sync, which detects the cross-loop situation and
            # offloads the close onto the binding loop's worker thread.
            try:
                thoth_db.run_sync(thoth_db.close())
            except Exception:
                # Binding loop is dead (e.g., a prior pytest-asyncio
                # per-test loop). Orphan the pool — Postgres will reap
                # the idle connections, Python GC will clean up the
                # holders. Leaks briefly but tests are independent.
                thoth_db._pool = None
    await thoth_db.init(thoth_db_dsn)
    yield thoth_db_dsn
    await thoth_db.close()


@pytest.fixture
def thoth_db_initialized_sync(thoth_db_dsn):
    """Pool initialised on thoth_db's persistent sync loop.

    Use this in **synchronous** test bodies that bridge to async DB
    calls via ``thoth_db.run_sync(coro)``. The pool's binding loop
    matches the sync loop, so ``run_sync`` round-trips cleanly. Async
    tests in pytest-asyncio scope should NOT use this fixture — they
    should use :func:`thoth_db_initialized` (above) which binds to the
    per-test asyncio loop.

    Counterpart to ``thoth_db_initialized``: same DSN, same migrated
    schema, different binding loop. The split exists because asyncpg
    pools are loop-bound and we have two different "current loop"
    notions in the test suite — pytest-asyncio's per-test loop vs.
    ``thoth_db._get_sync_loop()``'s persistent one.
    """
    import thoth_db

    # ensure_pool_sync uses thoth_db._get_sync_loop() to run the
    # asyncpg.create_pool coroutine — binding the pool to that loop.
    # Subsequent run_sync(coro) calls reuse the same loop, so the
    # binding matches.
    assert thoth_db.ensure_pool_sync(), "ensure_pool_sync failed; THOTH_PG_DSN should be set by thoth_db_dsn"
    try:
        yield thoth_db_dsn
    finally:
        # Close on the same loop the pool was opened on.
        thoth_db.run_sync(thoth_db.close())
