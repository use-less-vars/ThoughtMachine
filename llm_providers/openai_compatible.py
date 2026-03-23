"""
OpenAI-compatible provider implementation.
Works with OpenAI, DeepSeek, OpenCode/Big Pickle, and any OpenAI-compatible API.
"""
from typing import Dict, List, Any, Optional
import time
import logging
import os
import sys

from openai import OpenAI, APIError, RateLimitError, APIConnectionError
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

    
    def _normalize_deepseek_tool_calls(self, messages):
        """Normalize messages for DeepSeek API, ensuring proper IDs and tool call format.

         Key improvements:
         1. Preserve existing tool call IDs (don't overwrite)
         2. Only generate new IDs when ID is missing or None
         3. Convert IDs to strings
         4. Log mismatches between tool messages and assistant tool calls
        """
        import sys
        messages_with_ids = []
        
        # First pass: collect tool_call_ids from tool messages for reference
        tool_call_ids = []
        for msg in messages:
            if msg.get("role") == "tool" and "tool_call_id" in msg:
                tool_call_ids.append(msg["tool_call_id"])
        
        for i, msg in enumerate(messages):
            msg_copy = msg.copy()
            
            # Add message ID if missing (should already have from previous step)
            if "id" not in msg_copy or msg_copy["id"] is None or not isinstance(msg_copy["id"], str):
                msg_copy["id"] = str(i)
            
            # Normalize tool_calls in assistant messages
            if msg_copy.get("role") == "assistant" and "tool_calls" in msg_copy:
                tool_calls = msg_copy["tool_calls"]
                if tool_calls and isinstance(tool_calls, list):
                    normalized_tool_calls = []
                    for j, tc in enumerate(tool_calls):
                        if not isinstance(tc, dict):
                            tc = dict(tc) if hasattr(tc, '__dict__') else {"function": {"name": "", "arguments": "{}"}}
                        
                        tc_copy = tc.copy() if isinstance(tc, dict) else {}
                        
                        # Ensure tool call has an ID - preserve existing IDs
                        # Only generate new ID if id is missing or None
                        if "id" not in tc_copy or tc_copy["id"] is None:
                            # Try to use corresponding tool_call_id from tool messages if available
                            if j < len(tool_call_ids):
                                tc_copy["id"] = tool_call_ids[j]
                            else:
                                tc_copy["id"] = f"call_{i}_{j}"
                        # Ensure ID is string
                        if isinstance(tc_copy["id"], (int, float)):
                            tc_copy["id"] = str(tc_copy["id"])
                        
                        # Ensure type is "function" only if missing
                        if "type" not in tc_copy:
                            tc_copy["type"] = "function"
                        
                        # Convert flattened format to OpenAI format if needed
                        if "function" not in tc_copy:
                            tc_copy["function"] = {
                                "name": tc_copy.get("name", ""),
                                "arguments": tc_copy.get("arguments", "{}")
                            }
                            # Remove flattened fields but preserve id and type
                            tc_copy.pop("name", None)
                            tc_copy.pop("arguments", None)
                            tc_copy.pop("result", None)
                        
                        normalized_tool_calls.append(tc_copy)
                    
                    msg_copy["tool_calls"] = normalized_tool_calls
            
            # Ensure tool messages have proper structure
            if msg_copy.get("role") == "tool":
                if "tool_call_id" not in msg_copy:
                    # Try to infer from content or previous messages
                    msg_copy["tool_call_id"] = msg_copy.get("id", f"tool_{i}")
            
            messages_with_ids.append(msg_copy)
        
        # Debug logging
        print(f"[DEEPSEEK_TOOL_NORM] Processed {len(messages)} messages", file=sys.stderr)
        for i, msg in enumerate(messages_with_ids):
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    print(f"[DEEPSEEK_TOOL_NORM] Assistant tool call id={tc.get('id')}", file=sys.stderr)
            if msg.get("role") == "tool":
                print(f"[DEEPSEEK_TOOL_NORM] Tool message tool_call_id={msg.get('tool_call_id')}", file=sys.stderr)
        
        return messages_with_ids

    def _normalize_stepfun_tool_calls(self, messages):
        """Normalize messages for StepFun API via OpenRouter.
        
        StepFun expects tool calls to have either 'function' field (when type='function') 
        or 'custom' field (when type='custom'). OpenRouter may add 'index' field.
        This ensures tool calls have the required structure.
        """
        import sys
        messages_normalized = []
        
        for i, msg in enumerate(messages):
            msg_copy = msg.copy()
            
            # Normalize tool_calls in assistant messages
            if msg_copy.get("role") == "assistant" and "tool_calls" in msg_copy:
                tool_calls = msg_copy["tool_calls"]
                if tool_calls and isinstance(tool_calls, list):
                    normalized_tool_calls = []
                    for j, tc in enumerate(tool_calls):
                        if not isinstance(tc, dict):
                            tc = dict(tc) if hasattr(tc, '__dict__') else {}
                        
                        tc_copy = tc.copy() if isinstance(tc, dict) else {}
                        
                        # Preserve index field if present (added by OpenRouter)
                        # Ensure type field
                        if "type" not in tc_copy:
                            tc_copy["type"] = "function"
                        
                        # StepFun validation expects 'custom' type with 'custom' field
                        # Convert function tool calls to custom format
                        if tc_copy.get("type") == "function":
                            # Move function data to custom field
                            if "function" in tc_copy:
                                tc_copy["custom"] = tc_copy.pop("function")
                            else:
                                # Try to construct from flattened fields
                                tc_copy["custom"] = {
                                    "name": tc_copy.get("name", ""),
                                    "arguments": tc_copy.get("arguments", "{}")
                                }
                                # Remove flattened fields
                                tc_copy.pop("name", None)
                                tc_copy.pop("arguments", None)
                                tc_copy.pop("result", None)
                            # Change type to custom
                            tc_copy["type"] = "custom"
                        elif tc_copy.get("type") == "custom":
                            if "custom" not in tc_copy:
                                tc_copy["custom"] = {}
                        
                        normalized_tool_calls.append(tc_copy)
                    
                    msg_copy["tool_calls"] = normalized_tool_calls
            
            messages_normalized.append(msg_copy)
        
        # Debug logging
        print(f"[STEPFUN_TOOL_NORM] Processed {len(messages)} messages", file=sys.stderr)
        for i, msg in enumerate(messages_normalized):
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    print(f"[STEPFUN_TOOL_NORM] Assistant tool call id={tc.get('id')}, type={tc.get('type')}, has_function={'function' in tc}, has_custom={'custom' in tc}, index={tc.get('index')}", file=sys.stderr)
        
        return messages_normalized

    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> LLMResponse:
        """Execute chat completion with OpenAI-compatible API"""
        start_time = time.time()
        
        try:
            # DeepSeek requires message IDs - add them if missing
            if "deepseek" in self.config.model.lower() or (self.config.base_url and "deepseek" in self.config.base_url.lower()):
                print(f"[DEEPSEEK_DEBUG] Processing {len(messages)} messages for DeepSeek", file=sys.stderr)
                # DeepSeek requires message IDs - add them if missing
                messages_with_ids = []
                for i, msg in enumerate(messages):
                    msg_copy = msg.copy()
                    # Add ID if missing or None or not string
                    if "id" not in msg_copy or msg_copy["id"] is None or not isinstance(msg_copy["id"], str):
                        msg_copy["id"] = str(i)  # DeepSeek expects string IDs
                    # Also ensure tool messages have proper structure
                    if msg_copy.get("role") == "tool" and "tool_call_id" in msg_copy:
                        # Keep tool_call_id, also ensure id field exists
                        pass
                    messages_with_ids.append(msg_copy)
                    print(f"[DEEPSEEK_DEBUG] Message {i}: role={msg_copy.get('role')}, id={msg_copy.get('id')}, has_tool_call_id={'tool_call_id' in msg_copy}", file=sys.stderr)
                messages = messages_with_ids
                # Normalize tool calls for DeepSeek
                messages = self._normalize_deepseek_tool_calls(messages)
                print(f"[DEEPSEEK_TOOL_NORM] Normalized tool calls in {len(messages)} messages", file=sys.stderr)
                print(f"[DEBUG_DEEPSEEK] Added IDs to {len(messages)} messages", file=sys.stderr)
                logger.debug(f"DeepSeek: Added IDs to {len(messages)} messages")
                print(f"[DEBUG_DEEPSEEK] Added IDs to {len(messages)} messages", file=sys.stderr)
                # Debug: print all messages with IDs
                for i, msg in enumerate(messages):
                    print(f"[DEBUG_DEEPSEEK_AFTER] Message {i}: role={msg.get('role')}, id={msg.get('id')}", file=sys.stderr)
            
            # StepFun requires proper tool call structure
            if "stepfun" in self.config.model.lower():
                print(f"[STEPFUN_DEBUG] Processing {len(messages)} messages for StepFun", file=sys.stderr)
                messages = self._normalize_stepfun_tool_calls(messages)
            
            # Prepare completion kwargs
            completion_kwargs = {
                "model": self.config.model,
                "messages": messages,
            }
            # Apply any explicit kwargs first
            completion_kwargs.update(kwargs)
            # Set defaults for missing parameters
            if "temperature" not in completion_kwargs:
                completion_kwargs["temperature"] = self.config.temperature
            if "max_tokens" not in completion_kwargs and self.config.max_tokens is not None:
                completion_kwargs["max_tokens"] = self.config.max_tokens
            if "top_p" not in completion_kwargs and getattr(self.config, "top_p", None) is not None:
                completion_kwargs["top_p"] = self.config.top_p            
            # Add tools if provided
            if tools:
                completion_kwargs["tools"] = self.format_tools(tools)
                completion_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
            
            # Make API call
            logger.debug(f"OpenAI API call: model={completion_kwargs.get('model')}, temperature={completion_kwargs.get('temperature')}, max_tokens={completion_kwargs.get('max_tokens')}, tools_count={len(tools) if tools else 0}, base_url={self.client.base_url if hasattr(self.client, 'base_url') else 'default'}, api_key={self.config.api_key}")
            print(f"[DEBUG_OPENAI] API call: model={completion_kwargs.get('model')}, temperature={completion_kwargs.get('temperature')}, max_tokens={completion_kwargs.get('max_tokens')}, tools_count={len(tools) if tools else 0}, base_url={self.client.base_url if hasattr(self.client, 'base_url') else 'default'}, api_key={self.config.api_key}", file=sys.stderr)
            # Debug: print final messages being sent (DeepSeek only)
            if "deepseek" in self.config.model.lower() or (self.config.base_url and "deepseek" in self.config.base_url.lower()):
                print(f"[DEEPSEEK_DEBUG_FINAL] Sending {len(completion_kwargs.get('messages', []))} messages to API", file=sys.stderr)
                for i, msg in enumerate(completion_kwargs.get('messages', [])):
                    print(f"[DEEPSEEK_DEBUG_FINAL] Message {i}: {msg}", file=sys.stderr)
            # Debug: print final messages being sent (StepFun only)
            if "stepfun" in self.config.model.lower():
                print(f"[STEPFUN_DEBUG_FINAL] Sending {len(completion_kwargs.get('messages', []))} messages to API", file=sys.stderr)
                for i, msg in enumerate(completion_kwargs.get('messages', [])):
                    print(f"[STEPFUN_DEBUG_FINAL] Message {i}: {msg}", file=sys.stderr)
            
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
            # Debug logging for authentication errors
            import os
            if os.environ.get('DEBUG_OPENAI'):
                print(f"[DEBUG_AUTH_ERROR] APIError caught: {e}", file=sys.stderr)
                print(f"[DEBUG_AUTH_ERROR] Error type: {type(e)}", file=sys.stderr)
                print(f"[DEBUG_AUTH_ERROR] Error string: {str(e)}", file=sys.stderr)
                if hasattr(e, 'response'):
                    try:
                        resp_text = str(e.response)
                        if len(resp_text) > 1000:
                            resp_text = resp_text[:1000] + f"... (truncated, total {len(resp_text)} chars)"
                        print(f"[DEBUG_AUTH_ERROR] Error response: {resp_text}", file=sys.stderr)
                    except:
                        pass
            # Special handling for DeepSeek authentication via APIConnectionError
            if isinstance(e, APIConnectionError):
                # Debug logging
                if os.environ.get('DEBUG_OPENAI'):
                    print(f"[DEBUG_APICONNECTION] APIConnectionError caught: {e}", file=sys.stderr)
                    print(f"[DEBUG_APICONNECTION] Base URL: {self.config.base_url}", file=sys.stderr)
                # Check if this is a DeepSeek endpoint
                base_url = str(self.config.base_url or "").lower()
                if "deepseek" in base_url:
                    if os.environ.get('DEBUG_OPENAI'):
                        print(f"[DEBUG_APICONNECTION] Treating as DeepSeek authentication error", file=sys.stderr)
                    auth_error = AuthenticationError(f"Authentication failed (DeepSeek connection error): {e}")
                    if hasattr(e, 'response'):
                        auth_error.raw_response = e.response
                    raise auth_error
                else:
                    if os.environ.get('DEBUG_OPENAI'):
                        print(f"[DEBUG_APICONNECTION] Not DeepSeek, passing through as API error", file=sys.stderr)
            
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
        # Extract content (store locally to avoid mutating message object)
        content = message.content or ""
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
        
        # Extract reasoning content - check multiple attribute names
        reasoning = None
        # Try various attribute names used by different providers
        for attr_name in ('reasoning_content', 'reasoning', 'thinking'):
            if hasattr(message, attr_name) and getattr(message, attr_name):
                reasoning = getattr(message, attr_name)
                break
        
        # Fallback: extract reasoning from <think> tags in content
        if not reasoning:
            import re
            think_match = re.search(r'<think>(.*?)</think>', content, flags=re.DOTALL)
            if think_match:
                reasoning = think_match.group(1).strip()
                # Remove the </think> tags from content to avoid duplication
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        
        return LLMResponse(
            content=content,
            reasoning=reasoning if reasoning else None,
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
