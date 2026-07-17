# tools/workspace/worker.py
"""
Worker — manage background / child worker processes.

Workers run as threads on the host, reusing the agent's LLM provider
configuration.  Each worker has its own persisted conversation context
stored in ``<workspace_dir>/workers/<name>/context.json``.

Actions
-------
list:
    List known worker definitions from workers.json.
spawn:
    Launch a worker thread from a definition in workers.json.
    The worker starts running immediately and waits for queries.
check:
    Query the runtime status of a spawned worker.
query:
    Send a message to a running worker and block for a response.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Union
from pydantic import Field, field_validator

from tools.base import ToolBase
from tools.utils import model_to_openai_tool
from agent.logging import log

# File lock for atomic writes (same pattern as FileSystemSessionStore)
try:
    from session.lock import FileLock
except ImportError:
    FileLock = None  # type: ignore

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from thoughtmachine.workspace_capabilities import (
        resolve_workspace_id,
        _load_template_workers,
        _workspace_dir,
    )
    CAPABILITIES_AVAILABLE = True
except ImportError:
    CAPABILITIES_AVAILABLE = False
    resolve_workspace_id = None
    _workspace_dir = None

# Optional: WorkerContext (imported eagerly — no circular dep)
try:
    from agent.core.worker_context import WorkerContext
except ImportError:
    WorkerContext = None  # type: ignore

# NOTE: Agent and AgentConfig are imported *lazily* inside
# WorkerThread._build_agent_config() to avoid a circular import with
# agent/core/agent.py which imports from tools (TOOL_CLASSES).

# Presenter components for per-worker event processing
StateBridge = None  # lazy-imported in _run_tool_loop

EventProcessor = None  # lazy-imported in _run_tool_loop

try:
    from agent.core.state import ExecutionState
except ImportError:
    ExecutionState = None

# Optional: security gate for worker permission checks
try:
    from security.security_gate import check_required_categories
    GATE_AVAILABLE = True
except ImportError:
    GATE_AVAILABLE = False
    check_required_categories = None  # type: ignore

# NullEventBus — used for worker security prompts where no interactive
# user is available to respond (returns "deny" instantly).
try:
    from agent.events import NullEventBus
    _NULL_EVENT_BUS = NullEventBus()
except ImportError:
    NullEventBus = None  # type: ignore
    _NULL_EVENT_BUS = None

try:
    from agent.events import EventBus, create_event, EventType, global_event_bus
except ImportError:
    EventBus = None
    create_event = None
    EventType = None
    global_event_bus = None


class WorkerBusAdapter:
    """Drop-in replacement for GUIIntegration that publishes to per-worker EventBus.

    Provides the same interface as GUIIntegration (state property, emit_* methods)
    but publishes events to the worker's EventBus instead of emitting Qt signals.
    """

    def __init__(self, event_bus, worker_name: str, state_bridge=None):
        self._event_bus = event_bus
        self.worker_name = worker_name
        self._state_bridge = state_bridge
        self._state = ExecutionState.READY if ExecutionState else None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            if self._event_bus is not None and create_event is not None and EventType is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_STATUS,
                        data={
                            "worker_name": self.worker_name,
                            "execution_state": new_state.value if hasattr(new_state, 'value') else str(new_state),
                            "old_execution_state": old_state.value if hasattr(old_state, 'value') else str(old_state),
                        },
                        source="worker",
                        session_id="",
                    )
                    self._event_bus.publish(evt)
                except Exception:
                    pass

    def emit_tokens_updated(self, total_input: int, total_output: int) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_tokens_updated: "
            f"worker={self.worker_name} total_input={total_input} total_output={total_output}")
        self._publish("tokens_updated", {"total_input": total_input, "total_output": total_output})

    def emit_context_updated(self, context_length: int) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_context_updated: "
            f"worker={self.worker_name} context_length={context_length}")
        self._publish("context_updated", {"context_length": context_length})

    def emit_status_message(self, message: str) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_status_message: "
            f"worker={self.worker_name} message={str(message)[:100]!r}")
        self._publish("status_message", {"message": str(message)[:500]})

    def emit_error_occurred(self, error: str, traceback_str: str) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_error_occurred: "
            f"worker={self.worker_name} error={str(error)[:100]!r}")
        self._publish("error_occurred", {"error": str(error)[:500], "traceback": str(traceback_str)[:2000]})

    def emit_config_changed(self, config: dict) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_config_changed: "
            f"worker={self.worker_name} config_keys={list(config.keys())}")
        self._publish("config_changed", {"config": config})

    def emit_conversation_changed(self) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_conversation_changed: "
            f"worker={self.worker_name}")
        self._publish("conversation_changed", {})

    def forward_agent_event(self, event: dict) -> None:
        """Publish an agent event to the per-worker EventBus for frontend consumption."""
        event_type = event.get("type", "")
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: "
            f"worker={self.worker_name} event_type={event_type!r} "
            f"session_id={event.get('session_id', 'N/A')}")

        if event_type == "agent_responded":
            content = event.get("content", "") or event.get("data", {}).get("content", "")
            reasoning = event.get("reasoning_content", "") or event.get("data", {}).get("reasoning_content", "")
            response_type = event.get("response_type", event.get("data", {}).get("response_type", "answer"))
            self._publish("worker_message", {
                "content": str(content)[:1000],
                "reasoning_content": str(reasoning)[:2000] if reasoning else None,
                "response_type": response_type,
            })

        elif event_type == "tool_call":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: tool_call "
                f"[worker={self.worker_name}] tool={event.get('tool_name', '?')}")
            self._publish("tool_call", {
                "tool_name": event.get("tool_name", ""),
                "arguments": event.get("arguments", {}),
            })

        elif event_type == "tool_result":
            result = event.get("result", "") or event.get("data", {}).get("result", "")
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: tool_result "
                f"[worker={self.worker_name}] tool={event.get('tool_name', '?')} "
                f"success={event.get('success', True)}")
            self._publish("tool_result", {
                "tool_name": event.get("tool_name", ""),
                "success": event.get("success", True),
                "error": event.get("error", "") or "",
                "result": str(result)[:1000] if result else "",
            })

        elif event_type == "token_warning":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: token_warning "
                f"[worker={self.worker_name}] token_count={event.get('token_count', 0)}")
            self._publish("token_warning", {
                "message": (event.get("message", "") or event.get("data", {}).get("message", ""))[:500],
                "warning_message": (event.get("warning_message", "") or event.get("data", {}).get("warning_message", ""))[:500],
                "token_count": event.get("token_count", 0),
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
            })

        elif event_type == "turn_warning":
            message = event.get("message", "") or event.get("data", {}).get("message", "")
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: turn_warning "
                f"[worker={self.worker_name}] turn_count={event.get('turn_count', 0)}")
            self._publish("turn_warning", {
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
                "turn_count": event.get("turn_count", 0),
                "warning_message": str(message)[:500],
            })

        elif event_type == "time_warning":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: time_warning "
                f"[worker={self.worker_name}] elapsed_seconds={event.get('elapsed_seconds', 0)}")
            self._publish("time_warning", {
                "message": (event.get("message", "") or event.get("data", {}).get("message", ""))[:500],
                "elapsed_seconds": event.get("elapsed_seconds", 0),
            })

        elif event_type == "turn":
            content = event.get("content", "") or event.get("data", {}).get("content", "")
            reasoning = event.get("reasoning_content", "") or event.get("data", {}).get("reasoning_content", "")
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: assistant_message "
                f"[worker={self.worker_name}] content_len={len(str(content))} "
                f"reasoning={bool(reasoning)}")
            self._publish("assistant_message", {
                "content": str(content)[:1000],
                "reasoning_content": str(reasoning)[:2000] if reasoning else None,
            })

        elif event_type == "system_notification":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: system_notification "
                f"[worker={self.worker_name}] "
                f"type_detail={event.get('type_detail', event.get('notification_type', 'N/A'))}")
            self._publish("system_notification", {
                "type": event.get("type_detail", event.get("notification_type", "general")),
                "message": (event.get("message", "") or event.get("data", {}).get("message", ""))[:500],
                "context_length": event.get("context_length", 0),
            })

        elif event_type == "context_summarized":
            log('DEBUG', 'pipeline.worker_bus',
                f"forward_agent_event: context_summarized [worker={self.worker_name}] "
                f"message={event.get('message', '')[:100]}")
            self._publish("context_summarized", {
                "token_count": event.get("token_count", 0),
                "message": str(event.get("message", "") or "")[:500],
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
            })

        elif event_type == "token_recovery":
            log('DEBUG', 'pipeline.worker_bus',
                f"forward_agent_event: token_recovery [worker={self.worker_name}] "
                f"token_count={event.get('token_count', 0)}")
            self._publish("token_recovery", {
                "token_count": event.get("token_count", 0),
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
                "recovery_message": str(event.get("recovery_message", "") or "")[:500],
            })

        elif event_type == "user_message" or event_type == "user_interaction_requested":
            # DIAG: user_message/user_interaction_requested deliberately NOT forwarded
            # (these come from the old _publish_event path instead)
            log('DEBUG', 'pipeline.worker_bus',
                f"forward_agent_event -> SKIPPED {event_type} [worker={self.worker_name}]")
            pass

    def _publish(self, event_type: str, data: dict) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"_publish [worker={self.worker_name}]: event_type={event_type!r}, "
            f"data_keys={list(data.keys())}, event_bus={'SET' if self._event_bus else 'NONE'}, "
            f"EventType={'AVAILABLE' if EventType else 'UNAVAILABLE'}")
        if self._event_bus is None or EventType is None:
            log('DEBUG', 'pipeline.worker_bus',
                f"_publish SKIPPED [worker={self.worker_name}]: "
                f"event_bus={'None' if self._event_bus is None else 'set'}, "
                f"EventType={'None' if EventType is None else 'set'}")
            return
        try:
            from agent.events import BaseEvent, EventMetadata
            evt = EventType(event_type) if isinstance(event_type, str) else event_type
            event = BaseEvent(
                type=evt,
                metadata=EventMetadata(source="worker", session_id=""),
                data={**data, "worker_name": self.worker_name},
            )
            self._event_bus.publish(event)
            log('DEBUG', 'pipeline.worker_bus',
                f"_publish SUCCESS [worker={self.worker_name}]: event_type={event_type!r}, "
                f"subscribers_count={len(self._event_bus._subscribers.get(evt, [])) if hasattr(self._event_bus, '_subscribers') else 'N/A'}")
        except Exception as exc:
            log('WARNING', 'tools.worker', f"WorkerBusAdapter._publish failed for {event_type}: {exc}")


class WorkerSessionLifecycle:
    """Minimal SessionLifecycle stub for worker event processing.

    Provides the state property and setter that EventProcessor expects,
    without session persistence or GUI callback registration.
    """

    def __init__(self):
        self._state = ExecutionState.READY if ExecutionState else None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if self._state != new_state:
            self._state = new_state

    def mark_clean(self) -> None:
        pass

    def has_unsaved_changes(self) -> bool:
        return False


logger = logging.getLogger(__name__)

# Tools excluded from workers for safety reasons
# Workers could spawn other workers (recursion), manage containers, or
# modify workspace infrastructure — all operations reserved for the
# main agent / human user.
_WORKER_BLOCKLIST: frozenset[str] = frozenset({
    "Worker",           # recursion: worker spawning workers
    "EditDockerfile",    # container configuration
    "MCPValidator",      # MCP server management
})

# Default system prompt for worker sub-agents.
# This is a fixed instruction rather than inheriting the main agent's prompt,
# preventing delegation loops in Engineer mode.
DEFAULT_WORKER_SYSTEM_PROMPT = (
    "You are a capable autonomous sub-agent of ThoughtMachine. "
    "Complete the task given to you thoroughly, using all available tools. "
    "Think, research, write, edit, test, review. "
    "When finished, use the Respond tool to return your final result. "
    "Be concise but complete. "
    "Do not ask the user for clarification — the main agent already understood the request."
)

# Global tool name → class registry (built from tools.__init__.TOOL_CLASSES)
# Resolved at import time — tools registered after this module loads
# (Worker, EditDockerfile) are all in the blocklist, so no gap.
from tools import TOOL_CLASSES as _TOOL_CLASSES_LIST
_TOOL_REGISTRY: dict[str, type["ToolBase"]] = {
    cls.__name__: cls for cls in _TOOL_CLASSES_LIST
}

# ---------------------------------------------------------------------------
# Shutdown helper  (exposed for bridge integration)
# ---------------------------------------------------------------------------

def shutdown_workers(timeout: float = 5.0) -> None:
    """
    Gracefully stop all registered worker threads and persist their context.

    Called from an ``atexit`` handler and from the bridge's ``close_session``
    so that partial conversation state is not lost when the process exits or
    a session is closed with active workers.
    """
    with _registry_lock:
        keys = list(_worker_registry.keys())
    for key in keys:
        with _registry_lock:
            thread = _worker_registry.get(key)
        if thread is None or not thread.is_alive():
            continue
        worker_label = key[1] if isinstance(key, tuple) else str(key)
        logger.info("Shutting down worker '%s' (status=%s)", worker_label, thread.status)
        try:
            thread.stop()
            thread.join(timeout=timeout)
        except Exception:
            logger.exception("Error joining worker '%s' during shutdown", worker_label)
        finally:
            try:
                thread._save_context()
            except Exception:
                logger.exception("Error saving context for worker '%s' during shutdown", worker_label)

# Register atexit handler
atexit.register(shutdown_workers)

def register_worker_event_bus(session_id: str, worker_name: str, event_bus: Any) -> None:
    """
    Register a worker's per-worker EventBus so the bridge can discover it
    and subscribe to detailed events (tool_call, tool_result, etc.).
    """
    key = (session_id or "", worker_name)
    with _bus_registry_lock:
        _worker_event_bus_registry[key] = event_bus

def unregister_worker_event_bus(session_id: str, worker_name: str) -> None:
    """Unregister a worker's per-worker EventBus."""
    key = (session_id or "", worker_name)
    with _bus_registry_lock:
        _worker_event_bus_registry.pop(key, None)

