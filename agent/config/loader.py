"""
Configuration loading utilities for the ThoughtMachine agent.
Handles loading, saving, and validation of agent configurations.
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from agent.logging import log
from .models import AgentConfig

def _map_legacy_fields(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Map legacy field names to new field names for backward compatibility."""
    mapped = config_dict.copy()
    if 'warning_threshold' in mapped:
        mapped['token_monitor_warning_threshold'] = mapped['warning_threshold'] * 1000
        del mapped['warning_threshold']
    if 'critical_threshold' in mapped:
        mapped['token_monitor_critical_threshold'] = mapped['critical_threshold'] * 1000
        del mapped['critical_threshold']
    if 'tool_output_limit' in mapped:
        mapped['tool_output_token_limit'] = mapped['tool_output_limit']
        del mapped['tool_output_limit']
    if 'chunk_size' in mapped:
        mapped['rag_chunk_size'] = mapped['chunk_size']
        del mapped['chunk_size']
    if 'chunk_overlap' in mapped:
        mapped['rag_chunk_overlap'] = mapped['chunk_overlap']
        del mapped['chunk_overlap']
    if 'embedding_model' in mapped:
        mapped['rag_embedding_model'] = mapped['embedding_model']
        del mapped['embedding_model']
    return mapped

def load_default_config() -> Dict[str, Any]:
    """Return default configuration dictionary.
    
    This matches the defaults from agent_presenter._load_default_config()
    but uses AgentConfig for validation.
    """
    config = AgentConfig()
    config_dict = config.dict()
    return config_dict

def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from file and merge with defaults.
    
    Args:
        config_path: Path to configuration file (JSON)
        
    Returns:
        Configuration dictionary with defaults for missing keys
    """
    default_config = load_default_config()
    if not os.path.exists(config_path):
        log('DEBUG', 'config.loader', f'Config file {config_path} not found, using defaults')
        return default_config
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            saved_config = json.load(f)
        saved_config = _map_legacy_fields(saved_config)
        merged_config = default_config.copy()
        for key, value in saved_config.items():
            if key in merged_config:
                merged_config[key] = value
            else:
                merged_config[key] = value
        log('DEBUG', 'config.loader', f'Loaded config from {config_path}')
        return merged_config
    except Exception as e:
        log('WARNING', 'config.loader', f'Error loading config from {config_path}: {e}')
        return default_config

def save_config(config: Dict[str, Any], config_path: str) -> bool:
    """Save configuration to file.
    
    Args:
        config: Configuration dictionary
        config_path: Path to save configuration file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        log('DEBUG', 'config.loader', f'Saved config to {config_path}')
        return True
    except Exception as e:
        log('ERROR', 'config.loader', f'Error saving config to {config_path}: {e}')
        return False

def validate_config(config_dict: Dict[str, Any]) -> Optional[AgentConfig]:
    """Validate configuration dictionary and return AgentConfig instance.
    
    Args:
        config_dict: Configuration dictionary
        
    Returns:
        AgentConfig instance if valid, None otherwise
    """
    try:
        return AgentConfig(**config_dict)
    except Exception as e:
        log('ERROR', 'config.loader', f'Configuration validation failed: {e}')
        return None

def get_config_paths() -> Dict[str, str]:
    """Return paths to configuration files.

    Returns:
        Dictionary with:
        - 'global_config': Path to project-level global config file (agent_config.json)
        - 'user_config': Path to user-level config file (~/.thoughtmachine/config.json)
    """
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    global_config_path = os.path.join(project_root, 'agent_config.json')
    user_config_path = str(Path.home() / '.thoughtmachine' / 'config.json')
    return {
        'global_config': global_config_path,
        'user_config': user_config_path,
    }


def get_config_paths() -> Dict[str, str]:
    """Return paths for global and user configuration files.

    Returns:
        dict with keys 'global_config' (project-level agent_config.json)
        and 'user_config' (~/.thoughtmachine/config.json).
    """
    return {
        'global_config': os.path.join(os.getcwd(), 'agent_config.json'),
        'user_config': str(Path.home() / '.thoughtmachine' / 'config.json'),
    }


def update_config(current_config: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update configuration with partial updates.
    
    Args:
        current_config: Current configuration dictionary
        updates: Dictionary with updates to apply
        
    Returns:
        Updated configuration dictionary
    """
    old_ws = current_config.get('workspace_path', 'KEY_MISSING')
    new_ws = updates.get('workspace_path', 'KEY_MISSING')
    has_ws_key = 'workspace_path' in updates
    log('DEBUG', 'config.loader', f'[CONFIG_TRACE] loader.update_config: old_workspace_path={old_ws!r}, new_workspace_path={new_ws!r}, has_workspace_path_key={has_ws_key}, update_keys={list(updates.keys())}')
    updated = current_config.copy()
    updated.update(updates)
    return updated