"""
ConfigManager — extracted config-related functions from server.py + bridge.py.

Centralises all config format conversion, global defaults loading, and
atomic file writing so that both server.py and bridge.py can import them
from a single location.

Exported names (all public — no leading underscore):
    FALLBACK_FRONTEND_CONFIG
    GLOBAL_DEFAULT_KEYS
    load_global_defaults
    translate_frontend_config
    frontend_config_from_bridge
    backend_to_frontend_config
    default_frontend_config
    config_to_dict
    atomic_replace
    resolve_full_config
    session_config_from_merged
    agent_config_from_merged
    CONFIG_LAYER_ORDER
    CONFIG_LAYER_OWNERSHIP
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

from agent.config.config_manager import (
    _factory_defaults_path as _get_factory_defaults_path,
    _user_defaults_path as _get_user_defaults_path,
    _workspace_defaults_path as _get_workspace_defaults_path,
)
from agent.config.deep_merge import deep_merge
from agent.config.service import create_agent_config_service

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
    "timeout_seconds": None,
    "stop_check": None,
    "system_prompt": None,
    "api_key_configured": False,
    "token_monitor_warning_threshold": 65000,
    "token_monitor_critical_threshold": 80000,
    "turn_monitor_enabled": True,
    "enable_logging": True,
    "log_dir": os.path.join(os.path.expanduser("~"), ".thoughtmachine", "logs"),
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
#  GLOBAL_DEFAULT_KEYS
# ═══════════════════════════════════════════════════════════════════════════
# The only keys the Web UI may persist into the global-default layer
# (``~/.thoughtmachine/user/defaults.json``) when the user saves a config as
# the global default (``set_default_config``).  Every other key in a
# frontend/backend config payload is session-local or workspace-scoped and
# must NOT be written into the global defaults file.  See
# ``docs/architecture/config_ownership.md`` for the full ownership model.

GLOBAL_DEFAULT_KEYS = frozenset({
    "provider_id",
    "model",
    "base_url",
    "temperature",
    "max_turns",
    "system_prompt",
})


# Full layer precedence for ``resolve_full_config`` (lowest → highest).
# Each layer owns a documented key set (see CONFIG_LAYER_OWNERSHIP).
CONFIG_LAYER_ORDER = (
    "fallback",
    "factory",
    "global_defaults",
    "agent_config",
    "provider_profile",
    "workspace_config",
    "session_config",
    "worker_overrides",
)

# Per-key ownership for the merge chain.  Layers may only contribute the
# keys they own; ``resolve_full_config`` filters the global-defaults layer
# to GLOBAL_DEFAULT_KEYS and passes every other layer through as-is.
CONFIG_LAYER_OWNERSHIP: Dict[str, str] = {
    "fallback": "all keys (frontend shape base)",
    "factory": "all keys (base overrides)",
    "global_defaults": "provider_id, model, base_url, temperature, max_turns, system_prompt (GLOBAL_DEFAULT_KEYS only)",
    "agent_config": "legacy AgentConfig keys (read-compat only)",
    "provider_profile": "provider_type, api_key, base_url, provider_config {timeout, max_retries}, default_model → model",
    "workspace_config": "any flat config keys (vault workspaces/<id>/defaults.json)",
    "session_config": "mode, enabled_tools, session_permissions, workspace_path, provider_id, model, api_key, base_url, temperature, max_turns, timeout_seconds, feature flags",
    "worker_overrides": "model, temperature, max_turns, system_prompt, enabled_tools, timeout_seconds, token thresholds, session_permissions, workspace_path",
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
        ) or result.get('api_key_configured', False)
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
        # Legacy class names → stable tool names: configs written before the
        # naming cleanup (e.g. enabled_tools: ["GitInfoTool"]) keep working.
        _LEGACY_TOOL_NAMES = {
            "GitInfoTool": "git_read",
            "GitWriteTool": "git_write",
        }

        def _normalize(name: str) -> str:
            return _LEGACY_TOOL_NAMES.get(name, name)

        enabled_set = set(_normalize(n) for n in cfg.pop("enabled_tools"))
        mode = cfg.get("mode", "custom")
        from tools import SIMPLIFIED_TOOL_CLASSES

        if mode != "custom":
            mode_tool_names = set(_normalize(n) for n in get_tools_for_mode(mode))
        else:
            mode_tool_names = None

        allow_host_resources = bool(cfg.get("allow_host_resources", False))
        session_perms = cfg.get("session_permissions") or {}
        if hasattr(session_perms, "model_dump"):
            session_perms = session_perms.model_dump()
        elif not isinstance(session_perms, dict):
            session_perms = {}

        cfg["tools"] = [
            {
                "name": cls.tool_name(),
                "enabled": cls.tool_name() in enabled_set
                or cls.__name__ in enabled_set,
                # Keep tool descriptions so the frontend can show them without
                # a separate /api/tools round-trip (session_loaded tools fix).
                "description": (cls.__doc__ or "").strip(),
                # host_bash is gated on the allow_host_resources feature flag;
                # expose why it is off so the UI can explain it.
                "disabled_reason": (
                    "requires allow_host_resources: true"
                    if cls.tool_name() == "host_bash"
                    and not allow_host_resources
                    else None
                ),
                # host_bash has no outer-gate category (permission checks are
                # in-tool); surface the configured grain for the UI.
                "permission_level": (
                    session_perms.get("host_bash")
                    if cls.tool_name() == "host_bash"
                    else None
                ),
            }
            for cls in SIMPLIFIED_TOOL_CLASSES
            if mode_tool_names is None
            or cls.tool_name() in mode_tool_names
            or cls.__name__ in mode_tool_names
        ]

    # Ensure workspace_path is always present
    cfg.setdefault("workspace_path", None)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  default_frontend_config (was _default_frontend_config in server.py)
# ═══════════════════════════════════════════════════════════════════════════

def default_frontend_config() -> Dict[str, Any]:
    """Return config in frontend format, resolved through the full layer chain."""
    merged = resolve_full_config()
    api_key = merged.pop("api_key", None) or ""
    if not api_key:
        api_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_COMPATIBLE_API_KEY")
            or ""
        )
    result = backend_to_frontend_config(merged)
    result["api_key_configured"] = bool(api_key)
    return result


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


def get_effective_config(
    bridge_or_config,
    workspace_id: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return the COMPLETE effective agent config as a plain dict (api_key redacted).

    Accepts either a bridge (``_session_config`` attribute) or a config object
    (``SessionConfig`` / ``AgentConfig`` / plain dict).  The api_key is never
    included (``AgentConfig.api_key`` is ``exclude=True`` on dump); any other
    sensitive key is redacted via ``redact_config``.

    ``workspace_id`` / ``workspace_path`` arguments override the values derived
    from the bridge/config so callers can reflect the authoritative source.
    """
    from agent.config.audit import redact_config

    sc = None
    if bridge_or_config is None:
        return {}
    if hasattr(bridge_or_config, "_session_config"):
        sc = bridge_or_config._session_config
        if sc is None:
            return {}
    elif hasattr(bridge_or_config, "to_agent_config"):
        sc = bridge_or_config
    elif hasattr(bridge_or_config, "model_dump"):
        sc = bridge_or_config
    elif isinstance(bridge_or_config, dict):
        return redact_config(bridge_or_config)
    else:
        return {}

    try:
        if hasattr(sc, "to_agent_config"):
            agent_cfg = sc.to_agent_config()
        else:
            agent_cfg = sc
        if hasattr(agent_cfg, "model_dump"):
            data = agent_cfg.model_dump()
        elif isinstance(agent_cfg, dict):
            data = dict(agent_cfg)
        else:
            data = {k: str(v) for k, v in vars(agent_cfg).items()
                    if not k.startswith("_")}
    except Exception:
        return {}

    # workspace_path: explicit arg > bridge attribute > config value
    if workspace_path is None and bridge_or_config is not None:
        workspace_path = getattr(bridge_or_config, "_workspace_path", None)
    if workspace_path is None:
        workspace_path = getattr(sc, "workspace_path", None)
    if workspace_path is not None:
        data["workspace_path"] = workspace_path

    # workspace_id: explicit arg > bridge attribute > config value
    if workspace_id is None and bridge_or_config is not None:
        workspace_id = getattr(bridge_or_config, "_workspace_id", None)
    if workspace_id is None:
        workspace_id = getattr(sc, "workspace_id", None)
    if workspace_id is not None:
        data["workspace_id"] = workspace_id

    return redact_config(data)


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


