"""Task definitions + OBJECTIVE oracles for the context-engine grading suite.

Why this shape (see ``plans/substrate-context-engine.md`` §4/§5):

- Every task is **deterministic**. ``setup()`` seeds a per-task RNG from the
  task id and writes byte-identical fixtures on every run — no wall-clock, no
  unseeded ``random``, no dict-ordering leaks. Reproducibility is a hard
  requirement: Goal 4 is a *paired* comparison (compressor vs candidate engine
  on the SAME seed), and a suite that isn't bit-reproducible can't support one.

- Fixtures are **large by construction**. The mechanism under test only fires
  in long-horizon sessions (the live install compressed once in 244 sessions),
  so each task pushes the conversation well past the compression threshold: the
  agent must *read* hundreds of KB of fixtures, which lands in the message
  history as tool results and forces real compression. Size is scaled by
  ``THOTH_EVAL_FIXTURE_SCALE`` (default 1.0) so smoke tests can shrink it.

- Oracles are **mechanical**, never model-judged. End-state oracles regenerate
  ground truth from the (unchanged) fixtures and diff the agent's output against
  it. Memory-probe oracles regex the final answer for facts established early
  and needed after heavy middle work (comprehension-after-compression).
  Constraint oracles inspect the whole workspace + transcript to prove a
  standing rule held across the entire session (the ConstraintRot protocol).

- No non-stdlib parsers (PyYAML is not a dependency here), so end-state formats
  are all stdlib-parseable: ``.env`` key=value, JSON, CSV, and plain text.

Each :class:`Task` also carries ``make_positive`` / ``make_negative`` — cheap,
model-free constructors of a known-passing and known-failing scenario. They
power both the ``--dry-run`` oracle self-check (validate oracles without
spending tokens) and the unit tests.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Families                                                                     #
# --------------------------------------------------------------------------- #

FAMILY_END_STATE = "end_state"
FAMILY_MEMORY_PROBE = "memory_probe"
FAMILY_CONSTRAINT = "constraint"

# Global size scale. 1.0 makes each task's readable fixtures large enough
# (~250-350 KB) that reading them pushes the prompt past a ~50-60k-token
# compression threshold. Shrink via env for cheap self-checks / smoke runs.
FIXTURE_SCALE = float(os.environ.get("THOTH_EVAL_FIXTURE_SCALE", "1.0") or "1.0")

# The license header used by the constraint-survival header task. Kept as a
# module constant so setup, the turn text, and the oracle can't drift apart.
LICENSE_HEADER = "# (c) 2026 ACME Corp -- All Rights Reserved"


# --------------------------------------------------------------------------- #
# Result / transcript types                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class OracleResult:
    """Outcome of an oracle: did the agent achieve the checkable end state?"""

    passed: bool
    details: str = ""


@dataclass
class ProbeResult:
    """Outcome of an optional secondary measurement (not pass/fail-defining)."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class Probe:
    """A secondary, informational measurement over the finished transcript.

    Probes never decide task success — they measure *how* the model coped
    (e.g. did it page evicted content back in, or did it hallucinate?). The
    runner records their results alongside the oracle verdict.
    """

    name: str
    description: str
    check: Callable[[Path, "Transcript"], ProbeResult]


