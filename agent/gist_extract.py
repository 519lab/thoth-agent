"""Content-aware structural gist extraction (Round 3, Tier A).

Round 2 of the substrate-context-engine plan shipped restorable eviction: a
cooled/evicted tool result is replaced in-context by a small stub carrying a
retrieval handle, with the byte-exact original kept in Postgres. But the *gist*
inside that stub was a builder shortcut — the first ~120 chars of the evicted
body. The project owner rejected that shortcut, and the graded suite proved the
harm: task ``c2_license_header`` (every created file must start with a license
header) failed 2/3 under the cooling engine, because the model imitates the
early file reads it can still see, and a first-120-chars prefix gist threw the
header pattern away. The model kept "performing" from whatever survived in the
gist — so the gist has to carry what it needs to keep performing.

This module is the free, deterministic half of the Round-3 answer: given an
evicted tool-result body, produce a *content-aware structural* gist — no LLM, no
DB, no network, pure function of the input. It detects the shape of the content
and preserves the load-bearing parts of that shape:

  * **JSON tool-result envelopes** — parse and surface ``error`` / ``exit_code``
    / ``success`` / ``status`` plus a peek at the inner payload.
  * **File-read-like text** — the FIRST ~5 lines VERBATIM (this is the c2 fix:
    license headers, SPDX tags, shebangs, module docstrings live here), then a
    structure outline (def/class/function names, markdown headings, top-level
    config keys), then the last ~2 lines; annotated with line/char counts.
  * **Terminal output** — exit code if present, first ~3 + last ~3 lines, counts.
  * **Search-result listings** — match count + the file paths hit.
  * **Generic prose** — first + last lines + counts.

Everything is passed through :func:`agent.redact.redact_sensitive_text` before
return, so no secret leaks into the persisted stub or the substrate slice. The
result is bounded by a char budget (``CONTEXT_GIST_BUDGET_CHARS``, default ~700,
hard-capped) with the head placed first so it survives truncation.

Determinism is a contract: the same ``(content, tool_name, budget)`` always maps
to the same gist. No clocks, no randomness, no dict-iteration-order dependence.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

# --- Budget -----------------------------------------------------------------
# Round-4 forensic finding C: round-3's informative gists cost ~+45k tokens/task
# (gist bytes × turns). Default cut 700 → 450 chars: still fits a short verbatim
# head + an outline + a tail, but every stub reclaims more of a multi-KB result.
# ``CONTEXT_GIST_BUDGET_CHARS`` overrides (hard-capped below).
_BUDGET_DEFAULT = 450
_BUDGET_HARD_CAP = 4_000  # absolute ceiling regardless of env — a gist is a gist
_BUDGET_FLOOR = 80        # below this the shape labels themselves don't fit

# --- Structural extraction sizes (the plan's "~5 lines", "~3 lines" etc.) ---
# The file verbatim head is env-tunable (``CONTEXT_GIST_HEAD_LINES``) — see
# ``_file_head_lines()``. Round-4 finding C cut its default 5 → 3 to shed gist
# bytes; 5 stays reachable for tasks that must imitate a full license header.
_FILE_HEAD_LINES_DEFAULT = 3
_FILE_HEAD_LINES_MAX = 20  # sane ceiling — a "gist" head is not the whole file
_FILE_TAIL_LINES = 2
_TERM_HEAD_LINES = 3
_TERM_TAIL_LINES = 3
_GENERIC_HEAD_LINES = 3
_GENERIC_TAIL_LINES = 2
_OUTLINE_MAX = 12
_HEADINGS_MAX = 6
_SEARCH_PATHS_MAX = 15

# --- Tool-name → shape hints (strong signal; content heuristics back them up) -
_FILE_TOOLS = frozenset({
    "read_file", "read", "cat", "view", "open", "view_file", "fs_read",
    "get_file", "readfile", "file_read", "read_many_files",
})
_TERM_TOOLS = frozenset({
    "terminal", "bash", "shell", "run", "exec", "command", "run_command",
    "sh", "execute_command", "run_terminal_cmd", "run_shell",
})
_SEARCH_TOOLS = frozenset({
    "grep", "search", "glob", "find", "ripgrep", "rg", "search_files",
    "file_search", "codebase_search", "session_search", "grep_search",
})

# JSON keys that mark a dict as a tool-result *envelope* (vs an arbitrary blob).
_ENVELOPE_STATUS_KEYS = (
    "error", "exit_code", "exitcode", "returncode", "return_code",
    "success", "ok", "status", "code",
)
_ENVELOPE_PAYLOAD_KEYS = (
    "stdout", "output", "content", "result", "results", "data",
    "matches", "message", "stderr", "text", "body",
)

_EXIT_RE = re.compile(
    r"(?:exit(?:\s*code|_code)?|return\s*code|returncode)\s*[:=]?\s*(-?\d{1,5})",
    re.IGNORECASE,
)
# A "path:line:" match line, as ripgrep / grep -n emit.
_MATCH_LINE_RE = re.compile(r"^(\S[^:\n]*?):(\d+):")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
_CLASS_RE = re.compile(r"^\s*class\s+(\w+)")
_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)")
_TOPLEVEL_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):(?:\s|$)")


# ---------------------------------------------------------------------------
# Budget helpers
# ---------------------------------------------------------------------------

def _resolve_budget(budget_chars: Any) -> int:
    """Clamp a requested budget into ``[_BUDGET_FLOOR, _BUDGET_HARD_CAP]``."""
    try:
        b = int(budget_chars)
    except (TypeError, ValueError):
        b = _BUDGET_DEFAULT
    if b <= 0:
        b = _BUDGET_DEFAULT
    return max(_BUDGET_FLOOR, min(b, _BUDGET_HARD_CAP))


def default_budget_chars() -> int:
    """The runtime gist budget, from ``CONTEXT_GIST_BUDGET_CHARS`` (hard-capped)."""
    return _resolve_budget(os.environ.get("CONTEXT_GIST_BUDGET_CHARS", _BUDGET_DEFAULT))


def _file_head_lines() -> int:
    """Verbatim-head line count for file gists, from ``CONTEXT_GIST_HEAD_LINES``.

    Round-4 finding C: default 3 (down from 5) to shed gist bytes, clamped to
    ``[1, _FILE_HEAD_LINES_MAX]``. Set the env to 5+ for tasks that must imitate
    a full file header (e.g. license/SPDX blocks) — the c2 header fix stays
    reachable, it is just no longer paid on every stub by default.
    """
    try:
        n = int(os.environ.get("CONTEXT_GIST_HEAD_LINES", _FILE_HEAD_LINES_DEFAULT))
    except (TypeError, ValueError):
        n = _FILE_HEAD_LINES_DEFAULT
    if n <= 0:
        n = _FILE_HEAD_LINES_DEFAULT
    return max(1, min(n, _FILE_HEAD_LINES_MAX))


def _cap(text: str, budget: int) -> str:
    """Hard-cap ``text`` to ``budget`` chars, appending a single ellipsis."""
    if len(text) <= budget:
        return text
    keep = max(0, budget - 1)
    return text[:keep].rstrip() + "…"


# ---------------------------------------------------------------------------
# Shape classification
# ---------------------------------------------------------------------------

def _try_json(text: str) -> Optional[Any]:
    """Parse ``text`` as JSON iff it looks like a JSON object/array, else None."""
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, (dict, list)) else None


def _classify_shape(text: str, tool_name: Optional[str]) -> str:
    """Return one of ``file`` / ``terminal`` / ``search`` / ``generic``.

    Tool-name hints win when present (they are unambiguous); otherwise fall back
    to content heuristics. JSON is handled by the caller before this runs.
    """
    t = (tool_name or "").strip().lower()
    if t in _SEARCH_TOOLS:
        return "search"
    if t in _TERM_TOOLS:
        return "terminal"
    if t in _FILE_TOOLS:
        return "file"

    lines = text.split("\n")
    # search-like: several "path:line:" match lines.
    if sum(1 for ln in lines[:50] if _MATCH_LINE_RE.match(ln)) >= 3:
        return "search"
    # file-like: shebang / license / SPDX in the first lines, or code structure.
    first_blob = "\n".join(lines[:5]).lower()
    if (
        lines and lines[0].startswith("#!")
        or "copyright" in first_blob
        or "spdx-license" in first_blob
        or "licensed under" in first_blob
    ):
        return "file"
    # exit-code marker without a file/search shape → terminal.
    if _EXIT_RE.search(text) is not None:
        return "terminal"
    if len(lines) >= 8 and _code_outline(lines):
        return "file"
    return "generic"


# ---------------------------------------------------------------------------
# Per-shape extractors (each returns a raw, pre-redaction gist string)
# ---------------------------------------------------------------------------

def _short_scalar(value: Any, cap: int) -> str:
    """One-line string form of a JSON scalar/short container, capped."""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            s = str(value)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:cap]


def _first_lines(text: str, n: int) -> str:
    """First ``n`` non-empty lines of ``text`` joined by ⏎, whitespace-flattened."""
    out: List[str] = []
    for ln in text.split("\n"):
        if ln.strip():
            out.append(ln.strip())
        if len(out) >= n:
            break
    return " ⏎ ".join(out)


def _gist_json(obj: Any, budget: int) -> str:
    """Surface a JSON tool-result envelope: status fields + inner payload peek."""
    if isinstance(obj, list):
        head = _short_scalar(obj[0], 220) if obj else ""
        return f"JSON array — {len(obj)} items" + (f"; first: {head}" if head else "")

    lines: List[str] = []
    status_bits: List[str] = []
    for k in _ENVELOPE_STATUS_KEYS:
        if k in obj and not isinstance(obj[k], (dict, list)):
            status_bits.append(f"{k}={_short_scalar(obj[k], 120)}")
    if status_bits:
        lines.append("JSON result — " + ", ".join(status_bits))
    else:
        lines.append("JSON object — keys: " + ", ".join(list(obj.keys())[:20]))

    # Peek at the first non-empty payload field so the model sees what came back.
    for pk in _ENVELOPE_PAYLOAD_KEYS:
        if pk in obj and obj[pk] not in (None, "", [], {}):
            inner = obj[pk]
            inner_str = inner if isinstance(inner, str) else _short_scalar(inner, 600)
            snippet = _first_lines(inner_str, 3)
            lines.append(f"{pk} ({len(inner_str)} chars): {snippet}")
            break
    return "\n".join(lines)


def _code_outline(lines: List[str]) -> str:
    """Structure outline: def/class/function names, headings, or top-level keys."""
    names: List[str] = []
    n_def = n_class = 0
    headings: List[str] = []
    top_keys: List[str] = []
    for ln in lines:
        m = _DEF_RE.match(ln)
        if m:
            n_def += 1
            if len(names) < _OUTLINE_MAX:
                names.append("def " + m.group(1))
            continue
        m = _CLASS_RE.match(ln)
        if m:
            n_class += 1
            if len(names) < _OUTLINE_MAX:
                names.append("class " + m.group(1))
            continue
        m = _FUNC_RE.match(ln)
        if m:
            if len(names) < _OUTLINE_MAX:
                names.append("function " + m.group(1))
            continue
        m = _HEADING_RE.match(ln)
        if m and len(headings) < _HEADINGS_MAX:
            headings.append(m.group(2).strip()[:48])
            continue
        # Column-0 key (YAML/TOML-ish config) — only meaningful at the top level.
        if ln and not ln[0].isspace():
            m = _TOPLEVEL_KEY_RE.match(ln)
            if m and len(top_keys) < _OUTLINE_MAX:
                top_keys.append(m.group(1))

    parts: List[str] = []
    if names:
        tail = ""
        total = n_def + n_class
        if total > len(names):
            tail = f" (+{total - len(names)} more)"
        parts.append(" | ".join(names) + tail)
    if headings:
        parts.append("headings: " + " › ".join(headings))
    if not names and not headings and top_keys:
        parts.append("keys: " + ", ".join(top_keys))
    return "  ".join(parts)


def _gist_file(text: str, budget: int) -> str:
    """First ``CONTEXT_GIST_HEAD_LINES`` lines VERBATIM (the c2 header fix; default
    3 per finding C), then outline, then tail."""
    lines = text.split("\n")
    n = len(lines)
    head_lines = _file_head_lines()
    out = [f"file — {n} lines, {len(text)} chars"]
    out.append("head:")
    out.extend(lines[:head_lines])  # VERBATIM — headers/licenses/shebangs
    outline = _code_outline(lines)
    if outline:
        out.append("outline: " + outline)
    if n > head_lines + _FILE_TAIL_LINES:
        out.append("tail:")
        out.extend(lines[-_FILE_TAIL_LINES:])
    return "\n".join(out)


def _gist_terminal(text: str, budget: int) -> str:
    """Exit code (if present) + first ~3 and last ~3 lines + counts."""
    lines = text.split("\n")
    n = len(lines)
    exit_code = _find_exit_code(text)
    hdr = f"terminal — {n} lines, {len(text)} chars"
    if exit_code is not None:
        hdr += f", exit={exit_code}"
    out = [hdr, "head:"]
    out.extend(lines[:_TERM_HEAD_LINES])
    if n > _TERM_HEAD_LINES + _TERM_TAIL_LINES:
        out.append("tail:")
        out.extend(lines[-_TERM_TAIL_LINES:])
    return "\n".join(out)


def _gist_search(text: str, budget: int) -> str:
    """Match count + the distinct file paths hit."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    paths: List[str] = []
    seen = set()
    n_matches = 0
    for ln in lines:
        m = _MATCH_LINE_RE.match(ln)
        if m:
            n_matches += 1
            p = m.group(1)
            if p not in seen:
                seen.add(p)
                paths.append(p)
        elif "/" in ln and " " not in ln.strip() and ":" not in ln:
            # bare file-path listing (e.g. glob/find output)
            p = ln.strip()
            if p not in seen:
                seen.add(p)
                paths.append(p)
    if n_matches == 0:
        n_matches = len(lines)
    out = [f"search — {n_matches} matches across {len(paths)} files"]
    if paths:
        shown = paths[:_SEARCH_PATHS_MAX]
        out.append("files: " + ", ".join(shown))
        if len(paths) > len(shown):
            out.append(f"(+{len(paths) - len(shown)} more files)")
    return "\n".join(out)