def get_worker_event_bus(session_id: str, worker_name: str) -> Any:
    """Get a worker's per-worker EventBus, or None if not registered."""
    key = (session_id or "", worker_name)
    with _bus_registry_lock:
        return _worker_event_bus_registry.get(key)


def get_worker_event_buses_for_session(session_id: str) -> Dict[str, Any]:
    """Return dict of {worker_name: EventBus} for all registered workers in a session.

    Used by late-arriving bridges to discover already-running workers
    whose WORKER_SPAWNED event was published before the bridge subscribed.
    """
    result: Dict[str, Any] = {}
    with _bus_registry_lock:
        for (sid, wname), bus in _worker_event_bus_registry.items():
            if sid == (session_id or ""):
                result[wname] = bus
    return result


# ---------------------------------------------------------------------------
# Module-level worker registry  (persists across tool calls)
# ---------------------------------------------------------------------------

_worker_registry: dict = {}
_registry_lock = threading.Lock()

# Per-worker EventBus registry (for bridge discovery of per-worker buses)
_worker_event_bus_registry: Dict[Tuple[str, str], Any] = {}
_bus_registry_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Restrictive merge for permission ceiling enforcement
# ---------------------------------------------------------------------------

# Session permissions act as the ceiling — worker can only reduce strictness
_STRICTNESS_ORDER: dict[str, int] = {
    "container": 0,
    "filesystem": 1,
    "execution": 2,
    "network": 3,
}

# Value ordering within each category: stricter = lower index
# "deny" is stricter than "allow", "none" is stricter than "read"
_PERMISSION_ORDER: dict[str, dict[str, int]] = {
    "container": {"deny": 0, "allow": 1},
    "filesystem": {"none": 0, "read": 1, "write": 2},
    "execution": {"deny": 0, "allow": 1},
    "network": {"deny": 0, "allow": 1},
}

