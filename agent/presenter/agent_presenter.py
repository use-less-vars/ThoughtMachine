"""
Refactored AgentPresenter using modular architecture.

This version delegates to separate modules:
- StateBridge: Configuration and session state management
- GUIIntegration: Qt signal handling and GUI interaction  
- SessionLifecycle: Session start/stop/pause/save/load operations
- EventProcessor: Event processing and state updates
"""

import os
from typing import Optional, Dict, Any, List
from datetime import datetime
from agent.logging.debug_log import debug_log

from PyQt6.QtCore import QObject, pyqtSignal
from agent.controller import AgentController
from agent.config import AgentConfig, load_default_config
from tools import SIMPLIFIED_TOOL_CLASSES
from agent.core.state import ExecutionState
from session.models import Session, SessionConfig, RuntimeParams
from session.store import FileSystemSessionStore
from session.context_builder import LastNBuilder

from .state_bridge import StateBridge
from .gui_integration import GUIIntegration
from .session_lifecycle import SessionLifecycle
from .event_processor import EventProcessor


class RefactoredAgentPresenter(QObject):
    """
    Refactored presenter using modular architecture.
    
    Delegates responsibilities to specialized modules while maintaining
    the same public API as the original AgentPresenter.
    """
    
    # Signals (same as original)
    state_changed = pyqtSignal(ExecutionState)
    event_received = pyqtSignal(dict)
    tokens_updated = pyqtSignal(int, int)
    context_updated = pyqtSignal(int)
    status_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)
    config_changed = pyqtSignal(dict)
    conversation_changed = pyqtSignal()

    def __init__(self):
        """Initialize refactored presenter with modular architecture."""
        super().__init__()
        
        # Create core modules
        self.state_bridge = StateBridge()
        self.gui_integration = GUIIntegration()
        self.controller = AgentController()
        self._final_content = None
        self._final_reasoning = None
        
        # Create lifecycle and event processor with dependencies
        self.session_lifecycle = SessionLifecycle(self.state_bridge, self.controller)
        # Set up state change callback
        self.session_lifecycle._session_callback = self._on_session_state_change
        # Set up conversation change callback
        debug_log(f"Setting conversation callback", level="DEBUG", component="AgentPresenter")
        self.session_lifecycle._conversation_callback = lambda: self.gui_integration.emit_conversation_changed()
        debug_log(f"Conversation callback set", level="DEBUG", component="AgentPresenter")

        self.event_processor = EventProcessor(
            self.state_bridge, 
            self.session_lifecycle, 
            self.gui_integration
        )
        
        # Connect GUI integration signals to our signals
        self._connect_signals()
        
        
        
        debug_log(f"Initialized with modular architecture", level="DEBUG", component="AgentPresenter")

    def _connect_signals(self):
        """Connect module signals to presenter signals."""
        # Connect GUIIntegration signals to our signals
        self.gui_integration.state_changed.connect(self.state_changed)
        self.gui_integration.event_received.connect(self.event_received)
        self.gui_integration.tokens_updated.connect(self.tokens_updated)
        self.gui_integration.context_updated.connect(self.context_updated)
        self.gui_integration.status_message.connect(self.status_message)
        self.gui_integration.error_occurred.connect(self.error_occurred)
        self.gui_integration.config_changed.connect(self.config_changed)
        self.gui_integration.conversation_changed.connect(self.conversation_changed)
        # Connect controller events
        self.controller.event_occurred.connect(self._handle_controller_event)
        
        # Connect session lifecycle state changes
        # (StateBridge manages session state internally)

    @property
    def state(self) -> ExecutionState:
        """Current agent state (delegates to session lifecycle)."""
        return self.session_lifecycle.state

    def _on_session_state_change(self, old_state: ExecutionState, new_state: ExecutionState):
        """Callback when session lifecycle state changes."""
        # Update GUI integration state to emit signal
        if self.gui_integration.state != new_state:
            self.gui_integration.state = new_state

    def _handle_controller_event(self, event: Dict[str, Any]):
        """Forward controller events to event processor."""
        self.event_processor.process_event(event)

    # ----- Public API (delegates to modules) -----
    
    # Configuration methods
    def load_config(self, path: str) -> Optional[Dict[str, Any]]:
        """Load configuration from file path."""
        return self.state_bridge.load_config(path)
    
    def save_config(self, path: str, config: Dict[str, Any]) -> bool:
        """Save configuration to file path."""
        return self.state_bridge.save_config(path, config)
    

    
    def update_config_from_gui(self, config_dict: Dict[str, Any]):
        """Update configuration from GUI dictionary."""
        updated_config = self.state_bridge.update_config(config_dict)
        self.gui_integration.emit_config_changed(updated_config)
    
    def update_config(self, config_updates: dict):
        """Update configuration with partial updates (uses loader.update_config)."""
        self.update_config_from_gui(config_updates)
    
    def get_available_tools(self) -> List[str]:
        """Get list of available tool names."""
        return self.state_bridge.get_available_tools()
    
    def get_tool_schema(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Get schema for a specific tool."""
        return self.state_bridge.get_tool_schema(tool_name)
    
    # Session management
    def start_session(self, query: str, config: Optional[dict] = None, preset_name: str = None):
        """Start a new agent session."""
        self.session_lifecycle.start_session(query, config, preset_name)
    
    def new_session(self, name: str = None):
        """Start a brand new session."""
        self.session_lifecycle.new_session(name)
    
    def continue_session(self, query: str):
        """Continue an existing session with a new query."""
        self.session_lifecycle.continue_session(query)
    
    def pause_session(self):
        """Request pause of current session."""
        self.session_lifecycle.pause_session()
    
    def restart_session(self, query: str = None):
        """Restart a fresh session with current configuration."""
        self.session_lifecycle.restart_session(query)
    
    def save_session(self) -> bool:
        """Save current session to the session store."""
        return self.session_lifecycle.save_session()
    
    def load_session(self, filepath: str) -> bool:
        """Load a session from a JSON file.

        Args:
            filepath: Path to the session file

        Returns:
            True if loaded successfully, False otherwise
        """
        return self.session_lifecycle.load_session(filepath)
    
    def load_session_by_id(self, session_id: str) -> bool:
        """Load a session by ID from the session store."""
        return self.session_lifecycle.load_session_by_id(session_id)
    
    def load_current_session(self) -> bool:
        """Load the session marked as current from the store."""
        # This uses session store directly, could be added to SessionLifecycle
        # For now, implement directly
        session_id = self.session_lifecycle.session_store.get_current_session_id()
        if not session_id:
            return False
        return self.load_session_by_id(session_id)
    
    def export_session(self, filepath: str, set_as_external: bool = False) -> bool:
        """Export current session to a specified file path.

        Args:
            filepath: Path to export the session JSON file
            set_as_external: If True, set this file as the external file path
                for future auto-saves. Default False.
        """
        return self.session_lifecycle.export_session(filepath, set_as_external)
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        """List available sessions from the session store."""
        return self.session_lifecycle.list_sessions()
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session from the store."""
        return self.session_lifecycle.delete_session(session_id)
    
    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session's metadata name."""
        return self.session_lifecycle.rename_session(session_id, new_name)
    
    def auto_save_current_session(self) -> bool:
        """Auto-save current session."""
        return self.session_lifecycle.auto_save_current_session()
    


    def create_agent_config(self) -> AgentConfig:
        """Create AgentConfig from current state."""
        return self.state_bridge.create_agent_config()

    def _update_external_file_path(self, filepath: str) -> None:
        """Update external file path for auto-save."""
        self.state_bridge.update_external_file_path(filepath)

    def bind_session(self, session: Session) -> None:
        """Bind a session to the presenter."""
        self.state_bridge.bind_session(session)

    def _build_session_config(self, agent_config: AgentConfig) -> SessionConfig:
        """Build SessionConfig from AgentConfig."""
        return self.state_bridge.build_session_config(agent_config)

    # State properties (delegated to StateBridge)
    @property
    def state(self) -> ExecutionState:
        """Current execution state."""
        return self.session_lifecycle.state
    
    @property 
    def total_input(self) -> int:
        """Total input tokens for current session."""
        return self.state_bridge.total_input
    
    @property
    def total_output(self) -> int:
        """Total output tokens for current session."""
        return self.state_bridge.total_output
    
    @property
    def context_length(self) -> int:
        """Current conversation context length in tokens."""
        return self.state_bridge.context_length
    
    @property
    def current_session_id(self) -> Optional[str]:
        """Current session ID."""
        return self.state_bridge.current_session_id
    
    @property
    def current_session(self) -> Optional[Session]:
        """Current session object."""
        return self.state_bridge.current_session
    
    @property
    def session_name(self) -> Optional[str]:
        """Current session name."""
        return self.state_bridge.session_name
    
    @session_name.setter
    def session_name(self, name: str):
        """Set session name."""
        self.state_bridge.session_name = name
    
    @property
    def config(self) -> dict:
        """Return current configuration dictionary."""
        return self.state_bridge.get_config()
    
    @property  
    def _config(self) -> dict:
        """Backward compatibility for GUI accessing private attribute."""
        return self.state_bridge.get_config()
    
    @property
    def user_history(self) -> List[Dict[str, Any]]:
        """User conversation history."""
        return self.state_bridge.user_history
    
    @property
    def _initial_conversation(self) -> Optional[List[Dict[str, Any]]]:
        """Initial conversation for session loading (backward compatibility)."""
        return self.session_lifecycle._initial_conversation

    def get_conversation_snapshot(self) -> Optional[List[Dict[str, Any]]]:
        """Get current conversation snapshot from session.
        
        Returns:
            List of conversation messages or None if no session.
        """
        session = self.current_session
        if session:
            return session.get_conversation_snapshot()
        return None

    @property
    def session_store(self):
        """Session store for GUI access."""
        return self.session_lifecycle.session_store

    @session_store.setter
    def session_store(self, store):
        """Allow GUI to inject a session store (e.g., for testing)."""
        self.session_lifecycle.session_store = store

    @property
    def final_content(self) -> Optional[str]:
        """Final content from last agent response."""
        if self.current_session and self.current_session.final_content:
            return self.current_session.final_content
        return self._final_content

    @final_content.setter
    def final_content(self, content: Optional[str]):
        """Set final content."""
        self._final_content = content
        if self.current_session:
            self.current_session.final_content = content
    

    @property
    def final_reasoning(self) -> Optional[str]:
        """Final reasoning from last agent response."""
        if self.current_session and self.current_session.final_reasoning:
            return self.current_session.final_reasoning
        return self._final_reasoning

    @final_reasoning.setter
    def final_reasoning(self, reasoning: Optional[str]):
        """Set final reasoning."""
        self._final_reasoning = reasoning
        if self.current_session:
            self.current_session.final_reasoning = reasoning

    # Other methods

    

    
    def request_stop(self):
        """Request stop of current session."""
        if self.controller.is_running:
            self.controller.stop()
            self.gui_integration.emit_status_message("Stopping...")
    
    # GUI state methods
    def update_gui_state(self, state_dict: Dict[str, Any]):
        """Update GUI state from dictionary."""
        self.gui_integration.update_gui_state(state_dict)
    
    def get_gui_state(self) -> Dict[str, Any]:
        """Get current GUI state as dictionary."""
        return self.gui_integration.get_gui_state()
    
    def cleanup(self):
        """Clean up resources before closing."""
        # Stop controller if running
        if self.controller.is_running:
            self.controller.stop()
        # Clean up any dead thread state
        if hasattr(self.controller, '_cleanup_if_thread_dead'):
            self.controller._cleanup_if_thread_dead()
        # Disconnect signals to prevent memory leaks
        try:
            self.gui_integration.state_changed.disconnect()
            self.gui_integration.event_received.disconnect()
            self.gui_integration.tokens_updated.disconnect()
            self.gui_integration.context_updated.disconnect()
            self.gui_integration.status_message.disconnect()
            self.gui_integration.error_occurred.disconnect()
            self.gui_integration.config_changed.disconnect()
            self.gui_integration.conversation_changed.disconnect()
            self.controller.event_occurred.disconnect()
        except Exception:
            # Signals may already be disconnected or not connected
            pass