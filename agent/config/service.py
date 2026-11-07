"""
Configuration management service with debounced saves and change notifications.
"""
import os
import json
import threading
from typing import Dict, Any, Optional, Callable
from datetime import datetime
from pydantic import ValidationError
from agent.logging import log
from thoughtmachine.vault import vault_root

class ConfigService:
    """
    Manages application configuration with automatic saving and change notifications.
    
    Features:
    - JSON file persistence
    - Debounced auto-save (prevents frequent disk writes)
    - Change callbacks/observers
    - Default values with validation
    """

    def __init__(self, config_path: str, default_config: Optional[Dict[str, Any]]=None):
        """
        Initialize config service.
        
        Args:
            config_path: Path to JSON config file
            default_config: Default configuration values
        """
        self.config_path = config_path
        self.default_config = default_config or {}
        self._config = self.default_config.copy()
        self._listeners = []
        self._save_timer = None
        self._save_delay = 2.0
        self._lock = threading.RLock()
        self.load()

    def load(self) -> bool:
        """
        Load configuration from file.
        
        Returns:
            True if loaded successfully, False otherwise
        """
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    loaded = json.load(f)
                with self._lock:
                    self._config = self.default_config.copy()
                    self._config.update(loaded)
                log('INFO', 'config.service', f'Loaded config from {self.config_path}')
                self._notify_listeners()
                return True
            else:
                log('INFO', 'config.service', f'Config file not found, using defaults')
                with self._lock:
                    self._config = self.default_config.copy()
                return False
        except Exception as e:
            log('ERROR', 'config.service', f'Error loading config: {e}')
            with self._lock:
                self._config = self.default_config.copy()
            return False

    def save(self, immediate: bool=False) -> bool:
        """
        Save configuration to file.
        
        Args:
            immediate: If True, save immediately; otherwise use debounced save
            
        Returns:
            True if saved (or scheduled), False on error
        """
        log('DEBUG', 'config.service_debug', f'save called: immediate={immediate}, thread={threading.get_ident()}')
        if immediate:
            result = self._do_save()
            log('DEBUG', 'config.service_debug', f'save immediate result: {result}')
            return result
        else:
            self._schedule_save()
            log('DEBUG', 'config.service_debug', f'save scheduled, thread={threading.get_ident()}')
            return True

    def _do_save(self) -> bool:
        """Perform actual file save."""
        import threading
        import time
        thread_id = threading.get_ident()
        log('DEBUG', 'config.service_debug', f'_do_save start, thread {thread_id}, lock={self._lock}')
        try:
            log('DEBUG', 'config.service_debug', f'_do_save acquiring lock, thread {thread_id}')
            with self._lock:
                log('DEBUG', 'config.service_debug', f'_do_save lock acquired, thread {thread_id}')
                config_to_save = self._config.copy()
                log('DEBUG', 'config.service_debug', f'_do_save config copied, thread {thread_id}')
            log('DEBUG', 'config.service_debug', f'_do_save lock released, thread {thread_id}')
            os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
            # Atomic write via temp file + rename to prevent partial writes
            tmp_path = self.config_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(config_to_save, f, indent=2)
            os.replace(tmp_path, self.config_path)
            log('INFO', 'config.service', f'Saved config to {self.config_path}')
            log('DEBUG', 'config.service_debug', f'_do_save completed, thread {thread_id}')
            return True
        except Exception as e:
            log('ERROR', 'config.service', f'Error saving config: {e}')
            log('ERROR', 'config.service_debug', f'_do_save error, thread {thread_id}: {e}')
            return False

    def _schedule_save(self):
        """Schedule a debounced save."""
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(self._save_delay, self._do_save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def get(self, key: str, default: Any=None) -> Any:
        """
        Get configuration value.
        
        Args:
            key: Configuration key
            default: Default value if key doesn't exist
            
        Returns:
            Configuration value or default
        """
        with self._lock:
            return self._config.get(key, default)

    def set(self, key: str, value: Any, notify: bool=True, save: bool=True, validate: bool=True) -> None:
        """
        Set configuration value.
        
        Args:
            key: Configuration key
            value: New value
            notify: Whether to notify listeners
            save: Whether to trigger save (debounced)
            validate: Whether to validate against schema before setting

        Raises:
            pydantic.ValidationError: If ``validate`` is True and the resulting
                configuration would not validate against ``AgentConfig``.
        """
        with self._lock:
            old_value = self._config.get(key)
            if old_value != value:
                if validate:
                    # Validate the *candidate* mapping before touching live state:
                    # a rejected value is therefore never applied, never notified
                    # and never persisted - the mutation is atomic w.r.t. validation.
                    self._validate_candidate({key: value})
                self._config[key] = value
                if notify:
                    self._notify_listeners(key, old_value, value)
                if save:
                    self.save()

    def update(self, updates: Dict[str, Any], notify: bool=True, save: bool=True, validate: bool=True) -> None:
        """
        Update multiple configuration values.
        
        Args:
            updates: Dictionary of key-value updates
            notify: Whether to notify listeners
            save: Whether to trigger save (debounced)
            validate: Whether to validate against schema before updating

        Raises:
            pydantic.ValidationError: If ``validate`` is True and the resulting
                configuration would not validate against ``AgentConfig``.
        """
        changed = False
        changes = {}
        with self._lock:
            if validate:
                pending = {
                    key: value
                    for key, value in updates.items()
                    if self._config.get(key) != value
                }
                if pending:
                    # Same atomicity guarantee as ``set()``: validate the whole
                    # candidate mapping first, apply and notify only on success.
                    self._validate_candidate(pending)
            for key, value in updates.items():
                old_value = self._config.get(key)
                if old_value != value:
                    self._config[key] = value
                    changes[key] = (old_value, value)
                    changed = True
        if changed:
            if notify:
                for key, (old_val, new_val) in changes.items():
                    self._notify_listeners(key, old_val, new_val)
            if save:
                self.save()

    def get_all(self) -> Dict[str, Any]:
        """
        Get all configuration values.
        
        Returns:
            Copy of current configuration
        """
        with self._lock:
            return self._config.copy()

    def add_listener(self, callback: Callable[[str, Any, Any], None]) -> None:
        """
        Add a configuration change listener.
        
        Args:
            callback: Function called when config changes
                Signature: callback(key: str, old_value: Any, new_value: Any)
        """
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, Any, Any], None]) -> None:
        """Remove a configuration change listener."""
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _notify_listeners(self, key: Optional[str]=None, old_value: Any=None, new_value: Any=None):
        """Notify all listeners of configuration changes."""
        if key is None:
            for listener in self._listeners:
                try:
                    listener(None, None, None)
                except Exception as e:
                    log('ERROR', 'config.service', f'Error in listener: {e}')
        else:
            for listener in self._listeners:
                try:
                    listener(key, old_value, new_value)
                except Exception as e:
                    log('ERROR', 'config.service', f'Error in listener: {e}')

    def _validate_candidate(self, updates: Dict[str, Any]) -> bool:
        """
        Validate the config that *would* result from applying ``updates``.

        Never mutates ``self._config`` - callers use this to reject a write
        before it reaches live state.

        Raises:
            pydantic.ValidationError: If the candidate does not validate.
        """
        with self._lock:
            candidate = self._config.copy()
        candidate.update(updates)
        return self._validate_mapping(candidate)

    @staticmethod
    def _validate_mapping(config: Dict[str, Any]) -> bool:
        """Validate a plain config mapping against the ``AgentConfig`` schema."""
        from agent.config import AgentConfig
        try:
            AgentConfig.model_validate(config)
        except ValidationError as exc:
            # Loud, never a silent False: the caller must see invalid config.
            log('ERROR', 'config.service', f'Config validation failed: {exc}')
            raise
        return True

    def validate(self, schema: Optional[Dict[str, Any]]=None, strict: bool=True) -> bool:
        """
        Validate the live configuration against the ``AgentConfig`` schema.

        Args:
            schema: Ignored (kept for compatibility)
            strict: Ignored (kept for compatibility)

        Returns:
            True when the configuration validates.

        Raises:
            pydantic.ValidationError: If the configuration is invalid. Failures
                are raised (after logging), never reported as a silent False.
        """
        return self._validate_mapping(self.get_all())

    def reset_to_defaults(self) -> None:
        """Reset configuration to defaults."""
        with self._lock:
            self._config = self.default_config.copy()
        self._notify_listeners()
        self.save(immediate=True)

    def __enter__(self):
        """Context manager support."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Ensure pending saves are executed."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._do_save()

    def __del__(self):
        """Destructor - ensure pending saves are executed."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            try:
                self._do_save()
            except:
                pass

def create_agent_config_service(config_path: str = None) -> ConfigService:
    """
    Create a ConfigService with default agent configuration.
    
    Uses the full AgentConfig schema as the source of defaults.
    Legacy field name mappings (warning_threshold, critical_threshold,
    tool_output_limit) are handled by loaders/state_bridge for
    backward compatibility.
    
    Args:
        config_path: Path to config file
        
    Returns:
        ConfigService instance with agent defaults
    """
    if config_path is None:
        config_path = str(vault_root() / 'agent_config.json')

    from agent.config import AgentConfig
    default_agent_config = AgentConfig()
    default_config = default_agent_config.model_dump(exclude={'api_key'}, exclude_none=True)
    return ConfigService(config_path, default_config)