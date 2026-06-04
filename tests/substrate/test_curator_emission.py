"""Tests for Curator telemetry emission.

Every release and every alarm produces one ``substrate_telemetry`` row
(operational telemetry, non-perceptual — they used to be slices on
``substrate.self_state``, which fed the L0 feedback loop). Emissions run
AFTER the relevant transaction commits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from substrate import Substrate
from substrate.agents import Curator
from substrate.l0 import commit_slice
from substrate.storage import Family, Modality


@pytest_asyncio.fixture
async def substrate(thoth_db_initialized):
    import thoth_db

    return Substrate.from_pool(thoth_db.pool())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _register_profile(
    pool, name: str, *, tombstone_policy: str = "thin",
    release_after_consolidation: bool = False,
    window_seconds: int = 60,
    reinforcement_bump: float = 0.2,
    min_salience: float = 0.05,
) -> UUID:
    profile_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO substrate_decay_profiles
                (profile_id, name, natural_half_life, consolidation_window,
                 reinforcement_bump, min_salience_to_retain,
                 release_after_consolidation, pending_ttl,
                 tombstone_policy, applies_to_modality)
            VALUES
                ($1, $2, interval '1 hour', make_interval(secs => $3),
                 $4, $5, $6, interval '30 seconds',
                 $7, 'structured_event')
            """,
            profile_id, name, float(window_seconds), float(reinforcement_bump),
            float(min_salience), release_after_consolidation, tombstone_policy,
        )
    return profile_id


@pytest.mark.asyncio
async def test_release_emits_telemetry(substrate):
    """One release → one ``curator.release`` row in ``substrate_telemetry``."""
    import thoth_db

    profile_id = await _register_profile(substrate.pool, "test-emit-rel")
    stream = await substrate.streams.register(
        name="hermes.test.emit_release",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    await commit_slice(
        substrate, stream.stream_id, {"x": 1}, event_time_world=_now_utc()
    )
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET sentinel_state='passed', trust_score=0.5,
                   pending_committed_at=NULL,
                   salience_score=0.01,
                   salience_updated_at=now() - interval '1 minute'
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id=$1 ORDER BY ingest_time_world DESC LIMIT 1
             )
            RETURNING slice_id
            """,
            stream.stream_id,
        )

    curator = Curator(substrate)
    released = await curator._evaluate_releases()
    assert len(released) == 1
    await curator._emit_release_audit(released)

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT event, payload
              FROM substrate_telemetry
             WHERE event = 'curator.release'
               AND payload->>'slice_id' = $1
            """,
            str(slice_id),
        )
    assert len(rows) == 1
    assert rows[0]["event"] == "curator.release"
    payload = rows[0]["payload"]
    assert payload["slice_id"] == str(slice_id)
    assert payload["stream_id"] == str(stream.stream_id)
    assert payload["tombstone_policy"] == "thin"
    assert payload["salience_at_release"] == pytest.approx(0.01, abs=0.001)


