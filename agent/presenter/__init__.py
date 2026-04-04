"""
Presenter module for agent GUI.

Provides:
- AgentPresenter: Main presenter class for GUI integration
- StateBridge: Configuration and state management
- SessionLifecycle: Session operations
- EventProcessor: Event handling
- GUIIntegration: Qt signal handling
"""

from .state_bridge import StateBridge
from .session_lifecycle import SessionLifecycle
from .event_processor import EventProcessor
from .gui_integration import GUIIntegration
from agent.logging.debug_log import debug_log

__all__ = [
    'StateBridge',
    'SessionLifecycle', 
    'EventProcessor',
    'GUIIntegration',
]

