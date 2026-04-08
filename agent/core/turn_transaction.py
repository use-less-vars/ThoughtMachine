"""
TurnTransaction - Atomic turn execution buffer.

Provides atomic commit/rollback for a single turn consisting of:
- Assistant message (optional content, optional tool calls)
- Tool call messages (1 per tool call)
- Tool result messages (1 per tool result)

All messages are buffered and committed atomically to session.user_history
at turn completion, or rolled back on pause/interrupt.
"""

from __future__ import annotations
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from agent.logging.debug_log import debug_log
from session.models import ObservableList

logger = logging.getLogger(__name__)


class TurnTransaction:
    """Buffer for atomic turn execution."""
    
    def __init__(self, session, context_builder=None):
        """
        Initialize empty transaction.
        
        Args:
            session: Session object with user_history attribute
            context_builder: Optional HistoryProvider for cache invalidation
        """
        self.session = session
        self.context_builder = context_builder
        
        self._assistant_message: Optional[Dict[str, Any]] = None
        self._tool_calls_buffer: List[Dict[str, Any]] = []
        self._committed = False
        
        
    def add_assistant_message(self, message: Dict[str, Any]) -> None:
        """
        Add assistant message to transaction.
        
        Args:
            message: Assistant message dict with 'role': 'assistant'
                    May contain 'content' and/or 'tool_calls'
        """
        if message.get('role') != 'assistant':
            raise ValueError("Assistant message must have role='assistant'")
        
        self._assistant_message = message.copy()
        
    def add_tool_call(self, tool_call: Dict[str, Any]) -> None:
        """
        Add tool call message to transaction.
        
        Args:
            tool_call: Tool call dict with 'role': 'tool', 'tool_call_id', etc.
        """
        if tool_call.get('role') != 'tool':
            raise ValueError("Tool call must have role='tool'")
        
        self._tool_calls_buffer.append(tool_call.copy())
        
    def add_tool_result(self, tool_result: Dict[str, Any]) -> None:
        """
        Add tool result message to transaction.
        
        Args:
            tool_result: Tool result dict with 'role': 'tool', 'content', etc.
        """
        if tool_result.get('role') != 'tool':
            raise ValueError("Tool result must have role='tool'")
        
        self._tool_calls_buffer.append(tool_result.copy())
        
    def commit(self) -> List[Dict[str, Any]]:
        """
        Commit all buffered messages atomically to session.user_history.
        
        Returns:
            List of committed messages in order of addition
        """
        if self._committed:
            raise RuntimeError("Transaction already committed")
        
        if not self._assistant_message:
            raise RuntimeError("No assistant message to commit")
        
        # Build commit order: assistant message + tool calls/results
        commit_messages = []
        
        # 1. Assistant message
        commit_messages.append(self._assistant_message)
        # Debug log for Final tool calls
        tool_calls = self._assistant_message.get('tool_calls', [])
        for tc in tool_calls:
            if tc.get('name') in ('Final', 'FinalReport', 'RequestUserInteraction'):
                logger.debug(f"TurnTransaction committing {tc['name']} tool call with result in commit_messages")
                break
        
        # 2. All tool calls and results (interleaved as they were added)
        commit_messages.extend(self._tool_calls_buffer)
        
        # Atomic write to session.user_history
        if self.session:
            debug_log(f"[TurnTransaction] Extending user_history with {len(commit_messages)} messages")
            debug_log(f"[TurnTransaction] user_history type: {type(self.session.user_history).__name__}, is ObservableList: {isinstance(self.session.user_history, ObservableList)}")
            debug_log(f"[TurnTransaction] user_history id: {id(self.session.user_history)}")
            self.session.user_history.extend(commit_messages)
            self.session.updated_at = datetime.now()
        
        # Invalidate HistoryProvider cache if available
        if self.context_builder and hasattr(self.context_builder, 'clear_cache'):
            self.context_builder.clear_cache()
        
        self._committed = True
        
        logger.debug(f"TurnTransaction committed {len(commit_messages)} messages atomically")
        return commit_messages
    
    def rollback(self) -> None:
        """Discard all buffered messages without committing."""
        if self._committed:
            raise RuntimeError("Cannot rollback committed transaction")
        
        
        self._assistant_message = None
        self._tool_calls_buffer.clear()
        
        logger.debug("TurnTransaction rolled back")
    
    def get_buffer(self) -> List[Dict[str, Any]]:
        """
        Get all buffered messages in order they would be committed.
        
        Returns:
            List of message dicts
        """
        if not self._assistant_message:
            return []
        
        buffer = [self._assistant_message]
        buffer.extend(self._tool_calls_buffer)
        return buffer
    
    def is_empty(self) -> bool:
        """Check if transaction has any messages."""
        return self._assistant_message is None and len(self._tool_calls_buffer) == 0
    
    def has_assistant_message(self) -> bool:
        """Check if assistant message has been added."""
        return self._assistant_message is not None
    
    def count_messages(self) -> Tuple[int, int]:
        """
        Count messages in transaction.
        
        Returns:
            Tuple of (assistant_messages, tool_messages)
        """
        assistant_count = 1 if self._assistant_message else 0
        tool_count = len(self._tool_calls_buffer)
        return assistant_count, tool_count
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: commit on success, rollback on exception."""
        if exc_type is None:
            if not self.is_empty():
                self.commit()
        else:
            self.rollback()
        return False  # Don't suppress exceptions