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

logger = logging.getLogger(__name__)

# Debug flag for context building logging
DEBUG_CONTEXT = os.environ.get('DEBUG_CONTEXT') is not None


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

    @staticmethod
    def _cleanup_orphaned_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove orphaned tool messages and fix incomplete tool call sequences.
        
        This prevents "Tool results must follow an assistant tool call" and 
        "insufficient tool messages following tool_calls message" errors from the LLM.
        
        Handles three cases:
        1. Tool messages without a preceding assistant with tool_calls (orphaned)
        2. Tool messages that don't match any tool_call_id from the preceding assistant
        3. Assistant messages with tool_calls that don't have complete tool responses
        4. Duplicate tool messages for the same tool_call_id
        
        Returns a new list with valid message sequences only.
        """
        if not messages:
            return messages
        
        result = []
        i = 0
        n = len(messages)
        
        while i < n:
            msg = messages[i]
            role = msg.get('role')
            
            if role == 'assistant' and msg.get('tool_calls'):
                # Found an assistant with tool calls
                tool_calls = msg.get('tool_calls', [])
                # Create mapping of tool_call_id to tool call for validation
                tool_call_id_to_call = {}
                for tc in tool_calls:
                    tc_id = tc.get('id')
                    if tc_id:
                        tool_call_id_to_call[tc_id] = tc
                
                # Collect all tool messages that follow this assistant
                # (before the next non-tool message)
                tool_messages = []
                j = i + 1
                while j < n and messages[j].get('role') == 'tool':
                    tool_messages.append(messages[j])
                    j += 1
                
                # Map each tool_call_id to the first matching tool message
                tool_call_id_to_message = {}
                extra_tool_messages = []  # Duplicates or non-matching
                
                for tool_msg in tool_messages:
                    tool_call_id = tool_msg.get('tool_call_id')
                    if tool_call_id in tool_call_id_to_call:
                        # First message for this tool_call_id? Keep it
                        if tool_call_id not in tool_call_id_to_message:
                            tool_call_id_to_message[tool_call_id] = tool_msg
                        else:
                            # Duplicate tool message for same tool_call_id
                            extra_tool_messages.append(tool_msg)
                            logger.warning(f'[DEBUG_CONTEXT] Removing duplicate tool message for tool_call_id: {tool_call_id}')
                    else:
                        # Tool message doesn't match any tool call from this assistant
                        extra_tool_messages.append(tool_msg)
                        logger.warning(f'[DEBUG_CONTEXT] Removing tool message with non-matching tool_call_id: {tool_call_id}')
                
                # Check if we have at least one tool message for each tool call
                if len(tool_call_id_to_message) < len(tool_call_id_to_call):
                    # Incomplete: some tool calls missing responses
                    # Remove tool_calls field from assistant
                    cleaned_msg = msg.copy()
                    cleaned_msg.pop('tool_calls', None)
                    result.append(cleaned_msg)
                    missing_count = len(tool_call_id_to_call) - len(tool_call_id_to_message)
                    logger.warning(f'[DEBUG_CONTEXT] Removing incomplete tool sequence: {len(tool_call_id_to_call)} calls but only {len(tool_call_id_to_message)} valid tool messages ({missing_count} missing)')
                    # Skip all tool messages (they're orphaned)
                    i = j
                else:
                    # Complete: each tool call has at least one response
                    # Keep assistant with tool_calls
                    result.append(msg)
                    # Add tool messages in the order of tool calls
                    for tc in tool_calls:
                        tc_id = tc.get('id')
                        if tc_id and tc_id in tool_call_id_to_message:
                            result.append(tool_call_id_to_message[tc_id])
                    # Skip all tool messages (we've added the ones we need)
                    i = j
            elif role == 'tool':
                # This is a tool message without a preceding assistant with tool_calls
                # Look backwards in result to find if there's an assistant with tool_calls
                # that could own this tool message
                found_matching_assistant = False
                tool_call_id = msg.get('tool_call_id')
                
                # Search backwards through result
                for j in range(len(result) - 1, -1, -1):
                    prev_msg = result[j]
                    prev_role = prev_msg.get('role')
                    if prev_role == 'assistant':
                        # Check if this assistant has tool_calls that match our tool_call_id
                        tool_calls = prev_msg.get('tool_calls', [])
                        tool_call_ids = {tc.get('id') for tc in tool_calls if tc.get('id')}
                        if tool_call_id in tool_call_ids:
                            found_matching_assistant = True
                        # Stop searching at previous assistant
                        break
                    elif prev_role == 'user':
                        # No assistant between user and tool
                        break
                
                if found_matching_assistant:
                    # This tool message belongs to an assistant we've already processed
                    # It should have been added with that assistant, so it's a duplicate
                    logger.warning(f'[DEBUG_CONTEXT] Removing duplicate tool message: {tool_call_id}')
                else:
                    # Orphaned tool message - skip it
                    logger.warning(f'[DEBUG_CONTEXT] Removing orphaned tool message: {tool_call_id}')
                i += 1
            else:
                # Not a tool message or assistant with tool_calls, keep it
                result.append(msg)
                i += 1
        
        return result


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

        # Clean up any orphaned tool messages that may have been created by truncation
        context = self._cleanup_orphaned_tool_messages(context)

        # Debug output
        if DEBUG_CONTEXT:
            logger.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build returning {len(context)} context messages')
            # Show token estimate
            try:
                encoder = tiktoken.get_encoding("cl100k_base")
                total_tokens = sum(self._estimate_tokens(msg, encoder) for msg in context)
                logger.debug(f'[DEBUG_CONTEXT] Estimated token count for context: {total_tokens}')
            except Exception:
                pass
        
        return context

    def _truncate_to_max_tokens(self, messages: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
        """Truncate messages until under max_tokens.
        
        By default removes from beginning (oldest). If remove_from_end=True,
        removes from end (newest) instead.
        """
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
    - First N turns after summary (where N = pruning_keep_recent_turns from summary metadata)
      These are the turns that were kept from BEFORE the summarization event.
    - All newer turns that occurred after those kept turns
    
    When token limits require truncation and a summary exists, removes from the
    end (newest messages) first to preserve the originally-kept turns.
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
            logger.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build called with {len(user_history)} history messages')
            if user_history:
                max_to_show = 10
                for i, msg in enumerate(user_history[:max_to_show]):
                    role = msg.get('role', 'unknown')
                    content_preview = str(msg.get('content', ''))[:100].replace('\n', ' ')
                    logger.debug(f'  [{i}] role={role}, content: {content_preview}')
                if len(user_history) > max_to_show:
                    logger.debug(f'  ... and {len(user_history) - max_to_show} more messages')
        
        if not user_history:
            return []
        
        # Find main system prompt and latest summary
        main_prompt, summary_idx, summary_msg = self._find_main_prompt_and_summary(user_history)
        
        # Debug: log what we found
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Found main_prompt at index: {0 if main_prompt else -1}')
            logger.debug(f'[DEBUG_CONTEXT] Found summary at index: {summary_idx}, content preview: {summary_msg.get("content", "")[:100] if summary_msg else "None"}')
            if summary_msg:
                logger.debug(f'[DEBUG_CONTEXT] Summary metadata: pruning_keep_recent_turns={summary_msg.get("pruning_keep_recent_turns")}, pruning_insertion_idx={summary_msg.get("pruning_insertion_idx")}')
        
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

        # Determine which turns to keep
        if summary_msg is not None and keep_turns > 0:
            # We have a summary: keep_turns refers to turns from BEFORE the summary that were kept
            # These are the first `keep_turns` turns after the summary.
            # We MUST keep all of these originally-kept turns.
            # All newer turns (after the kept turns) are also included, subject to token limits.
            
            # Always keep the originally-kept turns
            if len(turns) >= keep_turns:
                kept_turns = turns[:keep_turns]
                newer_turns = turns[keep_turns:]
                
                # Start with kept turns + ALL newer turns
                # Token limits will trim from newest first if needed (remove_from_end=True)
                selected_turns = kept_turns + newer_turns
                
                turns = selected_turns
            else:
                # Fewer turns than keep_turns (shouldn't happen but handle gracefully)
                turns = turns
        else:
            # No summary or keep_turns == 0: keep only most recent keep_turns turns
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
        
        # If max_tokens is provided, further truncate
        # When there's a summary, remove newest turns first (after summary) to preserve originally-kept turns
        # When no summary, remove oldest turns first
        if max_tokens is not None:
            context = self._truncate_to_max_tokens(context, max_tokens, 
                                                  preserve_system=True, 
                                                  remove_from_end=(summary_msg is not None))
        
        # Clean up any orphaned tool messages that may have been created by truncation
        context = self._cleanup_orphaned_tool_messages(context)
        
        # Debug output
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build returning {len(context)} context messages')
            # Show token estimate
            try:
                encoder = tiktoken.get_encoding("cl100k_base")
                total_tokens = sum(self._estimate_tokens(msg, encoder) for msg in context)
                logger.debug(f'[DEBUG_CONTEXT] Estimated token count for context: {total_tokens}')
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
        summaries_found = []
        for i in range(len(user_history) - 1, -1, -1):
            msg = user_history[i]
            if msg.get('role') == 'system' and \
               'Summary of previous conversation:' in msg.get('content', ''):
                summaries_found.append((i, msg))
        
        # Debug: log all summaries found
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Found {len(summaries_found)} summaries total')
            for idx, summ_msg in summaries_found:
                logger.debug(f'[DEBUG_CONTEXT] Summary at index {idx}: {summ_msg.get("content", "")[:100]}...')
        
        # Pick the latest summary (highest index)
        if summaries_found:
            # Sort by index descending to get latest
            summaries_found.sort(key=lambda x: x[0], reverse=True)
            summary_idx, summary_msg = summaries_found[0]
            if os.environ.get('DEBUG_CONTEXT'):
                logger.debug(f'[DEBUG_CONTEXT] Using latest summary at index {summary_idx} with pruning_keep_recent_turns={summary_msg.get("pruning_keep_recent_turns")}')
                # Log all metadata
                for key, value in summary_msg.items():
                    if key not in ['role', 'content']:
                        logger.debug(f'[DEBUG_CONTEXT]   {key}: {value}')
        else:
            summary_idx = -1
            summary_msg = None
        
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
                # Validate that previous message in current_turn is assistant with tool_calls
                if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                    current_turn.append(msg)
                else:
                    # Orphaned tool message - log warning and skip it (cannot be used without assistant)
                    tool_call_id = msg.get('tool_call_id', 'unknown')
                    logger.warning(f'[DEBUG_CONTEXT] Orphaned tool message skipped: {tool_call_id}. Previous message in turn: {current_turn[-1] if current_turn else "none"}')
                    # Skip orphaned tool message to avoid API errors
                    continue
            # system messages already filtered out
        
        if current_turn:
            turns.append(current_turn)
            
        return turns
    
    def _truncate_to_max_tokens(self, messages: List[Dict[str, Any]], max_tokens: int, 
                               preserve_system: bool = True, remove_from_end: bool = False) -> List[Dict[str, Any]]:
        """Truncate messages until under max_tokens.
        
        By default removes from beginning (oldest) and preserves system messages.
        If remove_from_end=True, removes from end (newest) instead.
        """
        try:
            encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            encoder = None
        
        total = sum(self._estimate_tokens(msg, encoder) for msg in messages)
        
        if remove_from_end:
            # Remove from end (newest) first
            while total > max_tokens and len(messages) > 0:
                # Skip system messages at the end (though there shouldn't be any)
                if preserve_system and messages[-1].get('role') == 'system':
                    # Move to previous message
                    # But first check if we're stuck (all messages are system)
                    if all(msg.get('role') == 'system' for msg in messages):
                        break
                    # Try the next message from end
                    for i in range(2, len(messages) + 1):
                        if not (preserve_system and messages[-i].get('role') == 'system'):
                            removed_msg = messages.pop(-i)
                            total -= self._estimate_tokens(removed_msg, encoder)
                            break
                    else:
                        # All remaining messages are system
                        break
                else:
                    removed_msg = messages.pop()
                    total -= self._estimate_tokens(removed_msg, encoder)
        else:
            # Remove from beginning (oldest) first
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