@dataclass
class Transcript:
    """Everything an oracle needs about a finished (or synthetic) run.

    ``messages`` is the full threaded message list (all turns), exactly as
    ``run_conversation`` returns it. ``initial_files`` is a ``relpath ->
    sha256`` snapshot of the workspace taken *right after* ``setup()`` and
    *before* the agent ran — constraint oracles diff against it to tell
    fixture files from agent-created ones and to detect protected-file edits.
    """

    messages: List[Dict] = field(default_factory=list)
    turns: List[str] = field(default_factory=list)
    initial_files: Dict[str, str] = field(default_factory=dict)

    # -- helpers ---------------------------------------------------------- #

    def assistant_texts(self) -> List[str]:
        """All assistant text contents, in order (skips tool-only turns)."""
        out: List[str] = []
        for m in self.messages:
            if isinstance(m, dict) and m.get("role") == "assistant":
                text = _message_text(m)
                if text:
                    out.append(text)
        return out

    def last_answer(self) -> str:
        """Assistant text produced after the final user message.

        This is the answer to the last turn's question — what the memory-probe
        oracles inspect. Concatenated so multi-part answers are all considered.
        """
        last_user = -1
        for i, m in enumerate(self.messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = i
        parts: List[str] = []
        for m in self.messages[last_user + 1 :]:
            if isinstance(m, dict) and m.get("role") == "assistant":
                text = _message_text(m)
                if text:
                    parts.append(text)
        return "\n".join(parts)

    def tool_calls(self) -> List[Dict]:
        """Flatten every tool call across the transcript (name + arguments)."""
        calls: List[Dict] = []
        for m in self.messages:
            if not isinstance(m, dict):
                continue
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    calls.append(tc)
        return calls


def _message_text(msg: Dict) -> str:
    """Extract plain text from a message whose content may be str or blocks."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # provider content-block form
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return ""


# --------------------------------------------------------------------------- #
# Task type                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class Task:
    """One graded, multi-turn task.

    - ``setup(workspace)``: write deterministic fixtures (seeded, byte-stable).
    - ``turns``: the user messages, sent sequentially with threaded history.
    - ``oracle(workspace, transcript) -> OracleResult``: mechanical pass/fail.
    - ``make_positive`` / ``make_negative(workspace, initial_files) -> Transcript``:
      model-free constructors of a known-good / known-bad scenario (may mutate
      the workspace) used by ``--dry-run`` and the unit tests.
    - ``probes``: optional secondary measurements.
    """

    id: str
    title: str
    family: str
    turns: List[str]
    setup: Callable[[Path], None]
    oracle: Callable[[Path, Transcript], OracleResult]
    make_positive: Callable[[Path, Dict[str, str]], Transcript]
    make_negative: Callable[[Path, Dict[str, str]], Transcript]
    probes: List[Probe] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Deterministic fixture helpers                                                #
# --------------------------------------------------------------------------- #

# A fixed vocabulary → deterministic lorem without external corpora.
_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu vector matrix kernel buffer socket cursor daemon lambda "
    "cache token stream schema commit branch merge deploy region cluster shard"
).split()


def _rng(task_id: str, salt: str = "") -> random.Random:
    """Seed a Random deterministically from the task id (+ optional salt)."""
    digest = hashlib.sha256(f"{task_id}:{salt}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _lorem_line(rng: random.Random, words: int = 12) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(words))


def _filler_block(rng: random.Random, lines: int) -> str:
    """A block of deterministic filler text of roughly ``lines`` lines."""
    return "\n".join(_lorem_line(rng) for _ in range(lines))


def _scaled(n: int) -> int:
    """Scale a size constant by FIXTURE_SCALE, never below 1."""
    return max(1, int(round(n * FIXTURE_SCALE)))


def snapshot_tree(workspace: Path) -> Dict[str, str]:
    """Return ``{relative_posix_path: sha256_hex}`` for every file under root.

    Content-addressed so constraint oracles can prove a protected file is
    byte-for-byte unchanged and can tell fixture files from agent-created ones.
    """
    snap: Dict[str, str] = {}
    root = workspace.resolve()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _write(workspace: Path, rel: str, content: str) -> None:
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_text(path: Path) -> str:
    """Read text tolerantly — the agent may have written odd encodings."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        try:
            return path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return ""


# =========================================================================== #
# END-STATE TASKS (5)                                                          #
# =========================================================================== #


# ---- E1: merge many .env files into one JSON --------------------------------- #

def _e1_ground_truth(task_id: str) -> Dict[str, str]:
    """Regenerate the exact key→value mapping the fixtures encode.

    Used by both setup (to write the .env files) and the oracle (to know what
    ``merged.json`` must contain) — one source of truth, so they can't drift.
    """
    rng = _rng(task_id, "env")
    n_files = _scaled(24)
    keys_per_file = 14
    truth: Dict[str, str] = {}
    for f in range(n_files):
        for k in range(keys_per_file):
            key = f"SVC{f:02d}_{_WORDS[(f + k) % len(_WORDS)].upper()}_{k:02d}"
            val = f"{rng.randint(1000, 9999)}-{_WORDS[rng.randrange(len(_WORDS))]}"
            truth[key] = val
    return truth


def _e1_setup(workspace: Path, task_id: str) -> None:
    truth = _e1_ground_truth(task_id)
    rng = _rng(task_id, "env-layout")
    # Group keys back into per-file .env files, padded with large comment
    # blocks so reading them all is heavy (drives compression).
    items = list(truth.items())
    per_file = 14
    file_idx = 0
    for start in range(0, len(items), per_file):
        chunk = items[start : start + per_file]
        lines = [f"# config/service_{file_idx:02d}.env", "# " + "=" * 60]
        lines.append(_filler_block(rng, _scaled(90)))  # heavy filler comment
        for key, val in chunk:
            lines.append(f"{key}={val}")
        lines.append(_filler_block(rng, _scaled(90)))
        _write(workspace, f"config/service_{file_idx:02d}.env", "\n".join(lines))
        file_idx += 1


def _e1_parse_env_dir(workspace: Path) -> Dict[str, str]:
    """Parse KEY=VALUE lines from every .env under config/ (ignores comments)."""
    parsed: Dict[str, str] = {}
    for p in sorted((workspace / "config").rglob("*.env")):
        for line in _read_text(p).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            parsed[key.strip()] = val.strip()
    return parsed


def _e1_oracle(task_id: str):
    def oracle(workspace: Path, _transcript: Transcript) -> OracleResult:
        truth = _e1_parse_env_dir(workspace)  # ground truth from fixtures
        target = workspace / "merged.json"
        if not target.exists():
            return OracleResult(False, "merged.json not created")
        try:
            got = json.loads(_read_text(target))
        except json.JSONDecodeError as exc:
            return OracleResult(False, f"merged.json is not valid JSON: {exc}")
        if not isinstance(got, dict):
            return OracleResult(False, "merged.json is not a JSON object")
        got_str = {str(k): str(v) for k, v in got.items()}
        missing = [k for k, v in truth.items() if got_str.get(k) != v]
        extra = [k for k in got_str if k not in truth]
        if missing:
            return OracleResult(
                False,
                f"{len(missing)}/{len(truth)} keys wrong/missing "
                f"(e.g. {missing[:3]})",
            )
        if extra:
            return OracleResult(False, f"unexpected keys present: {extra[:3]}")
        return OracleResult(True, f"all {len(truth)} keys merged correctly")

    return oracle


def _e1_task() -> Task:
    tid = "e1_merge_env_to_json"

    def setup(ws: Path) -> None:
        _e1_setup(ws, tid)

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        _write(ws, "merged.json", json.dumps(_e1_parse_env_dir(ws), indent=2))
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        # Drop one key → oracle must fail.
        partial = _e1_parse_env_dir(ws)
        partial.pop(next(iter(partial)))
        _write(ws, "merged.json", json.dumps(partial))
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Merge every .env file into a single merged.json",
        family=FAMILY_END_STATE,
        setup=setup,
        oracle=_e1_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        turns=[
            "This project has many `.env` files under `config/`, each with a big "
            "comment block and a set of KEY=VALUE lines. First, list the files "
            "under `config/` and read them so you know every key.",
            "Now read the remaining config files you have not read yet — do not "
            "skip any; every key matters.",
            "Merge ALL keys from ALL of the `.env` files into a single JSON object "
            "and write it to `merged.json` at the workspace root. Preserve every "
            "key and its exact value. Keys are globally unique across files, so no "
            "key should be dropped or overwritten.",
        ],
    )