# ── Full-config merger (single entry point) ─────────────────────────────────


def _load_factory_defaults() -> Dict[str, Any]:
    """Layer base: FALLBACK_FRONTEND_CONFIG + factory_defaults.json (if present)."""
    merged = deep_merge({}, dict(FALLBACK_FRONTEND_CONFIG))
    try:
        path = Path(_get_factory_defaults_path())
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("config"), dict):
                raw = raw["config"]
            if isinstance(raw, dict):
                merged = deep_merge(merged, raw)
    except (OSError, json.JSONDecodeError) as exc:
        log("WARNING", "server.config", f"Could not load factory defaults: {exc}")
    return merged


def _load_global_defaults_layer() -> Dict[str, Any]:
    """Global-defaults layer — GLOBAL_DEFAULT_KEYS only, no auto-create side effect."""
    try:
        path = Path(_get_user_defaults_path())
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if k in GLOBAL_DEFAULT_KEYS}
    except (OSError, json.JSONDecodeError) as exc:
        log("WARNING", "server.config", f"Could not load global defaults: {exc}")
    return {}


def _load_agent_config_layer() -> Dict[str, Any]:
    """agent_config.json layer — legacy AgentConfig keys, read-compat only.

    Contributes only when the agent_config.json file actually exists:
    ``ConfigService.get_all()`` returns the full AgentConfig DEFAULTS dict
    when the file is missing, and those defaults must not override the
    factory/global layers.  Injected fakes without a ``config_path``
    attribute are treated as present (their ``get_all`` is authoritative).
    """
    try:
        service = create_agent_config_service()
        config_path = getattr(service, "config_path", None)
        if config_path is not None and not os.path.exists(config_path):
            return {}
        cfg = service.get_all()
        if isinstance(cfg, dict):
            return cfg
    except Exception as exc:
        log("WARNING", "server.config", f"Could not load agent_config.json: {exc}")
    return {}


