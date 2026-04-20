"""Config bridge for GUI configuration management."""
from PyQt6.QtCore import QTimer
from agent.logging import log
from typing import Callable, List, Optional, Dict, Any

class GUIConfigBridge:
    """Adapter between GUI and ConfigService.
    
    Provides debounced saving and change notification.
    """

    def __init__(self, config_service):
        """Initialize with a ConfigService instance."""
        self.config_service = config_service
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._pending_save_timer = QTimer()
        self._pending_save_timer.setSingleShot(True)
        self._pending_save_timer.timeout.connect(self._perform_save)
        self._pending_config: Optional[Dict[str, Any]] = None

    def save_config(self, config_dict: Dict[str, Any], immediate: bool=False) -> None:
        """Save configuration.

        Args:
            config_dict: Configuration dictionary to save.
            immediate: If True, save immediately; otherwise debounced (1s).
        """
        import threading
        log('DEBUG', 'debug.unknown', f'[GUIConfigBridge DEBUG] save_config: immediate={immediate}, thread={threading.get_ident()}')
        self.config_service.update(config_dict, notify=True, save=False)
        if immediate:
            self._perform_save()
        else:
            self._pending_config = config_dict
            self._pending_save_timer.start(1000)

    def _perform_save(self) -> None:
        """Actually save to the config service."""
        import threading
        log('DEBUG', 'debug.unknown', f'[GUIConfigBridge DEBUG] _perform_save start, thread={threading.get_ident()}')
        self.config_service.save(immediate=True)
        self._pending_config = None
        try:
            current = self.config_service.get_all()
            log('DEBUG', 'debug.unknown', f'[GUIConfigBridge DEBUG] _perform_save notifying listeners, thread={threading.get_ident()}')
            self._notify_listeners(current)
        except Exception as e:
            log('DEBUG', 'debug.unknown', f'[GUIConfigBridge] Error getting config after save: {e}')
        log('DEBUG', 'debug.unknown', f'[GUIConfigBridge DEBUG] _perform_save done, thread={threading.get_ident()}')

    def add_change_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Add a listener for configuration changes.
        
        Listeners are called with the current config dictionary after a save.
        Duplicate callbacks are ignored.
        """
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_change_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Remove a listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify_listeners(self, config: Dict[str, Any]) -> None:
        """Notify all listeners with the current config."""
        for listener in self._listeners:
            try:
                listener(config)
            except Exception as e:
                log('DEBUG', 'debug.unknown', f'[GUIConfigBridge] Error in listener: {e}')

    def get_config(self) -> Dict[str, Any]:
        """Get current configuration from service."""
        return self.config_service.get_all()