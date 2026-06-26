"""Cognitive substrate — the medium the agent inhabits.

See the [Phase A spec](https://github.com/ggrace519/llm-cognitive-thought/blob/main/docs/superpowers/specs/2026-05-22-phase-a-substrate-skeleton.md)
for what this package is and what it deliberately is NOT (Phase A is
write-only L0 perception emission; no recall, no curator, no LLM-driven
sub-agents).

Public surface (grows as Phase A tasks land):

    from substrate import Substrate
    from substrate.l0 import commit_slice, commit_slice_sync

    async def main():
        await thoth_db.init(dsn)         # Phase 0 pool
        sub = await Substrate.boot()
        await commit_slice(sub, stream_id, "hello", event_time_world=...)
        await sub.shutdown()
        await thoth_db.close()

Hooks live in ``substrate.events.thoth_hooks`` and are bound to the
booted Substrate instance via ``Substrate.boot()`` (binding lands in
Task 14 of the Phase A plan).
"""

from substrate.facade import Substrate


def get_bound_substrate():
    """Return the substrate instance currently bound to the perception
    hook layer, or ``None`` if ``Substrate.boot()`` has not been called
    yet.

    Phase C surface: the ``SubstrateMemoryProvider`` (registered into
    Thoth's ``MemoryManager`` at startup) uses this to reach the
    substrate without holding a hard reference — keeps the provider's
    import surface independent of Substrate's lifecycle.
    """
    # Late import so the hooks module isn't loaded just by importing
    # the substrate package.
    from substrate.events.thoth_hooks import _substrate

    return _substrate


__all__ = ["Substrate", "get_bound_substrate"]
