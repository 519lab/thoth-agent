"""Live-path tuned-weight resolution — learned-recall-weights innovation.

Pure tests for ``substrate/recall/api.py``'s weight resolution: config
baseline by default, active tuned row when the gate is on, explicit
THOTH_RECALL_* env vars outranking the learned value per-field, and the
kill-switch restoring pure config behaviour. The DB read is monkeypatched
(``_cached_tuned_weights``); persistence itself is covered by
``test_recall_weights_store.py``.
"""

from __future__ import annotations

import pytest

from substrate import config as _cfg
from substrate.recall import api as recall_api
from substrate.recall.replay import Weights


_TUNED = Weights(
    similarity=0.4, keyword=0.4, salience=0.10, recency=0.30,
    half_life_hours=24.0,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    recall_api._reset_tuned_weights_cache()
    yield
    recall_api._reset_tuned_weights_cache()


def _patch_active(monkeypatch, weights):
    async def _stub():
        return weights

    monkeypatch.setattr(recall_api, "_cached_tuned_weights", _stub)


@pytest.mark.asyncio
async def test_no_active_row_resolves_to_config_baseline(monkeypatch):
    monkeypatch.setattr(_cfg, "RECALL_TUNED_WEIGHTS", True)
    _patch_active(monkeypatch, None)
    kwargs = await recall_api._effective_rank_weights()
    assert kwargs["salience_weight"] == _cfg.RECALL_SALIENCE_WEIGHT
    assert kwargs["recency_half_life_hours"] == (
        _cfg.RECALL_RECENCY_HALF_LIFE_HOURS
    )


@pytest.mark.asyncio
async def test_active_row_overrides_config(monkeypatch):
    monkeypatch.setattr(_cfg, "RECALL_TUNED_WEIGHTS", True)
    # No operator hand-overrides in play for the tuned fields.
    for env in (
        "THOTH_RECALL_SIMILARITY_WEIGHT",
        "THOTH_RECALL_KEYWORD_WEIGHT",
        "THOTH_RECALL_SALIENCE_WEIGHT",
        "THOTH_RECALL_RECENCY_WEIGHT",
        "THOTH_RECALL_RECENCY_HALF_LIFE_HOURS",
    ):
        monkeypatch.delenv(env, raising=False)
    _patch_active(monkeypatch, _TUNED)
    kwargs = await recall_api._effective_rank_weights()
    assert kwargs["salience_weight"] == pytest.approx(0.10)
    assert kwargs["recency_weight"] == pytest.approx(0.30)
    assert kwargs["recency_half_life_hours"] == pytest.approx(24.0)


@pytest.mark.asyncio
async def test_explicit_env_var_outranks_tuned_value(monkeypatch):
    """The operator's hand override wins per-field: an explicitly-set
    THOTH_RECALL_SALIENCE_WEIGHT keeps the config value for salience while
    the other fields still take the learned values."""
    monkeypatch.setattr(_cfg, "RECALL_TUNED_WEIGHTS", True)
    monkeypatch.setenv("THOTH_RECALL_SALIENCE_WEIGHT", "0.35")
    monkeypatch.delenv("THOTH_RECALL_RECENCY_WEIGHT", raising=False)
    _patch_active(monkeypatch, _TUNED)
    kwargs = await recall_api._effective_rank_weights()
    assert kwargs["salience_weight"] == _cfg.RECALL_SALIENCE_WEIGHT
    assert kwargs["recency_weight"] == pytest.approx(0.30)  # still tuned


@pytest.mark.asyncio
async def test_kill_switch_restores_pure_config(monkeypatch):
    """RECALL_TUNED_WEIGHTS off → the active row is never even read."""
    monkeypatch.setattr(_cfg, "RECALL_TUNED_WEIGHTS", False)

    async def _explode():  # pragma: no cover — must not be called
        raise AssertionError("tuned-weight read attempted with gate off")

    monkeypatch.setattr(recall_api, "_cached_tuned_weights", _explode)
    kwargs = await recall_api._effective_rank_weights()
    assert kwargs["salience_weight"] == _cfg.RECALL_SALIENCE_WEIGHT


@pytest.mark.asyncio
async def test_failed_read_degrades_to_baseline(monkeypatch):
    """A DB error inside the cached read resolves to None → config baseline
    (tuned weights may degrade recall quality, never its availability)."""
    monkeypatch.setattr(_cfg, "RECALL_TUNED_WEIGHTS", True)

    class _BoomDB:
        def connection(self):
            raise RuntimeError("no database")

    import sys

    monkeypatch.setitem(sys.modules, "thoth_db", _BoomDB())
    recall_api._reset_tuned_weights_cache()
    kwargs = await recall_api._effective_rank_weights()
    assert kwargs["salience_weight"] == _cfg.RECALL_SALIENCE_WEIGHT
