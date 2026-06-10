"""
LLM Providers module for multi-provider support.
Implements Adapter pattern for normalizing different LLM provider APIs.
"""

from .base import (
    LLMProvider,
    LLMResponse,
    ProviderConfig,
)

from .factory import ProviderFactory
from .exceptions import (
    ProviderNotFoundError,
    InvalidConfigError,
    ProviderError,
)

# OpenAICompatibleProvider is available via ProviderFactory or lazy import below

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "ProviderConfig",
    "ProviderFactory",
    "ProviderNotFoundError",
    "InvalidConfigError",
    "ProviderError",
    "OpenAICompatibleProvider",
]


def __getattr__(name):
    """Lazy-import submodules to avoid circular dependencies."""
    if name == "OpenAICompatibleProvider":
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")