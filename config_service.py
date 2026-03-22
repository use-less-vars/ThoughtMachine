# config_service.py
"""
Configuration management service with debounced saves and change notifications.
"""

import os
import json
import threading
from typing import Dict, Any, Optional, Callable
from datetime import datetime


class ConfigService:
    """
    Manages application configuration with automatic saving and change notifications.
    
    Features:
    - JSON file persistence
    - Debounced auto-save (prevents frequent disk writes)
    - Change callbacks/observers
    - Default values with validation
    """
    
    def __init__(self, config_path: str, default_config: Optional[Dict[str, Any]] = None, 
                 schema: Optional[Dict[str, Any]] = None):
        """
        Initialize config service.
        
        Args:
            config_path: Path to JSON config file
            default_config: Default configuration values
            schema: Validation schema for configuration keys
        """
        self.config_path = config_path
        self.default_config = default_config or {}
        self.schema = schema or {}
        self._config = self.default_config.copy()
        self._listeners = []
        self._save_timer = None
        self._save_delay = 2.0  # seconds
        self._lock = threading.Lock()
        
        # Validate default config against schema
        if self.schema:
            self._validate_config(self.default_config, "default configuration", strict=True)
        
        # Load existing config or create with defaults
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
                
                # Validate loaded config before applying
                if self.schema:
                    test_config = self.default_config.copy()
                    test_config.update(loaded)
                    if not self._validate_config(test_config, "loaded configuration", strict=False):
                        print(f"[ConfigService] Loaded config failed validation, using defaults")
                        with self._lock:
                            self._config = self.default_config.copy()
                        return False
                
                with self._lock:
                    # Merge loaded config with defaults
                    self._config = self.default_config.copy()
                    self._config.update(loaded)
                
                print(f"[ConfigService] Loaded config from {self.config_path}")
                self._notify_listeners()
                return True
            else:
                print(f"[ConfigService] Config file not found, using defaults")
                with self._lock:
                    self._config = self.default_config.copy()
                return False
                
        except Exception as e:
            print(f"[ConfigService] Error loading config: {e}")
            with self._lock:
                self._config = self.default_config.copy()
            return False
    
    def save(self, immediate: bool = False) -> bool:
        """
        Save configuration to file.
        
        Args:
            immediate: If True, save immediately; otherwise use debounced save
            
        Returns:
            True if saved (or scheduled), False on error
        """
        if immediate:
            return self._do_save()
        else:
            self._schedule_save()
            return True
    
    def _do_save(self) -> bool:
        """Perform actual file save."""
        try:
            with self._lock:
                config_to_save = self._config.copy()
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
            
            with open(self.config_path, 'w') as f:
                json.dump(config_to_save, f, indent=2)
            
            print(f"[ConfigService] Saved config to {self.config_path}")
            return True
            
        except Exception as e:
            print(f"[ConfigService] Error saving config: {e}")
            return False
    
    def _schedule_save(self):
        """Schedule a debounced save."""
        if self._save_timer is not None:
            self._save_timer.cancel()
        
        self._save_timer = threading.Timer(self._save_delay, self._do_save)
        self._save_timer.daemon = True
        self._save_timer.start()
    
    def get(self, key: str, default: Any = None) -> Any:
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
    
    def set(self, key: str, value: Any, notify: bool = True, save: bool = True, validate: bool = True) -> None:
        """
        Set configuration value.
        
        Args:
            key: Configuration key
            value: New value
            notify: Whether to notify listeners
            save: Whether to trigger save (debounced)
            validate: Whether to validate against schema before setting
        """
        # Validate before setting if schema exists
        if validate and self.schema:
            test_config = self.get_all().copy()
            test_config[key] = value
            if not self._validate_config(test_config, f"set operation for key '{key}'", strict=True):
                print(f"[ConfigService] Validation failed for key '{key}', value not set")
                return
        
        with self._lock:
            old_value = self._config.get(key)
            if old_value != value:
                self._config[key] = value
                
                if notify:
                    self._notify_listeners(key, old_value, value)
                
                if save:
                    self.save()
    
    def update(self, updates: Dict[str, Any], notify: bool = True, save: bool = True, validate: bool = True) -> None:
        """
        Update multiple configuration values.
        
        Args:
            updates: Dictionary of key-value updates
            notify: Whether to notify listeners
            save: Whether to trigger save (debounced)
            validate: Whether to validate against schema before updating
        """
        # Validate all updates before applying if schema exists
        if validate and self.schema:
            test_config = self.get_all().copy()
            test_config.update(updates)
            if not self._validate_config(test_config, "bulk update operation", strict=True):
                print("[ConfigService] Validation failed for bulk update, no changes applied")
                return
        
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
    
    def _notify_listeners(self, key: Optional[str] = None, old_value: Any = None, new_value: Any = None):
        """Notify all listeners of configuration changes."""
        if key is None:
            # Full config reload
            for listener in self._listeners:
                try:
                    listener(None, None, None)
                except Exception as e:
                    print(f"[ConfigService] Error in listener: {e}")
        else:
            # Specific key change
            for listener in self._listeners:
                try:
                    listener(key, old_value, new_value)
                except Exception as e:
                    print(f"[ConfigService] Error in listener: {e}")
    
    def validate(self, schema: Optional[Dict[str, Any]] = None, strict: bool = True) -> bool:
        """
        Validate configuration against schema.
        
        Args:
            schema: Validation schema (optional, basic type checking if not provided)
            strict: If True, unknown keys cause validation failure; if False, unknown keys are ignored
            
        Returns:
            True if valid, False otherwise
        """
        return self._validate_config(self._config, "current configuration", schema, strict)
    
    def _validate_config(self, config: Dict[str, Any], context: str = "configuration", 
                         schema: Optional[Dict[str, Any]] = None, strict: bool = True) -> bool:
        """
        Validate configuration against schema.

        Args:
            config: Configuration dictionary to validate
            context: Description of what's being validated (for error messages)
            schema: Validation schema (optional, uses self.schema if not provided)
            strict: If True, unknown keys cause validation failure; if False, unknown keys are ignored

        Returns:
            True if valid, False otherwise
        """
        validation_schema = schema or self.schema
        if not validation_schema:
            return True
        
        errors = []
        
        for key, rules in validation_schema.items():
            if key not in config:
                if not rules.get('optional', False):
                    errors.append(f"Required key '{key}' missing")
                continue
            
            value = config[key]
            # Skip validation if nullable and value is None
            if rules.get('nullable', False) and value is None:
                continue
            
            # Type validation
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
            
            # Range validation for numbers
            if isinstance(value, (int, float)):
                if 'min' in rules and value < rules['min']:
                    errors.append(f"Key '{key}' must be >= {rules['min']}, got {value}")
                if 'max' in rules and value > rules['max']:
                    errors.append(f"Key '{key}' must be <= {rules['max']}, got {value}")
            
            # Choices validation
            if 'choices' in rules and value not in rules['choices']:
                errors.append(f"Key '{key}' must be one of {rules['choices']}, got {value}")
        
        # Check for unknown keys
        if strict:
            for key in config.keys():
                if key not in validation_schema:
                    errors.append(f"Unknown configuration key '{key}'")
        
        if errors:
            print(f"[ConfigService] Validation errors in {context}:\n" + "\n".join(errors))
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


