"""Tests for /title gateway slash command.

Tests the _handle_title_command handler (set/show session titles)
across all gateway messenger platforms.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/title", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner(session_db=None):
    """Create a bare GatewayRunner with a mock session_store and optional session_db."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = session_db

    # Mock session_store that returns a session entry with a known session_id
    mock_session_entry = MagicMock()
    mock_session_entry.session_id = "test_session_123"
    mock_session_entry.session_key = "telegram:12345:67890"
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = mock_session_entry
    runner.session_store = mock_store

    return runner


# ---------------------------------------------------------------------------
# _handle_title_command
# ---------------------------------------------------------------------------


class TestHandleTitleCommand:
    """Tests for GatewayRunner._handle_title_command."""

    @pytest.mark.asyncio
    async def test_set_title(self, thoth_db_initialized):
        """Setting a title returns confirmation."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title My Research Project")
        result = await runner._handle_title_command(event)
        assert "My Research Project" in result
        assert "✏️" in result

        # Verify in DB
        assert await db.get_session_title("test_session_123") == "My Research Project"

    @pytest.mark.asyncio
    async def test_show_title_when_set(self, thoth_db_initialized):
        """Showing title when one is set returns the title."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("test_session_123", "telegram")
        await db.set_session_title("test_session_123", "Existing Title")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title")
        result = await runner._handle_title_command(event)
        assert "Existing Title" in result
        assert "\U0001f4cc" in result

    @pytest.mark.asyncio
    async def test_show_title_when_not_set(self, thoth_db_initialized):
        """Showing title when none is set returns usage hint."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title")
        result = await runner._handle_title_command(event)
        assert "No title set" in result
        assert "/title" in result

    @pytest.mark.asyncio
    async def test_title_conflict(self, thoth_db_initialized):
        """Setting a title already used by another session returns error."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("other_session", "telegram")
        await db.set_session_title("other_session", "Taken Title")
        await db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title Taken Title")
        result = await runner._handle_title_command(event)
        assert "already in use" in result
        assert "⚠️" in result

    @pytest.mark.asyncio
    async def test_no_session_db(self):
        """Returns error when session database is not available."""
        runner = _make_runner(session_db=None)
        event = _make_event(text="/title My Title")
        result = await runner._handle_title_command(event)
        assert "not available" in result

    @pytest.mark.asyncio
    async def test_title_too_long(self, thoth_db_initialized):
        """Setting a title that exceeds max length returns error."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        long_title = "A" * 150
        event = _make_event(text=f"/title {long_title}")
        result = await runner._handle_title_command(event)
        assert "too long" in result
        assert "⚠️" in result

    @pytest.mark.asyncio
    async def test_title_control_chars_sanitized(self, thoth_db_initialized):
        """Control characters are stripped and sanitized title is stored."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title hello\x00world")
        result = await runner._handle_title_command(event)
        assert "helloworld" in result
        assert await db.get_session_title("test_session_123") == "helloworld"

    @pytest.mark.asyncio
    async def test_title_only_control_chars(self, thoth_db_initialized):
        """Title with only control chars returns empty error."""
        from thoth_state import SessionDB
        db = SessionDB()
        await db.create_session("test_session_123", "telegram")

        runner = _make_runner(session_db=db)
        event = _make_event(text="/title \x00\x01\x02")
        result = await runner._handle_title_command(event)
        assert "empty after cleanup" in result

    @pytest.mark.asyncio
    async def test_works_across_platforms(self, thoth_db_initialized):
        """The /title command works for Discord and Telegram too."""
        from thoth_state import SessionDB
        for i, platform in enumerate([Platform.DISCORD, Platform.TELEGRAM]):
            session_id = f"test_session_platform_{i}"
            title = f"Cross-Platform Test {platform.value}"  # unique per platform
            db = SessionDB()
            await db.create_session(session_id, platform.value)

            # Wire the mock session_store to return our per-platform session ID
            from gateway.run import GatewayRunner
            runner = object.__new__(GatewayRunner)
            runner.adapters = {}
            runner._voice_mode = {}
            runner._session_db = db
            mock_entry = MagicMock()
            mock_entry.session_id = session_id
            mock_entry.session_key = f"{platform.value}:12345:67890"
            mock_store = MagicMock()
            mock_store.get_or_create_session.return_value = mock_entry
            runner.session_store = mock_store

            event = _make_event(text=f"/title {title}", platform=platform)
            result = await runner._handle_title_command(event)
            assert title in result
            assert await db.get_session_title(session_id) == title


