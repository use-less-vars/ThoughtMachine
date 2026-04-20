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
from agent.logging import log
logger = logging.getLogger(__name__)
if os.environ.get('DEBUG_OPENAI'):
    logger.setLevel(logging.DEBUG)

class OpenAICompatibleProvider(LLMProvider):
    """
    Provider for OpenAI-compatible APIs.
    Supports: OpenAI, DeepSeek, OpenCode/Big Pickle, Local LLMs with OpenAI interface.
    """
    PRICING = {'gpt-4': {'input': 30.0, 'output': 60.0}, 'gpt-4-turbo': {'input': 10.0, 'output': 30.0}, 'gpt-3.5-turbo': {'input': 0.5, 'output': 1.5}, 'deepseek-reasoner': {'input': 0.14, 'output': 0.28}, 'opencode/big-pickle': {'input': 0.0, 'output': 0.0}}

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        client_kwargs = {'api_key': config.api_key, 'timeout': config.timeout, 'max_retries': config.max_retries}
        logger.debug(f'OpenAI client config: base_url={config.base_url}, model={config.model}, api_key={config.api_key}, timeout={config.timeout}, max_retries={config.max_retries}, extra_headers={config.extra_headers}')
        log('DEBUG', 'llm.openai', f'client config: base_url={config.base_url}, model={config.model}, api_key={config.api_key}, timeout={config.timeout}, max_retries={config.max_retries}, extra_headers={config.extra_headers}')
        if config.base_url:
            client_kwargs['base_url'] = config.base_url
        if config.extra_headers:
            client_kwargs['default_headers'] = config.extra_headers
        logger.debug(f"OpenAI client final kwargs: base_url={client_kwargs.get('base_url')}, default_headers={client_kwargs.get('default_headers')}")
        log('DEBUG', 'llm.openai', f"client final kwargs: base_url={client_kwargs.get('base_url')}, default_headers={client_kwargs.get('default_headers')}")
        self.client = OpenAI(**client_kwargs)
        logger.debug(f"OpenAI client created with base_url={(self.client.base_url if hasattr(self.client, 'base_url') else 'default')}")
        log('DEBUG', 'llm.openai', f"client created with base_url={(self.client.base_url if hasattr(self.client, 'base_url') else 'default')}")
        self.encoding = None

    def _load_encoding(self):
        """Lazily load tiktoken encoding for token counting"""
        if self.encoding is not None:
            return
        try:
            self.encoding = tiktoken.encoding_for_model(self.config.model)
            logger.debug(f'Loaded tokenizer for model: {self.config.model}')
        except KeyError:
            try:
                model_lower = self.config.model.lower()
                if 'gpt-4' in model_lower or 'gpt-3.5' in model_lower:
                    self.encoding = tiktoken.get_encoding('cl100k_base')
                elif 'deepseek' in model_lower:
                    self.encoding = tiktoken.get_encoding('cl100k_base')
                elif 'opencode' in model_lower or 'big-pickle' in model_lower:
                    self.encoding = tiktoken.get_encoding('cl100k_base')
                else:
                    self.encoding = tiktoken.get_encoding('cl100k_base')
            except Exception as e:
                logger.warning(f'Failed to load tokenizer: {e}. Token counting will be approximate.')
                self.encoding = None
        except Exception as e:
            logger.warning(f'Failed to load tokenizer: {e}. Token counting will be approximate.')
            self.encoding = None

    def _normalize_deepseek_tool_calls(self, messages):
        """Normalize messages for DeepSeek API, ensuring proper IDs and tool call format.

         Key improvements:
         1. Preserve existing tool call IDs (don't overwrite)
         2. Only generate new IDs when ID is missing or None
         3. Convert IDs to strings
         4. Log mismatches between tool messages and assistant tool calls
        """
        import sys, os
        debug_enabled = os.environ.get('THOUGHTMACHINE_DEBUG') == '1'
        messages_with_ids = []
        tool_call_ids = []
        for msg in messages:
            if msg.get('role') == 'tool' and 'tool_call_id' in msg:
                tool_call_ids.append(msg['tool_call_id'])
        for i, msg in enumerate(messages):
            msg_copy = msg.copy()
            if 'id' not in msg_copy or msg_copy['id'] is None or (not isinstance(msg_copy['id'], str)):
                msg_copy['id'] = str(i)
            if msg_copy.get('role') == 'assistant' and 'tool_calls' in msg_copy:
                tool_calls = msg_copy['tool_calls']
                if tool_calls and isinstance(tool_calls, list):
                    normalized_tool_calls = []
                    for j, tc in enumerate(tool_calls):
                        if not isinstance(tc, dict):
                            tc = dict(tc) if hasattr(tc, '__dict__') else {'function': {'name': '', 'arguments': '{}'}}
                        tc_copy = tc.copy() if isinstance(tc, dict) else {}
                        if 'id' not in tc_copy or tc_copy['id'] is None:
                            if j < len(tool_call_ids):
                                tc_copy['id'] = tool_call_ids[j]
                            else:
                                tc_copy['id'] = f'call_{i}_{j}'
                        if isinstance(tc_copy['id'], (int, float)):
                            tc_copy['id'] = str(tc_copy['id'])
                        if 'type' not in tc_copy:
                            tc_copy['type'] = 'function'
                        if 'function' not in tc_copy:
                            tc_copy['function'] = {'name': tc_copy.get('name', ''), 'arguments': tc_copy.get('arguments', '{}')}
                            tc_copy.pop('name', None)
                            tc_copy.pop('arguments', None)
                            tc_copy.pop('result', None)
                        normalized_tool_calls.append(tc_copy)
                    msg_copy['tool_calls'] = normalized_tool_calls
            if msg_copy.get('role') == 'tool':
                if 'tool_call_id' not in msg_copy:
                    msg_copy['tool_call_id'] = msg_copy.get('id', f'tool_{i}')
            messages_with_ids.append(msg_copy)
        if debug_enabled:
            log('DEBUG', 'debug.unknown', f'Processed {len(messages)} messages')
            for i, msg in enumerate(messages_with_ids):
                if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                    for tc in msg['tool_calls']:
                        log('DEBUG', 'debug.unknown', f"Assistant tool call id={tc.get('id')}")
                if msg.get('role') == 'tool':
                    log('DEBUG', 'debug.unknown', f"Tool message tool_call_id={msg.get('tool_call_id')}")
        return messages_with_ids

    def _normalize_stepfun_tool_calls(self, messages, is_openrouter=False):
        """Normalize messages for StepFun API via OpenRouter.
        
        StepFun expects tool calls to have either 'function' field (when type='function') 
        or 'custom' field (when type='custom'). OpenRouter may add 'index' field.
        This ensures tool calls have the required structure.
        
        When is_openrouter=True, convert to standard OpenAI format (type='function' with 'function' field)
        since OpenRouter expects that format.
        """
        import sys, os
        debug_enabled = os.environ.get('THOUGHTMACHINE_DEBUG') == '1'
        if debug_enabled:
            log('DEBUG', 'llm.stepfun', f'Normalizing with is_openrouter={is_openrouter}')
        messages_normalized = []
        if debug_enabled:
            log('DEBUG', 'llm.stepfun', f'Starting normalization of {len(messages)} messages')
            for idx, msg in enumerate(messages):
                if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                    log('DEBUG', 'llm.stepfun', f"Message {idx} has tool_calls: {msg['tool_calls']}")
        for i, msg in enumerate(messages):
            msg_copy = msg.copy()
            if msg_copy.get('role') == 'assistant' and 'tool_calls' in msg_copy:
                tool_calls = msg_copy['tool_calls']
                if tool_calls and isinstance(tool_calls, list):
                    normalized_tool_calls = []
                    for j, tc in enumerate(tool_calls):
                        if not isinstance(tc, dict):
                            tc = dict(tc) if hasattr(tc, '__dict__') else {}
                        tc_copy = tc.copy() if isinstance(tc, dict) else {}
                        if debug_enabled:
                            log('DEBUG', 'llm.stepfun', f'Tool call {j} before: {tc_copy}')
                        if 'type' not in tc_copy:
                            tc_copy['type'] = 'function'
                        current_type = tc_copy.get('type', 'function')
                        if is_openrouter:
                            if current_type == 'custom' and 'custom' in tc_copy:
                                custom_data = tc_copy.pop('custom')
                                tc_copy['function'] = custom_data
                                tc_copy['type'] = 'function'
                            elif current_type == 'function' and 'custom' in tc_copy:
                                tc_copy['function'] = tc_copy.pop('custom')
                                tc_copy['type'] = 'function'
                            elif current_type == 'custom' and 'function' not in tc_copy:
                                arguments = tc_copy.get('arguments', '{}')
                                if isinstance(arguments, str):
                                    try:
                                        import json
                                        arguments = json.loads(arguments)
                                    except:
                                        arguments = {}
                                tc_copy['function'] = {'name': tc_copy.get('name', ''), 'arguments': arguments}
                                tc_copy['type'] = 'function'
                                tc_copy.pop('name', None)
                                tc_copy.pop('arguments', None)
                                tc_copy.pop('result', None)
                            if tc_copy.get('type') != 'function':
                                tc_copy['type'] = 'function'
                        else:
                            if current_type == 'function':
                                if 'function' in tc_copy:
                                    tc_copy['custom'] = tc_copy.pop('function')
                                else:
                                    arguments = tc_copy.get('arguments', '{}')
                                    if isinstance(arguments, str):
                                        try:
                                            import json
                                            arguments = json.loads(arguments)
                                        except:
                                            arguments = {}
                                    tc_copy['custom'] = {'name': tc_copy.get('name', ''), 'arguments': arguments}
                                    tc_copy.pop('name', None)
                                    tc_copy.pop('arguments', None)
                                    tc_copy.pop('result', None)
                                tc_copy['type'] = 'custom'
                            elif not is_openrouter and tc_copy.get('type') == 'custom':
                                if 'custom' not in tc_copy:
                                    if 'function' in tc_copy:
                                        tc_copy['custom'] = tc_copy.pop('function')
                                    else:
                                        arguments = tc_copy.get('arguments', '{}')
                                        if isinstance(arguments, str):
                                            try:
                                                import json
                                                arguments = json.loads(arguments)
                                            except:
                                                arguments = {}
                                        tc_copy['custom'] = {'name': tc_copy.get('name', ''), 'arguments': arguments}
                                        tc_copy.pop('name', None)
                                        tc_copy.pop('arguments', None)
                                        tc_copy.pop('result', None)
                            custom_field = tc_copy.get('custom')
                            if not isinstance(custom_field, dict):
                                tc_copy['custom'] = {}
                                custom_field = {}
                            if not custom_field:
                                if 'name' in tc_copy or 'arguments' in tc_copy:
                                    arguments = tc_copy.get('arguments', '{}')
                                    if isinstance(arguments, str):
                                        try:
                                            import json
                                            arguments = json.loads(arguments)
                                        except:
                                            arguments = {}
                                    tc_copy['custom'] = {'name': tc_copy.get('name', ''), 'arguments': arguments}
                                    tc_copy.pop('name', None)
                                    tc_copy.pop('arguments', None)
                                    tc_copy.pop('result', None)
                                elif 'function' in tc_copy:
                                    tc_copy['custom'] = tc_copy.pop('function')
                                else:
                                    tc_copy['custom'] = {'name': '', 'arguments': {}}
                        if debug_enabled:
                            log('DEBUG', 'llm.stepfun', f'Tool call {j} after: {tc_copy}')
                        normalized_tool_calls.append(tc_copy)
                    msg_copy['tool_calls'] = normalized_tool_calls
            messages_normalized.append(msg_copy)
        if debug_enabled:
            log('DEBUG', 'llm.stepfun', f'Processed {len(messages)} messages')
            for i, msg in enumerate(messages_normalized):
                if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                    for tc in msg['tool_calls']:
                        log('DEBUG', 'llm.stepfun', f"Assistant tool call id={tc.get('id')}, type={tc.get('type')}, has_function={'function' in tc}, has_custom={'custom' in tc}, index={tc.get('index')}")
        if debug_enabled:
            log('DEBUG', 'llm.stepfun', f'Final normalized messages:')
            for idx, msg in enumerate(messages_normalized):
                log('DEBUG', 'llm.stepfun', f"Message {idx}: role={msg.get('role')}")
                if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                    log('DEBUG', 'llm.stepfun', f"  tool_calls: {msg['tool_calls']}")
        return messages_normalized

    def chat_completion(self, messages: List[Dict[str, str]], tools: Optional[List[Dict]]=None, **kwargs) -> LLMResponse:
        """Execute chat completion with OpenAI-compatible API"""
        start_time = time.time()
        try:
            if 'deepseek' in self.config.model.lower() or (self.config.base_url and 'deepseek' in self.config.base_url.lower()):
                messages_with_ids = []
                for i, msg in enumerate(messages):
                    msg_copy = msg.copy()
                    if 'id' not in msg_copy or msg_copy['id'] is None or (not isinstance(msg_copy['id'], str)):
                        msg_copy['id'] = str(i)
                    if msg_copy.get('role') == 'tool' and 'tool_call_id' in msg_copy:
                        pass
                    messages_with_ids.append(msg_copy)
                messages = messages_with_ids
                messages = self._normalize_deepseek_tool_calls(messages)
                logger.debug(f'DeepSeek: Added IDs to {len(messages)} messages')
                for i, msg in enumerate(messages):
                    pass
            is_stepfun = 'stepfun' in self.config.model.lower()
            is_openrouter = self.config.base_url and 'openrouter' in self.config.base_url.lower()
            if is_stepfun or (is_openrouter and is_stepfun):
                messages = self._normalize_stepfun_tool_calls(messages, is_openrouter=is_openrouter)
            completion_kwargs = {'model': self.config.model, 'messages': messages}
            completion_kwargs.update(kwargs)
            if 'temperature' not in completion_kwargs:
                completion_kwargs['temperature'] = self.config.temperature
            if 'max_tokens' not in completion_kwargs and self.config.max_tokens is not None:
                completion_kwargs['max_tokens'] = self.config.max_tokens
            if 'top_p' not in completion_kwargs and getattr(self.config, 'top_p', None) is not None:
                completion_kwargs['top_p'] = self.config.top_p
            if tools:
                completion_kwargs['tools'] = self.format_tools(tools)
                completion_kwargs['tool_choice'] = kwargs.get('tool_choice', 'auto')
            logger.debug(f"OpenAI API call: model={completion_kwargs.get('model')}, temperature={completion_kwargs.get('temperature')}, max_tokens={completion_kwargs.get('max_tokens')}, tools_count={(len(tools) if tools else 0)}, base_url={(self.client.base_url if hasattr(self.client, 'base_url') else 'default')}, api_key={self.config.api_key}")
            if 'deepseek' in self.config.model.lower() or (self.config.base_url and 'deepseek' in self.config.base_url.lower()):
                for i, msg in enumerate(completion_kwargs.get('messages', [])):
                    pass
            if 'stepfun' in self.config.model.lower():
                for i, msg in enumerate(completion_kwargs.get('messages', [])):
                    pass
            else:
                for i, msg in enumerate(completion_kwargs.get('messages', [])):
                    if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                        pass
            response = self.client.chat.completions.create(**completion_kwargs)
            import os
            if os.environ.get('DEBUG_OPENAI'):
                raw_str = str(response)
                if len(raw_str) > 1000:
                    raw_str = raw_str[:1000] + f'... (truncated, total {len(raw_str)} chars)'
            import os
            if os.environ.get('DEBUG_OPENAI'):
                log('DEBUG', 'llm.openai', f'Response type: {type(response)}')
                if hasattr(response, '__dict__'):
                    log('DEBUG', 'llm.openai', f'Response has __dict__, keys: {list(response.__dict__.keys())}')
                    try:
                        import json
                        resp_json = json.dumps(response.__dict__, default=str, indent=2)
                        if len(resp_json) > 2000:
                            resp_json = resp_json[:2000] + '... (truncated)'
                        log('DEBUG', 'llm.openai', f'Response JSON: {resp_json}')
                    except:
                        pass
                elif isinstance(response, dict):
                    log('DEBUG', 'llm.openai', f'Response is dict, keys: {list(response.keys())}')
                elif isinstance(response, str):
                    log('WARNING', 'llm.openai', f'WARNING: Response is string, not JSON object: {response[:500]}')
                else:
                    log('DEBUG', 'llm.openai', f'Response repr: {repr(response)[:500]}')
            try:
                llm_response = self.parse_response(response, start_time)
            except Exception as parse_error:
                if os.environ.get('DEBUG_OPENAI'):
                    log('ERROR', 'llm.openai', f'Failed to parse response: {parse_error}')
                    log('ERROR', 'llm.openai', f'Response that caused error: {response}')
                parse_provider_error = ProviderError(f'Failed to parse API response: {parse_error}')
                parse_provider_error.raw_response = response
                raise parse_provider_error
            self.track_usage(llm_response)
            return llm_response
        except RateLimitError as e:
            rate_limit_error = RateLimitExceeded(f'Rate limit exceeded: {e}')
            if hasattr(e, 'response'):
                rate_limit_error.raw_response = e.response
            raise rate_limit_error
        except APIError as e:
            import os
            if os.environ.get('DEBUG_OPENAI'):
                log('DEBUG', 'llm.openai', f'APIError caught: {e}')
                log('DEBUG', 'llm.openai', f'Error type: {type(e)}')
                log('DEBUG', 'llm.openai', f'Error string: {str(e)}')
                if hasattr(e, 'response'):
                    try:
                        resp_text = str(e.response)
                        if len(resp_text) > 1000:
                            resp_text = resp_text[:1000] + f'... (truncated, total {len(resp_text)} chars)'
                        log('DEBUG', 'llm.openai', f'Error response: {resp_text}')
                    except:
                        pass
            if isinstance(e, APIConnectionError):
                if os.environ.get('DEBUG_OPENAI'):
                    log('DEBUG', 'llm.openai', f'APIConnectionError caught: {e}')
                    log('DEBUG', 'llm.openai', f'Base URL: {self.config.base_url}')
                base_url = str(self.config.base_url or '').lower()
                if 'deepseek' in base_url:
                    if os.environ.get('DEBUG_OPENAI'):
                        log('DEBUG', 'llm.openai', f'Treating as DeepSeek authentication error')
                    auth_error = AuthenticationError(f'Authentication failed (DeepSeek connection error): {e}')
                    if hasattr(e, 'response'):
                        auth_error.raw_response = e.response
                    raise auth_error
                elif os.environ.get('DEBUG_OPENAI'):
                    log('DEBUG', 'llm.openai', f'Not DeepSeek, passing through as API error')
            if 'authentication' in str(e).lower() or 'api key' in str(e).lower():
                auth_error = AuthenticationError(f'Authentication failed: {e}')
                if hasattr(e, 'response'):
                    auth_error.raw_response = e.response
                raise auth_error
            api_error = ProviderError(f'API error: {e}')
            if hasattr(e, 'response'):
                api_error.raw_response = e.response
            raise api_error
        except Exception as e:
            import os
            if os.environ.get('DEBUG_OPENAI'):
                log('DEBUG', 'llm.openai', f'Exception type: {type(e)}')
                log('DEBUG', 'llm.openai', f'Exception message: {e}')
                if hasattr(e, 'response'):
                    try:
                        resp_text = str(e.response)
                        if len(resp_text) > 1000:
                            resp_text = resp_text[:1000] + f'... (truncated, total {len(resp_text)} chars)'
                        log('DEBUG', 'llm.openai', f'Exception response: {resp_text}')
                    except:
                        pass
            err_msg = f'Unexpected error: {e}'
            provider_error = ProviderError(err_msg)
            if hasattr(e, 'response'):
                provider_error.raw_response = e.response
                provider_error.args = (f'{err_msg}. Response: {e.response}',)
            if 'response' in locals():
                provider_error.raw_response = response
            raise provider_error

    def parse_response(self, raw_response: Any, start_time: float) -> LLMResponse:
        """Parse OpenAI-compatible response"""
        import os
        import sys
        latency = (time.time() - start_time) * 1000
        if os.environ.get('DEBUG_OPENAI'):
            log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] Starting parse, raw_response type: {type(raw_response)}')
            if hasattr(raw_response, '__dict__'):
                log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] raw_response has __dict__')
                for key, value in raw_response.__dict__.items():
                    if key == '_response' or key == 'response':
                        continue
                    log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE]   {key}: {value}')
            elif isinstance(raw_response, dict):
                log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] raw_response is dict, keys: {list(raw_response.keys())}')
            elif isinstance(raw_response, str):
                log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] raw_response is string (len={len(raw_response)}): {raw_response[:200]}')
            else:
                log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] raw_response repr: {repr(raw_response)[:200]}')
        if isinstance(raw_response, str):
            if os.environ.get('DEBUG_OPENAI'):
                pass
            raise ValueError(f'API returned string instead of JSON response: {raw_response[:200]}')
        if not hasattr(raw_response, 'choices'):
            if os.environ.get('DEBUG_OPENAI'):
                log('DEBUG', 'llm.openai', f"[DEBUG_PARSE_RESPONSE] ERROR: raw_response missing 'choices' attribute")
                log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] raw_response attributes: {dir(raw_response)}')
                if hasattr(raw_response, '__dict__'):
                    log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] raw_response __dict__ keys: {list(raw_response.__dict__.keys())}')
            raise AttributeError(f"Response missing 'choices' attribute. Response type: {type(raw_response)}")
        if not raw_response.choices:
            if os.environ.get('DEBUG_OPENAI'):
                log('DEBUG', 'llm.openai', f'[DEBUG_PARSE_RESPONSE] ERROR: raw_response.choices is empty')
            raise ValueError('Response has empty choices list')
        message = raw_response.choices[0].message
        content = message.content or ''
        tool_calls = None
        if hasattr(message, 'tool_calls') and message.tool_calls:
            tool_calls = []
            for idx, tc in enumerate(message.tool_calls):
                if hasattr(tc, '__dict__'):
                    pass
                elif isinstance(tc, dict):
                    pass
                if hasattr(tc, 'function'):
                    name = tc.function.name
                    arguments = tc.function.arguments
                    tc_id = tc.id
                elif 'custom' in tc:
                    custom_data = tc.get('custom', {})
                    name = custom_data.get('name')
                    arguments = custom_data.get('arguments')
                    tc_id = tc.get('id')
                else:
                    func = tc.get('function', {})
                    name = func.get('name')
                    arguments = func.get('arguments')
                    tc_id = tc.get('id')
                tc_type = 'function'
                if hasattr(tc, 'type'):
                    tc_type = tc.type
                elif isinstance(tc, dict) and 'type' in tc:
                    tc_type = tc.get('type')
                is_stepfun = 'stepfun' in self.config.model.lower()
                is_openrouter = self.config.base_url and 'openrouter' in self.config.base_url.lower()
                if is_openrouter and is_stepfun and (tc_type == 'custom'):
                    tool_calls.append({'id': tc_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}})
                else:
                    if tc_type not in ['function', 'custom']:
                        tc_type = 'function'
                    tool_calls.append({'id': tc_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}})
        usage = {}
        if hasattr(raw_response, 'usage'):
            usage = {'prompt_tokens': raw_response.usage.prompt_tokens, 'completion_tokens': raw_response.usage.completion_tokens, 'total_tokens': raw_response.usage.total_tokens}
        reasoning = None
        for attr_name in ('reasoning_content', 'reasoning', 'thinking'):
            if hasattr(message, attr_name) and getattr(message, attr_name):
                reasoning = getattr(message, attr_name)
                break
        if not reasoning:
            import re
            think_match = re.search('<think>(.*?)</think>', content, flags=re.DOTALL)
            if think_match:
                reasoning = think_match.group(1).strip()
                content = re.sub('<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return LLMResponse(content=content, reasoning=reasoning if reasoning else None, tool_calls=tool_calls, usage=usage, raw_response=raw_response, provider=self.provider_name, model=self.config.model, latency_ms=latency)

    def count_tokens(self, messages: List[Dict], tools: Optional[List]=None) -> int:
        """Count tokens using tiktoken"""
        if self.encoding is None:
            self._load_encoding()
        if self.encoding is None:
            text = ''
            for msg in messages:
                text += f"{msg.get('role', '')}: {msg.get('content', '')}\n"
            if tools:
                text += str(tools)
            return len(text) // 4
        try:
            text = ''
            for msg in messages:
                text += f"{msg.get('role', '')}: {msg.get('content', '')}\n"
            if tools:
                text += str(tools)
            return len(self.encoding.encode(text))
        except Exception as e:
            logger.warning(f'Token counting failed: {e}')
            return 0

    def _calculate_cost(self, response: LLMResponse) -> float:
        """Calculate cost based on model pricing"""
        model = self.config.model
        pricing = self.PRICING.get(model, self.PRICING.get('gpt-3.5-turbo'))
        prompt_tokens = response.usage.get('prompt_tokens', 0)
        completion_tokens = response.usage.get('completion_tokens', 0)
        cost = prompt_tokens * pricing['input'] / 1000000 + completion_tokens * pricing['output'] / 1000000
        return cost