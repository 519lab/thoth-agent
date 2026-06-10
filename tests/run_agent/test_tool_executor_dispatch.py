"""Unit tests for agent/tool_executor.py dispatch logic.

``tests/run_agent/test_run_agent.py`` exercises the sequential/concurrent
*selection* logic (which executor gets picked) but patches the executors
themselves; ``test_concurrent_interrupt.py`` covers interrupt fan-out. This
file covers the executor *bodies*, which sat at ~47% line coverage:

- the sequential special-tool dispatch ladder (todo, session_search, memory,
  clarify, delegate_task, context-engine tools, memory-provider tools) and
  its error branches,
- plugin/guardrail blocking, interrupt skipping, malformed-args handling,
  checkpointing, and callback plumbing,
- the cold concurrent-execution body (real ThreadPoolExecutor run): result
  ordering, per-slot blocking, worker error capture, and callback
  propagation.

All tests run against a real AIAgent (mocked OpenAI client, no network).
Timing-dependent branches (heartbeats, mid-flight interrupt cancellation)
are intentionally out of scope — see test_concurrent_interrupt.py.
"""

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root importable (mirrors sibling test files).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tool_executor import (  # noqa: E402
    execute_tool_calls_concurrent,
    execute_tool_calls_sequential,
)
from run_agent import AIAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def agent():
    """Minimal real AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch(
            "run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def _tool_call(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _assistant_msg(*tool_calls):
    return SimpleNamespace(content="", tool_calls=list(tool_calls))


def _last_tool_msg(messages):
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, f"no tool message appended; messages={messages!r}"
    return tool_msgs[-1]


# ---------------------------------------------------------------------------
# Sequential: special-tool dispatch ladder
# ---------------------------------------------------------------------------


class TestSequentialSpecialDispatch:
    def test_todo_dispatch(self, agent):
        tc = _tool_call("todo", '{"todos": [{"text": "x"}], "merge": true}')
        messages = []
        with patch("tools.todo_tool.todo_tool", return_value="todo ok") as mock_todo:
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        mock_todo.assert_called_once_with(
            todos=[{"text": "x"}], merge=True, store=agent._todo_store
        )
        assert _last_tool_msg(messages)["content"] == "todo ok"

    def test_session_search_without_db(self, agent):
        tc = _tool_call("session_search", '{"query": "foo"}')
        messages = []
        with patch.object(agent, "_get_session_db_for_recall", return_value=None):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        result = json.loads(_last_tool_msg(messages)["content"])
        assert result["success"] is False
        assert result["error"]

    def test_session_search_with_db(self, agent):
        tc = _tool_call("session_search", '{"query": "foo", "limit": 7}')
        messages = []
        fake_db = MagicMock()
        with (
            patch.object(agent, "_get_session_db_for_recall", return_value=fake_db),
            patch(
                "tools.session_search_tool.session_search",
                return_value='{"success": true}',
            ) as mock_search,
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert mock_search.call_args.kwargs["query"] == "foo"
        assert mock_search.call_args.kwargs["limit"] == 7
        assert mock_search.call_args.kwargs["db"] is fake_db
        assert _last_tool_msg(messages)["content"] == '{"success": true}'

    def test_memory_dispatch_resets_counter_and_bridges_provider(self, agent):
        agent._turns_since_memory = 9
        agent._memory_manager = MagicMock()
        tc = _tool_call("memory", '{"action": "add", "content": "fact"}')
        messages = []
        with patch("tools.memory_tool.memory_tool", return_value="saved") as mock_mem:
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert agent._turns_since_memory == 0
        assert mock_mem.call_args.kwargs["action"] == "add"
        agent._memory_manager.on_memory_write.assert_called_once()
        assert _last_tool_msg(messages)["content"] == "saved"

    def test_memory_bridge_error_is_swallowed(self, agent):
        agent._memory_manager = MagicMock()
        agent._memory_manager.on_memory_write.side_effect = RuntimeError("boom")
        tc = _tool_call("memory", '{"action": "replace", "content": "fact"}')
        messages = []
        with patch("tools.memory_tool.memory_tool", return_value="saved"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert _last_tool_msg(messages)["content"] == "saved"

    def test_clarify_dispatch(self, agent):
        agent.clarify_callback = MagicMock()
        tc = _tool_call("clarify", '{"question": "which?", "choices": ["a", "b"]}')
        messages = []
        with patch("tools.clarify_tool.clarify_tool", return_value="answer: a") as mock_cl:
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        mock_cl.assert_called_once_with(
            question="which?", choices=["a", "b"], callback=agent.clarify_callback
        )
        assert _last_tool_msg(messages)["content"] == "answer: a"

    def test_delegate_task_goal_label(self, agent):
        tc = _tool_call("delegate_task", '{"goal": "research X"}')
        messages = []
        with (
            patch.object(agent, "_dispatch_delegate_task", return_value="done") as mock_d,
            patch.object(agent, "_should_emit_quiet_tool_messages", return_value=True),
            patch.object(agent, "_should_start_quiet_spinner", return_value=False),
            patch.object(agent, "_vprint") as mock_vprint,
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        mock_d.assert_called_once_with({"goal": "research X"})
        assert _last_tool_msg(messages)["content"] == "done"
        assert agent._delegate_spinner is None
        mock_vprint.assert_called()  # cute message emitted without spinner

    def test_delegate_task_tasks_list_label(self, agent):
        tc = _tool_call("delegate_task", '{"tasks": [{"goal": "a"}, {"goal": "b"}]}')
        messages = []
        with patch.object(agent, "_dispatch_delegate_task", return_value="2 done"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert _last_tool_msg(messages)["content"] == "2 done"

    def test_context_engine_tool_success(self, agent):
        agent._context_engine_tool_names = {"lcm_grep"}
        agent.context_compressor = MagicMock()
        agent.context_compressor.handle_tool_call.return_value = '{"matches": []}'
        tc = _tool_call("lcm_grep", '{"pattern": "foo"}')
        messages = []
        execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        agent.context_compressor.handle_tool_call.assert_called_once_with(
            "lcm_grep", {"pattern": "foo"}, messages=messages
        )
        assert _last_tool_msg(messages)["content"] == '{"matches": []}'

    def test_context_engine_tool_error_becomes_json(self, agent):
        agent._context_engine_tool_names = {"lcm_grep"}
        agent.context_compressor = MagicMock()
        agent.context_compressor.handle_tool_call.side_effect = RuntimeError("engine down")
        tc = _tool_call("lcm_grep", "{}")
        messages = []
        execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        result = json.loads(_last_tool_msg(messages)["content"])
        assert "lcm_grep" in result["error"]
        assert "engine down" in result["error"]

    def test_memory_provider_tool_success(self, agent):
        agent._memory_manager = MagicMock()
        agent._memory_manager.has_tool.return_value = True
        agent._memory_manager.handle_tool_call.return_value = '{"retained": 1}'
        tc = _tool_call("hindsight_retain", '{"content": "x"}')
        messages = []
        execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        agent._memory_manager.handle_tool_call.assert_called_once_with(
            "hindsight_retain", {"content": "x"}
        )
        assert _last_tool_msg(messages)["content"] == '{"retained": 1}'

    def test_memory_provider_tool_error_becomes_json(self, agent):
        agent._memory_manager = MagicMock()
        agent._memory_manager.has_tool.return_value = True
        agent._memory_manager.handle_tool_call.side_effect = RuntimeError("provider down")
        tc = _tool_call("hindsight_retain", "{}")
        messages = []
        execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        result = json.loads(_last_tool_msg(messages)["content"])
        assert "hindsight_retain" in result["error"]
        assert "provider down" in result["error"]


# ---------------------------------------------------------------------------
# Sequential: blocking, interrupts, malformed args, default dispatch
# ---------------------------------------------------------------------------


class TestSequentialBlockingAndErrors:
    def test_plugin_block_skips_execution(self, agent):
        tc = _tool_call("web_search", '{"q": "x"}')
        messages = []
        with (
            patch(
                "thoth_cli.plugins.get_pre_tool_call_block_message",
                return_value="blocked by policy",
            ),
            patch("run_agent.handle_function_call") as mock_hfc,
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        mock_hfc.assert_not_called()
        result = json.loads(_last_tool_msg(messages)["content"])
        assert result["error"] == "blocked by policy"

    def test_guardrail_block_skips_execution(self, agent):
        tc = _tool_call("web_search", "{}")
        messages = []
        deny = SimpleNamespace(allows_execution=False)
        with (
            patch.object(agent._tool_guardrails, "before_call", return_value=deny),
            patch.object(
                agent, "_guardrail_block_result", return_value="[guardrail block]"
            ),
            patch("run_agent.handle_function_call") as mock_hfc,
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        mock_hfc.assert_not_called()
        assert _last_tool_msg(messages)["content"] == "[guardrail block]"

    def test_preexisting_interrupt_skips_all(self, agent):
        agent._interrupt_requested = True
        tc1 = _tool_call("web_search", "{}", call_id="c1")
        tc2 = _tool_call("web_search", "{}", call_id="c2")
        messages = []
        with patch("run_agent.handle_function_call") as mock_hfc:
            execute_tool_calls_sequential(agent, _assistant_msg(tc1, tc2), messages, "t1")
        mock_hfc.assert_not_called()
        assert len(messages) == 2
        assert all("user interrupt" in m["content"] for m in messages)
        assert [m["tool_call_id"] for m in messages] == ["c1", "c2"]

    def test_interrupt_during_tool_skips_remaining(self, agent):
        tc1 = _tool_call("web_search", "{}", call_id="c1")
        tc2 = _tool_call("web_search", "{}", call_id="c2")
        messages = []

        def _interrupting_call(*args, **kwargs):
            agent._interrupt_requested = True
            return "first result"

        with patch("run_agent.handle_function_call", side_effect=_interrupting_call) as mock_hfc:
            execute_tool_calls_sequential(agent, _assistant_msg(tc1, tc2), messages, "t1")
        assert mock_hfc.call_count == 1
        assert messages[-2]["content"] == "first result"
        assert "was not started" in messages[-1]["content"]
        assert messages[-1]["tool_call_id"] == "c2"

    def test_malformed_json_args_become_empty_dict(self, agent):
        tc = _tool_call("web_search", "{not json")
        messages = []
        with patch("run_agent.handle_function_call", return_value="ok") as mock_hfc:
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert mock_hfc.call_args.args[1] == {}

    def test_non_dict_json_args_become_empty_dict(self, agent):
        tc = _tool_call("web_search", '["a", "b"]')
        messages = []
        with patch("run_agent.handle_function_call", return_value="ok") as mock_hfc:
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert mock_hfc.call_args.args[1] == {}

    def test_default_dispatch_error_nonquiet(self, agent, capsys):
        agent.quiet_mode = False
        tc = _tool_call("web_search", '{"q": "x"}')
        messages = []
        with patch(
            "run_agent.handle_function_call", side_effect=RuntimeError("api down")
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        content = _last_tool_msg(messages)["content"]
        assert "Error executing tool 'web_search'" in content
        assert "api down" in content
        out = capsys.readouterr().out
        assert "Tool 1" in out  # non-quiet per-tool logging printed

    def test_default_dispatch_error_quiet_spinnerless(self, agent):
        tc = _tool_call("web_search", "{}")
        messages = []
        with (
            patch.object(agent, "_should_emit_quiet_tool_messages", return_value=True),
            patch.object(agent, "_should_start_quiet_spinner", return_value=False),
            patch.object(agent, "_vprint") as mock_vprint,
            patch("run_agent.handle_function_call", side_effect=RuntimeError("boom")),
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert "Error executing tool 'web_search'" in _last_tool_msg(messages)["content"]
        mock_vprint.assert_called()

    def test_verbose_nonquiet_prints_full_result(self, agent, capsys):
        agent.quiet_mode = False
        agent.verbose_logging = True
        tc = _tool_call("web_search", '{"q": "x"}')
        messages = []
        with patch("run_agent.handle_function_call", return_value="full result text"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        out = capsys.readouterr().out
        assert "full result text" in out

    def test_checkpoint_before_write_file(self, agent):
        agent._checkpoint_mgr = MagicMock(enabled=True)
        agent._checkpoint_mgr.get_working_dir_for_path.return_value = "/tmp/wd"
        tc = _tool_call("write_file", '{"path": "/tmp/wd/a.txt", "content": "x"}')
        messages = []
        with patch("run_agent.handle_function_call", return_value="written"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        agent._checkpoint_mgr.ensure_checkpoint.assert_called_once_with(
            "/tmp/wd", "before write_file"
        )

    def test_checkpoint_before_destructive_terminal(self, agent):
        agent._checkpoint_mgr = MagicMock(enabled=True)
        tc = _tool_call(
            "terminal", '{"command": "rm -rf build", "workdir": "/tmp/wd"}'
        )
        messages = []
        with patch("run_agent.handle_function_call", return_value="removed"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        args, _ = agent._checkpoint_mgr.ensure_checkpoint.call_args
        assert args[0] == "/tmp/wd"
        assert args[1].startswith("before terminal: rm -rf build")

    def test_subdir_hints_appended_to_result(self, agent):
        agent._subdirectory_hints = MagicMock()
        agent._subdirectory_hints.check_tool_call.return_value = "\n[hint: see sub/]"
        tc = _tool_call("read_file", '{"path": "sub/a.txt"}')
        messages = []
        with patch("run_agent.handle_function_call", return_value="contents"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        assert _last_tool_msg(messages)["content"] == "contents\n[hint: see sub/]"

    def test_tool_delay_sleeps_between_calls(self, agent):
        agent.tool_delay = 0.01
        tc1 = _tool_call("web_search", "{}", call_id="c1")
        tc2 = _tool_call("web_search", "{}", call_id="c2")
        messages = []
        with (
            patch("run_agent.handle_function_call", return_value="ok"),
            patch("agent.tool_executor.time.sleep") as mock_sleep,
        ):
            execute_tool_calls_sequential(agent, _assistant_msg(tc1, tc2), messages, "t1")
        mock_sleep.assert_called_once_with(0.01)

    def test_callbacks_fire_and_callback_errors_are_swallowed(self, agent):
        agent.tool_progress_callback = MagicMock(side_effect=RuntimeError("cb boom"))
        agent.tool_start_callback = MagicMock()
        agent.tool_complete_callback = MagicMock()
        tc = _tool_call("web_search", '{"q": "x"}', call_id="c9")
        messages = []
        with patch("run_agent.handle_function_call", return_value="ok"):
            execute_tool_calls_sequential(agent, _assistant_msg(tc), messages, "t1")
        # progress callback raised on tool.started and tool.completed — both swallowed
        assert agent.tool_progress_callback.call_count == 2
        agent.tool_start_callback.assert_called_once()
        start_args = agent.tool_start_callback.call_args.args
        assert start_args[0] == "c9" and start_args[1] == "web_search"
        agent.tool_complete_callback.assert_called_once()
        assert _last_tool_msg(messages)["content"] == "ok"


# ---------------------------------------------------------------------------
# Concurrent: cold execution body
# ---------------------------------------------------------------------------


class TestConcurrentExecution:
    def test_results_appended_in_original_order(self, agent):
        tc1 = _tool_call("web_search", '{"q": "1"}', call_id="c1")
        tc2 = _tool_call("read_file", '{"path": "x"}', call_id="c2")
        tc3 = _tool_call("web_search", '{"q": "3"}', call_id="c3")
        messages = []

        def _invoke(name, args, task_id, call_id, **kwargs):
            return f"result-for-{call_id}"

        with patch.object(agent, "_invoke_tool", side_effect=_invoke):
            execute_tool_calls_concurrent(
                agent, _assistant_msg(tc1, tc2, tc3), messages, "t1"
            )
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]
        assert [m["content"] for m in tool_msgs] == [
            "result-for-c1",
            "result-for-c2",
            "result-for-c3",
        ]

    def test_worker_exception_becomes_error_result(self, agent):
        tc = _tool_call("web_search", "{}", call_id="c1")
        messages = []
        with patch.object(agent, "_invoke_tool", side_effect=ValueError("worker boom")):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc), messages, "t1")
        content = _last_tool_msg(messages)["content"]
        assert "Error executing tool 'web_search'" in content
        assert "worker boom" in content

    def test_malformed_args_and_counter_resets(self, agent):
        agent._turns_since_memory = 7
        agent._iters_since_skill = 4
        tc1 = _tool_call("memory", "{broken", call_id="c1")
        tc2 = _tool_call("skill_manage", '"just a string"', call_id="c2")
        messages = []
        seen_args = {}

        def _invoke(name, args, task_id, call_id, **kwargs):
            seen_args[name] = args
            return "ok"

        with patch.object(agent, "_invoke_tool", side_effect=_invoke):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1, tc2), messages, "t1")
        assert agent._turns_since_memory == 0
        assert agent._iters_since_skill == 0
        assert seen_args == {"memory": {}, "skill_manage": {}}

    def test_plugin_blocked_slot_not_executed(self, agent):
        tc1 = _tool_call("web_search", "{}", call_id="c1")
        tc2 = _tool_call("terminal", '{"command": "ls"}', call_id="c2")
        messages = []

        def _block(name, args, task_id=""):
            return "terminal disabled" if name == "terminal" else None

        executed = []

        def _invoke(name, args, task_id, call_id, **kwargs):
            executed.append(name)
            return "ran"

        with (
            patch(
                "thoth_cli.plugins.get_pre_tool_call_block_message", side_effect=_block
            ),
            patch.object(agent, "_invoke_tool", side_effect=_invoke),
        ):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1, tc2), messages, "t1")
        assert executed == ["web_search"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs[0]["content"] == "ran"
        assert json.loads(tool_msgs[1]["content"])["error"] == "terminal disabled"

    def test_guardrail_blocked_slot_not_executed(self, agent):
        tc1 = _tool_call("web_search", "{}", call_id="c1")
        tc2 = _tool_call("web_search", "{}", call_id="c2")
        messages = []
        allow = SimpleNamespace(allows_execution=True)
        deny = SimpleNamespace(allows_execution=False)
        decisions = iter([allow, deny])
        executed = []

        def _invoke(name, args, task_id, call_id, **kwargs):
            executed.append(call_id)
            return "ran"

        with (
            patch.object(
                agent._tool_guardrails, "before_call", side_effect=lambda *a: next(decisions)
            ),
            patch.object(agent, "_guardrail_block_result", return_value="[blocked]"),
            patch.object(agent, "_invoke_tool", side_effect=_invoke),
        ):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1, tc2), messages, "t1")
        assert executed == ["c1"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs[0]["content"] == "ran"
        assert tool_msgs[1]["content"] == "[blocked]"

    def test_checkpoints_taken_for_mutating_tools(self, agent):
        agent._checkpoint_mgr = MagicMock(enabled=True)
        agent._checkpoint_mgr.get_working_dir_for_path.return_value = "/tmp/wd"
        tc1 = _tool_call("write_file", '{"path": "/tmp/wd/a.txt"}', call_id="c1")
        tc2 = _tool_call("terminal", '{"command": "rm -rf x", "workdir": "/tmp/wd"}', call_id="c2")
        messages = []
        with patch.object(agent, "_invoke_tool", return_value="ok"):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1, tc2), messages, "t1")
        checkpoint_labels = [
            c.args[1] for c in agent._checkpoint_mgr.ensure_checkpoint.call_args_list
        ]
        assert "before write_file" in checkpoint_labels
        assert any(label.startswith("before terminal: rm -rf x") for label in checkpoint_labels)

    def test_callbacks_fire_per_tool(self, agent):
        agent.tool_progress_callback = MagicMock()
        agent.tool_start_callback = MagicMock()
        agent.tool_complete_callback = MagicMock()
        tc1 = _tool_call("web_search", '{"q": "1"}', call_id="c1")
        tc2 = _tool_call("web_search", '{"q": "2"}', call_id="c2")
        messages = []
        with patch.object(agent, "_invoke_tool", return_value="ok"):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1, tc2), messages, "t1")
        started = [
            c for c in agent.tool_progress_callback.call_args_list
            if c.args[0] == "tool.started"
        ]
        completed = [
            c for c in agent.tool_progress_callback.call_args_list
            if c.args[0] == "tool.completed"
        ]
        assert len(started) == 2
        assert len(completed) == 2
        assert agent.tool_start_callback.call_count == 2
        assert agent.tool_complete_callback.call_count == 2
        assert {c.args[0] for c in agent.tool_complete_callback.call_args_list} == {"c1", "c2"}

    def test_approval_callbacks_propagate_to_workers(self, agent):
        from tools.terminal_tool import (
            _get_approval_callback,
            set_approval_callback,
            set_sudo_password_callback,
        )

        approval_cb = MagicMock()
        sudo_cb = MagicMock()
        seen_in_worker = {}

        def _invoke(name, args, task_id, call_id, **kwargs):
            seen_in_worker["approval"] = _get_approval_callback()
            return "ok"

        set_approval_callback(approval_cb)
        set_sudo_password_callback(sudo_cb)
        try:
            tc = _tool_call("terminal", '{"command": "echo hi"}', call_id="c1")
            with patch.object(agent, "_invoke_tool", side_effect=_invoke):
                execute_tool_calls_concurrent(agent, _assistant_msg(tc), [], "t1")
        finally:
            set_approval_callback(None)
            set_sudo_password_callback(None)
        assert seen_in_worker["approval"] is approval_cb

    def test_subdir_hints_appended_to_result(self, agent):
        agent._subdirectory_hints = MagicMock()
        agent._subdirectory_hints.check_tool_call.return_value = "\n[hint: see sub/]"
        tc = _tool_call("read_file", '{"path": "sub/a.txt"}', call_id="c1")
        messages = []
        with patch.object(agent, "_invoke_tool", return_value="contents"):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc), messages, "t1")
        assert _last_tool_msg(messages)["content"] == "contents\n[hint: see sub/]"

    def test_nonquiet_logging_paths(self, agent, capsys):
        agent.quiet_mode = False
        tc1 = _tool_call("web_search", '{"q": "' + "x" * 300 + '"}', call_id="c1")
        messages = []
        with patch.object(agent, "_invoke_tool", return_value="ok"):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1), messages, "t1")
        out = capsys.readouterr().out
        assert "Concurrent: 1 tool calls" in out
        assert "..." in out  # long args are truncated in the preview

    def test_verbose_logging_paths(self, agent, capsys):
        agent.quiet_mode = False
        agent.verbose_logging = True
        tc1 = _tool_call("web_search", '{"q": "x"}', call_id="c1")
        messages = []
        with patch.object(agent, "_invoke_tool", return_value="verbose result"):
            execute_tool_calls_concurrent(agent, _assistant_msg(tc1), messages, "t1")
        out = capsys.readouterr().out
        assert "verbose result" in out
