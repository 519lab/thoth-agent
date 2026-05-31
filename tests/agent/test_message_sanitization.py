"""Tests for ``agent/message_sanitization.py``.

These pure helpers are the last line of defence against payloads that
crash ``json.dumps`` inside the OpenAI SDK or get rejected by upstream
APIs (lone surrogates from byte-level reasoning models, malformed
tool-call JSON from local backends, non-ASCII on LANG=C hosts, images sent
to text-only servers). They run on the hot path of every retry, so the
in-place mutation contracts and the "return True iff something changed"
contracts both matter.
"""

import pytest

from agent.message_sanitization import (
    _escape_invalid_chars_in_json_strings,
    _repair_tool_call_arguments,
    _sanitize_messages_non_ascii,
    _sanitize_messages_surrogates,
    _sanitize_structure_non_ascii,
    _sanitize_structure_surrogates,
    _sanitize_surrogates,
    _sanitize_tools_non_ascii,
    _strip_images_from_messages,
    _strip_non_ascii,
)

# A lone high surrogate — valid as a Python str code point but invalid in
# UTF-8, which is exactly what crashes json.dumps in the SDK.
LONE_SURROGATE = "\ud800"


class TestSanitizeSurrogates:
    def test_replaces_lone_surrogate(self):
        assert _sanitize_surrogates(f"hi{LONE_SURROGATE}there") == "hi�there"

    def test_clean_text_is_returned_unchanged(self):
        text = "perfectly normal ünïcödé 🎉"
        # Fast path: identical object returned (no needless allocation).
        assert _sanitize_surrogates(text) is text

    def test_empty_string(self):
        assert _sanitize_surrogates("") == ""


class TestSanitizeStructureSurrogates:
    def test_nested_dict_and_list_mutated_in_place(self):
        payload = {
            "a": f"x{LONE_SURROGATE}",
            "nested": {"b": f"y{LONE_SURROGATE}"},
            "items": [f"z{LONE_SURROGATE}", {"c": "clean"}],
        }
        found = _sanitize_structure_surrogates(payload)
        assert found is True
        assert payload == {
            "a": "x�",
            "nested": {"b": "y�"},
            "items": ["z�", {"c": "clean"}],
        }

    def test_returns_false_when_nothing_to_fix(self):
        payload = {"a": "clean", "items": ["also clean"]}
        assert _sanitize_structure_surrogates(payload) is False


class TestSanitizeMessagesSurrogates:
    def test_string_content(self):
        messages = [{"role": "user", "content": f"hello{LONE_SURROGATE}"}]
        assert _sanitize_messages_surrogates(messages) is True
        assert messages[0]["content"] == "hello�"

    def test_list_content_parts(self):
        messages: list[dict] = [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"part{LONE_SURROGATE}"}],
            }
        ]
        assert _sanitize_messages_surrogates(messages) is True
        assert messages[0]["content"][0]["text"] == "part�"

    def test_tool_call_arguments_and_name(self):
        messages: list[dict] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call{LONE_SURROGATE}",
                        "function": {
                            "name": f"fn{LONE_SURROGATE}",
                            "arguments": f'{{"x":"{LONE_SURROGATE}"}}',
                        },
                    }
                ],
            }
        ]
        assert _sanitize_messages_surrogates(messages) is True
        tc = messages[0]["tool_calls"][0]
        assert "\ud800" not in tc["id"]
        assert "\ud800" not in tc["function"]["name"]
        assert "\ud800" not in tc["function"]["arguments"]

    def test_extra_reasoning_field_is_walked(self):
        # Byte-level reasoning models stash surrogates in reasoning_content /
        # reasoning_details that the per-field checks above don't reach.
        messages: list[dict] = [
            {
                "role": "assistant",
                "content": "ok",
                "reasoning_content": f"thinking{LONE_SURROGATE}",
                "reasoning_details": [{"text": f"deep{LONE_SURROGATE}"}],
            }
        ]
        assert _sanitize_messages_surrogates(messages) is True
        assert messages[0]["reasoning_content"] == "thinking�"
        assert messages[0]["reasoning_details"][0]["text"] == "deep�"

    def test_clean_messages_report_no_change(self):
        messages = [{"role": "user", "content": "clean text"}]
        assert _sanitize_messages_surrogates(messages) is False
        assert messages[0]["content"] == "clean text"

    def test_non_dict_entries_are_skipped(self):
        messages = ["not a dict", {"role": "user", "content": "ok"}]
        assert _sanitize_messages_surrogates(messages) is False


