"""
Refactored AgentPresenter using modular components.

This is a skeleton showing the structure of the refactored AgentPresenter
that delegates to specialized modules.
"""

import os
from typing import Optional

from PyQt6.QtCore import QObject
from agent.controller import AgentController

from .state_bridge import StateBridge
from .session_lifecycle import SessionLifecycle
from .event_processor import EventProcessor
from .gui_integration import GUIIntegration


class AgentPresenter(QObject):
    """
    Refactored presenter using modular components.
    
    Delegates responsibilities to:
    - StateBridge: Configuration and state management
    - SessionLifecycle: Session operations  
    - EventProcessor: Event handling
    - GUIIntegration: Qt signal handling
    """
    
    def __init__(self, config_path: Optional[str] = None):
        super().__init__()
        
        # Initialize controller
        self.controller = AgentController()
        
        # Initialize modules
        self.state_bridge = StateBridge(config_path or "agent_config.json")
        self.gui_integration = GUIIntegration()
        self.session_lifecycle = SessionLifecycle(
            state_bridge=self.state_bridge,
            controller=self.controller
        )
        self.event_processor = EventProcessor(
            state_bridge=self.state_bridge,
            session_lifecycle=self.session_lifecycle,
            gui_integration=self.gui_integration
        )
        
        # Connect controller events to processor
        self.controller.event_occurred.connect(self.event_processor.process_event)
        
        # Forward GUI signals from integration module
        self._connect_gui_signals()
        
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[AgentPresenter] Refactored version initialized")
    
    def _connect_gui_signals(self):
        """Connect GUI integration signals to local signals."""
        # Forward all signals from gui_integration
        self.gui_integration.state_changed.connect(self.state_changed)
        self.gui_integration.event_received.connect(self.event_received)
        self.gui_integration.tokens_updated.connect(self.tokens_updated)
        self.gui_integration.context_updated.connect(self.context_updated)
        self.gui_integration.status_message.connect(self.status_message)
        self.gui_integration.error_occurred.connect(self.error_occurred)
        self.gui_integration.config_changed.connect(self.config_changed)
        self.gui_integration.conversation_changed.connect(self.conversation_changed)
    
    # Public API methods will delegate to modules
    # These will be implemented during the extraction phase
    
    def start_session(self, query: str, config: Optional[dict] = None, preset_name: str = None):
        """Start a new agent session."""
        # Will delegate to session_lifecycle
        pass
    
    def restart_session(self, query: str = None):
        """Restart a fresh session with current configuration."""
        # Will delegate to session_lifecycle
        pass
    
    # ... other public methods
    
    # Property accessors
    @property
    def state(self):
        return self.gui_integration.state
    
    @state.setter
    def state(self, new_state):
        self.gui_integration.state = new_state
    
    @property
    def total_input(self):
        return self.state_bridge.total_input
    
    @property 
    def total_output(self):
        return self.state_bridge.total_output
    
    @property
    def context_length(self):
        return self.state_bridge.context_length
    
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

    # Signal definitions (same as original)    state_changed = GUIIntegration.state_changed
    event_received = GUIIntegration.event_received
    tokens_updated = GUIIntegration.tokens_updated
    context_updated = GUIIntegration.context_updated
    status_message = GUIIntegration.status_message
    error_occurred = GUIIntegration.error_occurred
    config_changed = GUIIntegration.config_changed
    conversation_changed = GUIIntegration.conversation_changed