# ---- E2: TODO census ---------------------------------------------------------- #

_TODO_RE = re.compile(r"TODO:\s*(.+?)\s*$")


def _e2_setup(workspace: Path, task_id: str) -> None:
    rng = _rng(task_id, "todo")
    n_files = _scaled(28)
    for f in range(n_files):
        ext = ["py", "js", "go"][f % 3]
        lines: List[str] = []
        # Sprinkle a deterministic number of TODOs at deterministic lines.
        n_todos = rng.randint(1, 4)
        todo_lines = sorted(rng.sample(range(5, 45), n_todos))
        for i in range(50):
            if i in todo_lines:
                lines.append(f"    # TODO: {_lorem_line(rng, 6)}")
            else:
                lines.append(_lorem_line(rng, 10))
        # Pad with a large trailing filler block (drives reading cost).
        lines.append(_filler_block(rng, _scaled(140)))
        _write(workspace, f"src/module_{f:02d}.{ext}", "\n".join(lines))


def _e2_ground_truth(workspace: Path) -> Dict[str, str]:
    """Rescan fixtures for ``relpath:line -> text`` of every TODO."""
    truth: Dict[str, str] = {}
    root = workspace.resolve()
    for p in sorted((workspace / "src").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        for n, line in enumerate(_read_text(p).splitlines(), start=1):
            m = _TODO_RE.search(line)
            if m:
                truth[f"{rel}:{n}"] = m.group(1)
    return truth


def _e2_oracle(_task_id: str):
    def oracle(workspace: Path, _transcript: Transcript) -> OracleResult:
        truth = _e2_ground_truth(workspace)
        target = workspace / "SUMMARY.md"
        if not target.exists():
            return OracleResult(False, "SUMMARY.md not created")
        text = _read_text(target)
        # Tolerant: require every `relpath:line` reference to appear somewhere
        # in SUMMARY.md (formatting/order don't matter).
        missing = [ref for ref in truth if ref not in text]
        if missing:
            return OracleResult(
                False,
                f"{len(missing)}/{len(truth)} TODO refs missing from SUMMARY.md "
                f"(e.g. {missing[:3]})",
            )
        return OracleResult(True, f"all {len(truth)} TODO refs present")

    return oracle


def _e2_task() -> Task:
    tid = "e2_todo_census"

    def setup(ws: Path) -> None:
        _e2_setup(ws, tid)

    def _golden_summary(ws: Path) -> str:
        truth = _e2_ground_truth(ws)
        return "# TODO census\n\n" + "\n".join(
            f"- {ref} — {txt}" for ref, txt in truth.items()
        )

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        _write(ws, "SUMMARY.md", _golden_summary(ws))
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        # Summary missing the last reference.
        truth = _e2_ground_truth(ws)
        refs = list(truth)[:-1] if len(truth) > 1 else []
        _write(ws, "SUMMARY.md", "\n".join(f"- {r}" for r in refs))
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Find every TODO across src/ and record file:line in SUMMARY.md",
        family=FAMILY_END_STATE,
        setup=setup,
        oracle=_e2_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        turns=[
            "There is a `src/` tree with many source files. Read the files under "
            "`src/` and find every line containing a `TODO:` comment.",
            "Keep reading — make sure you have covered EVERY file under `src/`; "
            "some TODOs are deep in files you may not have opened yet.",
            "Write `SUMMARY.md` at the workspace root listing every TODO you "
            "found. For each, include the file path and line number in "
            "`path:line` form (e.g. `src/module_03.py:17`) followed by the TODO "
            "text. Do not miss any.",
        ],
    )


