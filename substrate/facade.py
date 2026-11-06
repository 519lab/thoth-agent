"""``class Substrate`` — the public lifecycle + accessor surface.

Phase A spec §8: ``Substrate.boot()`` does the full lifecycle —
Alembic-head check, partition maintenance, stream auto-registration
(the 15 streams from spec §9), sub-agent task spawn (Sentinel +
force-reject + partition-maintenance + conductor stub), and binds the
``substrate.events.thoth_hooks`` module so Thoth call sites can emit
perception.

``Substrate.from_pool()`` is the test seam — it constructs a Substrate
without running boot-time side effects so individual subsystems can be
unit-tested.

Both constructors share the same dataclass-like instance shape so the
``substrate.l0.api.commit_slice`` call site doesn't branch on construction
mode.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    import logging as _logging

from substrate.config import SubstrateConfig
from substrate.storage import (
    DEFAULT_STRUCTURED_PROFILE,
    DEFAULT_TEXT_PROFILE,
    DecayProfileRepo,
    Family,
    Modality,
    SliceRepo,
    StreamRepo,
)


# ---------------------------------------------------------------------------
# Stream auto-registration table — Phase A spec §9.
# (name, family, modality, source, organ, decay_profile_id)
# ``substrate.self_state`` is seeded by the Alembic revision (§3.5) so it's
# not in this list; the rest are inserted at boot via idempotent ON CONFLICT.
# ---------------------------------------------------------------------------


_USER_MESSAGE_SOURCES = (
    "cli",
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "signal",
    "acp",
)


def _autoregister_specs() -> list[tuple[str, Family, Modality, str, str, "object"]]:
    """Return the (name, family, modality, source, organ, decay_profile)
    tuples for the streams ``Substrate.boot`` must auto-register.

    Kept as a function (rather than a module-level constant) so tests can
    monkeypatch / shorten it, and so the DEFAULT_* UUID imports stay near
    the call site that uses them.
    """
    specs: list[tuple[str, Family, Modality, str, str, object]] = []

    # Per-source user-message streams (7).
    for source in _USER_MESSAGE_SOURCES:
        specs.append(
            (
                f"thoth.world.user_message.{source}",
                Family.EXTEROCEPTIVE,
                Modality.TEXT,
                source,
                f"gateway.{source}" if source != "acp" else "acp_adapter",
                DEFAULT_TEXT_PROFILE,
            )
        )

    # Self-action + self-state streams (7 + substrate.self_state seeded by
    # the Alembic revision = 15 total per §9).
    specs.extend(
        [
            (
                "thoth.self_action.assistant_response",
                Family.SELF_ACTION,
                Modality.TEXT,
                "agent",
                "conversation_loop",
                DEFAULT_TEXT_PROFILE,
            ),
            (
                "thoth.self_action.tool_call",
                Family.SELF_ACTION,
                Modality.STRUCTURED_EVENT,
                "agent",
                "model_tools",
                DEFAULT_STRUCTURED_PROFILE,
            ),
            (
                "thoth.self_state.tool_result",
                Family.SELF_STATE,
                Modality.STRUCTURED_EVENT,
                "agent",
                "model_tools",
                DEFAULT_STRUCTURED_PROFILE,
            ),
            (
                "thoth.self_action.subagent_spawn",
                Family.SELF_ACTION,
                Modality.STRUCTURED_EVENT,
                "agent",
                "delegate_tool",
                DEFAULT_STRUCTURED_PROFILE,
            ),
            (
                "thoth.self_state.subagent_return",
                Family.SELF_STATE,
                Modality.STRUCTURED_EVENT,
                "agent",
                "delegate_tool",
                DEFAULT_STRUCTURED_PROFILE,
            ),
            (
                "thoth.self_state.session_lifecycle",
                Family.SELF_STATE,
                Modality.STRUCTURED_EVENT,
                "agent",
                "thoth_state",
                DEFAULT_STRUCTURED_PROFILE,
            ),
            (
                "thoth.self_state.cron_dispatch",
                Family.SELF_STATE,
                Modality.STRUCTURED_EVENT,
                "agent",
                "cron.scheduler",
                DEFAULT_STRUCTURED_PROFILE,
            ),
        ]
    )

    return specs


# Expected Alembic head — the substrate refuses to boot if the DB is on an
# older revision (unless ``THOTH_AUTO_MIGRATE=1``). When a future revision
# (Phase B+) lands, add it here AND keep the substrate code able to boot
# against the prior head until the new schema is required.
#
# - ``20260523_0003`` — Phase A substrate skeleton (the schema this code
#   actually depends on).
# - ``20260524_0004`` — kanban board metadata columns (PR #16). Strictly
#   additive — no substrate-owned tables touched — so the substrate is
#   safe to boot against it. Listed so the head check stops rejecting
#   freshly-migrated databases.
# - ``20260525_0005`` — Phase C substrate_recall_log observability table.
#   Strictly additive — only the recall pipeline reads/writes this table.
# - ``20260525_0006`` — Phase C substrate_slices.embedding column + ivfflat
#   index. Adds a NULLable column; pre-Phase-C code paths ignore it.
# - ``20260526_0009`` — Phase C configurable embedding dim. Reshapes the
#   ``embedding`` column to ``vector(THOTH_EMBEDDING_DIM)``; default 1536
#   makes this a no-op for installs that don't set the env var.
# - ``20260527_0010`` — substrate_agent_heartbeat (sub-agent liveness
#   telemetry). Strictly additive — one new table the run loop upserts and
#   the inspect CLI reads. Sub-agent heartbeat writes swallow a missing
#   table, so the substrate also boots fine against the prior head.
# - ``20260527_0011`` — Phase D L1 (l1_entities/relationships/citations).
#   Strictly additive — new tables; only the Parser + L1 store touch them.
# - ``20260527_0012`` — Phase D parser audit (substrate_parser_log).
#   Strictly additive — Parser observability, parallel to substrate_recall_log.
_EXPECTED_REVISIONS = frozenset(
    {
        "20260523_0003",
        "20260524_0004",
        "20260525_0005",
        "20260525_0006",
        "20260526_0009",
        "20260527_0010",
        "20260527_0011",
        "20260527_0012",
        # - ``20260527_0013`` — Phase E1 L2 (substrate_associations + edits).
        "20260527_0013",
        # - ``20260527_0014`` — Phase E2 L3 (l3_patterns).
        "20260527_0014",
        # - ``20260527_0015`` — Phase F L4 (l4_observations).
        "20260527_0015",
        # - ``20260527_0016`` — Phase F Dreamer (substrate_dreamer_log).
        "20260527_0016",
        # - ``20260527_0017`` — Phase F learned Conductor (substrate_conductor_log).
        "20260527_0017",
        # - ``20260527_0018`` — slice pinning (decay-immune memories).
        "20260527_0018",
        # - ``20260527_0019`` — substrate_telemetry (non-perceptual sink for
        #   the substrate's own operational events; replaces the audit-slice
        #   writes to substrate.self_state that drove the L0 feedback loop).
        "20260527_0019",
        # - ``20260527_0020`` — L3/L4 curation columns (embeddings for semantic
        #   dedup; L4 salience_score + last_seen_at for decay). The Curator now
        #   merges near-duplicate patterns/observations and decays→releases them.
        "20260527_0020",
        # - ``20260527_0021`` — align l3/l4 embedding dim to substrate_slices
        #   (fixes the 1536-vs-768 mismatch when THOTH_EMBEDDING_DIM was unset
        #   at 0020 apply time, which stalled the upper-layer embed backfill).
        "20260527_0021",
        # - ``20260528_0022`` — substrate_skill_proposals (self-improvement Tier 1:
        #   the SkillScout drafts skills from L3/L4 needs; drafts stage here as
        #   pending proposals until the user approves them in chat).
        "20260528_0022",
        # - ``20260528_0023`` — skill-proposal evaluator verdict (Phase 2: a
        #   frontier model judges each draft against a guardrail/design/intent
        #   rubric; verdict stored for review + gate-mode auto-reject).
        "20260528_0023",
        # - ``20260528_0024`` — substrate_agent_cost: per-call auxiliary-model
        #   token-usage sink (operational telemetry, never read by the awareness
        #   loop). Surfaced in ``thoth substrate health``.
        "20260528_0024",
        # - ``20260624_0025`` — recall outcome label (innovation #1: recall-replay
        #   eval harness). Adds nullable substrate_recall_log.outcome_score.
        "20260624_0025",
        # - ``20260701_0026`` — substrate_recall_weights: versioned, promotable
        #   recall weight sets (learned-recall-weights innovation). The tuner's
        #   --apply target; the live recall path prefers the active row.
        "20260701_0026",
        # - ``20260711_0027`` — agent_turn_cost: the MAIN agent's per-turn
        #   cost/latency sink (always-on visibility innovation). Written by the
        #   conversation loop, not the substrate; read by ``thoth cost`` and
        #   the gateway /metrics endpoint. Never read by the awareness loop.
        "20260711_0027",
    }
)


# Shutdown timeout — Phase A spec §8.2.
_SUBAGENT_SHUTDOWN_TIMEOUT = 2.0


class Substrate:
    """Process-wide substrate handle.

    Constructed via :meth:`boot` from Thoth startup. Tests use
    :meth:`from_pool` for deterministic unit-test setup without the
    full boot side effects.
    """

    def __init__(
        self,
        *,
        pool: "asyncpg.Pool",
        streams: StreamRepo,
        slices: SliceRepo,
        profiles: DecayProfileRepo,
        log: "_logging.Logger",
        config: SubstrateConfig,
    ) -> None:
        self.pool = pool
        self.streams = streams
        self.slices = slices
        self.profiles = profiles
        self.log = log
        self.config = config

        # Populated by ``boot()`` when sub-agents are started. Tests using
        # ``from_pool`` get an empty dict; ``shutdown`` handles both.
        self._subagents: dict[str, "object"] = {}
        self._conductor: Optional["object"] = None

        # Phase C: recall log writer. Lazy-attached at boot; tests that
        # build via ``from_pool`` get ``None`` (the recall API's
        # ``_safe_enqueue_log`` swallows that case).
        self.recall_log: Optional["object"] = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_pool(
        cls,
        pool: "asyncpg.Pool",
        *,
        log: "Optional[_logging.Logger]" = None,
        config: Optional[SubstrateConfig] = None,
    ) -> "Substrate":
        """Build a Substrate without running boot-time side effects.

        Useful for tests that exercise individual subsystems (commit
        path, repos, sub-agent ticks) without spinning up the full
        runtime. Boot-time work (Alembic check, stream auto-register,
        sub-agent spawn, hook bind) is skipped — see :meth:`boot` for
        the production entry point.
        """
        import logging

        return cls(
            pool=pool,
            streams=StreamRepo(pool),
            slices=SliceRepo(pool),
            profiles=DecayProfileRepo(pool),
            log=log or logging.getLogger("substrate"),
            config=config or SubstrateConfig.from_env(),
        )

    @classmethod
    async def boot(
        cls,
        *,
        log: "Optional[_logging.Logger]" = None,
        config: Optional[SubstrateConfig] = None,
        start_subagents: bool = True,
        bind_hooks: bool = True,
        start_recall_log: bool = True,
    ) -> "Substrate":
        """Full Phase A boot sequence.

        1. Assert ``thoth_db.pool()`` is initialised.
        2. Verify Alembic head is at or beyond
           ``20260523_0003_substrate_skeleton``. If older and
           ``THOTH_AUTO_MIGRATE=1`` (from ``SubstrateConfig.auto_migrate``)
           run ``alembic upgrade head``; otherwise raise.
        3. Ensure month partitions exist (current + next 2).
        4. Auto-register the 15 streams from spec §9 (idempotent).
        5. (``start_subagents``) Spawn sub-agent tasks: StubSentinel,
           ForceRejectWorker, PartitionMaintenanceWorker. Also instantiate
           StubConductor (which holds state but doesn't tick).
        5b. (``start_recall_log``) Start the Phase C recall-log writer.
        6. (``bind_hooks``) Bind ``substrate.events.thoth_hooks`` to this
           instance so Thoth call sites can emit perception via the hook
           surface.

        The three boolean kwargs let the caller pick a role. Most callers
        should use :meth:`boot_writer` or :meth:`boot_worker` which pick
        the correct combination for their process role; this primitive
        is exposed for tests and one-shot scripts that need a custom mix.

        Returns the booted Substrate. Failures are surfaced — the
        caller (Thoth startup) decides whether to abort the process
        or degrade gracefully.
        """
        import thoth_db

        pool = thoth_db.pool()  # raises if init() wasn't called
        cfg = config or SubstrateConfig.from_env()
        substrate = cls.from_pool(pool, log=log, config=cfg)

        # 2. Alembic head check (optional auto-migrate).
        await substrate._check_alembic_head(auto_migrate=cfg.auto_migrate)

        # 3. Partition maintenance (sync, one-shot at boot — the daily
        # tick is the PartitionMaintenanceWorker started below).
        from substrate.storage.partitions import ensure_partitions

        async with thoth_db.connection() as conn:
            await ensure_partitions(conn, ahead_months=2)

        # 4. Auto-register §9 streams.
        await substrate._autoregister_streams()

        # 5. Spawn sub-agents (unless deliberately suppressed by tests).
        if start_subagents:
            substrate._spawn_subagents()
        else:
            # Even when sub-agent loops are off, the Conductor exists so
            # operator surfaces can read/write intensity levels.
            from substrate.agents.conductor import StubConductor

            substrate._conductor = StubConductor(substrate)

        # 5b. Phase C: start the recall log writer. Optional via
        # ``start_recall_log`` — the worker subprocess (which only
        # ticks sub-agents and never serves recall queries) doesn't
        # need it. Tests that exercise recall() against ``from_pool``
        # substrates DO want it on, hence the default True.
        if start_recall_log:
            from substrate.recall.log import RecallLogWriter
            from substrate import config as _cfg

            substrate.recall_log = RecallLogWriter(
                substrate, max_queue_depth=_cfg.RECALL_LOG_QUEUE_DEPTH
            )
            substrate.recall_log.start()

        # 6. Bind hook module to this substrate instance. Optional via
        # ``bind_hooks`` — the worker subprocess (no perception hooks
        # fire there) skips this so the global hook-target reference
        # stays bound to whichever writer process owns it.
        if bind_hooks:
            from substrate.events import thoth_hooks

            thoth_hooks._bind(substrate)

        substrate.log.info(
            "substrate.boot.ok streams_registered=%d subagents=%d hooks=%s recall_log=%s",
            len(_autoregister_specs()) + 1,  # +1 for the seeded self_state
            len(substrate._subagents),
            "on" if bind_hooks else "off",
            "on" if start_recall_log else "off",
        )
        return substrate

    @classmethod
    async def boot_writer(
        cls,
        *,
        log: "Optional[_logging.Logger]" = None,
        config: Optional[SubstrateConfig] = None,
    ) -> "Substrate":
        """Boot in **writer** mode (perception emitters + recall reads).

        Use this from any process that emits perception slices via the
        hook surface OR queries the recall API: the gateway, the chat
        CLI, the ACP adapter, the cron runner. Streams are auto-
        registered, perception hooks get bound, and the recall log
        writer starts. **Sub-agent tick loops are NOT started** — those
        run in a dedicated ``thoth substrate worker run`` subprocess
        so they own their own event loop and asyncpg pool.

        Why the split: when sub-agents tick in a process that also
        runs ``thoth_db.run_sync`` (worker-thread bridge for sync DB
        callers like gateway session handlers), the substrate's
        main-loop pool and run_sync's ``_sync_loop`` pool collide. PG
        sees the same connection driven from two event loops, asyncpg
        rejects with ``cannot switch to state N; another operation
        is in progress``, the Curator/Sentinel ticks fail in a 5-second
        loop. The 2026-05-26 production incident: 4 hours of gateway
        uptime with zero embeddings landed because every Curator tick
        crashed on this race. Moving the sub-agents to their own
        process gives each loop a clean single-owner pool.
        """
        return await cls.boot(
            log=log,
            config=config,
            start_subagents=False,
            bind_hooks=True,
            start_recall_log=True,
        )

    @classmethod
    async def boot_worker(
        cls,
        *,
        log: "Optional[_logging.Logger]" = None,
        config: Optional[SubstrateConfig] = None,
    ) -> "Substrate":
        """Boot in **worker** mode (sub-agent tick loops only).

        Use this exclusively from the ``thoth substrate worker run``
        subprocess. Streams auto-register (idempotent — safe even if
        the writer process already registered them), sub-agent tick
        loops start (Sentinel, Curator, ForceRejectWorker,
        PartitionMaintenanceWorker; Conductor instantiated), but:

        * **Perception hooks are NOT bound** — the worker subprocess
          doesn't receive Thoth-side ``on_user_message`` / etc. calls.
          The hook target stays bound to whatever writer process owns
          it (typically the gateway).
        * **Recall log writer NOT started** — the worker doesn't serve
          recall queries; only writer processes do.

        See :meth:`boot_writer` for the rationale behind the split.
        """
        return await cls.boot(
            log=log,
            config=config,
            start_subagents=True,
            bind_hooks=False,
            start_recall_log=False,
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop sub-agents, flush pending audit emissions, unbind hooks.

        The asyncpg pool is NOT closed here — that belongs to
        ``thoth_db.close()`` which is owned by Thoth's own shutdown
        sequence (spec §8.2).
        """
        # Unbind hooks first so any in-flight Thoth call site that
        # reaches a hook during shutdown is a silent no-op rather than
        # racing against a partially-shut-down substrate.
        from substrate.events import thoth_hooks

        thoth_hooks._unbind()

        # Stop every running sub-agent with a bounded wait. The base
        # class's ``stop_and_wait`` cancels the task on timeout so a
        # misbehaving tick can't hang shutdown forever.
        if self._subagents:
            await asyncio.gather(
                *(
                    agent.stop_and_wait(timeout=_SUBAGENT_SHUTDOWN_TIMEOUT)
                    for agent in self._subagents.values()
                ),
                return_exceptions=True,
            )
            self._subagents.clear()
        self._conductor = None

        # Phase C: stop the recall log writer. In-flight queue contents
        # are dropped (per the spec — substrate shutdown is bounded).
        if self.recall_log is not None:
            try:
                await self.recall_log.stop()
            except Exception as exc:
                self.log.warning("recall_log.stop failed: %s", exc)
            self.recall_log = None

        self.log.info("substrate.shutdown.ok")

    # ------------------------------------------------------------------
    # Boot helpers
    # ------------------------------------------------------------------

    async def _check_alembic_head(self, *, auto_migrate: bool) -> None:
        """Verify the DB's Alembic head includes the substrate revision.

        The Phase 0 baseline created ``alembic_version`` — we just SELECT
        from it. If the revision is older than expected:
          * with ``auto_migrate=True``: run ``alembic upgrade head``.
          * otherwise: raise RuntimeError so the caller can prompt.
        """
        import thoth_db

        async with thoth_db.connection() as conn:
            current = await conn.fetchval(
                "SELECT version_num FROM alembic_version"
            )

        if current in _EXPECTED_REVISIONS:
            return

        if not auto_migrate:
            raise RuntimeError(
                f"substrate.boot: alembic head is {current!r}; expected one of "
                f"{sorted(_EXPECTED_REVISIONS)}. Set THOTH_AUTO_MIGRATE=1 or "
                f"run `alembic upgrade head` against this database."
            )

        # Run alembic upgrade in a thread — alembic.command.upgrade is
        # sync and would otherwise block the event loop.
        self.log.info(
            "substrate.boot.auto_migrate from=%s to=head", current
        )
        await asyncio.to_thread(_run_alembic_upgrade)

        # Verify the migration actually advanced the head.
        async with thoth_db.connection() as conn:
            after = await conn.fetchval(
                "SELECT version_num FROM alembic_version"
            )
        if after not in _EXPECTED_REVISIONS:
            raise RuntimeError(
                f"substrate.boot: alembic upgrade landed at {after!r}, not "
                f"one of {sorted(_EXPECTED_REVISIONS)}"
            )

    async def _autoregister_streams(self) -> None:
        """Insert every §9 stream via ``StreamRepo.register`` (idempotent
        on the unique ``name`` index)."""
        for (
            name,
            family,
            modality,
            source,
            organ,
            decay_profile_id,
        ) in _autoregister_specs():
            await self.streams.register(
                name=name,
                family=family,
                modality=modality,
                source=source,
                organ=organ,
                decay_profile_id=decay_profile_id,
            )

    def _spawn_subagents(self) -> None:
        """Instantiate + ``start()`` the Phase A + Phase B sub-agents.

        Order is deliberate:
          * partition-maintenance first — touches storage DDL, cheap.
          * force-reject second — protects the pending queue.
          * curator third — real decay/release loop (Phase B).
          * parser fourth — L0→L1 consolidation (Phase D); no-op tick
            unless THOTH_SUBSTRATE_PARSER=1, so registering it is free.
          * sentinel last — the highest-frequency tick.
        Conductor is instantiated but doesn't tick (still a stub —
        Phase B adds the push-on-set_intensity hook so live agents
        pick up intensity changes within one tick).
        """
        from substrate.agents.associator import Associator
        from substrate.agents.conductor import StubConductor
        from substrate.agents.conductor_policy import AdaptiveConductor
        from substrate.agents.critic import Critic
        from substrate.agents.curator import Curator
        from substrate.agents.dreamer import Dreamer
        from substrate.agents.force_reject import ForceRejectWorker
        from substrate.agents.parser import Parser
        from substrate.agents.pattern_finder import PatternFinder
        from substrate.agents.partition_maintenance import (
            PartitionMaintenanceWorker,
        )
        from substrate.agents.reflector import Reflector
        from substrate.agents.sentinel import StubSentinel
        from substrate.agents.skill_scout import SkillScout
        from substrate.agents.summarizer import Summarizer
        from substrate.agents.tuner_watch import RecallTunerWatch

        self._conductor = StubConductor(self)

        partition = PartitionMaintenanceWorker(self)
        force_reject = ForceRejectWorker(self)
        curator = Curator(self)
        parser = Parser(self)            # Phase D  — gated THOTH_SUBSTRATE_PARSER
        associator = Associator(self)    # Phase E1 — gated THOTH_SUBSTRATE_ASSOCIATOR
        pattern_finder = PatternFinder(self)  # Phase E2 — gated THOTH_SUBSTRATE_PATTERNFINDER
        critic = Critic(self)            # Phase F  — gated THOTH_SUBSTRATE_CRITIC
        reflector = Reflector(self)      # Phase F  — gated THOTH_SUBSTRATE_REFLECTOR
        dreamer = Dreamer(self)          # Phase F  — gated THOTH_SUBSTRATE_DREAMER
        summarizer = Summarizer(self)    # gated THOTH_SUBSTRATE_SUMMARIZER
        skill_scout = SkillScout(self)   # self-improvement Tier 1 — gated THOTH_SUBSTRATE_SKILL_SCOUT
        tuner_watch = RecallTunerWatch(self)  # report-only daily fit — gated THOTH_SUBSTRATE_TUNER_WATCH (#288)
        # Adaptive Conductor policy loop (Phase F). Drives the StubConductor
        # (self._conductor) when THOTH_SUBSTRATE_CONDUCTOR=1; no-op otherwise.
        conductor_policy = AdaptiveConductor(self)
        sentinel = StubSentinel(self)
        for agent in (
            partition, force_reject, curator, parser, associator,
            pattern_finder, critic, reflector, dreamer, summarizer,
            skill_scout, tuner_watch, conductor_policy, sentinel,
        ):
            agent.start()
            self._subagents[agent.name] = agent

    # ------------------------------------------------------------------
    # Accessors — convenient for the inspect CLI and tests.
    # ------------------------------------------------------------------

    @property
    def subagents(self) -> dict[str, object]:
        """Read-only view of the running sub-agents keyed by ``name``.
        Empty when ``Substrate`` was built via :meth:`from_pool` or with
        ``start_subagents=False``.
        """
        return dict(self._subagents)

    @property
    def conductor(self) -> "Optional[object]":
        """The StubConductor instance, or ``None`` if not booted."""
        return self._conductor


def _run_alembic_upgrade() -> None:
    """Thread-pool entry point for ``alembic upgrade head``.

    Kept at module scope so the Substrate doesn't carry an alembic
    reference at import time (alembic is a heavy import).
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config("migrations/alembic.ini")
    command.upgrade(cfg, "head")


__all__ = ["Substrate"]
