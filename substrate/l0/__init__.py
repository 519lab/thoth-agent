"""L0 — the perception substrate. Write-only public surface in Phase A.

The only public functions are :func:`commit_slice` (async; preferred
for async call sites — gateway, conversation loop, ACP server) and its
sync facade :func:`commit_slice_sync` (used from cron and other sync
sites; bridges via ``thoth_db.run_sync``).

Read endpoints (Sentinel batch tick, force-reject sweep) live in
``substrate.storage.slices.SliceRepo`` and are not exported here — they
are internal substrate machinery, not part of the public Phase A
surface.
"""

from substrate.l0.api import (
    commit_slice,
    commit_slice_sync,
    reinforce_slice,
    reinforce_slice_sync,
)

__all__ = [
    "commit_slice",
    "commit_slice_sync",
    "reinforce_slice",
    "reinforce_slice_sync",
]
