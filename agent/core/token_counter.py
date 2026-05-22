"""
Token estimation and management utilities.

Extracted from agent.py to separate token-related concerns.
"""
import json
import tiktoken
from typing import Optional, List, Dict, Any
from agent.logging import log

class TokenCounter:
    """Handles token estimation, context window management, and token formatting."""

    def __init__(self, config):
        """
        Initialize token counter.
        
        Args:
            config: AgentConfig instance for model and token settings.
        """
        self.config = config
        self._token_encoder = None

    def _get_encoder(self):
        """Get or initialize token encoder."""
        if self._token_encoder is None:
            try:
                self._token_encoder = tiktoken.get_encoding('cl100k_base')
            except Exception:
                self._token_encoder = None
        return self._token_encoder

    def estimate_tokens(self, text_or_message) -> int:
        """
        Estimate token count for a string or message dict using tiktoken.
        
        Args:
            text_or_message: Either a string or a message dictionary.
            
        Returns:
            Estimated token count.
        """
        # Per-message logging removed to reduce noise (was 2+ lines per message)
        encoder = self._get_encoder()
        if isinstance(text_or_message, dict):
            text = json.dumps(text_or_message)
        else:
            text = str(text_or_message)
        if encoder is not None:
            tokens = encoder.encode(text)
            return len(tokens)
        else:
            return len(text) // 4

    def estimate_request_tokens(self, messages, tool_definitions=None) -> int:
        """
        Estimate tokens for an API request including messages and tool definitions.

        Uses consistent tiktoken encoding for both messages and tools.
        No arbitrary overhead factor is added, making this match the actual
        token count that the API would return.

        Args:
            messages: List of message dictionaries.
            tool_definitions: Optional list of tool definition dictionaries.

        Returns:
            Estimated total tokens for the request.
        """
        # estimate_request_tokens entry log removed to reduce noise
        total_tokens = 0
        for msg in messages:
            total_tokens += self.estimate_tokens(msg)
        if tool_definitions:
            tools_json = json.dumps(tool_definitions)
            total_tokens += self.estimate_tokens(tools_json)
        return total_tokens
    def get_model_context_window(self) -> int:
        """
        Get approximate context window size for the current model.
        
        Returns:
            Context window size in tokens.
        """
        model = self.config.model.lower()
        context_windows = {'gpt-4': 8192, 'gpt-4-32k': 32768, 'gpt-4-turbo': 128000, 'gpt-4o': 128000, 'gpt-3.5-turbo': 16385, 'gpt-3.5-turbo-16k': 16385, 'gpt-3.5-turbo-instruct': 4096, 'deepseek-reasoner': 128000, 'deepseek-chat': 128000, 'deepseek-coder': 128000, 'step-3.5': 128000, 'claude-3-opus': 200000, 'claude-3-sonnet': 200000, 'claude-3-haiku': 200000, 'default': 128000}
        for key, window in context_windows.items():
            if key in model:
                return window
        if 'gpt-4' in model:
            return 128000
        elif 'gpt-3.5' in model:
            return 16385
        elif 'claude' in model:
            return 200000
        elif 'deepseek' in model:
            return 128000
        return 128000

    def format_tokens(self, tokens: int) -> str:
        """Format token count in thousands with 'k' suffix.

        Args:
            tokens: Token count.

        Returns:
            Formatted string like '51k' or '128k'.
        """
        if tokens >= 1000:
            return f'{tokens // 1000}k'
        return str(tokens)