def _gist_generic(text: str, budget: int) -> str:
    """First + last lines + counts — the shape-agnostic fallback."""
    lines = text.split("\n")
    n = len(lines)
    out = [f"output — {n} lines, {len(text)} chars", "head:"]
    out.extend(lines[:_GENERIC_HEAD_LINES])
    if n > _GENERIC_HEAD_LINES + _GENERIC_TAIL_LINES:
        out.append("tail:")
        out.extend(lines[-_GENERIC_TAIL_LINES:])
    return "\n".join(out)


def _find_exit_code(text: str) -> Optional[int]:
    m = _EXIT_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def structural_gist(
    content: str,
    tool_name: Optional[str],
    budget_chars: int,
) -> str:
    """Deterministic, content-aware structural gist of a tool-result body.

    Detects the content shape and preserves its load-bearing parts (see the
    module docstring). Always redacted, always within ``budget_chars`` (hard
    cap applies), with the head placed first so it survives truncation — that
    ordering is the c2 license-header fix: whatever else is dropped, the first
    lines of a file read stay.

    Never raises: any unexpected input degrades to a generic first/last gist.
    """
    budget = _resolve_budget(budget_chars)
    text = content if isinstance(content, str) else ("" if content is None else str(content))
    if not text.strip():
        return "(empty)"

    try:
        obj = _try_json(text)
        if obj is not None and (isinstance(obj, list) or _is_envelope(obj)):
            raw = _gist_json(obj, budget)
        else:
            shape = _classify_shape(text, tool_name)
            raw = {
                "file": _gist_file,
                "terminal": _gist_terminal,
                "search": _gist_search,
            }.get(shape, _gist_generic)(text, budget)
    except Exception:  # pragma: no cover - defensive: gist must never break eviction
        raw = _gist_generic(text, budget)

    gist = redact_sensitive_text(raw).strip() or "(empty)"
    return _cap(gist, budget)


def _is_envelope(obj: Dict[str, Any]) -> bool:
    """A dict is an envelope if it carries any status OR payload marker key."""
    return any(k in obj for k in _ENVELOPE_STATUS_KEYS) or any(
        k in obj for k in _ENVELOPE_PAYLOAD_KEYS
    )


__all__ = ["structural_gist", "default_budget_chars"]
