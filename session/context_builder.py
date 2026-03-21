"""
Context Builder: Decides which parts of user_history should be sent to the LLM.

Provides strategies for building the agent_context from the full user_history,
allowing the agent to operate within token limits while preserving the full
conversation for the user's view.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import tiktoken


class ContextBuilder(ABC):
    """Abstract base class for context building strategies."""

    @abstractmethod
    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Build the agent_context from the full user_history.

        Args:
            user_history: Complete list of message dicts (role, content)
            max_tokens: Optional maximum token count for the context

        Returns:
            A list of message dicts to be sent to the LLM.
        """
        pass

    def _estimate_tokens(self, message: Dict[str, Any], encoder: Optional[tiktoken.Encoding] = None) -> int:
        """Estimate token count for a message."""
        if encoder is None:
            try:
                encoder = tiktoken.get_encoding("cl100k_base")  # OpenAI default
            except Exception:
                # Fallback: rough estimate
                return len(str(message)) // 4

        content = message.get("content", "")
        if isinstance(content, str):
            return len(encoder.encode(content))
        else:
            # For multimodal content, sum tokens
            total = 0
            for part in content:
                if isinstance(part, str):
                    total += len(encoder.encode(part))
                elif isinstance(part, dict):
                    # Estimate for images etc. - crude fallback
                    total += 100
            return total


class LastNBuilder(ContextBuilder):
    """Simple strategy: keep the last N messages (or last N turns)."""

    def __init__(self, keep_last_messages: int = 10, keep_system_prompt: bool = True):
        """
        Initialize.

        Args:
            keep_last_messages: Number of recent messages to retain
            keep_system_prompt: If True, always include the system message(s) at the start
        """
        self.keep_last_messages = keep_last_messages
        self.keep_system_prompt = keep_system_prompt

    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Build context by taking the last N messages, optionally preserving system messages.
        If max_tokens is provided and the context exceeds it, further truncation may occur.
        """
        if not user_history:
            return []

        # Separate system messages and other messages
        system_messages = [msg for msg in user_history if msg.get("role") == "system"]
        other_messages = [msg for msg in user_history if msg.get("role") != "system"]

        # Keep the most recent messages from other_messages
        recent_others = other_messages[-self.keep_last_messages:] if other_messages else []

        # Combine system messages (if keep_system_prompt) with recent others
        if self.keep_system_prompt:
            context = system_messages + recent_others
        else:
            context = recent_others

        # If max_tokens is provided, attempt to honor it by further truncating
        if max_tokens is not None:
            context = self._truncate_to_max_tokens(context, max_tokens)

        return context

    def _truncate_to_max_tokens(self, messages: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
        """Truncate messages from the beginning (oldest) until under max_tokens."""
        try:
            encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            encoder = None

        total = sum(self._estimate_tokens(msg, encoder) for msg in messages)
        while total > max_tokens and len(messages) > 0:
            # Remove the oldest non-system message (system messages we keep at the front)
            # We'll be careful: if the first message is system, skip it.
            idx_to_remove = 0
            while idx_to_remove < len(messages) and messages[idx_to_remove].get("role") == "system":
                idx_to_remove += 1
            if idx_to_remove >= len(messages):
                break  # Can't truncate further
            removed_msg = messages.pop(idx_to_remove)
            total -= self._estimate_tokens(removed_msg, encoder)
        return messages


class SummaryBuilder(ContextBuilder):
    """
    Advanced strategy: keep recent messages + a summary of earlier conversation.
    Not implemented in Phase 0, but placeholder for future.
    """

    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        # For now, fall back to LastNBuilder
        # In future: generate or retrieve a summary of earlier messages, prepend as system message
        return LastNBuilder().build(user_history, max_tokens)
