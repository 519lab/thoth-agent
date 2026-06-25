"""``SubAgent`` base class + intensity dial.

Every substrate sub-agent (Sentinel, Curator, Reflector, ...) subclasses
``SubAgent`` so the lifecycle and intensity-dial machinery are uniform.
Phase A ships stubs (Sentinel passes everything, Conductor holds state
with no policy) but the contract here is the **same** one Phase B+ real
sub-agents will honor.

Intensity is per-agent: each agent reads a :class:`Level` and sleeps
between ticks proportional to it (see ``_INTERVAL_BY_LEVEL`` below).
``OFF`` is a hard stop — ``tick()`` is never called. The mapping is
deliberately conservative; Phase B+ may add per-agent overrides via a
class attribute.

Sentinel agents (``is_sentinel = True``) have a floor of ``FULL`` —
``set_intensity()`` silently coerces below-FULL settings back to FULL.
This enforces "Sentinel is never throttled" as a design invariant.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from substrate.facade import Substrate


# ---------------------------------------------------------------------------
# Intensity dial — 5-level. Mirrors the named-mode collapse from the MVS
# spec §3.5 (Ambient/Burst/Reactive/Rising/Background → LOW/MODERATE/HIGH/
# FULL/OFF). Levels are strings on the wire so Conductor and operator-side
# tooling can read them without enum imports.
# ---------------------------------------------------------------------------


class Level(str, Enum):
    """Sub-agent intensity. ``OFF`` is a hard stop (no tick); ``FULL``
    is "run as often as the implementation can".
    """

    OFF = "off"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    FULL = "full"


# Tick interval (seconds) per level. Returning ``None`` for OFF lets
# the run loop skip ``tick()`` without busy-waiting.
_INTERVAL_BY_LEVEL: dict[Level, Optional[float]] = {
    Level.OFF: None,
    Level.LOW: 10.0,
    Level.MODERATE: 3.0,
    Level.HIGH: 1.0,
    Level.FULL: 0.2,
}


# Sleep used by the run loop when intensity is OFF, so a future
# ``set_intensity(level=FULL)`` can pick up the change within a second.
_OFF_POLL_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Per-tick watchdog. A single tick() that hangs (a wedged DB call, a model
# request with no client-side timeout) would otherwise freeze the whole run
# loop forever — and because the heartbeat task beats independently (see
# ``_heartbeat_loop``), the agent would *look* alive while making zero
# progress. To make a stuck tick recoverable, the run loop wraps each tick in
# ``asyncio.wait_for`` with a generous per-intensity ceiling: hit the ceiling
# and the tick is abandoned (TimeoutError), counted, and the loop moves on.
#
# The ceiling is derived from the agent's tick interval times this multiplier,
# with a 30s floor so fast-cadence agents (FULL = 0.2s) still get a sane window.
# The multiplier is deliberately large — the watchdog catches *wedged* ticks,
# not merely slow ones; a tick that legitimately runs many multiples of its
# cadence (the Curator's hourly L3/L4 backfill) sets a per-agent
# ``tick_timeout_s`` override instead of relying on this derivation.
_TICK_TIMEOUT_MULT = float(
    os.environ.get("THOTH_SUBSTRATE_TICK_TIMEOUT_MULT", "50") or "50"
)
_TICK_TIMEOUT_FLOOR_S = 30.0

# DEFERRED (innovation #4 follow-up): in-process supervisor. With the watchdog
# above, a wedged agent is now *observable* — frozen ``_tick_count`` while the
# independent heartbeat keeps ``last_beat_at`` advancing. The planned next step
# (option a) is a small in-process supervisor inside the worker that watches its
# agents' ``_tick_count`` and ``os._exit(1)`` once it stalls past
# ``THOTH_SUBSTRATE_STALL_THRESHOLD``, letting ``systemd Restart=on-failure``
# respawn. NOT built in this slice — the watchdog + decoupled heartbeat are the
# self-contained recovery primitive; the supervisor is a separate, gated change.


# ---------------------------------------------------------------------------
# Liveness heartbeat. The sub-agent run loop upserts a row into
# ``substrate_agent_heartbeat`` on this cadence so a *different process*
# (the ``thoth substrate`` inspect CLI) can tell a live worker subprocess
# from a dead one. Before this existed, the inspect CLI printed a static
# "all healthy" sub-agent list — a dead worker was invisible (the
# 2026-05-26 production incident).
#
# The cadence is intentionally decoupled from tick interval: the
# partition-maintenance agent ticks once per 24h, but it must still report
# liveness every few seconds or it would look dead within minutes. The run
# loop therefore chunks its inter-tick sleep at this cadence and beats each
# chunk.
_HEARTBEAT_INTERVAL_S = float(
    os.environ.get("THOTH_SUBSTRATE_HEARTBEAT_S", "10") or "10"
)


# Last-writer-wins UPSERT keyed on ``agent_name``. ``last_beat_at`` uses
# PG's ``now()`` (not the host clock) so the inspect CLI's staleness math
# is skew-free. A worker restart simply overwrites the row with the new
# pid — no stale-row accumulation.
_HEARTBEAT_UPSERT_SQL = """
    INSERT INTO substrate_agent_heartbeat
        (agent_name, pid, host, level, is_sentinel,
         tick_count, started_at, last_beat_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, now())
    ON CONFLICT (agent_name) DO UPDATE SET
        pid          = EXCLUDED.pid,
        host         = EXCLUDED.host,
        level        = EXCLUDED.level,
        is_sentinel  = EXCLUDED.is_sentinel,
        tick_count   = EXCLUDED.tick_count,
        started_at   = EXCLUDED.started_at,
        last_beat_at = now()
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class SubAgent(ABC):
    """Common scaffold for every substrate sub-agent.

    Subclasses must:
      * Set ``name`` (class attribute) — used for logging and the
        inspect CLI.
      * Set ``is_sentinel`` if the agent is a defensive primitive that
        must never be throttled (Sentinel itself, and any future
        always-on auditor).
      * Implement ``async tick()`` — one unit of work; called inside
        the run loop with no arguments. Exceptions are caught and
        logged; the loop continues.
    """

    name: str = "unnamed"
    is_sentinel: bool = False

    def __init__(self, substrate: "Substrate") -> None:
        self._substrate = substrate
        # Sentinels start at FULL (which is also their floor). Other
        # agents start at LOW — they wake up but don't burn cycles
        # until Conductor (Phase B+) tells them otherwise.
        self._level: Level = Level.FULL if self.is_sentinel else Level.LOW
        self._stopped: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._log = logging.getLogger(f"substrate.agents.{self.name}")

        # Liveness heartbeat bookkeeping. ``_tick_count`` is reported in
        # the heartbeat so operators can see a stuck-but-alive agent
        # (count frozen) vs. a healthy one (count climbing).
        # ``_started_at`` is set when ``run()`` begins. ``_last_beat_mono``
        # rate-limits the upsert to ``_HEARTBEAT_INTERVAL_S`` using a
        # monotonic clock (immune to wall-clock jumps).
        self._tick_count: int = 0
        self._started_at: Optional[datetime] = None
        self._last_beat_mono: Optional[float] = None

        # Per-tick watchdog bookkeeping. ``_tick_timeout_count`` counts ticks
        # abandoned because they blew past ``_tick_ceiling_for(level)`` — a
        # frozen ``_tick_count`` paired with a climbing ``_tick_timeout_count``
        # is the signature of a wedged agent. ``tick_timeout_s`` is a per-agent
        # override (seconds) that, when set, replaces the interval-derived
        # ceiling — for agents whose tick legitimately runs far longer than its
        # cadence (the Curator's hourly upper-layer backfill).
        self._tick_timeout_count: int = 0
        self.tick_timeout_s: Optional[float] = None

        # Independent heartbeat task. The liveness beat must keep firing while
        # a tick hangs (that is precisely the case operators must see), so it
        # runs in its own task spawned by ``run()`` rather than being driven
        # off the tick loop's inter-tick sleep.
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def set_intensity(self, level: Level) -> None:
        """Change the sub-agent's intensity.

        Sentinels are silently floored at ``FULL``. Calling
        ``set_intensity(OFF)`` on a Sentinel is a no-op; the choice is
        intentional — the floor is a design invariant, and a noisy
        error would force the caller to special-case Sentinel in
        Conductor's bulk-dial code.
        """
        if self.is_sentinel and level is not Level.FULL:
            self._log.debug(
                "ignoring set_intensity(%s) — sentinel floor is FULL",
                level.value,
            )
            level = Level.FULL
        self._level = level

    @property
    def level(self) -> Level:
        """Current intensity level — useful for the inspect CLI."""
        return self._level

    async def run(self) -> None:
        """Main loop. Sleeps per current intensity, calls ``tick()``,
        respects the stopped event.

        Each ``tick()`` is wrapped in ``asyncio.wait_for`` with a generous
        per-intensity ceiling (``_tick_ceiling_for``): a wedged tick is
        abandoned (counted in ``_tick_timeout_count``, ``_tick_count`` frozen)
        and the loop moves on rather than hanging forever. Liveness is beaten
        by an *independent* ``_heartbeat_loop`` task spawned here, so a hung
        tick still reports its (frozen) ``tick_count`` and a wedged agent is
        distinguishable from a dead process.

        Exceptions in ``tick()`` are logged and the loop continues —
        Phase A sub-agents must never crash the substrate process.
        Phase B+ may introduce circuit-breaker behavior; not yet.

        Sleeps via ``wait_for(self._stopped.wait(), timeout=...)`` so
        ``stop()`` wakes the loop immediately instead of waiting out
        the full tick interval. With a plain ``asyncio.sleep(interval)``
        the partition-maintenance agent (24h cadence) and force-reject
        (3–10s) would always exceed the 2-second shutdown grace and
        log spurious ``subagent.stop.timeout`` warnings on clean exit.
        """
        self._started_at = _utcnow()
        self._log.debug(
            "subagent.run.start name=%s level=%s", self.name, self._level.value
        )

        # Spawn the heartbeat as an independent task so liveness keeps beating
        # regardless of tick state — a hung tick must still report the (frozen)
        # tick_count so operators can distinguish a wedged agent from a dead
        # process. It is cancelled in ``finally`` (and by ``stop_and_wait``).
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"substrate-{self.name}-heartbeat"
        )

        try:
            while not self._stopped.is_set():
                # Call ``self._interval_for(...)`` so subclasses can
                # override the mapping (e.g. partition-maintenance
                # forces a fixed 24h cadence regardless of intensity).
                # The base implementation looks up _INTERVAL_BY_LEVEL.
                interval = self._interval_for(self._level)
                if interval is None:  # OFF
                    await self._wait(_OFF_POLL_INTERVAL)
                    continue
                try:
                    # Per-tick watchdog: abandon a tick that blows past the
                    # ceiling rather than freezing the loop forever. A timed-out
                    # tick is counted but does NOT advance ``_tick_count`` — the
                    # frozen count is what the inspect CLI reads as "stuck".
                    await asyncio.wait_for(
                        self.tick(),
                        timeout=self._tick_ceiling_for(self._level),
                    )
                    self._tick_count += 1
                except asyncio.TimeoutError:
                    self._tick_timeout_count += 1
                    self._log.warning(
                        "subagent.tick.timeout name=%s level=%s "
                        "ceiling_s=%s tick_count=%s timeout_count=%s",
                        self.name,
                        self._level.value,
                        self._tick_ceiling_for(self._level),
                        self._tick_count,
                        self._tick_timeout_count,
                    )
                except Exception:
                    # Log with exc_info so the traceback lands in the
                    # substrate's log; never re-raise to the loop.
                    self._log.exception("subagent.tick.error name=%s", self.name)
                await self._wait(interval)
        finally:
            # Stop the heartbeat task before the loop returns so a shutdown
            # doesn't leave a dangling beater. Best-effort: cancellation races
            # are swallowed — a heartbeat teardown error must not mask a real
            # shutdown.
            await self._cancel_heartbeat()
            self._log.debug("subagent.run.stop name=%s", self.name)

    async def _heartbeat_loop(self) -> None:
        """Independent liveness beat. Fires a forced startup beat (so the agent
        is visible to the inspect CLI immediately, before its first — possibly
        slow — tick) then beats every ``_HEARTBEAT_INTERVAL_S`` until cancelled.

        Decoupled from the tick loop on purpose: a tick that hangs must NOT
        silence the heartbeat, otherwise a wedged agent would look dead. The
        beat reports the current (frozen, while a tick hangs) ``_tick_count``.
        """
        try:
            await self._maybe_heartbeat(force=True)
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=_HEARTBEAT_INTERVAL_S
                    )
                    return  # stop requested
                except asyncio.TimeoutError:
                    await self._maybe_heartbeat()
        except asyncio.CancelledError:
            # Normal teardown path (run()/stop_and_wait cancel us); re-raise so
            # the task records as cancelled rather than swallowing the signal.
            raise

    async def _cancel_heartbeat(self) -> None:
        """Cancel and await the heartbeat task. Idempotent + best-effort: a
        teardown error (including the expected ``CancelledError``) is swallowed
        so it can't mask a shutdown in progress."""
        task = self._heartbeat_task
        if task is None or task.done():
            self._heartbeat_task = None
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._heartbeat_task = None

    async def _wait(self, seconds: float) -> None:
        """Sleep up to *seconds*, returning early if ``stop()`` is called.

        The liveness beat now runs in its own task (``_heartbeat_loop``), so
        this is a plain interruptible sleep — ``stop()`` wakes it immediately,
        and the full interval is otherwise waited out.
        """
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return  # interval elapsed without a stop

    async def _maybe_heartbeat(self, *, force: bool = False) -> None:
        """Upsert this agent's liveness row, rate-limited to the heartbeat
        cadence. Best-effort: a DB hiccup (or a missing table on a
        pre-migration DB) is logged at debug and never propagates — a
        heartbeat failure must not perturb the tick loop.

        Guarded against a ``None`` substrate / poolless substrate so the
        base-class unit tests (which construct agents with ``substrate=None``)
        exercise the run loop without a database.
        """
        substrate = self._substrate
        pool = getattr(substrate, "pool", None) if substrate is not None else None
        if pool is None:
            return

        loop = asyncio.get_event_loop()
        now_mono = loop.time()
        if (
            not force
            and self._last_beat_mono is not None
            and now_mono - self._last_beat_mono < _HEARTBEAT_INTERVAL_S
        ):
            return

        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    _HEARTBEAT_UPSERT_SQL,
                    self.name,
                    os.getpid(),
                    socket.gethostname(),
                    self._level.value,
                    self.is_sentinel,
                    self._tick_count,
                    self._started_at or _utcnow(),
                )
            # Only advance the rate-limit clock on a successful write so a
            # transient failure retries on the next loop iteration.
            self._last_beat_mono = now_mono
        except Exception:
            self._log.debug(
                "subagent.heartbeat.failed name=%s", self.name, exc_info=True
            )

    def stop(self) -> None:
        """Request graceful stop. The run loop checks ``_stopped`` at
        the top of each iteration; in flight ``tick()`` completes."""
        self._stopped.set()

    async def stop_and_wait(self, *, timeout: float = 2.0) -> None:
        """Stop and wait for the run loop's task to exit.

        Used by :meth:`Substrate.shutdown`. The timeout is a safety
        net — a misbehaving ``tick()`` shouldn't hang shutdown
        indefinitely.
        """
        self.stop()
        # Tear the heartbeat task down explicitly: if ``run()`` itself is
        # wedged (a hung tick the watchdog hasn't yet caught), its ``finally``
        # may not run promptly, so we cancel the beater here too.
        await self._cancel_heartbeat()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            self._log.warning(
                "subagent.stop.timeout name=%s timeout=%s", self.name, timeout
            )
            self._task.cancel()

    def start(self) -> asyncio.Task:
        """Spawn the run loop as an asyncio task and return the task
        handle. Idempotent: calling ``start()`` twice returns the
        existing task.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"substrate-{self.name}")
        return self._task

    @property
    def task(self) -> Optional[asyncio.Task]:
        """The asyncio task created by :meth:`start`, or ``None`` if
        the agent hasn't been started yet."""
        return self._task

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    async def tick(self) -> None:
        """One unit of work. Called from the run loop. Must not block
        on user-facing operations. Exceptions are caught + logged by
        the run loop; subclasses must NOT swallow them silently."""

    # ------------------------------------------------------------------
    # Testing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _interval_for(level: Level) -> Optional[float]:
        """Public test seam — return the configured tick interval for
        ``level``. Returns ``None`` for ``OFF``.
        """
        return _INTERVAL_BY_LEVEL[level]

    def _tick_ceiling_for(self, level: Level) -> Optional[float]:
        """Watchdog deadline (seconds) for one ``tick()`` at *level*.

        A per-agent ``tick_timeout_s`` override wins outright (used by agents
        whose tick legitimately runs far longer than its cadence — the
        Curator's hourly L3/L4 backfill). Otherwise the ceiling is the agent's
        tick interval times ``_TICK_TIMEOUT_MULT``, floored at
        ``_TICK_TIMEOUT_FLOOR_S`` so a fast cadence (FULL = 0.2s) still gets a
        sane window. Returns ``None`` for ``OFF`` (the loop never ticks then).
        """
        if self.tick_timeout_s is not None:
            return self.tick_timeout_s
        interval = self._interval_for(level)
        if interval is None:  # OFF — no tick, no ceiling
            return None
        return max(_TICK_TIMEOUT_FLOOR_S, interval * _TICK_TIMEOUT_MULT)


__all__ = ["Level", "SubAgent"]
