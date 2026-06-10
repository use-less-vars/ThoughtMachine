"""
Factory pattern for creating provider instances.
Centralizes provider creation logic and handles configuration.
"""
from typing import Dict, Any, Optional
import os
from .base import ProviderConfig, LLMProvider
from .exceptions import ProviderNotFoundError, InvalidConfigError


class ProviderFactory:
    """
    Factory for creating LLM provider instances.
    Implements the Factory pattern.

    Provider classes are imported lazily to avoid circular imports
    (agent -> llm_providers -> agent.logging).
    """

    _providers = None  # Lazily initialized

    @classmethod
    def _get_providers(cls) -> Dict[str, type]:
        """Lazy-load provider classes to avoid circular imports."""
        if cls._providers is not None:
            return cls._providers

        from .openai_compatible import OpenAICompatibleProvider
        providers = {
            "openai_compatible": OpenAICompatibleProvider,
        }

        try:
            from .anthropic_provider import AnthropicProvider
            providers["anthropic"] = AnthropicProvider
        except ImportError as e:
            # Lazy import to avoid circular dependency
            from agent.logging import log
            log('WARNING', 'llm.factory', f'Anthropic provider not available: {e}')

        cls._providers = providers
        return cls._providers

    @classmethod
    def register_provider(cls, name: str, provider_class):
        """Register a new provider type"""
        providers = cls._get_providers()
        providers[name.lower()] = provider_class

    @classmethod
    def create_provider(
        cls,
        provider_type: str,
        api_key: Optional[str] = None,
        **config
    ) -> LLMProvider:
        """
        Create a provider instance from configuration.

        Args:
            provider_type: Type of provider (e.g., "openai_compatible", "anthropic")
            api_key: API key (can also be in config or env var)
            **config: Additional configuration parameters

        Returns:
            LLMProvider instance

        Raises:
            ProviderNotFoundError: If provider_type not registered
            InvalidConfigError: If required configuration missing
        """
        providers = cls._get_providers()
        provider_type = provider_type.lower()

        # Handle aliases
        if provider_type == "openai":
            provider_type = "openai_compatible"
            # Set default OpenAI base URL if not specified
            if "base_url" not in config:
                config["base_url"] = "https://api.openai.com/v1"

        if provider_type not in providers:
            raise ProviderNotFoundError(
                f"Provider '{provider_type}' not found. "
                f"Available: {list(providers.keys())}"
            )

        # Handle API key from multiple sources
        # Try provider-specific environment variable first
        env_var = f"{provider_type.upper()}_API_KEY"
        api_key = api_key or config.get("api_key") or os.getenv(env_var)

        # Try OPENAI_API_KEY for openai/openai_compatible
        if not api_key and provider_type in ["openai", "openai_compatible"]:
            api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise InvalidConfigError(
                f"No API key found for provider '{provider_type}'. "
                f"Provide api_key parameter or set {provider_type.upper()}_API_KEY environment variable."
            )

        # Create provider config
        provider_config = ProviderConfig(
            api_key=api_key,
            base_url=config.get("base_url"),
            model=config.get("model", ""),
            temperature=config.get("temperature", 0.7),
            timeout=config.get("timeout", 120),
            max_retries=config.get("max_retries", 3),
            extra_headers=config.get("extra_headers", {})
        )

        # Instantiate provider
        provider_class = providers[provider_type]
        return provider_class(provider_config)

    @classmethod
    def create_from_dict(cls, config_dict: Dict[str, Any]) -> LLMProvider:
        """
        Create provider from dictionary (useful for JSON configs).

        Example:
            {
                "provider_type": "openai_compatible",
                "api_key": "sk-...",
                "base_url": "https://api.opencode.com/v1",
                "model": "opencode/big-pickle",
                "temperature": 0.2
            }
        """
        if "provider_type" not in config_dict:
            raise InvalidConfigError("Config dict must contain 'provider_type'")

        provider_type = config_dict.pop("provider_type")
        return cls.create_provider(provider_type, **config_dict)