"""
WebAgentBridge — Pure‑Python bridge wrapping the Agent class for WebSocket
frontends.  No Qt / PyQt dependencies.

Architecture
────────────
A single bridge instance manages one agent session.  The lifecycle is:

    bridge.start(query, config_dict)
        → creates Agent in a daemon thread
        → runs process_query() → yields events
        → calls event_callback(event_dict) for each event

    bridge.pause()   / bridge.resume()   / bridge.stop()
        → thread‑safe control signals (events + threading.Event)

    bridge.get_conversation()
        → returns current message list

The event_callback is the glue to the outside world (FastAPI WebSocket,
CLI, or any consumer).  It receives raw dicts from the Agent's process_query
generator, plus synthetic events injected by the bridge itself.

Mapping: Agent events → frontend events
────────────────────────────────────────
The bridge translates raw agent events into the simplified event protocol
that the React frontend expects (see frontend/src/App.jsx):

    Agent → process_query yields:    Frontend receives:
    ──────────────────────────────────────────────────
    execution_state_change          → state_changed
    token_update                   → tokens_updated + context_updated
    user_query / turn / tool_call
      / tool_result / final        → conversation_changed
    (synthetic bridge events)      → status_message


Call chain for container integrity re-sync
─────────────────────────────────────────────

The bridge calls ``_maybe_re_sync_container`` at two points to ensure the
running Docker container (if any) matches the current session permissions:

1. **apply_config** (line ~1253) — When the user updates session permissions
   (or any config) via the frontend.  The new permissions are applied to
   the running agent, then ``_maybe_re_sync_container`` checks whether the
   existing container still complies.

2. **load_session** (line ~1603) — When a saved session is loaded, because
   its stored permissions may differ from those the container was created
   with in a previous agent run.  This catches stale containers whose
   network/volume settings no longer match the loaded session's permissions.

The full resolution chain:

    _maybe_re_sync_container
      → verify_container_integrity (docker_executor.py)
           → _resolve_container_config_via_gate
                → get_workspace_capabilities + get_effective_permissions
                     (security/security_gate.py)
"""

from __future__ import annotations

import json
import os
import re
import threading
import queue
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import datetime
from pathlib import Path

from agent import Agent
from agent.config import AgentConfig
from agent.config.provider_profile import ProviderManager
from agent.config.service import create_agent_config_service
from agent.controller import AgentController
from agent.logging import log

# Import event system for security prompt forwarding
try:
    from agent.events import (
        global_event_bus, EventType, SecurityPromptEvent,
        WorkerSpawnedEvent, WorkerStatusEvent, WorkerCompletedEvent, WorkerErrorEvent,
        WorkerPartialResultEvent,
        TokenWarningEvent, BaseEvent,
        ToolCallEvent, ToolResultEvent, AssistantMessageEvent, WorkerMessageEvent,
    )
    EVENT_SYSTEM_AVAILABLE = True
except ImportError:
    global_event_bus = None
    EventType = None
    SecurityPromptEvent = None
    WorkerSpawnedEvent = None
    WorkerStatusEvent = None
    WorkerCompletedEvent = None
    WorkerErrorEvent = None
    WorkerPartialResultEvent = None
    TokenWarningEvent = None
    ToolCallEvent = None
    ToolResultEvent = None
    AssistantMessageEvent = None
    WorkerMessageEvent = None
    EVENT_SYSTEM_AVAILABLE = False

from thoughtmachine.workspace_registry import WorkspaceRegistry

from session.models import Session
from session.store import FileSystemSessionStore
from session.session_registry import SessionRegistry

# Worker lifecycle — graceful shutdown of worker threads on session close
try:
    from tools.workspace.worker import (
        shutdown_workers, get_worker_event_bus, register_worker_event_bus,
        unregister_worker_event_bus,
    )
    from tools.workspace.worker_registry import WorkerRegistry as _WorkerRegistry
    _worker_registry = _WorkerRegistry.get_instance()._worker_registry
    _registry_lock = _WorkerRegistry.get_instance()._registry_lock
    WORKER_BUS_AVAILABLE = True
except ImportError:
    shutdown_workers = None  # type: ignore
    get_worker_event_bus = None
    register_worker_event_bus = None
    unregister_worker_event_bus = None
    _worker_registry = None
    _registry_lock = None
    WORKER_BUS_AVAILABLE = False

from agent.config.session_config import SessionConfig
from agent.config.audit import log_config_audit, restart_required_for

from web_ui.backend.event_forwarder import EventForwarder, _active_tab_bridges
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import (
    SessionManager,
)

# ── Workspace ID cache ──────────────────────────────────────────────────────
# Cache mapping workspace path → workspace ID, built once and reused across
# session load calls within the same bridge instance.
_workspace_id_cache: Dict[str, str] = {}
_workspace_cache_lock = threading.Lock()


# ── Worker instance label helpers ──────────────────────────────────────────────────
# Instance labels mirror WorkerRegistry.instance_label semantics: "<name>" for
# instance 1 and "<name>#<N>" for N>1.  The worker registry keys per-worker
# EventBuses by these labels (get_event_buses_for_session), so the bridge keys
# its per-worker subscriptions the same way to keep instances distinct.


def _worker_instance_label(worker_name: str, instance_id: Optional[int]) -> str:
    """Build the instance label for a worker (mirrors WorkerRegistry.instance_label).

    None/1 → bare worker name, N>1 → ``<name>#<N>``.
    """
    if not instance_id or instance_id == 1:
        return worker_name
    return f"{worker_name}#{instance_id}"


def _worker_instance_parts(label: str) -> Tuple[str, int]:
    """Split an instance label into ``(base_name, instance_id)``.

    Labels are ``<name>`` for instance 1 and ``<name>#<N>`` for N>1.  A trailing
    ``#<int>`` suffix belongs to that instance; anything else is a bare name
    (instance 1).
    """
    base, sep, suffix = label.rpartition('#')
    if sep and suffix.isdigit():
        return base, int(suffix)
    return label, 1


def _normalise_workspace_root(path: Optional[str]) -> str:
    """Normalise a workspace path for ownership comparison.

    Both sides of every comparison (the requested target and the registered
    root) MUST go through this function, otherwise the comparison is
    meaningless.  It:

      * ``os.path.abspath`` to make the path absolute (CWD-relative input),
      * ``os.path.realpath`` to collapse symlinks, ``..`` and macOS-style
        ``/tmp`` -> ``/private/tmp`` indirection,
      * converts backslashes to forward slashes,
      * strips the trailing separator.

    ``os.path.realpath`` does NOT raise for a path that does not exist yet
    (strict=False on every supported Python) \u2014 a brand-new project path
    still comes back normalised, so callers never need to guard it.
    """
    if not path:
        return ""
    return os.path.realpath(os.path.abspath(path)).replace("\\", "/").rstrip("/")


def _build_workspace_id_cache() -> Dict[str, str]:
    """
    Build an in-memory cache mapping project roots to workspace IDs
    by reading from the centralised workspace registry.

    Called once on first resolution; subsequent calls return the cached dict.
    Roots are keyed through ``_normalise_workspace_root`` so that a request
    path and a registered root are compared after the *same* normalisation.
    """
    with _workspace_cache_lock:
        if _workspace_id_cache:
            return _workspace_id_cache
        try:
            registry = WorkspaceRegistry.get_default()
            for entry in registry.list_workspaces():
                normalised = _normalise_workspace_root(entry.root_path)
                if not normalised:
                    continue
                _workspace_id_cache[normalised] = entry.id
        except Exception:
            pass
        return _workspace_id_cache


def _resolve_workspace_id(workspace_path: str) -> Optional[str]:
    """
    Resolve a workspace *workspace_path* to its workspace ID.

    The request path is realpath-normalised *before* it can fail to match, so
    a symlink pointing at workspace B's root resolves to B (it must never
    fall through as a "brand-new path").  The registered roots are normalised
    the same way via ``_build_workspace_id_cache``.

    Checks the in-memory cache first (built from the registry on first call),
    then falls back to a direct registry lookup.  Returns the workspace ID
    or ``None`` if no match is found.
    """
    if not workspace_path:
        return None

    normalised = _normalise_workspace_root(workspace_path)
    if not normalised:
        return None

    # Check cache first (builds it on first call)
    cache = _build_workspace_id_cache()
    cached = cache.get(normalised)
    if cached is not None:
        return cached

    # Fall back to direct registry lookup (handles race with newly-registered
    # workspaces).  Hand the registry the realpath-normalised path: its own
    # ``_normalize_path`` only does abspath, so the caller must realpath.
    try:
        registry = WorkspaceRegistry.get_default()
        entry = registry.resolve_by_root(normalised)
        if entry is not None:
            _workspace_id_cache[normalised] = entry.id
            return entry.id
    except Exception:
        pass

    return None


def _registered_workspace_roots() -> Optional[List[Tuple[str, str]]]:
    """Return ``[(realpath_normalised_root, workspace_id), ...]``.

    Same source of truth as ``_resolve_workspace_id``:
    ``WorkspaceRegistry.get_default().list_workspaces()``.  Returns ``None``
    when the registry is unavailable/corrupt \u2014 callers treat that as
    fail-closed (refuse), never as "no other workspaces exist".
    """
    try:
        registry = WorkspaceRegistry.get_default()
        roots: List[Tuple[str, str]] = []
        for entry in registry.list_workspaces():
            normalised = _normalise_workspace_root(entry.root_path)
            if normalised:
                roots.append((normalised, entry.id))
        return roots
    except Exception:
        return None


def _path_is_same_or_inside(path: str, root: str) -> bool:
    """True when *path* equals *root* or is contained by it.

    Component-aware: uses ``os.path.commonpath`` so ``/root-b-sibling`` is
    NOT considered inside ``/root-b`` (a bare string-prefix test would).
    Both arguments are expected to be already normalised via
    ``_normalise_workspace_root``.
    """
    if not path or not root:
        return False
    if path == root:
        return True
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Mixed absolute/relative, or different drives on Windows.
        return False


def _ownership_refusal_ids(
    target_workspace_id: Optional[str],
    bound_workspace_id: Optional[str],
) -> Optional[str]:
    """Single source of truth for the workspace-ownership rule, over IDs.

    Returns ``None`` when the target is ALLOWED, otherwise a human-readable
    refusal reason.  ``_ownership_refusal`` (path in) and
    ``_ownership_refusal_by_id`` (session id in) are adapters that resolve
    their input to a workspace ID and delegate here, so the rule cannot drift
    between call sites.

    Rule (D3: ``None`` fails closed \u2014 a side that cannot name its workspace
    proves nothing, so it is refused instead of waved through):
      1. REFUSE when ``bound_workspace_id`` is None (no ownership claim).
      2. REFUSE when ``target_workspace_id`` is None (nothing to compare).
      3. ALLOW when the two IDs are equal (same workspace).
      4. REFUSE otherwise (a different workspace).

    The comparison is identity, not location.  A workspace keeps its ID when
    its root path is renamed, so the rule survives a rename by construction;
    no registry lookup happens here (resolution belongs to the adapters).
    """
    if bound_workspace_id is None:
        return (
            "connection has no workspace scope, so target workspace "
            f"{target_workspace_id!r} cannot be proven to belong to it"
        )

    if target_workspace_id is None:
        return f"target workspace is unspecified, expected {bound_workspace_id!r}"

    if target_workspace_id == bound_workspace_id:
        return None

    return (
        f"target workspace {target_workspace_id!r} is not the bound workspace "
        f"{bound_workspace_id!r}"
    )


def _ownership_refusal_by_id(
    session_id: str,
    bound_workspace_id: Optional[str],
) -> Optional[str]:
    """Session-shaped adapter over ``_ownership_refusal_ids``.

    Resolves ``session_id`` to the workspace its stored record belongs to, then
    applies the id-form rule.  An unreadable or missing record is refused (fail
    closed): a session that cannot be found proves nothing about ownership.

    No D3 call site uses this \u2014 it is D2's (binding-step) hook.
    """
    try:
        session = FileSystemSessionStore().load_session(session_id)
    except Exception as exc:  # noqa: BLE001 - refusal, not an exception, is the contract
        return f"session {session_id!r} could not be read: {exc}"

    if session is None:
        return f"session {session_id!r} not found, so its workspace cannot be proven"

    return _ownership_refusal_ids(session.workspace_id, bound_workspace_id)


def _ownership_refusal(target_path: str, bound_workspace_id: Optional[str]) -> Optional[str]:
    """Single source of truth for the workspace-ownership rule.

    Returns ``None`` when the target is ALLOWED, otherwise a human-readable
    refusal reason.  Both ``apply_config`` and ``set_project`` call this, so
    the rule cannot drift between them.

    Rule (D3: an unscoped side is refused, never waved through):
      1. ``resolved = _resolve_workspace_id(target_path)``
      2. ``resolved is not None`` (a registered workspace): the id-form rule
         decides \u2014 ``_ownership_refusal_ids(resolved, bound_workspace_id)``,
         so a different workspace \u2014 and an unscoped connection \u2014 is refused.
      3. REFUSE when ``bound_workspace_id`` is None (no claim, no allow).
      4. ``resolved is None`` (not a registered workspace root): ALLOW iff the
         realpath'd target is neither equal to nor inside the realpath'd root
         of ANY OTHER registered workspace.  A brand-new path outside every
         root is allowed; a path inside \u2014 or symlinked into \u2014 another
         workspace's tree is refused.
    """
    if bound_workspace_id is None:
        # D3: None fails closed.  This branch used to return None (ALLOW): an
        # unscoped connection passed every target through the gate.
        return (
            "connection has no workspace scope, so target "
            f"{target_path!r} cannot be proven to belong to it"
        )

    resolved = _resolve_workspace_id(target_path)

    if resolved is not None:
        # Registered target: the id-form primitive decides (identity, not path).
        return _ownership_refusal_ids(resolved, bound_workspace_id)

    # Unregistered target \u2014 allow only if it cannot reach another workspace.
    roots = _registered_workspace_roots()
    if roots is None:
        return (
            "workspace registry unavailable \u2014 cannot prove "
            f"{target_path!r} is outside every other workspace"
        )

    target_norm = _normalise_workspace_root(target_path)
    if not target_norm:
        return f"target path {target_path!r} is empty or unresolvable"

    for other_root, other_id in roots:
        if other_id == bound_workspace_id:
            continue
        if _path_is_same_or_inside(target_norm, other_root):
            return (
                f"target {target_path!r} is inside workspace {other_id!r} "
                f"(root {other_root!r}), not outside every other workspace"
            )

    return None


def _validate_workspace_id(workspace_id: Optional[str]) -> Optional[str]:
    """Validate a workspace ID supplied by an external caller.

    Raises ``ValueError`` for non-string, empty, path-like (``/``, ``\\``,
    ``.``, ``..``) or NUL-containing values.  Returns the ID unchanged on
    success.
    """
    if workspace_id is None:
        return None
    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id must be a non-empty string")
    if workspace_id in ('/', '\\', '.', '..'):
        raise ValueError(f"workspace_id {workspace_id!r} is not a valid workspace ID")
    if '\x00' in workspace_id:
        raise ValueError("workspace_id must not contain NUL bytes")
    return workspace_id


