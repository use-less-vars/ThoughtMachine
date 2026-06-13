"""
LLM client for handling LLM provider interactions.

Extracted from agent.py to separate LLM communication concerns.
"""
import os
from typing import Optional, List, Dict, Any
from agent.logging import log
from llm_providers.factory import ProviderFactory
from llm_providers.exceptions import ProviderError, RateLimitExceeded, AuthenticationError, ModelNotFoundError, TokenLimitExceededError, ProviderTimeoutError, InvalidConfigError, ProviderNotFoundError, ToolFormatError

class LLMError(ProviderError):
    """Generic LLM error for provider-independent error handling."""

    def __init__(self, error_type: str, message: str, original_exception: Exception=None):
        self.error_type = error_type
        self.message = message
        self.original_exception = original_exception
        super().__init__(f'{error_type}: {message}')

class LLMClient:
    """Handles LLM provider communication, context building, and system prompts."""

    def __init__(self, config, session=None, logger=None):
        """
        Initialize LLM client.
        
        Args:
            config: AgentConfig instance.
            session: Optional Session object.
            logger: Optional logger instance.
        """
        self.config = config
        self.session = session
        self.logger = logger
        self.provider = ProviderFactory.create_provider(provider_type=config.provider_type, api_key=config.api_key, base_url=config.base_url, model=config.model, temperature=config.temperature)
        self.context_builder = None

    def create_context_builder(self, token_limit=None):
        """Create a ContextBuilder based on configuration.
        
        Args:
            token_limit: Maximum token limit for context. Defaults to 8000 if not provided.
        """
        from session.history_provider import HistoryProvider
        if self.session is None:
            if self.logger and hasattr(self.logger, 'py_logger'):
                log('WARNING', 'core.llm_client', 'Creating HistoryProvider without session')
            elif self.logger:
                self.logger.log_warning('Creating HistoryProvider without session')
            else:
                log('WARNING', 'core.llm_client', 'Creating HistoryProvider without session')
            log('DEBUG', 'core.context_builder', f'LLMClient.create_context_builder: session is None, returning None')
            return None
        if self.logger and hasattr(self.logger, 'py_logger'):
            log('INFO', 'core.llm_client', f'[CONTEXT_BUILDER] Creating HistoryProvider with token_limit={token_limit}')
        return HistoryProvider(session=self.session, token_limit=token_limit)

    def load_system_prompt(self) -> str:
        """Load system prompt from file."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [os.path.join(script_dir, 'system_prompt.txt'), os.path.join(script_dir, '..', 'system_prompt.txt'), './system_prompt.txt']
        system_prompt = None
        for path in possible_paths:
            try:
                with open(path, 'r') as f:
                    system_prompt = f.read()
                    break
            except FileNotFoundError:
                continue
        if system_prompt is None:
            raise RuntimeError('Could not find system_prompt.txt in any known location')
        return system_prompt

    def ensure_system_prompt(self, conversation: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Ensure system prompt is present in conversation, replacing any existing one.

        On agent restart after a config change, the conversation may still
        contain the *old* system prompt.  This method strips any existing
        system messages and inserts the fresh prompt from the current config,
        so that system-prompt changes take effect immediately.

        Args:
            conversation: Current conversation messages.

        Returns:
            Updated conversation with the current system prompt at position 0.
        """
        # Determine the current system prompt (from config or file fallback)
        if self.config.system_prompt:
            system_prompt = self.config.system_prompt
        else:
            system_prompt = self.load_system_prompt()

        # Remove old system prompts but preserve summary messages
        # (Summaries also use role='system' and are needed by SummaryBuilder.
        #  Without this guard, the next SummaryBuilder.build() call will find
        #  no summary and fall back to including the ENTIRE history, causing
        #  a ~4.7x token explosion on restart.)
        conversation[:] = [
            msg for msg in conversation
            if not (msg.get('role') == 'system'
                    and not msg.get('content', '').startswith('Summary of previous conversation:'))
        ]

        # Insert the fresh system prompt at the front
        conversation.insert(0, {'role': 'system', 'content': system_prompt})

        return conversation

    def chat_completion(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]=None, **kwargs):
        """
        Make LLM chat completion request.
        
        Args:
            messages: List of message dictionaries.
            tools: Optional tool definitions.
            **kwargs: Additional chat completion parameters.
            
        Returns:
            LLM response object.
            
        Raises:
            RateLimitExceeded: If rate limit is hit.
            LLMError: For provider-independent errors (authentication, timeout, etc.).
        """
        try:
            response = self.provider.chat_completion(messages=messages, tools=tools, **kwargs)
            return response
        except RateLimitExceeded as e:
            raise
        except ProviderError as e:
            error_mapping = {AuthenticationError: 'authentication_error', ModelNotFoundError: 'model_not_found', TokenLimitExceededError: 'token_limit_exceeded', ProviderTimeoutError: 'timeout', InvalidConfigError: 'invalid_config', ProviderNotFoundError: 'provider_not_found', ToolFormatError: 'tool_format_error'}
            error_type = 'provider_error'
            for provider_exception, generic_type in error_mapping.items():
                if isinstance(e, provider_exception):
                    error_type = generic_type
                    break
            raise LLMError(error_type=error_type, message=str(e), original_exception=e)

    def close(self):
        """Close and release provider resources."""
        provider = getattr(self, 'provider', None)
        if provider is not None:
            if hasattr(provider, 'close'):
                try:
                    provider.close()
                except Exception as e:
                    log('DEBUG', 'core.llm_client', f'Error closing provider: {e}')
            elif hasattr(provider, 'aclose'):
                try:
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(provider.aclose())
                        else:
                            loop.run_until_complete(provider.aclose())
                    except RuntimeError:
                        pass
                    except Exception as e:
                        log('DEBUG', 'core.llm_client', f'Error closing async provider: {e}')
                except Exception as e:
                    log('DEBUG', 'core.llm_client', f'Error closing provider: {e}')
        self.provider = None

    def format_tools(self, tool_definitions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format tool definitions for the provider.
        
        Args:
            tool_definitions: Raw tool definitions.
            
        Returns:
            Formatted tool definitions for the provider.
        """
        return self.provider.format_tools(tool_definitions) if hasattr(self.provider, 'format_tools') else tool_definitions