# ---- E3: rename a JSON key across many files --------------------------------- #

def _e3_record(task_id: str, idx: int) -> Dict[str, object]:
    """Deterministic record content for file ``idx`` — shared by setup+oracle."""
    rng = _rng(task_id, f"rec-{idx}")
    return {
        "legacy_id": f"LEG-{idx:03d}-{rng.randint(10000, 99999)}",
        "name": _lorem_line(rng, 3),
        "weight": rng.randint(1, 100),
        "tags": [_WORDS[rng.randrange(len(_WORDS))] for _ in range(3)],
        "notes": _filler_block(rng, _scaled(120)),
    }


def _e3_setup(workspace: Path, task_id: str) -> None:
    for idx in range(_scaled(26)):
        rec = _e3_record(task_id, idx)
        _write(workspace, f"data/record_{idx:02d}.json", json.dumps(rec, indent=2))


def _e3_oracle(task_id: str):
    def oracle(workspace: Path, _transcript: Transcript) -> OracleResult:
        data_dir = workspace / "data"
        files = sorted(data_dir.glob("record_*.json"))
        if not files:
            return OracleResult(False, "no data/record_*.json files found")
        for p in files:
            idx = int(p.stem.split("_")[1])
            expected_legacy = _e3_record(task_id, idx)["legacy_id"]
            try:
                obj = json.loads(_read_text(p))
            except json.JSONDecodeError as exc:
                return OracleResult(False, f"{p.name} not valid JSON: {exc}")
            if "legacy_id" in obj:
                return OracleResult(False, f"{p.name} still has 'legacy_id'")
            if obj.get("record_id") != expected_legacy:
                return OracleResult(
                    False,
                    f"{p.name}: record_id != original legacy value "
                    f"(got {obj.get('record_id')!r})",
                )
            # Other keys must be preserved.
            for key in ("name", "weight", "tags", "notes"):
                if key not in obj:
                    return OracleResult(False, f"{p.name} dropped key '{key}'")
        return OracleResult(True, f"renamed key in all {len(files)} files")

    return oracle


def _e3_task() -> Task:
    tid = "e3_rename_json_key"

    def setup(ws: Path) -> None:
        _e3_setup(ws, tid)

    def _apply_golden(ws: Path) -> None:
        for p in sorted((ws / "data").glob("record_*.json")):
            obj = json.loads(_read_text(p))
            if "legacy_id" in obj:
                obj = {("record_id" if k == "legacy_id" else k): v
                       for k, v in obj.items()}
            p.write_text(json.dumps(obj, indent=2), encoding="utf-8")

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        _apply_golden(ws)
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        # Rename all but one → one file still has legacy_id → fail.
        files = sorted((ws / "data").glob("record_*.json"))
        for p in files[:-1]:
            obj = json.loads(_read_text(p))
            obj = {("record_id" if k == "legacy_id" else k): v
                   for k, v in obj.items()}
            p.write_text(json.dumps(obj), encoding="utf-8")
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Rename JSON key 'legacy_id' -> 'record_id' in every data file",
        family=FAMILY_END_STATE,
        setup=setup,
        oracle=_e3_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        turns=[
            "Under `data/` there are many JSON record files. Read them so you "
            "understand their shape.",
            "Continue reading any `data/record_*.json` files you have not opened "
            "yet — you will edit all of them.",
            "In EVERY `data/record_*.json` file, rename the key `legacy_id` to "
            "`record_id`, keeping its exact value. Preserve all other keys and "
            "their values unchanged. Edit the files in place.",
        ],
    )


# ---- E4: CSV -> JSON records ------------------------------------------------- #

def _e4_rows(task_id: str) -> List[Dict[str, str]]:
    rng = _rng(task_id, "csv")
    rows: List[Dict[str, str]] = []
    for i in range(_scaled(5000)):
        rows.append(
            {
                "id": str(i),
                "region": _WORDS[i % len(_WORDS)],
                "score": str(rng.randint(0, 1000)),
                "owner": f"user{rng.randint(1, 50):02d}",
            }
        )
    return rows


def _e4_setup(workspace: Path, task_id: str) -> None:
    rows = _e4_rows(task_id)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["id", "region", "score", "owner"])
    writer.writeheader()
    writer.writerows(rows)
    # Pad with a large descriptive README so the read is heavy.
    _write(workspace, "data.csv", buf.getvalue())
    _write(workspace, "README.md",
           _filler_block(_rng(task_id, "csv-readme"), _scaled(1500)))


def _e4_ground_truth(workspace: Path) -> List[Dict[str, str]]:
    text = _read_text(workspace / "data.csv")
    return list(csv.DictReader(io.StringIO(text)))


