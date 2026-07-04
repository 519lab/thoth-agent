"""Context compression — extract the AIAgent methods that drive summarisation.

Three concerns live here:

* :func:`check_compression_model_feasibility` — startup probe of the
  configured auxiliary compression model.  Warns when the aux context
  window can't fit the main model's compression threshold; auto-lowers
  the session threshold when possible; hard-rejects auxes below
  ``MINIMUM_CONTEXT_LENGTH``.

* :func:`replay_compression_warning` — re-emit a stored warning through
  the gateway ``status_callback`` once it's wired up (the callback is
  set after :class:`AIAgent` construction).

* :func:`compress_context` — the actual compression call.  Runs the
  configured compressor, splits the SQLite session, rotates the
  session_id, notifies plugin context engines / memory providers, and
  returns the compressed message list and freshly-built system prompt.

* :func:`try_shrink_image_parts_in_messages` — image-too-large recovery
  helper that re-encodes ``data:image/...;base64,...`` parts at a smaller
  size so retries can fit under provider ceilings (Anthropic's 5 MB).

``run_agent`` keeps thin wrappers for each so existing call sites
(``self._compress_context(...)``) keep working.  Tests that exercise
these paths see no behavioural change.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from agent.model_metadata import estimate_request_tokens_rough
from agent.session_db_bridge import resolve_maybe_awaitable

logger = logging.getLogger(__name__)


def _emit_compression_telemetry(
    agent: Any,
    *,
    trigger: str,
    messages_before: int,
    messages_after: int,
    tokens_before: Optional[int],
    tokens_after: Optional[int],
    duration_s: float,
    aborted: bool,
    summary_fallback: bool = False,
    old_session_id: Optional[str] = None,
    new_session_id: Optional[str] = None,
) -> None:
    """Emit one ``context.compressed`` row, best-effort and guarded.

    Evidence-driven repair (round-4 forensic finding B, part 2): ~30% of
    ``context.compressed`` rows reported impossible NEGATIVE ``tokens_saved``
    because ``tokens_before`` and ``tokens_after`` were sampled on DIFFERENT
    bases — ``tokens_before`` came from the caller's ``approx_tokens`` (which,
    depending on the trigger path, was either the real ``last_prompt_tokens``
    or a messages-only ``estimate_messages_tokens_rough``), while
    ``tokens_after`` was a schema-inclusive ``estimate_request_tokens_rough``.
    When ``before`` was messages-only and ``after`` was schema-inclusive, the
    tool-schema overhead alone made ``after > before`` even though summarisation
    removed real content.

    Both sides are now sampled with ``estimate_request_tokens_rough`` at the
    call sites, so ``tokens_saved = before - after`` is measured on ONE
    consistent, schema-inclusive basis and cannot go negative for a genuine
    summarisation pass. This helper stays neutral to that (it just relays the
    values), and keeps the graceful no-op when ``agent.context_telemetry`` (the
    Phase-0b module) is absent from this worktree.
    """
    try:
        from agent import context_telemetry  # arrives with Phase-0b
    except ImportError:
        return
    emit = getattr(context_telemetry, "emit_compression_event", None)
    if not callable(emit):
        return
    try:
        emit(
            agent,
            trigger=trigger,
            messages_before=messages_before,
            messages_after=messages_after,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            duration_s=duration_s,
            aborted=aborted,
            summary_fallback=summary_fallback,
            old_session_id=old_session_id,
            new_session_id=new_session_id,
        )
    except Exception:  # telemetry must never break compression
        logger.debug("context.compressed emit failed", exc_info=True)


def check_compression_model_feasibility(agent: Any) -> None:
    """Warn at session start if the auxiliary compression model's context
    window is smaller than the main model's compression threshold.

    When the auxiliary model cannot fit the content that needs summarising,
    compression will either fail outright (the LLM call errors) or produce
    a severely truncated summary.

    Called during ``AIAgent.__init__`` so CLI users see the warning
    immediately (via ``_vprint``).  The gateway sets ``status_callback``
    *after* construction, so :func:`replay_compression_warning` re-sends
    the stored warning through the callback on the first
    ``run_conversation()`` call.
    """
    if not agent.compression_enabled:
        return
    try:
        from agent.auxiliary_client import (
            _resolve_task_provider_model,
            get_text_auxiliary_client,
        )
        from agent.model_metadata import (
            MINIMUM_CONTEXT_LENGTH,
            get_model_context_length,
        )

        client, aux_model = get_text_auxiliary_client(
            "compression",
            main_runtime=agent._current_main_runtime(),
        )
        # Best-effort aux provider label for the warning message. The
        # configured provider may be "auto", in which case we fall back
        # to the client's base_url hostname so the user can still tell
        # where the compression model is actually being called.
        try:
            _aux_cfg_provider, _, _, _, _ = _resolve_task_provider_model("compression")
        except Exception:
            _aux_cfg_provider = ""
        if client is None or not aux_model:
            if _aux_cfg_provider and _aux_cfg_provider != "auto":
                msg = (
                    "⚠ Configured auxiliary compression provider "
                    f"'{_aux_cfg_provider}' is unavailable — context "
                    "compression will drop middle turns without a summary. "
                    "Check auxiliary.compression in config.yaml and "
                    "reauthenticate that provider."
                )
            else:
                msg = (
                    "⚠ No auxiliary LLM provider configured — context "
                    "compression will drop middle turns without a summary. "
                    "Run `thoth setup` or set OPENROUTER_API_KEY."
                )
            agent._compression_warning = msg
            agent._emit_status(msg)
            logger.warning(
                "No auxiliary LLM provider for compression — "
                "summaries will be unavailable."
            )
            return

        aux_base_url = str(getattr(client, "base_url", ""))
        # ``client.api_key`` may be a callable (Azure Foundry Entra ID
        # bearer provider). The context-length resolver chain expects a
        # string, but it only needs a key for live catalogue probes
        # (provider model lists). For Entra clients the model-metadata
        # chain still resolves via models.dev + hardcoded family
        # fallbacks, which don't require auth — pass empty string rather
        # than minting a bearer JWT just to look up a context length.
        _raw_aux_key = getattr(client, "api_key", "")
        aux_api_key = "" if (callable(_raw_aux_key) and not isinstance(_raw_aux_key, str)) else str(_raw_aux_key or "")

        aux_context = get_model_context_length(
            aux_model,
            base_url=aux_base_url,
            api_key=aux_api_key,
            config_context_length=getattr(agent, "_aux_compression_context_length_config", None),
            # Each model must be resolved with its own provider so that
            # provider-specific paths (e.g. Bedrock static table, OpenRouter API)
            # are invoked for the correct client, not inherited from the main model.
            provider=(_aux_cfg_provider if _aux_cfg_provider and _aux_cfg_provider != "auto" else getattr(agent, "provider", "")),
            custom_providers=agent._custom_providers,
        )

        # Hard floor: the auxiliary compression model must have at least
        # MINIMUM_CONTEXT_LENGTH (64K) tokens of context.  The main model
        # is already required to meet this floor (checked earlier in
        # __init__), so the compression model must too — otherwise it
        # cannot summarise a full threshold-sized window of main-model
        # content.  Mirrors the main-model rejection pattern.
        if aux_context and aux_context < MINIMUM_CONTEXT_LENGTH:
            raise ValueError(
                f"Auxiliary compression model {aux_model} has a context "
                f"window of {aux_context:,} tokens, which is below the "
                f"minimum {MINIMUM_CONTEXT_LENGTH:,} required by Thoth "
                f"Agent.  Choose a compression model with at least "
                f"{MINIMUM_CONTEXT_LENGTH // 1000}K context (set "
                f"auxiliary.compression.model in config.yaml), or set "
                f"auxiliary.compression.context_length to override the "
                f"detected value if it is wrong."
            )

        threshold = agent.context_compressor.threshold_tokens
        if aux_context < threshold:
            # Auto-correct: lower the live session threshold so
            # compression actually works this session.  The hard floor
            # above guarantees aux_context >= MINIMUM_CONTEXT_LENGTH,
            # so the new threshold is always >= 64K.
            #
            # The compression summariser sends a single user-role
            # prompt (no system prompt, no tools) to the aux model, so
            # new_threshold == aux_context is safe: the request is
            # the raw messages plus a small summarisation instruction.
            old_threshold = threshold
            new_threshold = aux_context
            agent.context_compressor.threshold_tokens = new_threshold
            # Keep threshold_percent in sync so future main-model
            # context_length changes (update_model) re-derive from a
            # sensible number rather than the original too-high value.
            main_ctx = agent.context_compressor.context_length
            if main_ctx:
                agent.context_compressor.threshold_percent = (
                    new_threshold / main_ctx
                )
            safe_pct = int((aux_context / main_ctx) * 100) if main_ctx else 50
            # Build human-readable "model (provider)" labels for both
            # the main model and the compression model so users can
            # tell at a glance which provider each side is actually
            # using. When the configured provider is empty or "auto",
            # fall back to the client's base_url hostname.
            _main_model = getattr(agent, "model", "") or "?"
            _main_provider = getattr(agent, "provider", "") or ""
            _aux_provider_label = (
                _aux_cfg_provider
                if _aux_cfg_provider and _aux_cfg_provider != "auto"
                else ""
            )
            if not _aux_provider_label:
                try:
                    from urllib.parse import urlparse
                    _aux_provider_label = (
                        urlparse(aux_base_url).hostname or aux_base_url
                    )
                except Exception:
                    _aux_provider_label = aux_base_url or "auto"
            _main_label = (
                f"{_main_model} ({_main_provider})"
                if _main_provider
                else _main_model
            )
            _aux_label = f"{aux_model} ({_aux_provider_label})"
            msg = (
                f"⚠ Compression model {_aux_label} context is "
                f"{aux_context:,} tokens, but the main model "
                f"{_main_label}'s compression threshold was "
                f"{old_threshold:,} tokens. "
                f"Auto-lowered this session's threshold to "
                f"{new_threshold:,} tokens so compression can run.\n"
                f"  To make this permanent, edit config.yaml — either:\n"
                f"  1. Use a larger compression model:\n"
                f"       auxiliary:\n"
                f"         compression:\n"
                f"           model: <model-with-{old_threshold:,}+-context>\n"
                f"  2. Lower the compression threshold:\n"
                f"       compression:\n"
                f"         threshold: 0.{safe_pct:02d}"
            )
            agent._compression_warning = msg
            agent._emit_status(msg)
            logger.warning(
                "Auxiliary compression model %s has %d token context, "
                "below the main model's compression threshold of %d "
                "tokens — auto-lowered session threshold to %d to "
                "keep compression working.",
                aux_model,
                aux_context,
                old_threshold,
                new_threshold,
            )
    except ValueError:
        # Hard rejections (aux below minimum context) must propagate
        # so the session refuses to start.
        raise
    except Exception as exc:
        logger.debug(
            "Compression feasibility check failed (non-fatal): %s", exc
        )


def replay_compression_warning(agent: Any) -> None:
    """Re-send the compression warning through ``status_callback``.

    During ``__init__`` the gateway's ``status_callback`` is not yet
    wired, so ``_emit_status`` only reaches ``_vprint`` (CLI).  This
    method is called once at the start of the first
    ``run_conversation()`` — by then the gateway has set the callback,
    so every platform (Telegram, Discord, Slack, etc.) receives the
    warning.
    """
    msg = getattr(agent, "_compression_warning", None)
    if msg and agent.status_callback:
        try:
            agent.status_callback("lifecycle", msg)
        except Exception:
            pass


def compress_context(
    agent: Any,
    messages: list,
    system_message: str,
    *,
    approx_tokens: Optional[int] = None,
    task_id: str = "default",
    focus_topic: Optional[str] = None,
    force: bool = False,
) -> Tuple[list, str]:
    """Compress conversation context and split the session in SQLite.

    Args:
        agent: The owning :class:`AIAgent`.
        messages: Current message history (will be summarised).
        system_message: Current system prompt; rebuilt after compression.
        approx_tokens: Pre-compression token estimate, logged for ops.
        task_id: Tool task scope (used for clearing file-read dedup state).
        focus_topic: Optional focus string for guided compression — the
            summariser will prioritise preserving information related to
            this topic.  Inspired by Claude Code's ``/compact <focus>``.
        force: If True, bypass any active summary-failure cooldown.  Set
            by the manual ``/compress`` slash command so users can retry
            immediately after an auto-compress abort.  Auto-compress
            callers use the default ``False``.

    Returns:
        ``(compressed_messages, new_system_prompt)`` tuple.  When
        compression aborts (aux LLM failed to produce a usable summary),
        returns the original messages unchanged and the existing system
        prompt — the session is NOT rotated.  Callers should detect the
        no-op via ``len(returned) == len(input)`` and stop the retry loop.
    """
    # Lazy feasibility check — run the auxiliary-provider probe + context
    # length lookup just-in-time on the first compression attempt instead of
    # at AIAgent.__init__. Saves ~400ms cold off every short session that
    # never reaches the threshold (the vast majority of ``chat -q`` runs).
    # The check itself sets ``agent._compression_warning`` so the
    # status-callback replay machinery still emits the warning to the user
    # the first time it would matter.
    if not getattr(agent, "_compression_feasibility_checked", True):
        try:
            check_compression_model_feasibility(agent)
        finally:
            agent._compression_feasibility_checked = True

    _pre_msg_count = len(messages)
    _tel_started = time.monotonic()
    # Finding B(2): sample tokens_before on the SAME schema-inclusive basis as
    # tokens_after (estimate_request_tokens_rough, tool schemas included) rather
    # than the caller's path-dependent approx_tokens — otherwise savings can go
    # negative. Uses the pre-compression system prompt (old cache) so the delta
    # reflects the message-body summarisation, not a prompt swap.
    _tel_tokens_before = estimate_request_tokens_rough(
        messages,
        system_prompt=getattr(agent, "_cached_system_prompt", None) or "",
        tools=agent.tools or None,
    )
    logger.info(
        "context compression started: session=%s messages=%d tokens=~%s model=%s focus=%r",
        agent.session_id or "none", _pre_msg_count,
        f"{approx_tokens:,}" if approx_tokens else "unknown", agent.model,
        focus_topic,
    )
    agent._emit_status(
        "🗜️ Compacting context — summarizing earlier conversation so I can continue..."
    )

    # Notify external memory provider before compression discards context
    if agent._memory_manager:
        try:
            agent._memory_manager.on_pre_compress(messages)
        except Exception:
            pass

    try:
        compressed = agent.context_compressor.compress(messages, current_tokens=approx_tokens, focus_topic=focus_topic, force=force)
    except TypeError:
        # Plugin context engine with strict signature that doesn't accept
        # focus_topic / force — fall back to calling without them.
        compressed = agent.context_compressor.compress(messages, current_tokens=approx_tokens)

    # If compression aborted (aux LLM failed to produce a usable summary)
    # the compressor returns the input messages unchanged.  Surface the
    # error to the user, skip the session-rotation work entirely (no
    # session has logically ended), and let auto-compress callers detect
    # the no-op via len(returned) == len(input).
    if getattr(agent.context_compressor, "_last_compress_aborted", False):
        _err = getattr(agent.context_compressor, "_last_summary_error", None) or "unknown error"
        if getattr(agent, "_last_compression_summary_warning", None) != _err:
            agent._last_compression_summary_warning = _err
            agent._emit_warning(
                f"⚠ Compression aborted: {_err}. "
                "No messages were dropped — conversation continues unchanged. "
                "Run /compress to retry, or /new to start a fresh session."
            )
        _existing_sp = getattr(agent, "_cached_system_prompt", None)
        if not _existing_sp:
            _existing_sp = agent._build_system_prompt(system_message)
        # Aborted pass: no content removed, so before == after (tokens_saved 0 —
        # never negative). Consistent-basis sampling per finding B(2).
        _emit_compression_telemetry(
            agent,
            trigger="auto" if not force else "manual",
            messages_before=_pre_msg_count,
            messages_after=len(messages),
            tokens_before=_tel_tokens_before,
            tokens_after=_tel_tokens_before,
            duration_s=time.monotonic() - _tel_started,
            aborted=True,
        )
        return messages, _existing_sp

    # Eviction-only pass (substrate context engine, plan §2.6): Tier 0+1
    # relieved pressure by replacing old tool-result BODIES in place with
    # retrieval stubs — the message count, order, and roles are unchanged and
    # the conversation logically continues in the SAME session. Session
    # rotation exists for summarisation-compaction, which *restructures*
    # history (drops the middle, mints a summary turn); applying it to stub
    # eviction would be wrong — it would strand the continuing turn under a new
    # session id and append a todo snapshot that changes the message count.
    # So skip the whole rotation block. We still rebuild the system-prompt
    # cache and recompute last_prompt_tokens (exactly as the normal path does
    # below) so pressure math sees the smaller context, and we append NO todo
    # snapshot. ``compressed`` is the in-place-edited list. Plain
    # ContextCompressor never sets this flag, so its path is untouched.
    if getattr(agent.context_compressor, "_last_compress_eviction_only", False):
        agent._invalidate_system_prompt()
        new_system_prompt = agent._build_system_prompt(system_message)
        agent._cached_system_prompt = new_system_prompt
        _evicted_est = estimate_request_tokens_rough(
            compressed,
            system_prompt=new_system_prompt or "",
            tools=agent.tools or None,
        )
        agent.context_compressor.last_prompt_tokens = _evicted_est
        agent.context_compressor.last_completion_tokens = 0
        logger.info(
            "context eviction (no rotation): session=%s messages=%d tokens=~%s",
            agent.session_id or "none", len(compressed), f"{_evicted_est:,}",
        )
        return compressed, new_system_prompt

    summary_error = getattr(agent.context_compressor, "_last_summary_error", None)
    if summary_error:
        if getattr(agent, "_last_compression_summary_warning", None) != summary_error:
            agent._last_compression_summary_warning = summary_error
            agent._emit_warning(
                f"⚠ Compression summary failed: {summary_error}. "
                "Inserted a fallback context marker."
            )
    else:
        # No hard failure — but did the configured aux model error out
        # and get recovered by retrying on main?  Surface that so users
        # know their auxiliary.compression.model setting is broken even
        # though compression succeeded.
        _aux_fail_model = getattr(agent.context_compressor, "_last_aux_model_failure_model", None)
        _aux_fail_err = getattr(agent.context_compressor, "_last_aux_model_failure_error", None)
        if _aux_fail_model:
            # Dedup on (model, error) so we don't spam on every compaction
            _aux_key = (_aux_fail_model, _aux_fail_err)
            if getattr(agent, "_last_aux_fallback_warning_key", None) != _aux_key:
                agent._last_aux_fallback_warning_key = _aux_key
                agent._emit_warning(
                    f"ℹ Configured compression model '{_aux_fail_model}' failed "
                    f"({_aux_fail_err or 'unknown error'}). Recovered using main model — "
                    "check auxiliary.compression.model in config.yaml."
                )

    todo_snapshot = agent._todo_store.format_for_injection()
    if todo_snapshot:
        compressed.append({"role": "user", "content": todo_snapshot})

    agent._invalidate_system_prompt()
    new_system_prompt = agent._build_system_prompt(system_message)
    agent._cached_system_prompt = new_system_prompt

    if agent._session_db:
        # Each DB op is wrapped independently so a single failure can't strand
        # the session_id rotation. Rotation MUST happen even if persistence fails
        # — otherwise the new session writes into the old session's slot.
        old_title = None
        try:
            old_title = resolve_maybe_awaitable(
                agent._session_db.get_session_title(agent.session_id)
            )
        except Exception as e:
            logger.debug("Could not read old session title on compression: %s", e)

        try:
            agent.commit_memory_session(messages)
        except Exception as e:
            logger.warning("Memory extraction on compression failed: %s", e)

        try:
            resolve_maybe_awaitable(
                agent._session_db.end_session(agent.session_id, "compression")
            )
        except Exception as e:
            logger.warning("Could not mark old session ended on compression: %s", e)

        # Unconditional rotation — must happen even if every DB call above raised.
        old_session_id = agent.session_id
        agent.session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        os.environ["THOTH_SESSION_ID"] = agent.session_id
        try:
            from gateway.session_context import _SESSION_ID
            _SESSION_ID.set(agent.session_id)
        except Exception:
            pass
        agent._session_db_created = False
        # Reset flush cursor — new session starts with no messages written
        agent._last_flushed_db_idx = 0

        try:
            resolve_maybe_awaitable(
                agent._session_db.create_session(
                    session_id=agent.session_id,
                    source=agent.platform or os.environ.get("THOTH_SESSION_SOURCE", "cli"),
                    model=agent.model,
                    model_config=agent._session_init_model_config,
                    parent_session_id=old_session_id,
                )
            )
            agent._session_db_created = True
        except Exception as e:
            logger.warning("Could not create continuation session in DB: %s", e)

        if old_title:
            try:
                new_title = resolve_maybe_awaitable(
                    agent._session_db.get_next_title_in_lineage(old_title)
                )
                resolve_maybe_awaitable(
                    agent._session_db.set_session_title(agent.session_id, new_title)
                )
            except Exception as e:
                logger.debug("Could not propagate title on compression: %s", e)

        try:
            resolve_maybe_awaitable(
                agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
            )
        except Exception as e:
            logger.debug("Could not persist system prompt on compression: %s", e)

    # Notify the context engine that the session_id rotated because of
    # compression (not a fresh /new). Plugin engines (e.g. hermes-lcm) use
    # boundary_reason="compression" to preserve DAG lineage across the
    # rollover instead of re-initializing fresh per-session state.
    # See hermes-lcm#68. Built-in ContextCompressor ignores kwargs.
    try:
        _old_sid = locals().get("old_session_id")
        if _old_sid and hasattr(agent.context_compressor, "on_session_start"):
            agent.context_compressor.on_session_start(
                agent.session_id or "",
                boundary_reason="compression",
                old_session_id=_old_sid,
            )
    except Exception as _ce_err:
        logger.debug("context engine on_session_start (compression): %s", _ce_err)

    # Notify memory providers of the compression-driven session_id rotation
    # so provider-cached per-session state (Hindsight's _document_id,
    # accumulated turn buffers, counters) refreshes. reset=False because
    # the logical conversation continues; only the id and DB row rolled
    # over. See #6672.
    try:
        _old_sid = locals().get("old_session_id")
        if _old_sid and agent._memory_manager:
            agent._memory_manager.on_session_switch(
                agent.session_id or "",
                parent_session_id=_old_sid,
                reset=False,
                reason="compression",
            )
    except Exception as _me_err:
        logger.debug("memory manager on_session_switch (compression): %s", _me_err)

    # Warn on repeated compressions (quality degrades with each pass)
    _cc = agent.context_compressor.compression_count
    if _cc >= 2:
        agent._vprint(
            f"{agent.log_prefix}⚠️  Session compressed {_cc} times — "
            f"accuracy may degrade. Consider /new to start fresh.",
            force=True,
        )

    # Update token estimate after compaction so pressure calculations
    # use the post-compression count, not the stale pre-compression one.
    # Use estimate_request_tokens_rough() so tool schemas are included —
    # with 50+ tools enabled, schemas alone can add 20-30K tokens, and
    # omitting them delays the next compression cycle far past the
    # configured threshold (issue #14695).
    _compressed_est = estimate_request_tokens_rough(
        compressed,
        system_prompt=new_system_prompt or "",
        tools=agent.tools or None,
    )
    agent.context_compressor.last_prompt_tokens = _compressed_est
    agent.context_compressor.last_completion_tokens = 0

    # Clear the file-read dedup cache.  After compression the original
    # read content is summarised away — if the model re-reads the same
    # file it needs the full content, not a "file unchanged" stub.
    try:
        from tools.file_tools import reset_file_dedup
        reset_file_dedup(task_id)
    except Exception:
        pass

    logger.info(
        "context compression done: session=%s messages=%d->%d tokens=~%s",
        agent.session_id or "none", _pre_msg_count, len(compressed),
        f"{_compressed_est:,}",
    )
    # Finding B(2): tokens_before and tokens_after are BOTH
    # estimate_request_tokens_rough (schema-inclusive) — before was sampled at
    # entry, after is _compressed_est just computed above — so tokens_saved is a
    # single-basis measurement and cannot be negative for a genuine pass.
    _emit_compression_telemetry(
        agent,
        trigger="auto" if not force else "manual",
        messages_before=_pre_msg_count,
        messages_after=len(compressed),
        tokens_before=_tel_tokens_before,
        tokens_after=_compressed_est,
        duration_s=time.monotonic() - _tel_started,
        aborted=False,
        summary_fallback=bool(summary_error),
        old_session_id=locals().get("old_session_id"),
        new_session_id=agent.session_id,
    )
    return compressed, new_system_prompt


def try_shrink_image_parts_in_messages(api_messages: list) -> bool:
    """Re-encode all native image parts at a smaller size to recover from
    image-too-large errors (Anthropic 5 MB, unknown other providers).

    Mutates ``api_messages`` in place. Returns True if any image part was
    actually replaced, False if there were no image parts to shrink or
    Pillow couldn't help (caller should surface the original error).

    Strategy: look for ``image_url`` / ``input_image`` parts carrying a
    ``data:image/...;base64,...`` payload.  For each one whose encoded
    size exceeds 4 MB (a safe target that slides under Anthropic's 5 MB
    ceiling with header overhead), write the base64 to a tempfile, call
    ``vision_tools._resize_image_for_vision`` to produce a smaller data
    URL, and substitute it in place.

    Non-data-URL images (http/https URLs) are not touched — the provider
    fetches those itself and the size limit is different.
    """
    if not api_messages:
        return False

    try:
        from tools.vision_tools import _resize_image_for_vision
    except Exception as exc:
        logger.warning("image-shrink recovery: vision_tools unavailable — %s", exc)
        return False

    # 4 MB target leaves comfortable headroom under Anthropic's 5 MB.
    # Non-Anthropic providers we haven't observed rejecting are fine with
    # much larger; shrinking to 4 MB here loses quality but only fires
    # after a confirmed provider rejection, so the alternative is failure.
    target_bytes = 4 * 1024 * 1024
    changed_count = 0

    def _shrink_data_url(url: str) -> Optional[str]:
        """Return a smaller data URL, or None if shrink can't help."""
        if not isinstance(url, str) or not url.startswith("data:"):
            return None
        if len(url) <= target_bytes:
            # This specific image wasn't the oversized one.
            return None
        try:
            header, _, data = url.partition(",")
            mime = "image/jpeg"
            if header.startswith("data:"):
                mime_part = header[len("data:"):].split(";", 1)[0].strip()
                if mime_part.startswith("image/"):
                    mime = mime_part
            import base64 as _b64
            raw = _b64.b64decode(data)
            suffix = {
                "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/bmp": ".bmp",
            }.get(mime, ".jpg")
            tmp = tempfile.NamedTemporaryFile(
                prefix="thoth_shrink_", suffix=suffix, delete=False,
            )
            try:
                tmp.write(raw)
                tmp.close()
                resized = _resize_image_for_vision(
                    Path(tmp.name),
                    mime_type=mime,
                    max_base64_bytes=target_bytes,
                )
            finally:
                try:
                    Path(tmp.name).unlink(missing_ok=True)
                except Exception:
                    pass
            if not resized or len(resized) >= len(url):
                # Shrink didn't help (or made it bigger — corrupt input?).
                return None
            return resized
        except Exception as exc:
            logger.warning("image-shrink recovery: re-encode failed — %s", exc)
            return None

    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype not in {"image_url", "input_image"}:
                continue
            image_value = part.get("image_url")
            # OpenAI chat.completions: {"image_url": {"url": "data:..."}}
            # OpenAI Responses: {"image_url": "data:..."}
            if isinstance(image_value, dict):
                url = image_value.get("url", "")
                resized = _shrink_data_url(url)
                if resized:
                    image_value["url"] = resized
                    changed_count += 1
            elif isinstance(image_value, str):
                resized = _shrink_data_url(image_value)
                if resized:
                    part["image_url"] = resized
                    changed_count += 1

    if changed_count:
        logger.info(
            "image-shrink recovery: re-encoded %d image part(s) to fit under %.0f MB",
            changed_count, target_bytes / (1024 * 1024),
        )
    return changed_count > 0


__all__ = [
    "check_compression_model_feasibility",
    "replay_compression_warning",
    "compress_context",
    "try_shrink_image_parts_in_messages",
]
