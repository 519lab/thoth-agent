"""SubstrateMemoryProvider — Phase C built-in provider backed by the
substrate's L0 recall API.

Activation is gated by the ``HERMES_SUBSTRATE_RECALL`` env var (default
0 in Phase C). When disabled the provider registers (so the registration
path is exercised in CI/tests) but ``prefetch()`` returns ``""`` so the
foreground's ``<memory-context>`` block continues to come from the
existing built-in path. Flipping to ``1`` is a one-line follow-up PR
once the operator has validated the substrate path manually.

The provider is sync (matches Thoth's ``MemoryProvider`` ABC); it
bridges to the async ``recall()`` via ``recall_sync`` (which goes
through ``thoth_db.run_sync``). Failures are absorbed — substrate
errors never reach the Thoth call site (mirrors the Phase A hook
discipline).

The provider exposes one tool — ``substrate_recall_more`` — for the
model to call when the prefetched context didn't surface what it
needed. The tool uses a larger token budget and a wider time window
(default 1 week) than the per-turn prefetch.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider


_log = logging.getLogger("substrate.memory_provider")


# Env-var name controlling activation. Kept here (not in substrate.config)
# so the import surface stays Thoth-side; substrate.config reads the
# same name for its own enable flag.
_ENABLE_ENV_VAR = "HERMES_SUBSTRATE_RECALL"

# Default tool budget for ``substrate_recall_more`` — bigger than the
# per-turn prefetch budget because the model is explicitly asking for
# a deeper search.
_RECALL_MORE_TOKEN_BUDGET = 3000
_RECALL_MORE_DEFAULT_WINDOW_HOURS = 168  # 1 week


def _is_enabled() -> bool:
    # Default ON: this fork installs the substrate as the primary memory
    # backend; recall opting out makes sense for power users who want to
    # diff substrate-vs-built-in behavior, not as a normal default.
    # Set HERMES_SUBSTRATE_RECALL=0 to fall back to the upstream
    # built-in provider exclusively.
    raw = os.environ.get(_ENABLE_ENV_VAR, "1")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class SubstrateMemoryProvider(MemoryProvider):
    """Substrate-backed read-only memory provider.

    Writes already flow through the Phase A perception hooks
    (``commit_slice`` on every user message and assistant response),
    so ``sync_turn`` / ``on_session_end`` are deliberately no-ops.
    """

    def __init__(self) -> None:
        self._session_id: str = ""
        self._enabled: bool = False

    # -- Identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        # NOT "builtin" — that would conflict with the existing in-process
        # memory tool. "substrate" is recognised as built-in by
        # MemoryManager._BUILTIN_PROVIDER_NAMES so the one-external-provider
        # invariant doesn't reject this provider when an actual external
        # plugin (Honcho, etc.) is also configured.
        return "substrate"

    # -- Lifecycle -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the substrate has been booted.

        The substrate's boot is non-fatal to Thoth (Phase A §0) — if
        the substrate failed to boot, the provider is "unavailable"
        and Thoth continues with whatever else is registered.
        """
        from substrate import get_bound_substrate

        return get_bound_substrate() is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        # Read the env var at initialize time, not import time, so a
        # test can monkeypatch the env and re-create the provider to
        # flip the toggle.
        self._enabled = _is_enabled()
        if self._enabled:
            _log.info("substrate memory provider enabled for session %s", session_id)

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "Memory: persistent substrate is active. Prior conversation context "
            "is surfaced via the <memory-context> block when relevant; you may "
            "also call the substrate_recall_more tool to request expanded "
            "recall on a specific topic."
        )

    # -- Prefetch / recall ---------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._enabled:
            return ""
        if not query or not query.strip():
            return ""
        substrate = self._substrate_or_none()
        if substrate is None:
            return ""
        # Late import — keeps the provider's module-level imports light.
        try:
            from substrate.recall import recall_sync
        except Exception as exc:  # pragma: no cover — defensive only
            _log.warning("substrate recall import failed: %s", exc)
            return ""
        try:
            projection = recall_sync(
                substrate,
                query,
                session_id=session_id or self._session_id,
            )
        except Exception as exc:
            _log.warning("substrate recall failed: %s", exc)
            return ""
        return projection.text

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Phase C: no background prefetch. prefetch() is bounded by
        # RECALL_TIMEOUT_MS so the per-turn cost is predictable.
        return

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        # No-op: perception hooks (Phase A) already wrote both slices
        # for this turn. Calling commit_slice from here would double-write.
        return

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # No extraction step. The substrate is a perception log; the
        # Curator + future Reflector (Phase E) consolidate at write time.
        return

    # -- Tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [
            {
                "name": "substrate_recall_more",
                "description": (
                    "Expand substrate recall on a specific topic. Use when the "
                    "prefetched <memory-context> didn't surface what you need "
                    "and you want a deeper search across a wider time window."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic or keyword to search the substrate for.",
                        },
                        "time_window_hours": {
                            "type": "integer",
                            "description": (
                                "How far back to look, in hours. Default 168 (one week)."
                            ),
                        },
                    },
                    "required": ["topic"],
                },
            }
        ]

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        if tool_name != "substrate_recall_more":
            return ""
        topic = (args or {}).get("topic", "")
        if not topic:
            return "[substrate_recall_more: topic is required]"
        try:
            window_hours = int((args or {}).get(
                "time_window_hours", _RECALL_MORE_DEFAULT_WINDOW_HOURS
            ))
        except (TypeError, ValueError):
            window_hours = _RECALL_MORE_DEFAULT_WINDOW_HOURS
        substrate = self._substrate_or_none()
        if substrate is None:
            return "[substrate_recall_more: substrate not booted]"
        try:
            from substrate.recall import recall_sync
        except Exception as exc:  # pragma: no cover
            return f"[substrate_recall_more failed: {exc}]"
        try:
            projection = recall_sync(
                substrate,
                topic,
                session_id=self._session_id,
                time_window=timedelta(hours=window_hours),
                token_budget=_RECALL_MORE_TOKEN_BUDGET,
            )
        except Exception as exc:
            return f"[substrate_recall_more failed: {exc}]"
        return projection.text or "[no matching substrate slices]"

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _substrate_or_none():
        """Resolve the bound substrate without raising. Imports late so
        a substrate-import failure doesn't crash Thoth provider setup."""
        try:
            from substrate import get_bound_substrate
        except Exception:
            return None
        return get_bound_substrate()


__all__ = ["SubstrateMemoryProvider"]
