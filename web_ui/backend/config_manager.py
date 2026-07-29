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
    """Load global defaults from ``~/.thoughtmachine/agent_config.json``.

    Auto-creates the file with sensible defaults on first run.
    """
    config_dir = Path.home() / ".thoughtmachine"
    config_path = config_dir / "agent_config.json"

    config_dir.mkdir(parents=True, exist_ok=True)

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
        # Bridge exists but has no _session_config yet (before-first-query state).
        result = backend_to_frontend_config({
            "mode": "custom",
            "temperature": 1.0,
            "max_turns": 100,
            "enabled_tools": [],
            "provider_type": "openai_compatible",
        })
        if bridge._workspace_path:
            result["workspace_path"] = bridge._workspace_path
        result["api_key_configured"] = bool(
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