def _resolve_provider_layer(
    merged: Dict[str, Any],
    provider_id: Optional[str] = None,
    fallback_any: bool = True,
) -> Dict[str, Any]:
    """Provider-profile layer: api_key/base_url/provider_config/model from profile."""
    from agent.config.provider_profile import ProviderManager

    pid = provider_id or merged.get("provider_id")
    try:
        manager = ProviderManager()
        resolved = manager.resolve_config(
            {**merged, "provider_id": pid} if pid else dict(merged)
        )
        if fallback_any and not resolved.get("api_key"):
            for profile in manager.list_profiles():
                if profile.api_key:
                    resolved["api_key"] = profile.api_key
                    resolved["provider_id"] = profile.id
                    break
        return resolved
    except Exception as exc:
        log("WARNING", "server.config",
            f"Provider resolution failed in resolve_full_config: {exc}")
        return merged


def resolve_full_config(
    workspace_id: Optional[str] = None,
    session_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    worker_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the full config by merging every layer (lowest → highest).

    Precedence (see CONFIG_LAYER_ORDER):
        fallback < factory < global defaults < agent_config.json
        < provider profile < workspace config < session config
        < worker overrides

    Never raises: every layer is guarded; missing files/layers are skipped.
    Returns a plain dict (not a model).
    """
    merged = _load_factory_defaults()
    merged = deep_merge(merged, _load_global_defaults_layer())
    merged = deep_merge(merged, _load_agent_config_layer())
    merged = _resolve_provider_layer(merged, provider_id=provider_id)

    if workspace_id:
        try:
            path = Path(_get_workspace_defaults_path(workspace_id))
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    merged = deep_merge(merged, raw)
        except (OSError, json.JSONDecodeError) as exc:
            log("WARNING", "server.config", f"Could not load workspace config: {exc}")

    if session_id:
        try:
            from session.store import FileSystemSessionStore
            session = FileSystemSessionStore().load_session(
                session_id, workspace_id=workspace_id
            )
            if session is not None:
                raw = session.metadata.get("session_config") or session.metadata.get("agent_config")
                if isinstance(raw, dict):
                    raw = dict(raw)
                    if "mode" not in raw:
                        raw["mode"] = "agent"  # mirror repair_session legacy default
                    merged = deep_merge(merged, raw)
        except Exception as exc:
            log("WARNING", "server.config", f"Could not load session config: {exc}")

    if worker_overrides and isinstance(worker_overrides, dict):
        merged = deep_merge(merged, worker_overrides)

    return merged


def _filter_model_fields(data: Dict[str, Any], model) -> Dict[str, Any]:
    """Filter *data* to fields declared by *model* (strict-schema safe)."""
    return {k: v for k, v in data.items() if k in model.model_fields and v is not None}


def session_config_from_merged(merged: Dict[str, Any]):
    """Build a ``SessionConfig`` from a merged dict (extra keys filtered)."""
    from agent.config.session_config import SessionConfig
    return SessionConfig(**_filter_model_fields(merged, SessionConfig))


def agent_config_from_merged(merged: Dict[str, Any]):
    """Build an ``AgentConfig`` from a merged dict (extra keys filtered)."""
    from agent.config.models import AgentConfig
    return AgentConfig(**_filter_model_fields(merged, AgentConfig))


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
    def resolve_full_config(
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        worker_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Resolve the full config by merging all layers (module-level)."""
        return resolve_full_config(
            workspace_id=workspace_id,
            session_id=session_id,
            provider_id=provider_id,
            worker_overrides=worker_overrides,
        )

    @staticmethod
    def session_config_from_merged(merged: Dict[str, Any]):
        """Build a ``SessionConfig`` from a merged dict (extra keys filtered)."""
        return session_config_from_merged(merged)

    @staticmethod
    def agent_config_from_merged(merged: Dict[str, Any]):
        """Build an ``AgentConfig`` from a merged dict (extra keys filtered)."""
        return agent_config_from_merged(merged)

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
    def get_effective_config(
        bridge_or_config,
        workspace_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get the COMPLETE effective agent config (api_key redacted).

        Wrapper around the module-level ``get_effective_config``.
        """
        return get_effective_config(
            bridge_or_config,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
        )

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
    def resolve_effective_permissions(
        session_config,
    ) -> Dict[str, Any]:
        """
        Resolve effective session permissions from a ``SessionConfig``.

        Returns a normalized permissions dict with defaults applied for any
        missing categories.  This merges the raw ``session_permissions``
        dict from the config with the system defaults so the frontend always
        sees a complete permissions profile.
        """
        from thoughtmachine.security import SessionPermissions

        raw_perms = getattr(session_config, "session_permissions", None) or {}
        try:
            perms_obj = SessionPermissions(**raw_perms)
            return perms_obj.model_dump()
        except Exception:
            return {
                "container": False,
                "network": "banned",
                "filesystem": "read",
                "system": "read",
                "git": "read",
                "execution": "banned",
            }

    @staticmethod
    def extract_settings(frontend_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract core operational settings from a frontend-format config dict.

        Returns a subset containing only the operational knobs that control
        agent behaviour (provider, model, temperature, mode, etc.), excluding
        tools, permissions, and workspace metadata.
        """
        keys = (
            "mode",
            "provider",
            "provider_id",
            "model",
            "base_url",
            "temperature",
            "top_p",
            "max_tokens",
            "max_turns",
            "timeout_seconds",
            "system_prompt",
            "api_key_configured",
        )
        return {k: frontend_config.get(k) for k in keys if k in frontend_config}

    @staticmethod
    def apply_config(
        config_dict: Dict[str, Any],
        current_config,
        is_running: bool = False,
        has_session: bool = False,
        workspace_path: Optional[str] = None,
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
        from agent.config.session_config import SessionConfig, normalize_system_prompt
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
        # Defense-in-depth: normalize the raw client value BEFORE update_prompt
        # (SessionConfig.update_prompt also normalizes) so a file-object dict
        # {"name", "content", ...} can never be stored raw and later
        # str()/json.dumps-ed into the LLM system message.
        if "system_prompt" in config_dict:
            session_config.update_prompt(
                normalize_system_prompt(config_dict["system_prompt"])
            )

        # Mutable fields (always allowed regardless of mode)
        for field in ("provider_id", "model", "base_url", "temperature", "top_p", "max_turns",
                      "timeout_seconds",
                      "token_monitor_warning_threshold", "token_monitor_critical_threshold"):
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
                merged = resolve_full_config(provider_id=config_dict["provider_id"])
                if merged.get("api_key"):
                    session_config.api_key = merged["api_key"]
                if merged.get("base_url"):
                    session_config.base_url = merged["base_url"]
                # Merge provider-specific config (timeout/max_retries) into session
                resolved_pc = merged.get("provider_config") or {}
                if resolved_pc:
                    merged_pc = dict(getattr(session_config, "provider_config", None) or {})
                    merged_pc.update(resolved_pc)
                    session_config.provider_config = merged_pc
            except Exception as e:
                log("WARNING", "server.bridge",
                    f"Provider resolution failed during apply_config: {e}")

        # Convert updated session_config to frontend format for broadcasting
        frontend_result = ConfigManager.session_config_to_frontend(
            session_config, workspace_path=workspace_path
        )

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

        # Check timeout_seconds
        timeout_seconds = config.get("timeout_seconds")
        if timeout_seconds is not None:
            try:
                ts = int(timeout_seconds)
                if ts < 1:
                    field_errors["timeout_seconds"] = "Must be at least 1"
                    errors.append("timeout_seconds must be at least 1")
            except (ValueError, TypeError):
                field_errors["timeout_seconds"] = "Must be an integer"
                errors.append("Invalid timeout_seconds value")

        # Check token monitor thresholds — warning must stay below critical
        warn_thr = config.get("token_monitor_warning_threshold")
        crit_thr = config.get("token_monitor_critical_threshold")
        if warn_thr is not None and crit_thr is not None:
            try:
                if int(warn_thr) >= int(crit_thr):
                    field_errors["token_monitor_warning_threshold"] = (
                        "Warning threshold must be below critical threshold")
                    warnings.append("Warning threshold should be below critical threshold")
            except (ValueError, TypeError):
                pass

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

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "field_errors": field_errors,
        }

    @staticmethod
    def workspace_metadata(workspace_id: str) -> Dict[str, Any]:
        """Return workspace metadata (purpose, permissions, host flag, risk).

        Reads ``vault workspaces/<id>/config.json``; missing files degrade to
        the ``general`` purpose with catalog-default permissions.  The risk
        score is computed at runtime by ``agent.config.risk_model``.  Pure
        read path — does not touch the config merge chain.
        """
        from agent.config.workspace_purpose import apply_purpose_preset
        from agent.config.risk_model import compute_workspace_risk

        cfg_path = (
            Path.home()
            / ".thoughtmachine"
            / "workspaces"
            / workspace_id
            / "config.json"
        )
        cfg: Dict[str, Any] = {}
        try:
            if cfg_path.exists():
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = raw
        except (OSError, json.JSONDecodeError):
            cfg = {}

        purpose = cfg.get("purpose", "general")
        allow_host_resources = bool(cfg.get("allow_host_resources", False))
        saved = cfg.get("permissions")
        if isinstance(saved, dict) and saved:
            permissions = {str(k): str(v) for k, v in saved.items()}
        else:
            permissions = apply_purpose_preset(purpose)

        try:
            risk = compute_workspace_risk(
                permissions=permissions,
                allow_host_resources=allow_host_resources,
                purpose=purpose,
            )
        except ImportError:
            risk = {"level": "low", "error": "risk_model unavailable"}

        return {
            "workspace_id": workspace_id,
            "purpose": purpose,
            "permissions": permissions,
            "allow_host_resources": allow_host_resources,
            "risk": risk,
        }

