"""The hermes-* -> thoth-* toolset preset rename migrates existing configs.

Toolset presets were renamed (hermes-cli -> thoth-cli, etc.). Rather than carry
permanent back-compat aliases in the toolset registry, persisted configs are
rewritten in place by ``_normalize_toolset_names`` on load and save.
"""
from thoth_cli.config import _normalize_toolset_names, _TOOLSET_RENAME_MAP
import toolsets


def test_renamed_names_match_registry():
    """Every migration target must be a real toolset (no dangling rewrites)."""
    for old, new in _TOOLSET_RENAME_MAP.items():
        assert old.startswith("hermes-") and new == "thoth-" + old[len("hermes-"):]
        assert new in toolsets.TOOLSETS, f"{new} missing from registry"
        assert old not in toolsets.TOOLSETS, f"legacy {old} should be gone"


def test_root_and_nested_toolset_lists_are_rewritten():
    cfg = {
        "toolsets": ["hermes-cli", "web", "browser"],
        "disabled_toolsets": ["hermes-acp"],
        "platform_toolsets": {
            "slack": {"toolsets": ["hermes-slack", "hermes-telegram"]},
            "gateway": {"toolsets": ["hermes-gateway"]},
        },
    }
    out = _normalize_toolset_names(cfg)
    assert out["toolsets"] == ["thoth-cli", "web", "browser"]
    assert out["disabled_toolsets"] == ["thoth-acp"]
    assert out["platform_toolsets"]["slack"]["toolsets"] == ["thoth-slack", "thoth-telegram"]
    assert out["platform_toolsets"]["gateway"]["toolsets"] == ["thoth-gateway"]


def test_non_toolset_values_and_already_migrated_are_untouched():
    cfg = {
        "toolsets": ["thoth-cli", "custom-set"],
        "model": {"default": "claude-opus-4-8"},
        "note": "hermes-cli was the old default",  # bare string, not a list item
    }
    out = _normalize_toolset_names(cfg)
    assert out["toolsets"] == ["thoth-cli", "custom-set"]
    assert out["model"] == {"default": "claude-opus-4-8"}
    # idempotent
    assert _normalize_toolset_names(out) == out