# ---------------------------------------------------------------------------
# /title in help and known_commands
# ---------------------------------------------------------------------------


class TestTitleInHelp:
    """Verify /title appears in help text and known commands."""

    @pytest.mark.asyncio
    async def test_title_in_help_output(self):
        """The /help output includes /title."""
        runner = _make_runner()
        event = _make_event(text="/help")
        # Need hooks for help command
        from gateway.hooks import HookRegistry
        runner.hooks = HookRegistry()
        result = await runner._handle_help_command(event)
        assert "/title" in result

    def test_title_is_known_command(self):
        """The /title command is in the _known_commands set."""
        from gateway.run import GatewayRunner
        import inspect
        source = inspect.getsource(GatewayRunner._handle_message)
        assert '"title"' in source


# ---------------------------------------------------------------------------
# /new with title
# ---------------------------------------------------------------------------


class TestResetCommandWithTitle:
    """Tests for GatewayRunner._handle_reset_command with a title argument."""

    @pytest.mark.asyncio
    async def test_reset_command_with_title(self):
        """Sending /new <title> resets session and sets the title."""
        from datetime import datetime

        from gateway.run import GatewayRunner
        from gateway.session import SessionEntry, SessionSource, build_session_key

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
        )
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner._voice_mode = {}
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
        runner._session_model_overrides = {}
        runner._pending_model_notes = {}
        runner._background_tasks = set()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )
        session_key = build_session_key(source)
        new_session_entry = SessionEntry(
            session_key=session_key,
            session_id="sess-new",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = new_session_entry
        runner.session_store.reset_session.return_value = new_session_entry
        runner.session_store._entries = {session_key: new_session_entry}
        runner.session_store._generate_session_key.return_value = session_key
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._session_db = MagicMock()
        # set_session_title is async on _AsyncSessionDB; use AsyncMock so
        # 'await self._session_db.set_session_title(...)' doesn't silently fail.
        runner._session_db.set_session_title = AsyncMock()
        runner._agent_cache = {}
        runner._agent_cache_lock = None
        runner._is_user_authorized = lambda _source: True
        runner._format_session_info = lambda: ""

        event = _make_event(text="/new Custom Name")
        result = await runner._handle_reset_command(event)

        runner.session_store.reset_session.assert_called_once()
        runner._session_db.set_session_title.assert_called_once_with(
            "sess-new", "Custom Name"
        )
        # Header reflects the applied title
        assert "Custom Name" in str(result)

    @pytest.mark.asyncio
    async def test_reset_command_duplicate_title_surfaces_warning(self):
        """/new <title> with an already-in-use title returns a warning in the reply."""
        from datetime import datetime

        from gateway.run import GatewayRunner
        from gateway.session import SessionEntry, SessionSource, build_session_key

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
        )
        adapter = MagicMock()
        adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner._voice_mode = {}
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
        runner._session_model_overrides = {}
        runner._pending_model_notes = {}
        runner._background_tasks = set()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )
        session_key = build_session_key(source)
        new_session_entry = SessionEntry(
            session_key=session_key,
            session_id="sess-new",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = new_session_entry
        runner.session_store.reset_session.return_value = new_session_entry
        runner.session_store._entries = {session_key: new_session_entry}
        runner.session_store._generate_session_key.return_value = session_key
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._session_db = MagicMock()
        # set_session_title is async; use AsyncMock with a side_effect so
        # 'await' works correctly and the ValueError is raised.
        runner._session_db.set_session_title = AsyncMock(
            side_effect=ValueError("Title 'Dup' is already in use by session abc-123")
        )
        runner._agent_cache = {}
        runner._agent_cache_lock = None
        runner._is_user_authorized = lambda _source: True
        runner._format_session_info = lambda: ""

        event = _make_event(text="/new Dup")
        result = await runner._handle_reset_command(event)

        runner._session_db.set_session_title.assert_called_once()
        reply = str(result)
        assert "already in use" in reply
        assert "session started untitled" in reply
        # Header must NOT claim the rejected title as the session name
        assert "New session started: Dup" not in reply


# ---------------------------------------------------------------------------
# /new in help output
# ---------------------------------------------------------------------------


class TestNewInHelp:
    """Verify /new appears in help text with the [name] args hint."""

    def test_new_command_in_help_output(self):
        """The gateway help output includes /new with the [name] hint."""
        from thoth_cli.commands import gateway_help_lines
        lines = gateway_help_lines()
        new_line = next((line for line in lines if line.startswith("`/new ")), None)
        assert new_line is not None
        assert "[name]" in new_line
