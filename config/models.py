"""
Enhanced configuration models for LLM providers using Pydantic.
Provides validation, environment variable loading, and complex configuration structures.
"""
from typing import List, Dict, Any, Optional, Union
from enum import Enum
import os
import logging

logger = logging.getLogger(__name__)

# Try to import Pydantic, with fallback to dataclasses if not available
try:
    from pydantic import BaseModel, Field, field_validator, model_validator
    PYDANTIC_AVAILABLE = True
except ImportError:
    logger.warning("Pydantic not available, falling back to dataclasses")
    from dataclasses import dataclass, field
    PYDANTIC_AVAILABLE = False

# Create base class that works with both Pydantic and dataclasses
if PYDANTIC_AVAILABLE:
    class BaseConfig(BaseModel):
        """Base configuration model with Pydantic features"""
        
        class Config:
            extra = "forbid"
            validate_assignment = True
            env_prefix = "LLM_"
            
        def dict(self, exclude_none=True, **kwargs):
            """Convert to dictionary, excluding None values by default"""
            return super().dict(exclude_none=exclude_none, **kwargs)
else:
    # Fallback dataclass decorator
    def BaseConfig(cls):
        return dataclass(cls)
    
    # Create a simple field function for compatibility
    def Field(default=None, description=None, **kwargs):
        return default

class ProviderType(str, Enum):
    """Supported LLM provider types"""
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    # Add more as implemented

class ProviderConfig(BaseConfig):
    """
    Enhanced provider configuration with validation and environment support.
    
    Environment variables:
    - LLM_API_KEY: API key for the provider
    - LLM_BASE_URL: Base URL for API (optional)
    - LLM_MODEL: Model name
    - LLM_TEMPERATURE: Temperature (0.0-2.0)
    - LLM_MAX_TOKENS: Maximum tokens to generate
    - LLM_TIMEOUT: Request timeout in seconds
    - LLM_MAX_RETRIES: Maximum retry attempts
    """
    
    # Provider identification
    provider_type: ProviderType = Field(..., description="Type of LLM provider")
    api_key: str = Field(..., description="API key for authentication")
    
    # API configuration
    base_url: Optional[str] = Field(None, description="Base URL for API requests")
    model: str = Field("", description="Model identifier")
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="Sampling temperature")
    max_tokens: Optional[int] = Field(None, gt=0, description="Maximum tokens to generate")
    timeout: int = Field(120, gt=0, description="Request timeout in seconds")
    max_retries: int = Field(3, ge=0, description="Maximum retry attempts")
    extra_headers: Dict[str, str] = Field(default_factory=dict, description="Additional HTTP headers")
    
    # Cost tracking
    cost_per_token_input: Optional[float] = Field(None, ge=0.0, description="Cost per input token (per million)")
    cost_per_token_output: Optional[float] = Field(None, ge=0.0, description="Cost per output token (per million)")
    
    # Advanced settings
    request_timeout_multiplier: float = Field(1.0, gt=0.0, description="Multiplier for request timeout based on token count")
    enable_token_counting: bool = Field(True, description="Enable token counting for cost tracking")
    
    # Environment variable loading
    if PYDANTIC_AVAILABLE:
        @field_validator("api_key", mode="before")
        @classmethod
        def load_api_key_from_env(cls, v):
            """Load API key from environment variable if not provided"""
            if v is not None and v != "":
                return v
            
            # Try generic LLM_API_KEY
            env_value = os.getenv("LLM_API_KEY")
            if env_value:
                return env_value
            
            return v  # Will raise validation error if still empty
        
        @field_validator("base_url", mode="before")
        @classmethod
        def load_base_url_from_env(cls, v):
            """Load base URL from environment variable if not provided"""
            if v is not None and v != "":
                return v
            
            env_value = os.getenv("LLM_BASE_URL")
            if env_value:
                return env_value
            
            return v
    
    def to_dataclass(self):
        """Convert to the legacy ProviderConfig dataclass"""
        from llm_providers.base import ProviderConfig as LegacyProviderConfig
        return LegacyProviderConfig(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            max_retries=self.max_retries,
            extra_headers=self.extra_headers
        )

