"""
Configuration management service with debounced saves and change notifications.
"""
import os
import json
import threading
from typing import Dict, Any, Optional, Callable
from datetime import datetime
from agent.logging import log

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
        self.schema = {}
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
                log('WARNING', 'config.service', f'Loaded config from {self.config_path}')
                self._notify_listeners()
                return True
            else:
                log('WARNING', 'config.service', f'Config file not found, using defaults')
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
        log('WARNING', 'config.service_debug', f'save called: immediate={immediate}, thread={threading.get_ident()}')
        if immediate:
            result = self._do_save()
            log('WARNING', 'config.service_debug', f'save immediate result: {result}')
            return result
        else:
            self._schedule_save()
            log('WARNING', 'config.service_debug', f'save scheduled, thread={threading.get_ident()}')
            return True

    def _do_save(self) -> bool:
        """Perform actual file save."""
        import threading
        import time
        thread_id = threading.get_ident()
        log('WARNING', 'config.service_debug', f'_do_save start, thread {thread_id}, lock={self._lock}')
        try:
            log('WARNING', 'config.service_debug', f'_do_save acquiring lock, thread {thread_id}')
            with self._lock:
                log('WARNING', 'config.service_debug', f'_do_save lock acquired, thread {thread_id}')
                config_to_save = self._config.copy()
                log('WARNING', 'config.service_debug', f'_do_save config copied, thread {thread_id}')
            log('WARNING', 'config.service_debug', f'_do_save lock released, thread {thread_id}')
            os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump(config_to_save, f, indent=2)
            log('WARNING', 'config.service', f'Saved config to {self.config_path}')
            log('WARNING', 'config.service_debug', f'_do_save completed, thread {thread_id}')
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
        """
        with self._lock:
            old_value = self._config.get(key)
            if old_value != value:
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
        """
        changed = False
        changes = {}
        with self._lock:
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

    def validate(self, schema: Optional[Dict[str, Any]]=None, strict: bool=True) -> bool:
        """
        Validate configuration (no-op - validation handled by AgentConfig).
        
        Args:
            schema: Ignored (kept for compatibility)
            strict: Ignored (kept for compatibility)
            
        Returns:
            Always True
        """
        return True

    def _validate_config(self, config: Dict[str, Any], context: str='configuration', schema: Optional[Dict[str, Any]]=None, strict: bool=True) -> bool:
        """
        Validate configuration against schema.

        Args:
            config: Configuration dictionary to validate
            context: Description of what's being validated (for error messages)
            schema: Validation schema (ignored - validation handled by AgentConfig)
            strict: If True, unknown keys cause validation failure; if False, unknown keys are ignored

        Returns:
            True if valid, False otherwise
        """
        validation_schema = schema or self.schema
        if not validation_schema or validation_schema == {}:
            return True
        errors = []
        for key, rules in validation_schema.items():
            if key not in config:
                if not rules.get('optional', False):
                    errors.append(f"Required key '{key}' missing")
                continue
            value = config[key]
            if rules.get('nullable', False) and value is None:
                continue
            if 'type' in rules:
                expected_type = rules['type']
                if expected_type == 'int':
                    if not isinstance(value, int):
                        errors.append(f"Key '{key}' must be int, got {type(value).__name__}")
                elif expected_type == 'float':
                    if not isinstance(value, (int, float)):
                        errors.append(f"Key '{key}' must be float, got {type(value).__name__}")
                elif expected_type == 'str':
                    if not isinstance(value, str):
                        errors.append(f"Key '{key}' must be str, got {type(value).__name__}")
                elif expected_type == 'bool':
                    if not isinstance(value, bool):
                        errors.append(f"Key '{key}' must be bool, got {type(value).__name__}")
                elif expected_type == 'list':
                    if not isinstance(value, list):
                        errors.append(f"Key '{key}' must be list, got {type(value).__name__}")
                elif expected_type == 'none':
                    if value is not None:
                        errors.append(f"Key '{key}' must be None, got {type(value).__name__}")
            if isinstance(value, (int, float)):
                if 'min' in rules and value < rules['min']:
                    errors.append(f"Key '{key}' must be >= {rules['min']}, got {value}")
                if 'max' in rules and value > rules['max']:
                    errors.append(f"Key '{key}' must be <= {rules['max']}, got {value}")
            if 'choices' in rules and value not in rules['choices']:
                errors.append(f"Key '{key}' must be one of {rules['choices']}, got {value}")
        if strict:
            for key in config.keys():
                if key not in validation_schema:
                    errors.append(f"Unknown configuration key '{key}'")
        if errors:
            log('WARNING', 'config.service', f'Validation errors in {context}:\n' + '\n'.join(errors))
            return False
        return True

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

def create_agent_config_service(config_path: str='agent_config.json') -> ConfigService:
    """
    Create a ConfigService with default agent configuration.
    
    Args:
        config_path: Path to config file
        
    Returns:
        ConfigService instance with agent defaults
    """
    from tools import SIMPLIFIED_TOOL_CLASSES
    from agent.config import AgentConfig
    default_agent_config = AgentConfig()
    default_config = default_agent_config.model_dump()
    default_config['warning_threshold'] = default_agent_config.token_monitor_warning_threshold // 1000
    default_config['critical_threshold'] = default_agent_config.token_monitor_critical_threshold // 1000
    default_config['tool_output_limit'] = default_agent_config.tool_output_token_limit
    fields_to_remove = ['initial_input_tokens', 'initial_output_tokens', 'enable_logging', 'log_dir', 'log_level', 'enable_file_logging', 'enable_console_logging', 'jsonl_format', 'max_file_size_mb', 'max_backup_files', 'tool_output_token_limit']
    for key in fields_to_remove:
        default_config.pop(key, None)
    default_config['use_qml_ui'] = False
    return ConfigService(config_path, default_config)