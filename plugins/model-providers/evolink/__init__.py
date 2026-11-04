"""EvoLink provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

evolink = ProviderProfile(
    name="evolink",
    aliases=("evolink-ai", "evolinkai", "evo-link", "evo_link"),
    display_name="EvoLink",
    description="EvoLink - OpenAI-compatible model gateway",
    signup_url="https://evolink.ai/dashboard/keys",
    env_vars=("EVOLINK_API_KEY", "EVOLINK_BASE_URL"),
    base_url="https://direct.evolink.ai/v1",
    auth_type="api_key",
    default_aux_model="gpt-5.2",
    fallback_models=(
        "gpt-5.2",
        "gpt-5.1",
        "gemini-3.1-flash-lite-preview",
        "deepseek-v4-flash",
    ),
)

register_provider(evolink)
