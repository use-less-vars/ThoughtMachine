"""
Configuration module for LLM providers.
Provides enhanced configuration models, validation, and loading.
"""

from .models import (
    BaseConfig,
    ProviderConfig,
    ProviderType,
    FallbackConfig,
    BudgetConfig,
    LLMConfig,
    PYDANTIC_AVAILABLE
)

from .loader import ConfigLoader

__all__ = [
    "BaseConfig",
    "ProviderConfig",
    "ProviderType",
    "FallbackConfig",
    "BudgetConfig",
    "LLMConfig",
    "ConfigLoader",
    "PYDANTIC_AVAILABLE"
]

__version__ = "0.1.0"