def _restrictive_merge(
    session_perms: dict[str, Any],
    worker_perms: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge worker permissions into session permissions, picking the *more
    restrictive* (stricter) value for each key.

    The session acts as the ceiling — a worker cannot exceed the session's
    permission level for any category.  Values may be booleans (``False`` =
    deny, ``True`` = allow) or strings whose strictness order is defined
    by ``_PERMISSION_ORDER``.

    Examples
    --------
    >>> _restrictive_merge({"execution": "allow"}, {"execution": "deny"})
    {'execution': 'deny'}
    >>> _restrictive_merge({"execution": "deny"}, {"execution": "allow"})
    {'execution': 'deny'}        # session ceiling wins
    >>> _restrictive_merge({"filesystem": "none"}, {"filesystem": "write"})
    {'filesystem': 'none'}
    >>> _restrictive_merge({"container": False}, {"container": True})
    {'container': False}         # session ceiling wins
    """
    result = {}
    all_keys = set(session_perms) | set(worker_perms)
    for key in all_keys:
        s_val = session_perms.get(key)
        w_val = worker_perms.get(key)

        if s_val is None:
            result[key] = w_val  # worker fills in
        elif w_val is None:
            result[key] = s_val  # session provides
        elif isinstance(s_val, bool) or isinstance(w_val, bool):
            # Boolean case: False (deny) is stricter than True (allow)
            result[key] = False if (s_val is False or w_val is False) else True
        else:
            # Both are strings — pick the stricter one by order map
            order = _PERMISSION_ORDER.get(key, {})
            s_rank = order.get(s_val, 99)
            w_rank = order.get(w_val, 99)
            # Lower rank = stricter
            result[key] = s_val if s_rank <= w_rank else w_val
    return result

# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class WorkerThread(threading.Thread):
    """
    A worker runs as a daemon thread on the host, reusing the agent's
    LLM provider configuration.  It maintains a conversation context
    persisted to ``<workspace_dir>/workers/<name>/context.json``.

    Lifecycle
    ---------
    ready  ──spawn──▶  ready  ──query──▶  busy  ──done──▶  ready
      ▲                    │            │                   │
      │                    ├──stop──▶  completed            │
      │                    ├──pause──▶ paused ──resume──▶  ready
      │                    └──error──▶  error               │
      └───────────────────────────  spawn again  ◄──────────┘
    """

    def __init__(
        self,
        name: str,
        definition: dict,
        agent_config: dict,
        workspace_dir: Path,
        tool_classes: Optional[Dict[str, type[ToolBase]]] = None,
        session_permissions: Optional[Dict[str, Any]] = None,
        project_root: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> None:
        super().__init__(daemon=True, name=f"worker-{name}")
        self.worker_name = name
        self.definition = definition
        self._agent_config_dict = agent_config
        self.session_id = session_id
        self._worker_dir = workspace_dir / "workers" / name
        self._worker_dir.mkdir(parents=True, exist_ok=True)

        # Tool classes available to this worker (name -> class)
        self._tool_classes: Dict[str, type[ToolBase]] = tool_classes or {}

        # Session permissions for gate-checking tool calls
        self._session_permissions: Dict[str, Any] = session_permissions or {}
        # Worker-level permission footprint from definition
        self._permission_footprint: Dict[str, Any] = definition.get("permission_footprint") or definition.get("worker_permissions", {})

        # Project root from the session (resolved from workspace config)
        self._project_root: Optional[str] = project_root

        # Override timeout (from spawn parameter, else from definition, else 600)
        self._timeout_seconds: int = (
            timeout_seconds
            if timeout_seconds is not None
            else definition.get("timeout_seconds", 600)
        )

        # Runtime state
        self.status: str = "ready"      # ready | busy | paused | completed | error
        self.current_task: Optional[str] = None
        self.error: Optional[str] = None
        self.last_heartbeat: Optional[str] = None
        self._last_reasoning: Optional[str] = None

        # Cached authoritative token count from agent's token_update events
        self._cached_context_tokens: Optional[int] = None

        # Agent instance + WorkerContext (created lazily in run())
        self._agent: Optional[Any] = None
        self._worker_ctx: Optional[Any] = None
        self._initial_context: Optional[Dict[str, Any]] = None

        # Per-worker EventBus (created lazily in run() before Agent)
        self._event_bus: Optional[Any] = None

        # Presenter components (created lazily in _run_tool_loop)
        self._state_bridge: Optional[Any] = None
        self._event_processor: Optional[Any] = None
        self._worker_bus_adapter: Optional[Any] = None

        # Inter-thread communication
        self._input_queue: queue.Queue = queue.Queue()
        self._output_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._resume_event = threading.Event()

    # ── public API called from the tool thread ─────────────────────

    @property
    def event_bus(self):
        """Return the per-worker EventBus instance."""
        return self._event_bus

    @property
    def max_context_tokens(self) -> int:
        """
        Return the context window limit for this worker's model.

        Derives the limit from the model name in the injected
        ``_agent_config_dict``, using the same lookup as
        ``TokenCounter.get_model_context_window()``.

        Returns:
            Maximum context tokens (int). Defaults to 128000.
        """
        model = (self._agent_config_dict or {}).get("model", "").lower()
        windows = {
            "gpt-4-32k": 32768,
            "gpt-4-turbo": 128000,
            "gpt-4o": 128000,
            "gpt-4": 8192,
            "gpt-3.5-turbo": 16385,
            "gpt-3.5": 16385,
            "deepseek": 128000,
            "claude-3-opus": 200000,
            "claude-3-sonnet": 200000,
            "claude-3-haiku": 200000,
            "claude": 200000,
        }
        for key, window in windows.items():
            if key in model:
                return window
        return 128000

    def get_current_context_tokens(self) -> int:
        """
        Return the current conversation's token count.

        Prefers the authoritative value from ``StateBridge.context_length``
        (updated by ``EventProcessor`` from ``token_update`` events). Falls
        back to the legacy ``_cached_context_tokens`` (still written by the
        summarization-detection code), then estimation via
        ``WorkerContext.estimated_context_tokens()``.

        Returns:
            Token count (int).
        """
        if self._state_bridge is not None and self._state_bridge.context_length > 0:
            return self._state_bridge.context_length
        if self._cached_context_tokens is not None:
            return self._cached_context_tokens
        if self._worker_ctx is not None:
            return self._worker_ctx.estimated_context_tokens()
        return 0

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        """Send a query to this worker and block for a response."""
        # Push query before checking heartbeat (query clears the queue)
        self._input_queue.put(query)
        try:
            response = self._output_queue.get(timeout=timeout)
            return response
        except queue.Empty:
            # Fallback: read heartbeat from status.json if in-memory is None
            hb = self.last_heartbeat
            if hb is None:
                try:
                    status_path = self._worker_dir / "status.json"
                    if status_path.exists():
                        data = json.loads(status_path.read_text(encoding="utf-8"))
                        hb = data.get("last_heartbeat")
                except (OSError, json.JSONDecodeError):
                    pass
            if hb:
                try:
                    hb_dt = datetime.fromisoformat(hb)
                    age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                    detail = f" (last heartbeat {age:.0f}s ago)"
                except (ValueError, TypeError):
                    detail = f" (last heartbeat: {hb})"
            else:
                detail = ""
            raise TimeoutError(
                f"Worker '{self.worker_name}' did not respond within {timeout}s{detail}"
            )

    def stop(self) -> None:
        """Signal the worker to stop after completing its current task."""
        self._stop_event.set()
        # Write a stop command file for cross-process signalling
        try:
            cmd_path = self._worker_dir / "command.json"
            cmd_path.write_text(json.dumps({"action": "stop"}), encoding="utf-8")
        except OSError:
            pass
        # Unblock the input queue wait
        self._input_queue.put(None)

    def pause(self) -> None:
        """Signal the worker to pause after completing its current task."""
        self._pause_event.set()
        self._resume_event.clear()
        # Write command file for cross-process signalling
        try:
            cmd_path = self._worker_dir / "command.json"
            cmd_path.write_text(json.dumps({"action": "pause"}), encoding="utf-8")
        except OSError:
            pass
        # Unblock the input queue wait so it can check the pause event
        self._input_queue.put(None)

    def resume(self) -> None:
        """Resume a paused worker."""
        self._pause_event.clear()
        self._resume_event.set()
        self.status = "ready"
        self._write_status_file()

    def _poll_command(self) -> None:
        """
        Check for a ``command.json`` file in the worker's directory.

        If found and the action is ``"stop"``, delete the file and signal
        the stop event.  This enables cross-process stop (e.g. from the
        Web UI via the REST API).
        """
        cmd_path = self._worker_dir / "command.json"
        if not cmd_path.is_file():
            return
        try:
            data = json.loads(cmd_path.read_text(encoding="utf-8"))
            action = data.get("action")
            if action == "stop":
                cmd_path.unlink(missing_ok=True)
                self._stop_event.set()
                self._input_queue.put(None)
            elif action == "pause":
                cmd_path.unlink(missing_ok=True)
                self._pause_event.set()
                self._input_queue.put(None)
        except (json.JSONDecodeError, OSError):
            # Corrupted file — delete and ignore
            try:
                cmd_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _last_elapsed(self) -> Optional[float]:
        """Return elapsed seconds from the most recent query execution."""
        return getattr(self, '_last_elapsed_val', None)

    def _build_agent_config(self) -> Any:
        """
        Build an AgentConfig from the worker definition and parent agent config.

        Imports ``AgentConfig`` lazily to avoid circular imports with
        ``agent.core.agent`` which pulls in ``tools.TOOL_CLASSES``.

        The worker inherits all fields from the parent (ToolExecutor-injected)
        config dict, then overrides only worker-specific settings
        (system_prompt, enabled_tools, max_turns, timeout_seconds, stop_check).

        Also injects ``session_permissions`` so that tools running inside
        the worker use the same security policy as the parent agent.

        Returns None if AgentConfig is not importable, or if provider/model
        are missing.
        """
        try:
            from agent.config.models import AgentConfig
        except ImportError:
            logger.warning("AgentConfig not available — cannot create worker agent")
            return None

        cfg = self._agent_config_dict or {}
        provider_type = cfg.get("provider", "")
        model = cfg.get("model", "")
        if not provider_type or not model:
            logger.warning(
                "agent_config missing provider (%s) or model (%s)",
                provider_type, model,
            )
            return None

        # ── Forward the full parent config dict ────────────────────────
        worker_cfg = dict(cfg)
        # The ToolExecutor injects the key as ``provider``, but AgentConfig
        # expects the Pydantic field name ``provider_type``.
        worker_cfg["provider_type"] = worker_cfg.pop("provider")

        # ── Worker-specific overrides ──────────────────────────────────
        worker_cfg["system_prompt"] = self.definition.get(
            "system_prompt",
            DEFAULT_WORKER_SYSTEM_PROMPT,
        )
        worker_tools = self.definition.get("tools", [])
        if worker_tools:
            enabled_tools = [t for t in worker_tools if t not in _WORKER_BLOCKLIST]
        else:
            from tools import SIMPLIFIED_TOOL_CLASSES
            enabled_tools = [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES
                             if cls.__name__ not in _WORKER_BLOCKLIST]
        worker_cfg["enabled_tools"] = enabled_tools
        worker_cfg["max_turns"] = self.definition.get(
            "max_turns", cfg.get("max_turns", 100)
        )
        # Override token monitor warning threshold from worker definition
        warning_threshold = self.definition.get("warning_threshold_tokens", None)
        if warning_threshold is not None:
            worker_cfg["token_monitor_warning_threshold"] = warning_threshold

        # Override token monitor critical threshold from worker definition
        critical_threshold = self.definition.get("critical_threshold_tokens", None)
        if critical_threshold is not None:
            worker_cfg["token_monitor_critical_threshold"] = critical_threshold
        worker_cfg["timeout_seconds"] = self._timeout_seconds
        # Warn at 80% of timeout (minimum 5s) so CRITICAL triggers before
        # the hard cutoff when using the worker tool's timeout window.
        worker_cfg["time_warning_threshold"] = max(
            5, int(self._timeout_seconds * 0.8)
        )
        worker_cfg["time_monitor_enabled"] = True
        worker_cfg["stop_check"] = self._stop_event.is_set

        # ── Inject session permissions (restrictive merge — session is ceiling) ──
        merged = _restrictive_merge(self._session_permissions, self._permission_footprint)
        worker_cfg["session_permissions"] = merged

        # ── Safety net: inject workspace_path if missing ────────────────
        # Worker._build_agent_config (the Tool-class method) now injects
        # workspace_path from the parent, but in case that fails (e.g., the
        # path was not available at the tool level), fall back to the
        # project_root that was resolved from workspace_dir/config.json.
        if "workspace_path" not in worker_cfg and self._project_root:
            worker_cfg["workspace_path"] = self._project_root

        # Mark config as running inside a worker for security context
        worker_cfg["worker_mode"] = True

        log('DEBUG', 'core.token', f"Worker agent config: token_monitor_warning_threshold={worker_cfg.get('token_monitor_warning_threshold', 'NOT SET')} token_monitor_critical_threshold={worker_cfg.get('token_monitor_critical_threshold', 'NOT SET')} max_turns={worker_cfg.get('max_turns', 'NOT SET')} timeout_seconds={worker_cfg.get('timeout_seconds', 'NOT SET')}")

        return AgentConfig(**worker_cfg)

    def _run_tool_loop(
        self,
        query: str,
    ) -> str:
        """
        Run the agent conversation loop for a single query using Agent.process_query().

        Iterates over all events yielded by the agent, logging heartbeats
        and checking the stop event. Returns the final response text.
        """
        if self._agent is None:
            return json.dumps({"error": "Agent not initialized"})

        self.current_task = query[:200]
        final_content = ""
        _start = time.monotonic()

        # Lazy-init presenter components for event processing
        if self._state_bridge is None:
            try:
                from agent.presenter.state_bridge import StateBridge
            except ImportError:
                log('WARNING', 'tools.worker', 'Failed to lazy-import StateBridge')
                StateBridge = None
            try:
                from agent.presenter.event_processor import EventProcessor
            except ImportError:
                log('WARNING', 'tools.worker', 'Failed to lazy-import EventProcessor')
                EventProcessor = None
            if StateBridge is not None and ExecutionState is not None:
                log('DEBUG', 'pipeline.worker',
                    f"Lazy-init presenter [worker={self.worker_name}]: creating WorkerBusAdapter "
                    f"with event_bus={'SET' if self._event_bus else 'NONE'}")
                self._state_bridge = StateBridge(config_path=None)
                bus_adapter = WorkerBusAdapter(self._event_bus, self.worker_name, state_bridge=self._state_bridge)
                self._worker_bus_adapter = bus_adapter
                session_lifecycle = WorkerSessionLifecycle()
                self._event_processor = EventProcessor(
                    state_bridge=self._state_bridge,
                    session_lifecycle=session_lifecycle,
                    gui_integration=bus_adapter,
                )
                log('DEBUG', 'pipeline.worker',
                    f"Lazy-init complete [worker={self.worker_name}]: "
                    f"worker_bus_adapter={'SET' if self._worker_bus_adapter else 'NONE'}, "
                    f"event_processor={'SET' if self._event_processor else 'NONE'}")

        # Log the user query as a user_message event
        self._publish_event("user_message", {"query": query})

        # Check stop before starting tool loop
        if self._stop_event.is_set():
            return json.dumps({
                "status": "stopped",
                "message": "Worker stopped before processing query",
            })

        try:
            for event in self._agent.process_query(query):
                                # Fix 1.1B: Poll for stop command on every event
                self._poll_command()

                # Log heartbeat for liveliness checks and flush to disk
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()
                self._write_status_file()

                # Check stop signal
                if self._stop_event.is_set():
                    self._agent.request_pause()
                    final_content = json.dumps({
                        "status": "stopped",
                        "message": "Worker stopped by user",
                    })
                    break

                # Check pause signal
                if self._pause_event.is_set():
                    self._agent.request_pause()
                    self.status = "paused"
                    self._write_status_file()
                    self._publish_event('worker_paused', {'status': 'paused', 'worker_name': self.worker_name})
                    final_content = json.dumps({
                        "status": "paused",
                        "message": "Worker paused by user",
                    })
                    break

                event_type = event.get("type", "")

                # Feed each event through the EventProcessor BEFORE the existing chain
                if self._event_processor is not None:
                    try:
                        self._event_processor.process_event(event)
                    except Exception as exc:
                        log('WARNING', 'tools.worker', f"EventProcessor failed for {event_type}: {exc}")

                # Forward event to the per-worker EventBus via the bus adapter
                # (replaces the old manual self._publish_event() calls for
                #  tool_call, tool_result, token_warning, turn_warning,
                if self._worker_bus_adapter is not None:
                    log('TRACE', '[WORKER_EVENT_OBSERVE]', f"event: type={event.get('type')}, yielding to presenter")
                    try:
                        self._worker_bus_adapter.forward_agent_event(event)
                    except Exception as exc:
                        log('WARNING', 'tools.worker', f"forward_agent_event failed for {event_type}: {exc}")

                # Capture final response content and reasoning
                if event_type == "agent_responded":
                    final_content = event.get("content", "")
                    self._last_reasoning = event.get("reasoning")

                elif event_type == "stopped":
                    stop_reason = event.get("stop_reason", "unknown")
                    if stop_reason == "timeout":
                        final_content = json.dumps({
                            "error": "Worker execution timed out",
                        })
                    elif stop_reason == "max_turns_reached":
                        final_content = json.dumps({
                            "error": "Worker reached max turns",
                        })
                    break

                elif event_type == "stop_reason":
                    reason = event.get("stop_reason", "unknown")
                    if reason == "max_turns_reached":
                        final_content = json.dumps({
                            "error": "Worker reached maximum turns",
                        })
                    break

                elif event_type == "error":
                    error_msg = event.get("error", "Unknown error")
                    final_content = json.dumps({"error": error_msg})
                    break

                # [PIPELINE:HOPS] Cache the agent's authoritative post-prune token count
                if event_type == "token_update":
                    log('DEBUG', 'pipeline.hops',
                        f"[PIPELINE:HOPS] Received token_update event: "
                        f"worker={self.worker_name} "
                        f"context_length={event.get('context_length', '?')} "
                        f"total_input={event.get('total_input', '?')} "
                        f"total_output={event.get('total_output', '?')}")
                    context_length = event.get("context_length")
                    if context_length is not None:
                        self._cached_context_tokens = int(context_length)

                        # Emit to per-worker bus so the bridge forwards to frontend
                        if self._worker_bus_adapter is not None:
                            self._worker_bus_adapter.emit_context_updated(int(context_length))

                # Keep legacy tool_execution handler for backward compatibility
                if event_type == "tool_execution":
                    tool_name = event.get("tool_name", "")
                    tool_args = event.get("tool_args", {})
                    result = event.get("result", "")

        except Exception as exc:
            logger.exception("Worker _run_tool_loop failed")
            final_content = json.dumps({"error": f"Worker execution failed: {exc}"})

        self.current_task = None
        # Store elapsed time for inclusion in query result
        self._last_elapsed_val = time.monotonic() - _start
        return final_content

    # ── thread run loop ────────────────────────────────────────────

    def run(self) -> None:
        """Main worker loop — processes queries until stop is signalled.

        Creates the Agent and WorkerContext lazily on first query,
        reusing them for subsequent queries to maintain conversation state.
        """
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()

        try:
            # ── Load persisted context or create fresh ────────────────
            self._worker_ctx = self._load_context()

            # Override persisted status/error with live thread state
            self.status = "ready"
            self.error = None
            self._write_status_file()
            # Create per-worker EventBus early so _publish_event below works
            self._event_bus = EventBus()
            register_worker_event_bus(self.session_id or "", self.worker_name, self._event_bus)
            # Attach EventLogger to this worker's per-worker bus
            try:
                from agent.logging.event_logger import EventLogger
                EventLogger.instance().attach_worker_bus(self.worker_name, self._event_bus)
            except Exception:
                pass
            # Publish WORKER_SPAWNED to the global bus so the bridge can
            # discover and subscribe to this worker's per-worker EventBus
            # immediately, rather than waiting for the first query.
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_SPAWNED,
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                            "status": "ready",
                        },
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
            # Also publish to the per-worker bus for local subscribers
            self._publish_event('worker_spawned', {'status': 'ready'})
            if self._worker_ctx is None:
                # Load system prompt from definition
                system_prompt = self.definition.get(
                    "system_prompt",
                    "You are a helpful worker assistant."
                )
                user_history = [
                    {"role": "system", "content": system_prompt}
                ]
                if self._initial_context:
                    user_history.append({
                        "role": "system",
                        "content": f"Initial context: {json.dumps(self._initial_context, default=str)}",
                    })
                # Reset cached token count for a fresh run
                self._cached_context_tokens = None
                self._worker_ctx = WorkerContext(
                    worker_name=self.worker_name,
                    user_history=user_history,
                )

                # ── Fix 2: Auto-process initial context query ──
                # When spawn provides context with a "query" key, the worker
                # should start working immediately without requiring a
                # separate query action. We push the query into the input
                # queue so the existing loop (agent creation + _run_tool_loop)
                # handles it naturally.
                if self._initial_context:
                    initial_query = self._initial_context.get("query")
                    if initial_query:
                        logger.debug(
                            "Auto-queueing initial query for worker '%s'",
                            self.worker_name,
                        )
                        self._input_queue.put(initial_query)

            else:
                # ── Context loaded from disk: merge initial_context if provided ──
                # When context.json exists, we preserve the loaded conversation and
                # merge _initial_context into it rather than replacing it.
                if self._initial_context:
                    # Add initial context as a system message for continuity
                    ctx_msg = {
                        "role": "system",
                        "content": f"Initial context: {json.dumps(self._initial_context, default=str)}",
                    }
                    # Check if this initial_context was already injected (avoid duplicates
                    # on repeated spawn calls)
                    already_present = any(
                        msg.get("content", "") == ctx_msg["content"]
                        for msg in self._worker_ctx.user_history
                    )
                    if not already_present:
                        self._worker_ctx.user_history.append(ctx_msg)
                        logger.debug(
                            "Merged initial_context into loaded context for worker '%s'",
                            self.worker_name,
                        )
                    # Auto-queue the query if present
                    initial_query = self._initial_context.get("query")
                    if initial_query:
                        logger.debug(
                            "Auto-queueing initial query for worker '%s' (loaded context)",
                            self.worker_name,
                        )
                        self._input_queue.put(initial_query)

            self._save_context()

            while not self._stop_event.is_set():
                # Check for command.json before blocking on input queue
                self._poll_command()
                if self._stop_event.is_set():
                    break
                # Wait for the next query with a 2-second timeout
                # so we can also poll for command.json periodically
                try:
                    query = self._input_queue.get(timeout=2.0)
                except queue.Empty:
                    continue
                if query is None or self._stop_event.is_set():
                    break

                # ── Create Agent lazily (first query only) ────────────
                if self._agent is None:
                    agent_cfg = self._build_agent_config()
                    if agent_cfg is None:
                        reply = json.dumps({
                            "error": "Cannot create Agent: invalid agent_config"
                        })
                        self._output_queue.put(reply)
                        break
                    # Lazy import to avoid circular dep: agent.core.agent ↔ tools
                    try:
                        from agent.core.agent import Agent
                    except ImportError:
                        reply = json.dumps({
                            "error": "Cannot create Agent: module not importable"
                        })
                        self._output_queue.put(reply)
                        break

                    # Publish WORKER_SPAWNED is now handled earlier in run() —
                    # no need for a duplicate lazy publish here.
                    self._agent = Agent(
                        config=agent_cfg,
                        session=self._worker_ctx,
                        event_bus=self._event_bus,
                    )

                # ── Fix 1.2: Set busy before processing query ───────────
                self.status = "busy"
                self._write_status_file()
                self._publish_event('worker_status', {'status': 'busy', 'current_task': self.current_task})
                # Also publish to global_event_bus so the bridge receives it
                if global_event_bus is not None and EventType is not None and create_event is not None:
                    try:
                        evt = create_event(
                            EventType.WORKER_STATUS,
                            data={
                                "session_id": self.session_id or "",
                                "worker_name": self.worker_name,
                                "status": "busy",
                                "current_task": self.current_task,
                                "current_context_tokens": self.get_current_context_tokens(),
                                "max_context_tokens": self.max_context_tokens,
                            },
                            source=f"worker:{self.worker_name}",
                            session_id=self.session_id or "",
                        )
                        global_event_bus.publish(evt)
                    except Exception:
                        pass
                reply = self._run_tool_loop(query)

                # Check if worker was paused during the tool loop
                if self._pause_event.is_set():
                    # Preserve paused status — don't overwrite with "ready"
                    self.status = "paused"
                    self._write_status_file()
                    self._publish_event('worker_paused', {'status': 'paused', 'worker_name': self.worker_name})
                    # Also publish to global_event_bus so the bridge receives it
                    if global_event_bus is not None and EventType is not None and create_event is not None:
                        try:
                            evt = create_event(
                                EventType.WORKER_STATUS,
                                data={
                                    "session_id": self.session_id or "",
                                    "worker_name": self.worker_name,
                                    "status": "paused",
                                    "current_context_tokens": self.get_current_context_tokens(),
                                    "max_context_tokens": self.max_context_tokens,
                                },
                                source=f"worker:{self.worker_name}",
                                session_id=self.session_id or "",
                            )
                            global_event_bus.publish(evt)
                        except Exception:
                            pass

                    self.current_task = None
                    self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                    # Persist context before blocking
                    if self._worker_ctx is not None:
                        self._worker_ctx.compact_after_summary()
                    self._save_context()

                    # Send the pause response back to the waiting tool call
                    self._output_queue.put(reply)

                    # Block until resumed (or stopped)
                    while self._pause_event.is_set() and not self._stop_event.is_set():
                        self._resume_event.wait(1.0)

                    if self._stop_event.is_set():
                        break

                    # Resume — transition back to ready
                    self.status = "ready"
                    self._resume_event.clear()
                    self._write_status_file()
                    self._publish_event('worker_resumed', {'status': 'ready', 'worker_name': self.worker_name})
                    if global_event_bus is not None and EventType is not None and create_event is not None:
                        try:
                            evt = create_event(
                                EventType.WORKER_STATUS,
                                data={
                                    "session_id": self.session_id or "",
                                    "worker_name": self.worker_name,
                                    "status": "ready",
                                    "current_context_tokens": self.get_current_context_tokens(),
                                    "max_context_tokens": self.max_context_tokens,
                                },
                                source=f"worker:{self.worker_name}",
                                session_id=self.session_id or "",
                            )
                            global_event_bus.publish(evt)
                        except Exception:
                            pass

                    # Continue the outer loop to wait for next query
                    continue

                # Back to ready after query completes (not paused)
                self.status = "ready"
                self._write_status_file()
                self._publish_event('worker_status', {'status': 'ready'})
                # Also publish to global_event_bus so the bridge receives it
                if global_event_bus is not None and EventType is not None and create_event is not None:
                    try:
                        evt = create_event(
                            EventType.WORKER_STATUS,
                            data={
                                "session_id": self.session_id or "",
                                "worker_name": self.worker_name,
                                "status": "ready",
                                "current_context_tokens": self.get_current_context_tokens(),
                                "max_context_tokens": self.max_context_tokens,
                            },
                            source=f"worker:{self.worker_name}",
                            session_id=self.session_id or "",
                        )
                        global_event_bus.publish(evt)
                    except Exception:
                        pass

                self.current_task = None
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                # Compact conversation history after summarization
                # (Agent inserts summary messages but doesn't remove old ones)
                if self._worker_ctx is not None:
                    self._worker_ctx.compact_after_summary()

                # Persist and log
                self._save_context()

                # Send the response back to the waiting tool call
                self._output_queue.put(reply)

        except Exception as exc:
            logger.exception("Worker thread %s failed", self.worker_name)
            self.status = "error"
            self.error = str(exc)
            self._write_status_file()
            # Put the error into the output queue so any waiting query call gets it
            error_json = json.dumps({"error": str(exc)})
            try:
                self._output_queue.put_nowait(error_json)
            except queue.Full:
                pass
            self._publish_event('worker_error', {'error': str(exc)})
            # Also publish to global_event_bus so the bridge receives it
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_ERROR,
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "error": str(exc),
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                        },
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
        else:
            self.status = "completed"
            self._write_status_file()
            self._publish_event('worker_completed', {'status': 'completed', 'worker_name': self.worker_name})
            # Also publish to global_event_bus so the bridge receives it
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_COMPLETED,
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "status": "completed",
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                        },
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
        finally:
            try:
                unregister_worker_event_bus(self.session_id or "", self.worker_name)
            except Exception:
                pass
            if self._worker_ctx is not None:
                self._save_context()

    # ── persistence ────────────────────────────────────────────────

    def _context_path(self) -> Path:
        return self._worker_dir / "context.json"


    def _load_context(self) -> Optional[WorkerContext]:
        """Load WorkerContext from disk, if present. Returns None if not found."""
        path = self._context_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ctx = WorkerContext.from_persistable_dict(data)
                self.status = data.get("status", "ready")
                self.error = data.get("error")
                self.last_heartbeat = data.get("last_heartbeat")
                return ctx
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to load worker context %s: %s", path, exc
                )
        return None

    def _write_status_file(self) -> None:
        """
        Write runtime status to ``status.json`` so the web API backend
        (which runs in a separate process) can read it.

        This file is read by ``GET /api/workspace/{ws_id}/workers``
        to populate ``runtime_status``, ``current_task``,
        ``last_heartbeat`` and ``error`` for each worker.
        """
        data = {
            "runtime_status": self.status,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "error": self.error,
            "session_id": self.session_id,
            "current_context_tokens": self.get_current_context_tokens(),
            "max_context_tokens": self.max_context_tokens,
        }
        target = self._worker_dir / "status.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".status_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            if FileLock is not None:
                with FileLock(str(target)):
                    os.replace(tmp_path_str, str(target))
            else:
                os.replace(tmp_path_str, str(target))
        except (OSError, Exception) as exc:
            logger.error("Failed to write worker status file: %s", exc)
            try:
                if os.path.exists(tmp_path_str):
                    os.unlink(tmp_path_str)
            except OSError:
                pass

    def _save_context(self) -> None:
        """
        Persist WorkerContext + runtime status to disk atomically.

        Uses WorkerContext.to_persistable_dict() for the conversation
        data, augmented with runtime fields (status, error, heartbeat).
        Also writes a lightweight ``status.json`` consumed by the
        web API backend.
        """
        if self._worker_ctx is None:
            self._write_status_file()
            return
        ctx_data = self._worker_ctx.to_persistable_dict()
        data = {
            **ctx_data,
            "status": self.status,
            "error": self.error,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
        }
        target = self._context_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory (atomic on same filesystem)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".context_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            if FileLock is not None:
                with FileLock(str(target)):
                    os.replace(tmp_path_str, str(target))
            else:
                os.replace(tmp_path_str, str(target))
        except (OSError, Exception) as exc:
            logger.error("Failed to save worker context: %s", exc)
            # Clean up the temp file if the write failed
            try:
                if os.path.exists(tmp_path_str):
                    os.unlink(tmp_path_str)
            except OSError:
                pass
        self._write_status_file()

    def _publish_event(self, event_type: str, data: dict) -> None:
        """
        Publish a typed event to the worker's own EventBus for real-time forwarding
        to WebSocket clients via the bridge subscriber.

        Auto-injects ``worker_name`` from ``self.worker_name`` so that call sites
        don't need to include it — many events (WorkerSpawnedEvent,
        WorkerMessageEvent, AssistantMessageEvent, etc.) validate that
        ``worker_name`` is present in the data dict.
        """
        if "worker_name" not in data:
            data = {**data, "worker_name": self.worker_name}
        if self._event_bus is None or create_event is None or EventType is None:
            return

        try:
            if isinstance(event_type, str):
                try:
                    resolved_type = EventType(event_type)
                except ValueError:
                    log('WARNING', 'tools.worker', f"Unknown event type: {event_type}")
                    return

            event = create_event(
                event_type=resolved_type,
                data=data,
                source="worker",
                session_id=self.session_id or self.worker_name,
            )
            self._event_bus.publish(event)
        except Exception as e:
            log('ERROR', 'tools.worker', f"Failed to publish event {event_type}: {e}")

# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class Worker(ToolBase):
    """Manage background or child worker processes.

    When spawning a worker, set ``force=True`` to automatically stop any
    existing instance of the same worker (across all sessions) before
    creating a fresh one.  This is useful for recovering from stale or
    hanging workers left behind by previous sessions.
    """

    tool: str = "Worker"
    required_categories: ClassVar[List[str]] = ["execution:read"]

    action: str = Field(
        description="Action: list, spawn, check, query, stop. "
        "When action='spawn' and context has a 'query' key, the spawn call "
        "BLOCKS until the worker finishes the task and returns the full result. "
        "Without a 'query' key, spawn returns immediately; use action='query' later."
    )

    worker_name: Optional[str] = Field(
        default=None,
        description="Name of the worker",
    )

    worker_query: Optional[str] = Field(
        default=None,
        description="Query string to send to an already-spawned worker. "
        "Only valid with action='query'. The worker processes this and the call "
        "BLOCKS until the worker responds.",
    )

    context: Optional[Union[Dict, str]] = Field(
        default=None,
        description="Optional context dict passed only on action='spawn'. "
        "If this dict includes a 'query' key (e.g., {'query': 'Review src/'}), "
        "the worker executes that task immediately and the spawn call BLOCKS "
        "until the worker finishes. The 'query' value becomes the worker's "
        "first instruction. Other keys (e.g., config) are passed as metadata. "
        "You may also pass a plain string, which will be treated as the query.",
    )

    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Override the worker's default timeout in seconds. "
                      "If not set, the worker definition's timeout is used.",
    )

    session_id: Optional[str] = Field(
        default=None,
        description="Session ID that spawned this worker (injected by ToolExecutor)",
    )

    force: bool = Field(
        default=False,
        description="If True, stop any existing worker instance (across all sessions) before spawning a fresh one.",
    )

    skip_output_truncation: ClassVar[bool] = True

    VALID_ACTIONS: ClassVar[list[str]] = ["list", "spawn", "check", "query", "stop"]

    # ── Pydantic v2 validator: coerce plain string context to {"query": ...} ──
    @field_validator('context', mode='before')
    @classmethod
    def coerce_context(cls, v: Any) -> Any:
        """If the LLM passes a plain string as context, wrap it as a query dict."""
        if isinstance(v, str):
            return {"query": v}
        return v

    # ------------------------------------------------------------------
    def execute(self) -> str:
        try:
            if self.action not in self.VALID_ACTIONS:
                return json.dumps({
                    "error": f"Unknown action: {self.action}",
                    "available_actions": self.VALID_ACTIONS,
                })

            if self.action in ("spawn", "check", "query") and not self.worker_name:
                return json.dumps({
                    "error": f"worker_name is required for action '{self.action}'",
                })

            # Resolve workspace ID
            ws_id = None
            if resolve_workspace_id and self.workspace_path:
                ws_id = resolve_workspace_id(self.workspace_path)

            workers = self._load_workers(ws_id)

            handler = {
                "list": lambda: self._action_list(workers),
                "spawn": lambda: self._action_spawn(workers, ws_id),
                "check": lambda: self._action_check(workers),
                "query": lambda: self._action_query(workers),
                "stop": lambda: self._action_stop(workers),
            }[self.action]

            result = handler()
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.exception("Worker failed")
            return json.dumps(
                {"error": str(exc), "action": self.action}, indent=2
            )

    # -- helpers -----------------------------------------------------

    def _load_workers(self, ws_id: Optional[str]) -> list:
        """
        Load workers list from workers.json in workspace dir.

        Falls back to ``_load_template_workers()`` when the file is missing,
        empty, or fails to parse.
        """
        if not CAPABILITIES_AVAILABLE or not _workspace_dir or not ws_id:
            return []

        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            logger.info(f"workers.json not found at {workers_path}, falling back to templates")
            return _load_template_workers()

        try:
            data = json.loads(workers_path.read_text(encoding="utf-8"))
            if not data:  # empty array
                logger.info(f"workers.json is empty, falling back to templates")
                return _load_template_workers()
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load workers.json: {e}, falling back to templates")
            return _load_template_workers()

    def _find_worker(self, workers: list, name: str) -> Optional[dict]:
        """Find a worker by name in the workers list."""
        for w in workers:
            if isinstance(w, dict) and w.get("name") == name:
                return w
        return None

    def _resolve_ws_dir(self, ws_id: str) -> Optional[Path]:
        """Resolve workspace directory from workspace ID."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir:
            return None
        return _workspace_dir(ws_id)

    def _check_worker_permissions(
        self,
        definition: dict,
        session_permissions: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """
        Check whether the current session has sufficient permissions to
        spawn this worker.  Returns an error message string if denied,
        or ``None`` if allowed.
        """
        if not GATE_AVAILABLE or not check_required_categories:
            return None  # no gate = allow

        required = definition.get("required_categories", [])
        if not required:
            return None

        worker_perms = definition.get("permission_footprint") or definition.get("worker_permissions", {})

        ok, error_msg = check_required_categories(
            required=required,
            effective=session_permissions or {},
            tool_name="Worker",
            tool_args={"action": self.action, "worker_name": self.worker_name},
            description=f"Spawn worker '{self.worker_name}'",
            permission_footprint=worker_perms,
            is_worker_context=True,
        )

        if not ok:
            return error_msg
        return None

    def _build_agent_config(self) -> dict:
        """
        Build an agent config dict from the tool's injected agent_config.

        Returns a dict with provider, model, api_key, base_url, temperature
        and any other fields present in ``self.agent_config`` (injected by
        ToolExecutor).  This dict is passed to WorkerThread so it can create
        an ``AgentConfig`` at runtime.

        Also injects ``workspace_path`` from the parent tool's workspace_path
        so that the worker's AgentConfig has it set, which in turn lets the
        worker's ToolExecutor propagate it to tool calls.
        """
        cfg = self.agent_config or {}
        result = dict(cfg)
        if self.workspace_path:
            result["workspace_path"] = self.workspace_path
        return result

    def _resolve_tool_class(self, tool_name: str) -> Optional[type[ToolBase]]:
        """Resolve a tool name string to its class via _TOOL_REGISTRY.

        Uses the module-level ``_TOOL_REGISTRY`` dict built from
        ``tools.TOOL_CLASSES`` at import time, so no lazy imports are needed.
        """
        cls = _TOOL_REGISTRY.get(tool_name)
        if cls is None:
            logger.warning("No known tool class for '%s'", tool_name)
            return None
        return cls

    def _find_all_worker_threads(self, worker_name: str) -> list[tuple[str, Any]]:
        """
        Search the entire ``_worker_registry`` for all entries matching
        *worker_name*, regardless of session_id.

        Returns a list of ``(session_key, thread)`` tuples.
        """
        results: list[tuple[str, Any]] = []
        with _registry_lock:
            for (sid, wname), thread in list(_worker_registry.items()):
                if wname == worker_name:
                    results.append((sid, thread))
        return results

    # -- action implementations --------------------------------------

    def _action_list(self, workers: list) -> dict:
        """Return all known worker definitions plus runtime status."""
        session_key = self.session_id or ""
        augmented = []
        for w in workers:
            name = w.get("name", "")
            entry = dict(w)
            # Merge runtime status from registry (session-scoped key)
            with _registry_lock:
                thread = _worker_registry.get((session_key, name))
            if thread is not None:
                entry["runtime_status"] = thread.status
                entry["current_task"] = thread.current_task
                entry["last_heartbeat"] = thread.last_heartbeat
                entry["error"] = thread.error
            else:
                entry["runtime_status"] = "stopped"
            augmented.append(entry)

        return {"workers": augmented, "count": len(augmented)}

    def _action_spawn(self, workers: list, ws_id: Optional[str]) -> dict:
        """Spawn a worker thread from its definition in workers.json."""
        definition = self._find_worker(workers, self.worker_name)
        if definition is None:
            return {
                "error": f"Worker '{self.worker_name}' not found in workers.json",
            }

        # ── Force-respawn: stop any existing instances across all sessions ──
        if self.force:
            stale_instances = self._find_all_worker_threads(self.worker_name)
            stopped_info = []
            for sid, thread in stale_instances:
                worker_label = f"{self.worker_name} (session={sid})"
                logger.info("Force-spawn: stopping stale worker '%s'", worker_label)
                try:
                    thread.stop()
                    # Robust join with retry (same pattern as _action_stop)
                    _budget = max(30, thread._timeout_seconds)
                    _elapsed = 0.0
                    while _elapsed < _budget:
                        thread.join(timeout=2.0)
                        _elapsed += 2.0
                        if not thread.is_alive():
                            break
                    if thread.is_alive():
                        logger.warning(
                            "Force-spawn: stale worker '%s' did not stop within %ds",
                            worker_label, _budget,
                        )
                except Exception as exc:
                    logger.exception("Error stopping stale worker '%s': %s", worker_label, exc)
                finally:
                    thread._save_context()
                    with _registry_lock:
                        _worker_registry.pop((sid, self.worker_name), None)
                stopped_info.append({"session_id": sid, "status": thread.status})
            if stopped_info:
                logger.info(
                    "Force-spawn: stopped %d stale instance(s) of worker '%s'",
                    len(stopped_info), self.worker_name,
                )

        # Prevent duplicate spawns (session-scoped key)
        session_key = self.session_id or ""
        with _registry_lock:
            existing = _worker_registry.get((session_key, self.worker_name))
            if existing is not None and existing.is_alive():
                return {
                    "error": f"Worker '{self.worker_name}' is already running",
                    "status": existing.status,
                }

        # Permission gate check
        gate_error = self._check_worker_permissions(
            definition, self.session_permissions
        )
        if gate_error is not None:
            return {
                "error": f"Permission denied: {gate_error}",
                "worker_name": self.worker_name,
            }

        # Build agent config for this worker
        agent_config = self._build_agent_config()
        if agent_config is None:
            return {
                "error": "Cannot create worker: AgentConfig unavailable. "
                          "Check that agent_config has provider and model.",
                "worker_name": self.worker_name,
            }

        # Resolve workspace directory for persistence
        ws_dir: Optional[Path] = None
        if ws_id:
            ws_dir = self._resolve_ws_dir(ws_id)
        if ws_dir is None:
            return {
                "error": "Cannot create worker: no workspace directory resolved.",
                "worker_name": self.worker_name,
            }

        # Resolve tool classes from definition
        tool_classes: Dict[str, type[ToolBase]] = {}
        missing_tools: list[str] = []
        tool_names = definition.get("tools", [])
        worker_perms = definition.get("permission_footprint") or definition.get("worker_permissions", {})
        if tool_names:
            for tool_name in tool_names:
                if tool_name in _WORKER_BLOCKLIST:
                    missing_tools.append(f"{tool_name} (excluded for worker safety)")
                    continue
                cls = self._resolve_tool_class(tool_name)
                if cls is None:
                    missing_tools.append(tool_name)
                    continue
                # Spawn-time footprint validation: check tool categories against
                # the worker's declared permission footprint.
                tool_cats = cls.get_required_categories({})
                if tool_cats and GATE_AVAILABLE and check_required_categories is not None:
                    ok, _err = check_required_categories(
                        required=tool_cats,
                        effective=worker_perms,
                        tool_name=tool_name,
                        tool_args={},
                        description=(
                            f"Worker '{self.worker_name}' footprint validation"
                            f" for {tool_name}"
                        ),
                        permission_footprint=worker_perms,
                        is_worker_context=True,
                    )
                    if not ok:
                        missing_tools.append(
                            f"{tool_name} (permission denied by worker footprint)"
                        )
                        continue
                tool_classes[tool_name] = cls

        # Resolve project root from workspace config so tools get the right
        # workspace_path (the project root, not the worker's internal dir).
        project_root: Optional[str] = None
        if ws_dir:
            config_path = ws_dir / "config.json"
            try:
                config_data = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(config_data, dict):
                    project_root = config_data.get("root")
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                project_root = None

        # Compute effective timeout: spawn override > definition > 600
        effective_timeout = (
            self.timeout_seconds
            if self.timeout_seconds is not None
            else definition.get("timeout_seconds", 600)
        )

        # Create and start the worker thread
        thread = WorkerThread(
            name=self.worker_name,
            definition=definition,
            agent_config=agent_config,
            workspace_dir=ws_dir,
            tool_classes=tool_classes if tool_classes else None,
            session_permissions=self.session_permissions,
            project_root=project_root,
            timeout_seconds=effective_timeout,
            session_id=self.session_id,
        )

        # Store initial context for the thread to pick up in run()
        if self.context is not None and isinstance(self.context, dict):
            thread._initial_context = self.context

        # Context is always preserved — context.json is never deleted on spawn.
        # If context.json exists from a previous run, it is loaded in run()
        # and _initial_context is merged into the loaded context rather than
        # replacing it. This ensures worker conversation state persists across
        # spawn/query boundaries.

        # ── Fix 1.1A: Clean up stale command.json before starting ──
        cmd_path = thread._worker_dir / "command.json"
        if cmd_path.exists():
            cmd_path.unlink(missing_ok=True)

        with _registry_lock:
            _worker_registry[(session_key, self.worker_name)] = thread

        thread.start()

        if missing_tools:
            logger.warning(
                "Worker '%s' spawned but tool(s) not found: %s",
                self.worker_name, missing_tools,
            )

        # ── Wait for first response if auto-query was queued ──
        # If initial context has a "query" key, the worker auto-processes it
        # and puts the result in _output_queue.  We wait for that first
        # response, then return it while the thread stays alive for future
        # queries.  If no auto-query was queued, return immediately.
        has_auto_query = (
            thread._initial_context is not None
            and isinstance(thread._initial_context, dict)
            and "query" in thread._initial_context
        )
        if has_auto_query:
            try:
                final_result = thread._output_queue.get(timeout=effective_timeout)
            except queue.Empty:
                final_result = json.dumps({
                    "error": f"Worker '{self.worker_name}' did not respond "
                             f"within {effective_timeout}s",
                    "note": "Worker timed out. The result above is partial work "
                             "completed before timeout. The worker thread is still "
                             "alive — you can query it again via action='query' to "
                             "request continuation.",
                })
            try:
                parsed = json.loads(final_result) if final_result else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {"response": final_result}
            if not isinstance(parsed, dict):
                parsed = {"response": str(parsed)}
            parsed.setdefault("worker_name", self.worker_name)
            parsed.setdefault("spawned", True)
            elapsed = thread._last_elapsed()
            if elapsed is not None:
                parsed["elapsed_seconds"] = round(elapsed, 1)
            return parsed
        else:
            # No auto-query — return immediately; worker stays alive
            return {
                "worker_name": self.worker_name,
                "spawned": True,
                "status": "ready",
            }

    def _action_check(self, workers: list) -> dict:
        """Check on a specific worker by name.

        First tries the current session; if not found, searches across all
        sessions and reports any foreign-session instances found.
        """
        session_key = self.session_id or ""
        with _registry_lock:
            thread = _worker_registry.get((session_key, self.worker_name))

        if thread is None:
            # Not found in current session → search across all sessions
            all_instances = self._find_all_worker_threads(self.worker_name)
            if all_instances:
                # Found in another session — report with note
                if len(all_instances) == 1:
                    sid, t = all_instances[0]
                    return {
                        "worker_name": self.worker_name,
                        "status": t.status,
                        "current_task": t.current_task,
                        "last_heartbeat": t.last_heartbeat,
                        "error": t.error,
                        "session_id": sid,
                        "note": "Worker is from a different session",
                        "alive": t.is_alive(),
                    }
                # Multiple instances across sessions
                instances = []
                for sid, t in all_instances:
                    instances.append({
                        "session_id": sid,
                        "status": t.status,
                        "current_task": t.current_task,
                        "last_heartbeat": t.last_heartbeat,
                        "error": t.error,
                        "alive": t.is_alive(),
                    })
                return {
                    "worker_name": self.worker_name,
                    "note": "Worker instances found across multiple sessions",
                    "instances": instances,
                    "count": len(instances),
                }

            # No instances found anywhere — report as stopped
            entry = self._find_worker(workers, self.worker_name)
            if entry:
                return {
                    "worker_name": self.worker_name,
                    "status": "stopped",
                    "current_task": None,
                    "last_heartbeat": None,
                    "error": None,
                }
            return {
                "error": f"Worker '{self.worker_name}' not found",
            }

        return {
            "worker_name": self.worker_name,
            "status": thread.status,
            "current_task": thread.current_task,
            "last_heartbeat": thread.last_heartbeat,
            "error": thread.error,
            "session_id": thread.session_id,
            "current_context_tokens": thread.get_current_context_tokens(),
            "max_context_tokens": thread.max_context_tokens,
            "alive": thread.is_alive(),
            "conversation_length": len(thread._worker_ctx.user_history) if thread._worker_ctx else 0,
        }

    def _action_query(self, workers: list) -> dict:
        """Query a worker and wait for a response (synchronous, blocking)."""
        session_key = self.session_id or ""
        with _registry_lock:
            thread = _worker_registry.get((session_key, self.worker_name))

        if thread is None:
            return {
                "error": f"Worker '{self.worker_name}' is not running. "
                          f"Use 'spawn' first.",
            }

        if not thread.is_alive():
            return {
                "error": f"Worker '{self.worker_name}' is no longer alive "
                          f"(status: {thread.status}).",
                "status": thread.status,
                "error_detail": thread.error,
            }

        if not self.worker_query:
            return {
                "error": "worker_query is required for action 'query'",
            }

        try:
            # Block until the worker responds
            response = thread.send_query(self.worker_query, timeout=300.0)
            elapsed = thread._last_elapsed()
            result = {
                "worker_name": self.worker_name,
                "response": response,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            }
            if thread._last_reasoning is not None:
                result["reasoning"] = thread._last_reasoning
            return result
        except TimeoutError as exc:
            # Check heartbeat to distinguish "worker hung" vs "worker still busy"
            if thread.last_heartbeat:
                try:
                    hb_dt = datetime.fromisoformat(thread.last_heartbeat)
                    age_seconds = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                    if age_seconds > 600.0:  # 2× the 300s query timeout
                        return {
                            "error": f"Worker appears hung (last heartbeat: {thread.last_heartbeat}, {age_seconds:.0f}s ago)",
                            "worker_name": self.worker_name,
                            "status": thread.status,
                            "current_task": thread.current_task,
                        }
                except (ValueError, TypeError):
                    pass  # Malformed heartbeat — fall through to generic timeout
            return {
                "error": str(exc),
                "note": "Worker did not respond in time. The worker thread is "
                         "still alive — you can query it again with a shorter task.",
                "worker_name": self.worker_name,
            }

    def _action_stop(self, workers: list) -> dict:
        """Stop a running worker and persist its context.

        First tries the current session; if not found, searches across all
        sessions and stops any matching instances.
        """
        session_key = self.session_id or ""
        with _registry_lock:
            thread = _worker_registry.get((session_key, self.worker_name))

        # ── Not found in current session → search across all sessions ──
        if thread is None:
            all_instances = self._find_all_worker_threads(self.worker_name)
            if not all_instances:
                # No instances anywhere — report as not running
                entry = self._find_worker(workers, self.worker_name)
                if entry:
                    return {
                        "worker_name": self.worker_name,
                        "status": "stopped",
                        "message": f"Worker '{self.worker_name}' was not running.",
                    }
                return {
                    "error": f"Worker '{self.worker_name}' not found",
                }

            # Found in another session — stop all instances
            stopped_info = []
            for sid, t in all_instances:
                worker_label = f"{self.worker_name} (session={sid})"
                logger.info("Cross-session stop: stopping worker '%s'", worker_label)
                try:
                    t.stop()
                    # Robust join with retry (same pattern as _action_stop)
                    _budget = max(30, t._timeout_seconds)
                    _elapsed = 0.0
                    while _elapsed < _budget:
                        t.join(timeout=2.0)
                        _elapsed += 2.0
                        if not t.is_alive():
                            break
                    if t.is_alive():
                        logger.warning(
                            "Cross-session stop: worker '%s' did not stop within %ds",
                            worker_label, _budget,
                        )
                except Exception as exc:
                    logger.exception("Error stopping worker '%s'", worker_label)
                    stopped_info.append({"session_id": sid, "error": str(exc)})
                finally:
                    t._save_context()
                    with _registry_lock:
                        _worker_registry.pop((sid, self.worker_name), None)
                    if t.status not in [s.get("status") for s in stopped_info]:
                        stopped_info.append({"session_id": sid, "status": t.status})

            return {
                "worker_name": self.worker_name,
                "status": "stopped",
                "message": (
                    f"Stopped {len(stopped_info)} instance(s) of worker "
                    f"'{self.worker_name}' across sessions."
                ),
                "stopped_instances": stopped_info,
            }

        # ── Found in current session ──
        if not thread.is_alive():
            with _registry_lock:
                _worker_registry.pop((session_key, self.worker_name), None)
            thread._save_context()
            return {
                "worker_name": self.worker_name,
                "status": thread.status,
                "message": f"Worker '{self.worker_name}' was already stopped.",
            }

        try:
            thread.stop()
            # ── Robust join: retry loop to handle slow LLM API calls ──
            # The worker's _run_tool_loop can be blocked inside
            # agent.process_query() waiting for an LLM response that takes
            # 15-30s.  A single join(timeout=10) would expire before the
            # _stop_event is checked.  We retry with a longer total budget.
            _join_budget = max(30, thread._timeout_seconds)
            _join_elapsed = 0.0
            _join_step = 2.0
            while _join_elapsed < _join_budget:
                thread.join(timeout=_join_step)
                _join_elapsed += _join_step
                if not thread.is_alive():
                    break
                logger.debug(
                    "Still waiting for worker '%s' to stop "
                    "(%.0f/%ds elapsed)",
                    self.worker_name, _join_elapsed, _join_budget,
                )
            if thread.is_alive():
                logger.warning(
                    "Worker '%s' did not stop within %ds budget. "
                    "Popping from registry anyway (daemon thread).",
                    self.worker_name, _join_budget,
                )
        except Exception as exc:
            logger.exception("Error joining worker '%s' during stop", self.worker_name)
            return {
                "error": f"Error stopping worker '{self.worker_name}': {exc}",
            }
        finally:
            thread._save_context()
            with _registry_lock:
                _worker_registry.pop((session_key, self.worker_name), None)

        return {
            "worker_name": self.worker_name,
            "status": "stopped",
            "message": f"Worker '{self.worker_name}' stopped successfully.",
        }