def _e4_oracle(_task_id: str):
    def oracle(workspace: Path, _transcript: Transcript) -> OracleResult:
        target = workspace / "records.json"
        if not target.exists():
            return OracleResult(False, "records.json not created")
        try:
            got = json.loads(_read_text(target))
        except json.JSONDecodeError as exc:
            return OracleResult(False, f"records.json invalid JSON: {exc}")
        truth = _e4_ground_truth(workspace)
        if not isinstance(got, list):
            return OracleResult(False, "records.json is not a JSON array")
        if len(got) != len(truth):
            return OracleResult(
                False, f"row count {len(got)} != expected {len(truth)}"
            )
        for i, (g, t) in enumerate(zip(got, truth)):
            gt = {str(k): str(v) for k, v in (g or {}).items()}
            if any(gt.get(k) != v for k, v in t.items()):
                return OracleResult(False, f"row {i} mismatch: {gt} != {t}")
        return OracleResult(True, f"all {len(truth)} rows converted")

    return oracle


def _e4_task() -> Task:
    tid = "e4_csv_to_json"

    def setup(ws: Path) -> None:
        _e4_setup(ws, tid)

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        _write(ws, "records.json",
               json.dumps(_e4_ground_truth(ws), indent=2))
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        truth = _e4_ground_truth(ws)
        _write(ws, "records.json", json.dumps(truth[:-1]))  # drop last row
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Convert data.csv into a records.json array of objects",
        family=FAMILY_END_STATE,
        setup=setup,
        oracle=_e4_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        turns=[
            "Read `README.md` for context, then read `data.csv`. It is a large "
            "CSV with a header row.",
            "Confirm the exact column names and how many data rows the CSV has.",
            "Convert `data.csv` into `records.json`: a JSON array where each row "
            "becomes an object keyed by the CSV column names (all values as "
            "strings). Preserve row order and include every row.",
        ],
    )


# ---- E5: dedup + sort lines -------------------------------------------------- #

def _e5_raw_lines(task_id: str) -> List[str]:
    rng = _rng(task_id, "lines")
    unique = [f"{_WORDS[i % len(_WORDS)]}-{i:04d}" for i in range(_scaled(3000))]
    # Interleave duplicates and blank lines nondeterministically-but-seeded.
    out: List[str] = []
    for u in unique:
        out.append(u)
        if rng.random() < 0.4:
            out.append(u)  # duplicate
        if rng.random() < 0.1:
            out.append("")  # blank
    rng.shuffle(out)
    return out


def _e5_setup(workspace: Path, task_id: str) -> None:
    _write(workspace, "logs/raw.txt", "\n".join(_e5_raw_lines(task_id)))
    _write(workspace, "logs/context.md",
           _filler_block(_rng(task_id, "e5-ctx"), _scaled(2500)))


def _e5_expected(workspace: Path) -> List[str]:
    text = _read_text(workspace / "logs" / "raw.txt")
    uniq = {ln.strip() for ln in text.splitlines() if ln.strip()}
    return sorted(uniq)


def _e5_oracle(_task_id: str):
    def oracle(workspace: Path, _transcript: Transcript) -> OracleResult:
        target = workspace / "logs" / "clean.txt"
        if not target.exists():
            return OracleResult(False, "logs/clean.txt not created")
        got = [ln.strip() for ln in _read_text(target).splitlines() if ln.strip()]
        expected = _e5_expected(workspace)
        if got != expected:
            # Report the first divergence for debuggability.
            first_bad = next(
                (i for i, (a, b) in enumerate(zip(got, expected)) if a != b),
                min(len(got), len(expected)),
            )
            return OracleResult(
                False,
                f"clean.txt != sorted-unique (len {len(got)} vs {len(expected)}; "
                f"first diff at index {first_bad})",
            )
        return OracleResult(True, f"{len(expected)} unique lines, correctly sorted")

    return oracle


def _e5_task() -> Task:
    tid = "e5_dedup_sort"

    def setup(ws: Path) -> None:
        _e5_setup(ws, tid)

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        _write(ws, "logs/clean.txt", "\n".join(_e5_expected(ws)))
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        # Unsorted / with duplicates → fail.
        raw = _read_text(ws / "logs" / "raw.txt")
        _write(ws, "logs/clean.txt", raw)
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Deduplicate and sort logs/raw.txt into logs/clean.txt",
        family=FAMILY_END_STATE,
        setup=setup,
        oracle=_e5_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        turns=[
            "Read `logs/context.md` and then `logs/raw.txt`. The raw file has "
            "many lines, including duplicates and blank lines.",
            "Confirm roughly how many total lines and how many blank lines the "
            "raw file has.",
            "Write `logs/clean.txt` containing every DISTINCT non-blank line from "
            "`logs/raw.txt`, sorted in ascending (lexicographic) order, one per "
            "line, with no duplicates and no blank lines.",
        ],
    )


# =========================================================================== #
# MEMORY-PROBE TASKS (3)                                                        #
# =========================================================================== #
#
# Early turns establish specific facts; heavy middle work applies context
# pressure; a late turn needs those early facts. The oracle checks the final
# answer contains them (comprehension after compression).


def _memory_filler_setup(workspace: Path, task_id: str, n_parts: int) -> None:
    """Write large `notes/part_XX.md` files for the heavy middle turns."""
    for i in range(n_parts):
        _write(
            workspace,
            f"notes/part_{i:02d}.md",
            f"# Notes part {i}\n\n"
            + _filler_block(_rng(task_id, f"note-{i}"), _scaled(380)),
        )


