"""
LLM client for handling LLM provider interactions.

Extracted from agent.py to separate LLM communication concerns.
"""

import os
import logging
from typing import Optional, List, Dict, Any

from llm_providers.factory import ProviderFactory
from llm_providers.exceptions import ProviderError, RateLimitExceeded


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
        self.provider = ProviderFactory.create_provider(
            provider_type=config.provider_type,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens
        )
        self.context_builder = None
    
    def create_context_builder(self, token_limit=None):
        """Create a ContextBuilder based on configuration.
        
        Args:
            token_limit: Maximum token limit for context. Defaults to 8000 if not provided.
        """
        from session.history_provider import HistoryProvider
        
        if self.session is None:
            # Should not happen in normal use, but fallback
            if self.logger and hasattr(self.logger, 'py_logger'):
                self.logger.py_logger.warning("Creating HistoryProvider without session")
            elif self.logger:
                self.logger.log_warning("Creating HistoryProvider without session")
            else:
                logging.warning("Creating HistoryProvider without session")
            return None
        
        if token_limit is None:
            token_limit = 8000  # Default fallback
        
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f"[CONTEXT_BUILDER] Creating HistoryProvider with token_limit={token_limit}")
        
        return HistoryProvider(session=self.session, token_limit=token_limit)
    
    def load_system_prompt(self) -> str:
        """Load system prompt from file."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [
            os.path.join(script_dir, "system_prompt.txt"),
            os.path.join(script_dir, "..", "system_prompt.txt"),
            "./system_prompt.txt"
        ]
        system_prompt = None
        for path in possible_paths:
            try:
                with open(path, "r") as f:
                    system_prompt = f.read()
                    break
            except FileNotFoundError:
                continue
        if system_prompt is None:
            raise RuntimeError("Could not find system_prompt.txt in any known location")
        return system_prompt
    
    def ensure_system_prompt(self, conversation: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Ensure system prompt is present in conversation.
        
        Args:
            conversation: Current conversation messages.
            
        Returns:
            Updated conversation with system prompt if needed.
        """
        if not any(msg.get("role") == "system" for msg in conversation):
            if self.config.system_prompt:
                system_prompt = self.config.system_prompt
            else:
                system_prompt = self.load_system_prompt()
            # Insert at beginning
            conversation.insert(0, {"role": "system", "content": system_prompt})
        return conversation
    
    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ):
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
            ProviderError: For other provider errors.
        """
        try:
            response = self.provider.chat_completion(
                messages=messages,
                tools=tools,
                **kwargs
            )
            return response
        except RateLimitExceeded as e:
            # Re-raise for handling by caller
            raise
        except ProviderError as e:
            # Re-raise for handling by caller
            raise
    
    def format_tools(self, tool_definitions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format tool definitions for the provider.
        
        Args:
            tool_definitions: Raw tool definitions.
            
        Returns:
            Formatted tool definitions for the provider.
        """
        return self.provider.format_tools(tool_definitions) if hasattr(self.provider, 'format_tools') else tool_definitions