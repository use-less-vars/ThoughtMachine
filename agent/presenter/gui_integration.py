"""
GUIIntegration: Qt signal handling and GUI state management.

Provides Qt signals for GUI communication and handles state updates.
Separates Qt dependencies from business logic.
"""

from typing import Optional, Dict, Any
from agent.logging.debug_log import debug_log

from PyQt6.QtCore import QObject, pyqtSignal
from agent.core.state import ExecutionState


class GUIIntegration(QObject):
    """
    Handles Qt signals for GUI communication.
    
    Signals:
        state_changed(state: ExecutionState): Emitted when agent state changes
        event_received(event: dict): Emitted when a new event arrives from controller
        tokens_updated(total_input: int, total_output: int): Emitted when token counts update
        status_message(message: str): Emitted for status updates
        context_updated(context_length: int): Emitted when context token count updates
        error_occurred(error: str, traceback: str): Emitted for errors
        config_changed(config: dict): Emitted when configuration changes
        conversation_changed(): Emitted when conversation changes
    """
    
    # Signals
    state_changed = pyqtSignal(ExecutionState)
    event_received = pyqtSignal(dict)
    tokens_updated = pyqtSignal(int, int)
    context_updated = pyqtSignal(int)
    status_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)
    config_changed = pyqtSignal(dict)
    conversation_changed = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self._state = ExecutionState.IDLE
    
    # State management
    @property
    def state(self) -> ExecutionState:
        """Current agent state."""
        return self._state
    
    @state.setter
    def state(self, new_state: ExecutionState):
        """Update state and emit signal."""
        if self._state != new_state:
            self._state = new_state
            self.state_changed.emit(new_state)
    
    # Signal emission methods
    def emit_event_received(self, event: Dict[str, Any]) -> None:
        """Emit event_received signal."""
        self.event_received.emit(event)
    
    def emit_tokens_updated(self, total_input: int, total_output: int) -> None:
        """Emit tokens_updated signal."""
        self.tokens_updated.emit(total_input, total_output)
    
    def emit_context_updated(self, context_length: int) -> None:
        """Emit context_updated signal."""
        self.context_updated.emit(context_length)
    
    def emit_status_message(self, message: str) -> None:
        """Emit status_message signal."""
        self.status_message.emit(message)
    
    def emit_error_occurred(self, error: str, traceback: str) -> None:
        """Emit error_occurred signal."""
        self.error_occurred.emit(error, traceback)
    
    def emit_config_changed(self, config: dict) -> None:
        """Emit config_changed signal."""
        self.config_changed.emit(config)
    
    def emit_conversation_changed(self) -> None:
        """Emit conversation_changed signal."""
        self.conversation_changed.emit()

