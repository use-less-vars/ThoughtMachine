"""
ConfigManager — extracted config-related functions from server.py + bridge.py.

Centralises all config format conversion, global defaults loading, and
atomic file writing so that both server.py and bridge.py can import them
from a single location.

Exported names (all public — no leading underscore):
    FALLBACK_FRONTEND_CONFIG
    load_global_defaults
    translate_frontend_config
    frontend_config_from_bridge
    backend_to_frontend_config
    default_frontend_config
    config_to_dict
    atomic_replace
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agent.config.presets import get_tools_for_mode
from agent.logging import log

# ── Project-root discovery (same logic as server.py) ──────────────────────
_project_root: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# ═══════════════════════════════════════════════════════════════════════════
#  FALLBACK_FRONTEND_CONFIG
# ═══════════════════════════════════════════════════════════════════════════

FALLBACK_FRONTEND_CONFIG: Dict[str, Any] = {
    "base_url": "https://api.deepseek.com/v1/",
    "model": "deepseek-v4-flash",
    "provider_type": "openai_compatible",
    "provider_config": {},
    "provider_id": "v4_flash",
    "model_override": None,
    "temperature": 1.0,
    "max_turns": 200,
    "stop_check": None,
    "system_prompt": None,
    "api_key_configured": False,
    "token_monitor_warning_threshold": 60000,
    "token_monitor_critical_threshold": 75000,
    "turn_monitor_enabled": True,
    "enable_logging": True,
    "log_dir": "./logs",
    "log_level": "INFO",
    "enable_file_logging": True,
    "jsonl_format": True,
    "log_categories": ["SESSION", "LLM", "TOOLS"],
    "max_file_size_mb": 10,
    "max_backup_files": 5,
    "workspace_path": _project_root,
    "rag_enabled": False,
    "rag_embedding_model": "BAAI/bge-small-en-v1.5",
    "rag_vector_store_path": None,
    "rag_chunk_size": 1500,
    "rag_chunk_overlap": 200,
    "rag_batch_size": 16,
    "rag_truncate_dim": 256,
    "kb_enabled": True,
    "kb_path": None,
    "tool_output_token_limit": 10000,
    "mode": "agent",
    "detail": "normal",
    "session_permissions": {
        "container": False,
        "network": "banned",
        "filesystem": "read",
        "system": "read",
        "git": "read",
        "execution": "banned",
    },
    "enabled_tools": get_tools_for_mode("agent"),
    "tools": [],
}


# ═══════════════════════════════════════════════════════════════════════════
#  load_global_defaults
# ═══════════════════════════════════════════════════════════════════════════

def load_global_defaults() -> Dict[str, Any]:
    """Load global defaults from ``~/.thoughtmachine/user/defaults.json``.

    Auto-creates the file with sensible defaults on first run.
    """
    config_dir = Path.home() / ".thoughtmachine"
    config_path = config_dir / "user" / "defaults.json"

    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except Exception:
            log("ERROR", "server.config",
                f"Could not parse {config_path}, using fallback")
            return dict(FALLBACK_FRONTEND_CONFIG)
    else:
        log("INFO", "server.config", f"Creating default config at {config_path}")
        with open(config_path, "w") as f:
            json.dump(FALLBACK_FRONTEND_CONFIG, f, indent=2)
        return dict(FALLBACK_FRONTEND_CONFIG)


# ═══════════════════════════════════════════════════════════════════════════
#  translate_frontend_config (was _translate_frontend_config in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def translate_frontend_config(fe_config: Dict[str, Any]) -> Dict[str, Any]:
    """Translate frontend config format to ``AgentConfig`` format.

    This is purely format conversion — no mode-based presets or
    permissions coercion (``SessionConfig`` handles those centrally).

    Frontend sends::

        provider: 'openai' | 'anthropic' | 'local'
        tools: [{name, enabled}, ...]
        temperature, max_turns, etc.

    ``AgentConfig`` expects::

        provider_type: 'openai_compatible' | 'anthropic' | 'openai'
        enabled_tools: ['name1', 'name2', ...]
    """
    cfg = dict(fe_config)

    # Map provider names
    provider_map = {
        "openai": "openai",
        "anthropic": "anthropic",
        "local": "openai_compatible",
        "openai_compatible": "openai_compatible",
    }
    provider = cfg.pop("provider", None)
    if provider:
        cfg["provider_type"] = provider_map.get(provider, provider)

    # Translate frontend tools list → backend enabled_tools
    tools_list = cfg.pop("tools", None)
    if isinstance(tools_list, list):
        enabled = [
            t["name"]
            for t in tools_list
            if isinstance(t, dict) and t.get("enabled")
        ]
        if enabled is not None:
            cfg["enabled_tools"] = enabled

    # Remove any keys that start with _
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}

    # Diagnostic log
    log("INFO", "server.config",
        f"[TRANSLATE] frontend config: provider={fe_config.get('provider')}, "
        f"model={fe_config.get('model')}, keys={list(fe_config.keys())}")
    log("DEBUG", "server.config",
        f"[TRANSLATE] full dump: tools_field={fe_config.get('tools')}, "
        f"enabled_tools_after={cfg.get('enabled_tools')}")

    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  frontend_config_from_bridge (was _frontend_config_from_bridge in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def frontend_config_from_bridge(bridge) -> Dict[str, Any]:
    """Convert bridge's ``SessionConfig`` back to frontend config format."""
    if bridge is None:
        return default_frontend_config()

    cfg = bridge.get_config()
    if cfg is None:
        result = default_frontend_config()
        # The bridge may still provide workspace_path even without a config
        if bridge is not None and bridge._workspace_path:
            result['workspace_path'] = bridge._workspace_path
        result['api_key_configured'] = bool(
            os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_COMPATIBLE_API_KEY")
            or ""
        )
        return result

    # Check if API key is configured before stripping it
    api_key = cfg.get("api_key", "") or ""
    if not api_key:
        api_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_COMPATIBLE_API_KEY")
            or ""
        )
    api_key_configured = bool(api_key)

    # Ensure mode is explicitly set
    if cfg.get("mode") is None:
        cfg["mode"] = "custom"

    result = backend_to_frontend_config(cfg)
    result["api_key_configured"] = api_key_configured
    if bridge._workspace_path:
        result["workspace_path"] = bridge._workspace_path
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  backend_to_frontend_config (was _backend_to_frontend_config in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def backend_to_frontend_config(backend: Dict[str, Any]) -> Dict[str, Any]:
    """Convert backend ``AgentConfig`` format to frontend format for WS messages."""
    cfg = dict(backend)

    # Map provider_type → provider
    provider_reverse = {
        "openai": "openai",
        "anthropic": "anthropic",
        "openai_compatible": "local",
    }
    provider_type = cfg.pop("provider_type", None)
    cfg["provider"] = provider_reverse.get(provider_type, "local")

    # Map enabled_tools → tools list (bidirectional with translate_frontend_config)
    if "enabled_tools" in cfg:
        enabled_set = set(cfg.pop("enabled_tools"))
        mode = cfg.get("mode", "custom")
        from tools import SIMPLIFIED_TOOL_CLASSES

        if mode != "custom":
            mode_tool_names = set(get_tools_for_mode(mode))
        else:
            mode_tool_names = None

        cfg["tools"] = [
            {"name": cls.__name__, "enabled": cls.__name__ in enabled_set}
            for cls in SIMPLIFIED_TOOL_CLASSES
            if mode_tool_names is None or cls.__name__ in mode_tool_names
        ]

    # Ensure workspace_path is always present
    cfg.setdefault("workspace_path", None)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  default_frontend_config (was _default_frontend_config in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def default_frontend_config() -> Dict[str, Any]:
    """Return config in frontend format, merged with global defaults."""
    defaults = dict(FALLBACK_FRONTEND_CONFIG)
    global_defaults = load_global_defaults()
    defaults.update(global_defaults)
    return backend_to_frontend_config(defaults)


# ═══════════════════════════════════════════════════════════════════════════
#  config_to_dict (was _config_to_dict in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def config_to_dict(cfg) -> Dict[str, Any]:
    """Convert an ``AgentConfig`` to a plain dict for JSON serialization."""
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(exclude={"api_key", "stop_check"}, exclude_none=True)
    if hasattr(cfg, "dict"):
        return cfg.dict()
    return {k: str(v) for k, v in vars(cfg).items() if not k.startswith("_")}


# ═══════════════════════════════════════════════════════════════════════════
#  atomic_replace (was _atomic_replace in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def atomic_replace(data: dict, dst: str, work_dir: str, retries: int = 3) -> None:
    """Atomically write *data* as JSON to *dst*, with Windows-safe retries.

    Writes to a temporary file in *work_dir*, then replaces the destination.
    Retries up to *retries* times on ``OSError`` (covers Windows sharing
    violations from antivirus / file locks).  Falls back to ``shutil.move``
    if ``os.replace`` fails after all retries.
    """
    for attempt in range(1, retries + 2):
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=work_dir,
            suffix=".tmp",
            prefix="agent_config_",
        ) as tmp:
            json.dump(data, tmp, indent=2, default=str)
            tmp.flush()
            tmp_path = tmp.name

        try:
            os.replace(tmp_path, dst)
            return  # success
        except OSError:
            # Clean up orphaned temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            if attempt > retries:
                # Final fallback: try shutil.move (more resilient on Windows)
                try:
                    shutil.move(tmp_path, dst)
                    log("WARNING", "server.config",
                        f"atomic_replace: os.replace failed after {retries} retries, "
                        f"used shutil.move as fallback")
                    return
                except OSError as exc:
                    raise exc

            # Back off before retrying
            time.sleep(0.2 * attempt)


# ═══════════════════════════════════════════════════════════════════════════
#  ConfigManager class
# ═══════════════════════════════════════════════════════════════════════════


class ConfigManager:
    """
    Central config management facade.

    Wraps all standalone config-translation functions as ``@staticmethod``
    methods so callers (server.py, bridge.py) can use a single import
    and instance.  Also adds higher-level convenience methods
    (``get_frontend_config``, ``apply_config``, ``validate``) that
    reduce boilerplate in WebSocket handlers.

    Typical usage::

        config_manager = ConfigManager()
        backend = config_manager.translate_frontend_config(fe_config)
        fe_config = config_manager.get_frontend_config(bridge)
        errors = config_manager.validate(fe_config)
    """

    # ── Class-level references to module constants ──────────────────────
    FALLBACK_FRONTEND_CONFIG: Dict[str, Any] = FALLBACK_FRONTEND_CONFIG

    @staticmethod
    def load_global_defaults() -> Dict[str, Any]:
        """Load global defaults from ``~/.thoughtmachine/user/defaults.json``."""
        return load_global_defaults()

    @staticmethod
    def translate_frontend_config(fe_config: Dict[str, Any]) -> Dict[str, Any]:
        """Translate frontend config format to ``AgentConfig`` format."""
        return translate_frontend_config(fe_config)

    @staticmethod
    def frontend_config_from_bridge(bridge) -> Dict[str, Any]:
        """Convert bridge's ``SessionConfig`` back to frontend config format."""
        return frontend_config_from_bridge(bridge)

    @staticmethod
    def backend_to_frontend_config(backend: Dict[str, Any]) -> Dict[str, Any]:
        """Convert backend ``AgentConfig`` format to frontend format."""
        return backend_to_frontend_config(backend)

    @staticmethod
    def default_frontend_config() -> Dict[str, Any]:
        """Return config in frontend format, merged with global defaults."""
        return default_frontend_config()

    @staticmethod
    def config_to_dict(cfg) -> Dict[str, Any]:
        """Convert an ``AgentConfig`` to a plain dict for JSON serialization."""
        return config_to_dict(cfg)

    @staticmethod
    def atomic_replace(data: dict, dst: str, work_dir: str, retries: int = 3) -> None:
        """Atomically write *data* as JSON to *dst*, with Windows-safe retries."""
        return atomic_replace(data, dst, work_dir, retries)

    # ── Higher-level convenience methods ─────────────────────────────

    @staticmethod
    def get_frontend_config(bridge) -> Dict[str, Any]:
        """
        Get the frontend-format config for a bridge.

        Thin wrapper around ``frontend_config_from_bridge`` with a cleaner
        name for callers that want a read-only snapshot of the current
        bridge configuration.
        """
        return frontend_config_from_bridge(bridge)

    @staticmethod
    def session_config_to_frontend(
        session_config,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert a ``SessionConfig`` directly to frontend format (no bridge needed)."""
        cfg_dict = config_to_dict(session_config)
        result = backend_to_frontend_config(cfg_dict)
        # Check if API key is configured
        api_key = getattr(session_config, "api_key", "") or ""
        if not api_key:
            api_key = (
                os.getenv("OPENAI_API_KEY")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_COMPATIBLE_API_KEY")
                or ""
            )
        result["api_key_configured"] = bool(api_key)
        if workspace_path:
            result["workspace_path"] = workspace_path
        return result

    @staticmethod
    def apply_config(
        config_dict: Dict[str, Any],
        current_config,
        is_running: bool = False,
        has_session: bool = False,
    ) -> tuple[Dict[str, Any], Optional[Any]]:
        """
        Validate + translate frontend config dict, enforce mode rules.

        Moves the validation logic from ``bridge.apply_config()`` into here.
        Does **not** do controller update, container sync, or persistence.

        Args:
            config_dict: Raw frontend config dict.
            current_config: The current ``SessionConfig`` instance.
            is_running: Whether the bridge is currently running.
            has_session: Whether a session has been started.

        Returns:
            ``(frontend_format_dict, updated_SessionConfig_or_None)``
        """
        import copy
        from agent.config.session_config import SessionConfig
        from agent.config.provider_profile import ProviderManager

        # Work on a copy to avoid mutating caller's object on failure
        session_config = copy.deepcopy(current_config)

        # Mode field — only mutable before session starts
        if "mode" in config_dict:
            new_mode = config_dict["mode"]
            valid_modes = {"agent", "engineer", "custom"}
            if new_mode not in valid_modes:
                log("WARNING", "server.bridge",
                    f"apply_config: invalid mode '{new_mode}'")
            elif has_session or is_running:
                log("WARNING", "server.bridge",
                    f"apply_config: mode change to '{new_mode}' rejected — "
                    f"mode is immutable after session start")
            elif new_mode != session_config.mode:
                log("INFO", "server.bridge",
                    f"apply_config: changing mode from '{session_config.mode}' to '{new_mode}'")
                old_cfg = session_config.model_dump(exclude={'api_key'}, exclude_none=True)
                old_cfg['mode'] = new_mode
                try:
                    session_config = SessionConfig(**old_cfg)
                except Exception as e:
                    log("WARNING", "server.bridge",
                        f"apply_config: failed to apply mode '{new_mode}': {e}")

        # Tool changes (mode-locked: only works in custom mode)
        if "enabled_tools" in config_dict:
            session_config.update_tools(config_dict["enabled_tools"])

        # Prompt changes (mode-locked: only works in custom mode)
        if "system_prompt" in config_dict:
            session_config.update_prompt(config_dict["system_prompt"])

        # Mutable fields (always allowed regardless of mode)
        for field in ("provider_id", "model", "base_url", "temperature", "top_p", "max_tokens"):
            if field in config_dict:
                setattr(session_config, field, config_dict[field])

        # Session permissions (always mutable)
        if "session_permissions" in config_dict:
            sp = config_dict["session_permissions"]
            if sp is not None and isinstance(sp, dict):
                session_config.session_permissions = sp

        # If provider_id changed, resolve provider credentials
        if "provider_id" in config_dict and config_dict["provider_id"]:
            try:
                manager = ProviderManager()
                resolved = manager.resolve_config(session_config.model_dump(exclude_none=True))
                if "api_key" in resolved:
                    session_config.api_key = resolved["api_key"]
                if "base_url" in resolved:
                    session_config.base_url = resolved["base_url"]
            except Exception as e:
                log("WARNING", "server.bridge",
                    f"Provider resolution failed during apply_config: {e}")

        # Convert updated session_config to frontend format for broadcasting
        frontend_result = ConfigManager.session_config_to_frontend(session_config)

        return frontend_result, session_config

    @staticmethod
    def validate(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate a frontend-format config dict.

        Checks for common configuration issues and returns a dict with:

        - ``valid`` (bool): ``True`` if no blocking errors found.
        - ``errors`` (list): blocking issues (e.g., missing provider).
        - ``warnings`` (list): non-blocking suggestions.
        - ``field_errors`` (dict): field-level error messages.
        """
        errors: list = []
        warnings: list = []
        field_errors: Dict[str, str] = {}

        if not config:
            errors.append("Config is empty")
            return {"valid": False, "errors": errors, "warnings": [], "field_errors": {}}

        # Check required fields
        if not config.get("provider"):
            field_errors["provider"] = "Provider is required"
            errors.append("No provider selected")

        if not config.get("model"):
            field_errors["model"] = "Model is required"
            errors.append("No model specified")

        # Check for potentially unsafe API key exposure
        api_key = config.get("api_key", "")
        if api_key and len(api_key) > 0:
            warnings.append("API key included in config; will be stripped before persistence")

        # Check temperature range
        temp = config.get("temperature", 1.0)
        if temp is not None:
            try:
                t = float(temp)
                if t < 0.0 or t > 2.0:
                    warnings.append(f"Temperature {t} is outside recommended range [0.0, 2.0]")
            except (ValueError, TypeError):
                field_errors["temperature"] = "Temperature must be a number"
                errors.append("Invalid temperature value")

        # Check max_turns
        max_turns = config.get("max_turns", 200)
        if max_turns is not None:
            try:
                mt = int(max_turns)
                if mt < 1:
                    field_errors["max_turns"] = "Must be at least 1"
                    errors.append("max_turns must be at least 1")
            except (ValueError, TypeError):
                field_errors["max_turns"] = "Must be an integer"
                errors.append("Invalid max_turns value")

        # Check workspace_path
        workspace = config.get("workspace_path", "")
        if workspace and not os.path.isdir(workspace):
            warnings.append(f"Workspace path '{workspace}' does not exist on disk")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "field_errors": field_errors,
        }