class TestEscapeInvalidCharsInJsonStrings:
    def test_control_char_inside_string_is_escaped(self):
        raw = '{"a":"line1\nline2"}'  # literal newline inside the value
        out = _escape_invalid_chars_in_json_strings(raw)
        assert "\n" not in out
        assert "\\u000a" in out

    def test_control_char_outside_string_is_left_alone(self):
        raw = '{"a":1}\n'  # newline is structural whitespace, not in a string
        out = _escape_invalid_chars_in_json_strings(raw)
        assert out.endswith("\n")

    def test_already_escaped_sequence_passes_through(self):
        raw = r'{"a":"tab\tend"}'
        assert _escape_invalid_chars_in_json_strings(raw) == raw

    def test_clean_json_unchanged(self):
        raw = '{"a":"b","c":[1,2,3]}'
        assert _escape_invalid_chars_in_json_strings(raw) == raw


class TestRepairToolCallArguments:
    def test_valid_json_is_recompacted(self):
        assert _repair_tool_call_arguments('{"a": 1}') == '{"a":1}'

    def test_empty_becomes_empty_object(self):
        assert _repair_tool_call_arguments("") == "{}"
        assert _repair_tool_call_arguments("   ") == "{}"

    def test_python_none_becomes_empty_object(self):
        assert _repair_tool_call_arguments("None") == "{}"

    def test_trailing_comma_repaired(self):
        assert _repair_tool_call_arguments('{"a":1,}') == '{"a":1}'

    def test_unclosed_brace_repaired(self):
        assert _repair_tool_call_arguments('{"a":1') == '{"a":1}'

    def test_control_chars_in_value_repaired(self):
        # strict=False json.loads accepts the literal newline, and the
        # reserialised form escapes it — a valid repair.
        out = _repair_tool_call_arguments('{"a":"x\ny"}')
        assert out == '{"a":"x\\ny"}'

    def test_unrepairable_falls_back_to_empty_object(self):
        assert _repair_tool_call_arguments("not json at all {{{") == "{}"


class TestStripNonAscii:
    def test_strips_non_ascii(self):
        assert _strip_non_ascii("café") == "caf"

    def test_pure_ascii_unchanged(self):
        assert _strip_non_ascii("plain ascii 123") == "plain ascii 123"

    def test_sanitize_messages_non_ascii_in_place(self):
        messages = [{"role": "user", "content": "héllo"}]
        assert _sanitize_messages_non_ascii(messages) is True
        assert messages[0]["content"] == "hllo"

    def test_sanitize_messages_non_ascii_no_change(self):
        messages = [{"role": "user", "content": "ascii only"}]
        assert _sanitize_messages_non_ascii(messages) is False

    def test_sanitize_messages_non_ascii_covers_all_fields(self):
        messages: list[dict] = [
            {
                "role": "tool",
                "content": [{"type": "text", "text": "rés"}],
                "name": "tøol",
                "reasoning_content": "thïnk",
                "tool_calls": [
                    {"function": {"arguments": '{"q":"naïve"}'}},
                ],
            }
        ]
        assert _sanitize_messages_non_ascii(messages) is True
        msg = messages[0]
        assert msg["content"][0]["text"] == "rs"
        assert msg["name"] == "tol"
        assert msg["reasoning_content"] == "thnk"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"q":"nave"}'

    def test_sanitize_tools_non_ascii(self):
        tools: list[dict] = [
            {"function": {"name": "lookup", "description": "find a café nearby"}}
        ]
        assert _sanitize_tools_non_ascii(tools) is True
        assert tools[0]["function"]["description"] == "find a caf nearby"

    def test_sanitize_structure_non_ascii_nested_list_and_dict(self):
        payload = {"a": ["nö", {"b": "yés"}], "c": "clean"}
        assert _sanitize_structure_non_ascii(payload) is True
        assert payload == {"a": ["n", {"b": "ys"}], "c": "clean"}

    def test_sanitize_structure_non_ascii_no_change(self):
        assert _sanitize_structure_non_ascii({"a": "clean", "b": ["also"]}) is False


class TestStripImagesFromMessages:
    def test_image_part_removed_keeps_text(self):
        messages: list[dict] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look:"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        assert _strip_images_from_messages(messages) is True
        assert messages[0]["content"] == [{"type": "text", "text": "look:"}]

    def test_tool_message_of_only_images_gets_placeholder_not_deleted(self):
        # Deleting it would orphan the assistant tool_call_id -> HTTP 400.
        messages: list[dict] = [
            {
                "role": "tool",
                "tool_call_id": "abc",
                "content": [{"type": "image_url", "image_url": {"url": "x"}}],
            }
        ]
        assert _strip_images_from_messages(messages) is True
        assert messages[0]["content"] == (
            "[image content removed — server does not support images]"
        )
        assert messages[0]["tool_call_id"] == "abc"

    def test_image_only_user_message_is_dropped(self):
        messages: list[dict] = [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
            {"role": "user", "content": "keep me"},
        ]
        assert _strip_images_from_messages(messages) is True
        assert len(messages) == 1
        assert messages[0]["content"] == "keep me"

    def test_no_images_reports_no_change(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert _strip_images_from_messages(messages) is False
