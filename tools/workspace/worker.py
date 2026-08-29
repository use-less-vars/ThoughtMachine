"""tools.workspace.worker — thin re-export facade (W4).

The workspace worker subsystem lives in :mod:`tools.workspace.worker_thread`:
``WorkerThread``, the worker-side lifecycle helpers (``WorkerBusAdapter``,
``WorkerSessionLifecycle``, event-bus publishers, stale-observer and
job-registry accessors, permission-ceiling merging, registry aliases) and the
``Worker`` tool class, plus the module-level constants and optional-dependency
guards they use.  This module re-exports every public and test-facing name so
existing importers and tests keep working unchanged; it owns no state.

Backward-compat re-exports: constants from ``agent.config.defaults``,
container-label helpers from ``tools.workspace.worker_container`` and timeout
helpers from ``tools.workspace.worker_timeout``.
"""
from __future__ import annotations

# NOTE: ``time`` is imported at module scope so that
# ``mock.patch("tools.workspace.worker.time.monotonic")`` keeps working: the
# facade shares the single ``time`` module object with worker_thread.
import time  # noqa: F401  (re-exported for test patching)

from tools.workspace.worker_thread import (
    EventBus,
    EventType,
    WorkerBusAdapter,
    WorkerSessionLifecycle,
    WorkerThread,
    WORKER_DEFAULT_TRUNCATION,
    _WorkerAmbiguityError,
    _WorkerRegistry,
    _bus_registry_lock,
    _get_worker_job_registry,
    _get_worker_lifecycle_observer,
    _load_safe_defaults,
    _on_worker_stale,
    _publish_global_worker_event,
    _registry_lock,
    _resolve_worker_thread,
    _restrictive_merge,
    _worker_event_bus_registry,
    _worker_registry,
    create_event,
    global_event_bus,
    logger,
    register_worker_event_bus,
    unregister_worker_event_bus,
    _WORKER_BLOCKLIST,
    _WORKER_JOB_REGISTRY,
    _NO_SESSION_KEY,
    _SESSION_MAIN_PAUSED,
    WORKER_DEFAULT_MAX_TOKENS,
    WORKER_DEFAULT_MAX_RUNTIME_S,
    CAPABILITIES_AVAILABLE,
    resolve_workspace_id,
    _load_template_workers,
    _workspace_dir,
    StateBridge,
    EventProcessor,
    GATE_AVAILABLE,
    check_required_categories,
    NullEventBus,
    _NULL_EVENT_BUS,
    _build_tool_registry,
    _TOOL_REGISTRY,
    _get_tool_registry,
    shutdown_workers,
    get_worker_event_bus,
    get_worker_event_buses_for_session,
    Worker,
)

from agent.config.defaults import (
    SPAWN_QUEUE_TIMEOUT,
    QUERY_WAIT_GRACE_SECONDS,
    MAX_WORKERS_PER_SESSION,
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_STALE_AFTER_S,
    WORKER_DEFAULT_MAX_CONTAINERS,
    DEFAULT_WORKER_SYSTEM_PROMPT,
    WORKER_DEFAULT_TEMPERATURE,
)

from tools.workspace.worker_container import (
    _WORKER_CONTAINER_LABEL,
    _RESOURCE_CONTAINER_LABEL,
    cleanup_worker_containers,
    is_resource_container,
    is_worker_owned_container,
    worker_owner_label,
)

from tools.workspace.worker_timeout import (
    _worker_timeout_detected,
    _worker_query_wait_timeout,
    clamped_wait_for_job_timeout,
    wait_for_worker_exit,
)

__all__ = [
    "Worker",
    "WorkerThread",
    "WorkerBusAdapter",
    "WorkerSessionLifecycle",
    "EventBus",
    "EventType",
    "create_event",
    "global_event_bus",
    "logger",
    "shutdown_workers",
    "get_worker_event_bus",
    "get_worker_event_buses_for_session",
]
