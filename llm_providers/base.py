"""
Abstract base classes for LLM providers.
Implements the Adapter pattern for provider abstraction.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import re
import time

#: Replacement text for redacted header values.
REDACTED_HEADER_VALUE = '<redacted>'

#: Header values that carry credentials verbatim.
_SECRET_HEADER_VALUE_PATTERNS = (
    re.compile(r'^(?:Bearer|Basic)\s+\S+$'),
    re.compile(r'^sk-[A-Za-z0-9_-]+$'),
)


def _redact_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Return a copy of *headers* with credential-shaped values redacted.

    Applied wherever headers (``extra_headers`` / ``default_headers``) are
    interpolated into a log line.  Values matching ``Bearer <token>`` /
    ``Basic <token>`` or a bare ``sk-...`` API key are replaced with
    ``<redacted>``; every other value is passed through unchanged so the log
    line stays debuggable.

    Never raises: a non-dict / ``None`` input comes back as ``{}``.
    """
    if not headers:
        return {}
    try:
        redacted: Dict[str, str] = {}
        for name, value in dict(headers).items():
            text = value if isinstance(value, str) else str(value)
            if any(p.match(text) for p in _SECRET_HEADER_VALUE_PATTERNS):
                text = REDACTED_HEADER_VALUE
            redacted[name] = text
        return redacted
    except Exception:
        # Redaction must never break provider construction.
        return dict(headers) if isinstance(headers, dict) else {}

@dataclass
class LLMResponse:
    """Normalized response from any LLM provider"""
    content: str
    reasoning: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    usage: Dict[str, int] = None  # {"prompt_tokens": x, "completion_tokens": y}
    raw_response: Any = None
    provider: str = "unknown"
    model: str = "unknown"
    latency_ms: float = 0.0
    
    def __post_init__(self):
        if self.usage is None:
            self.usage = {}

@dataclass
class ProviderConfig:
    """Configuration for a specific provider instance"""
    api_key: str
    base_url: Optional[str] = None
    model: str = ""
    temperature: float = 0.7
    top_p: Optional[float] = None
    timeout: int = 120
    max_retries: int = 3
    extra_headers: Dict[str, str] = None
    
    def __post_init__(self):
        if self.extra_headers is None:
            self.extra_headers = {}
        # A plain str api_key handed to the provider directly (provider built
        # without going through AgentConfig) is upgraded to Secret so the
        # class-level redaction applies to this layer's log lines too.
        try:
            from agent.config.secret import Secret
            if isinstance(self.api_key, str) and not isinstance(self.api_key, Secret) and self.api_key:
                self.api_key = Secret(self.api_key)
        except Exception:
            pass
        # Normalise explicit None back to the dataclass defaults so factory
        # callers (e.g. ``config.get("timeout", 120)``) that pass None through
        # never end up with a None timeout/max_retries on the provider.
        # 0 is preserved (0 max_retries == no retries).
        if self.timeout is None:
            self.timeout = 120
        if self.max_retries is None:
            self.max_retries = 3

class LLMProvider(ABC):
    """
    Abstract base class for all LLM providers.
    Uses Adapter pattern to normalize different provider APIs.
    """
    
    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_name = self.__class__.__name__.replace("Provider", "").lower()
        self._usage_stats = {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_cost": 0.0,
            "calls": 0
        }
    
    @abstractmethod
    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Main completion method - all providers must implement.
        Returns normalized LLMResponse.
        """
        pass
    
    @abstractmethod
    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        """Token counting for cost tracking"""
        pass
    
    def format_tools(self, tools: List[Dict]) -> Any:
        """
        Convert internal tool format to provider format.
        Override if provider uses different schema.
        """
        return tools
    
    def parse_response(self, raw_response: Any, start_time: float) -> LLMResponse:
        """
        Parse provider-specific response into normalized format.
        Override for provider-specific response structures.
        """
        latency = (time.time() - start_time) * 1000
        return LLMResponse(
            content="",
            raw_response=raw_response,
            provider=self.provider_name,
            model=self.config.model,
            latency_ms=latency
        )
    
    def track_usage(self, response: LLMResponse):
        """Track token usage and costs"""
        if response.usage:
            self._usage_stats["total_prompt_tokens"] += response.usage.get("prompt_tokens", 0)
            self._usage_stats["total_completion_tokens"] += response.usage.get("completion_tokens", 0)
            self._usage_stats["calls"] += 1
            # Calculate cost based on provider pricing
            self._usage_stats["total_cost"] += self._calculate_cost(response)
    
    def _calculate_cost(self, response: LLMResponse) -> float:
        """Calculate cost based on provider pricing - override in subclasses"""
        return 0.0
    
    def get_usage_stats(self) -> Dict:
        """Return current usage statistics"""
        return self._usage_stats.copy()
    
    def reset_usage_stats(self):
        """Reset usage statistics"""
        self._usage_stats = {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_cost": 0.0,
            "calls": 0
        }