"""
History Provider: Manages append-only conversation history with summary-based pruning.

Key principles:
1. Session.user_history is append-only (full log of all messages)
2. Summary messages are added as regular system messages with metadata
3. Runtime context for LLM is assembled on demand based on last summary + recent turns
4. Token counting and pruning decisions are centralized here
"""
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import logging
import os
import sys

# Import our clean debug logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../")
try:
    from debug_pruning import (
        debug_log, log_session_history, log_history_provider_reconstruction,
        log_message_insertion, log_pruning_operation, log_token_count,
        log_summary_operation, truncate_message
    )
    DEBUG_PRUNING_AVAILABLE = True
except ImportError:
    DEBUG_PRUNING_AVAILABLE = False
    debug_log = lambda *args, **kwargs: None
    log_session_history = lambda *args, **kwargs: None
    log_history_provider_reconstruction = lambda *args, **kwargs: None
    log_message_insertion = lambda *args, **kwargs: None
    log_pruning_operation = lambda *args, **kwargs: None
    log_token_count = lambda *args, **kwargs: None
    log_summary_operation = lambda *args, **kwargs: None
    truncate_message = lambda entry, max_len: str(entry)[:max_len]

from .models import Session
from .context_builder import ContextBuilder, SummaryBuilder

logger = logging.getLogger(__name__)

# Default number of recent turns to keep after a summary
DEFAULT_KEEP_TURNS = 5

# Debug flag for detailed history provider logging
DEBUG_HISTORY_PROVIDER = os.environ.get('DEBUG_HISTORY_PROVIDER') is not None
DEBUG_CONTEXT = os.environ.get('DEBUG_CONTEXT') is not None

# Configure logging if debug flags are set
if DEBUG_HISTORY_PROVIDER or DEBUG_CONTEXT:
    import sys
    # Only configure specific loggers, not the root logger
    # This prevents verbose output from openai, httpcore, httpx, etc.
    debug_loggers = ['session.history_provider', 'session.context_builder', 'agent']
    
    # Create a formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Create a handler for stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    
    # Configure each logger
    for logger_name in debug_loggers:
        logger_obj = logging.getLogger(logger_name)
        logger_obj.setLevel(logging.DEBUG)
        # Remove existing handlers to avoid duplicates
        logger_obj.handlers = []
        logger_obj.addHandler(handler)
        logger_obj.propagate = False  # Don't propagate to root logger
    
    # Also set root logger to WARNING to suppress other DEBUG messages
    logging.getLogger().setLevel(logging.WARNING)


