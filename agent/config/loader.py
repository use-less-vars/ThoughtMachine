"""
Configuration loading utilities for the ThoughtMachine agent.
Handles loading, saving, and validation of agent configurations.
"""
import os
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Set
from agent.logging import log
from .models import AgentConfig

# ── System prompt paths ───────────────────────────────────────────────────────
USER_DIR = Path.home() / ".thoughtmachine"
CUSTOM_SYSTEM_PROMPT_PATH = str(USER_DIR / "custom_system_prompt.txt")
LEGACY_SYSTEM_PROMPT_PATH = str(USER_DIR / "system_prompt.txt")

# ── Factory config ──────────────────────────────────────────────────────────
FACTORY_CONFIG_PATH = str(Path(__file__).resolve().parent.parent.parent / "resources" / "default_config.json")
DEFAULT_SYSTEM_PROMPT_PATH = str(Path(__file__).resolve().parent.parent.parent / "resources" / "default_system_prompt.txt")
_factory_config_cache: Optional[Dict[str, Any]] = None


def load_custom_system_prompt() -> Optional[str]:
    """Load the custom system prompt from ``~/.thoughtmachine/custom_system_prompt.txt``.

    Returns the prompt text if the file exists and is non-empty, otherwise ``None``.
    """
    path = Path(CUSTOM_SYSTEM_PROMPT_PATH)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text if text else None
    except (IOError, OSError) as exc:
        log("WARNING", "config.loader",
            f"Failed to read custom system prompt from {CUSTOM_SYSTEM_PROMPT_PATH}: {exc}")
        return None


def load_default_system_prompt_text() -> str:
    """Load the factory-default system prompt from ``resources/default_system_prompt.txt``.

    This is the prompt that ships with the application and is used as the
    fallback when no custom prompt is set.

    Returns:
        The default prompt text. If the file cannot be read, returns an empty
        string so callers can treat it as "no default".
    """
    path = Path(DEFAULT_SYSTEM_PROMPT_PATH)
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""
    except (IOError, OSError) as exc:
        log("WARNING", "config.loader",
            f"Failed to read default system prompt from {DEFAULT_SYSTEM_PROMPT_PATH}: {exc}")
        return ""


# ── Factory config loader ──────────────────────────────────────────────────────


def load_factory_config() -> Dict[str, Any]:
    """Load factory default configuration from ``resources/default_config.json``.

    The factory config is the single source of truth for default values.
    Results are cached in memory after the first load for performance.

    Returns:
        A copy of the factory config dictionary.
    """
    global _factory_config_cache
    if _factory_config_cache is not None:
        return _factory_config_cache.copy()
    path = Path(FACTORY_CONFIG_PATH)
    if not path.exists():
        log("WARNING", "config.loader",
            f"Factory config not found at {FACTORY_CONFIG_PATH}, using model defaults")
        _factory_config_cache = load_default_config()
        return _factory_config_cache.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            config: Dict[str, Any] = json.load(f)
        _factory_config_cache = config
        log("DEBUG", "config.loader", f"Loaded factory config from {FACTORY_CONFIG_PATH}")
        return config.copy()
    except Exception as e:
        log("WARNING", "config.loader",
            f"Failed to load factory config from {FACTORY_CONFIG_PATH}: {e}, using model defaults")
        _factory_config_cache = load_default_config()
        return _factory_config_cache.copy()


