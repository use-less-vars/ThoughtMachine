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
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/../')

logger = logging.getLogger(__name__)
# Lazy import to avoid circular dependency
_log_real = None
def log(level, tag, message, data=None, event_type=None, truncate_hint=None):
    global _log_real
    if _log_real is None:
        from agent.logging import log as real_log
        _log_real = real_log
    return _log_real(level, tag, message, data, event_type, truncate_hint)
# Import for Phase 3 debugging
try:
    from agent.logging_helpers import dump_messages
    DUMP_MESSAGES_AVAILABLE = True
except ImportError:
    DUMP_MESSAGES_AVAILABLE = False
    dump_messages = lambda messages, label: None
DEBUG_CONTEXT = os.environ.get('DEBUG_CONTEXT') is not None

class ContextBuilder(ABC):
    """Abstract base class for context building strategies."""

    @abstractmethod
    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int]=None) -> List[Dict[str, Any]]:
        """
        Build the agent_context from the full user_history.

        Args:
            user_history: Complete list of message dicts (role, content)
            max_tokens: Optional maximum token count for the context

        Returns:
            A list of message dicts to be sent to the LLM.
        """
        pass

    def _estimate_tokens(self, message: Dict[str, Any], encoder: Optional[tiktoken.Encoding]=None) -> int:
        """Estimate token count for a message."""
        log('DEBUG', 'core.context_builder', f"_estimate_tokens called, message role: {message.get('role', 'unknown')}")
        content_preview = str(message.get('content', ''))[:100].replace('\n', ' ') if 'content' in message else 'no content'
        log('DEBUG', 'core.context_builder', f'  content preview: {content_preview}')
        import json
        if encoder is None:
            try:
                encoder = tiktoken.get_encoding('cl100k_base')
            except Exception:
                return len(json.dumps(message)) // 4
        message_json = json.dumps(message)
        token_count = len(encoder.encode(message_json))
        log('DEBUG', 'core.context_builder', f'  estimated tokens: {token_count}')
        return token_count

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
                tool_calls = msg.get('tool_calls', [])
                tool_call_id_to_call = {}
                for tc in tool_calls:
                    tc_id = tc.get('id')
                    if tc_id:
                        tool_call_id_to_call[tc_id] = tc
                tool_messages = []
                j = i + 1
                while j < n and messages[j].get('role') == 'tool':
                    tool_messages.append(messages[j])
                    j += 1
                tool_call_id_to_message = {}
                extra_tool_messages = []
                for tool_msg in tool_messages:
                    tool_call_id = tool_msg.get('tool_call_id')
                    if tool_call_id in tool_call_id_to_call:
                        if tool_call_id not in tool_call_id_to_message:
                            tool_call_id_to_message[tool_call_id] = tool_msg
                        else:
                            extra_tool_messages.append(tool_msg)
                            logger.warning(f'[DEBUG_CONTEXT] Removing duplicate tool message for tool_call_id: {tool_call_id}')
                    else:
                        extra_tool_messages.append(tool_msg)
                        logger.warning(f'[DEBUG_CONTEXT] Removing tool message with non-matching tool_call_id: {tool_call_id}')
                if len(tool_call_id_to_message) < len(tool_call_id_to_call):
                    cleaned_msg = msg.copy()
                    cleaned_msg.pop('tool_calls', None)
                    result.append(cleaned_msg)
                    missing_count = len(tool_call_id_to_call) - len(tool_call_id_to_message)
                    logger.warning(f'[DEBUG_CONTEXT] Removing incomplete tool sequence: {len(tool_call_id_to_call)} calls but only {len(tool_call_id_to_message)} valid tool messages ({missing_count} missing)')
                    i = j
                else:
                    result.append(msg)
                    for tc in tool_calls:
                        tc_id = tc.get('id')
                        if tc_id and tc_id in tool_call_id_to_message:
                            result.append(tool_call_id_to_message[tc_id])
                    i = j
            elif role == 'tool':
                found_matching_assistant = False
                tool_call_id = msg.get('tool_call_id')
                for j in range(len(result) - 1, -1, -1):
                    prev_msg = result[j]
                    prev_role = prev_msg.get('role')
                    if prev_role == 'assistant':
                        tool_calls = prev_msg.get('tool_calls', [])
                        tool_call_ids = {tc.get('id') for tc in tool_calls if tc.get('id')}
                        if tool_call_id in tool_call_ids:
                            found_matching_assistant = True
                        break
                    elif prev_role == 'user':
                        break
                if found_matching_assistant:
                    logger.warning(f'[DEBUG_CONTEXT] Removing duplicate tool message: {tool_call_id}')
                else:
                    logger.warning(f'[DEBUG_CONTEXT] Removing orphaned tool message: {tool_call_id}')
                i += 1
            else:
                result.append(msg)
                i += 1
        return result

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

    def __init__(self, default_keep_turns: int=5):
        """Initialize with default number of turns to keep when no summary exists."""
        self.default_keep_turns = default_keep_turns

    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int]=None) -> List[Dict[str, Any]]:
        """
        Build context from full history, respecting summaries.
        
        When a summary exists: includes all messages after the summary, with token
        truncation removing newest messages first (to preserve originally-kept turns).
        
        When no summary exists: includes all messages, with token truncation
        removing oldest messages first.
        """
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
        main_prompt, summary_idx, summary_msg = self._find_main_prompt_and_summary(user_history)
        log('DEBUG', 'session.summary_builder', f'SummaryBuilder.build: found summary_idx={summary_idx}, summary_msg exists={summary_msg is not None}')
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Found main_prompt at index: {(0 if main_prompt else -1)}')
            logger.debug(f"[DEBUG_CONTEXT] Found summary at index: {summary_idx}, content preview: {(summary_msg.get('content', '')[:100] if summary_msg else 'None')}")
            if summary_msg:
                logger.debug(f"[DEBUG_CONTEXT] Summary metadata: pruning_keep_recent_turns={summary_msg.get('pruning_keep_recent_turns')}, pruning_insertion_idx={summary_msg.get('pruning_insertion_idx')}")
        keep_turns = self.default_keep_turns
        if summary_msg is not None:
            keep_turns = summary_msg.get('pruning_keep_recent_turns', self.default_keep_turns)
        log('DEBUG', 'session.summary_builder', f'SummaryBuilder.build: keep_turns={keep_turns}')
        if summary_idx >= 0:
            post_summary = user_history[summary_idx + 1:]
        else:
            post_summary = user_history
        system_warnings = []
        non_system = []
        for msg in post_summary:
            if msg.get('role') == 'system':
                if main_prompt is not None and msg == main_prompt:
                    continue
                system_warnings.append(msg)
            else:
                non_system.append(msg)

        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Collected {len(system_warnings)} system warnings:')
            for i, warn in enumerate(system_warnings):
                content_preview = warn.get('content', '')[:100].replace('\n', ' ')
                logger.debug(f'  [{i}] {content_preview}')
        turns = self._group_messages_into_turns(non_system)
        log('DEBUG', 'session.summary_builder', f'SummaryBuilder.build: grouped {len(non_system)} non-system messages into {len(turns)} turns')
        if summary_msg is not None:
            pass
        else:
            pass
        log('DEBUG', 'session.summary_builder', f'SummaryBuilder.build: after selection, keeping {len(turns)} turns')
        context = []
        if main_prompt:
            context.append(main_prompt)
        if summary_msg is not None:
            context.append(summary_msg)
        context.extend(system_warnings)
        for turn in turns:
            context.extend(turn)
        log('DEBUG', 'session.summary_builder', f'SummaryBuilder.build: assembled {len(context)} messages before truncation')
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Context before truncation ({len(context)} messages):')
            for i, msg in enumerate(context):
                role = msg.get('role')
                content_preview = str(msg.get('content', ''))[:80].replace('\\n', ' ')
                logger.debug(f'  [{i}] role={role}: {content_preview}')
        if max_tokens is not None:
            context = self._truncate_to_max_tokens(context, max_tokens, preserve_system=True, remove_from_end=summary_msg is not None)
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Context after truncation ({len(context)} messages):')
            for i, msg in enumerate(context):
                role = msg.get('role')
                content_preview = str(msg.get('content', ''))[:80].replace('\\n', ' ')
                logger.debug(f'  [{i}] role={role}: {content_preview}')
        context = self._cleanup_orphaned_tool_messages(context)
        log('DEBUG', 'session.summary_builder', f'SummaryBuilder.build: final context length {len(context)} messages')
        # Phase 3 logging: LLM context built
        try:
            encoder = tiktoken.get_encoding('cl100k_base')
            total_tokens = sum((self._estimate_tokens(msg, encoder) for msg in context))
            log('DEBUG', 'core.context', 'LLM context built', {
                'num_messages': len(context),
                'estimated_tokens': total_tokens
            })
            if DUMP_MESSAGES_AVAILABLE:
                dump_messages(context, 'llm_context final')
        except Exception as e:
            log('DEBUG', 'core.context', f'Failed to compute token count: {e}')
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] SummaryBuilder.build returning {len(context)} context messages')
            try:
                encoder = tiktoken.get_encoding('cl100k_base')
                total_tokens = sum((self._estimate_tokens(msg, encoder) for msg in context))
                logger.debug(f'[DEBUG_CONTEXT] Estimated token count for context: {total_tokens}')
            except Exception:
                pass
        for msg in context:
            if msg.get('role') == 'system':
                content = msg.get('content', '')
                if '[SYSTEM]' in content:
                    logger.info(f'[WARNING_IN_CONTEXT] {content[:100]}')
        return context

    def _find_main_prompt_and_summary(self, user_history: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], int, Optional[Dict[str, Any]]]:
        """
        Find main system prompt and latest summary.
        
        Returns:
            (main_prompt, summary_index, summary_message)
            summary_index is -1 if no summary found
        """
        main_prompt = None
        summary_idx = -1
        summary_msg = None
        for msg in user_history:
            if msg.get('role') == 'system':
                content = msg.get('content', '')
                if 'Summary of previous conversation:' not in content:
                    main_prompt = msg
                    break
        summaries_found = []
        for i in range(len(user_history) - 1, -1, -1):
            msg = user_history[i]
            if msg.get('role') == 'system' and 'Summary of previous conversation:' in msg.get('content', ''):
                summaries_found.append((i, msg))
        if os.environ.get('DEBUG_CONTEXT'):
            logger.debug(f'[DEBUG_CONTEXT] Found {len(summaries_found)} summaries total')
            for idx, summ_msg in summaries_found:
                logger.debug(f"[DEBUG_CONTEXT] Summary at index {idx}: {summ_msg.get('content', '')[:100]}...")
        if summaries_found:
            summaries_found.sort(key=lambda x: x[0], reverse=True)
            summary_idx, summary_msg = summaries_found[0]
            if os.environ.get('DEBUG_CONTEXT'):
                logger.debug(f"[DEBUG_CONTEXT] Using latest summary at index {summary_idx} with pruning_keep_recent_turns={summary_msg.get('pruning_keep_recent_turns')}")
                for key, value in summary_msg.items():
                    if key not in ['role', 'content']:
                        logger.debug(f'[DEBUG_CONTEXT]   {key}: {value}')
        else:
            summary_idx = -1
            summary_msg = None
        return (main_prompt, summary_idx, summary_msg)

    def _group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group messages into conversation turns (user + assistant + tool responses).
        
        Rules:
        - User messages always start a new turn
        - Assistant messages with tool_calls can also start a turn (after pruning)
        - All messages after a turn start belong to that turn until next user message
        - System messages should be filtered out before calling this method
        - Turns that don't start with user or assistant-with-tools are discarded
        
        This ensures tool call sequences stay together, even when they start with
        an assistant (due to pruning cutting off the user part of the turn).
        """
        turns = []
        current_turn = []
        import os
        debug = os.environ.get('DEBUG_CONTEXT')
        if debug:
            logger.debug(f'[DEBUG_CONTEXT] Grouping {len(messages)} messages')
            max_to_show = 10
            for i, msg in enumerate(messages[:max_to_show]):
                role = msg.get('role')
                content_preview = str(msg.get('content', ''))[:50]
                has_tool_calls = 'tool_calls' in msg and msg['tool_calls']
                logger.debug(f'  [{i}] {role}: {content_preview}... tool_calls={has_tool_calls}')
            if len(messages) > max_to_show:
                logger.debug(f'  ... and {len(messages) - max_to_show} more messages')
        for msg in messages:
            role = msg.get('role')
            if role == 'system':
                continue
            if role == 'user':
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif role == 'assistant' and msg.get('tool_calls'):
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif current_turn:
                if role == 'tool':
                    if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                        current_turn.append(msg)
                    else:
                        if debug:
                            tool_call_id = msg.get('tool_call_id', 'unknown')
                            logger.debug(f'[DEBUG_CONTEXT] Discarding orphaned tool message: {tool_call_id}')
                        continue
                else:
                    current_turn.append(msg)
            else:
                if debug:
                    logger.debug(f'[DEBUG_CONTEXT] Discarding orphaned {role} message')
                continue
        if current_turn:
            turns.append(current_turn)
        valid_turns = []
        for turn in turns:
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get('role')
            if first_role == 'user':
                valid_turns.append(turn)
            elif first_role == 'assistant' and first_msg.get('tool_calls'):
                valid_turns.append(turn)
            elif debug:
                logger.debug(f'[DEBUG_CONTEXT] Discarding turn starting with {first_role}')
        if debug:
            logger.debug(f'[DEBUG_CONTEXT] Returned {len(valid_turns)} valid turns')
            max_to_show = 10
            for i, turn in enumerate(valid_turns[:max_to_show]):
                logger.debug(f"  Turn {i}: {[msg.get('role') for msg in turn]}")
            if len(valid_turns) > max_to_show:
                logger.debug(f'  ... and {len(valid_turns) - max_to_show} more turns')
        return valid_turns

    def _truncate_to_max_tokens(self, messages: List[Dict[str, Any]], max_tokens: int, preserve_system: bool=True, remove_from_end: bool=False) -> List[Dict[str, Any]]:
        """Truncate messages until under max_tokens.
        
        By default removes from beginning (oldest) and preserves system messages.
        If remove_from_end=True, removes from end (newest) instead.
        """
        try:
            encoder = tiktoken.get_encoding('cl100k_base')
        except Exception:
            encoder = None
        total = sum((self._estimate_tokens(msg, encoder) for msg in messages))
        if remove_from_end:
            while total > max_tokens and len(messages) > 0:
                if preserve_system and messages[-1].get('role') == 'system':
                    if all((msg.get('role') == 'system' for msg in messages)):
                        break
                    for i in range(2, len(messages) + 1):
                        if not (preserve_system and messages[-i].get('role') == 'system'):
                            removed_msg = messages.pop(-i)
                            total -= self._estimate_tokens(removed_msg, encoder)
                            if os.environ.get('DEBUG_CONTEXT'):
                                role = removed_msg.get('role', 'unknown')
                                content_preview = str(removed_msg.get('content', ''))[:100].replace('\\n', ' ')
                                logger.debug(f'[DEBUG_CONTEXT] Truncation removed message [-i]: role={role}, content: {content_preview}')
                            break
                    else:
                        break
                else:
                    removed_msg = messages.pop()
                    total -= self._estimate_tokens(removed_msg, encoder)
                    if os.environ.get('DEBUG_CONTEXT'):
                        role = removed_msg.get('role', 'unknown')
                        content_preview = str(removed_msg.get('content', ''))[:100].replace('\\n', ' ')
                        logger.debug(f'[DEBUG_CONTEXT] Truncation removed message [end]: role={role}, content: {content_preview}')
        else:
            idx_to_remove = 0
            while total > max_tokens and idx_to_remove < len(messages):
                if preserve_system and messages[idx_to_remove].get('role') == 'system':
                    idx_to_remove += 1
                    continue
                removed_msg = messages.pop(idx_to_remove)
                total -= self._estimate_tokens(removed_msg, encoder)
        return messages