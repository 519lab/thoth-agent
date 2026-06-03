"""Proactive user notification from the substrate worker process.

Substrate sub-agents run in a separate process from the gateway and normally
only write to the DB. The SkillScout, though, needs to tell the user it drafted
a skill to review. This delivers a one-off proactive message to the user's
configured home channel(s) using the same out-of-process path cron uses
(``cron.scheduler._deliver_result`` → ``tools.send_message_tool._send_to_platform``
→ each platform's standalone sender).

Design constraints:
  * **Lazy imports** — keep ``substrate`` importable without the gateway stack.
  * **Never raises** — a delivery failure must not crash the SkillScout tick;
    the proposal is already persisted and reviewable later. Returns a list of
    error strings (empty == delivered to at least one channel).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _home_targets():
    """Yield ``(platform_enum, pconfig, chat_id, thread_id)`` for every enabled
    platform with a configured home channel.

    Primary source is the gateway config home channel (set via ``/sethome``);
    falls back to cron's env-var home targets (``*_HOME_CHANNEL``) so a
    deploy configured the cron way still gets notified.
    """
    from gateway.config import load_gateway_config, Platform

    config = load_gateway_config()
    seen: set[tuple] = set()

    # 1) Gateway-config home channels (the /sethome mechanism).
    for platform, pconfig in (config.platforms or {}).items():
        if not getattr(pconfig, "enabled", False):
            continue
        home = getattr(pconfig, "home_channel", None)
        if home and home.chat_id:
            key = (platform.value, str(home.chat_id), home.thread_id)
            if key not in seen:
                seen.add(key)
                yield platform, pconfig, str(home.chat_id), home.thread_id

    # 2) Cron-style env-var home targets, for any platform not already covered.
    try:
        from cron.scheduler import (
            _iter_home_target_platforms,
            _get_home_target_chat_id,
            _get_home_target_thread_id,
        )

        for name in _iter_home_target_platforms():
            chat_id = _get_home_target_chat_id(name)
            if not chat_id:
                continue
            try:
                platform = Platform(name.lower())
            except (ValueError, KeyError):
                continue
            pconfig = (config.platforms or {}).get(platform)
            if not pconfig or not getattr(pconfig, "enabled", False):
                continue
            thread_id = _get_home_target_thread_id(name)
            key = (platform.value, str(chat_id), thread_id)
            if key not in seen:
                seen.add(key)
                yield platform, pconfig, str(chat_id), thread_id
    except Exception:
        logger.debug("cron home-target fallback unavailable", exc_info=True)


async def notify_user(text: str) -> list[str]:
    """Deliver ``text`` to the user's home channel(s). Returns a list of error
    strings; an empty list means it reached at least one channel. Never raises."""
    if not (text or "").strip():
        return ["empty message"]

    try:
        from tools.send_message_tool import _send_to_platform
        # Ensure plugin platforms register their standalone senders (idempotent).
        try:
            from thoth_cli.plugins import discover_plugins

            discover_plugins()
        except Exception:
            logger.debug("discover_plugins unavailable", exc_info=True)
        targets = list(_home_targets())
    except Exception as e:  # gateway stack not importable, config broken, etc.
        logger.warning("notify_user: could not resolve delivery targets: %s", e)
        return [f"no delivery target: {e}"]

    if not targets:
        return ["no home channel configured"]

    errors: list[str] = []
    delivered = 0
    for platform, pconfig, chat_id, thread_id in targets:
        try:
            result = await _send_to_platform(
                platform, pconfig, chat_id, text, thread_id=thread_id
            )
            if isinstance(result, dict) and result.get("error"):
                errors.append(f"{platform.value}: {result['error']}")
            else:
                delivered += 1
        except Exception as e:
            logger.warning("notify_user: send to %s failed: %s", platform, e)
            errors.append(f"{platform.value}: {e}")

    # Success on any channel clears the error list (best-effort delivery).
    return [] if delivered else (errors or ["delivery failed"])


__all__ = ["notify_user"]
