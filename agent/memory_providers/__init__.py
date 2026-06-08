"""Built-in memory providers shipped with Thoth.

Phase C adds the SubstrateMemoryProvider — a substrate-backed memory
provider that reads from the L0 substrate (Phase A storage) via the
recall API (Phase C). Activation is gated by the
``THOTH_SUBSTRATE_RECALL`` env var so default behavior is unchanged.

External (plugin) providers continue to live under
``plugins/memory/<name>/`` and load via ``plugins.memory.load_memory_provider``.
"""