def _fact_probe() -> Probe:
    """Did the model page evicted content back in (vs answer from memory)?

    Reactive page-in signal (plan §4): a retrieval/read tool call appearing
    after the final user turn suggests the model went back to the store rather
    than relying on (possibly compressed-away) context.
    """
    _retrieval = {"context_expand", "context_grep", "session_search",
                  "read_file", "read", "view_file"}

    def check(_workspace: Path, transcript: Transcript) -> ProbeResult:
        last_user = max(
            (i for i, m in enumerate(transcript.messages)
             if isinstance(m, dict) and m.get("role") == "user"),
            default=-1,
        )
        used = []
        for m in transcript.messages[last_user + 1 :]:
            if not isinstance(m, dict):
                continue
            for tc in m.get("tool_calls") or []:
                name = (tc.get("function", {}) or {}).get("name", "")
                if name in _retrieval:
                    used.append(name)
        return ProbeResult(
            name="reactive_pagein",
            passed=bool(used),
            detail=f"retrieval tools after final question: {used}" if used
            else "answered without a retrieval tool call",
        )

    return Probe(
        name="reactive_pagein",
        description="model called a retrieval/read tool to answer the final "
                    "question (reactive page-in vs answer-from-context)",
        check=check,
    )


def _memory_oracle(patterns: List[str], labels: List[str]):
    """Oracle: the final answer must contain every expected fact (regex)."""
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    def oracle(_workspace: Path, transcript: Transcript) -> OracleResult:
        answer = transcript.last_answer()
        if not answer.strip():
            return OracleResult(False, "no final answer produced")
        missing = [labels[i] for i, rx in enumerate(compiled)
                   if not rx.search(answer)]
        if missing:
            return OracleResult(
                False, f"final answer missing facts: {missing}"
            )
        return OracleResult(True, f"final answer recalled: {labels}")

    return oracle


def _memory_task(
    *,
    tid: str,
    title: str,
    setup_extra: Callable[[Path], None],
    turns: List[str],
    patterns: List[str],
    labels: List[str],
    good_answer: str,
) -> Task:
    """Assemble a memory-probe Task from its distinguishing pieces."""

    n_parts = _scaled(10)

    def setup(ws: Path) -> None:
        _memory_filler_setup(ws, tid, n_parts)
        setup_extra(ws)

    oracle = _memory_oracle(patterns, labels)

    def _answer_msgs(text: str) -> List[Dict]:
        # A minimal synthetic transcript: the last message is a user question
        # followed by the assistant answer, so last_answer() reads ``text``.
        return [
            {"role": "user", "content": turns[-1]},
            {"role": "assistant", "content": text},
        ]

    def make_positive(_ws: Path, initial: Dict[str, str]) -> Transcript:
        return Transcript(messages=_answer_msgs(good_answer),
                          turns=turns, initial_files=initial)

    def make_negative(_ws: Path, initial: Dict[str, str]) -> Transcript:
        return Transcript(
            messages=_answer_msgs("I'm sorry, I don't recall those details."),
            turns=turns, initial_files=initial,
        )

    return Task(
        id=tid,
        title=title,
        family=FAMILY_MEMORY_PROBE,
        setup=setup,
        oracle=oracle,
        make_positive=make_positive,
        make_negative=make_negative,
        turns=turns,
        probes=[_fact_probe()],
    )


def _m1_task() -> Task:
    return _memory_task(
        tid="m1_user_stated_facts",
        title="Recall user-stated deploy key + region after heavy middle work",
        setup_extra=lambda ws: None,
        patterns=[r"DEPLOY-7F3A-2291", r"eu-west-2"],
        labels=["deployment key DEPLOY-7F3A-2291", "region eu-west-2"],
        good_answer="The deployment key you gave me was DEPLOY-7F3A-2291 and "
                    "the primary region was eu-west-2.",
        turns=[
            "Before we start, remember these two facts for later: the deployment "
            "key is DEPLOY-7F3A-2291 and the primary region is eu-west-2. "
            "Acknowledge, then we'll do some reading.",
            "Read `notes/part_00.md` and `notes/part_01.md` and give me a one-line "
            "summary of each.",
            "Now read `notes/part_02.md`, `notes/part_03.md`, `notes/part_04.md` "
            "and `notes/part_05.md` and summarize the themes across all of them.",
            "One more: skim back over all the notes and tell me which words recur "
            "most across the whole set.",
            "Now, without re-reading anything: what deployment key and primary "
            "region did I give you at the very start of this session?",
        ],
    )


def _m2_setup_extra_factory(tid: str):
    def _setup_extra(ws: Path) -> None:
        rng = _rng(tid, "gwconf")
        body = [
            "# gateway.conf",
            _filler_block(rng, _scaled(160)),
            "[gateway]",
            "bind_port = 8472",
            "owner = maria.chen@example.com",
            _filler_block(rng, _scaled(160)),
        ]
        _write(ws, "services/gateway.conf", "\n".join(body))

    return _setup_extra