def _deep_merge_config(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge *overlay* into *base*, preserving nested dicts.

    For keys present in both *base* and *overlay* where the value is a dict
    in both, the merge is recursive (sub-keys are merged).  For all other
    keys, *overlay* wins outright (scalar replacement).

    Args:
        base: Base configuration (typically factory defaults).
        overlay: Override configuration (typically user settings).

    Returns:
        A new dict with the merged result (neither input is mutated).
    """
    result = base.copy()
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_config(result[key], value)
        else:
            result[key] = value
    return result


def _compute_config_diff(factory: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the diff between factory defaults and a current config.

    Returns only the keys where *current* differs from *factory*.
    For nested dict values, recurses to produce a minimal diff.

    Args:
        factory: Factory default configuration.
        current: Current (user) configuration.

    Returns:
        A dict containing only the differences (empty if identical).
    """
    diff: Dict[str, Any] = {}
    for key, value in current.items():
        if key not in factory:
            # Key not present in factory at all → include it
            diff[key] = value
        elif isinstance(value, dict) and isinstance(factory.get(key), dict):
            # Nested dict → recurse
            sub_diff = _compute_config_diff(factory[key], value)
            if sub_diff:
                diff[key] = sub_diff
        elif value != factory[key]:
            diff[key] = value
    return diff


def _migrate_legacy_system_prompt() -> None:
    """Migrate the legacy ``~/.thoughtmachine/system_prompt.txt`` to the new
    ``~/.thoughtmachine/custom_system_prompt.txt``.

    The old file was deployed by the @field_validator in earlier versions but
    was never in MANIFEST.json. If the new file already exists the migration
    is skipped (the new file takes precedence).
    """
    legacy = Path(LEGACY_SYSTEM_PROMPT_PATH)
    custom = Path(CUSTOM_SYSTEM_PROMPT_PATH)
    if not legacy.exists():
        return
    if custom.exists():
        log("DEBUG", "config.loader",
            f"Both {LEGACY_SYSTEM_PROMPT_PATH} and {CUSTOM_SYSTEM_PROMPT_PATH} exist — "
            f"keeping custom (new name) and removing legacy.")
        legacy.unlink(missing_ok=True)
        return
    try:
        text = legacy.read_text(encoding="utf-8").strip()
        if text:
            custom.write_text(text + "\n", encoding="utf-8")
            log("INFO", "config.loader",
                f"Migrated legacy system prompt from {LEGACY_SYSTEM_PROMPT_PATH} "
                f"to {CUSTOM_SYSTEM_PROMPT_PATH}")
        legacy.unlink(missing_ok=True)
    except (IOError, OSError) as exc:
        log("WARNING", "config.loader",
            f"Failed to migrate legacy system prompt {LEGACY_SYSTEM_PROMPT_PATH}: {exc}")

# ── Backup safety ───────────────────────────────────────────────────────────
BACKUP_DIR_NAME = '.config_backups'


def _ensure_backup_dir() -> str:
    """Return path to config backup directory, creating it if needed."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    backup_dir = os.path.join(project_root, BACKUP_DIR_NAME)
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def _backup_config(config_path: str) -> Optional[str]:
    """Create a timestamped backup of an existing config file before overwriting.

    Returns the backup path, or None if no existing file or backup failed.
    """
    if not os.path.exists(config_path):
        return None
    try:
        backup_dir = _ensure_backup_dir()
        base = os.path.basename(config_path)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f'{base}.{ts}.bak'
        backup_path = os.path.join(backup_dir, backup_name)
        shutil.copy2(config_path, backup_path)
        log('INFO', 'config.loader', f'Backed up config to {backup_path}')
        return backup_path
    except Exception as e:
        log('WARNING', 'config.loader', f'Failed to back up config {config_path}: {e}')
        return None


def _sanitize_config_for_serialization(config: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any non-serializable values (callables, etc.) from a config dict.

    This is a safety net so that a leaked ``stop_check`` or similar callable
    never reaches ``json.dump()`` and causes a hard crash.
    """
    cleaned = {}
    for k, v in config.items():
        if callable(v):
            log('WARNING', 'config.loader',
                f'Found callable in config key {k!r} — stripping it from serialization')
            continue
        if isinstance(v, dict):
            cleaned[k] = _sanitize_config_for_serialization(v)
        elif isinstance(v, list):
            cleaned[k] = [
                _sanitize_config_for_serialization(item) if isinstance(item, dict) else item
                for item in v
                if not callable(item)
            ]
        else:
            cleaned[k] = v
    return cleaned


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
    """Return default configuration dictionary from model defaults.

    This is a fallback when the factory config file is unavailable.
    Prefer :func:`load_factory_config` for production use.
    """
    config = AgentConfig()
    config_dict = config.model_dump()
    return config_dict
def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from file and overlay on factory defaults.

    Uses the factory default config (from ``resources/default_config.json``)
    as the base and overlays user settings from *config_path* on top.

    Gracefully handles:
      - Missing user config file → returns factory defaults
      - Corrupted JSON → logs warning, returns factory defaults
      - Legacy field names → auto-migrated via ``_map_legacy_fields``
      - Null fields → backfilled from model defaults via ``_backfill_nulls``
      - Legacy ``system_prompt.txt`` → auto-migrated to ``custom_system_prompt.txt``

    Args:
        config_path: Path to user configuration file (JSON overlay)

    Returns:
        Configuration dictionary with factory defaults merged with user overrides
    """
    # Migrate legacy system_prompt.txt → custom_system_prompt.txt if needed
    _migrate_legacy_system_prompt()

    factory_config = load_factory_config()
    if not os.path.exists(config_path):
        log('DEBUG', 'config.loader', f'Config file {config_path} not found, using factory defaults')
        return factory_config
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            raw_content = f.read()
        if not raw_content.strip():
            log('WARNING', 'config.loader', f'Config file {config_path} is empty, using factory defaults')
            return factory_config
        saved_config = json.loads(raw_content)
        if not isinstance(saved_config, dict):
            log('WARNING', 'config.loader',
                f'Config file {config_path} does not contain a JSON object, using factory defaults')
            return factory_config
        saved_config = _map_legacy_fields(saved_config)
        saved_config = _sanitize_config_for_serialization(saved_config)
        # Deep-merge user overlay on top of factory defaults
        merged_config = _deep_merge_config(factory_config, saved_config)
        log('DEBUG', 'config.loader', f'Loaded config from {config_path} (overlay on factory)')
        merged_config = _backfill_nulls(merged_config)
        return merged_config
    except json.JSONDecodeError as e:
        log('WARNING', 'config.loader',
            f'Corrupted config file {config_path}: {e}. Falling back to factory defaults.')
        return factory_config
    except Exception as e:
        log('WARNING', 'config.loader', f'Error loading config from {config_path}: {e}')
        return factory_config
def _get_valid_field_names() -> Set[str]:
    """Return the set of valid field names for AgentConfig."""
    return set(AgentConfig.model_fields.keys())


def _warn_stray_keys(config: Dict[str, Any]) -> None:
    """Log a warning if config dict contains keys not in AgentConfig model.

    This catches stray/injected keys that would otherwise vanish silently
    on save-write, preventing data corruption from spreading.
    """
    valid = _get_valid_field_names()
    stray = [k for k in config if k not in valid]
    if stray:
        log('WARNING', 'config.loader',
            f'Stray keys detected in config (will NOT be persisted): {stray}')
        for key in stray:
            del config[key]


def _backfill_nulls(config: Dict[str, Any]) -> Dict[str, Any]:
    """Replace None/null values with model defaults.

    If a saved config explicitly contains null for a field, use the
    AgentConfig default for that field instead. This prevents None
    values from propagating through the runtime.
    """
    default_cfg = AgentConfig()
    for field_name in AgentConfig.model_fields:
        if field_name in config and config[field_name] is None:
            default_value = getattr(default_cfg, field_name, None)
            if default_value is not None:
                config[field_name] = default_value
    return config


def save_config(config: Dict[str, Any], config_path: str, backup: bool = True) -> bool:
    """Save configuration to file with self-healing safeguards.

    Steps:
      1. Warn about stray keys (keys not in AgentConfig schema).
      2. Sanitize — strip any callables / non-serializable values.
      3. Create a timestamped backup of the previous file (if *backup* = True).
      4. Write atomically via a temp file + rename.

    Args:
        config: Configuration dictionary
        config_path: Path to save configuration file
        backup: Whether to create a timestamped backup before overwriting

    Returns:
        True if successful, False otherwise
    """
    try:
        _warn_stray_keys(config)
        config = _sanitize_config_for_serialization(config)

        # Strip sensitive keys before persisting to disk
        config.pop('api_key', None)

        if backup:
            _backup_config(config_path)

        os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)

        # Atomic write via temp file to prevent partial writes
        tmp_path = config_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, config_path)

        log('DEBUG', 'config.loader', f'Saved config to {config_path}')
        return True
    except Exception as e:
        log('ERROR', 'config.loader', f'Error saving config to {config_path}: {e}')
        return False
def _migrate_system_prompt_in_config(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Strip ``system_prompt`` from config if a custom file is present.

    When the user has a ``custom_system_prompt.txt``, the ``system_prompt``
    key in the JSON config is redundant (it will be loaded from the file at
    model-validation time).  Removing it prevents confusion about which
    source is authoritative.
    """
    if "system_prompt" in config_dict and load_custom_system_prompt() is not None:
        config_dict = config_dict.copy()
        del config_dict["system_prompt"]
    return config_dict


def migrate_config(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a config dict to the latest schema version.

    Currently handled migrations:
      - Legacy field name remapping (delegates to ``_map_legacy_fields``)
      - Null backfill (delegates to ``_backfill_nulls``)
      - Sanitization (removes callables)

    Returns a new dict (the original is not mutated).
    """
    migrated = config_dict.copy()
    migrated = _map_legacy_fields(migrated)
    migrated = _sanitize_config_for_serialization(migrated)
    migrated = _backfill_nulls(migrated)
    migrated = _migrate_system_prompt_in_config(migrated)
    return migrated


def validate_config(config_dict: Dict[str, Any]) -> Optional[AgentConfig]:
    """Validate configuration dictionary and return AgentConfig instance.

    Args:
        config_dict: Configuration dictionary

    Returns:
        AgentConfig instance if valid, None otherwise
    """
    try:
        # Strip runtime-only callable fields that may have been serialised
        # as strings in old sessions (pre-f33bb6a).  stop_check is a live
        # callback set by the controller and must never be persisted to JSON.
        config_dict = {k: v for k, v in config_dict.items()
                       if not (k == 'stop_check' and isinstance(v, str))}
        return AgentConfig(**config_dict)
    except Exception as e:
        log('ERROR', 'config.loader', f'Configuration validation failed: {e}')
        return None

def get_config_paths() -> Dict[str, str]:
    """Return paths to configuration files.

    Returns:
        Dictionary with:
        - 'global_config': Path to project-level global config file (agent_config.json)
        - 'user_config': Path to user-level config file (~/.thoughtmachine/agent_config.json)
    """
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    global_config_path = os.path.join(project_root, 'agent_config.json')
    user_config_path = str(Path.home() / '.thoughtmachine' / 'agent_config.json')
    return {
        'global_config': global_config_path,
        'user_config': user_config_path,
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