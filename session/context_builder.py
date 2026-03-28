"""
Context Builder: Decides which parts of user_history should be sent to the LLM.

Provides strategies for building the agent_context from the full user_history,
allowing the agent to operate within token limits while preserving the full
conversation for the user's view.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
import tiktoken
import os
import logging


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
        import json
        if encoder is None:
            try:
                encoder = tiktoken.get_encoding("cl100k_base")  # OpenAI default
            except Exception:
                # Fallback: rough estimate
                return len(json.dumps(message)) // 4

        # Tokenize the entire message as JSON to include all fields (role, content, tool_calls, etc.)
        # This matches how the OpenAI API counts tokens for the message object
        message_json = json.dumps(message)
        return len(encoder.encode(message_json))


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

        # Debug output
        if os.environ.get('DEBUG_CONTEXT'):
            logging.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build returning {len(context)} context messages')
            # Show token estimate
            try:
                encoder = tiktoken.get_encoding("cl100k_base")
                total_tokens = sum(self._estimate_tokens(msg, encoder) for msg in context)
                logging.debug(f'[DEBUG_CONTEXT] Estimated token count for context: {total_tokens}')
            except Exception:
                pass
        
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
    
    Looks for summary system messages in user_history and assembles context as:
    - Main system prompt (first non-summary system message)
    - Most recent summary message (if any)
    - Most recent N turns after summary (where N = pruning_keep_recent_turns from summary metadata)
    """

    def __init__(self, default_keep_turns: int = 5):
        """Initialize with default number of turns to keep when no summary exists."""
        self.default_keep_turns = default_keep_turns

    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Build context from full history, respecting summaries.
        
        If no summary found, falls back to LastNBuilder with default_keep_turns.
        """
        # Debug context building
        if os.environ.get('DEBUG_CONTEXT'):
            logging.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build called with {len(user_history)} history messages')
            if user_history:
                for i, msg in enumerate(user_history):
                    role = msg.get('role', 'unknown')
                    content_preview = str(msg.get('content', ''))[:100].replace('\n', ' ')
                    logging.debug(f'  [{i}] role={role}, content: {content_preview}')
        
        if not user_history:
            return []
        
        # Find main system prompt and latest summary
        main_prompt, summary_idx, summary_msg = self._find_main_prompt_and_summary(user_history)
        
        # Determine how many turns to keep
        keep_turns = self.default_keep_turns
        if summary_msg is not None:
            keep_turns = summary_msg.get('pruning_keep_recent_turns', self.default_keep_turns)
        
        # Get messages after summary (or all messages if no summary)
        if summary_idx >= 0:
            # Summary will be added separately, so take messages after summary
            post_summary = user_history[summary_idx + 1:]  # excludes summary
        else:
            post_summary = user_history
        
        # Filter out system messages from post-summary (except we'll add main_prompt and summary)
        non_system = [msg for msg in post_summary if msg.get('role') != 'system']
        
        # Group into turns
        turns = self._group_messages_into_turns(non_system)
        
        # Keep only most recent keep_turns turns
        if keep_turns < len(turns):
            turns = turns[-keep_turns:]
        
        # Assemble context
        context = []
        if main_prompt:
            context.append(main_prompt)
        if summary_msg is not None:
            context.append(summary_msg)
        
        # Flatten kept turns
        for turn in turns:
            context.extend(turn)
        
        # If max_tokens is provided, further truncate from oldest turns
        if max_tokens is not None:
            context = self._truncate_to_max_tokens(context, max_tokens, preserve_system=True)
        
        # Debug output
        if os.environ.get('DEBUG_CONTEXT'):
            logging.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build returning {len(context)} context messages')
            # Show token estimate
            try:
                encoder = tiktoken.get_encoding("cl100k_base")
                total_tokens = sum(self._estimate_tokens(msg, encoder) for msg in context)
                logging.debug(f'[DEBUG_CONTEXT] Estimated token count for context: {total_tokens}')
            except Exception:
                pass
        
        return context
    
    def _find_main_prompt_and_summary(self, user_history: List[Dict[str, Any]]) -> \
            Tuple[Optional[Dict[str, Any]], int, Optional[Dict[str, Any]]]:
        """
        Find main system prompt and latest summary.
        
        Returns:
            (main_prompt, summary_index, summary_message)
            summary_index is -1 if no summary found
        """
        main_prompt = None
        summary_idx = -1
        summary_msg = None
        
        # First pass: find main prompt (first non-summary system message)
        for msg in user_history:
            if msg.get('role') == 'system':
                content = msg.get('content', '')
                if 'Summary of previous conversation:' not in content:
                    main_prompt = msg
                    break
        
        # Second pass: find latest summary (scan from end)
        for i in range(len(user_history) - 1, -1, -1):
            msg = user_history[i]
            if msg.get('role') == 'system' and \
               'Summary of previous conversation:' in msg.get('content', ''):
                summary_idx = i
                summary_msg = msg
                break
        
        return main_prompt, summary_idx, summary_msg
    
    def _group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group messages into conversation turns (user + assistant + tool responses)."""
        turns = []
        current_turn = []
        
        for msg in messages:
            role = msg.get('role')
            if role == 'user':
                if current_turn:
                    turns.append(current_turn)
                    current_turn = []
                current_turn.append(msg)
            elif role == 'assistant':
                current_turn.append(msg)
            elif role == 'tool':
                current_turn.append(msg)
            # system messages already filtered out
        
        if current_turn:
            turns.append(current_turn)
            
        return turns
    
    def _truncate_to_max_tokens(self, messages: List[Dict[str, Any]], max_tokens: int, 
                               preserve_system: bool = True) -> List[Dict[str, Any]]:
        """Truncate messages from the beginning (oldest) until under max_tokens."""
        try:
            encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            encoder = None
        
        total = sum(self._estimate_tokens(msg, encoder) for msg in messages)
        
        # Start from index 0, but skip system messages if preserve_system=True
        idx_to_remove = 0
        while total > max_tokens and idx_to_remove < len(messages):
            if preserve_system and messages[idx_to_remove].get('role') == 'system':
                idx_to_remove += 1
                continue
                
            # Remove this message
            removed_msg = messages.pop(idx_to_remove)
            total -= self._estimate_tokens(removed_msg, encoder)
            # Note: idx_to_remove stays the same because list shifted
        
        return messages
