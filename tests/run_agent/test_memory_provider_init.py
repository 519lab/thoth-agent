"""Regression tests for memory provider selection during AIAgent init."""

from types import SimpleNamespace
from unittest.mock import patch


def test_blank_memory_provider_does_not_auto_enable_honcho():
    """Blank memory.provider should remain opt-out even if Honcho fallback looks configured."""
    cfg = {"memory": {"provider": ""}, "agent": {}}
    honcho_cfg = SimpleNamespace(enabled=True, api_key="stale-key", base_url=None)

    with (
        patch("thoth_cli.config.load_config", return_value=cfg),
        patch("thoth_cli.config.save_config") as save_config,
        patch(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            return_value=honcho_cfg,
        ) as from_global_config,
        patch("plugins.memory.load_memory_provider") as load_memory_provider,
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
        )

    assert agent._memory_manager is None
    from_global_config.assert_not_called()
    load_memory_provider.assert_not_called()
    save_config.assert_not_called()


class AsyncSessionDB:
    async def get_session_title(self, _session_id):
        return "Async Session Title"


class FakeMemoryProvider:
    name = "fake-memory"

    def __init__(self):
        self.initialize_kwargs = None

    def is_available(self):
        return True

    def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs

    def get_tool_schemas(self):
        return []


def test_memory_provider_init_bridges_async_session_title_lookup():
    """AIAgent init is sync but may receive an async-backed SessionDB."""
    cfg = {"memory": {"provider": "fake"}, "agent": {}}
    provider = FakeMemoryProvider()
    session_db = AsyncSessionDB()

    with (
        patch("thoth_cli.config.load_config", return_value=cfg),
        patch("thoth_cli.config.save_config"),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="async-title-session",
            session_db=session_db,
        )

    assert agent._memory_manager is not None
    assert provider.initialize_kwargs is not None
    assert provider.initialize_kwargs["session_id"] == "async-title-session"
    assert provider.initialize_kwargs["session_title"] == "Async Session Title"