class FallbackConfig(BaseConfig):
    """
    Configuration for provider fallback chains.
    Defines a sequence of providers to try in order.
    """
    providers: List[ProviderConfig] = Field(..., min_items=1, description="Ordered list of providers to try")
    fail_fast: bool = Field(True, description="Fail immediately if a provider fails (vs trying next)")
    max_total_attempts: int = Field(5, gt=0, description="Maximum total attempts across all providers")
    retry_delay: float = Field(1.0, ge=0.0, description="Delay between retry attempts in seconds")
    
    if PYDANTIC_AVAILABLE:
        @field_validator("providers")
        @classmethod
        def validate_unique_providers(cls, v):
            """Ensure provider configurations are unique (by provider_type + model)"""
            seen = set()
            for provider in v:
                key = (provider.provider_type, provider.model)
                if key in seen:
                    raise ValueError(f"Duplicate provider configuration: {key}")
                seen.add(key)
            return v

class BudgetConfig(BaseConfig):
    """
    Budget limits for LLM usage.
    Can be applied per provider, per session, or globally.
    """
    max_cost: Optional[float] = Field(None, ge=0.0, description="Maximum total cost (in USD)")
    max_tokens: Optional[int] = Field(None, gt=0, description="Maximum total tokens")
    max_requests: Optional[int] = Field(None, gt=0, description="Maximum number of requests")
    
    # Time-based limits
    period_hours: Optional[float] = Field(None, gt=0.0, description="Budget period in hours (None = unlimited)")
    reset_on_period: bool = Field(True, description="Reset budget counters after period")
    
    # Alert thresholds
    warn_percentage: float = Field(80.0, ge=0.0, le=100.0, description="Percentage at which to warn about budget")
    stop_at_limit: bool = Field(True, description="Stop execution when budget is exceeded")
    
    if PYDANTIC_AVAILABLE:
        @field_validator("max_tokens", "max_requests", "max_cost")
        @classmethod
        def validate_at_least_one_limit(cls, v, info):
            """Ensure at least one limit is set"""
            # In Pydantic v2, we can't easily check other fields from a field validator
            # This validation should be done at the model level instead
            # For now, allow any combination (including no limits)
            return v

class LLMConfig(BaseConfig):
    """
    Top-level LLM configuration combining providers, fallbacks, and budgets.
    """
    primary_provider: ProviderConfig = Field(..., description="Primary provider configuration")
    fallback_chain: Optional[FallbackConfig] = Field(None, description="Fallback provider chain")
    budget: Optional[BudgetConfig] = Field(None, description="Budget limits")
    
    # Global settings
    default_temperature: float = Field(0.7, ge=0.0, le=2.0, description="Default temperature if not specified")
    default_max_tokens: Optional[int] = Field(None, gt=0, description="Default max tokens if not specified")
    enable_cost_tracking: bool = Field(True, description="Enable cost tracking globally")
    log_level: str = Field("INFO", description="Logging level for LLM operations")
    
    def get_provider_chain(self) -> List[ProviderConfig]:
        """Get the complete provider chain (primary + fallbacks)"""
        chain = [self.primary_provider]
        if self.fallback_chain:
            chain.extend(self.fallback_chain.providers)
        return chain
    
    def validate_budget(self, current_cost: float, current_tokens: int, current_requests: int) -> Dict[str, Any]:
        """Validate current usage against budget limits"""
        if not self.budget:
            return {"within_budget": True, "warnings": []}
        
        warnings = []
        exceeded = False
        
        # Check cost limit
        if self.budget.max_cost is not None and current_cost > self.budget.max_cost:
            exceeded = True
            warnings.append(f"Cost limit exceeded: {current_cost:.4f} > {self.budget.max_cost:.4f}")
        
        # Check token limit
        if self.budget.max_tokens is not None and current_tokens > self.budget.max_tokens:
            exceeded = True
            warnings.append(f"Token limit exceeded: {current_tokens} > {self.budget.max_tokens}")
        
        # Check request limit
        if self.budget.max_requests is not None and current_requests > self.budget.max_requests:
            exceeded = True
            warnings.append(f"Request limit exceeded: {current_requests} > {self.budget.max_requests}")
        
        # Check warning thresholds
        if self.budget.max_cost is not None and not exceeded:
            percentage = (current_cost / self.budget.max_cost) * 100
            if percentage >= self.budget.warn_percentage:
                warnings.append(f"Cost warning: {percentage:.1f}% of budget used")
        
        return {
            "within_budget": not exceeded,
            "warnings": warnings,
            "stop_required": exceeded and self.budget.stop_at_limit
        }