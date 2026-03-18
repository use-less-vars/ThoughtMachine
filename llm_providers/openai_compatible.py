"""
OpenAI-compatible provider implementation.
Works with OpenAI, DeepSeek, OpenCode/Big Pickle, and any OpenAI-compatible API.
"""
from typing import Dict, List, Any, Optional
import time
import logging
import os
import sys

from openai import OpenAI, APIError, RateLimitError
import tiktoken

from .base import LLMProvider, ProviderConfig, LLMResponse
from .exceptions import ProviderError, RateLimitExceeded, AuthenticationError

logger = logging.getLogger(__name__)
if os.environ.get('DEBUG_OPENAI'):
    logger.setLevel(logging.DEBUG)

class OpenAICompatibleProvider(LLMProvider):
    """
    Provider for OpenAI-compatible APIs.
    Supports: OpenAI, DeepSeek, OpenCode/Big Pickle, Local LLMs with OpenAI interface.
    """
    
    # Provider-specific pricing (per 1M tokens) - update as needed
    PRICING = {
        "gpt-4": {"input": 30.0, "output": 60.0},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
        "deepseek-reasoner": {"input": 0.14, "output": 0.28},  # Example pricing
        "opencode/big-pickle": {"input": 0.0, "output": 0.0},  # Currently free 
    }
    
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        
        # Initialize OpenAI client
        client_kwargs = {
            "api_key": config.api_key,
            "timeout": config.timeout,
            "max_retries": config.max_retries,
        }
        
        # Debug logging
        logger.debug(f"OpenAI client config: base_url={config.base_url}, model={config.model}, api_key={config.api_key}, timeout={config.timeout}, max_retries={config.max_retries}, extra_headers={config.extra_headers}")
        print(f"[DEBUG_OPENAI] client config: base_url={config.base_url}, model={config.model}, api_key={config.api_key}, timeout={config.timeout}, max_retries={config.max_retries}, extra_headers={config.extra_headers}", file=sys.stderr)
        
        if config.base_url:
            client_kwargs["base_url"] = config.base_url
        
        if config.extra_headers:
            client_kwargs["default_headers"] = config.extra_headers
        
        # Debug logging after headers
        logger.debug(f"OpenAI client final kwargs: base_url={client_kwargs.get('base_url')}, default_headers={client_kwargs.get('default_headers')}")
        print(f"[DEBUG_OPENAI] client final kwargs: base_url={client_kwargs.get('base_url')}, default_headers={client_kwargs.get('default_headers')}", file=sys.stderr)
        
        self.client = OpenAI(**client_kwargs)
        logger.debug(f"OpenAI client created with base_url={self.client.base_url if hasattr(self.client, 'base_url') else 'default'}")
        print(f"[DEBUG_OPENAI] client created with base_url={self.client.base_url if hasattr(self.client, 'base_url') else 'default'}", file=sys.stderr)
        
        # Initialize tokenizer for token counting (lazy loading)
        self.encoding = None
        # We'll try to load tiktoken only when needed
        # This avoids network dependencies during initialization

    def _load_encoding(self):
        """Lazily load tiktoken encoding for token counting"""
        if self.encoding is not None:
            return
        
        try:
            # Try to get encoding for the model
            self.encoding = tiktoken.encoding_for_model(self.config.model)
            logger.debug(f"Loaded tokenizer for model: {self.config.model}")
        except KeyError:
            # Model not recognized by tiktoken, try to map to known encoding
            try:
                # Map known model families to encodings
                model_lower = self.config.model.lower()
                if "gpt-4" in model_lower or "gpt-3.5" in model_lower:
                    self.encoding = tiktoken.get_encoding("cl100k_base")
                elif "deepseek" in model_lower:
                    self.encoding = tiktoken.get_encoding("cl100k_base")
                elif "opencode" in model_lower or "big-pickle" in model_lower:
                    self.encoding = tiktoken.get_encoding("cl100k_base")
                else:
                    # Unknown model, fallback to cl100k_base (most common)
                    self.encoding = tiktoken.get_encoding("cl100k_base")
            except Exception as e:
                logger.warning(f"Failed to load tokenizer: {e}. Token counting will be approximate.")
                self.encoding = None
        except Exception as e:
            logger.warning(f"Failed to load tokenizer: {e}. Token counting will be approximate.")
            self.encoding = None

    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> LLMResponse:
        """Execute chat completion with OpenAI-compatible API"""
        start_time = time.time()
        
        try:
            # Prepare completion kwargs
            completion_kwargs = {
                "model": self.config.model,
                "messages": messages,
                "temperature": kwargs.get("temperature", self.config.temperature),
                **kwargs
            }
            
            if self.config.max_tokens:
                completion_kwargs["max_tokens"] = self.config.max_tokens
            
            # Add tools if provided
            if tools:
                completion_kwargs["tools"] = self.format_tools(tools)
                completion_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
            
            # Make API call
            logger.debug(f"OpenAI API call: model={completion_kwargs.get('model')}, temperature={completion_kwargs.get('temperature')}, max_tokens={completion_kwargs.get('max_tokens')}, tools_count={len(tools) if tools else 0}, base_url={self.client.base_url if hasattr(self.client, 'base_url') else 'default'}, api_key={self.config.api_key}")
            print(f"[DEBUG_OPENAI] API call: model={completion_kwargs.get('model')}, temperature={completion_kwargs.get('temperature')}, max_tokens={completion_kwargs.get('max_tokens')}, tools_count={len(tools) if tools else 0}, base_url={self.client.base_url if hasattr(self.client, 'base_url') else 'default'}, api_key={self.config.api_key}", file=sys.stderr)
            response = self.client.chat.completions.create(**completion_kwargs)
            
            # Debug: Print raw response if environment variable is set
            import os
            if os.environ.get('DEBUG_OPENAI'):
                raw_str = str(response)
                if len(raw_str) > 1000:
                    raw_str = raw_str[:1000] + f"... (truncated, total {len(raw_str)} chars)"
                print(f"[DEBUG_OPENAI_RAW] Raw API response type: {type(response)}", file=sys.stderr)
                print(f"[DEBUG_OPENAI_RAW] Raw API response: {raw_str}", file=sys.stderr)
            
            # Debug: Print raw response details before parsing
            import os
            if os.environ.get('DEBUG_OPENAI'):
                print(f"[DEBUG_BEFORE_PARSE] Response type: {type(response)}", file=sys.stderr)
                if hasattr(response, '__dict__'):
                    print(f"[DEBUG_BEFORE_PARSE] Response has __dict__, keys: {list(response.__dict__.keys())}", file=sys.stderr)
                    # Try to get a string representation of the response
                    try:
                        import json
                        resp_json = json.dumps(response.__dict__, default=str, indent=2)
                        if len(resp_json) > 2000:
                            resp_json = resp_json[:2000] + "... (truncated)"
                        print(f"[DEBUG_BEFORE_PARSE] Response JSON: {resp_json}", file=sys.stderr)
                    except:
                        pass
                elif isinstance(response, dict):
                    print(f"[DEBUG_BEFORE_PARSE] Response is dict, keys: {list(response.keys())}", file=sys.stderr)
                elif isinstance(response, str):
                    print(f"[DEBUG_BEFORE_PARSE] WARNING: Response is string, not JSON object: {response[:500]}", file=sys.stderr)
                else:
                    print(f"[DEBUG_BEFORE_PARSE] Response repr: {repr(response)[:500]}", file=sys.stderr)
            
            # Parse response
            try:
                llm_response = self.parse_response(response, start_time)
            except Exception as parse_error:
                # If parse fails, add more context and re-raise with raw response attached
                if os.environ.get('DEBUG_OPENAI'):
                    print(f"[DEBUG_PARSE_ERROR] Failed to parse response: {parse_error}", file=sys.stderr)
                    print(f"[DEBUG_PARSE_ERROR] Response that caused error: {response}", file=sys.stderr)
                
                # Create a ProviderError with the raw response
                parse_provider_error = ProviderError(f"Failed to parse API response: {parse_error}")
                parse_provider_error.raw_response = response
                raise parse_provider_error
            
            # Track usage
            self.track_usage(llm_response)
            
            return llm_response
            
        except RateLimitError as e:
            # Include raw response if available
            rate_limit_error = RateLimitExceeded(f"Rate limit exceeded: {e}")
            if hasattr(e, 'response'):
                rate_limit_error.raw_response = e.response
            raise rate_limit_error
        except APIError as e:
            if "authentication" in str(e).lower() or "api key" in str(e).lower():
                auth_error = AuthenticationError(f"Authentication failed: {e}")
                if hasattr(e, 'response'):
                    auth_error.raw_response = e.response
                raise auth_error
            api_error = ProviderError(f"API error: {e}")
            if hasattr(e, 'response'):
                api_error.raw_response = e.response
            raise api_error
        except Exception as e:
            # Add more debug info about what was returned
            import os
            if os.environ.get('DEBUG_OPENAI'):
                print(f"[DEBUG_OPENAI_ERROR] Exception type: {type(e)}", file=sys.stderr)
                print(f"[DEBUG_OPENAI_ERROR] Exception message: {e}", file=sys.stderr)
                # Try to get the response if it exists in the exception
                if hasattr(e, 'response'):
                    try:
                        resp_text = str(e.response)
                        if len(resp_text) > 1000:
                            resp_text = resp_text[:1000] + f"... (truncated, total {len(resp_text)} chars)"
                        print(f"[DEBUG_OPENAI_ERROR] Exception response: {resp_text}", file=sys.stderr)
                    except:
                        pass
            
            # Create a ProviderError with the raw response if available
            err_msg = f"Unexpected error: {e}"
            provider_error = ProviderError(err_msg)
            if hasattr(e, 'response'):
                provider_error.raw_response = e.response
                provider_error.args = (f"{err_msg}. Response: {e.response}",)
            
            # Also attach the actual response object from the API call if it exists
            if 'response' in locals():
                provider_error.raw_response = response
                
            raise provider_error
    
    def parse_response(self, raw_response: Any, start_time: float) -> LLMResponse:
        """Parse OpenAI-compatible response"""
        import os
        import sys
        
        latency = (time.time() - start_time) * 1000
        
        # Debug: print raw response details
        if os.environ.get('DEBUG_OPENAI'):
            print(f"[DEBUG_PARSE_RESPONSE] Starting parse, raw_response type: {type(raw_response)}", file=sys.stderr)
            if hasattr(raw_response, '__dict__'):
                print(f"[DEBUG_PARSE_RESPONSE] raw_response has __dict__", file=sys.stderr)
                for key, value in raw_response.__dict__.items():
                    if key == '_response' or key == 'response':
                        continue  # Skip large response objects
                    print(f"[DEBUG_PARSE_RESPONSE]   {key}: {value}", file=sys.stderr)
            elif isinstance(raw_response, dict):
                print(f"[DEBUG_PARSE_RESPONSE] raw_response is dict, keys: {list(raw_response.keys())}", file=sys.stderr)
            elif isinstance(raw_response, str):
                print(f"[DEBUG_PARSE_RESPONSE] raw_response is string (len={len(raw_response)}): {raw_response[:200]}", file=sys.stderr)
            else:
                print(f"[DEBUG_PARSE_RESPONSE] raw_response repr: {repr(raw_response)[:200]}", file=sys.stderr)
        
        # Check if raw_response is a string (error response from API)
        if isinstance(raw_response, str):
            if os.environ.get('DEBUG_OPENAI'):
                print(f"[DEBUG_PARSE_RESPONSE] ERROR: API returned string instead of JSON: {raw_response}", file=sys.stderr)
            raise ValueError(f"API returned string instead of JSON response: {raw_response[:200]}")
        
        # Check if raw_response has the expected structure
        if not hasattr(raw_response, 'choices'):
            if os.environ.get('DEBUG_OPENAI'):
                print(f"[DEBUG_PARSE_RESPONSE] ERROR: raw_response missing 'choices' attribute", file=sys.stderr)
                print(f"[DEBUG_PARSE_RESPONSE] raw_response attributes: {dir(raw_response)}", file=sys.stderr)
                if hasattr(raw_response, '__dict__'):
                    print(f"[DEBUG_PARSE_RESPONSE] raw_response __dict__ keys: {list(raw_response.__dict__.keys())}", file=sys.stderr)
            raise AttributeError(f"Response missing 'choices' attribute. Response type: {type(raw_response)}")
        
        if not raw_response.choices:
            if os.environ.get('DEBUG_OPENAI'):
                print(f"[DEBUG_PARSE_RESPONSE] ERROR: raw_response.choices is empty", file=sys.stderr)
            raise ValueError("Response has empty choices list")
        
        message = raw_response.choices[0].message        
        # Extract tool calls if present
        tool_calls = None
        if hasattr(message, 'tool_calls') and message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                # Handle both dictionary and object tool calls
                if hasattr(tc, 'function'):
                    # Object format (OpenAI SDK)
                    name = tc.function.name
                    arguments = tc.function.arguments
                    tc_id = tc.id
                else:
                    # Dictionary format
                    func = tc.get("function", {})
                    name = func.get("name")
                    arguments = func.get("arguments")
                    tc_id = tc.get("id")
                tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments
                    }
                })
        
        # Extract usage
        usage = {}
        if hasattr(raw_response, 'usage'):
            usage = {
                "prompt_tokens": raw_response.usage.prompt_tokens,
                "completion_tokens": raw_response.usage.completion_tokens,
                "total_tokens": raw_response.usage.total_tokens
            }
        
        # Extract reasoning content if present (e.g., DeepSeek reasoning)
        reasoning = None
        if hasattr(message, 'reasoning_content') and message.reasoning_content:
            reasoning = message.reasoning_content
        
        return LLMResponse(
            content=message.content or "",
            reasoning=reasoning,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=raw_response,
            provider=self.provider_name,
            model=self.config.model,
            latency_ms=latency
        )
    
    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        """Count tokens using tiktoken"""
        # Lazy load encoding if needed
        if self.encoding is None:
            self._load_encoding()
        
        if self.encoding is None:
            # If no tokenizer available, return approximate count
            text = ""
            for msg in messages:
                text += f"{msg.get('role', '')}: {msg.get('content', '')}\n"
            
            if tools:
                text += str(tools)
            
            # Approximate: 4 chars per token
            return len(text) // 4
        
        try:
            text = ""
            for msg in messages:
                text += f"{msg.get('role', '')}: {msg.get('content', '')}\n"
            
            if tools:
                text += str(tools)
            
            return len(self.encoding.encode(text))
        except Exception as e:
            logger.warning(f"Token counting failed: {e}")
            return 0
    
    def _calculate_cost(self, response: LLMResponse) -> float:
        """Calculate cost based on model pricing"""
        model = self.config.model
        pricing = self.PRICING.get(model, self.PRICING.get("gpt-3.5-turbo"))
        
        prompt_tokens = response.usage.get("prompt_tokens", 0)
        completion_tokens = response.usage.get("completion_tokens", 0)
        
        cost = (prompt_tokens * pricing["input"] / 1_000_000) + \
               (completion_tokens * pricing["output"] / 1_000_000)
        
        return cost
