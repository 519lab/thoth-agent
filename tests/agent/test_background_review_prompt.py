"""Tests for the evidence-gated skill-review prompt (innovation #8).

The background skill review used to open with a churn-biased framing — "Be
ACTIVE … a pass that does nothing is a missed learning opportunity" — which
pushed the forked review agent to emit a low-value skill edit almost every
session.  Innovation #8 reframes "no skill change" as the common, correct
outcome, gated on hard evidence: (a) a repeated multi-turn pattern, (b) a user
correction, or (c) a loaded skill that proved wrong; otherwise the review
replies "Nothing to save."

These assertions pin the new framing so a regression that reintroduces the
bias fails loudly.
"""

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _SKILL_REVIEW_PROMPT,
)


def test_skill_prompt_offers_nothing_to_save():
    """The 'Nothing to save.' exit must be present and presented as valid."""
    assert "Nothing to save." in _SKILL_REVIEW_PROMPT


def test_skill_prompt_drops_churn_bias_phrases():
    """The old 'be active / missed opportunity' framing must be gone."""
    assert "missed learning opportunity" not in _SKILL_REVIEW_PROMPT
    assert "Be ACTIVE" not in _SKILL_REVIEW_PROMPT
    assert (
        "most sessions produce at least one skill update"
        not in _SKILL_REVIEW_PROMPT
    )
    # The closing paragraph must not reassert the bias either.
    assert "should NOT be the" not in _SKILL_REVIEW_PROMPT


def test_skill_prompt_states_no_change_is_correct():
    """The opener must establish that no skill change is the common outcome."""
    lowered = _SKILL_REVIEW_PROMPT.lower()
    assert "no change is the common, correct outcome" in lowered


def test_skill_prompt_names_the_three_evidence_gates():
    """All three evidence gates (a)/(b)/(c) must be spelled out."""
    lowered = _SKILL_REVIEW_PROMPT.lower()
    assert "repeated" in lowered and "multi-turn pattern" in lowered  # (a)
    assert "user correction" in lowered  # (b)
    assert "proved wrong" in lowered  # (c)


def test_skill_prompt_keeps_how_to_shape_guidance():
    """The how-to-shape guidance (class-level skills, references/) is intact."""
    assert "CLASS-LEVEL skills" in _SKILL_REVIEW_PROMPT
    assert "`references/" in _SKILL_REVIEW_PROMPT
    # The preference-order ladder that governs HOW to update must remain.
    assert "UPDATE A CURRENTLY-LOADED SKILL" in _SKILL_REVIEW_PROMPT
    assert "CREATE A NEW CLASS-LEVEL UMBRELLA" in _SKILL_REVIEW_PROMPT


def test_combined_prompt_also_drops_churn_bias():
    """The combined memory+skill prompt shares the de-biased skill framing."""
    assert "Nothing to save." in _COMBINED_REVIEW_PROMPT
    assert "missed learning opportunity" not in _COMBINED_REVIEW_PROMPT
    assert (
        "most sessions produce at least one skill update"
        not in _COMBINED_REVIEW_PROMPT
    )
