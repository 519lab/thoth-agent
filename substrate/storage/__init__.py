"""Substrate storage layer — repositories over the shared asyncpg pool.

All repos are async. Repos that participate in caller-controlled
transactions accept an explicit ``asyncpg.Connection`` argument;
read-only no-txn helpers may acquire from the shared
``thoth_db.pool()`` themselves.
"""

from substrate.storage.decay_profiles import (
    DEFAULT_BINARY_PROFILE,
    DEFAULT_SIGNAL_PROFILE,
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    DecayProfileRepo,
)
from substrate.storage.slices import ReleaseRecord, SliceRepo
from substrate.storage.streams import StreamRepo, is_perceptual
from substrate.storage.types import (
    Address,
    ConsolidationState,
    DecayProfile,
    Family,
    Lifecycle,
    Modality,
    SentinelState,
    Slice,
    Stream,
    TombstonePolicy,
)

__all__ = [
    "Address",
    "ConsolidationState",
    "DEFAULT_BINARY_PROFILE",
    "DEFAULT_SIGNAL_PROFILE",
    "DEFAULT_STRUCTURED_PROFILE",
    "DEFAULT_TEXT_PROFILE",
    "DecayProfile",
    "DecayProfileRepo",
    "Family",
    "Lifecycle",
    "Modality",
    "ReleaseRecord",
    "SentinelState",
    "Slice",
    "SliceRepo",
    "Stream",
    "StreamRepo",
    "TombstonePolicy",
    "is_perceptual",
]
