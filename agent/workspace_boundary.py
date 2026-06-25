"""Workspace permission-boundary guard for file-mutating tools.

Gates ``write_file`` / ``patch`` calls whose target path resolves *outside* the
agent's active workspace root (``THOTH_ACTIVE_ROOT``).  Reads are never
inspected here.  Shell commands are gated separately in
``tools.approval.check_all_command_guards`` via ``detect_out_of_root_command``.

The boundary reuses the existing approval system
(``tools.approval.check_path_boundary_guard``): an interactive user is prompted
(once / session / always), a gateway user gets a blocking approval request, and
a non-interactive/headless context fails closed (deny).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Mirror tools/file_tools.py patch_tool path extraction (around line 855).
_V4A_FILE_RE = re.compile(
    r'^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$', re.MULTILINE
)

_GATED_TOOLS = {"write_file", "patch"}


def _extract_target_paths(tool_name: str, args: dict) -> list[str]:
    """Extract the paths a write_file / patch call would mutate.

    Mirrors the extraction in ``tools.file_tools.patch_tool`` so the boundary
    inspects exactly the paths the real write touches.  ``None``/empty entries
    are skipped.
    """
    paths: list[str] = []
    p = args.get("path")
    if p:
        paths.append(p)
    if tool_name == "patch":
        patch_text = args.get("patch")
        if patch_text:
            for m in _V4A_FILE_RE.finditer(patch_text):
                g = m.group(1).strip()
                if g:
                    paths.append(g)
    return paths


def maybe_require_workspace_boundary_approval(
    tool_name: str, args: dict, task_id: str
) -> Optional[str]:
    """Require approval before a write/patch lands outside the active root.

    Returns ``None`` when the call is allowed (inside the root, boundary
    disabled, or the user approved).  Returns a JSON tool-error string when the
    write is denied.  Only ``write_file`` / ``patch`` are inspected — every
    other tool (including all reads) returns ``None`` immediately.
    """
    if tool_name not in _GATED_TOOLS:
        return None
    try:
        from agent.file_safety import check_path_boundary, get_active_root
        from tools.file_tools import _resolve_path_for_task

        out_of_root: list[str] = []
        for p in _extract_target_paths(tool_name, args or {}):
            if not p:
                continue
            try:
                resolved = _resolve_path_for_task(p, task_id)
            except Exception:
                continue
            if check_path_boundary(str(resolved), "write") == "needs_approval":
                out_of_root.append(str(resolved))

        if not out_of_root:
            return None

        from tools.approval import check_path_boundary_guard
        from tools.terminal_tool import _get_approval_callback

        decision = check_path_boundary_guard(
            out_of_root, approval_callback=_get_approval_callback()
        )
        if decision.get("approved"):
            return None

        root = get_active_root()
        return json.dumps(
            {
                "error": (
                    f"Write denied: {', '.join(out_of_root)} outside the active "
                    f"workspace root {root}. Approve to proceed."
                )
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        # Fail closed: a guard failure on a write must never silently allow
        # the write through (mirrors model_tools.py's edit-approval guard).
        logger.warning("workspace boundary guard failed: %s", exc, exc_info=True)
        if tool_name in _GATED_TOOLS:
            return json.dumps(
                {"error": "Write denied: workspace boundary guard failed"},
                ensure_ascii=False,
            )
        return None