def _worker_is_running_async_job(thread: Any) -> bool:
    """Return True when *thread* is currently executing a submit_query (async) job.

    Async jobs are enqueued by ``action='submit_query'`` as ``(job_id, query,
    None)`` tuples; the run loop mirrors them onto ``_current_query_id`` (set)
    while ``_current_reply_queue`` stays None (a synchronous ``send_query``
    carries a private reply queue instead).  The ``status == "busy"`` guard
    excludes idle workers whose ``_current_query_id`` is never reset between
    queries, so only a worker that is actively running an async job is skipped.
    """
    return (
        getattr(thread, "status", None) == "busy"
        and getattr(thread, "_current_query_id", None) is not None
        and getattr(thread, "_current_reply_queue", None) is None
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Bridge class
# ══════════════════════════════════════════════════════════════════════════════



class WebAgentBridge:
    """
    Thread‑safe bridge that runs one Agent session and emits events through
    a callback.  Designed to be driven by a FastAPI WebSocket endpoint.
    """

    def __init__(self, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
                 session_store: Optional[FileSystemSessionStore] = None):
        self._agent: Optional[Agent] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._processing = False

        # Event primitives
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()          # start in "running" (not paused)

        # Query queue — the agent thread pulls from this
        self._query_queue: queue.Queue = queue.Queue()

        # Callbacks — called from the agent thread for every event.
        # Stored as a dict keyed by WebSocket id (or other unique key)
        # so multiple frontend connections can receive the same events.
        # Event forwarder — owns callback registry, event mapping, and emission
        self._forwarder = EventForwarder(self, event_callback)

        # Reconnect buffer — worker events published while no WebSocket callback
        # is registered (e.g. F5 reload gap) are kept here and replayed to the
        # next connection via _flush_worker_event_buffer() in set_event_callback().
        self._worker_event_buffer: List[Dict[str, Any]] = []
        self._worker_event_buffer_max = 100
        self._worker_event_buffer_lock = threading.Lock()

        # Track last known controller busy state for state_changed is_running
        self._last_state_busy: Optional[bool] = None

        # Current config (for get_config / get_conversation)
        self._session_config: Optional[SessionConfig] = None
        self._config_manager = ConfigManager()
        self._session_id: Optional[str] = None
        self._workspace_path: Optional[str] = None

        # Controller integration (optional — used when Web UI wants to
        # reuse the existing AgentController instead of creating an Agent directly)
        self._controller: Optional[AgentController] = None

        # Session persistence — use shared store if provided, otherwise create one
        self._session_store = session_store if session_store is not None else FileSystemSessionStore()
        self._session_manager = SessionManager(self._session_store, self._config_manager)
        self._session: Optional[Session] = None
        self._loaded_session: Optional[Session] = None
        self._workspace_id: Optional[str] = None

        # Subscribe to global event bus for security prompt events
        self._security_subscription = None
        # Subscribe to global event bus for worker lifecycle events
        self._worker_spawned_sub = None
        self._worker_status_sub = None
        self._worker_completed_sub = None
        self._worker_error_sub = None
        self._worker_partial_result_sub = None
        self._worker_token_warning_sub = None
        self._worker_message_sub = None
        # Per-worker EventBus subscriptions, keyed by instance label:
        #   worker_label -> {
        #       'bus': <the EventBus object the handlers were registered on>,
        #       'session_id': <session id used at subscribe time>,
        #       'instance_id': <int>,
        #       'subs': {event_type: (event_type, handler_fn)},
        #   }
        # The bus object is stored so unsubscription always targets the bus the
        # handlers actually live on — the registry may already hold a NEW bus
        # after a worker respawn, and unsubscribing from that one would silently
        # no-op while leaving the bridge bound to the dead bus.
        self._worker_bus_subs: Dict[str, Dict[str, Any]] = {}
        # Track last context_length sent per worker for context_updated dedup
        self._last_context_updated: Dict[str, str] = {}
        self._subscribe_to_security_events()
        self._subscribe_to_worker_events()

        # Track whether close_session() was called cleanly — used by server.py
        # to avoid re-saving on abrupt disconnect (data loss guard).
        self._cleanly_closed = False

        # Config queued while the controller was busy (deferred apply).
        # Populated by apply_config_queued(); consumed in _on_controller_event
        # as soon as the controller becomes idle (last write wins).  Cleared
        # in stop() so a torn-down bridge never leaks a pending config.
        self._pending_config = None

        # Uncapped persistence snapshot of the session config.  load_session()
        # caps the LIVE config through the workspace permission ceiling, and
        # persisting that capped dump would permanently collapse stored full
        # grants on the next switch-away / shutdown save.  This snapshot keeps
        # the uncapped operator view for STORAGE only; the live capped config
        # is never touched.  (session_permissions is stripped before storage --
        # the REST-owned permission sidecar is the canonical grant store.)
        self._session_config_persist_dump: Optional[Dict[str, Any]] = None

        # Persisted worker contexts loaded from workspace on session load
        self._persisted_workers: Dict[str, Dict[str, Any]] = {}
        # Track session conversation version for efficient history sync
        self._history_version: int = 0

    def _bind_workspace(self, workspace_id: Optional[str], mode: str) -> Optional[str]:
        """The ONE authority that may set ``self._workspace_id``.

        Every workspace binding in the load / create / switch flow routes here,
        so the rule is auditable in one place instead of scattered writes.

        Modes (the only two that exist):
          ``bootstrap`` \u2014 first bind only.  An unbound bridge adopts the id; an
              already-bound bridge is left alone when the ids match and REFUSED
              otherwise, because a bootstrap is not a scope change.
          ``switch``    \u2014 an explicit scope change.  Unbound adopts (a switch can
              bootstrap), equal is idempotent, and a real change is decided by
              ``_ownership_refusal_ids`` so switch mode inherits whatever the
              ownership authority allows (today: always refuse).

        Returns ``None`` on success, otherwise a human-readable refusal reason.
        ``self._workspace_id`` is never reset to ``None`` here: a refusal leaves
        the existing binding untouched (fail closed, not fail open).
        """
        if workspace_id is None:
            return "cannot bind workspace: no workspace id was supplied"

        if mode not in ("bootstrap", "switch"):
            return (
                f"cannot bind workspace {workspace_id!r}: unknown mode {mode!r} "
                f"(expected 'bootstrap' or 'switch')"
            )

        # Both modes bootstrap an unbound bridge and no-op on the same id.
        if self._workspace_id is None:
            self._workspace_id = workspace_id
            return None
        if self._workspace_id == workspace_id:
            return None

        if mode == "bootstrap":
            return (
                f"bootstrap cannot rebind: already bound to "
                f"{self._workspace_id!r}, refusing to rebind to {workspace_id!r}"
            )

        # mode == "switch": the ownership authority owns the verdict.
        return _ownership_refusal_ids(workspace_id, self._workspace_id)

    # ── Security event subscription ───────────────────────────────────────────

    def _subscribe_to_security_events(self) -> None:
        """
        Subscribe to SECURITY_PROMPT events from the global event bus.

        When the tool executor publishes a SecurityPromptEvent (because a
        tool requires a permission set to ``ask``), this handler forwards
        it to the frontend via the WebSocket event callback so the user
        can see a dialog and approve or deny.
        """
        if not EVENT_SYSTEM_AVAILABLE or global_event_bus is None:
            log('WARNING', 'server.bridge', 'Event system unavailable — security prompts not forwarded')
            return

        def _security_prompt_handler(event: SecurityPromptEvent) -> None:
            """Forward a SecurityPromptEvent to the frontend."""
            if not self._forwarder._callbacks:
                log('DEBUG', 'server.bridge', 'No event callbacks — dropping security prompt')
                return
            data = event.data or {}
            # Filter: only handle events for this bridge's session
            if data.get('session_id') != self._session_id:
                log('DEBUG', 'server.bridge',
                    f'Ignoring security prompt for different session '
                    f'(event session_id={data.get("session_id")}, '
                    f'bridge session_id={self._session_id})')
                return
            event_dict = {
                'type': 'security_prompt',
                'request_id': data.get('request_id', ''),
                'agent_id': data.get('agent_id', ''),
                'tool_name': data.get('tool_name', ''),
                'capabilities': data.get('capabilities', []),
                'arguments': data.get('arguments', {}),
                'session_id': data.get('session_id', self._session_id),
                'description': (
                    f"Tool '{data.get('tool_name', '?')}' requires: "
                    f"{', '.join(data.get('capabilities', []))}"
                ),
            }
            for cb in list(self._forwarder._callbacks.values()):
                try:
                    cb(event_dict)
                except Exception as exc:
                    log('ERROR', 'server.bridge',
                        f'Failed to forward security prompt: {exc}')

        self._security_subscription = global_event_bus.subscribe(
            EventType.SECURITY_PROMPT, _security_prompt_handler
        )
        log('INFO', 'server.bridge', 'Subscribed to SECURITY_PROMPT events')

    def _subscribe_to_worker_events(self) -> None:
        """
        Subscribe to worker lifecycle events from the global event bus and
        forward them to frontend WebSocket clients as worker:* messages.

        Also manages per-worker EventBus subscriptions for detailed events
        (tool_call, tool_result, token_warning, etc.) by subscribing on
        WORKER_SPAWNED and unsubscribing on WORKER_COMPLETED / WORKER_ERROR.
        """
        if not EVENT_SYSTEM_AVAILABLE or global_event_bus is None:
            log('WARNING', 'server.bridge', 'Event system unavailable - worker events not forwarded')
            return

        def _make_handler(event_cls):
            def _handler(event: event_cls) -> None:
                data = event.data or {}
                log('DEBUG', 'pipeline.bridge',
                    f"[TOKEN_PIPELINE] bridge global bus handler: type={event.type.value!r}, "
                    f"worker_name={data.get('worker_name', '?')}, "
                    f"session_id={data.get('session_id', '?')}, "
                    f"bridge_session_id={self._session_id}")
                # Only forward events for this bridge's session
                if data.get('session_id') and data['session_id'] != self._session_id:
                    log('DEBUG', 'pipeline.bridge',
                        f"Global bus handler: SKIPPING event for different session "
                        f"(event={data.get('session_id')}, bridge={self._session_id})")
                    return
                event_dict = {
                    'type': f'worker:{event.type.value}',
                    'worker_name': data.get('worker_name', ''),
                    'instance_id': data.get('instance_id'),
                    'instance_label': data.get('instance_label'),
                    'timestamp': event.metadata.timestamp.isoformat(),
                    'data': data,
                }
                # Buffer before the drop gate so lifecycle events (worker_status,
                # worker_message) published during the F5 reconnect gap are replayed.
                self._buffer_worker_event(event_dict)
                if not self._forwarder._callbacks:
                    log('DEBUG', 'pipeline.bridge',
                        f"Global bus handler: no event_callbacks registered, "
                        f"buffered worker:{event.type.value} (replay on next connect)")
                    return
                for cb in list(self._forwarder._callbacks.values()):
                    try:
                        cb(event_dict)
                    except Exception as exc:
                        log('ERROR', 'server.bridge',
                            f'Failed to forward worker event: {exc}')
            return _handler

        self._worker_spawned_sub = global_event_bus.subscribe(
            EventType.WORKER_SPAWNED, self._on_worker_spawned
        )
        self._worker_status_sub = global_event_bus.subscribe(
            EventType.WORKER_STATUS, _make_handler(WorkerStatusEvent)
        )
        self._worker_completed_sub = global_event_bus.subscribe(
            EventType.WORKER_COMPLETED, self._on_worker_completed
        )
        self._worker_error_sub = global_event_bus.subscribe(
            EventType.WORKER_ERROR, self._on_worker_error
        )
        self._worker_partial_result_sub = global_event_bus.subscribe(
            EventType.WORKER_PARTIAL_RESULT, _make_handler(WorkerPartialResultEvent)
        )
        self._worker_token_warning_sub = global_event_bus.subscribe(
            EventType.TOKEN_WARNING, self._on_worker_token_warning
        )
        self._worker_message_sub = global_event_bus.subscribe(
            EventType.WORKER_MESSAGE, _make_handler(BaseEvent)
        )

        log('INFO', 'server.bridge', 'Subscribed to worker lifecycle events')

        # Discover already-running workers (late-arriving bridge guard)
        # This handles the case where workers were spawned before this bridge
        # subscribed to WORKER_SPAWNED (e.g., second tab, reconnection).
        if self._session_id:
            self._discover_existing_workers(self._session_id)

    # Bounded retry for resolving a worker's per-worker EventBus right after
    # WORKER_SPAWNED.  Deliberately bounded (5 x 250 ms ~= 1.25 s worst case):
    # this runs on the event-bus publishing thread and must never block it
    # indefinitely when a worker's bus genuinely never appears.
    _WORKER_BUS_RESOLVE_ATTEMPTS = 5
    _WORKER_BUS_RESOLVE_DELAY = 0.25

    @staticmethod
    def _stored_entry_is_stale(entry: Any, current_bus: Any) -> bool:
        """True only when *current_bus* is a different bus than the stored one.

        A missing/undeterminable *current_bus* (``None``) counts as "not stale"
        so a live subscription is never torn down on a mere lookup miss.
        """
        if current_bus is None:
            return False
        if not isinstance(entry, dict) or 'bus' not in entry:
            return False
        return entry.get('bus') is not current_bus

    def _worker_bus_is_stale(self, worker_label: str) -> bool:
        """True when the registry now holds a *different* bus for this worker.

        Only a positive mismatch (the registry has a bus object that is not the
        one we subscribed on) counts as stale; if the registry has no bus at all
        we cannot tell, so the existing subscription is kept as-is.
        """
        entry = self._worker_bus_subs.get(worker_label)
        if not isinstance(entry, dict) or 'bus' not in entry:
            return False
        if not (WORKER_BUS_AVAILABLE and get_worker_event_bus is not None):
            return False
        base_name, _ = _worker_instance_parts(worker_label)
        if not base_name:
            return False
        session_id = entry.get('session_id') or self._session_id or ''
        instance_id = entry.get('instance_id') or 1
        try:
            current_bus = get_worker_event_bus(
                session_id, base_name, instance_id=instance_id
            )
        except Exception:
            return False
        return self._stored_entry_is_stale(entry, current_bus)

    def _discover_existing_workers(self, session_id: str) -> None:
        """Discover and subscribe to per-worker EventBuses for already-running workers.

        Handles late-arriving bridge: second tab, reconnection, or deferred bridge start
        where WORKER_SPAWNED events were already published before this bridge existed.
        Workers discovered here are subscribed the same way as via _on_worker_spawned.
        """
        if not session_id:
            return
        try:
            from tools.workspace.worker_registry import WorkerRegistry as _WorkerRegistry
            buses = _WorkerRegistry.get_instance().get_event_buses_for_session(session_id)
            for worker_label, bus in buses.items():
                stored_entry = self._worker_bus_subs.get(worker_label)
                if stored_entry is not None:
                    if not self._stored_entry_is_stale(stored_entry, bus):
                        log('DEBUG', 'pipeline.bridge',
                            f"[DISCOVERY] Already subscribed to {worker_label}, skipping")
                        continue
                    # The registry now holds a different bus for this worker
                    # (respawn) — detach from the dead bus before re-binding.
                    log('INFO', 'server.bridge',
                        f"[DISCOVERY] Per-worker bus for {worker_label} was replaced "
                        f"\u2014 re-binding to the current bus")
                    self._unsubscribe_worker_bus(worker_label)
                log('INFO', 'server.bridge',
                    f"[DISCOVERY] Found existing worker: {worker_label} \u2014 subscribing to per-worker bus")
                self._subscribe_to_worker_bus(
                    worker_label, bus,
                    session_id=session_id,
                    instance_id=_worker_instance_parts(worker_label)[1],
                )
        except ImportError:
            log('DEBUG', 'server.bridge',
                "[DISCOVERY] get_worker_event_buses_for_session not available")
        except Exception as e:
            log('WARNING', 'server.bridge',
                f"[DISCOVERY] Failed to discover existing workers: {e}")

    def _on_worker_token_warning(self, event: TokenWarningEvent) -> None:
        """Forward non-worker token warnings to the frontend as system notifications.

        Worker-sourced token warnings (source='worker') are already forwarded
        via the per-worker EventBus subscription (Path A in _make_bus_handler)
        as 'worker:token_warning' events.  This global bus handler (Path B)
        only forwards token warnings from the main agent (non-worker context)
        so they render as 'worker:system_notification'.  Skipping worker-sourced
        events eliminates the duplicate bubble problem.
        """
        if not self._forwarder._callbacks:
            return
        source = event.metadata.source if event.metadata else ""
        if source == "worker":
            return  # worker-sourced → already forwarded via per-worker bus
        data = event.data or {}
        # Only forward events for this bridge's session
        if data.get('session_id') and data['session_id'] != self._session_id:
            return
        log('DEBUG', 'core.token', f"Bridge forward: worker={event.data.get('worker_name','?') if event.data else '?'} tokens={event.data.get('token_count','?') if event.data else '?'}")
        event_dict = {
            'type': 'worker:system_notification',
            'worker_name': data.get('worker_name', ''),
            'session_id': event.metadata.session_id if event.metadata else None,
            'timestamp': datetime.datetime.now().isoformat(),
            'response': {
                'type': 'token_warning',
                'message': data.get('warning_message', ''),
                'token_count': data.get('token_count', 0),
            },
        }

        for cb in list(self._forwarder._callbacks.values()):
            try:
                cb(event_dict)
            except Exception as exc:
                log('ERROR', 'server.bridge',
                    f'Failed to forward worker token warning: {exc}')
                continue

    def _unsubscribe_security_events(self) -> None:
        """Unsubscribe from security events."""
        if self._security_subscription is not None:
            try:
                # The subscription might be a registration handle;
                # try to remove it if the bus provides that API.
                if hasattr(global_event_bus, 'unsubscribe'):
                    global_event_bus.unsubscribe(self._security_subscription)
            except Exception:
                pass
            self._security_subscription = None

    def _unsubscribe_worker_events(self) -> None:
        """Unsubscribe from worker lifecycle events and all per-worker buses."""
        worker_subs = [
            ('_worker_spawned_sub',),
            ('_worker_status_sub',),
            ('_worker_completed_sub',),
            ('_worker_error_sub',),
            ('_worker_partial_result_sub',),
            ('_worker_token_warning_sub',),
            ('_worker_message_sub',),
        ]
        for attr_name, in worker_subs:
            sub = getattr(self, attr_name, None)
            if sub is not None:
                try:
                    if hasattr(global_event_bus, 'unsubscribe'):
                        global_event_bus.unsubscribe(sub)
                except Exception:
                    pass
                setattr(self, attr_name, None)

        # Clean up all per-worker bus subscriptions (keyed by instance label)
        for worker_label in list(self._worker_bus_subs.keys()):
            self._unsubscribe_worker_bus(worker_label)
        self._worker_bus_subs.clear()

    # ── Per-worker EventBus subscription management ───────────────────────────

    def _on_worker_spawned(self, event: WorkerSpawnedEvent) -> None:
        """
        Forward worker spawned event to frontend and subscribe to
        the per-worker EventBus for detailed events.

        Subscription to the per-worker bus happens *before* the
        event_callbacks guard so that detailed events are captured
        even if no WebSocket client is connected yet (race condition fix).
        """
        data = event.data or {}

        # Only handle events for this bridge's session
        worker_name = data.get('worker_name', '')
        instance_id = data.get('instance_id') or 1
        worker_label = _worker_instance_label(worker_name, instance_id)
        session_id = data.get('session_id', self._session_id or '')
        log('DEBUG', 'pipeline.bridge',
            f"[TOKEN_PIPELINE] bridge._on_worker_spawned: worker_name={worker_name!r}, "
            f"event_session_id={data.get('session_id', 'N/A')!r}, "
            f"bridge_session_id={self._session_id!r}, "
            f"resolved_session_id={session_id!r}, "
            f"n_callbacks={len(self._forwarder._callbacks)}, "
            f"WORKER_BUS_AVAILABLE={WORKER_BUS_AVAILABLE}, "
            f"get_worker_event_bus={'SET' if get_worker_event_bus is not None else 'NONE'}")
        if data.get('session_id') and data['session_id'] != self._session_id:
            log('DEBUG', 'pipeline.bridge',
                f"_on_worker_spawned: SKIPPING (session mismatch)")
            return

        # A respawned worker gets a brand-new per-worker EventBus, while our
        # stored subscriptions still live on the dead one.  Detect that and
        # detach (from the stored bus, never the registry's new one) before the
        # duplicate-subscription guard below, so we re-bind to the current bus.
        if self._worker_bus_is_stale(worker_label):
            log('INFO', 'server.bridge',
                f"_on_worker_spawned: per-worker bus for {worker_label} was replaced "
                f"— re-binding to the current bus")
            self._unsubscribe_worker_bus(worker_label)

        # Guard against duplicate subscriptions — if already subscribed for this
        # worker, skip re-subscribing to avoid duplicate events on the frontend.
        if worker_label in self._worker_bus_subs:
            log('DEBUG', 'pipeline.bridge',
                f"_on_worker_spawned: already subscribed for {worker_label}, "
                f"skipping duplicate subscription")
        else:
            # Subscribe to per-worker EventBus for detailed events
            # This must happen regardless of _event_callbacks to avoid
            # a race where the event arrives before any WebSocket client connects.
            if worker_name and WORKER_BUS_AVAILABLE and get_worker_event_bus is not None:
                worker_bus = self._resolve_worker_bus_with_retry(
                    session_id, worker_name, instance_id
                )
                if worker_bus is not None:
                    log('DEBUG', 'pipeline.bridge',
                        f"_on_worker_spawned: found per-worker bus for {worker_label}, "
                        f"subscribing to detailed events")
                    self._subscribe_to_worker_bus(
                        worker_label, worker_bus,
                        session_id=session_id, instance_id=instance_id,
                    )
                else:
                    log('WARNING', 'server.bridge',
                        f'Per-worker EventBus for {worker_name} (session={session_id}) not found '
                        f'— detailed events (tool_call, tool_result, assistant_message) '
                        f'will NOT be forwarded to the frontend. '
                        f'This may be a race condition: worker bus registered before bridge subscribes.')
                    # DIAG: additional diagnostics for missing bus
                    log('WARNING', 'pipeline.bridge',
                        f"_on_worker_spawned: worker_bus is None for {worker_name} "
                        f"(session_id={session_id!r}). Check that register_worker_event_bus() "
                        f"was called BEFORE the WORKER_SPAWNED event was published.")
                    # Degrade visibly: one notification so the panel is not
                    # silently missing this worker's tool calls.
                    self._notify_worker_events_unavailable(
                        worker_name, instance_id, worker_label, session_id
                    )

        # Build the event dict and buffer it BEFORE the no-callback drop gate so
        # a worker spawned during the F5 reconnect gap is replayed on next connect.
        event_dict = {
            'type': f'worker:{event.type.value}',
            'worker_name': worker_name,
            'instance_id': instance_id,
            'instance_label': worker_label,
            'timestamp': event.metadata.timestamp.isoformat(),
            'data': data,
        }
        self._buffer_worker_event(event_dict)
        # Forward the event to frontend callbacks (if any)
        if not self._forwarder._callbacks:
            log('DEBUG', 'pipeline.bridge',
                f"_on_worker_spawned: no event_callbacks, buffered worker_spawned "
                f"(worker_bus subscription still happened if bus was found)")
            return
        for cb in list(self._forwarder._callbacks.values()):
            try:
                cb(event_dict)
            except Exception as exc:
                log('ERROR', 'server.bridge',
                    f'Failed to forward worker event: {exc}')

    def _subscribe_to_worker_bus(self, worker_label: str, worker_bus: Any,
                                session_id: Optional[str] = None,
                                instance_id: Optional[int] = None) -> None:
        """
        Subscribe to a worker instance's per-worker EventBus for detailed
        real-time events (tool_call, tool_result, token_warning, worker_message,
        etc.).  *worker_label* is the instance label (``<name>`` or ``<name>#<N>``)
        that this bridge uses to key per-worker subscriptions.

        The bus object, the session id and the instance id are stored alongside
        the subscriptions so ``_unsubscribe_worker_bus`` can detach from exactly
        the bus it subscribed on (see ``_worker_bus_subs``).
        """
        if not EVENT_SYSTEM_AVAILABLE or EventType is None:
            log('DEBUG', 'pipeline.bridge',
                f"_subscribe_to_worker_bus: cannot subscribe for {worker_label}, "
                f"EVENT_SYSTEM_AVAILABLE={EVENT_SYSTEM_AVAILABLE}, EventType={'SET' if EventType else 'NONE'}")
            return

        # DIAG: Log what event types we plan to subscribe to
        subscribed_types = ['tool_call', 'tool_call_start', 'tool_result',
                            'worker_message', 'assistant_message',
                            'context_updated', 'context_cleared', 'context_summarized',
                            'token_recovery', 'token_warning', 'turn_warning', 'time_warning',
                            'user_message', 'system_notification',
                            'worker_paused', 'worker_resumed',
                            ]
        log('DEBUG', 'pipeline.bridge',
            f"_subscribe_to_worker_bus [worker={worker_label}]: "
            f"subscribing to {len(subscribed_types)} event types: {subscribed_types}")

        subs = {}

        def _make_bus_handler(original_type: str):
            def _handler(event: Any) -> None:
                data = event.data or {}
                # [PIPELINE:HOPS] Per-worker bus handler entry
                log('DEBUG', 'pipeline.hops',
                    f"[PIPELINE:HOPS] bridge._make_bus_handler entry [worker={worker_label}]: "
                    f"original_type={original_type!r}, "
                    f"data_keys={list(data.keys())}, "
                    f"n_callbacks={len(self._forwarder._callbacks)}")
                # Special handling for tokens_updated - flatten to top-level
                # so the frontend's existing 'tokens_updated' handler picks it up.
                if original_type == 'tokens_updated':
                    event_dict = {
                        'type': 'worker:tokens_updated',
                        'input': data.get('total_input', 0),
                        'output': data.get('total_output', 0),
                        'source': 'worker',
                        'worker_name': data.get('worker_name', worker_label),
                        'instance_id': data.get('instance_id', _worker_instance_parts(worker_label)[1]),
                        'instance_label': data.get('instance_label', worker_label),
                    }
                elif original_type == 'context_updated':
                    context_length_val = data.get('context_length', 0)
                    # Format to match frontend display: X.XK for values >= 1000
                    if context_length_val >= 1000:
                        display_str = f"{context_length_val / 1000:.1f}K"
                    else:
                        display_str = str(context_length_val)
                    # Event-level dedup: skip if same formatted display string already forwarded
                    last_display = self._last_context_updated.get(worker_label)
                    if last_display is not None and display_str == last_display:
                        log('DEBUG', 'pipeline.bridge',
                            f"Dedup context_updated for {worker_label}: "
                            f"skipping duplicate context_length={context_length_val} "
                            f"(display={display_str!r})")
                        return
                    self._last_context_updated[worker_label] = display_str
                    log('DEBUG', 'pipeline.hops',
                        f"[PIPELINE:HOPS] bridge forwarding context_updated: "
                        f"worker={worker_label} "
                        f"context_length={data.get('context_length', '?')}")
                    event_dict = {
                        'type': 'worker:context_updated',
                        'context_length': context_length_val,
                        'source': 'worker',
                        'worker_name': data.get('worker_name', worker_label),
                        'instance_id': data.get('instance_id', _worker_instance_parts(worker_label)[1]),
                        'instance_label': data.get('instance_label', worker_label),
                        'timestamp': (
                            event.metadata.timestamp.isoformat()
                            if hasattr(event, 'metadata') and event.metadata
                            else datetime.datetime.now().isoformat()
                        ),
                    }
                elif original_type == 'context_summarized':
                    event_dict = {
                        'type': 'worker:context_summarized',
                        'worker_name': data.get('worker_name', worker_label),
                        'instance_id': data.get('instance_id', _worker_instance_parts(worker_label)[1]),
                        'instance_label': data.get('instance_label', worker_label),
                        'message': data.get('message', 'Context has been summarized'),
                        'timestamp': (
                            event.metadata.timestamp.isoformat()
                            if hasattr(event, 'metadata') and event.metadata
                            else datetime.datetime.now().isoformat()
                        ),
                        'data': data,
                    }
                else:
                    event_dict = {
                        'type': f'worker:{original_type}',
                        'worker_name': data.get('worker_name', worker_label),
                        'instance_id': data.get('instance_id', _worker_instance_parts(worker_label)[1]),
                        'instance_label': data.get('instance_label', worker_label),
                        'timestamp': (
                            event.metadata.timestamp.isoformat()
                            if hasattr(event, 'metadata') and event.metadata
                            else datetime.datetime.now().isoformat()
                        ),
                        'data': data,
                    }
                # Buffer the event BEFORE the drop gate so events published while
                # no WebSocket callback is registered (F5 reconnect gap) are
                # replayed to the next connection instead of being lost.
                # Heartbeat events (tokens_updated / context_updated) are NOT
                # buffered — they are transient token/context snapshots.
                if event_dict.get('type') not in ('worker:tokens_updated', 'worker:context_updated'):
                    self._buffer_worker_event(event_dict)
                if not self._forwarder._callbacks:
                    log('DEBUG', 'pipeline.bridge',
                        f"Per-worker bus handler for {worker_label}/{original_type}: "
                        f"NO event_callbacks registered, buffered event (replay on next connect)")
                    return
                log('DEBUG', 'pipeline.bridge',
                    f"[TOKEN_PIPELINE] bridge per-worker bus handler [{worker_label}/{original_type}]: "
                    f"forwarding type={event_dict.get('type')}, "
                    f"worker={event_dict.get('worker_name', 'N/A')}, "
                    f"data_keys={list(data.keys())}, "
                    f"n_callbacks={len(self._forwarder._callbacks)}, "
                    f"bridge_session_id={self._session_id}")
                # [PIPELINE:HOPS] Dispatching event to WebSocket callbacks
                log('DEBUG', 'pipeline.hops',
                    f"[PIPELINE:HOPS] bridge dispatching to {len(self._forwarder._callbacks)} callbacks: "
                    f"type={event_dict.get('type')} worker={event_dict.get('worker_name', 'N/A')}")
                for cb in list(self._forwarder._callbacks.values()):
                    try:
                        cb(event_dict)
                    except Exception as exc:
                        log('ERROR', 'server.bridge',
                            f'Failed to forward worker bus event: {exc}')
                        continue
            return _handler

        # Subscribe to detailed event types on the per-worker bus.
        # Note: 'token_warning' is handled here via the per-worker bus because
        # workers publish warnings to their own bus, not the global bus.
        # The global bus handler (_on_worker_token_warning) only catches
        # warnings from the main agent, not worker sub-agents.
        # NOTE: tokens_updated and context_updated ARE now subscribed to enable
        # real-time token count updates in the Web UI for worker sub-agents.
        # The following event types remain intentionally NOT subscribed:
        #   status_message, error_occurred, config_changed, conversation_changed
        # These are emitted by WorkerBusAdapter.emit_*() methods but have NO frontend subscriber.
        # If you need them forwarded, add the event type string to the list below.
        for evt_type in subscribed_types:
            try:
                evt_enum = EventType(evt_type)
                handler_fn = _make_bus_handler(evt_type)
                worker_bus.subscribe(evt_enum, handler_fn)
                subs[evt_enum] = (evt_enum, handler_fn)
                log('DEBUG', 'pipeline.bridge',
                    f"Subscribed to {evt_type!r} on per-worker bus for {worker_label}")
            except (ValueError, Exception) as exc:
                log('DEBUG', 'server.bridge',
                    f'Could not subscribe to {evt_type} for {worker_label}: {exc}')

        if session_id is None:
            session_id = self._session_id or ''
        if instance_id is None:
            instance_id = _worker_instance_parts(worker_label)[1]
        self._worker_bus_subs[worker_label] = {
            'bus': worker_bus,
            'session_id': session_id,
            'instance_id': instance_id,
            'subs': subs,
        }
        log('INFO', 'server.bridge',
            f'Subscribed to per-worker bus for {worker_label} ({len(subs)} event types)')
        log('DEBUG', 'pipeline.bridge',
            f"_subscribe_to_worker_bus complete [worker={worker_label}]: "
            f"{len(subs)}/{len(subscribed_types)} subscriptions successful")
        if len(subs) < len(subscribed_types):
            missing = [t for t in subscribed_types
                       if EventType(t) not in subs]
            log('WARNING', 'pipeline.bridge',
                f"_subscribe_to_worker_bus [worker={worker_label}]: "
                f"FAILED to subscribe to: {missing}")

    def _unsubscribe_worker_bus(self, worker_label: str) -> None:
        """Unsubscribe all per-worker bus subscriptions for a worker instance.

        *worker_label* is the instance label (``<name>`` or ``<name>#<N>``) that
        ``_subscribe_to_worker_bus`` keyed the subscriptions under.
        """
        entry = self._worker_bus_subs.pop(worker_label, None)
        # Clean up context_updated dedup tracking
        self._last_context_updated.pop(worker_label, None)
        if not entry:
            return
        # Tolerate the legacy value shape (a plain {event_type: (event_type, cb)}
        # dict) for bridges that keyed subscriptions before the entry format.
        if isinstance(entry, dict) and 'subs' in entry:
            worker_bus = entry.get('bus')
            subs = entry.get('subs') or {}
        else:
            worker_bus = None
            subs = entry
        if not subs:
            return
        if worker_bus is None:
            # Legacy entry without a stored bus: best-effort registry lookup.
            base_name, instance_id = _worker_instance_parts(worker_label)
            if WORKER_BUS_AVAILABLE and get_worker_event_bus is not None:
                try:
                    worker_bus = get_worker_event_bus(
                        self._session_id or '', base_name, instance_id=instance_id
                    )
                except Exception:
                    worker_bus = None
        for evt_type_key, (stored_evt_type, callback_fn) in subs.items():
            try:
                if worker_bus is not None and hasattr(worker_bus, 'unsubscribe'):
                    worker_bus.unsubscribe(stored_evt_type, callback_fn)
            except Exception:
                pass
        log('INFO', 'server.bridge',
            f'Unsubscribed from per-worker bus for {worker_label}')

    def _resolve_worker_bus_with_retry(
        self, session_id: str, worker_name: str, instance_id: int
    ) -> Any:
        """Resolve a worker's per-worker EventBus, retrying a bounded number of times.

        The bus is normally registered *before* WORKER_SPAWNED is published, but
        on a fast respawn there is a window where the registry entry is still
        missing.  A single lookup would then leave the worker's detailed events
        dropped for its whole lifetime, so retry — but strictly bounded
        (``_WORKER_BUS_RESOLVE_ATTEMPTS`` x ``_WORKER_BUS_RESOLVE_DELAY``, ~1.25 s)
        because this runs on the event-bus publishing thread and must never block
        it indefinitely.
        """
        for attempt in range(1, self._WORKER_BUS_RESOLVE_ATTEMPTS + 1):
            try:
                worker_bus = get_worker_event_bus(
                    session_id, worker_name, instance_id=instance_id
                )
            except Exception as exc:
                worker_bus = None
                log('DEBUG', 'pipeline.bridge',
                    f"_resolve_worker_bus_with_retry: lookup raised for "
                    f"{worker_name}: {exc}")
            if worker_bus is not None:
                if attempt > 1:
                    log('INFO', 'server.bridge',
                        f"_resolve_worker_bus_with_retry: per-worker bus for "
                        f"{_worker_instance_label(worker_name, instance_id)} appeared on "
                        f"attempt {attempt}/{self._WORKER_BUS_RESOLVE_ATTEMPTS}")
                return worker_bus
            if attempt < self._WORKER_BUS_RESOLVE_ATTEMPTS:
                time.sleep(self._WORKER_BUS_RESOLVE_DELAY)
        return None

    def _notify_worker_events_unavailable(
        self, worker_name: str, instance_id: int, worker_label: str, session_id: str
    ) -> None:
        """Tell the frontend once that a worker's detailed events are not forwarded.

        Reuses the existing ``worker:system_notification`` envelope (no new event
        type, no new field) so the worker panel renders the degradation instead of
        silently missing that worker's tool calls.
        """
        event_dict = {
            'type': 'worker:system_notification',
            'worker_name': worker_name,
            'instance_id': instance_id,
            'instance_label': worker_label,
            'session_id': session_id,
            'timestamp': datetime.datetime.now().isoformat(),
            'response': {
                'type': 'system_notification',
                'message': (
                    f"Detailed events for worker '{worker_label}' are unavailable: "
                    f"its per-worker event bus could not be found. Tool calls, tool "
                    f"results and messages from this worker are not shown."
                ),
            },
        }
        self._buffer_worker_event(event_dict)
        if not self._forwarder._callbacks:
            log('DEBUG', 'pipeline.bridge',
                f"_notify_worker_events_unavailable: no event_callbacks, buffered "
                f"notification for {worker_label}")
            return
        for cb in list(self._forwarder._callbacks.values()):
            try:
                cb(event_dict)
            except Exception as exc:
                log('ERROR', 'server.bridge',
                    f'Failed to forward worker bus-missing notification: {exc}')
                continue

    def _on_worker_completed(self, event: WorkerCompletedEvent) -> None:
        """
        Forward worker completed event to frontend and unsubscribe from
        the per-worker EventBus.
        """
        self._forward_worker_event(event)
        data = event.data or {}
        # The global bus is process-global: only tear down per-worker
        # subscriptions for a worker belonging to THIS bridge's session.
        if data.get('session_id') and data['session_id'] != self._session_id:
            return
        worker_name = data.get('worker_name', '')
        if worker_name:
            self._unsubscribe_worker_bus(
                _worker_instance_label(worker_name, data.get('instance_id') or 1)
            )

    def _on_worker_error(self, event: WorkerErrorEvent) -> None:
        """
        Forward worker error event to frontend and unsubscribe from
        the per-worker EventBus.
        """
        self._forward_worker_event(event)
        data = event.data or {}
        # The global bus is process-global: only tear down per-worker
        # subscriptions for a worker belonging to THIS bridge's session.
        if data.get('session_id') and data['session_id'] != self._session_id:
            return
        worker_name = data.get('worker_name', '')
        if worker_name:
            self._unsubscribe_worker_bus(
                _worker_instance_label(worker_name, data.get('instance_id') or 1)
            )

    def _forward_worker_event(self, event: Any) -> None:
        """Forward a worker lifecycle event to frontend (shared handler logic)."""
        data = event.data or {}
        log('DEBUG', 'pipeline.bridge',
            f"[TOKEN_PIPELINE] bridge._forward_worker_event: type={event.type.value if hasattr(event, 'type') else '?'}, "
            f"worker_name={data.get('worker_name', '?')}, "
            f"event_session_id={data.get('session_id', '?')}, "
            f"bridge_session_id={self._session_id}, "
            f"n_callbacks={len(self._forwarder._callbacks)}")
        # Only forward events for this bridge's session
        if data.get('session_id') and data['session_id'] != self._session_id:
            log('DEBUG', 'pipeline.bridge',
                f"_forward_worker_event: SKIPPING event for different session "
                f"(event={data.get('session_id')}, bridge={self._session_id})")
            return
        event_dict = {
            'type': f'worker:{event.type.value}',
            'worker_name': data.get('worker_name', ''),
            'instance_id': data.get('instance_id'),
            'instance_label': data.get('instance_label'),
            'timestamp': event.metadata.timestamp.isoformat(),
            'data': data,
        }
        # Buffer before the drop gate so worker_completed / worker_error events
        # published during the F5 reconnect gap are replayed on next connect.
        self._buffer_worker_event(event_dict)
        if not self._forwarder._callbacks:
            log('DEBUG', 'pipeline.bridge',
                f"_forward_worker_event: no callbacks, buffered "
                f"type={event.type.value if hasattr(event, 'type') else '?'}")
            return
        for cb in list(self._forwarder._callbacks.values()):
            try:
                cb(event_dict)
            except Exception as exc:
                log('ERROR', 'server.bridge',
                    f'Failed to forward worker event: {exc}')

    # ── Global tab registry ──────────────────────────────────────────────────

    def register(self) -> None:
        """Register this bridge in the global active-tab set."""
        _active_tab_bridges.add(self)

    def unregister(self) -> None:
        """Remove this bridge from the global active-tab set."""
        _active_tab_bridges.discard(self)

    @staticmethod
    def broadcast_rename(session_id: str, new_name: str) -> None:
        """Update in-memory state on all bridges holding this session."""
        EventForwarder.broadcast_rename(session_id, new_name)

    @staticmethod
    def broadcast_logging_config(config: Dict[str, Any]) -> None:
        """Broadcast a logging config change to all active tab bridges."""
        EventForwarder.broadcast_logging_config(config)


    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def session(self) -> Optional[Session]:
        """Get the active session, falling back to the loaded session."""
        return self._session or self._loaded_session

    @property
    def workspace_id(self) -> Optional[str]:
        """Get the workspace ID for this bridge instance."""
        return self._workspace_id

    @property
    def agent_is_running(self) -> bool:
        """True if the agent controller exists and its thread is alive (can accept queries)."""
        if self._controller is not None:
            return self._controller.is_running
        return False

    @property
    def is_running(self) -> bool:
        """
        Whether the agent thread (standalone) or controller thread (controller mode)
        is still alive and can accept follow-up queries.
        """
        if self._controller is not None:
            return self._controller.is_busy
        return self._running and (self._thread is not None and self._thread.is_alive())

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    @property
    def is_processing(self) -> bool:
        return self._processing

    # ── Event callback ──────────────────────────────────────────────────────

    def set_event_callback(self, callback: Callable[[Dict[str, Any]], None],
                          key: Optional[int] = None) -> None:
        """Register a WebSocket callback. Delegates to EventForwarder.register_websocket.

        After registration, any worker events buffered while no callback was
        registered (F5 reload / reconnect gap) are replayed to this callback.
        """
        self._forwarder.register_websocket(key, callback)
        self._flush_worker_event_buffer(key)

    def remove_event_callback(self, key: int) -> None:
        """Remove a WebSocket callback. Delegates to EventForwarder.unregister_websocket."""
        self._forwarder.unregister_websocket(key)

    def _buffer_worker_event(self, event_dict: Dict[str, Any]) -> None:
        """Append a worker event to the reconnect ring buffer (max 100).

        Called from per-worker event handlers *before* the no-callback drop gate
        so events published while no WebSocket callback is registered (e.g. F5
        reconnect gap) are replayed to the next connection instead of being lost.
        """
        with self._worker_event_buffer_lock:
            self._worker_event_buffer.append(event_dict)
            if len(self._worker_event_buffer) > self._worker_event_buffer_max:
                self._worker_event_buffer.pop(0)

    def _flush_worker_event_buffer(self, key: Optional[int] = None) -> None:
        """Replay buffered worker events to the just-registered callback.

        Only the callback identified by *key* receives the replay — already
        connected callbacks received these events live.  The buffer is cleared
        so each connection replays exactly the events it missed.
        """
        with self._worker_event_buffer_lock:
            if not self._worker_event_buffer:
                return
            buffered, self._worker_event_buffer = self._worker_event_buffer[:], []
        if key is None:
            cbs = list(self._forwarder._callbacks.values())
        else:
            cb = self._forwarder._callbacks.get(key)
            cbs = [cb] if cb is not None else []
        for event_dict in buffered:
            for cb in cbs:
                try:
                    cb(event_dict)
                except Exception as exc:
                    log('ERROR', 'server.bridge',
                        f'Failed to replay buffered worker event: {exc}')

    def set_controller(self, controller: AgentController) -> None:
        """
        Attach an existing AgentController instance.
        When set, bridge delegates start/pause/resume/stop to the controller
        and receives events via controller.set_event_callback().
        """
        self._controller = controller
        controller.set_event_callback(self._on_controller_event)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def _build_global_agent_config(self) -> AgentConfig:
        """
        Build an AgentConfig from the global config file
        (~/.thoughtmachine/agent_config.json) via ConfigService,
        mirroring what the PyQt GUI does in
        SessionTab.create_new_session() -> state_bridge.create_agent_config().

        This removes the dependency on presets for config defaults.

        Note: provider resolution and API-key fallback happen *after* all
        three config layers are merged in start(), so they are not done here.
        """
        try:
            service = create_agent_config_service()
            raw_config = service.get_all()
            return AgentConfig(**raw_config)
        except Exception as e:
            log('ERROR', 'server.bridge', f"Could not build global agent config: {e}")
            # A silent fallback (AgentConfig(api_key='')) hid config corruption
            # and made callers start sessions with unusable defaults.  Surface
            # the failure so load_session() can decide how to handle it.
            raise RuntimeError(f"Could not build global agent config: {e}") from e

    def start(self, query: str, session_config: Optional[SessionConfig] = None) -> None:
        """Start a new agent session with the given SessionConfig."""
        # Log config source for debugging
        _sc = session_config or self._session_config
        log('INFO', 'server.bridge',
            f'start() called | query={query[:80]!r}... | '
            f'_controller={self._controller is not None} | '
            f'_loaded_session={self._loaded_session.session_id if self._loaded_session else None} | '
            f'is_running={self.is_running} | '
            f'config_provider={session_config.provider_id if session_config else self._session_config.provider_id if self._session_config else "N/A"} | '
            f'mode={_sc.mode if _sc else "N/A"} | '
            f'config_source={"explicit" if session_config else "fallback"}')
        if session_config is None:
            session_config = self._session_config
            if session_config is None:
                raise RuntimeError("start() called without SessionConfig and no session loaded")
        # Reset session reference to ensure a clean slate
        self._session = None

        # Store the SessionConfig and derive AgentConfig
        self._session_config = session_config

        # Resolve API key from the full config chain if not already set
        if not session_config.api_key:
            try:
                resolved = self._config_manager.resolve_full_config(
                    workspace_id=session_config.workspace_id or self._workspace_id,
                    session_id=self._session_id or (
                        self._loaded_session.session_id if self._loaded_session else None
                    ),
                    provider_id=session_config.provider_id,
                )
                if resolved.get("api_key"):
                    session_config.api_key = resolved["api_key"]
                if not session_config.base_url and resolved.get("base_url"):
                    session_config.base_url = resolved["base_url"]
                # Merge provider-specific config (timeout/max_retries) into session
                resolved_pc = resolved.get("provider_config") or {}
                if resolved_pc:
                    merged = dict(getattr(session_config, "provider_config", None) or {})
                    merged.update(resolved_pc)
                    session_config.provider_config = merged
            except Exception:
                pass

        agent_config = session_config.to_agent_config()

        # If no workspace_id on SessionConfig, try to resolve from bridge state
        if not session_config.workspace_id and self._workspace_id:
            session_config.workspace_id = self._workspace_id

        # Set workspace_path from workspace registry if available
        if self._workspace_id and not self._workspace_path:
            try:
                from thoughtmachine.workspace_registry import WorkspaceRegistry
                registry = WorkspaceRegistry.get_default()
                entry = registry.get_workspace(self._workspace_id)
                if entry and entry.root_path:
                    self._workspace_path = entry.root_path
            except Exception:
                pass

        # ── Log workspace defaults status (diagnostic for fresh-vault debugging) ──
        if self._workspace_id and self._workspace_path:
            ws_defaults_path = os.path.join(
                str(Path.home() / ".thoughtmachine" / "workspaces" / self._workspace_id),
                "defaults.json"
            )
            if not os.path.exists(ws_defaults_path):
                log('INFO', 'server.bridge',
                    f'No workspace defaults for {self._workspace_id} at {ws_defaults_path}, '
                    f'using factory+user defaults only')

        # If a controller already exists, delegate to it
        if self._controller is not None:
            session_arg = self._loaded_session
            self._running = True
            self._session = session_arg
            self._controller.start(query, agent_config, session=session_arg)
            self._loaded_session = None  # consumed

            # Immediately try to capture session from controller's agent
            if self._session is None and self._controller is not None:
                try:
                    controller_agent = getattr(self._controller, 'agent', None)
                    if controller_agent is not None:
                        agent_session = getattr(controller_agent, 'session', None)
                        if agent_session is not None:
                            self._session = agent_session
                            self._history_version = getattr(agent_session, 'conversation_version', 0)
                            log('INFO', 'server.bridge',
                                f'Session {agent_session.session_id} captured from controller agent in start()')
                except Exception as exc:
                    log('WARNING', 'server.bridge',
                        f'Could not capture session from controller agent in start(): {exc}')
            return

        if self.is_running:
            raise RuntimeError("Bridge is already running. Stop it first.")

        # Reset primitives
        self._stop_event.clear()
        self._pause_event.set()
        self._running = True
        self._processing = False

        # Create session for this run
        if self._loaded_session is not None:
            session = self._loaded_session
            self._loaded_session = None  # consumed
        else:
            session = Session()
            session.metadata['source'] = 'web_ui'

        # Propagate mode from config to session
        if self._session_config and hasattr(self._session_config, 'mode'):
            session.mode = self._session_config.mode

        # Apply workspace_id from bridge if session doesn't already have one
        if self._workspace_id and not session.workspace_id:
            session.workspace_id = self._workspace_id

        self._session = session
        self._agent = Agent(agent_config, session=session)
        self._session_id = session.session_id

        # Queue the initial query
        self._query_queue.put(query)

        # Start background thread
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="web-bridge-agent",
        )
        self._thread.start()

    def continue_session(self, query: str, config_dict: Optional[Dict[str, Any]] = None) -> None:
        """
        Submit a follow‑up query to a running (or paused) agent.

        If config_dict is provided, it is merged into the current config
        via apply_config() (validated, provider-resolved, persisted) and
        pushed to the controller via request_config_update() so the agent
        picks it up on the next process_query() boundary.
        """
        # ── Apply config update if provided ──────────────────────────────
        # apply_config resolves the provider, persists the session, pushes the
        # new config to the live controller, and logs the outcome itself
        # ("Config applied and persisted via apply_config").  The old success
        # gate here keyed on result["success"], a key apply_config never sets,
        # so its else-branch fired on every successful apply and emitted a
        # bogus "Config update skipped during continue_session: unknown error"
        # warning.  Rely on apply_config's own contract instead of re-gating.
        if config_dict:
            self.apply_config(config_dict)

        # ── Submit the query ──────────────────────────────────────────────
        if self._controller is not None:
            # Defensive health check: the controller object reference may exist
            # even if its background thread has died (silent death after first
            # query).  If that happens, create a fresh controller and hand off
            # via process_query() so the session continues seamlessly.
            if self._controller.is_running:
                self._controller.continue_session(query)
                return
            # ── Controller is dead — resurrect ──
            log('WARNING', 'server.bridge',
                f'continue_session: controller dead (thread died after first query), '
                f'creating new controller for session {self._session_id}')
            new_controller = AgentController()
            new_controller.set_event_callback(self._on_controller_event)
            if self._session is not None and self._session_config is not None:
                new_controller.set_session(
                    self._session,
                    self._session_config.to_agent_config()
                )
                new_controller.process_query(query)
            self._controller = new_controller
            return
        if not self.is_running:
            raise RuntimeError("Bridge is not running. Start it first.")
        self.resume()
        self._query_queue.put(query)

    def pause(self) -> None:
        """Request the agent to pause after the current turn and pause all workers."""
        if self._controller is not None:
            self._controller.pause()
        else:
            if not self.is_running:
                return
            self._pause_event.clear()
            if self._agent is not None:
                self._agent.request_pause()
        # Pause all workers for this session
        if WORKER_BUS_AVAILABLE and _worker_registry is not None:
            with _registry_lock:
                for key, thread in list(_worker_registry.items()):
                    if len(key) >= 3:
                        sid, wname, _iid = key
                    else:
                        sid, wname = key
                    if sid == self._session_id:
                        if _worker_is_running_async_job(thread):
                            log('INFO', 'server.bridge',
                                f'Skip pause of worker {wname}: async job in progress')
                            continue
                        try:
                            thread.pause()
                            log('INFO', 'server.bridge', f'Worker paused: {wname}')
                        except Exception as exc:
                            log('WARNING', 'server.bridge', f'Failed to pause worker {wname}: {exc}')

    def resume(self) -> None:
        """Resume a paused agent and all its workers."""
        if self._controller is not None:
            self._controller.resume()
        else:
            self._pause_event.set()
            if self._agent is not None:
                self._agent._pause_requested = False
        # Resume all workers for this session
        if WORKER_BUS_AVAILABLE and _worker_registry is not None:
            with _registry_lock:
                for key, thread in list(_worker_registry.items()):
                    if len(key) >= 3:
                        sid, wname, _iid = key
                    else:
                        sid, wname = key
                    if sid == self._session_id:
                        if getattr(thread, "status", None) != "paused":
                            continue
                        try:
                            # resume() returns False when a stop is already in
                            # flight; the thread will die without ever
                            # listening, so do not log a resume that did not
                            # happen.
                            outcome = thread.resume()
                        except Exception as exc:
                            log('WARNING', 'server.bridge', f'Failed to resume worker {wname}: {exc}')
                        else:
                            if outcome is False:
                                log('WARNING', 'server.bridge',
                                    f'Worker {wname} refused to resume (stop already in flight)')
                            else:
                                log('INFO', 'server.bridge', f'Worker resumed: {wname}')

    def request_stop(self) -> bool:
        """Truncate the CURRENT TURN and keep the bridge thread and session alive.

        This is the Stop-button path. It is deliberately narrow: it asks the
        controller for a turn-level halt (the agent breaks out of the stream,
        commits the partial assistant text it had already produced and emits its
        'interrupted' terminal), and touches nothing else — no teardown, no
        '[STOP]', no thread join, no unregistration, no queued-config drop.

        Contrast with :meth:`stop`, which is TEARDOWN: it shuts the controller
        down (and, on the legacy no-controller path, ends the bridge loop). Use
        ``close_session``/``stop()`` to end the session; use this to end a turn.

        Returns True when a turn was in flight (``controller.is_busy`` as sampled
        BEFORE the halt, since the halt itself clears that flag), so a caller can
        distinguish "a turn was halted" from "nothing was running".
        """
        if self._controller is not None:
            busy = bool(getattr(self._controller, 'is_busy', False))
            log('INFO', 'server.bridge',
                f'request_stop(): turn-level halt requested (busy={busy}, '
                'session retained)')
            request_halt = getattr(self._controller, 'request_halt', None)
            if not callable(request_halt):
                log('WARNING', 'server.bridge',
                    'request_stop(): controller has no request_halt(); '
                    'turn-level stop is unavailable on this build — nothing halted')
                return False
            request_halt()
            return busy
        # Legacy no-controller path: the loop drives self._agent directly and no
        # halt_check is wired into it, so there is no turn-level lever to pull.
        # The only way to stop the running turn here is to end the loop, hence
        # the fallback to the old teardown signal.
        log('WARNING', 'server.bridge',
            'request_stop(): no controller on this bridge path; turn-level stop '
            'is unavailable — falling back to full teardown (_stop_event set, '
            'bridge loop exits)')
        self._stop_event.set()
        self._pause_event.set()  # unblock if paused
        return True

    def stop(self) -> None:
        """Tear the bridge down: stop the controller and end the bridge loop."""
        # Drop any queued config — the bridge is being torn down and the
        # caller (close_session / server close path) owns the lifecycle now.
        self._pending_config = None
        self.unregister()
        self._unsubscribe_security_events()
        self._unsubscribe_worker_events()
        if self._controller is not None:
            self._controller.stop()
            return
        self._stop_event.set()
        self._pause_event.set()  # unblock if paused

    # ── Query API ───────────────────────────────────────────────────────────

    def get_conversation(self) -> Optional[List[Dict[str, Any]]]:
        """
        Return the current conversation for frontend display.
        Delegates to SessionManager for normalization.
        """
        return self._session_manager.get_conversation(self._session)

    def get_config(self) -> Optional[Dict[str, Any]]:
        """Return the current session config as a serializable dict (no api_key)."""
        if self._session_config is None:
            return None
        return self._config_manager.config_to_dict(self._session_config)

    # ── Controller restart / health check ───────────────────────────────────

    def _restart_controller(self, config: Optional[SessionConfig] = None) -> None:
        """
        Restart the controller thread with an optional new config.

        Stops the existing controller (if any), creates a fresh one,
        re-attaches it to the bridge, and preserves the current session
        state.  Called automatically by apply_config() when the controller
        is unresponsive, or explicitly to force a full controller restart.

        Args:
            config: Optional new SessionConfig.  If None, the current
                    self._session_config is kept.
        """
        old_controller = self._controller
        new_config = config or self._session_config

        if old_controller is not None:
            log('INFO', 'server.bridge', '_restart_controller: stopping old controller')
            old_controller.stop()

        # Create a fresh controller
        from agent.controller import AgentController
        new_controller = AgentController()
        self.set_controller(new_controller)
        self._controller = new_controller
        self._session_config = new_config

        # Preserve session ID
        if self._session is not None:
            self._session_id = self._session.session_id

        log('INFO', 'server.bridge', '_restart_controller: controller restarted')

    # ── Container integrity re-sync ───────────────────────────────────────────
    # Called by apply_config() when session_permissions change.

    def _maybe_re_sync_container(self, workspace_path: str, session_permissions: Any) -> None:
        """Verify container integrity after a config change that may affect permissions.

        If *session_permissions* and *workspace_path* are both provided,
        this calls ``verify_container_integrity`` so any existing container whose
        network/volume mode no longer matches the new permissions is stopped and
        removed.  The container will be recreated with the correct settings the
        next time the agent needs it.

        This is a no-op when Docker is unavailable or no workspace path is known.
        """
        if session_permissions is None or not workspace_path:
            return

        sp_dict = session_permissions.model_dump() if hasattr(session_permissions, 'model_dump') else session_permissions
        try:
            from infra.containers.executor import verify_container_integrity
            result = verify_container_integrity(workspace_path, sp_dict)
            if result.get('action_taken') == 'removed':
                log('INFO', 'server.bridge',
                    f'Container re-synced after config change: '
                    f'{result.get("container_name")} removed '
                    f'(reason: {result.get("mismatch_reason")})')
            elif result.get('action_taken') == 'error':
                log('WARNING', 'server.bridge',
                    f'Container re-sync error: {result.get("mismatch_reason")}')
        except Exception as exc:
            log('WARNING', 'server.bridge',
                f'Container re-sync skipped: {exc}')

    def apply_config(self, config_dict: Dict[str, Any], source: str = 'user') -> Dict[str, Any]:
        """Apply partial config updates from the frontend.

        Delegates validation and mode enforcement to ``ConfigManager.apply_config``,
        then handles controller update, container re-sync, and persistence.

        SessionConfig enforces mode rules:
        - agent/engineer mode: tools and prompt are LOCKED (update_tools/update_prompt log warnings and no-op)
        - custom mode: tools and prompt can be freely changed
        - Other fields (provider_id, model, temperature, etc.) are always mutable.
        """
        if self._session_config is None:
            log("INFO", "server.bridge",
                "apply_config: initializing default session config")
            from agent.config.session_config import SessionConfig
            self._session_config = SessionConfig()

        # Capture pre-application state for the old -> new diff log below
        old_session_config = self._session_config

        # Step 1: Delegate validation and update to ConfigManager
        frontend_result, new_config = self._config_manager.apply_config(
            config_dict,
            current_config=self._session_config,
            is_running=self.is_running,
            has_session=self._session is not None,
            workspace_path=self._workspace_path,
        )

        # Step 2: Update session config if ConfigManager returned a new one
        if new_config is not None:
            self._session_config = new_config
            # Refresh the uncapped persistence snapshot (grants are only
            # overlaid when the payload explicitly carries session_permissions
            # — never folded from the possibly ceiling-capped live config).
            self._refresh_persistence_dump(new_config, config_dict)

        # Log an old -> new field diff for observability (None == '' treated
        # as equivalent for string fields, mirroring Agent._configs_are_identical).
        changed_keys = set()
        if old_session_config is not None and new_config is not None:
            old_dump = old_session_config.model_dump(exclude={'api_key', 'stop_check'})
            new_dump = new_config.model_dump(exclude={'api_key', 'stop_check'})
            changes = []
            for key in sorted(set(old_dump) | set(new_dump)):
                old_val = old_dump.get(key)
                new_val = new_dump.get(key)
                if isinstance(old_val, str) and old_val == '':
                    old_val = None
                if isinstance(new_val, str) and new_val == '':
                    new_val = None
                if old_val != new_val:
                    changes.append(f'{key}: {repr(old_val)[:80]} -> {repr(new_val)[:80]}')
                    changed_keys.add(key)
            if changes:
                log('INFO', 'server.bridge',
                    f'apply_config: {len(changes)} change(s): ' + '; '.join(changes))
            else:
                log('DEBUG', 'server.bridge', 'apply_config: no field changes detected')

        # A change that ONLY touches session_permissions is persisted (see
        # _refresh_persistence_dump above and save_session below) and picked up
        # by the tool-executor gate from disk on the next tool call.
        permission_only_change = (
            old_session_config is not None
            and new_config is not None
            and bool(changed_keys)
            and changed_keys == {'session_permissions'}
        )

        # Step 3: Convert to AgentConfig (always needed below for audit + merge).
        agent_config = self._session_config.to_agent_config()

        # Permission-only changes are persist-only: pushing them into the live
        # controller is pointless and re-syncing the container would needlessly
        # kill/rebuild it. Both are skipped for permission-only changes; non-
        # permission (or mixed) changes keep both behaviors.
        if not permission_only_change:
            if self._controller is not None:
                controller_alive = (
                    hasattr(self._controller, "thread")
                    and self._controller.thread is not None
                    and self._controller.thread.is_alive()
                )
                if not controller_alive:
                    log("WARNING", "server.bridge",
                        "apply_config: controller thread is dead — restarting controller")
                    self._restart_controller(self._session_config)
                self._controller.request_config_update(agent_config)

            # Step 4: Re-sync container
            self._maybe_re_sync_container(
                self._workspace_path or "",
                getattr(agent_config, "session_permissions", None),
            )

        # Step 5: Persist to disk
        self.save_session()

        log("INFO", "server.bridge", "Config applied and persisted via apply_config")

        # Step 6: Build enhanced result with settings, permissions, and merged_config
        settings = self._config_manager.extract_settings(frontend_result)
        permissions = self._config_manager.resolve_effective_permissions(
            self._session_config,
            self._session_id or (self._loaded_session.session_id if self._loaded_session else None),
            self._session_config.workspace_id or self._workspace_id,
        )

        result = {
            "config": frontend_result,
            "settings": settings,
            "permissions": permissions,
            "merged_config": frontend_result,
        }

        # Step 7: Audit the config change (redacted) and attach the effective
        # (redacted) config for the frontend.
        try:
            log_config_audit(
                source=source,
                component='config.apply',
                old=old_session_config,
                new=self._session_config,
                restart_required=restart_required_for(old_session_config, self._session_config),
                injected=agent_config,
            )
            result["effective_config"] = ConfigManager.get_effective_config(
                self,
                workspace_id=self._workspace_id,
                workspace_path=self._workspace_path,
            )
        except Exception:
            # Audit/logging must never break the config apply path.
            log('WARNING', 'server.bridge', 'apply_config: audit/effective_config step failed')

        return result

    def apply_config_queued(self, config_dict: Dict[str, Any], source: str = 'user') -> Dict[str, Any]:
        """Apply config now if the controller is idle, otherwise queue it.

        When the controller is busy (agent mid-turn, ``is_busy`` == RUNNING/
        PAUSING) the config is stored in ``self._pending_config`` and applied
        automatically by ``_on_controller_event`` as soon as the controller
        becomes idle again — the deferred ``config_changed`` is broadcast from
        there (last write wins: a newer queued config replaces an older one).

        Returns ``{"status": "queued"}`` when queued; otherwise returns the
        usual ``apply_config`` result dict (``{"config": ..., ...}``).
        """
        if self._controller is not None and self._controller.is_busy:
            self._pending_config = config_dict
            log('INFO', 'server.bridge',
                "Controller busy — config queued for deferred apply")
            return {"status": "queued"}
        return self.apply_config(config_dict, source=source)

    # ── Session persistence ──────────────────────────────────────────────────

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all saved sessions from the session store."""
        try:
            return self._session_store.list_sessions()
        except Exception as e:
            log('ERROR', 'server.bridge', f"list_sessions error: {e}")
            return []

    # ── Uncapped persistence snapshot ──────────────────────────────────────

    def _refresh_persistence_dump(self, new_config, payload: Optional[Dict[str, Any]]) -> None:
        """Update the uncapped persistence snapshot after a config change.

        The live ``_session_config`` may be ceiling-capped in memory
        (``load_session`` applies the workspace ceiling to the EFFECTIVE
        config), and persisting that capped dump would collapse stored full
        grants on the next switch-away / shutdown save.  This snapshot keeps
        the uncapped operator view for STORAGE only: non-permission fields are
        refreshed from ``new_config``, while ``session_permissions`` only
        changes when the payload explicitly carries a ``session_permissions``
        block (an operator grant/revoke) — capped live values are never folded
        into it.
        """
        if new_config is None:
            return
        new_dump = new_config.model_dump(exclude={'api_key'}, exclude_none=True)
        payload_perms = payload.get('session_permissions') if isinstance(payload, dict) else None
        prior = self._session_config_persist_dump
        if isinstance(payload_perms, dict) and payload_perms:
            # Explicit operator grant/revoke: overlay on the prior grant set.
            if isinstance(prior, dict) and isinstance(prior.get('session_permissions'), dict):
                merged_perms = dict(prior['session_permissions'])
            else:
                merged_perms = {}
            merged_perms.update(payload_perms)
            new_dump['session_permissions'] = merged_perms
            self._session_config_persist_dump = new_dump
        elif isinstance(prior, dict):
            # No permission change: keep prior (uncapped) grants, refresh fields.
            refreshed = dict(prior)
            for key, value in new_dump.items():
                if key != 'session_permissions':
                    refreshed[key] = value
            self._session_config_persist_dump = refreshed
        else:
            self._session_config_persist_dump = new_dump

    def _config_dump_for_persistence(self) -> Optional[Dict[str, Any]]:
        """Serialised config used for persistence.

        Prefers the uncapped persistence snapshot so switch-away / shutdown
        saves never collapse stored full permission grants; falls back to the
        live config dump when no snapshot exists yet (fresh bridge / defaults).
        """
        if self._session_config_persist_dump is not None:
            return dict(self._session_config_persist_dump)
        if self._session_config is not None:
            return self._session_config.model_dump(exclude={'api_key'}, exclude_none=True)
        return None

    def save_session(self, name: Optional[str] = None) -> Optional[Session]:
        """
        Save current conversation as a session to the store.
        Delegates persistence to SessionManager, then reloads worker contexts.
        """
        try:
            # Use the active session if available (standalone or controller path)
            session = getattr(self, '_session', None)
            # Persist the UNCAPPED snapshot (see _config_dump_for_persistence)
            # so a switch-away / shutdown save can never collapse the stored
            # full grants with the ceiling-capped live config.
            persist_dump = self._config_dump_for_persistence()
            if session is None:
                # Fallback when no session is active — build from existing session data
                session_id = self._loaded_session.session_id if self._loaded_session else str(uuid.uuid4())
                session = Session(
                    session_id=session_id,
                    user_history=list(self._loaded_session.user_history) if self._loaded_session else [],
                    workspace_id=self._workspace_id,
                    metadata={
                        'session_config': (
                            {k: v for k, v in persist_dump.items()
                             if k != 'session_permissions'}
                            if persist_dump
                            else {}
                        ),
                        'source': 'web_ui',
                    }
                )
            else:
                # Update existing session metadata
                session.metadata.setdefault('session_config', {})
                if persist_dump:
                    # session_permissions is NOT a persistence-layer grant
                    # source: the canonical, REST-owned store is the permission
                    # sidecar (P1).  Strip the key so this uncapped dump can
                    # never act as a shadow grants writer.
                    session.metadata['session_config'] = {
                        k: v for k, v in persist_dump.items()
                        if k != 'session_permissions'
                    }
                session.metadata.setdefault('source', 'web_ui')

            # Apply name: explicit arg > existing loaded session name > generated
            if name:
                session.metadata['name'] = name
            elif self._loaded_session and self._loaded_session.metadata.get('name'):
                session.metadata['name'] = self._loaded_session.metadata['name']
            session.ensure_name()

            # Delegate save to SessionManager
            self._session_manager.save_session(session, session_config=None, name=None)

            self._loaded_session = session

            # ── Load persisted worker contexts for this workspace ──────────
            self._persisted_workers.clear()
            self._load_worker_contexts()
            log('INFO', 'server.bridge', f"Session saved: {session.session_id} ({session.metadata.get('name')})")
            return session
        except Exception as e:
            import traceback
            traceback.print_exc()
            log('ERROR', 'server.bridge', f"save_session error: {e}")
            return None

    @staticmethod
    def _normalize_for_frontend(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalize messages for frontend display without modifying the originals.
        """
        FINAL_TOOL_NAMES = {"Respond"}
        # Legacy tool names that map to Respond for old session replay
        LEGACY_TO_RESPOND = {
            "Final": {"response_type": "answer"},
            "FinalReport": {"response_type": "answer"},
            "RequestUserInteraction": {"response_type": "question"},
        }
        # Combined set: current Respond + all legacy tools mapped to Respond
        ALL_RESPOND_NAMES = FINAL_TOOL_NAMES | set(LEGACY_TO_RESPOND.keys())
        SUMMARY_TOOL_NAMES = {"SummarizeTool", "summarize", "Summarize"}
        normalized = []
        last_tool_call_name = None       # track for final detection
        pending_final_assistant = False  # mark next assistant as final

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # ── structured tool_calls array (current format) ──
            if role == "assistant" and msg.get("tool_calls"):
                assistant_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                if assistant_msg.get("content") or msg.get("tool_calls"):
                    normalized.append(assistant_msg)
                for tc in msg["tool_calls"]:
                    # 🟢 FIX 1: extract from nested function object
                    func = tc.get("function", {})
                    tool_name = func.get("name", "?")
                    args_str = func.get("arguments", "{}")
                    # arguments is a JSON string; parse if possible, else keep raw
                    try:
                        args_obj = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args_obj = args_str  # fallback: display raw string
                    normalized.append({
                        "role": "tool_call",
                        "content": json.dumps({
                            "name": tool_name,
                            "arguments": args_obj,
                        }),
                        "is_final": False,
                        "is_system_notification": False,
                    })
                    last_tool_call_name = tool_name   # remember for final detection
                continue

            # ── old-format tool call fallback ──
            if role == "assistant" and isinstance(content, str) and content.startswith("[Tool call:"):
                match = re.match(r'^\[Tool call:\s*(\w+)\(([^)]*)\)\]$', content)
                if match:
                    tool_name = match.group(1)
                    args_str = match.group(2)
                    try:
                        args_obj = json.loads(args_str.replace("'", '"'))
                    except Exception:
                        args_obj = {}
                    normalized.append({
                        "role": "tool_call",
                        "content": json.dumps({"name": tool_name, "arguments": args_obj}),
                        "is_final": False,
                        "is_system_notification": False,
                    })
                    last_tool_call_name = tool_name
                    continue

            # ── tool result → tool_result + final detection ──
            if role == "tool":
                new_msg = dict(msg)
                new_msg["role"] = "tool_result"
                # 🟣 FIX 2: if preceding tool call was Final/FinalReport, mark final
                if last_tool_call_name in ALL_RESPOND_NAMES:
                    new_msg["is_final"] = True
                    pending_final_assistant = True   # next assistant also final
                # 🟡 SummarizeTool results: dark golden, full markdown, no truncation
                if last_tool_call_name in SUMMARY_TOOL_NAMES:
                    new_msg["is_summary"] = True
                normalized.append(new_msg)
                continue

            # ── assistant message (no tool_calls) ──
            if role == "assistant":
                # drop empty placeholder messages
                if isinstance(content, str) and content.strip() == "":
                    continue
                new_msg = dict(msg)
                if pending_final_assistant:
                    new_msg["is_final"] = True
                    pending_final_assistant = False
                normalized.append(new_msg)
                continue

            # ── everything else (user, system notifications) ──
            normalized.append(dict(msg))

        # ── post-processing: inject is_system_notification for frontend ──
        from agent.core.message import SYSTEM_NOTIFICATION_PREFIX
        for m in normalized:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                if m["content"].startswith(SYSTEM_NOTIFICATION_PREFIX):
                    m["is_system_notification"] = True
                else:
                    m.setdefault("is_system_notification", False)
            else:
                m.setdefault("is_system_notification", False)
            m.setdefault("is_final", False)
            m.setdefault("response_type", None)

        return normalized

    def create_session(self, mode: str = "custom", workspace_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """
        Create a new empty session and return (session_id, frontend_config).

        Delegates to ``SessionManager.create_session``, then updates bridge
        in-memory state (``_session``, ``_loaded_session``, ``_session_config``)
        WITHOUT broadcasting events — the caller (e.g. server.py) is responsible
        for sending session_loaded / state_changed to the frontend.

        ``workspace_id`` (if given) is validated with ``_validate_workspace_id``
        before session creation.
        """
        if workspace_id is not None:
            _validate_workspace_id(workspace_id)
        session_id, frontend_config = self._session_manager.create_session(
            mode=mode, workspace_path=self._workspace_path, workspace_id=workspace_id
        )
        # Load session into in-memory state (no broadcast).  Scope the read to
        # the workspace this session was just created in (falling back to the
        # bridge's own workspace) so a same-id record from another namespace
        # can never be adopted here.
        _scope = workspace_id or self._workspace_id
        session = self._session_manager.load_session(session_id, workspace_id=_scope)
        if (
            _scope
            and session is not None
            and session.workspace_id
            and session.workspace_id != _scope
        ):
            log('WARNING', 'server.bridge',
                f"create_session refused: session {session_id} belongs to workspace "
                f"{session.workspace_id!r}, expected {_scope!r}")
            session = None
        if session is not None:
            self._session = session
            self._loaded_session = session
            self._history_version = session.conversation_version
            # A workspace-less record must not unscope the bridge: keep the
            # existing binding when the freshly created session carries none.
            self._workspace_id = session.workspace_id or self._workspace_id
            sc = self._session_manager.extract_session_config(session)
            if sc is not None:
                self._session_config = sc
        return session_id, frontend_config

    def load_session(
        self,
        session_id: str,
        limit: Optional[int] = 50,
        offset: int = 0,
        workspace_id: Optional[str] = None,
    ) -> bool:
        """Load a session from the store, replacing current conversation.

        Delegates persistence and role repair to ``SessionManager.load_session``
        and config extraction to ``SessionManager.extract_session_config``.

        After calling this, the server handler should send ``config_changed``
        (in frontend format) so the frontend controls update.

        Important: The session's ``user_history`` is kept in canonical API format
        (``role: "tool"``). Frontend display normalization happens at emit time
        via ``_normalize_for_frontend()`` so the data stays valid for API calls.

        Pagination: if ``limit`` is provided, only the most recent ``limit``
        messages are emitted in ``conversation_changed``, with ``total_count``
        and ``has_more`` to let the frontend fetch older pages.

        Workspace scope: a session may only be loaded into a bridge of its own
        workspace.  ``workspace_id`` optionally names the expected workspace,
        but the bridge's own binding always wins: a caller asking for a
        different workspace, or a stored record carrying a different
        ``workspace_id``, is refused with ``False`` and leaves the bridge
        state untouched.
        """
        try:
            # ── Workspace scope: never pivot this bridge across workspaces ──
            # Loading adopts the session's workspace_id below, so a record from
            # another workspace would silently re-scope every later write
            # (session saves, containers, permission lookups).  The bridge's own
            # binding is authoritative; a caller-supplied id may only scope a
            # bridge that has none yet.
            _scope: Optional[str] = self._workspace_id or workspace_id
            # D3: the id-form primitive decides who may be loaded.  A caller that
            # NAMES a workspace must match the bridge's binding, and an unscoped
            # bridge can no longer be scoped by naming one (it now REFUSES; the
            # caller id used to scope it).  A bare call is decided after the read,
            # below, where the record's own workspace_id is known.
            if workspace_id is not None and _ownership_refusal_ids(
                    workspace_id, self._workspace_id) is not None:
                log('WARNING', 'server.bridge',
                    f"load_session refused: workspace {workspace_id!r} requested but "
                    f"bridge is scoped to {self._workspace_id!r} (session {session_id})")
                return False
            # Load from the bridge's current store (caller may have replaced _session_store)
            session = self._session_store.load_session(session_id, workspace_id=_scope)
            if session is None:
                log('WARNING', 'server.bridge', f"Session not found: {session_id}")
                return False
            # The store also scans the legacy, workspace-agnostic directory,
            # whose records can still carry a foreign workspace_id.  Refuse
            # one here rather than pivoting the bridge into that workspace.
            # D3: the same primitive.  A record that carries a workspace must
            # match the settled scope, and an ABSENT scope now REFUSES (the
            # inversion this wave lands: an unscoped bridge used to load any
            # workspace's session).  A workspace-less legacy record has no
            # identity to compare, so there is nothing to enforce for it.
            if session.workspace_id is not None and _ownership_refusal_ids(
                    session.workspace_id, _scope) is not None:
                log('WARNING', 'server.bridge',
                    f"load_session refused: session {session_id} belongs to workspace "
                    f"{session.workspace_id!r}, expected {_scope!r}")
                return False
            # Apply role repair, config migration, registry registration
            self._session_manager.repair_session(session)
            self._session = session
            self._history_version = session.conversation_version
            # A workspace-less legacy record must not unscope the bridge: keep
            # the existing binding when the record does not carry one.
            self._workspace_id = session.workspace_id or self._workspace_id

            # ── Backfill workspace_path from the workspace registry ─────
            if self._workspace_id and not self._workspace_path:
                try:
                    registry = WorkspaceRegistry.get_default()
                    entry = registry.get_workspace(self._workspace_id)
                    if entry and entry.root_path:
                        self._workspace_path = entry.root_path
                        if self._session_config is None:
                            global_cfg = self._build_global_agent_config()
                            self._session_config = SessionConfig.from_agent_config(
                                global_cfg, workspace_id=self._workspace_id or ''
                            )
                        log('INFO', 'server.bridge',
                            f"Backfilled workspace_path from registry for workspace {self._workspace_id}: {entry.root_path}")
                except Exception as exc:
                    log('WARNING', 'server.bridge',
                        f"Could not backfill workspace_path from registry: {exc}")

            self._loaded_session = session

            # ── Load persisted worker contexts for this workspace ──────────
            self._persisted_workers.clear()
            self._load_worker_contexts()

            # ── Extract session config from session metadata via SessionManager ──
            sc = self._session_manager.extract_session_config(session)
            if sc is not None:
                # Snapshot the serialised config BEFORE the ceiling cap below.
                # The cap is applied to the live config only — persisting it
                # would permanently collapse the stored full grants after a
                # single reload, so the comparison/rewrite below must use the
                # uncapped dump (restart-safe persistence).
                uncapped_dump = sc.model_dump(exclude={"api_key"}, exclude_none=True)
                # Keep this uncapped snapshot as the persistence source so a
                # later switch-away / shutdown save_session never collapses the
                # stored grants with the ceiling-capped live config below.
                self._session_config_persist_dump = uncapped_dump
                # Re-cap the stored session permissions through the current
                # workspace permission ceiling — exactly like a fresh config
                # apply (see config_manager.resolve_full_config) — so a
                # restored session can never exceed the workspace ceiling
                # until the next config change.
                # Raw grants now live in the canonical P1 sidecar, not in the
                # session record.  Fail-closed: an unreadable grants path (or
                # a session with no workspace) yields the deny-all sentinel,
                # never the permissive Pydantic default and never ``None``.
                from thoughtmachine.permission_store import read_grants_or_deny_all

                stored_perms = read_grants_or_deny_all(
                    session.session_id, self._workspace_id
                )
                if self._workspace_id:
                    try:
                        from web_ui.backend.config_manager import _load_workspace_permission_ceiling

                        ceiling = _load_workspace_permission_ceiling(self._workspace_id)
                        if ceiling:
                            from security.security_gate import cap

                            stored_perms = cap(
                                stored_perms, ceiling
                            )
                    except Exception as exc:
                        log('WARNING', 'server.bridge',
                            f"Could not apply workspace permission ceiling to session config: {exc}")
                    sc.session_permissions = stored_perms
                self._session_config = sc
                # Migrate saved config to new format (exclude api_key).
                # Only write when something actually changed (stored raw vs
                # capped/merged dump) so loading a session never rewrites an
                # identical stored config — an unconditional rewrite here is
                # what let a partial in-memory config collapse the stored
                # permission set.
                stored_raw = session.metadata.get("session_config")
                # Compare against the UNCAPPED dump so the conditional rewrite
                # below never persists the in-memory ceiling cap (which would
                # collapse full stored grants after a single restart).
                if stored_raw != uncapped_dump:
                    self._session_manager.save_config_to_session(
                        session, sc, config_dump=uncapped_dump
                    )
                log('INFO', 'server.bridge',
                    f'Loaded session_config from metadata: mode={sc.mode}, '
                    f'provider={sc.provider_id}, model={sc.model}')
            else:
                log('INFO', 'server.bridge', 'No session_config in session metadata — session will use defaults')
                merged = self._config_manager.resolve_full_config(
                    workspace_id=self._workspace_id,
                    session_id=session_id,
                )
                self._session_config = self._config_manager.session_config_from_merged(merged)
                self._session_config_persist_dump = self._session_config.model_dump(
                    exclude={"api_key"}, exclude_none=True
                )

            # ── Fallback: derive workspace_id from config if session has none ──
            if self._workspace_id is None and self._workspace_path:
                try:
                    resolved = _resolve_workspace_id(self._workspace_path)
                    if resolved:
                        # D2 (site 2, bootstrap-bypass): route the write through
                        # the bind authority.  The surrounding guard is exactly
                        # what `bootstrap` mode does, now named and auditable.
                        # bootstrap from unbound: refusal is structurally unreachable today.
                        _bind_refusal = self._bind_workspace(resolved, "bootstrap")
                        if _bind_refusal:
                            log('WARNING', 'server.bridge',
                                f"Could not bind workspace_id from config.workspace_path: {_bind_refusal}")
                        else:
                            log('INFO', 'server.bridge',
                                f"Resolved workspace_id from config.workspace_path: {resolved}")
                except Exception as exc:
                    log('WARNING', 'server.bridge',
                        f"Could not resolve workspace_id from workspace_path: {exc}")

            # Emit conversation_changed so the frontend updates
            # Use _normalize_for_frontend to convert API roles
            # (e.g. "tool") to frontend roles (e.g. "tool_result")
            # without modifying the session data.
            # Pagination: only emit a slice of messages.
            all_messages = session.user_history or []
            total_count = len(all_messages)
            if limit is not None and len(all_messages) > limit:
                # Send the most recent `limit` messages
                page = self._normalize_for_frontend(all_messages[-limit:])
                has_more = total_count > limit
            else:
                page = self._normalize_for_frontend(all_messages)
                has_more = False
            self._forwarder.broadcast(self._session_id, "conversation_changed", {
                "messages": page,
                "total_count": total_count,
                "has_more": has_more,
            })
            # Emit session_loaded for metadata
            # Fix 4a: embed the frontend config so the chat UI renders from the
            # first event (uses the existing ConfigManager instance — no new import).
            try:
                _fe_config = self._config_manager.frontend_config_from_bridge(self)
            except Exception:
                _fe_config = None
            self._forwarder.broadcast(self._session_id, "session_loaded", {
                "session_id": session_id,
                "session_name": session.metadata.get('name', 'Untitled Session'),
                "message_count": len(session.user_history),
                "workspace_id": self.workspace_id,
                "workspace_path": self._workspace_path or '',
                # is_running mirrors the live state_changed semantics (controller.is_busy:
                # RUNNING/PAUSING) so the reconnect/load path agrees with live updates —
                # the server no longer sends a separate state_changed after
                # session_loaded (Fix 2a).
                "is_running": self._controller.is_busy if self._controller else False,
                "config": _fe_config,
                # Full tool list (name/enabled/description) so the store's
                # receiveSessionLoaded can populate the tools panel without a
                # separate /api/tools round-trip (session_loaded tools fix).
                "tools": (_fe_config or {}).get("tools", []),
            })
            # Emit initial context_length so the frontend status bar shows
            # the correct value immediately (no need to wait for a live token_update).
            self._forwarder.broadcast(self._session_id, "context_updated", {
                "context_length": self._session.context_length,
            })
            # Also emit initial token counts so the frontend status bar shows
            # the correct values immediately (not stuck at 0/0).
            self._forwarder.broadcast(self._session_id, "tokens_updated", {
                "input": getattr(self._session, 'total_input_tokens', 0),
                "output": getattr(self._session, 'total_output_tokens', 0),
            })

            log('INFO', 'server.bridge', f"Session loaded: {session_id} ({session.metadata.get('name')}) — {len(session.user_history)} messages")

            # Register this session as an open session (persists to open_sessions.json)
            # so it survives server restarts and the hub WS can return it.
            # Use add_open_session directly to avoid an unnecessary full session write.
            try:
                self._session_store.add_open_session(session_id)
            except Exception as e:
                log('WARNING', 'server.bridge', f"Could not register open session: {e}")

            # Discover already-running workers for this session
            # (late-arriving bridge guard: workers may have been spawned
            # between bridge.__init__ and load_session).
            self._discover_existing_workers(session_id)

            # Verify container integrity after loading a session whose
            # permissions may have changed since the last active session.
            # If an existing container no longer matches the new permissions,
            # it is stopped and removed (recreated on next use).
            self._maybe_re_sync_container(
                self._workspace_path or "",
                getattr(self._session_config, "session_permissions", None)
                if self._session_config else None,
            )

            return True
        except Exception as e:
            import traceback
            traceback.print_exc()
            log('ERROR', 'server.bridge', f"load_session error: {e}")
            return False

    def load_more_messages(self, offset: int, limit: int = 50) -> Optional[Dict[str, Any]]:
        """Return a page of older messages — delegates to SessionManager."""
        session = self._session or self._loaded_session
        return self._session_manager.load_more_messages(session, offset, limit=limit)

    def delete_session(
        self, session_id: str, workspace_id: Optional[str] = None
    ) -> bool:
        """Delete a session — delegates to SessionManager (workspace-scoped)."""
        return self._session_manager.delete_session(
            session_id, workspace_id=workspace_id
        )

    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session — delegates to SessionManager, then syncs in-memory state."""
        result = self._session_manager.rename_session(session_id, new_name, workspace_id=self._workspace_id)
        if result:
            # Update loaded session name if it's the one being renamed
            if self._loaded_session and self._loaded_session.session_id == session_id:
                self._loaded_session.metadata['name'] = new_name
            # Also update active in-memory session to prevent save_session() from reverting
            if self._session and self._session.session_id == session_id:
                self._session.metadata['name'] = new_name
        return result

    # ── Open sessions management ────────────────────────────────────────────

    def get_open_sessions(self) -> List[str]:
        """Return the list of open session IDs — delegates to SessionManager."""
        return self._session_manager.get_open_sessions()

    def save_open_session(self, session_id: Optional[str] = None) -> None:
        """
        Save the current session and add it to the open sessions list.
        Delegates persistence to SessionManager.
        """
        sid = session_id or self._session_id or (
            self._loaded_session.session_id if self._loaded_session else None
        )
        if sid is None:
            log('DEBUG', 'server.bridge', "save_open_session: no session ID available")
            return
        # Save the session first (so it exists on disk)
        self.save_session()
        # Then add to open list via SessionManager
        self._session_manager.save_open_session(sid)

    def remove_open_session(self, session_id: Optional[str] = None) -> None:
        """
        Remove a session from the open sessions list.
        Delegates to SessionManager.
        """
        sid = session_id or self._session_id or (
            self._loaded_session.session_id if self._loaded_session else None
        )
        if sid is None:
            log('WARNING', 'server.bridge', "remove_open_session: no session ID available")
            return
        self._session_manager.remove_open_session(sid)

    def close_session(self, session_id: Optional[str] = None) -> None:
        """
        Save session, remove from open sessions list, and stop the bridge.
        This is the session/bridge close sequence, NOT a complete "close tab"
        teardown: workspace-scoped containers are left running.
        Persistence cleanup is delegated to SessionManager.

        Args:
            session_id: Session to close. If None, uses the bridge's current
                        session or loaded session.
        """
        sid = session_id or self._session_id or (
            self._loaded_session.session_id if self._loaded_session else None
        )
        log('INFO', 'server.bridge', f"Closing session: {sid or '(no id)'}")
        # STOP the bridge FIRST — let the agent thread finish and flush
        # final messages into user_history before we snapshot.
        self.stop()
        # Wait for agent thread to fully exit (controller.stop() already
        # joins the controller thread with a 5s timeout in shutdown()).
        if self._controller is None and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=60)
        # NOW save — all final messages are captured
        if sid:
            self.save_session()
            self._session_manager.close_session(sid)
            # Containers are now workspace-scoped (thoughtmachine.workspace_id
            # label) and survive session close; workspace cleanup is handled
            # by cleanup_workspace() when a workspace is decommissioned.
            # They are NOT leaked, though: the periodic container sweep
            # reclaims EXITED ephemeral containers at their lifecycle-class
            # TTL, and any container still left is reclaimed on decommission.

        # Gracefully stop any worker threads spawned during this session
        # scoped to this session only; without a resolvable id we stop nothing rather than everything
        if shutdown_workers is not None and sid:
            try:
                shutdown_workers(timeout=5.0, session_id=sid)
            except Exception:
                log('WARNING', 'server.bridge', 'Error shutting down workers during session close')

        # Clear persisted worker contexts
        self._persisted_workers.clear()

        # Reset state
        self._session = None
        self._loaded_session = None
        self._session_id = None
        self._forwarder.broadcast(None, "session_cleared", {})
        self._forwarder.broadcast(None, "state_changed", {
            "state": "IDLE",
            "is_running": False,
        })
        self._cleanly_closed = True
        log('INFO', 'server.bridge', f"Session closed: {sid or '(no id)'}")

    # ── Worker persistence ──────────────────────────────────────────────

    def _load_worker_contexts(self) -> None:
        """
        Scan the workspace ``workers/`` directory for persisted worker
        conversation contexts and populate ``self._persisted_workers``.

        Called from ``load_session()`` after the session is loaded so that
        the bridge knows about idle workers that can be resumed.
        """
        if not self._workspace_id:
            return
        try:
            from thoughtmachine.workspace_capabilities import _workspace_dir
            ws_dir = _workspace_dir(self._workspace_id)
        except ImportError:
            log('WARNING', 'server.bridge',
                '_load_worker_contexts: workspace_capabilities not available')
            return
        except Exception as exc:
            log('WARNING', 'server.bridge',
                f'_load_worker_contexts: failed to resolve workspace dir: {exc}')
            return

        workers_dir = ws_dir / "workers"
        if not workers_dir.is_dir():
            return

        loaded = 0
        for subdir in workers_dir.iterdir():
            if not subdir.is_dir():
                continue
            name = subdir.name
            context_path = subdir / "context.json"
            if not context_path.exists():
                continue
            try:
                context = json.loads(context_path.read_text(encoding="utf-8"))
                self._persisted_workers[name] = {
                    "name": name,
                    "context": context,
                }
                loaded += 1
            except (json.JSONDecodeError, OSError):
                continue

        if loaded:
            log('INFO', 'server.bridge',
                f'_load_worker_contexts: loaded {loaded} persisted worker(s)')

    def resume_worker(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Return the persisted conversation context for a worker by name.

        Returns ``None`` if no persisted context exists for that worker.
        The caller (typically the ``Worker`` tool) can use this to restore
        a worker's conversation history before ``spawn``.
        """
        entry = self._persisted_workers.get(name)
        if entry is None:
            return None
        return entry.get("context")

    def clear_loaded_session(self) -> None:
        """Clear the loaded session reference for a fresh start."""
        self._session = None
        self._loaded_session = None
        self._forwarder.broadcast(self._session_id, "conversation_changed", {
            "messages": []
        })
        log('INFO', 'server.bridge', "Session cleared for fresh start")

    def _on_controller_event(self, event: Dict[str, Any]) -> None:
        """
        Callback registered with the controller via set_event_callback().
        Receives raw events from the controller's agent thread and maps
        them to the frontend protocol, exactly like _map_and_emit does
        for the standalone agent loop.

        Also captures the session ID from the first event that carries one
        and triggers save_open_session() — deferred from start() because
        the session ID isn't known until the controller has created its agent.

        Additionally, propagates the session object from the controller's
        agent back to the bridge when it becomes available. This fixes the
        case where a new session (started with session=None) is created
        internally by the controller's Agent and the bridge never receives
        it, causing _map_and_emit to skip conversation_changed events.
        """
        log('INFO', 'server.bridge',
            f'_on_controller_event: type={event.get("type")!r} | '
            f'_session_id={self._session_id} | '
            f'_session={self._session is not None} | '
            f'_controller={self._controller is not None}')
        # Capture session ID from first event that has one
        if self._session_id is None:
            sid = event.get('session_id')
            if sid is not None:
                self._session_id = sid
                self.save_open_session()
                log('INFO', 'server.bridge', f"Session ID captured from controller event: {sid}")

        # If the bridge doesn't have a session yet, try to grab it from the
        # controller's agent (which created a new Session internally in _run()).
        # This runs on the controller thread, where self._controller.agent is
        # guaranteed to be set before the first event is emitted.
        if self._session is None and self._controller is not None:
            try:
                controller_agent = getattr(self._controller, 'agent', None)
                if controller_agent is not None:
                    agent_session = getattr(controller_agent, 'session', None)
                    if agent_session is not None:
                        self._session = agent_session
                        self._history_version = getattr(agent_session, 'conversation_version', 0)
                        log('INFO', 'server.bridge',
                            f'Session {agent_session.session_id} propagated to bridge from controller agent')
                else:
                    log('WARNING', 'server.bridge',
                        '_on_controller_event: controller.agent is None, '
                        'session capture deferred to next event')
            except Exception as exc:
                log('WARNING', 'server.bridge',
                    f'_on_controller_event: session capture failed: {exc}')

        # ── Deferred config apply ─────────────────────────────────────────────
        # If a config was queued while the controller was busy, apply it now
        # that the controller is idle.  This runs on the controller's agent
        # thread (the callback is invoked from there) — safe because the agent
        # is READY, so no query is in flight.  The deferred config_changed is
        # broadcast through the normal forwarder (same payload shape as the
        # immediate server.py path).
        if (
            self._pending_config is not None
            and self._controller is not None
            and not self._controller.is_busy
        ):
            pending = self._pending_config
            self._pending_config = None
            try:
                result = self.apply_config(pending)
                if isinstance(result, dict) and "config" in result:
                    self._forwarder.broadcast(self._session_id, "config_changed", result)
                    log('INFO', 'server.bridge',
                        "Deferred (queued) config applied — controller idle")
                else:
                    err_msg = (
                        result.get('error', 'unknown error')
                        if isinstance(result, dict) else 'unknown error'
                    )
                    self._forwarder.broadcast(
                        self._session_id, "status_message",
                        {"text": f"⚠ Failed to apply queued config: {err_msg}"},
                    )
                    self._forwarder.broadcast(
                        self._session_id, "config_apply_failed",
                        {"text": f"⚠ Failed to apply queued config: {err_msg}"},
                    )
            except Exception as exc:
                log('ERROR', 'server.bridge',
                    f"Deferred config apply failed: {exc}")
                self._forwarder.broadcast(
                    self._session_id, "status_message",
                    {"text": f"⚠ Queued config apply failed: {exc}"},
                )
                self._forwarder.broadcast(
                    self._session_id, "config_apply_failed",
                    {"text": f"⚠ Queued config apply failed: {exc}"},
                )

        self._map_and_emit(event)

    def _core_persisted_error_notice(self, raw_event: Dict[str, Any]) -> bool:
        """Return True iff the bridge should SUPPRESS its transient ``⚠ Error``
        ``status_message`` for this error event because the core already
        persisted a corresponding notice.

        Suppression requires BOTH:

        1. **Session state** — ``self._session`` is set, its ``user_history`` is
           non-empty, and its LAST message is a ``[SYSTEM NOTIFICATION]`` whose
           ``content`` embeds ``raw_event['message']`` (correspondence prevents
           suppressing an *unrelated* trailing notice, e.g. a token warning).
           This is the only viable signal: the duplicate producers and the
           bridge-only ones can emit byte-identical event dicts (P5 and P7 both
           emit ``AGENT_CREATION_ERROR`` + ``message=str(e)`` + ``traceback``),
           so no event field distinguishes them.

        2. **Producer exclusion** — ``raw_event['error_type']`` must NOT be
           ``'AGENT_CREATION_ERROR'`` (P5 agent-start failure / P7 restart
           failure) nor ``'invalid_config'`` (P1 config-change failure). Those
           own a first-class UI surface and MUST always keep the bubble; P7
           additionally has no matching notice. Only P2 (LLMError), P3
           (ProviderError) and P4 (unexpected Exception) are suppressible.

        Only the ``status_message`` broadcast is guarded; the
        ``conversation_changed`` re-sync is unchanged.
        """
        if raw_event.get('error_type') in ('AGENT_CREATION_ERROR', 'invalid_config'):
            return False
        if self._session is None:
            return False
        raw_message = raw_event.get('message')
        if not raw_message:
            return False
        history = self._session.user_history
        if not history:
            return False
        last = history[-1]
        try:
            content = last.get('content', '') if hasattr(last, 'get') else ''
        except Exception:
            return False
        if not isinstance(content, str) or not content.startswith('[SYSTEM NOTIFICATION]'):
            return False
        return raw_message in content

    def _map_and_emit(self, raw_event: Dict[str, Any]) -> None:
        """
        Dual-stream event mapping: tick state and conversation changes
        from raw agent events to frontend protocol events.

        State stream: state_changed, tokens_updated, context_updated, status_message
        Conversation stream: conversation_changed (always sourced from session)
        """
        log('DEBUG', 'server.bridge',
            f'_map_and_emit ENTRY: event_type={raw_event.get("type", "unknown")!r}')
        event_type = raw_event.get("type", "")

        # ── 1. Sync conversation changes from session ──────────────────────
        # This picks up silent mutations (e.g. _pending_warnings flushed
        # after tool commit, errors added as system notifications) so they
        # arrive chronologically before the new event's content.
        if self._session is not None:
            current_version = self._session.conversation_version
            if current_version != self._history_version:
                self._history_version = current_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })
        else:
            log('DEBUG', 'server.bridge',
                f'_map_and_emit: session is None (event_type={event_type!r}), '
                f'skipping conversation_changed sync')

        # ── 2. Handle event-type-specific logic ───────────────────────────
        _is_busy = self._controller.is_busy if self._controller else False

        if event_type == "execution_state_change":
            new_state = raw_event.get("new_state", "")
            frontend_state = {
                "running": "RUNNING",
                "pausing": "PAUSING",
                "paused": "PAUSED",
                "idle": "IDLE",
                "error": "IDLE",
                "stopped": "IDLE",
                "completed": "IDLE",
                "ready": "IDLE",
                "waiting": "WAITING_FOR_USER",
            }.get(new_state.lower(), new_state.upper())
            self._forwarder.broadcast(self._session_id, "state_changed", {
                "state": frontend_state,
                "is_running": _is_busy,
            })

        elif event_type == "token_update":
            tokens_in = raw_event.get("total_input", 0)
            tokens_out = raw_event.get("total_output", 0)
            log('DEBUG', 'pipeline.hops',
                f"[PIPELINE:HOPS] bridge processing token_update from main agent: "
                f"tokens_in={tokens_in} tokens_out={tokens_out} "
                f"context_length={raw_event.get('context_length', '?')}")
            self._forwarder.broadcast(self._session_id, "tokens_updated", {
                "input": tokens_in,
                "output": tokens_out,
                "agent_type": "main",
            })
            ctx = raw_event.get("context_length", 0)
            if ctx is not None:
                if self._session is not None:
                    self._session.context_length = ctx
                log('DEBUG', 'pipeline.hops',
                    f"[PIPELINE:HOPS] bridge emitting context_updated for main agent: "
                    f"context_length={ctx}")
                self._forwarder.broadcast(self._session_id, "context_updated", {
                    "context_length": ctx,
                    "agent_type": "main",
                })

        elif event_type in ("token_warning", "turn_warning"):
            # Conversation should already reflect the warning in session;
            # re-read to ensure frontend gets the full snapshot.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        elif event_type == "tool_call":
            # Session has been updated by the agent; sync to frontend.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        elif event_type == "text_delta":
            # Token-level assistant text. Rendered into an in-flight bubble by
            # the frontend and reconciled against the terminal `turn`
            # conversation_changed. session_id rides INSIDE the payload because
            # the forwarder's broadcast() ignores its session_id argument.
            self._forwarder.broadcast(self._session_id, "assistant_delta", {
                "session_id": self._session_id,
                "text": raw_event.get("text", ""),
                "turn": raw_event.get("turn"),
            })

        elif event_type == "reasoning_delta":
            # Token-level model REASONING (the "thinking" phase). Forwarded
            # under the same event name so the frontend can paint it into the
            # in-flight bubble while the model is still thinking; without this
            # arm the reasoning is only visible at turn end (terminal `turn`
            # event / conversation_changed), so the bubble looks frozen.
            # session_id rides INSIDE the payload for the same reason as the
            # text_delta arm above: broadcast() ignores its session_id argument.
            self._forwarder.broadcast(self._session_id, "reasoning_delta", {
                "session_id": self._session_id,
                "text": raw_event.get("text", ""),
                "turn": raw_event.get("turn", 0),
            })

        elif event_type == "interrupted":
            # A hard stop truncated the stream. The agent already committed the
            # partial assistant text to the session (so the version sync above
            # has shipped it in a conversation_changed), and this event tells
            # the frontend to FOLD the in-flight bubble into the transcript
            # rather than wait for a turn event that will never arrive.
            # Same broadcast helper and payload shape as assistant_delta so
            # routing cannot drift; this is not a second stop terminal - the
            # controller still emits session_stop from the threaded stop_reason.
            self._forwarder.broadcast(self._session_id, "interrupted", {
                "session_id": self._session_id,
                "text": raw_event.get("content", "") or "",
                "stop_reason": raw_event.get("stop_reason", "interrupted"),
                "turn": raw_event.get("turn"),
            })

        elif event_type in ("user_query", "turn", "tool_result"):
            # Session has been updated by the agent; sync to frontend.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        elif event_type == "agent_responded":
            # Always sync conversation first — ensures frontend sees the agent's last message.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })
            # Then decide UI state based on response_type
            if raw_event.get('response_type') == 'question':
                self._forwarder.broadcast(self._session_id, "state_changed", {
                    "state": "WAITING_FOR_USER",
                    "is_running": _is_busy,
                })
            else:
                self._forwarder.broadcast(self._session_id, "state_changed", {
                    "state": "IDLE",
                    "is_running": _is_busy,
                })

        elif event_type == "error":
            error_type = raw_event.get('error_type', 'PROVIDER_ERROR')
            # Emit the transient ⚠ bubble UNLESS the core already persisted a
            # matching [SYSTEM NOTIFICATION] notice AND the producer is not an
            # excluded one (invalid_config / AGENT_CREATION_ERROR always emit;
            # see _core_persisted_error_notice). The status_message channel
            # itself is otherwise unchanged.
            if not self._core_persisted_error_notice(raw_event):
                msg_text = raw_event.get('message', 'unknown')
                self._forwarder.broadcast(self._session_id, "status_message", {
                    "text": f"⚠ Error: {msg_text}",
                })
            # Error may have added a system notification to session; sync it
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        # ── 3. Handle stop signals ────────────────────────────────────────
        if event_type == "session_stop":
            stop_reason = raw_event.get("stop_reason", "unknown")
            log('INFO', 'server.bridge',
                f'_map_and_emit: session_stop received | '
                f'stop_reason={stop_reason!r} | '
                f'_is_busy={_is_busy} | '
                f'_session={self._session is not None} | '
                f'controller_running={self._controller.is_running if self._controller else False}')
            if stop_reason:
                self._forwarder.broadcast(self._session_id, "state_changed", {
                    "state": "IDLE",
                    "is_running": _is_busy,
                })
            # Emit final conversation snapshot so the frontend gets the last
            # messages even if session_stop is the only event emitted.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._forwarder.broadcast(self._session_id, "conversation_changed", {
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })
                log('DEBUG', 'server.bridge',
                    f'session_stop: emitted final conversation_changed '
                    f'(version={self._history_version}, messages={len(self._session.user_history)})')
            return

        if raw_event.get("stop_reason"):
            self._forwarder.broadcast(self._session_id, "state_changed", {
                "state": "IDLE",
                "is_running": _is_busy,
            })

    # ── Internal: agent thread ──────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Background thread: owns the Agent and runs process_query()."""
        try:
            while self._running and not self._stop_event.is_set():
                # Check pause
                if not self._pause_event.is_set():
                    self._pause_event.wait(timeout=0.5)
                    continue

                # Wait for a query
                try:
                    query = self._query_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # Handle special commands
                if query == "[STOP]":
                    break
                if query == "[RESET]":
                    if self._agent is not None:
                        self._agent.reset()
                    continue

                # Process the query
                self._processing = True
                # A halt terminates the turn with stop_reason 'interrupted'; the
                # synthetic bridge terminal below must not mislabel that as a
                # clean completion, so the turn's own reason is carried out of
                # the loop and preferred over the 'completed' default.
                turn_stop_reason = None
                try:
                    for raw_event in self._agent.process_query(query):
                        if self._stop_event.is_set():
                            break

                        self._map_and_emit(raw_event)

                        # Publish raw event to global event bus for EventLogger
                        if EVENT_SYSTEM_AVAILABLE and global_event_bus is not None:
                            try:
                                from agent.events import convert_from_legacy_format
                                typed_event = convert_from_legacy_format(raw_event)
                                global_event_bus.publish(typed_event)
                            except Exception:
                                pass

                        # Check pause after each event
                        if not self._pause_event.is_set():
                            break

                        # If the agent yielded a stop_reason, break
                        if raw_event.get("stop_reason"):
                            turn_stop_reason = raw_event.get("stop_reason")
                            break
                except Exception as exc:
                    traceback.print_exc()
                    self._forwarder.broadcast(self._session_id, "error", {
                        "error_type": "PROCESS_QUERY_ERROR",
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    })
                finally:
                    self._processing = False
                    self._map_and_emit({
                        "type": "session_stop",
                        "stop_reason": turn_stop_reason or "completed",
                    })
        except Exception as exc:
            traceback.print_exc()
            self._forwarder.broadcast(self._session_id, "error", {
                "error_type": "BRIDGE_LOOP_ERROR",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            })
        finally:
            self._running = False
            self._forwarder.broadcast(self._session_id, "state_changed", {
                "state": "IDLE",
                "is_running": False,
            })
            self._forwarder.broadcast(self._session_id, "thread_finished", {})


# ══════════════════════════════════════════════════════════════════════════════
#  Convenience factory
# ══════════════════════════════════════════════════════════════════════════════

def create_bridge(event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> WebAgentBridge:
    """Factory function – create a WebAgentBridge with optional callback."""
    return WebAgentBridge(event_callback=event_callback)