@pytest.mark.asyncio
async def test_alarm_emits_telemetry(substrate):
    import thoth_db

    profile_id = await _register_profile(
        substrate.pool, "test-emit-alarm",
        window_seconds=60, reinforcement_bump=0.3,
    )
    stream = await substrate.streams.register(
        name="hermes.test.emit_alarm",
        family=Family.SELF_STATE,
        modality=Modality.STRUCTURED_EVENT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    await commit_slice(
        substrate, stream.stream_id, {"x": 1}, event_time_world=_now_utc()
    )
    async with thoth_db.connection() as conn:
        slice_id = await conn.fetchval(
            """
            UPDATE substrate_slices
               SET sentinel_state='passed', trust_score=0.5,
                   pending_committed_at=NULL,
                   salience_score=0.4,
                   event_time_world      = now() - interval '120 seconds',
                   perception_time_world = now() - interval '120 seconds',
                   ingest_time_world     = now() - interval '120 seconds',
                   time_start_world      = now() - interval '120 seconds',
                   time_end_world        = now() - interval '120 seconds',
                   salience_updated_at   = now() - interval '120 seconds'
             WHERE slice_id = (
                 SELECT slice_id FROM substrate_slices
                  WHERE stream_id=$1 ORDER BY ingest_time_world DESC LIMIT 1
             )
            RETURNING slice_id
            """,
            stream.stream_id,
        )

    curator = Curator(substrate)
    # Production cooldown (1 hour) would suppress this freshly-seeded
    # slice. Disable cooldown so the alarm fires on the first tick.
    curator.ALARM_COOLDOWN_SECONDS = 0
    alarmed = await curator._alarm_pathological()
    assert len(alarmed) == 1
    await curator._emit_alarm_audit(alarmed)

    async with thoth_db.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT event, payload
              FROM substrate_telemetry
             WHERE event = 'curator.pathological_forgetting_alarm'
               AND payload->>'slice_id' = $1
            """,
            str(slice_id),
        )
    assert len(rows) == 1
    assert rows[0]["event"] == "curator.pathological_forgetting_alarm"
    payload = rows[0]["payload"]
    assert payload["slice_id"] == str(slice_id)
    assert payload["age_seconds"] >= 60
    assert payload["consolidation_window_seconds"] == 60
    # ``bumped_to`` historically reflected the post-bump salience. After
    # the alarm-amplification fix, alarm no longer modifies salience —
    # the field now records the current (unchanged) salience for audit.
    assert payload["bumped_to"] == pytest.approx(0.4, abs=0.001)


@pytest.mark.asyncio
async def test_no_emit_when_nothing_to_audit(substrate):
    """Quiet tick (nothing to release, nothing to alarm) produces zero
    ``curator.*`` telemetry rows."""
    import thoth_db

    curator = Curator(substrate)
    await curator._emit_release_audit([])
    await curator._emit_alarm_audit([])

    async with thoth_db.connection() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM substrate_telemetry WHERE event LIKE 'curator.%'"
        )
    assert n == 0


@pytest.mark.asyncio
async def test_curator_embed_omits_model_kwarg_when_config_default(
    substrate, monkeypatch
):
    """Regression: when ``HERMES_RECALL_EMBEDDING_MODEL`` is unset,
    the Curator must NOT pass ``model=`` to ``embed()`` — letting
    ``embed()`` resolve from ``auxiliary.embedding.model`` in config.

    The 2026-05-26 production bug: ``RECALL_EMBEDDING_MODEL`` defaulted
    to the hardcoded ``"text-embedding-3-small"`` even when the operator
    had ``auxiliary.embedding.model: nomic-embed-text`` in config.yaml.
    The Curator passed that hardcoded name to Ollama → 404 → embed()
    returned ``[None]*N`` → every backfill marked slices ``embedding_failed``.

    With the fix, the Curator passes no ``model=`` kwarg when the env
    var is unset, so embed() reads config.yaml's value.
    """
    from substrate import config as _cfg
    from substrate.agents import curator as _curator_mod

    # Force RECALL_EMBEDDING_MODEL=None (the env-unset default).
    monkeypatch.setattr(_cfg, "RECALL_EMBEDDING_MODEL", None)

    # Seed one passed slice on a stream so list_unembedded returns it.
    profile_id = await _register_profile(substrate.pool, "embed_kwarg_test")
    stream = await substrate.streams.register(
        name="hermes.test.embed_kwarg",
        family=Family.EXTEROCEPTIVE,
        modality=Modality.TEXT,
        source="test",
        organ="pytest",
        decay_profile_id=profile_id,
    )
    await commit_slice(
        substrate, stream.stream_id, "hello", event_time_world=_now_utc(),
    )
    # Promote to passed via the Sentinel so it's a candidate for embedding.
    from substrate.agents import StubSentinel
    await StubSentinel(substrate).tick()

    # Capture the kwargs ``embed`` is called with.
    captured: list[dict] = []

    async def _spy_embed(texts, **kw):
        captured.append(kw)
        return [[0.0] * 1536 for _ in texts]

    monkeypatch.setattr(_curator_mod, "embed", _spy_embed)

    curator = Curator(substrate)
    await curator._emit_embeddings_for_unembedded()

    assert captured, "Curator never called embed()"
    assert "model" not in captured[0], (
        f"Curator passed model={captured[0].get('model')!r} to embed() "
        "even though RECALL_EMBEDDING_MODEL is None. This overrides the "
        "auxiliary.embedding.model from config.yaml and breaks every "
        "non-OpenAI provider (the 2026-05-26 prod incident)."
    )
