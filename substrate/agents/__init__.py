"""Substrate sub-agents — each owns a tick loop gated by an intensity dial.

Phase A ships stubs (Sentinel passes everything, Conductor holds state
with no policy) so the lifecycle and intensity machinery are *exercised*
from day one. Phase B+ swaps in real defense / decay / forecasting logic
without re-plumbing.
"""

from substrate.agents.base import Level, SubAgent
from substrate.agents.conductor import StubConductor
from substrate.agents.curator import Curator
from substrate.agents.force_reject import ForceRejectWorker
from substrate.agents.partition_maintenance import PartitionMaintenanceWorker
from substrate.agents.sentinel import StubSentinel, _trust_for_modality
from substrate.agents.tuner_watch import RecallTunerWatch

__all__ = [
    "Curator",
    "ForceRejectWorker",
    "Level",
    "PartitionMaintenanceWorker",
    "RecallTunerWatch",
    "StubConductor",
    "StubSentinel",
    "SubAgent",
    "_trust_for_modality",
]
