"""Translate legacy ``HERMES_*`` env-var names to ``THOTH_*`` in a .env file.

Once the in-memory ``HERMES_* <-> THOTH_*`` mirror is gone, a .env imported
from a legacy ``~/.hermes`` install would still carry ``HERMES_X=...`` lines
that no ``os.environ.get("THOTH_X")`` reader would ever see. The importer runs
this translation on the copied .env so the migrated install is THOTH_*-native.

Rules:

* Rewrite only assignment lines: ``HERMES_<NAME>=...`` → ``THOTH_<NAME>=...``,
  including ``export HERMES_<NAME>=...``. Leading whitespace and the entire
  right-hand side (value, inline comments, trailing whitespace) are preserved
  verbatim.
* Comments (``# ...``), blank lines, and lines that already use ``THOTH_`` are
  left untouched, so the rewrite is **idempotent** — running it twice changes
  nothing on the second pass.
* Order, blank lines, comments, and the trailing newline are preserved.

The rewrite is atomic (temp file + ``atomic_replace``) so a crash mid-write
can never leave a half-translated .env, and a symlinked .env keeps its link.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from utils import atomic_replace

# An assignment line whose variable name starts with HERMES_ (case-insensitive
# prefix; the rest of the name is the captured suffix). Captures:
#   1: leading whitespace + optional ``export ``
#   2: the variable-name suffix after the HERMES_ prefix
#   3: the ``=`` and everything after it (value + any trailing content)
_ASSIGN_RE = re.compile(
    r"^(\s*(?:export\s+)?)HERMES_([A-Za-z0-9_]*)(\s*=.*)$",
    re.IGNORECASE | re.DOTALL,
)


def translate_env_file_legacy_to_thoth(path: str | os.PathLike[str]) -> int:
    """Rewrite ``HERMES_*`` assignment lines to ``THOTH_*`` in the file at *path*.

    Returns the number of lines rewritten (0 when there is nothing to translate
    or the file is already THOTH_*-native). Comments, blank lines, ordering, and
    the trailing newline are preserved. Idempotent and atomic.
    """
    p = Path(path)
    if not p.is_file():
        return 0

    for enc in ("utf-8", "latin-1"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return 0

    lines = text.splitlines(keepends=True)
    changed = 0
    out: list[str] = []
    for line in lines:
        m = _ASSIGN_RE.match(line)
        if m:
            out.append(f"{m.group(1)}THOTH_{m.group(2)}{m.group(3)}")
            changed += 1
        else:
            out.append(line)

    if changed == 0:
        return 0

    new_text = "".join(out)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".env_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return changed
