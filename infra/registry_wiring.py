"""Container registry wiring — process-wide singleton access (Phase 3).

The registry is a per-process singleton: at most one *enabled* instance
(connected to the docker daemon) and one *disabled* instance (which never
touches docker) are ever constructed.  ``get_active_registry`` returns the
registry appropriate for the given session config; ``is_registry_active``
reports whether callers may delegate container lifecycle to it.

Flag semantics match ``container_registry.is_container_registry_enabled``:
``use_container_registry`` in the session config (default False).  Both
helpers are safe to call with ``None`` / ``{}`` configs — the disabled
instance is returned and no docker connection is attempted.
"""

from __future__ import annotations

import threading
from typing import Optional

from infra.container_registry import (
    ContainerRegistry,
    is_container_registry_enabled,
)

_lock = threading.Lock()
_registries: dict = {}


def get_active_registry(session_config: Optional[dict] = None) -> ContainerRegistry:
    """Return the process-wide registry for the given session config.

    Enabled configs share one registry constructed via ``docker.from_env()``
    on first use; disabled configs share a registry that never touches
    docker.  Thread-safe: construction happens at most once under a lock.
    A missing daemon is only noticed when the flag is actually on (the
    constructor degrades gracefully to ``_docker_available=False``).
    """
    enabled = is_container_registry_enabled(session_config)
    key = "enabled" if enabled else "disabled"
    with _lock:
        registry = _registries.get(key)
        if registry is None:
            if enabled:
                registry = ContainerRegistry(
                    docker_client=None, feature_flag_check=None
                )
            else:
                registry = ContainerRegistry(
                    docker_client=None, feature_flag_check=lambda: False
                )
            _registries[key] = registry
        return registry


def is_registry_active(session_config: Optional[dict] = None) -> bool:
    """True when the registry is enabled AND has a usable docker client.

    A config with ``use_container_registry`` on but no reachable daemon
    yields an enabled-but-unavailable registry — callers must fall back to
    the legacy manager path in that case.
    """
    registry = get_active_registry(session_config)
    if not registry.is_enabled():
        return False
    return bool(getattr(registry, "_docker_available", False))
