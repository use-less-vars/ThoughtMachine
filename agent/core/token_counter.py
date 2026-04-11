"""
Token estimation and management utilities.

Extracted from agent.py to separate token-related concerns.
"""

import json
import tiktoken
from typing import Optional, List, Dict, Any
from agent.logging.debug_log import debug_log


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
            # Default to cl100k_base (used by gpt-4, gpt-3.5-turbo)
            try:
                self._token_encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                # Fallback to approximate estimation
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
        # DEBUG: Add logging for token counting
        debug_log('token_counter', f"estimate_tokens called with type: {type(text_or_message)}")
        if isinstance(text_or_message, dict):
            content_preview = str(text_or_message.get('content', ''))[:100].replace('\n', ' ') if 'content' in text_or_message else 'no content'
            debug_log('token_counter', f"  role: {text_or_message.get('role', 'unknown')}, content preview: {content_preview}")
        
        encoder = self._get_encoder()
        
        if isinstance(text_or_message, dict):
            # Convert dict to JSON string for tokenization (more accurate for API)
            text = json.dumps(text_or_message)
        else:
            text = str(text_or_message)
        
        if encoder is not None:
            tokens = encoder.encode(text)
            return len(tokens)
        else:
            # Fallback when encoder not available
            return len(text) // 4
    
    def estimate_request_tokens(self, messages, tool_definitions=None) -> int:
        """
        Estimate tokens for an API request including messages and tool definitions.
        
        Args:
            messages: List of message dictionaries.
            tool_definitions: Optional list of tool definition dictionaries.
            
        Returns:
            Estimated total tokens for the request.
        """
        # DEBUG: Add logging for request token counting
        debug_log('token_counter', f"estimate_request_tokens called with {len(messages)} messages")
        if messages:
            first_msg = messages[0]
            last_msg = messages[-1]
            debug_log('token_counter', f"  first msg role: {first_msg.get('role', 'unknown')}, last msg role: {last_msg.get('role', 'unknown')}")
        
        # Note: This method may be called with a provider that has count_tokens method.
        # The actual implementation in Agent class checks for provider.count_tokens.
        # For now, we implement fallback logic.
        
        total_tokens = 0
        for msg in messages:
            total_tokens += self.estimate_tokens(msg)
        
        # Add tool definition tokens (crude estimate)
        if tool_definitions:
            # JSON stringify and estimate
            tools_json = json.dumps(tool_definitions)
            total_tokens += len(tools_json) // 4
        
        # Add some overhead for JSON structure, field names, etc.
        # OpenAI's actual token count includes JSON structure, field names, etc.
        # Add 10% overhead as rough estimate
        total_tokens = int(total_tokens * 1.1)
        
        return total_tokens
    
    def get_model_context_window(self) -> int:
        """
        Get approximate context window size for the current model.
        
        Returns:
            Context window size in tokens.
        """
        model = self.config.model.lower()
        
        # Common model context windows
        context_windows = {
            # OpenAI models
            "gpt-4": 8192,
            "gpt-4-32k": 32768,
            "gpt-4-turbo": 128000,
            "gpt-4o": 128000,
            "gpt-3.5-turbo": 16385,
            "gpt-3.5-turbo-16k": 16385,
            "gpt-3.5-turbo-instruct": 4096,
            # DeepSeek models
            "deepseek-reasoner": 128000,
            "deepseek-chat": 128000,
            "deepseek-coder": 128000,
            # StepFun models
            "step-3.5": 128000,
            # Anthropic models
            "claude-3-opus": 200000,
            "claude-3-sonnet": 200000,
            "claude-3-haiku": 200000,
            # Default fallback
            "default": 128000
        }
        
        # Check for exact match
        for key, window in context_windows.items():
            if key in model:
                return window
        
        # Check for partial matches
        if "gpt-4" in model:
            return 128000  # Most GPT-4 variants are 128k
        elif "gpt-3.5" in model:
            return 16385
        elif "claude" in model:
            return 200000
        elif "deepseek" in model:
            return 128000
        
        # Default to 128k for unknown models
        return 128000
    
    def format_tokens(self, tokens: int) -> str:
        """Format token count in thousands with 'k' suffix.
        
        Args:
            tokens: Token count.
        """
        
        # Add some overhead for JSON structure, field names, etc.
        # OpenAI's actual token count includes JSON structure, field names, etc.
        # Add 10% overhead as rough estimate
        total_tokens = int(total_tokens * 1.1)
        
        return total_tokens