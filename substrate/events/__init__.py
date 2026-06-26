"""Substrate ↔ Thoth integration surface.

Thoth call sites import from ``substrate.events.thoth_hooks``::

    from substrate.events.thoth_hooks import on_user_message_async
    await on_user_message_async(session_id, source, text, t_event)

Each hook comes as a matched (``_async`` + sync facade) pair. Hooks are
silently bound to the booted Substrate via ``Substrate.boot()``; calling
a hook before boot is a no-op.
"""