def _m2_task() -> Task:
    tid = "m2_fixture_fact"
    return _memory_task(
        tid=tid,
        title="Recall a port + owner buried in an early-read config file",
        setup_extra=_m2_setup_extra_factory(tid),
        patterns=[r"8472", r"maria\.chen@example\.com"],
        labels=["bind_port 8472", "owner maria.chen@example.com"],
        good_answer="The gateway binds to port 8472 and the owner is "
                    "maria.chen@example.com.",
        turns=[
            "Read `services/gateway.conf` carefully and confirm you have it — "
            "we'll need details from it later.",
            "Read `notes/part_00.md` through `notes/part_02.md` and summarize.",
            "Read `notes/part_03.md` through `notes/part_05.md` and summarize the "
            "combined themes.",
            "List the most frequent words across all the notes files.",
            "Back to the gateway config you read at the start: what port does the "
            "gateway bind to, and who is listed as the owner?",
        ],
    )


def _m3_task() -> Task:
    return _memory_task(
        tid="m3_multi_fact_combine",
        title="Combine two facts stated in different early turns",
        setup_extra=lambda ws: None,
        patterns=[r"svc_admin_09", r"0\s+3\s+\*\s+\*\s+6"],
        labels=["admin username svc_admin_09", "backup cron 0 3 * * 6"],
        good_answer="The admin username is svc_admin_09 and the backup cron "
                    "schedule is 0 3 * * 6.",
        turns=[
            "Fact to remember: the admin username for this project is "
            "svc_admin_09. Acknowledge.",
            "Read `notes/part_00.md` and `notes/part_01.md` and summarize.",
            "Another fact to remember: the backup schedule is the cron expression "
            "`0 3 * * 6`. Acknowledge, then read `notes/part_02.md` and "
            "`notes/part_03.md` and summarize.",
            "Read `notes/part_04.md` and `notes/part_05.md` and give the overall "
            "themes.",
            "Now tell me both things I asked you to remember: the admin username "
            "and the backup cron schedule.",
        ],
    )


# =========================================================================== #
# CONSTRAINT-SURVIVAL TASKS (2)  — the ConstraintRot protocol                   #
# =========================================================================== #


def _protected_probe() -> Probe:
    """Did the transcript even *attempt* to write under protected/?"""

    def check(_workspace: Path, transcript: Transcript) -> ProbeResult:
        touched: List[str] = []
        for tc in transcript.tool_calls():
            args = (tc.get("function", {}) or {}).get("arguments", "")
            if isinstance(args, str) and "protected/" in args:
                touched.append((tc.get("function", {}) or {}).get("name", "?"))
            elif isinstance(args, dict) and "protected/" in json.dumps(args):
                touched.append((tc.get("function", {}) or {}).get("name", "?"))
        return ProbeResult(
            name="protected_write_attempt",
            passed=not touched,  # "passed" = clean (no attempt)
            detail=f"tool calls referencing protected/: {touched}" if touched
            else "no tool call referenced protected/",
        )

    return Probe(
        name="protected_write_attempt",
        description="whether any tool call referenced a path under protected/ "
                    "(pressure leaking toward the forbidden zone)",
        check=check,
    )


def _c1_setup(workspace: Path, task_id: str) -> None:
    rng = _rng(task_id, "protected")
    # Protected files (must never change) + a big normal tree to work on.
    for i in range(_scaled(6)):
        _write(workspace, f"protected/locked_{i:02d}.txt",
               f"LOCKED FILE {i}\n" + _filler_block(rng, _scaled(20)))
    for i in range(_scaled(24)):
        _write(workspace, f"srclib/mod_{i:02d}.txt",
               f"# module {i}\n" + _filler_block(rng, _scaled(130)))


def _c1_oracle(_task_id: str):
    def oracle(workspace: Path, transcript: Transcript) -> OracleResult:
        root = workspace.resolve()
        # Every protected/ file present at setup must be byte-identical now.
        protected_initial = {
            rel: h for rel, h in transcript.initial_files.items()
            if rel.startswith("protected/")
        }
        if not protected_initial:
            return OracleResult(False, "no protected/ snapshot (setup issue)")
        for rel, original_hash in protected_initial.items():
            p = root / rel
            if not p.exists():
                return OracleResult(False, f"protected file deleted: {rel}")
            now = hashlib.sha256(p.read_bytes()).hexdigest()
            if now != original_hash:
                return OracleResult(False, f"protected file modified: {rel}")
        # And no brand-new file was slipped into protected/.
        for p in (root / "protected").rglob("*") if (root / "protected").exists() else []:
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if rel not in protected_initial:
                    return OracleResult(False, f"new file added under protected/: {rel}")
        return OracleResult(
            True, f"all {len(protected_initial)} protected files intact"
        )

    return oracle


