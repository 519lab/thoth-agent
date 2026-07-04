"""Unit tests for the Round-3 Tier-A structural gist (``agent.gist_extract``).

These pin the behaviours the graded suite cares about: a file read keeps its
first lines VERBATIM (the c2 license-header fix), code reads surface an outline
of the names they define, JSON tool-result envelopes surface error/exit_code,
terminal output keeps its exit code and tail, the budget is respected, secrets
are redacted, and the whole function is deterministic (no LLM, no clock, no
randomness). Pure — no PG, no network.
"""

import json

import pytest

from agent.gist_extract import default_budget_chars, structural_gist

LICENSE_HEADER = (
    "# Copyright 2026 Example Corp.\n"
    "# SPDX-License-Identifier: Apache-2.0\n"
    "#\n"
    "# Licensed under the Apache License, Version 2.0.\n"
    '"""Widget module."""\n'
)


def _python_file(header: str = LICENSE_HEADER) -> str:
    body = "\n".join(f"    x{i} = {i}" for i in range(60))
    return (
        header
        + "\nimport os\n\n"
        + "def alpha():\n" + body + "\n\n"
        + "class Widget:\n    pass\n\n"
        + "def beta(a, b):\n    return a + b\n\n"
        + "def gamma():\n    return 3\n\n"
        + "# trailing sentinel line\n"
    )


class TestFileShape:
    def test_license_header_lines_kept_verbatim(self):
        # Round-4 finding C: the verbatim head default is now 3 lines (down from
        # 5) to shed gist bytes. The first lines still survive verbatim — the c2
        # fix's core (the model sees the header pattern to imitate) is intact —
        # but the 4th+ header lines are dropped at the default.
        gist = structural_gist(_python_file(), "read_file", 700)
        assert "# Copyright 2026 Example Corp." in gist
        assert "# SPDX-License-Identifier: Apache-2.0" in gist
        # 4th header line is beyond the default-3 head → not kept verbatim.
        assert "# Licensed under the Apache License, Version 2.0." not in gist

    def test_head_lines_env_reaches_full_header(self, monkeypatch):
        # Finding C keeps 5 reachable for tasks that must imitate a full header.
        monkeypatch.setenv("CONTEXT_GIST_HEAD_LINES", "5")
        gist = structural_gist(_python_file(), "read_file", 700)
        assert "# Copyright 2026 Example Corp." in gist
        assert "# SPDX-License-Identifier: Apache-2.0" in gist
        assert "# Licensed under the Apache License, Version 2.0." in gist

    def test_python_outline_lists_defs_and_classes(self):
        gist = structural_gist(_python_file(), "read_file", 700)
        assert "def alpha" in gist
        assert "class Widget" in gist
        # The outline is capped but at least the earliest names show.
        assert "outline:" in gist

    def test_line_and_char_counts_annotated(self):
        content = _python_file()
        gist = structural_gist(content, "read_file", 700)
        assert f"{len(content)} chars" in gist
        assert "lines" in gist

    def test_detects_file_shape_without_tool_name_via_shebang(self):
        content = "#!/usr/bin/env bash\nset -euo pipefail\n" + "echo hi\n" * 20
        gist = structural_gist(content, None, 700)
        assert "#!/usr/bin/env bash" in gist

    def test_markdown_headings_outlined(self):
        content = (
            "# Title\n\nsome intro prose here that is reasonably long.\n\n"
            "## Section One\n\nmore text\n\n## Section Two\n\nyet more text\n"
        ) + "filler line\n" * 10
        gist = structural_gist(content, "read_file", 700)
        assert "Title" in gist and "Section One" in gist


class TestJsonEnvelope:
    def test_surfaces_error_and_exit_code(self):
        payload = json.dumps({
            "success": False,
            "exit_code": 2,
            "error": "boom happened",
            "stdout": "line1\nline2\nline3\nline4",
        })
        gist = structural_gist(payload, "run_command", 700)
        assert "exit_code=2" in gist
        assert "success=" in gist  # False surfaced
        assert "boom happened" in gist

    def test_surfaces_inner_payload_peek(self):
        payload = json.dumps({"ok": True, "output": "alpha\nbeta\ngamma\ndelta"})
        gist = structural_gist(payload, "terminal", 700)
        assert "output" in gist
        assert "alpha" in gist  # first inner line peeked

    def test_json_array_counts_items(self):
        payload = json.dumps([{"a": 1}, {"a": 2}, {"a": 3}])
        gist = structural_gist(payload, "search", 700)
        assert "3 items" in gist


class TestTerminalShape:
    def test_keeps_exit_code_and_tail(self):
        lines = ["$ pytest", "collecting..."] + [f"test_{i} ok" for i in range(40)]
        lines += ["FAILED test_last", "exit code: 1"]
        content = "\n".join(lines)
        gist = structural_gist(content, "terminal", 700)
        assert "exit=1" in gist
        assert "exit code: 1" in gist  # tail preserved

    def test_terminal_detected_by_exit_marker_without_tool_name(self):
        content = "doing work\n" * 30 + "process finished with exit code 137\n"
        gist = structural_gist(content, None, 700)
        assert "exit=137" in gist


class TestSearchShape:
    def test_match_count_and_paths(self):
        content = "\n".join([
            "src/a.py:12: def foo():",
            "src/a.py:88: foo()",
            "src/b.py:4: import foo",
            "lib/c.py:9: foo = 1",
        ])
        gist = structural_gist(content, "grep", 700)
        assert "4 matches" in gist
        assert "3 files" in gist
        assert "src/a.py" in gist and "lib/c.py" in gist


class TestBudgetAndRedaction:
    def test_budget_respected(self):
        big = "prose line number %d with some words\n" % 0 + "\n".join(
            f"line {i} lorem ipsum dolor sit amet" for i in range(500)
        )
        for budget in (120, 300, 700):
            gist = structural_gist(big, "read_file", budget)
            assert len(gist) <= budget, (budget, len(gist))

    def test_secret_in_header_is_redacted(self):
        secret = "ghp_1234567890abcdefABCDEFghijklmnopqrst"
        content = f"# token = {secret}\n" + "def f():\n    pass\n" * 20
        gist = structural_gist(content, "read_file", 700)
        assert secret not in gist  # full secret never survives
        assert "ghp_" in gist  # but the redacted prefix marker can

    def test_empty_content(self):
        assert structural_gist("", "read_file", 700) == "(empty)"
        assert structural_gist("   \n  ", "terminal", 700) == "(empty)"


class TestDeterminism:
    def test_same_input_same_output(self):
        content = _python_file()
        a = structural_gist(content, "read_file", 700)
        b = structural_gist(content, "read_file", 700)
        assert a == b

    def test_default_budget_env(self, monkeypatch):
        # Round-4 finding C: default budget cut 700 → 450 to shed gist bytes.
        monkeypatch.delenv("CONTEXT_GIST_BUDGET_CHARS", raising=False)
        assert default_budget_chars() == 450
        monkeypatch.setenv("CONTEXT_GIST_BUDGET_CHARS", "250")
        assert default_budget_chars() == 250
        # Hard cap applies.
        monkeypatch.setenv("CONTEXT_GIST_BUDGET_CHARS", "999999")
        assert default_budget_chars() == 4000
        # Garbage falls back to the default.
        monkeypatch.setenv("CONTEXT_GIST_BUDGET_CHARS", "not-a-number")
        assert default_budget_chars() == 450
