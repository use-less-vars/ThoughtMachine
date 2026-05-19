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
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from agent.logging import log
from session.models import ObservableList


class TurnTransaction:
    """Buffer for atomic turn execution."""

    def __init__(self, session, context_builder=None, conversation=None):
        """
        Initialize empty transaction.
        
        Args:
            session: Session object with user_history attribute
            context_builder: Optional HistoryProvider for cache invalidation
            conversation: Fallback conversation list when session is None
        """
        self.session = session
        self.context_builder = context_builder
        self.conversation = conversation
        self._assistant_message: Optional[Dict[str, Any]] = None
        self._tool_calls_buffer: List[Dict[str, Any]] = []
        self._committed = False
        self._assistant_committed: bool = False

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

    def commit_assistant_only(self) -> List[Dict[str, Any]]:
        """
        Commit just the assistant message immediately, before tool execution.
        
        The transaction remains open to buffer tool results, which can be
        committed via a subsequent commit() call. This enables the assistant's
        response to be visible in user_history before tool execution completes,
        preventing message loss on pause/interrupt.
        
        Returns:
            List containing just the committed assistant message
        """
        if self._committed:
            raise RuntimeError('Transaction already committed')
        if not self._assistant_message:
            raise RuntimeError('No assistant message to commit')
        commit_messages = [self._assistant_message]
        if self.session:
            log('DEBUG', 'core.turn_transaction', f'[TurnTransaction] Extending user_history with assistant message (immediate commit)')
            self.session.user_history.extend(commit_messages)
            self.session.updated_at = datetime.now()
        elif self.conversation is not None:
            log('DEBUG', 'core.turn_transaction', f'[TurnTransaction] Extending fallback conversation with assistant message (immediate commit)')
            self.conversation.extend(commit_messages)
        if self.context_builder and hasattr(self.context_builder, 'clear_cache'):
            self.context_builder.clear_cache()
        self._assistant_committed = True
        log('DEBUG', 'core.turn_transaction', 'TurnTransaction committed assistant message immediately (transaction still open for tool results)')
        return commit_messages

    def commit(self) -> List[Dict[str, Any]]:
        """
        Commit all buffered messages atomically to session.user_history.
        
        If commit_assistant_only() was already called, only tool results
        are committed. Otherwise, everything is committed atomically.
        
        Returns:
            List of committed messages in order of addition
        """
        if self._committed:
            raise RuntimeError('Transaction already committed')
        if self._assistant_committed:
            # Assistant already committed; only tool results remain
            commit_messages = list(self._tool_calls_buffer)
        else:
            if not self._assistant_message:
                raise RuntimeError('No assistant message to commit')
            commit_messages = []
            commit_messages.append(self._assistant_message)
            tool_calls = self._assistant_message.get('tool_calls', [])
            for tc in tool_calls:
                if tc.get('name') in ('Final', 'FinalReport', 'RequestUserInteraction'):
                    log('DEBUG', 'core.turn_transaction', f"TurnTransaction committing {tc['name']} tool call with result in commit_messages")
                    break
            commit_messages.extend(self._tool_calls_buffer)
        if commit_messages:
            if self.session:
                log('DEBUG', 'core.turn_transaction', f'[TurnTransaction] Extending user_history with {len(commit_messages)} messages')
                log('DEBUG', 'core.turn_transaction', f'[TurnTransaction] user_history type: {type(self.session.user_history).__name__}, is ObservableList: {isinstance(self.session.user_history, ObservableList)}')
                log('DEBUG', 'core.turn_transaction', f'[TurnTransaction] user_history id: {id(self.session.user_history)}')
                self.session.user_history.extend(commit_messages)
                self.session.updated_at = datetime.now()
            elif self.conversation is not None:
                log('DEBUG', 'core.turn_transaction', f'[TurnTransaction] Extending fallback conversation with {len(commit_messages)} messages (session=None)')
                self.conversation.extend(commit_messages)
        if self.context_builder and hasattr(self.context_builder, 'clear_cache'):
            self.context_builder.clear_cache()
        self._committed = True
        log('DEBUG', 'core.turn_transaction', f'TurnTransaction committed {len(commit_messages)} messages')
        return commit_messages

    def rollback(self) -> None:
        """Discard all buffered messages without committing."""
        if self._committed:
            raise RuntimeError('Cannot rollback committed transaction')
        self._assistant_message = None
        self._tool_calls_buffer.clear()
        log('DEBUG', 'core.turn_transaction', 'TurnTransaction rolled back')

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
        return (assistant_count, tool_count)

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
        return False