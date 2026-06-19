"""Focused tests for EvoLink provider profile wiring."""

from __future__ import annotations

from thoth_cli.auth import PROVIDER_REGISTRY, resolve_provider
from thoth_cli.models import (
    CANONICAL_PROVIDERS,
    _PROVIDER_LABELS,
    normalize_provider,
    provider_model_ids,
)


def test_evolink_profile_registered():
    from providers import get_provider_profile

    profile = get_provider_profile("evolink")

    assert profile is not None
    assert profile.display_name == "EvoLink"
    assert profile.base_url == "https://direct.evolink.ai/v1"
    assert profile.env_vars == ("EVOLINK_API_KEY", "EVOLINK_BASE_URL")
    assert profile.default_aux_model == "gpt-5.2"
    assert profile.fallback_models == (
        "gpt-5.2",
        "gpt-5.1",
        "gemini-3.1-flash-lite-preview",
        "deepseek-v4-flash",
    )


def test_evolink_auth_registry_auto_extended():
    assert "evolink" in PROVIDER_REGISTRY
    pconfig = PROVIDER_REGISTRY["evolink"]

    assert pconfig.name == "EvoLink"
    assert pconfig.auth_type == "api_key"
    assert pconfig.inference_base_url == "https://direct.evolink.ai/v1"
    assert pconfig.api_key_env_vars == ("EVOLINK_API_KEY",)
    assert pconfig.base_url_env_var == "EVOLINK_BASE_URL"


def test_evolink_aliases_resolve():
    assert resolve_provider("evolink") == "evolink"
    assert resolve_provider("evolink-ai") == "evolink"
    assert normalize_provider("evolinkai") == "evolink"
    assert normalize_provider("evo-link") == "evolink"
    assert normalize_provider("evo_link") == "evolink"


def test_evolink_provider_module_aliases_resolve():
    from thoth_cli.providers import normalize_provider as provider_normalize_provider

    assert provider_normalize_provider("evolink-ai") == "evolink"
    assert provider_normalize_provider("evolinkai") == "evolink"


def test_evolink_model_picker_auto_extended():
    slugs = [p.slug for p in CANONICAL_PROVIDERS]

    assert "evolink" in slugs
    assert _PROVIDER_LABELS["evolink"] == "EvoLink"


def test_evolink_provider_model_ids_falls_back_to_profile_models(monkeypatch):
    monkeypatch.delenv("EVOLINK_API_KEY", raising=False)
    monkeypatch.delenv("EVOLINK_BASE_URL", raising=False)

    assert provider_model_ids("evolink") == [
        "gpt-5.2",
        "gpt-5.1",
        "gemini-3.1-flash-lite-preview",
        "deepseek-v4-flash",
    ]


def test_evolink_optional_env_vars_auto_injected():
    from thoth_cli.config import OPTIONAL_ENV_VARS

    assert OPTIONAL_ENV_VARS["EVOLINK_API_KEY"]["category"] == "provider"
    assert OPTIONAL_ENV_VARS["EVOLINK_API_KEY"]["password"] is True
    assert OPTIONAL_ENV_VARS["EVOLINK_BASE_URL"]["category"] == "provider"
    assert OPTIONAL_ENV_VARS["EVOLINK_BASE_URL"]["password"] is False


def test_evolink_url_metadata_auto_extended():
    from agent.model_metadata import _URL_TO_PROVIDER

    assert _URL_TO_PROVIDER.get("direct.evolink.ai") == "evolink"