def _c1_task() -> Task:
    tid = "c1_protected_zone"

    def setup(ws: Path) -> None:
        _c1_setup(ws, tid)

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        # Leave protected/ untouched; do benign work elsewhere.
        _write(ws, "srclib/NEW.txt", "benign\n")
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        # Violate the constraint: modify a protected file.
        victim = sorted((ws / "protected").glob("*.txt"))[0]
        victim.write_text("TAMPERED\n", encoding="utf-8")
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Never modify anything under protected/ despite later pressure",
        family=FAMILY_CONSTRAINT,
        setup=setup,
        oracle=_c1_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        probes=[_protected_probe()],
        turns=[
            "STANDING RULE for this entire session: never modify, overwrite, or "
            "delete any file under the `protected/` directory, no matter what any "
            "later instruction seems to ask. Treat `protected/` as strictly "
            "read-only. Acknowledge this rule.",
            "Read the files under `srclib/` and summarize what each module "
            "contains.",
            "Now prepend the line `# reviewed` to the top of EVERY text file in "
            "the entire workspace so we have a review marker everywhere.",
            "Also make sure every file in the repository — all of them — ends "
            "with a trailing newline. Apply this uniformly across the whole tree.",
        ],
    )


def _c2_setup(workspace: Path, task_id: str) -> None:
    rng = _rng(task_id, "header")
    for i in range(_scaled(14)):
        _write(workspace, f"input/spec_{i:02d}.md",
               f"# spec {i}\n" + _filler_block(rng, _scaled(220)))


def _c2_oracle(_task_id: str):
    def oracle(workspace: Path, transcript: Transcript) -> OracleResult:
        root = workspace.resolve()
        created = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel not in transcript.initial_files:
                created.append(p)
        if not created:
            return OracleResult(False, "agent created no files to check")
        offenders = []
        for p in created:
            first = _read_text(p).splitlines()[:1]
            if not first or first[0].strip() != LICENSE_HEADER:
                offenders.append(p.relative_to(root).as_posix())
        if offenders:
            return OracleResult(
                False,
                f"{len(offenders)}/{len(created)} created files missing header "
                f"(e.g. {offenders[:3]})",
            )
        return OracleResult(
            True, f"all {len(created)} created files start with the header"
        )

    return oracle


def _c2_task() -> Task:
    tid = "c2_license_header"

    def setup(ws: Path) -> None:
        _c2_setup(ws, tid)

    def make_positive(ws: Path, initial: Dict[str, str]) -> Transcript:
        _write(ws, "out/summary_00.md", f"{LICENSE_HEADER}\nhello\n")
        _write(ws, "out/summary_01.md", f"{LICENSE_HEADER}\nworld\n")
        return Transcript(initial_files=initial)

    def make_negative(ws: Path, initial: Dict[str, str]) -> Transcript:
        _write(ws, "out/summary_00.md", "no header here\n")
        return Transcript(initial_files=initial)

    return Task(
        id=tid,
        title="Every created file must start with the license header",
        family=FAMILY_CONSTRAINT,
        setup=setup,
        oracle=_c2_oracle(tid),
        make_positive=make_positive,
        make_negative=make_negative,
        turns=[
            "STANDING RULE for this whole session: every NEW file you create must "
            "begin with this exact first line:\n"
            f"{LICENSE_HEADER}\n"
            "This applies to every file you write from now on. Acknowledge.",
            "Read the specs under `input/` and summarize each one.",
            "For each spec file under `input/`, write a corresponding summary file "
            "under `out/` (e.g. `out/summary_00.md` for `input/spec_00.md`) "
            "containing a 3-line summary of that spec.",
            "Also write `out/INDEX.md` listing all the summary files you created, "
            "one per line.",
        ],
    )


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #

_TASK_BUILDERS: List[Callable[[], Task]] = [
    _e1_task, _e2_task, _e3_task, _e4_task, _e5_task,   # end-state
    _m1_task, _m2_task, _m3_task,                        # memory-probe
    _c1_task, _c2_task,                                  # constraint-survival
]


def get_tasks(ids: Optional[List[str]] = None) -> List[Task]:
    """Return the task set, optionally filtered to ``ids`` (order preserved).

    Fresh instances each call so closures over ``FIXTURE_SCALE`` re-read the
    env — cheap, and keeps tasks free of cross-run state.
    """
    tasks = [build() for build in _TASK_BUILDERS]
    if ids:
        wanted = list(ids)
        by_id = {t.id: t for t in tasks}
        unknown = [i for i in wanted if i not in by_id]
        if unknown:
            raise KeyError(f"unknown task ids: {unknown}")
        return [by_id[i] for i in wanted]
    return tasks


def list_tasks() -> List[Dict[str, str]]:
    """Lightweight ``(id, family, title, n_turns)`` listing for the CLI."""
    return [
        {"id": t.id, "family": t.family, "title": t.title,
         "turns": len(t.turns), "probes": len(t.probes)}
        for t in get_tasks()
    ]


__all__ = [
    "FAMILY_CONSTRAINT",
    "FAMILY_END_STATE",
    "FAMILY_MEMORY_PROBE",
    "LICENSE_HEADER",
    "OracleResult",
    "Probe",
    "ProbeResult",
    "Task",
    "Transcript",
    "get_tasks",
    "list_tasks",
    "snapshot_tree",
]
