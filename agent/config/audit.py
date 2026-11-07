"""
Config audit logging (stdlib-only).

Writes JSONL records to ``~/.thoughtmachine/config_audit.jsonl`` (mode 0600)
describing configuration changes: old -> new effective config, whether a
restart is required, and the actual values injected into the running agent.

This module intentionally imports ONLY the Python standard library so it can
be imported both by agent-side code (``agent.core.agent``) and by web-ui side
code (``web_ui.backend.bridge`` / ``config_manager``) without import cycles or
heavy dependencies.

Safety contract:
  * ``log_config_audit`` NEVER raises -- failures are noted on stderr only.
  * Secret values (api_key, tokens, passwords, ...) are ALWAYS redacted before
    writing (see ``redact_config``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

AUDIT_LOG_PATH = Path.home() / '.thoughtmachine' / 'config_audit.jsonl'

VALID_SOURCES = frozenset({'user', 'api', 'env', 'worker', 'system'})

# Keys treated as sensitive, compared lower-cased with '-' folded to '_'.
SENSITIVE_KEYS = frozenset({
    'api_key', 'apikey', 'authorization', 'password', 'token', 'secret',
})

REDACTED = '<redacted>'


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    norm = key.lower().replace('-', '_').strip()
    return norm in SENSITIVE_KEYS or any(
        norm.endswith(suffix) for suffix in ('_api_key', '_apikey', '_token',
                                             '_secret', '_password')
    )


def redact_config(obj: Any) -> Any:
    """Recursively redact sensitive keys. Path -> str. Returns JSON-safe copy."""
    if isinstance(obj, dict):
        out: Dict[Any, Any] = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact_config(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_config(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # Fall back to a plain representation (model objects etc.).
    try:
        if hasattr(obj, 'model_dump'):
            return redact_config(obj.model_dump())
        if hasattr(obj, 'to_dict'):
            return redact_config(obj.to_dict())
        if hasattr(obj, '__dict__'):
            return redact_config(vars(obj))
    except Exception:
        pass
    try:
        return str(obj)
    except Exception:
        return None


def _to_plain(obj: Any) -> Any:
    """Convert a config object (pydantic model / dict / plain) to plain data."""
    if obj is None:
        return None
    try:
        if hasattr(obj, 'model_dump'):
            return redact_config(obj.model_dump())
        if hasattr(obj, 'to_dict'):
            return redact_config(obj.to_dict())
        if isinstance(obj, dict):
            return redact_config(obj)
    except Exception:
        pass
    return redact_config(obj)


def log_config_audit(
    source: str = 'system',
    component: str = 'config.change',
    old: Any = None,
    new: Any = None,
    restart_required: Optional[bool] = None,
    injected: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Append one JSONL record to the config audit log.

    Never raises: all failures are noted on stderr and swallowed so config
    application paths can never be broken by auditing.
    """
    if source not in VALID_SOURCES:
        source = 'system'
    record: Dict[str, Any] = {
        'timestamp': datetime.now().astimezone().isoformat(timespec='seconds'),
        'source': source,
        'component': component,
        'old': _to_plain(old),
        'new': _to_plain(new),
        'restart_required': bool(restart_required) if restart_required is not None else None,
        'injected': _to_plain(injected),
    }
    if extra:
        try:
            record['extra'] = redact_config(extra)
        except Exception:
            record['extra'] = None

    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if AUDIT_LOG_PATH.exists():
            try:
                os.chmod(str(AUDIT_LOG_PATH), 0o600)
            except Exception:
                pass
        else:
            try:
                fd = os.open(str(AUDIT_LOG_PATH), os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
            except Exception:
                open(str(AUDIT_LOG_PATH), 'a').close()
        with open(str(AUDIT_LOG_PATH), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, default=str) + '\n')
            fh.flush()
    except Exception as exc:  # pragma: no cover - defensive
        try:
            import sys
            print(f'[config_audit] failed to write audit record: {exc}',
                  file=sys.stderr)
        except Exception:
            pass


# Fields that force a restart when changed (mirrors agent.core.agent._can_hot_swap).
RESTART_BLOCKING_FIELDS = frozenset({
    'provider_type', 'model', 'api_key', 'base_url', 'system_prompt',
    'workspace_path', 'provider_config',
})


def restart_required_for(old_cfg: Any, new_cfg: Any) -> Optional[bool]:
    """
    Determine whether switching from old_cfg to new_cfg requires a restart.

    Mirrors the agent's hot-swap blocking fields. Returns True/False, or None
    if the configs cannot be compared (never raises).
    """
    try:
        if old_cfg is None and new_cfg is None:
            return None
        if old_cfg is None or new_cfg is None:
            return True

        def _as_dict(cfg: Any) -> Optional[Dict[str, Any]]:
            if hasattr(cfg, 'to_agent_config'):
                agent_cfg = cfg.to_agent_config()
                if hasattr(agent_cfg, 'model_dump'):
                    return agent_cfg.model_dump()
                if isinstance(agent_cfg, dict):
                    return dict(agent_cfg)
            if hasattr(cfg, 'model_dump'):
                return cfg.model_dump()
            if isinstance(cfg, dict):
                return dict(cfg)
            return None

        old_d = _as_dict(old_cfg)
        new_d = _as_dict(new_cfg)
        if old_d is None or new_d is None:
            return None
        # api_key may be excluded from model_dump; treat absence as unchanged.
        for field in RESTART_BLOCKING_FIELDS:
            old_v = old_d.get(field)
            new_v = new_d.get(field)
            if old_v != new_v:
                return True
        return False
    except Exception:
        return None