# Factory function for agent configuration
def create_agent_config_service(config_path: str = "agent_config.json") -> ConfigService:
    """
    Create a ConfigService with default agent configuration.
    
    Args:
        config_path: Path to config file
        
    Returns:
        ConfigService instance with agent defaults
    """
    from tools import SIMPLIFIED_TOOL_CLASSES
    from agent_core import AgentConfig

    # Create default AgentConfig instance
    default_agent_config = AgentConfig()
    # Convert to dict for ConfigService
    default_config = default_agent_config.model_dump(exclude_none=True)
    # Ensure backward compatibility: add warning_threshold and critical_threshold (in thousands)
    default_config["warning_threshold"] = default_agent_config.token_monitor_warning_threshold // 1000
    default_config["critical_threshold"] = default_agent_config.token_monitor_critical_threshold // 1000
    # tool_output_limit alias
    default_config["tool_output_limit"] = default_agent_config.tool_output_token_limit
    # Remove fields that are not needed for UI/config storage
    # (ConfigService will ignore extra keys, but we filter to reduce warnings)
    fields_to_remove = [
        "initial_input_tokens", "initial_output_tokens",
        "enable_logging", "log_dir", "log_level", "enable_file_logging",
        "enable_console_logging", "jsonl_format", "max_file_size_mb", "max_backup_files",
        "tool_output_token_limit",  # replaced by tool_output_limit alias
    ]
    for key in fields_to_remove:
        default_config.pop(key, None)

    # Schema for validation (simplified, using AgentConfig validation primarily)
    schema = {
        "temperature": {"type": "float", "min": 0.0, "max": 2.0},
        "max_turns": {"type": "int", "min": 1, "max": 500},
        "token_monitor_enabled": {"type": "bool"},
        "warning_threshold": {"type": "int", "min": 1, "max": 200},
        "critical_threshold": {"type": "int", "min": 1, "max": 200},
        "workspace_path": {"type": "str", "nullable": True, "optional": True},
        "tool_output_limit": {"type": "int", "min": 1000, "max": 100000},
        "tool_output_token_limit": {"type": "int", "min": 1000, "max": 100000, "optional": True},
        "provider_type": {"type": "str", "choices": ["openai_compatible", "anthropic", "openai"]},
        "api_key": {"type": "str", "optional": True},
        "base_url": {"type": "str", "optional": True},
        "model": {"type": "str"},  # Allow any model name
        "detail": {"type": "str", "choices": ["minimal", "normal", "verbose"]},
        "enabled_tools": {"type": "list"},
        "provider_config": {"type": "dict", "optional": True},
        # Token monitor thresholds in tokens
        "token_monitor_warning_threshold": {"type": "int", "min": 1000, "max": 200000},
        "token_monitor_critical_threshold": {"type": "int", "min": 1000, "max": 200000},
        # Turn monitoring settings (fractions of max_turns)
        "turn_monitor_enabled": {"type": "bool"},
        "turn_monitor_warning_threshold": {"type": "float", "min": 0.0, "max": 1.0},
        "turn_monitor_critical_threshold": {"type": "float", "min": 0.0, "max": 1.0},
        "critical_countdown_turns": {"type": "int", "min": 0, "max": 20, "optional": True},
        # Conversation pruning settings
        "max_history_turns": {"type": "int", "min": 0, "max": 1000, "optional": True, "nullable": True},
        "keep_initial_query": {"type": "bool"},
        "keep_system_messages": {"type": "bool"},
        "preset_name": {"type": "str", "optional": True, "nullable": True}
    }

    return ConfigService(config_path, default_config, schema)
