"""
Anthropic Claude provider implementation.
Supports Claude models with tool use .
"""
from typing import Dict, List, Any, Optional
import time
import json
import logging
import os
import sys

import anthropic
from anthropic import APIError, RateLimitError

from .base import LLMProvider, ProviderConfig, LLMResponse
from .tool_converter import ToolFormatConverter
from .exceptions import ProviderError, RateLimitExceeded, AuthenticationError
from agent.logging.debug_log import debug_log

logger = logging.getLogger(__name__)
if os.environ.get('DEBUG_ANTHROPIC'):
    logger.setLevel(logging.DEBUG)


class AnthropicProvider(LLMProvider):
    """
    Provider for Anthropic Claude API.
    Supports Claude 3 Opus, Sonnet, Haiku with tool use .
    """
    
    # Claude 3 pricing (per 1M tokens) - update as needed
    PRICING = {
        "claude-3-opus-20240229": {"input": 15.0, "output": 75.0},
        "claude-3-sonnet-20240229": {"input": 3.0, "output": 15.0},
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3.5-sonnet-20240620": {"input": 3.0, "output": 15.0},
    }
    
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        
        # Initialize Anthropic client
        self.client = anthropic.Anthropic(
            api_key=config.api_key,
            timeout=config.timeout,
            max_retries=config.max_retries
        )
        
        self.converter = ToolFormatConverter()
    
    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> LLMResponse:
        """Execute chat completion with Claude API"""
        start_time = time.time()
        
        try:
            # Convert messages to Anthropic format
            system_msg = None
            anthropic_messages = []
            
            for msg in messages:
                if msg["role"] == "system":
                    system_msg = msg["content"]
                else:
                    # Map roles: user/assistant only
                    role = "user" if msg["role"] == "user" else "assistant"
                    anthropic_messages.append({
                        "role": role,
                        "content": msg["content"]
                    })
            
            # Prepare API kwargs
            api_kwargs = {
                "model": self.config.model,
                "messages": anthropic_messages,
                "max_tokens": kwargs.get("max_tokens", self.config.max_tokens or 4096),
                "temperature": kwargs.get("temperature", self.config.temperature),
                **kwargs
            }
            
            if system_msg:
                api_kwargs["system"] = system_msg
            
            # Add tools if provided
            if tools:
                api_kwargs["tools"] = self.converter.to_anthropic(tools)
            
            # Make API call
            logger.debug(f"Anthropic API call: model={api_kwargs.get('model')}, temperature={api_kwargs.get('temperature')}, max_tokens={api_kwargs.get('max_tokens')}, tools_count={len(tools) if tools else 0}, api_key={self.config.api_key}")
            debug_log(f"[DEBUG_ANTHROPIC] API call: model={api_kwargs.get('model')}, temperature={api_kwargs.get('temperature')}, max_tokens={api_kwargs.get('max_tokens')}, tools_count={len(tools) if tools else 0}, api_key={self.config.api_key}", component="anthropic")
            response = self.client.messages.create(**api_kwargs)
            
            # Debug: Print raw response if environment variable is set
            if os.environ.get('DEBUG_ANTHROPIC'):
                raw_str = str(response)
                if len(raw_str) > 1000:
                    raw_str = raw_str[:1000] + f"... (truncated, total {len(raw_str)} chars)"
                debug_log(f"[DEBUG_ANTHROPIC_RAW] Raw API response type: {type(response)}", component="anthropic")
                debug_log(f"[DEBUG_ANTHROPIC_RAW] Raw API response: {raw_str}", component="anthropic")
            
            # Debug: Print raw response details before parsing
            if os.environ.get('DEBUG_ANTHROPIC'):
                debug_log(f"[DEBUG_BEFORE_PARSE] Response type: {type(response)}", component="anthropic")
                if hasattr(response, '__dict__'):
                    debug_log(f"[DEBUG_BEFORE_PARSE] Response has __dict__, keys: {list(response.__dict__.keys())}", component="anthropic")
                    # Try to get a string representation of the response
                    try:
                        import json
                        resp_json = json.dumps(response.__dict__, default=str, indent=2)
                        if len(resp_json) > 2000:
                            resp_json = resp_json[:2000] + "... (truncated)"
                        debug_log(f"[DEBUG_BEFORE_PARSE] Response JSON: {resp_json}", component="anthropic")
                    except:
                        pass
                elif isinstance(response, dict):
                    debug_log(f"[DEBUG_BEFORE_PARSE] Response is dict, keys: {list(response.keys())}", component="anthropic")
                elif isinstance(response, str):
                    debug_log(f"[DEBUG_BEFORE_PARSE] WARNING: Response is string, not JSON object: {response[:500]}", component="anthropic")
                else:
                    debug_log(f"[DEBUG_BEFORE_PARSE] Response repr: {repr(response)[:500]}", component="anthropic")
            
            # Parse response
            llm_response = self.parse_response(response, start_time)
            
            # Track usage
            self.track_usage(llm_response)
            
            return llm_response
            
        except RateLimitError as e:
            # Add debug logging
            if os.environ.get('DEBUG_ANTHROPIC'):
                debug_log(f"[DEBUG_ANTHROPIC_ERROR] RateLimitError: {e}", component="anthropic")
            raise RateLimitExceeded(f"Rate limit exceeded: {e}")
        except APIError as e:
            # Add debug logging
            if os.environ.get('DEBUG_ANTHROPIC'):
                debug_log(f"[DEBUG_ANTHROPIC_ERROR] APIError: {e}", component="anthropic")
                if hasattr(e, 'response'):
                    try:
                        resp_text = str(e.response)
                        if len(resp_text) > 1000:
                            resp_text = resp_text[:1000] + f"... (truncated, total {len(resp_text)} chars)"
                        debug_log(f"[DEBUG_ANTHROPIC_ERROR] APIError response: {resp_text}", component="anthropic")
                    except:
                        pass
            
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
            if os.environ.get('DEBUG_ANTHROPIC'):
                debug_log(f"[DEBUG_ANTHROPIC_ERROR] Exception type: {type(e)}", component="anthropic")
                debug_log(f"[DEBUG_ANTHROPIC_ERROR] Exception message: {e}", component="anthropic")
                # Try to get the response if it exists in the exception
                if hasattr(e, 'response'):
                    try:
                        resp_text = str(e.response)
                        if len(resp_text) > 1000:
                            resp_text = resp_text[:1000] + f"... (truncated, total {len(resp_text)} chars)"
                        debug_log(f"[DEBUG_ANTHROPIC_ERROR] Exception response: {resp_text}", component="anthropic")
                    except:
                        pass
            
            # Create a ProviderError with the raw response if available
            err_msg = f"Unexpected error: {e}"
            provider_error = ProviderError(err_msg)
            if hasattr(e, 'response'):
                provider_error.raw_response = e.response
            raise provider_error
    
    def parse_response(self, raw_response: Any, start_time: float) -> LLMResponse:
        """Parse Anthropic-specific response format """
        latency = (time.time() - start_time) * 1000

        # Debug: print raw response details
        if os.environ.get('DEBUG_ANTHROPIC'):
            debug_log(f"[DEBUG_PARSE_RESPONSE] Starting parse, raw_response type: {type(raw_response)}", component="anthropic")
            if hasattr(raw_response, '__dict__'):
                debug_log(f"[DEBUG_PARSE_RESPONSE] raw_response has __dict__", component="anthropic")
                for key, value in raw_response.__dict__.items():
                    if key == '_response' or key == 'response':
                        continue  # Skip large response objects
                    debug_log(f"[DEBUG_PARSE_RESPONSE]   {key}: {value}", component="anthropic")
            elif isinstance(raw_response, dict):
                debug_log(f"[DEBUG_PARSE_RESPONSE] raw_response is dict, keys: {list(raw_response.keys())}", component="anthropic")
            elif isinstance(raw_response, str):
                debug_log(f"[DEBUG_PARSE_RESPONSE] raw_response is string (len={len(raw_response)}): {raw_response[:200]}", component="anthropic")
            else:
                debug_log(f"[DEBUG_PARSE_RESPONSE] raw_response repr: {repr(raw_response)[:200]}", component="anthropic")
        
        # Check if raw_response is a string (error response from API)
        if isinstance(raw_response, str):
            if os.environ.get('DEBUG_ANTHROPIC'):
                debug_log(f"[DEBUG_PARSE_RESPONSE] ERROR: API returned string instead of JSON: {raw_response}", component="anthropic")
            raise ValueError(f"API returned string instead of JSON response: {raw_response[:200]}")
        
        # Check if raw_response has the expected structure for Anthropic
        if not hasattr(raw_response, 'content'):
            if os.environ.get('DEBUG_ANTHROPIC'):
                debug_log(f"[DEBUG_PARSE_RESPONSE] ERROR: raw_response missing 'content' attribute", component="anthropic")
                debug_log(f"[DEBUG_PARSE_RESPONSE] raw_response attributes: {dir(raw_response)}", component="anthropic")
                if hasattr(raw_response, '__dict__'):
                    debug_log(f"[DEBUG_PARSE_RESPONSE] raw_response __dict__ keys: {list(raw_response.__dict__.keys())}", component="anthropic")
            raise AttributeError(f"Response missing 'content' attribute. Response type: {type(raw_response)}")
        
        # Extract content and reasoning
        content = ""
        reasoning = ""
        tool_calls = []

        for content_block in raw_response.content:
            if content_block.type == "text":
                content = content_block.text
            elif content_block.type == "thinking":
                # Extract reasoning from thinking blocks
                if hasattr(content_block, 'thinking'):
                    reasoning += content_block.thinking + "\n"
                elif isinstance(content_block, dict) and 'thinking' in content_block:
                    reasoning += content_block['thinking'] + "\n"
            elif content_block.type == "tool_use":                # Handle both dictionary and object tool calls
                if hasattr(content_block, 'name'):
                    # Object format (Anthropic SDK)
                    name = content_block.name
                    arguments = json.dumps(content_block.input)
                    cb_id = content_block.id
                else:
                    # Dictionary format
                    name = content_block.get("name")
                    arguments = json.dumps(content_block.get("input", {}))
                    cb_id = content_block.get("id")
                tool_calls.append({
                    "id": cb_id,
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
                "prompt_tokens": raw_response.usage.input_tokens,
                "completion_tokens": raw_response.usage.output_tokens,
                "total_tokens": raw_response.usage.input_tokens + raw_response.usage.output_tokens
            }
        
        return LLMResponse(
            content=content,
            reasoning=reasoning if reasoning else None,
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
            raw_response=raw_response,
            provider="anthropic",
            model=self.config.model,
            latency_ms=latency
        )
    
    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        """
        Count tokens using Anthropic's token counting API .
        Falls back to estimation if API not available.
        """
        try:
            # Convert messages to Anthropic format
            anthropic_messages = []
            for msg in messages:
                if msg["role"] != "system":
                    anthropic_messages.append({
                        "role": "user" if msg["role"] == "user" else "assistant",
                        "content": msg["content"]
                    })
            
            # Use Anthropic's token counter
            response = self.client.beta.messages.count_tokens(
                model=self.config.model,
                messages=anthropic_messages
            )
            return response.input_tokens
            
        except Exception as e:
            logger.warning(f"Anthropic token counting failed: {e}")
            # Rough estimation: ~4 chars per token
            text = " ".join([m.get("content", "") for m in messages])
            return len(text) // 4
    
    def _calculate_cost(self, response: LLMResponse) -> float:
        """Calculate cost based on Claude pricing"""
        pricing = self.PRICING.get(self.config.model, self.PRICING["claude-3-haiku-20240307"])
        
        prompt_tokens = response.usage.get("prompt_tokens", 0)
        completion_tokens = response.usage.get("completion_tokens", 0)
        
        cost = (prompt_tokens * pricing["input"] / 1_000_000) + \
               (completion_tokens * pricing["output"] / 1_000_000)
        
        return cost