class HistoryProvider:
    """Manages conversation history for token-limited LLM context windows."""
    
    def __init__(self, session: Session, token_limit: Optional[int] = None):
        self._session = session
        self.token_limit = token_limit
        self._cached_context: Optional[List[Dict[str, Any]]] = None
        
        # Use SummaryBuilder for context assembly
        self.context_builder = SummaryBuilder()
    
    @property
    def session(self):
        return self._session
    
    @session.setter
    def session(self, value):
        self._session = value
        # Clear cache when session changes
        self._cached_context = None
        
    def get_context_for_llm(self) -> List[Dict[str, Any]]:
        """Return messages suitable for LLM context (pruned view)."""
        if self._cached_context is not None:
            return self._cached_context
            
        # Use the context builder to assemble context from full history
        context = self.context_builder.build(
            self.session.user_history, 
            max_tokens=self.token_limit
        )
        
        # Debug output - use our clean debug logging
        if DEBUG_CONTEXT and DEBUG_PRUNING_AVAILABLE:
            debug_log('history_provider', 
                     f'get_context_for_llm: {len(self.session.user_history)} history → {len(context)} context')
            if self.token_limit:
                debug_log('history_provider', f'Token limit: {self.token_limit}')
            
            # Log session history with our clean format
            log_session_history(self.session.user_history, 'Session history')
            log_history_provider_reconstruction('HistoryProvider', 
                                              self.session.user_history, context)
        elif DEBUG_CONTEXT:
            # Fallback to old debug logging
            logger.debug(f'[DEBUG_CONTEXT] HistoryProvider.get_context_for_llm: full history={len(self.session.user_history)} messages, context={len(context)} messages')
            if self.token_limit:
                logger.debug(f'[DEBUG_CONTEXT] Token limit: {self.token_limit}')
        
        # Validate context and debug logging
        if DEBUG_HISTORY_PROVIDER:
            logger.info(f'[DEBUG_HISTORY_PROVIDER] Original history ({len(self.session.user_history)} messages):')
            max_to_show = 10
            for i, msg in enumerate(self.session.user_history[:max_to_show]):
                role = msg.get('role')
                content_preview = str(msg.get('content', ''))[:100].replace('\n', ' ')
                tool_calls = msg.get('tool_calls')
                logger.info(f'  [{i}] {role}: {content_preview} tool_calls={tool_calls}')
            if len(self.session.user_history) > max_to_show:
                logger.info(f'  ... and {len(self.session.user_history) - max_to_show} more messages')
            logger.info(f'[DEBUG_HISTORY_PROVIDER] Context ({len(context)} messages):')
            max_to_show = 10
            for i, msg in enumerate(context[:max_to_show]):
                role = msg.get('role')
                content_preview = str(msg.get('content', ''))[:100].replace('\n', ' ')
                tool_calls = msg.get('tool_calls')
                logger.info(f'  [{i}] {role}: {content_preview} tool_calls={tool_calls}')
            if len(context) > max_to_show:
                logger.info(f'  ... and {len(context) - max_to_show} more messages')
        
        # Clean up any orphaned tool messages to prevent LLM errors
        original_len = len(context)
        context = ContextBuilder._cleanup_orphaned_tool_messages(context)
        if DEBUG_HISTORY_PROVIDER and len(context) != original_len:
            logger.warning(
                f'[DEBUG_HISTORY_PROVIDER] Removed {original_len - len(context)} orphaned tool messages from context'
            )
        
        self._cached_context = context
        return context
    
    def build(self, user_history: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        """Build context from user_history with optional max_tokens limit.
        
        This method implements the ContextBuilder interface, delegating to
        the internal SummaryBuilder.
        """
        # Use passed max_tokens, fallback to instance token_limit
        limit = max_tokens if max_tokens is not None else self.token_limit
        return self.context_builder.build(user_history, max_tokens=limit)
    
    def add_message(self, message: Dict[str, Any]) -> None:
        """Append message to session.user_history."""
        session_id = self.session.session_id if self.session and hasattr(self.session, 'session_id') else 'no-id'
        print(f"[HISTORY_DEBUG] HistoryProvider.add_message called: role={message.get('role')}, session_id={session_id}, history_len={len(self.session.user_history) if self.session else 'no session'}")
        # Debug timestamp ordering
        if DEBUG_CONTEXT:
            prev_timestamp = None
            if self.session.user_history:
                prev_msg = self.session.user_history[-1]
                prev_timestamp = prev_msg.get('created_at')
                print(f"[TIMESTAMP_DEBUG] Previous message created_at: {prev_timestamp}")
            current_timestamp = message.get('created_at')
            print(f"[TIMESTAMP_DEBUG] Adding message created_at: {current_timestamp}")
            if prev_timestamp and current_timestamp:
                # Compare timestamps (assuming ISO format strings)
                if prev_timestamp > current_timestamp:
                    print(f"[TIMESTAMP_WARNING] Timestamp ordering violation! Previous {prev_timestamp} > current {current_timestamp}")
        # Ensure message has a sequence number for ordering
        if 'seq' not in message:
            message['seq'] = self.session._get_next_seq()
            if DEBUG_CONTEXT:
                print(f"[SEQ_DEBUG] Assigned seq={message['seq']} to message")
        
        self.session.user_history.append(message)
        self.session.updated_at = datetime.now()
        self._cached_context = None
        
        # Debug output - use our clean debug logging
        if DEBUG_CONTEXT and DEBUG_PRUNING_AVAILABLE:
            # Get surrounding messages for context
            history_len = len(self.session.user_history)
            surrounding_start = max(0, history_len - 3)  # Last 3 messages before insertion
            surrounding_end = history_len  # Up to current length (before new message)
            surrounding = self.session.user_history[surrounding_start:surrounding_end]
            
            log_message_insertion(message, history_len, surrounding)
            
            # Also show updated session history
            log_session_history(self.session.user_history, 'After adding message')
        elif DEBUG_CONTEXT:
            logger.debug(f'[DEBUG_CONTEXT] HistoryProvider.add_message: role={message.get("role")}, type={message.get("type", "N/A")}, tool_calls={message.get("tool_calls", "N/A")}')

        # Log the addition
        role = message.get('role', 'unknown')
        content_preview = str(message.get('content', ''))[:100]
        logger.debug(f"HistoryProvider added {role} message: {content_preview}")
    
    def clear_cache(self) -> None:
        """Explicitly clear the cached context."""
        self._cached_context = None
    
    def check_token_limit(self) -> Tuple[bool, Optional[str]]:
        """
        Check if token limit is approaching or exceeded.
        
        Returns:
            (needs_pruning, warning_message)
            - needs_pruning: True if pruning should be triggered
            - warning_message: Optional warning to display to user
        """
        if self.token_limit is None:
            return False, None
            
        # Estimate tokens in current context
        context = self.get_context_for_llm()
        token_count = self._estimate_context_tokens(context)
        
        # Log token count with our clean debug logging
        if DEBUG_PRUNING_AVAILABLE:
            log_token_count('HistoryProvider.check_token_limit', token_count, 
                          f'{token_count}/{self.token_limit} ({token_count/self.token_limit*100:.1f}%)')
        
        # Simple heuristic: warn at 80% of limit, prune at 95%
        warning_threshold = self.token_limit * 0.8
        prune_threshold = self.token_limit * 0.95
        
        if token_count >= prune_threshold:
            warning = f"Token limit almost reached ({token_count}/{self.token_limit}). Pruning recommended."
            return True, warning
        elif token_count >= warning_threshold:
            warning = f"Token usage high ({token_count}/{self.token_limit}). Consider pruning."
            return False, warning
        else:
            return False, None
    
    def create_summary(self, summary_text: str, keep_recent_turns: int) -> Dict[str, Any]:
        """
        Create a summary system message with metadata and add it to history.
        
        Args:
            summary_text: The summary content
            keep_recent_turns: How many recent turns to keep after summary
            
        Returns:
            The summary message dict
        """
        summary_msg = {
            'role': 'system',
            'content': f'Summary of previous conversation: {summary_text}',
            'pruning_keep_recent_turns': keep_recent_turns,
            'pruning_insertion_idx': len(self.session.user_history),
            'timestamp': datetime.now().isoformat()
        }
        
        self.add_message(summary_msg)
        
        # Also update session.summary field for backward compatibility
        self.session.summary = summary_msg
        
        # Log summary creation with our clean debug logging
        if DEBUG_PRUNING_AVAILABLE:
            log_summary_operation('Created summary', summary_msg)
            debug_log('pruning', f'Keeping {keep_recent_turns} recent turns after summary')
            log_session_history(self.session.user_history, 'After adding summary')
        
        logger.info(f"Created summary: {summary_text[:100]}... (keeping {keep_recent_turns} recent turns)")
        return summary_msg
    
    def _estimate_context_tokens(self, context: List[Dict[str, Any]]) -> int:
        """Estimate total tokens in context."""
        # Reuse token estimation from context builder
        return sum(self.context_builder._estimate_tokens(msg) for msg in context)
    
    def _estimate_tokens(self, message: Dict[str, Any], encoder=None) -> int:
        """Estimate token count for a single message.
        
        Implements the ContextBuilder interface.
        """
        return self.context_builder._estimate_tokens(message, encoder)
    
    # Helper methods for finding summaries and main prompt
    def _find_latest_summary(self) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        Find the most recent summary message in user_history.
        
        Returns:
            (index, summary_message) or (-1, None)
        """
        for i in range(len(self.session.user_history) - 1, -1, -1):
            msg = self.session.user_history[i]
            if (msg.get('role') == 'system' and 
                'Summary of previous conversation:' in msg.get('content', '')):
                return i, msg
        return -1, None
    
    def _find_main_system_prompt(self) -> Optional[Dict[str, Any]]:
        """Find the main system prompt (first system message)."""
        for msg in self.session.user_history:
            if msg.get('role') == 'system':
                # Check if it's a summary or main prompt
                content = msg.get('content', '')
                if 'Summary of previous conversation:' not in content:
                    return msg
        return None
    
    def _group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        Group messages into conversation turns.
        
        A turn typically consists of user + assistant (optional tool calls).
        This is a simplified grouping for determining what to keep after summary.
        """
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
                    # Orphaned tool message - skip it (cannot be used without assistant)
                    tool_call_id = msg.get('tool_call_id', 'unknown')
                    logging.warning(f'[DEBUG_CONTEXT] Orphaned tool message skipped in history provider: {tool_call_id}. Previous message in turn: {current_turn[-1] if current_turn else "none"}')
                    continue
            # system messages should already be filtered out
        
        if current_turn:
            turns.append(current_turn)
            
        return turns