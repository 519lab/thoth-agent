"""Substrate context engine — Phase 2a skeleton with verbatim retrieval handles.

This is the top of a planned eviction ladder that unifies the conversation
context system with the substrate/session stores (design: see
``plans/substrate-context-engine.md``). The end-state idea is to treat the live
context window as a *cache* over the durable Postgres session store rather than
a window we lossily summarise: evicted content becomes a small, actionable stub
carrying a **retrieval handle**, while the byte-exact original stays in the
``messages`` table and the substrate indexes it for proactive recall.

Phased delivery — this file is **Phase 2a only**:

  * **2a (this PR)** — the engine skeleton plus the two verbatim retrieval
    handles (``context_expand`` / ``context_grep``) over the session store. No
    eviction happens yet: every compaction concern (``should_compress``,
    ``compress``, token tracking, lifecycle) delegates to an internal
    ``ContextCompressor`` organ so behaviour is byte-identical to today's
    default engine. The only *new* observable surface is the two tools.
  * **2b (next)** — Tier-0 structural prune + Tier-1 evict-to-substrate with
    stubs, boundary/threshold triggers, batching, hot-page protection
    (plan §2.2). The handles minted here are the retrieval side of that
    mechanism.
  * **2c** — substrate integration (eviction slices, proactive recall
    surfacing, dereference → reinforce).
  * **2d** — Tier-2 absorption: the ``ContextCompressor`` organ becomes the
    degraded/overflow fallback rather than the mechanism.

Composition, not inheritance: the engine *owns* a ``ContextCompressor``
("Tier-2 organ") instead of subclassing it, so 2b/2c/2d can layer eviction on
top without entangling the two lifecycles. Because ``run_agent`` /
``conversation_compression`` read and write the compaction state directly on
the engine object (``last_prompt_tokens``, ``threshold_tokens``,
``compression_count``, plus internal ``_last_compress_aborted`` etc.), the
token-state fields are exposed as properties that read/write straight through
to the inner compressor — one source of truth — and any other attribute access
falls through to it via ``__getattr__``. That is what makes 2a delegation
byte-identical: there is no second copy of the state to drift.

Handle format (documented in the tool schemas so the model can reuse handles it
sees in stubs/recall in later phases):

    sid:<session_id>#m:<message_id>

A handle is exactly the ``(session_id, message_id)`` pair the session store is
keyed on — eviction copies nothing, it just points at the row that is already
there. Retrieval uses the same sync bridge and byte-exact fetch primitives as
``tools/session_search_tool.py`` (the ``_SyncDB`` / ``_ensure_sync_db`` pattern
that solved the async-port trap in PR #201).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextEngine

logger = logging.getLogger(__name__)

# Handle grammar: ``sid:<session_id>#m:<message_id>``. Session ids are the
# timestamp+hex slugs the agent mints (``20260703_120000_ab12cd``) plus the
# odd test/legacy id, so allow any non-``#`` run for the session part and a
# plain integer for the message id.
_HANDLE_RE = re.compile(r"^sid:(?P<sid>[^#]+)#m:(?P<mid>\d+)$")

# Default ceiling on the content returned for a single expanded message. Large
# tool results in the live store run to ~88KB; returning them whole would blow
# the very context budget eviction exists to protect. Capped content carries an
# explicit marker + the handle so the model can page a narrower slice back in.
_DEFAULT_EXPAND_MAX_CHARS = 20_000

# How far up the ``parent_session_id`` chain ``context_grep`` walks to build the
# current conversation's lineage. Bounded so a pathological/cyclic chain can't
# turn a grep into an unbounded walk (the walk is a handful of ~1ms
# ``get_session`` reads, well within the "cheaply available" bar from plan §2.4).
_LINEAGE_MAX_DEPTH = 25


def _make_handle(session_id: str, message_id: Any) -> str:
    """Build the canonical handle string for a ``(session_id, message_id)`` pair."""
    return f"sid:{session_id}#m:{message_id}"


def _parse_handle(handle: str) -> Optional[tuple]:
    """Parse a handle into ``(session_id, message_id:int)``.

    Returns ``None`` for anything that isn't a well-formed handle so callers
    can turn it into a clean error string rather than raising.
    """
    if not isinstance(handle, str):
        return None
    m = _HANDLE_RE.match(handle.strip())
    if not m:
        return None
    return m.group("sid"), int(m.group("mid"))


class _DBUnavailable(Exception):
    """Raised internally when the session store can't be reached; converted to a
    clean error-string tool result (never propagated to the loop)."""


class SubstrateContextEngine(ContextEngine):
    """Context engine that caches conversation context over the session store.

    Phase 2a: a thin skeleton. All compaction behaviour delegates to an internal
    :class:`ContextCompressor` (the "Tier-2 organ"); the only new surface is the
    ``context_expand`` / ``context_grep`` retrieval tools that fetch byte-exact
    stored messages by handle. Tier-0/Tier-1 eviction, stubs, and substrate
    slices arrive in Phase 2b/2c (see module docstring and plan §2.2).
    """

    def __init__(
        self,
        *args: Any,
        expand_max_chars: int = _DEFAULT_EXPAND_MAX_CHARS,
        **kwargs: Any,
    ) -> None:
        # Own a ContextCompressor with the exact constructor contract the
        # default engine uses — every positional/keyword arg the caller would
        # pass to ContextCompressor is forwarded verbatim, so a substrate engine
        # is a drop-in for the compressor at construction time.
        self._compressor = ContextCompressor(*args, **kwargs)
        self._expand_max_chars = max(1_000, int(expand_max_chars))
        # Current conversation session id, captured at on_session_start (and on
        # each compression-driven rotation). context_grep scopes to this id's
        # lineage; context_expand doesn't need it (handles carry their own sid).
        self._session_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "substrate"

    # ------------------------------------------------------------------
    # Token / compaction state — delegated to the inner compressor so there is
    # exactly one source of truth. run_agent + conversation_compression read
    # AND write these directly on the engine object, so each is a read/write
    # property straight through to the organ.
    # ------------------------------------------------------------------

    @property
    def last_prompt_tokens(self) -> int:
        return self._compressor.last_prompt_tokens

    @last_prompt_tokens.setter
    def last_prompt_tokens(self, value: int) -> None:
        self._compressor.last_prompt_tokens = value

    @property
    def last_completion_tokens(self) -> int:
        return self._compressor.last_completion_tokens

    @last_completion_tokens.setter
    def last_completion_tokens(self, value: int) -> None:
        self._compressor.last_completion_tokens = value

    @property
    def last_total_tokens(self) -> int:
        return self._compressor.last_total_tokens

    @last_total_tokens.setter
    def last_total_tokens(self, value: int) -> None:
        self._compressor.last_total_tokens = value

    @property
    def threshold_tokens(self) -> int:
        return self._compressor.threshold_tokens

    @threshold_tokens.setter
    def threshold_tokens(self, value: int) -> None:
        self._compressor.threshold_tokens = value

    @property
    def context_length(self) -> int:
        return self._compressor.context_length

    @context_length.setter
    def context_length(self, value: int) -> None:
        self._compressor.context_length = value

    @property
    def compression_count(self) -> int:
        return self._compressor.compression_count

    @compression_count.setter
    def compression_count(self, value: int) -> None:
        self._compressor.compression_count = value

    @property
    def threshold_percent(self) -> float:
        return self._compressor.threshold_percent

    @threshold_percent.setter
    def threshold_percent(self, value: float) -> None:
        self._compressor.threshold_percent = value

    @property
    def protect_first_n(self) -> int:
        return self._compressor.protect_first_n

    @protect_first_n.setter
    def protect_first_n(self, value: int) -> None:
        self._compressor.protect_first_n = value

    @property
    def protect_last_n(self) -> int:
        return self._compressor.protect_last_n

    @protect_last_n.setter
    def protect_last_n(self, value: int) -> None:
        self._compressor.protect_last_n = value

    def __getattr__(self, name: str) -> Any:
        """Fall through unknown attribute reads to the inner compressor.

        This exposes the compressor's private compaction state
        (``_last_compress_aborted``, ``_last_summary_error``,
        ``_previous_summary``, ``abort_on_summary_failure``, ``quiet_mode`` …)
        on the engine, which ``conversation_compression.compress_context``
        reads via ``getattr(agent.context_compressor, ...)``. Only fires when
        normal lookup misses, so the properties/methods above always win.
        """
        # Guard against recursion before ``_compressor`` is assigned in __init__.
        if name == "_compressor":
            raise AttributeError(name)
        return getattr(self._compressor, name)

    # ------------------------------------------------------------------
    # Core compaction interface — pure delegation in Phase 2a.
    # ------------------------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        return self._compressor.update_from_response(usage)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return self._compressor.should_compress(prompt_tokens)

    def compress(self, messages, *args, **kwargs):
        # Signature-compatible with ContextCompressor.compress (current_tokens,
        # focus_topic, force). compress_context already tolerates engines with
        # narrower signatures via a TypeError fallback, but forwarding *args
        # keeps the manual /compress focus + force paths working unchanged.
        return self._compressor.compress(messages, *args, **kwargs)

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        return self._compressor.should_compress_preflight(messages)

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        return self._compressor.has_content_to_compress(messages)

    def update_model(self, *args: Any, **kwargs: Any) -> None:
        return self._compressor.update_model(*args, **kwargs)

    def get_status(self) -> Dict[str, Any]:
        return self._compressor.get_status()

    # ------------------------------------------------------------------
    # Lifecycle — capture the session id (for context_grep scoping) and
    # otherwise delegate to the organ.
    # ------------------------------------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        # Called at session start AND on every compression-driven rotation
        # (conversation_compression passes boundary_reason="compression" plus
        # the new session_id), so this stays current across rotations. Handles
        # already minted keep resolving because they carry their own sid.
        self._session_id = session_id or self._session_id
        try:
            self._compressor.on_session_start(session_id, **kwargs)
        except Exception as exc:  # organ default is a no-op; never fatal here
            logger.debug("inner compressor on_session_start raised: %s", exc)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        return self._compressor.on_session_end(session_id, messages)

    def on_session_reset(self) -> None:
        return self._compressor.on_session_reset()

    # ------------------------------------------------------------------
    # Retrieval tools (the Phase-2a surface).
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Two engine-owned retrieval tools, in flat OpenAI schema form.

        agent_init wraps each as ``{"type": "function", "function": <schema>}``
        before appending to ``agent.tools`` — matching every other engine/tool
        that ships a flat ``{name, description, parameters}`` schema.
        """
        handle_doc = (
            "A handle is `sid:<session_id>#m:<message_id>` — exactly the "
            "(session_id, message_id) pair the durable session store is keyed "
            "on. You will see handles like this inside eviction stubs and "
            "recall-surfaced memory once eviction is enabled; pass them here to "
            "page the byte-exact original back in."
        )
        return [
            {
                "name": "context_expand",
                "description": (
                    "Fetch the byte-exact stored message for a context handle "
                    "(optionally with surrounding messages). Use this to page "
                    "back in content that was evicted from the live context — "
                    "the full original lives verbatim in the Postgres session "
                    "store, not a lossy summary.\n\n"
                    f"{handle_doc}\n\n"
                    "Returns the message's role, exact content, and tool_name "
                    "(for tool results). With window>0 it also returns the "
                    "immediately surrounding messages as `neighbors`, each with "
                    "its own handle. Very large content is capped (a marker "
                    "notes the truncation and repeats the handle so you can "
                    "re-expand a narrower ask). No LLM call — a ~1ms DB fetch."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {
                            "type": "string",
                            "description": (
                                "The context handle to expand, in the form "
                                "`sid:<session_id>#m:<message_id>`."
                            ),
                        },
                        "window": {
                            "type": "integer",
                            "description": (
                                "Number of messages to also return on EACH side "
                                "of the handle's message (default 0 = just the "
                                "one message). Clamped to [0, 20]. Use 1-2 to "
                                "recover the immediate exchange around an "
                                "evicted turn."
                            ),
                            "default": 0,
                        },
                    },
                    "required": ["handle"],
                },
            },
            {
                "name": "context_grep",
                "description": (
                    "Full-text search over THIS conversation's own message "
                    "history (the current session plus its parent-session "
                    "lineage) in the durable session store, including messages "
                    "that were evicted from the live context. Returns matched "
                    "messages with a highlighted snippet and a `handle` you can "
                    "pass to context_expand for the byte-exact full content.\n\n"
                    f"{handle_doc}\n\n"
                    "Scoped to the current conversation only — it will not "
                    "surface other sessions (use session_search for that). "
                    "Postgres tsvector FTS: multi-word queries are AND by "
                    "default; use OR / quoted phrases / prefix* as needed. No "
                    "LLM call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Full-text query to find in this conversation's "
                                "history (keywords, quoted phrases, or boolean "
                                "expressions)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max matches to return (default 5, max 20)."
                            ),
                            "default": 5,
                        },
                    },
                    "required": ["pattern"],
                },
            },
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch context_expand / context_grep. Always returns a JSON string.

        ``db`` and ``current_session_id`` may be injected via kwargs (tests do
        this); in production ``tool_executor`` only passes ``messages=`` so the
        DB is built here from the process-wide pool exactly as session_search
        does, and the session id comes from ``on_session_start``.
        """
        args = args or {}
        try:
            db = self._resolve_db(kwargs.get("db"))
        except _DBUnavailable as exc:
            return json.dumps({"error": str(exc)})

        if name == "context_expand":
            return self._handle_expand(db, args)
        if name == "context_grep":
            return self._handle_grep(db, args, kwargs.get("current_session_id"))
        return json.dumps({"error": f"Unknown context engine tool: {name}"})

    # ------------------------------------------------------------------
    # Retrieval implementation
    # ------------------------------------------------------------------

    def _resolve_db(self, injected: Any):
        """Return a sync-callable session DB, or raise :class:`_DBUnavailable`.

        Reuses the exact plumbing session_search uses: wrap an injected raw
        async ``_AsyncSessionDB`` (or leave an already-sync one alone) via
        ``_ensure_sync_db``; otherwise bootstrap one from the process-wide pool.
        """
        from tools.session_search_tool import _SyncDB, _ensure_sync_db

        if injected is not None:
            return _ensure_sync_db(injected)
        try:
            import thoth_db
            thoth_db.pool()  # raises RuntimeError if the pool isn't initialised
            from thoth_state import _AsyncSessionDB
            return _SyncDB(_AsyncSessionDB())
        except RuntimeError as exc:
            raise _DBUnavailable(
                "session store unavailable (Postgres pool not initialised) — "
                "cannot resolve context handles"
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise _DBUnavailable(f"session store unavailable: {exc}") from exc

    def _cap_content(self, content: Any, handle: str) -> tuple:
        """Return ``(text, truncated: bool)`` capped at ``_expand_max_chars``.

        Content under the cap is returned byte-exact (the whole point of
        verbatim handles). Over the cap, we keep the head and append a marker
        that names the dropped byte count and repeats the handle so the model
        can page a narrower slice back in later.
        """
        if content is None:
            return "", False
        if not isinstance(content, str):
            content = str(content)
        if len(content) <= self._expand_max_chars:
            return content, False
        dropped = len(content) - self._expand_max_chars
        marker = (
            f"\n\n[context_expand: content truncated — {dropped:,} of "
            f"{len(content):,} chars omitted. The full text remains stored "
            f"verbatim; re-request a narrower slice with "
            f'context_expand("{handle}") or read the source artifact directly.]'
        )
        return content[: self._expand_max_chars] + marker, True

    def _shape_message(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Shape a message row into a result dict: {handle, role, content, tool_name?}."""
        handle = _make_handle(row.get("session_id"), row.get("id"))
        capped, truncated = self._cap_content(row.get("content"), handle)
        out: Dict[str, Any] = {
            "handle": handle,
            "role": row.get("role"),
            "content": capped,
        }
        if truncated:
            out["truncated"] = True
        if row.get("tool_name"):
            out["tool_name"] = row.get("tool_name")
        return out

    def _handle_expand(self, db, args: Dict[str, Any]) -> str:
        handle = args.get("handle")
        parsed = _parse_handle(handle)
        if not parsed:
            return json.dumps({
                "error": (
                    f"malformed handle {handle!r} — expected "
                    "'sid:<session_id>#m:<message_id>'"
                )
            })
        session_id, message_id = parsed

        window = args.get("window", 0)
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 0
        window = max(0, min(window, 20))

        try:
            view = db.get_messages_around(session_id, message_id, window=window)
        except Exception as exc:
            logger.warning("context_expand get_messages_around failed: %s", exc, exc_info=True)
            return json.dumps({"error": f"failed to load message for handle {handle}: {exc}"})

        rows = view.get("window") or []
        if not rows:
            return json.dumps({
                "error": (
                    f"no message found for handle {handle} "
                    "(session or message id not in the session store)"
                )
            })

        anchor = next((r for r in rows if r.get("id") == message_id), None)
        if anchor is None:
            # get_messages_around only returns a non-empty window when the
            # anchor exists, so this is defensive.
            return json.dumps({
                "error": f"no message found for handle {handle}"
            })

        result = self._shape_message(anchor)
        if window > 0:
            neighbors = [self._shape_message(r) for r in rows if r.get("id") != message_id]
            if neighbors:
                result["neighbors"] = neighbors
        return json.dumps(result, ensure_ascii=False, default=str)

    def _session_lineage(self, db, session_id: str) -> List[str]:
        """Walk ``parent_session_id`` from ``session_id`` up to the lineage root.

        Returns ``[session_id, parent, grandparent, …]`` (current first),
        bounded by ``_LINEAGE_MAX_DEPTH`` and cycle-guarded. Any DB hiccup
        degrades gracefully to whatever lineage was gathered so far (at minimum
        the current session), matching plan §2.4's "else current session".
        """
        lineage: List[str] = []
        seen: set = set()
        cur = session_id
        while cur and cur not in seen and len(lineage) < _LINEAGE_MAX_DEPTH:
            seen.add(cur)
            lineage.append(cur)
            try:
                meta = db.get_session(cur)
            except Exception as exc:
                logger.debug("context_grep lineage walk stopped at %s: %s", cur, exc)
                break
            cur = (meta or {}).get("parent_session_id")
        return lineage

    def _handle_grep(self, db, args: Dict[str, Any], current_session_id: Optional[str]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return json.dumps({"error": "context_grep requires a non-empty pattern"})
        pattern = pattern.strip()

        limit = args.get("limit", 5)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))

        session_id = current_session_id or self._session_id
        if not session_id:
            return json.dumps({
                "error": (
                    "context_grep has no current session to scope to "
                    "(engine not attached to a session yet)"
                )
            })

        lineage = set(self._session_lineage(db, session_id))

        try:
            # search_messages has no session filter, so over-fetch and scope to
            # the lineage in-process. Include tool output — evicted tool results
            # are the primary reason to grep this conversation's own history.
            raw = db.search_messages(
                query=pattern,
                role_filter=["user", "assistant", "tool"],
                limit=max(limit * 5, 25),
            )
        except Exception as exc:
            logger.warning("context_grep search_messages failed: %s", exc, exc_info=True)
            return json.dumps({"error": f"search failed: {exc}"})

        matches = []
        for row in raw or []:
            if row.get("session_id") not in lineage:
                continue
            handle = _make_handle(row.get("session_id"), row.get("id"))
            entry = {
                "handle": handle,
                "role": row.get("role"),
                "snippet": row.get("snippet") or "",
            }
            if row.get("tool_name"):
                entry["tool_name"] = row.get("tool_name")
            matches.append(entry)
            if len(matches) >= limit:
                break

        return json.dumps({
            "pattern": pattern,
            "matches": matches,
            "count": len(matches),
        }, ensure_ascii=False, default=str)


__all__ = ["SubstrateContextEngine"]
