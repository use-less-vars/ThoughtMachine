# tools/workspace/worker.py
"""
Worker — manage background / child worker processes.

Workers run as threads on the host, reusing the agent's LLM provider
configuration.  Each worker has its own persisted conversation context
stored in ``<workspace_dir>/workers/<name>/context.json``.

Container cleanup
-----------------
When a worker exits (completed, stopped, errored, or timed out) it stops
and removes any containers it created while running. Ownership is
established by the EXACT VALUE of the ``thoughtmachine.worker`` label: a
container belongs to a worker only when the label value EQUALS the
worker's owner identity ``<session_id or 'unknown'>:<worker_name>`` (the
identity the worker container bridge stamped when the tool call created
it). Containers with a stale or mismatched value are ignored - they may
belong to a sibling worker or a previous session. Resource containers
(``thoughtmachine.resource`` label, ``tm-res-*`` names, or the
``tm-resource-git`` image) are shared workspace infrastructure managed by
the workspace lifecycle manager and are never touched during worker
teardown.

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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union
from pydantic import Field, field_validator

from tools.base import ToolBase
from tools.utils import model_to_openai_tool
try:
    from agent.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# File lock for atomic writes (same pattern as FileSystemSessionStore)
try:
    from session.lock import FileLock
except ImportError:
    FileLock = None  # type: ignore

# ── Spawn queue timeout (generous fixed value, NOT tied to agent timeout) ──
# The agent's internal timeout mechanism (timeout_seconds) enforces time limits
# via tool restrictions. The spawn Queue.get timeout must be a generous fixed
# value so it never preempts the agent's own timeout logic.
SPAWN_QUEUE_TIMEOUT = 600

# ── Session-level worker spawn cap (safe default) ──
# Limits how many LIVE worker threads a single session may have running at
# once. Configurable per session via the ``max_workers`` config key (top-level
# agent_config, or nested under ``session_config``). Force-replacements
# (force=True) do NOT count toward the cap — the stale instance is stopped
# and removed from the registry before the cap check runs.
MAX_WORKERS_PER_SESSION = 5

# Registry key fallback used when a worker is spawned without a session_id.
_NO_SESSION_KEY = "<no-session>"

# Session-level main-agent pause set (UNIT C, pause/resume v2). A session id
# present here means the session's MAIN agent is paused ("Pause Main"); its
# async workers keep running unless they were paused too ("Pause All"). While
# the main agent is paused, join/wait_for_job return {"status": "paused"}
# promptly instead of blocking. Pausing is cooperative only: workers pause at
# their next safe loop boundary via WorkerThread.pause(); no thread is killed.
_SESSION_MAIN_PAUSED: set[str] = set()

# ── Worker heartbeat / liveness (Phase 2A) ──
# Workers publish WORKER_HEARTBEAT events to the global bus while idle,
# paused, and busy. HEARTBEAT_INTERVAL_S throttles publication; a worker is
# considered "stale" (possibly hung) when its last heartbeat is older than
# HEARTBEAT_STALE_AFTER_S.
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_STALE_AFTER_S = 600

# ── Per-worker resource budgets (Phase 3, item 6) ───────────────────────────
# Containers: a worker may keep at most this many containers active at once.
# The default (4) aligns with infra.container_registry.DEFAULT_MAX_CONTAINERS
# and the container_manager default.  Token and runtime budgets default to
# None (unlimited) so existing behaviour is unchanged unless a session config
# sets ``max_token_usage`` / ``max_runtime_s``.
WORKER_DEFAULT_MAX_CONTAINERS = 4
WORKER_DEFAULT_MAX_TOKENS: Optional[int] = None
WORKER_DEFAULT_MAX_RUNTIME_S: Optional[float] = None

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
    from agent.core.worker_context import (
        WorkerContext,
        WORKER_NAME_CONTEXTVAR,
    )
except ImportError:
    WorkerContext = None  # type: ignore
    WORKER_NAME_CONTEXTVAR = None  # type: ignore

# Optional: ContainerManager — used at spawn time to build a per-worker
# manager for worker-scoped container teardown. Imported lazily so worker.py
# stays importable when the docker stack is unavailable (cleanup then no-ops).
try:
    from infra.container_manager import ContainerManager as _CM
except Exception:
    _CM = None  # type: ignore

# NOTE: Agent and AgentConfig are imported *lazily* inside
# WorkerThread._build_agent_config() to avoid a circular import with
# agent/core/agent.py which imports from tools (TOOL_CLASSES).

# Presenter components for per-worker event processing
StateBridge = None  # lazy-imported in _run_tool_loop

EventProcessor = None  # lazy-imported in _run_tool_loop

try:
    from agent.core.state import ExecutionState, TurnState, TimeState
except ImportError:
    ExecutionState = None
    TurnState = None
    TimeState = None

# Optional: security gate for worker permission checks
try:
    from security.security_gate import check_required_categories
    GATE_AVAILABLE = True
except ImportError:
    GATE_AVAILABLE = False
    check_required_categories = None  # type: ignore

# Fail-closed default levels for permission categories a session profile does
# not explicitly expose.  Used by _restrictive_merge() so a worker can never
# fill in a missing category with its own (potentially permissive) footprint
# value.  Imported LAZILY (not at module import time): a module-level
# ``from thoughtmachine.security import SAFE_DEFAULTS`` triggered
# thoughtmachine/security.py's try/except ImportError during pytest
# collection — while agent.events was mid-load — permanently pinning its
# EventType to None and breaking security-prompt tests later in the session.
_SAFE_DEFAULTS_CACHE: Optional[Dict[str, Any]] = None


def _load_safe_defaults() -> Dict[str, Any]:
    """Return the fail-closed SAFE_DEFAULTS map, importing it lazily.

    Cached after the first successful import so the cost is paid once.
    Falls back to an empty dict (with a warning) if thoughtmachine.security
    is unavailable; callers then resolve missing categories to ``None``.
    """
    global _SAFE_DEFAULTS_CACHE
    if _SAFE_DEFAULTS_CACHE is None:
        try:
            from thoughtmachine.security import SAFE_DEFAULTS

            _SAFE_DEFAULTS_CACHE = SAFE_DEFAULTS
        except ImportError:
            log(
                "WARN",
                "worker.permissions",
                "thoughtmachine.security unavailable; "
                "worker safe-defaults fall back to an empty dict",
            )
            _SAFE_DEFAULTS_CACHE = {}
    return _SAFE_DEFAULTS_CACHE

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

# Optional: WorkerLifecycleObserver (Phase 2A) — imported lazily so worker.py
# stays importable without tools.workspace.worker_lifecycle (which only
# depends on agent.events, so there is no import cycle at runtime).
try:
    from tools.workspace.worker_lifecycle import WorkerLifecycleObserver
except ImportError:
    WorkerLifecycleObserver = None  # type: ignore


def _on_worker_stale(worker_name: str, info: dict) -> None:
    """Best-effort callback invoked when the observer flags a hung worker.

    Looks up the worker thread in the module-level registry and terminates its
    in-flight executions so the supervisor can reclaim the worker. Deliberately
    never raises: this is invoked from the lifecycle observer's event path and
    must not break it. Registry globals (``_registry_lock`` / ``_worker_registry``)
    are assigned later in this module, so they are resolved at call time.
    """
    try:
        session_id = info.get("session_id") or ""
        instance_id = info.get("instance_id")
        thread = None
        try:
            thread, _sid, _iid = _resolve_worker_thread(
                worker_name,
                instance_id=instance_id,
                session_id=session_id or None,
            )
        except _WorkerAmbiguityError:
            thread = None
        if thread is None:
            with _registry_lock:
                thread = _worker_registry.get((session_id, worker_name, instance_id or 1))
                if thread is None:
                    thread = _worker_registry.get((session_id, worker_name))
                if thread is None:
                    thread = _worker_registry.get((_NO_SESSION_KEY, worker_name))
        if thread is None:
            # Name-only fallback: first matching entry across sessions.
            with _registry_lock:
                for key, cand in _worker_registry.items():
                    if key[1] == worker_name:
                        thread = cand
                        break
        if thread is None:
            return
        wlm = getattr(thread, "_wlm", None)
        if wlm is None:
            return
        wlm.terminate_executions()
    except Exception:
        pass


_WORKER_LIFECYCLE_OBSERVER = None


def _get_worker_lifecycle_observer():
    """Return the module-level WorkerLifecycleObserver singleton (best-effort).

    Creates and subscribes it on first use. Returns None when the observer
    module or the event bus is unavailable, so callers can safely ignore the
    result.
    """
    global _WORKER_LIFECYCLE_OBSERVER
    if WorkerLifecycleObserver is None:
        return None
    if _WORKER_LIFECYCLE_OBSERVER is None:
        _WORKER_LIFECYCLE_OBSERVER = WorkerLifecycleObserver(
            stale_callback=_on_worker_stale
        )
        try:
            _WORKER_LIFECYCLE_OBSERVER.ensure_subscribed()
        except Exception:
            pass
    return _WORKER_LIFECYCLE_OBSERVER


# Optional: WorkerJobRegistry (Phase 2B) — imported lazily so worker.py stays
# importable without tools.workspace.job_registry (stdlib + agent.events only).
try:
    from tools.workspace.job_registry import WorkerJobRegistry
except ImportError:
    WorkerJobRegistry = None  # type: ignore

_WORKER_JOB_REGISTRY = None


def _get_worker_job_registry():
    """Return the module-level WorkerJobRegistry singleton (best-effort).

    Creates and subscribes it on first use. Returns None when the registry
    module or the event bus is unavailable, so callers can safely ignore the
    result.
    """
    global _WORKER_JOB_REGISTRY
    if WorkerJobRegistry is None:
        return None
    if _WORKER_JOB_REGISTRY is None:
        _WORKER_JOB_REGISTRY = WorkerJobRegistry()
        try:
            _WORKER_JOB_REGISTRY.ensure_subscribed()
        except Exception:
            pass
    return _WORKER_JOB_REGISTRY


def _publish_global_worker_event(event_type, data: dict) -> None:
    """Publish a worker lifecycle event to the global event bus (best-effort).

    Never raises: worker lifecycle events are observability aids, not control
    plane. ``data`` must include ``worker_name`` and may include ``session_id``.
    """
    if global_event_bus is None or EventType is None or create_event is None:
        return
    try:
        evt = create_event(
            event_type,
            data=data,
            source=f"worker:{data.get('worker_name', 'unknown')}",
            session_id=data.get("session_id") or "",
        )
        global_event_bus.publish(evt)
    except Exception:
        pass


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
            # FIX C: skip publishing empty placeholder messages. They produce invisible
            # bubbles in the worker panel AND consume the frontend dedup key (canonical
            # type|timestamp), causing the real full-content message (same logical event,
            # same timestamp) to be deduplicated away. Verified: no streaming /
            # update-in-place flow depends on empty-content worker_message publishes.
            if not content and not reasoning:
                log('DEBUG', 'pipeline.worker_bus',
                    f"forward_agent_event: SKIPPED agent_responded (empty content/reasoning) "
                    f"[worker={self.worker_name}]")
                return
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
            # FIX C: skip empty placeholder messages (see agent_responded guard above).
            if not content and not reasoning:
                log('DEBUG', 'pipeline.worker_bus',
                    f"forward_agent_event: SKIPPED turn (empty content/reasoning) "
                    f"[worker={self.worker_name}]")
                return
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

# Hard blocklist: tools that workers must NEVER use, regardless of permissions.
# NOTE: container tools (DockerCodeRunner, ContainerStartTool, ContainerExecTool,
# etc.) ARE allowed for workers — they are permission-gated like any other tool,
# NOT hard-blocked: DockerCodeRunner requires filesystem:write + container:true
# (docker_code_runner.py:56) and the ToolExecutor enforces those categories via
# check_required_categories, so a worker can only reach them when its effective
# (session × footprint) profile allows it. Only the tools below are hard-blocked
# (recursion, MCP server management, system/infrastructure discovery, persistent
# knowledge store). This is defense-in-depth: the blocklist is enforced at two
# separate points (enabled_tools filtering and tool class resolution).
_WORKER_BLOCKLIST: frozenset[str] = frozenset({
    "Worker",           # recursion: worker spawning workers
    "MCPValidator",      # MCP server management
    "CheckSystem",       # system/network/worker/infrastructure discovery
    "KnowledgeBaseTool", # persistent cross-session knowledge store
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

# Worker LLM sampling defaults (used when the worker definition does not
# specify a value).  These are worker-scoped: the main agent's sampling
# settings are never inherited.
WORKER_DEFAULT_TEMPERATURE = 0.7
WORKER_DEFAULT_MAX_TOKENS: Optional[int] = None
WORKER_DEFAULT_TRUNCATION: Optional[int] = None

# Global tool name → class registry (built lazily to avoid circular import
# with tools.__init__ — that module imports this file (Worker) before
# TOOL_CLASSES is fully populated, so we resolve on first use).
def _build_tool_registry() -> dict[str, type["ToolBase"]]:
    from tools import TOOL_CLASSES
    return {cls.__name__: cls for cls in TOOL_CLASSES}

_TOOL_REGISTRY: dict[str, type["ToolBase"]] = {}
def _get_tool_registry() -> dict[str, type["ToolBase"]]:
    if not _TOOL_REGISTRY:
        _TOOL_REGISTRY.update(_build_tool_registry())
    return _TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Module-level worker registry (delegates to WorkerRegistry singleton)
# ---------------------------------------------------------------------------

from tools.workspace.worker_registry import WorkerRegistry as _WorkerRegistry

_registry_instance = _WorkerRegistry.get_instance()
_worker_registry = _registry_instance._worker_registry
_registry_lock = _registry_instance._registry_lock
_worker_event_bus_registry = _registry_instance._worker_event_bus_registry
_bus_registry_lock = _registry_instance._bus_registry_lock

# ---------------------------------------------------------------------------
# Shutdown helper  (exposed for bridge integration)
# ---------------------------------------------------------------------------

def shutdown_workers(timeout: float = 5.0) -> None:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    _WorkerRegistry.get_instance().shutdown_workers(timeout=timeout)

def register_worker_event_bus(session_id: str, worker_name: str, event_bus: Any, instance_id: int = 1) -> None:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    _WorkerRegistry.get_instance().register_event_bus(session_id, worker_name, event_bus, instance_id=instance_id)

def unregister_worker_event_bus(session_id: str, worker_name: str, instance_id: int = 1) -> None:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    _WorkerRegistry.get_instance().unregister_event_bus(session_id, worker_name, instance_id=instance_id)

def get_worker_event_bus(session_id: str, worker_name: str, instance_id: int = 1) -> Any:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    return _WorkerRegistry.get_instance().get_event_bus(session_id, worker_name, instance_id=instance_id)


def get_worker_event_buses_for_session(session_id: str) -> Dict[str, Any]:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    return _WorkerRegistry.get_instance().get_event_buses_for_session(session_id)


class _WorkerAmbiguityError(Exception):
    """Raised when a worker name maps to multiple live instances and no
    ``instance_id`` was given to disambiguate."""


def _resolve_worker_thread(
    worker_name: str,
    instance_id: Optional[int] = None,
    session_id: Optional[str] = None,
):
    """Resolve a worker name (+ optional instance/session) to a live thread.

    Returns ``(thread, session_key, iid)``. With no explicit ``instance_id``,
    exactly one live candidate must exist; multiple live candidates raise
    ``_WorkerAmbiguityError`` (callers must pass ``instance_id``). No live
    candidate raises ``_WorkerAmbiguityError`` with a 'not running' message.
    """
    with _registry_lock:
        if instance_id is not None:
            # Exact 3-tuple lookup first, then a name+instance scan (session
            # filter applies only when given).
            if session_id is not None:
                thread = _worker_registry.get((session_id, worker_name, instance_id))
                if thread is not None:
                    return (thread, session_id, instance_id)
            for key, thread in list(_worker_registry.items()):
                wname = key[1]
                iid = key[2] if len(key) >= 3 else 1
                if wname == worker_name and iid == instance_id:
                    if session_id is None or key[0] == session_id:
                        return (thread, key[0], iid)
            raise _WorkerAmbiguityError(
                f"Worker '{worker_name}' instance {instance_id} is not running"
            )
        candidates = []
        for key, thread in list(_worker_registry.items()):
            wname = key[1]
            iid = key[2] if len(key) >= 3 else 1
            if wname != worker_name:
                continue
            if session_id is not None and key[0] != session_id:
                continue
            alive = bool(getattr(thread, "is_alive", lambda: True)())
            if not alive:
                continue
            candidates.append((key[0], iid, thread))
    if not candidates:
        raise _WorkerAmbiguityError(f"Worker '{worker_name}' is not running")
    if len(candidates) == 1:
        sid, iid, thread = candidates[0]
        return (thread, sid, iid)
    labels = sorted(
        _WorkerRegistry.instance_label(worker_name, iid) for _sid, iid, _t in candidates
    )
    raise _WorkerAmbiguityError(
        f"Worker '{worker_name}' is ambiguous: {len(candidates)} live "
        f"instances ({', '.join(labels)}). Specify instance_id."
    )

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
            # Fail-closed: the session does not expose this category, so the
            # worker may NOT fill it in with its own (potentially permissive)
            # footprint value.  Resolve to the safe-default level instead.
            result[key] = _load_safe_defaults().get(key)
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
# Worker-scoped container cleanup helpers
# ---------------------------------------------------------------------------

# Ownership label stamped on containers created inside worker tool calls by
# the worker container bridge. Ownership is established by the EXACT VALUE:
# the label must equal the owning worker's owner identity
# ("<session_id or 'unknown'>:<worker_name>") for teardown to reclaim the
# container. Stale values (a sibling worker's identity, a bare worker name,
# a previous session's identity) are deliberately ignored so a worker never
# stops a container it does not own.
_WORKER_CONTAINER_LABEL = "thoughtmachine.worker"
# Label marking shared resource containers (git checkouts, tooling images)
# managed by the workspace lifecycle manager — always excluded from
# worker teardown.
_RESOURCE_CONTAINER_LABEL = "thoughtmachine.resource"


def is_resource_container(container: Any) -> bool:
    """Return True when ``container`` is shared workspace infrastructure.

    Resource containers are managed by the workspace lifecycle manager and
    must never be stopped or removed during worker teardown. Handles both
    the object shape (docker container objects / ``SimpleNamespace`` with
    ``labels``, ``name``, ``image`` attributes) and the dict shape returned
    by ``ContainerManager.list_containers()`` (``container_id``, ``name``,
    ``image`` keys).
    """
    labels = getattr(container, "labels", None)
    if labels is None and isinstance(container, dict):
        labels = container.get("labels")
    if labels and labels.get(_RESOURCE_CONTAINER_LABEL):
        return True
    name = getattr(container, "name", None)
    if name is None and isinstance(container, dict):
        name = container.get("name")
    if name and str(name).startswith("tm-res-"):
        return True
    image = getattr(container, "image", None)
    if image is None and isinstance(container, dict):
        image = container.get("image")
    if image == "tm-resource-git":
        return True
    return False


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
      │                    ├──stop──▶  stopping ──▶ stopped │
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
        instance_id: int = 1,
        container_manager: Optional[Any] = None,
        *,
        max_container_count: Optional[int] = None,
        max_token_usage: Optional[int] = None,
        max_runtime_s: Optional[float] = None,
    ) -> None:
        self.instance_id = instance_id
        instance_label = _WorkerRegistry.instance_label(name, instance_id)
        super().__init__(daemon=True, name=f"worker-{instance_label}")
        self.worker_name = name
        self.definition = definition
        self._agent_config_dict = agent_config
        self.session_id = session_id
        # Container manager used to stop/remove containers owned by this
        # worker at teardown (see _cleanup_worker_containers).
        self._container_manager: Optional[Any] = container_manager
        self._worker_dir = workspace_dir / "workers" / instance_label
        self._worker_dir.mkdir(parents=True, exist_ok=True)

        # F2: generation token — a monotonic per-worker counter that guards
        # context.json against stale-thread overwrites after a force-spawn
        # (a still-alive old thread must not clobber the replacement's file).
        self._generation: int = self._allocate_generation()

        # Tool classes available to this worker (name -> class)
        self._tool_classes: Dict[str, type[ToolBase]] = tool_classes or {}

        # Session permissions for gate-checking tool calls
        self._session_permissions: Dict[str, Any] = session_permissions or {}
        # Worker-level permission footprint from definition
        self._permission_footprint: Dict[str, Any] = definition.get("permission_footprint") or definition.get("worker_permissions", {})

        # Project root from the session (resolved from workspace config)
        self._project_root: Optional[str] = project_root

        # Override timeout (from spawn parameter, else from definition, else 600)
        # NOTE: definition.get("timeout_seconds", 600) would return None if the
        # key exists with a null value, so we use "or 600" to catch that case.
        _def_timeout = definition.get("timeout_seconds")
        if _def_timeout is None:
            _def_timeout = 600
        self._timeout_seconds: int = (
            timeout_seconds
            if timeout_seconds is not None
            else _def_timeout
        )

        # Per-worker resource budgets (Phase 3, item 6).  Resolution order:
        # explicit constructor param → session_config (the nested
        # ``agent_config["session_config"]`` dict) → module defaults
        # (containers=4, tokens/runtime=unlimited).
        _session_cfg = (agent_config or {}).get("session_config") or {}
        if not isinstance(_session_cfg, dict):
            _session_cfg = {}

        self._max_container_count: int = WORKER_DEFAULT_MAX_CONTAINERS
        if max_container_count is not None:
            self._max_container_count = max_container_count
        else:
            _limits = _session_cfg.get("container_limits")
            if isinstance(_limits, dict) and _limits.get("max_containers") is not None:
                try:
                    self._max_container_count = max(1, int(_limits["max_containers"]))
                except (TypeError, ValueError):
                    self._max_container_count = WORKER_DEFAULT_MAX_CONTAINERS

        self._max_token_usage: Optional[int] = WORKER_DEFAULT_MAX_TOKENS
        if max_token_usage is not None:
            self._max_token_usage = max_token_usage
        elif _session_cfg.get("max_token_usage") is not None:
            try:
                self._max_token_usage = int(_session_cfg["max_token_usage"])
            except (TypeError, ValueError):
                self._max_token_usage = WORKER_DEFAULT_MAX_TOKENS

        self._max_runtime_s: Optional[float] = WORKER_DEFAULT_MAX_RUNTIME_S
        if max_runtime_s is not None:
            self._max_runtime_s = max_runtime_s
        elif _session_cfg.get("max_runtime_s") is not None:
            try:
                self._max_runtime_s = float(_session_cfg["max_runtime_s"])
            except (TypeError, ValueError):
                self._max_runtime_s = WORKER_DEFAULT_MAX_RUNTIME_S

        # Monotonic start of this worker's lifetime; the runtime budget is
        # measured against it (per-worker lifetime, not per-query).
        self._budget_started_at: float = time.monotonic()

        # Runtime state
        self.status: str = "ready"      # ready | busy | paused | completed | error | stopping | stopped
        self.current_task: Optional[str] = None
        self.error: Optional[str] = None
        self.last_heartbeat: Optional[str] = None
        self._last_heartbeat_monotonic: float = 0.0
        # Worker tray truth fields — persisted in status.json, exposed via
        # GET /workspace/{ws_id}/workers and published in worker_status WS
        # events. ``paused_manually`` is derived from ``_manual_only_pause``
        # (defined below), never a separate attribute.
        self.started_at: Optional[str] = None
        self.last_query_at: Optional[str] = None
        self._last_reasoning: Optional[str] = None

        # Telemetry tracking
        self._tool_call_count: int = 0
        self._timeout_triggered: bool = False
        # Idempotency guard for worker-scoped container cleanup: cleanup
        # runs at most once per thread, so it is safe to call from both
        # run() teardown and _action_stop finally paths.
        self._containers_cleaned: bool = False
        self._wlm_soft_warning_received: bool = False
        self._final_token_usage: Optional[int] = None
        self._respond_metadata: Dict[str, Any] = {}  # captures status/confidence/meta from last Respond call
        # Legacy query id of the in-flight attempt (set by run() when it
        # dequeues a (query_id, query) tuple; None for plain-string producers
        # such as the WLM handler). Stamped into the envelope so send_query
        # can correlate replies even when multiple callers overlap.
        self._current_query_id: Optional[str] = None
        # Per-caller reply channel (set by run() when it dequeues a
        # (query_id, query, reply_q) triple from send_query). Envelopes are
        # routed to this private queue when set, so concurrent send_query
        # callers can never steal each other's replies; the shared
        # _output_queue remains the fallback for plain-string producers
        # (WLM handler, spawn auto-query). None between queries.
        self._current_reply_queue: Optional[queue.Queue] = None

        # Cached authoritative token count from agent's token_update events
        self._cached_context_tokens: Optional[int] = None

        # F1 markers: last query dequeued, and last query that completed.
        # Persisted in context.json so a force-respawn can tell whether the
        # previous attempt finished (last_query == last_completed_query) or
        # was cut short by a soft timeout / force-stop. If it never completed
        # and the new spawn carries a DIFFERENT query, the stale partial
        # attempt is pruned before the new context is merged (no doubling).
        self._last_query: Optional[str] = None
        self._last_completed_query: Optional[str] = None
        # WLM query id of the last attempt — persisted alongside the F1 markers
        # so a WLM-enabled force-respawn can prune abandoned attempts exactly.
        self._last_query_id: Optional[str] = None
        # Cumulative count of stale/abandoned attempt prunes performed by this
        # worker (F1 + WLM prune paths). Persisted in context.json so it
        # survives restarts; exposed via GET /api/workspace/{ws_id}/workers
        # as ``pruned_since_last_query``.
        self._pruned_since_last_query: int = 0

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
        # UNIT C: True when this worker was paused with the manual-only flag.
        # A later query must NOT auto-resume such a worker (see _action_spawn's
        # sole-paused path); it has to be resumed explicitly via the 'resume'
        # action (which clears the marker).
        self._manual_only_pause: bool = False

    # ── public API called from the tool thread ─────────────────────

    @property
    def event_bus(self):
        """Return the per-worker EventBus instance."""
        return self._event_bus

    @property
    def owner_identity(self) -> str:
        """Owner identity stamped on containers this worker creates.

        Format: ``<session_id or 'unknown'>:<worker_name>`` — the single
        source of truth used BOTH when stamping the ``thoughtmachine.worker``
        docker label (via the worker-name context var in ``_run_tool_loop``)
        and when comparing labels at teardown
        (``_is_worker_owned_container``).
        """
        return f"{self.session_id or 'unknown'}:{self.worker_name}"

    @property
    def instance_label(self) -> str:
        """Return this instance's display label (``name`` or ``name#iid``)."""
        return _WorkerRegistry.instance_label(self.worker_name, self.instance_id)

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

    def _budget_check(self) -> Optional[Dict[str, str]]:
        """Return a fail-closed error payload when a per-worker budget is exceeded.

        Checks the runtime budget first (monotonic elapsed time vs
        ``_max_runtime_s``), then the current context-token count vs
        ``_max_token_usage``. Returns ``None`` while both budgets are within
        limits (or unset). Never raises.
        """
        if self._max_runtime_s is not None:
            if time.monotonic() - self._budget_started_at > self._max_runtime_s:
                return {
                    "error": f"Worker runtime budget exceeded ({self._max_runtime_s}s)",
                    "reason": "runtime_budget",
                }
        if self._max_token_usage is not None:
            current = self.get_current_context_tokens()
            if current > self._max_token_usage:
                return {
                    "error": f"Worker token budget exceeded ({current} > {self._max_token_usage})",
                    "reason": "token_budget",
                }
        return None

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        """Discard everything currently queued on ``q`` (get_nowait to empty)."""
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _drain_output_queue(self) -> None:
        """Discard any stale replies currently sitting in the output queue.

        A reply produced for a previous (timed-out or superseded) query must
        never satisfy a later send_query call — drain before enqueueing and
        again on timeout so late replies cannot leak into the next call.
        """
        self._drain_queue(self._output_queue)

    def _emit_reply(self, payload: str, nowait: bool = False) -> None:
        """Route a reply to the current caller's private reply queue when one
        is set (legacy send_query correlation), else to the shared output
        queue (plain-string producers: WLM handler, spawn auto-query).
        """
        target = (
            self._current_reply_queue
            if self._current_reply_queue is not None
            else self._output_queue
        )
        if nowait:
            target.put_nowait(payload)
        else:
            target.put(payload)

    def _reply_matches_query(self, response: str, query_id: str) -> bool:
        """Return True when a queue item is the envelope for ``query_id``.

        Envelopes produced by the run() loop carry a ``query_id`` key; plain
        strings (error payloads, legacy replies) and envelopes without a
        matching id cannot satisfy a correlated send_query call.
        """
        try:
            payload = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict) or "query_id" not in payload:
            return False
        return payload["query_id"] == query_id

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        """Send a query to this worker and block for a response."""
        # Workspace Lifecycle Manager fast path (feature-flagged, default off)
        try:
            used, reply = self._process_query_via_wlm(query, timeout=timeout)
        except TimeoutError:
            self._publish_global_event(EventType.WORKER_TIMEOUT, {
                "status": self.status,
                "reason": "query_timeout",
                "timeout_seconds": timeout,
                "last_heartbeat": self.last_heartbeat,
                **({"query_id": self._current_query_id}
                   if self._current_query_id is not None else {}),
            })
            raise
        if used:
            return reply
        # Correlate this call with a fresh query id and drain any stale
        # replies left over from a previous timed-out query, so a late reply
        # can never satisfy this (or the next) call. The worker run() loop
        # dequeues the (query_id, query) tuple and echoes the id back in the
        # envelope it produces.
        self._drain_output_queue()
        query_id = uuid.uuid4().hex
        # Each call gets its own private reply queue, carried to the run()
        # loop inside the input item. The worker routes its envelope to this
        # queue, so two concurrent send_query callers can never steal each
        # other's replies (the shared _output_queue is only a fallback for
        # plain-string producers). The correlation check below is kept as
        # defense in depth.
        reply_q: queue.Queue = queue.Queue()
        self._input_queue.put((query_id, query, reply_q))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                response = reply_q.get(timeout=remaining if remaining > 0 else 0)
            except queue.Empty:
                # Drain our private queue AND the shared fallback queue so a
                # late reply (on either channel) cannot leak into the next
                # call. Our queue is dropped anyway (local to this call), but
                # the drain keeps the invariant explicit.
                self._drain_queue(reply_q)
                self._drain_output_queue()
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
                self._publish_global_event(EventType.WORKER_TIMEOUT, {
                    "status": self.status,
                    "reason": "query_timeout",
                    "timeout_seconds": timeout,
                    "last_heartbeat": self.last_heartbeat,
                    **({"query_id": self._current_query_id}
                       if self._current_query_id is not None else {}),
                })
                raise TimeoutError(
                    f"Worker '{self.worker_name}' did not respond within {timeout}s{detail}"
                )
            if self._reply_matches_query(response, query_id):
                return response
            # Non-matching item: a stale envelope or another caller's reply.
            # Discard it and keep waiting for our own envelope.
            continue

    # ------------------------------------------------------------------
    # Workspace Lifecycle Manager wiring (feature-flagged, default off).
    # When enabled, send_query delegates to the supervisor; the legacy
    # queue-based path above remains byte-for-byte untouched and is used
    # whenever the flag is off or the supervisor is unavailable.
    # ------------------------------------------------------------------

    def _get_wlm(self):
        """Return the lazily-created WorkerSupervisor, or None when disabled."""
        existing = getattr(self, "_wlm", None)
        if existing is not None:
            return existing
        if not self._wlm_flag_enabled():
            return None
        from infra.workspace_lifecycle_manager import WorkerSupervisor, HARD_TIMEOUT

        supervisor = WorkerSupervisor(
            worker_id=self.worker_name,
            container_manager=getattr(self, "_container_manager", None),
            resource_container_manager=None,
            feature_flag_check=lambda: True,
            session_id=self.session_id,
            permissions_provider=lambda: self._session_permissions,
            max_container_count=getattr(
                self, "_max_container_count", WORKER_DEFAULT_MAX_CONTAINERS
            ),
        )

        def _wlm_handler(query, query_id):
            """Bridge the supervisor to the worker's existing run() loop."""
            self._input_queue.put(query)
            try:
                reply = self._output_queue.get(timeout=HARD_TIMEOUT)
            except queue.Empty:
                reply = json.dumps({"error": "Worker did not respond in time."})
            supervisor._publish_reply(query_id, reply)

        supervisor.query_handler = _wlm_handler
        self._wlm = supervisor
        log("INFO", "tools.worker",
            f"Workspace Lifecycle Manager enabled for worker '{self.worker_name}'")
        return supervisor

    def _wlm_flag_enabled(self) -> bool:
        """True when the workspace lifecycle manager feature flag is set.

        The flag is read from (in priority order):
        1. top-level ``use_workspace_lifecycle_manager`` in the injected
           agent-config dict (production plumbing: SessionConfig →
           AgentConfig → ToolExecutor → Worker tool),
        2. the legacy nested ``session_config`` dict (tests / live smoke),
        3. the worker definition.
        """
        acfg = self._agent_config_dict or {}
        if acfg.get("use_workspace_lifecycle_manager"):
            return True
        cfg = acfg.get("session_config")
        if isinstance(cfg, dict) and cfg.get("use_workspace_lifecycle_manager"):
            return True
        return bool((self.definition or {}).get("use_workspace_lifecycle_manager", False))

    def _process_query_via_wlm(self, query: str, timeout=None):
        """Route a query through the supervisor when enabled.

        Returns (used, reply): used=False means the caller should fall back
        to the legacy queue-based path.
        """
        supervisor = self._get_wlm()
        if supervisor is None:
            return (False, None)
        return (True, supervisor.process_query(query, timeout=timeout))

    def stop(self) -> None:
        """Signal the worker to stop after completing its current task."""
        # Synchronous lifecycle transition: 'stopping' must be observable
        # before the run loop acknowledges the stop event.
        self.status = "stopping"
        self._write_status_file()
        wlm = getattr(self, "_wlm", None)
        if self._wlm_flag_enabled() and wlm is not None:
            try:
                wlm.stop()
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"WLM stop delegation failed for worker '{self.worker_name}': {exc}")
        self._publish_global_event(EventType.WORKER_STOPPING, {"status": "stopping", "reason": "stop_requested"})
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
        wlm = getattr(self, "_wlm", None)
        if self._wlm_flag_enabled() and wlm is not None:
            try:
                wlm.pause()
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"WLM pause delegation failed for worker '{self.worker_name}': {exc}")
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
        wlm = getattr(self, "_wlm", None)
        if self._wlm_flag_enabled() and wlm is not None:
            try:
                wlm.resume()
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"WLM resume delegation failed for worker '{self.worker_name}': {exc}")
        # Remove any stale pause command file left by pause() while the
        # worker was busy (it would re-pause the worker on the next poll).
        # This MUST happen before _pause_event.clear(): the pause park loop
        # exits as soon as the event is cleared, so the file must already be
        # gone before the worker can wake and poll it again.  Symmetric with
        # stop()/pause() command file writes.
        try:
            (self._worker_dir / "command.json").unlink(missing_ok=True)
        except OSError:
            pass
        self._pause_event.clear()
        self._resume_event.set()
        # UNIT C: the worker is now actually running again, so the manual-only
        # marker is stale.  Clear it so later queries/spawns do not treat this
        # worker as manually paused (the REST fast-path resume reaches this
        # method without going through _action_resume, which used to be the
        # only place that cleared the marker).
        self._manual_only_pause = False
        self.status = "ready"
        self._write_status_file()

    def _poll_command(self) -> None:
        """
        Check for a ``command.json`` file in the worker's directory.

        If found, the action is ``"stop"``/``"pause"``/``"resume"``: delete
        the file and signal the corresponding event.  This enables
        cross-process control (e.g. from the Web UI via the REST API).
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
            elif action == "resume":
                # Resume while a pause is pending (or a leftover from an
                # earlier REST resume): cancel the pause so the worker keeps
                # running.  Mirrors WorkerThread.resume() semantics; without
                # this the command.json would linger forever (no-op on every
                # poll).  Events are thread-safe, so this is safe even if the
                # blocked pause loop is waiting on _resume_event.
                cmd_path.unlink(missing_ok=True)
                self._pause_event.clear()
                self._resume_event.set()
                # Mirrors WorkerThread.resume(): the worker keeps running, so
                # the manual-only marker is stale and must be cleared too.
                self._manual_only_pause = False
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
        (system_prompt, temperature, enabled_tools, max_turns,
        timeout_seconds, stop_check).

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
        # The worker NEVER inherits the main agent's system_prompt or sampling
        # settings: every sampling-relevant field below is resolved from the
        # worker definition (or its own worker defaults), so spawning cannot
        # leak main-agent settings into a worker agent.
        worker_cfg["system_prompt"] = self.definition.get(
            "system_prompt",
            DEFAULT_WORKER_SYSTEM_PROMPT,
        )
        # Temperature is worker-scoped: the worker definition wins, else the
        # worker default (0.7) — the main agent's temperature is never
        # inherited.  max_tokens / truncation_limit live on the worker
        # definition schema only (None = worker default); AgentConfig has no
        # such fields, so they are intentionally NOT injected into the cfg
        # (Pydantic's extra='ignore' would silently drop them anyway).
        worker_cfg["temperature"] = (
            self.definition.get("temperature")
            if self.definition.get("temperature") is not None
            else WORKER_DEFAULT_TEMPERATURE
        )
        worker_tools = self.definition.get("tools", [])
        parent_enabled_tools = cfg.get("enabled_tools")
        if worker_tools:
            enabled_tools = [t for t in worker_tools if t not in _WORKER_BLOCKLIST]
        else:
            from tools import SIMPLIFIED_TOOL_CLASSES
            enabled_tools = [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES
                             if cls.__name__ not in _WORKER_BLOCKLIST]
        # Intersect with parent's enabled_tools — worker cannot exceed parent
        if parent_enabled_tools is not None:
            enabled_tools = [t for t in enabled_tools if t in parent_enabled_tools]
        worker_cfg["enabled_tools"] = enabled_tools
        # Resolve max_turns: prefer definition, then parent config (treating
        # None as absent), then default 100.
        _inherit_max_turns = cfg.get("max_turns")
        if _inherit_max_turns is None:
            _inherit_max_turns = 100
        _def_max_turns = self.definition.get("max_turns")
        if _def_max_turns is None:
            _def_max_turns = _inherit_max_turns
        worker_cfg["max_turns"] = _def_max_turns
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
        _timeout_for_warning = self._timeout_seconds if self._timeout_seconds is not None else 600
        worker_cfg["time_warning_threshold"] = max(
            5, int(_timeout_for_warning * 0.8)
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

        # Safety net: ensure max_turns is a valid int (fall back to 100)
        if "max_turns" not in worker_cfg or worker_cfg.get("max_turns") is None:
            worker_cfg["max_turns"] = 100

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

        # Stamp the worker-name context var for the duration of this turn:
        # tool calls executed by the agent read it via
        # agent.core.worker_context.current_worker_name() and stamp the
        # ``thoughtmachine.worker`` docker label on containers they create,
        # so teardown can reclaim them (see module docstring). The stamp
        # carries the worker's OWNER IDENTITY (``<session_id or
        # 'unknown'>:<worker_name>``), not the bare name, so teardown can
        # match labels exactly. Reset in the finally block below so the var
        # never leaks across queries.
        _worker_ctx_token = None
        if WORKER_NAME_CONTEXTVAR is not None:
            _worker_ctx_token = WORKER_NAME_CONTEXTVAR.set(self.owner_identity)

        try:
            for event in self._agent.process_query(query):
                                # Fix 1.1B: Poll for stop command on every event
                self._poll_command()

                # Per-worker resource budgets (Phase 3, item 6): fail closed.
                budget_payload = self._budget_check()
                if budget_payload is not None:
                    self.error = budget_payload["error"]
                    final_content = json.dumps(budget_payload)
                    self._publish_event("worker_error", {
                        "error": budget_payload["error"],
                        "reason": budget_payload["reason"],
                    })
                    self._publish_global_event(
                        EventType.WORKER_ERROR if EventType is not None else "worker_error",
                        {
                            "error": budget_payload["error"],
                            "reason": budget_payload["reason"],
                        },
                    )
                    break

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
                    self._publish_event('worker_paused', {
                        'status': 'paused',
                        'worker_name': self.worker_name,
                        'session_id': self.session_id or "",
                        'current_context_tokens': self.get_current_context_tokens(),
                        'max_context_tokens': self.max_context_tokens,
                    })
                    final_content = json.dumps({
                        "status": "paused",
                        "message": "Worker paused by user",
                    })
                    break

                # WLM: surface the supervisor's soft-timeout warning to the
                # worker log exactly once per query (the worker loop keeps
                # running; the supervisor enforces the hard deadline).
                wlm = getattr(self, "_wlm", None)
                if (
                    self._wlm_flag_enabled()
                    and wlm is not None
                    and getattr(wlm, "soft_timeout_warning_emitted", False)
                    and not self._wlm_soft_warning_received
                ):
                    self._wlm_soft_warning_received = True
                    log("WARNING", "tools.worker",
                        f"[WLM] soft timeout warning received "
                        f"query_id={getattr(wlm, 'current_query_id', None)} "
                        f"worker={self.worker_name}")

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
                    # Capture Respond metadata
                    self._respond_metadata = {
                        "status": event.get("status"),
                        "confidence": event.get("confidence"),
                        "meta": event.get("meta"),
                    }

                # Phase 2B: emit WORKER_PARTIAL_RESULT while work is running so
                # non-blocking callers (submit_query) can stream progress. The
                # terminal PARTIAL_RESULT publish below covers failed/stopped
                # attempts; this covers the success path mid-flight. Turn-level
                # only (agent_responded / partial events with content) — no
                # per-token spam.
                if (
                    self._current_query_id is not None
                    and event_type in ("agent_responded", "partial", "agent_partial",
                                       "agent_message", "assistant_message")
                ):
                    _partial_content = event.get("content", "")
                    if _partial_content:
                        _publish_global_worker_event(EventType.WORKER_PARTIAL_RESULT, {
                            "worker_name": self.worker_name,
                            "session_id": self.session_id or "",
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "query_id": self._current_query_id,
                            "status": "running",
                            "reason": "partial",
                            "content": str(_partial_content)[:2000],
                        })

                # Track tool call count
                if event_type == "tool_call":
                    self._tool_call_count += 1
                    # WLM: register the in-flight execution so the supervisor
                    # can terminate it on timeout/stop.
                    wlm = getattr(self, "_wlm", None)
                    if self._wlm_flag_enabled() and wlm is not None:
                        try:
                            _container_id = event.get("container_id")
                            _exec_id = event.get("exec_id")
                            _pid = event.get("pid")
                            # Nested tool payloads (arguments/data/result) may
                            # carry the execution identifiers instead.
                            for _key in ("arguments", "data", "result"):
                                _nested = event.get(_key)
                                if isinstance(_nested, dict):
                                    if _container_id is None:
                                        _container_id = _nested.get("container_id")
                                    if _exec_id is None:
                                        _exec_id = _nested.get("exec_id")
                                    if _pid is None:
                                        _pid = _nested.get("pid")
                            _exec_type = (
                                "container_exec" if (_container_id and _pid) else "subprocess"
                            )
                            wlm.execution_tracker.register(
                                worker_id=self.worker_name,
                                query_id=wlm.current_query_id,
                                tool_call_id=event.get("tool_call_id"),
                                tool_name=event.get("name") or event.get("tool_name"),
                                container_id=_container_id,
                                exec_id=_exec_id,
                                pid=_pid,
                                type=_exec_type,
                            )
                        except Exception as exc:
                            log("WARNING", "tools.worker",
                                f"WLM execution registration failed for worker "
                                f"'{self.worker_name}': {exc}")

                elif event_type == "stopped":
                    stop_reason = event.get("stop_reason", "unknown")
                    final_content = json.dumps({
                        "error": f"Worker stopped: {stop_reason}",
                        "content": f"Worker stopped before producing a final response. Reason: {stop_reason}."
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
        finally:
            if _worker_ctx_token is not None and WORKER_NAME_CONTEXTVAR is not None:
                WORKER_NAME_CONTEXTVAR.reset(_worker_ctx_token)

        self.current_task = None
        # Store elapsed time for inclusion in query result
        self._last_elapsed_val = time.monotonic() - _start

        # Detect timeout
        if hasattr(self, '_agent') and self._agent is not None:
            try:
                agent_state = self._agent.state
                if (
                    hasattr(agent_state, 'time_state')
                    and hasattr(agent_state.time_state, 'value')
                    and agent_state.time_state.value == "CRITICAL"
                    and getattr(agent_state, 'restriction_reason', None) == "timeout"
                ):
                    self._timeout_triggered = True
            except Exception:
                pass

        # Capture final token usage
        self._final_token_usage = self.get_current_context_tokens()

        return final_content

    # ── worker-scoped container cleanup ───────────────────────────────────────────────

    def _is_worker_owned_container(self, container: Any) -> bool:
        """Return True when ``container`` belongs to this worker.

        Ownership is established by an EXACT match: the
        ``thoughtmachine.worker`` label value must equal this worker's owner
        identity (``<session_id or 'unknown'>:<worker_name>`` — see module
        docstring). Stale/mismatched values (sibling workers, bare names,
        previous sessions) are ignored.
        """
        labels = getattr(container, "labels", None)
        if labels is None and isinstance(container, dict):
            labels = container.get("labels")
        if not labels:
            return False
        return labels.get(_WORKER_CONTAINER_LABEL) == self.owner_identity

    def _cleanup_worker_containers(self) -> None:
        """Stop and remove containers owned by this worker.

        Idempotent (runs at most once per thread, guarded by
        ``_containers_cleaned``) and never raises, so it is safe to call
        from every teardown path — ``run()``'s finally block and
        ``_action_stop``'s finally paths — without masking the worker's
        own terminal status. Only containers carrying the
        worker-ownership label are touched; resource containers are always
        excluded (see ``is_resource_container``).
        """
        if getattr(self, "_containers_cleaned", False):
            return
        self._containers_cleaned = True
        cm = getattr(self, "_container_manager", None)
        if cm is None:
            return
        try:
            listed = cm.list_containers()
        except Exception:
            return
        if listed is None:
            return
        if not isinstance(listed, (list, tuple)):
            try:
                listed = list(listed)
            except TypeError:
                return
        for container in listed:
            try:
                if is_resource_container(container):
                    continue
                if not self._is_worker_owned_container(container):
                    continue
                if isinstance(container, dict):
                    target = container.get("container_id") or container.get("name")
                else:
                    target = container
                if target is None:
                    continue
                try:
                    cm.stop(target)
                except Exception:
                    pass
                try:
                    cm.remove(target)
                except Exception:
                    pass
            except Exception:
                continue

    # ── thread run loop ────────────────────────────────────────────

    def run(self) -> None:
        """Main worker loop — processes queries until stop is signalled.

        Creates the Agent and WorkerContext lazily on first query,
        reusing them for subsequent queries to maintain conversation state.
        """
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
        # Worker tray truth: run() executes exactly once per thread, so this
        # is the worker's birth timestamp.
        self.started_at = datetime.now(timezone.utc).isoformat()

        try:
            # ── Load persisted context or create fresh ────────────────
            self._worker_ctx = self._load_context()

            # Override persisted status/error with live thread state
            self.status = "ready"
            self.error = None
            self._write_status_file()
            # Create per-worker EventBus early so _publish_event below works
            self._event_bus = EventBus()
            register_worker_event_bus(self.session_id or "", self.worker_name, self._event_bus, instance_id=self.instance_id)
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
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
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
                    DEFAULT_WORKER_SYSTEM_PROMPT,
                )
                user_history = [
                    {"role": "system", "content": system_prompt}
                ]
                if self._initial_context:
                    user_history.append({
                        "role": "system",
                        "content": f"Initial context: {json.dumps(self._initial_context, default=str, ensure_ascii=True)}",
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
                    # F1: prune an incomplete attempt for a DIFFERENT query
                    # before merging the new context. Prevents context doubling
                    # on force-respawn after a soft timeout: the loaded
                    # history's last attempt belongs to a previous task
                    # (last_query != new query) and never completed
                    # (last_query != last_completed_query). Completed attempts
                    # are preserved.
                    initial_query = self._initial_context.get("query")
                    if initial_query:
                        if self._wlm_flag_enabled() and getattr(self, "_wlm", None) is not None:
                            self._prune_abandoned_attempt_via_wlm(initial_query)
                        else:
                            self._prune_stale_attempt_before_merge(initial_query)
                    # Add initial context as a system message for continuity
                    ctx_msg = {
                        "role": "system",
                        "content": f"Initial context: {json.dumps(self._initial_context, default=str, ensure_ascii=True)}",
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
                    raw = self._input_queue.get(timeout=2.0)
                except queue.Empty:
                    self._heartbeat_tick()
                    continue
                if self._stop_event.is_set():
                    break
                if raw is None:
                    # A bare None normally means stop, but when a pause was
                    # requested while the worker is idle it must NOT terminate
                    # the worker: honor the pause here instead of treating the
                    # sentinel as a stop signal (BUG 1).
                    if self._pause_event.is_set():
                        self.status = "paused"
                        self._write_status_file()
                        self._publish_event('worker_paused', {
                            'status': 'paused',
                            'worker_name': self.worker_name,
                            'session_id': self.session_id or "",
                            'current_context_tokens': self.get_current_context_tokens(),
                            'max_context_tokens': self.max_context_tokens,
                        })
                        if global_event_bus is not None and EventType is not None and create_event is not None:
                            try:
                                evt = create_event(
                                    EventType.WORKER_STATUS,
                                    data={
                                        "session_id": self.session_id or "",
                                        "worker_name": self.worker_name,
                                        "instance_id": self.instance_id,
                                        "instance_label": self.instance_label,
                                        "status": "paused",
                                        "current_context_tokens": self.get_current_context_tokens(),
                                        "max_context_tokens": self.max_context_tokens,
                                        "started_at": self.started_at,
                                        "last_query_at": self.last_query_at,
                                        "paused_manually": bool(self._manual_only_pause),
                                    },
                                    source=f"worker:{self.worker_name}",
                                    session_id=self.session_id or "",
                                )
                                global_event_bus.publish(evt)
                            except Exception:
                                pass
                        self.current_task = None
                        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
                        # Block until resumed (or stopped)
                        while self._pause_event.is_set() and not self._stop_event.is_set():
                            self._heartbeat_tick()
                            self._resume_event.wait(1.0)
                        if self._stop_event.is_set():
                            break
                        # Resume — transition back to ready
                        self.status = "ready"
                        self._resume_event.clear()
                        self._write_status_file()
                        self._publish_event('worker_resumed', {
                            'status': 'ready',
                            'worker_name': self.worker_name,
                            'session_id': self.session_id or "",
                            'current_context_tokens': self.get_current_context_tokens(),
                            'max_context_tokens': self.max_context_tokens,
                        })
                        if global_event_bus is not None and EventType is not None and create_event is not None:
                            try:
                                evt = create_event(
                                    EventType.WORKER_STATUS,
                                    data={
                                        "session_id": self.session_id or "",
                                        "worker_name": self.worker_name,
                                        "instance_id": self.instance_id,
                                        "instance_label": self.instance_label,
                                        "status": "ready",
                                        "current_context_tokens": self.get_current_context_tokens(),
                                        "max_context_tokens": self.max_context_tokens,
                                        "started_at": self.started_at,
                                        "last_query_at": self.last_query_at,
                                        "paused_manually": bool(self._manual_only_pause),
                                    },
                                    source=f"worker:{self.worker_name}",
                                    session_id=self.session_id or "",
                                )
                                global_event_bus.publish(evt)
                            except Exception:
                                pass
                        # Drain stale None/unblock signals left by pause() so
                        # they are not misinterpreted as stop signals.
                        try:
                            while True:
                                item = self._input_queue.get_nowait()
                                if item is not None:
                                    # Real query — put it back in front
                                    self._input_queue.put(item)
                                    break
                        except queue.Empty:
                            pass
                        continue
                    # Bare None with no pause and no stop event is the legacy
                    # stop sentinel. Preserve that contract.
                    break
                # send_query enqueues (query_id, query[, reply_q]) tuples so
                # replies can be correlated and routed to the caller's private
                # queue; plain-string producers (WLM handler, initial
                # auto-query, stop/pause markers) carry no id and keep the
                # shared _output_queue fallback.
                if isinstance(raw, tuple) and len(raw) == 3:
                    query_id, query, reply_q = raw
                elif isinstance(raw, tuple) and len(raw) == 2:
                    query_id, query = raw
                    reply_q = None
                else:
                    query_id, query, reply_q = None, raw, None
                self._current_query_id = query_id
                self._current_reply_queue = reply_q

                # F1: record the most recent query dequeued (even if it times
                # out mid-attempt) so a later force-respawn can tell whether
                # the last attempt completed.
                self._last_query = query
                # Worker tray truth: record when this worker accepted a query.
                self.last_query_at = datetime.now(timezone.utc).isoformat()

                # ── Create Agent lazily (first query only) ────────────
                if self._agent is None:
                    agent_cfg = self._build_agent_config()
                    if agent_cfg is None:
                        reply = json.dumps({
                            "error": "Cannot create Agent: invalid agent_config",
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        })
                        self._emit_reply(reply)
                        break
                    # Lazy import to avoid circular dep: agent.core.agent ↔ tools
                    try:
                        from agent.core.agent import Agent
                    except ImportError:
                        reply = json.dumps({
                            "error": "Cannot create Agent: module not importable",
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        })
                        self._emit_reply(reply)
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
                                "instance_id": self.instance_id,
                                "instance_label": self.instance_label,
                                "status": "busy",
                                "current_task": self.current_task,
                                "current_context_tokens": self.get_current_context_tokens(),
                                "max_context_tokens": self.max_context_tokens,
                                "started_at": self.started_at,
                                "last_query_at": self.last_query_at,
                                "paused_manually": bool(self._manual_only_pause),
                            },
                            source=f"worker:{self.worker_name}",
                            session_id=self.session_id or "",
                        )
                        global_event_bus.publish(evt)
                    except Exception:
                        pass
                self._publish_global_event(EventType.WORKER_RUNNING, {
                    "status": "busy",
                    "current_task": self.current_task,
                    "current_context_tokens": self.get_current_context_tokens(),
                    "max_context_tokens": self.max_context_tokens,
                    **({"query_id": self._current_query_id}
                       if self._current_query_id is not None else {}),
                })
                # ── Reset turn/time state per query ────────────────────────
                # Each query is an isolated unit — reset turn counter and
                # time tracking so the restriction pipeline starts fresh.
                if self._agent is not None and hasattr(self._agent, 'state'):
                    self._agent.state.current_turn = 0
                    if TurnState is not None:
                        self._agent.state.turn_state = TurnState.LOW
                        self._agent.state.last_turn_warning_state = TurnState.LOW
                    self._agent.state.restrictions_active = False
                    self._agent.state.restrictions_pending = False
                    self._agent.state.restriction_reason = None
                    # wall-clock time; agent.py uses time.time() for consistency
                    self._agent.state.time_start = time.time()
                    if TimeState is not None:
                        self._agent.state.last_time_warning_state = TimeState.LOW
                # A soft timeout is per-query: clear the sticky flag so the
                # next query starts fresh (same per-query reset semantics as
                # the turn/time state above, but independent of the agent).
                self._timeout_triggered = False

                reply = self._run_tool_loop(query)

                # Check if worker was paused during the tool loop
                if self._pause_event.is_set():
                    # Preserve paused status — don't overwrite with "ready"
                    self.status = "paused"
                    self._write_status_file()
                    self._publish_event('worker_paused', {
                        'status': 'paused',
                        'worker_name': self.worker_name,
                        'session_id': self.session_id or "",
                        'current_context_tokens': self.get_current_context_tokens(),
                        'max_context_tokens': self.max_context_tokens,
                    })
                    # Also publish to global_event_bus so the bridge receives it
                    if global_event_bus is not None and EventType is not None and create_event is not None:
                        try:
                            evt = create_event(
                                EventType.WORKER_STATUS,
                                data={
                                    "session_id": self.session_id or "",
                                    "worker_name": self.worker_name,
                                    "instance_id": self.instance_id,
                                    "instance_label": self.instance_label,
                                    "status": "paused",
                                    "current_context_tokens": self.get_current_context_tokens(),
                                    "max_context_tokens": self.max_context_tokens,
                                    "started_at": self.started_at,
                                    "last_query_at": self.last_query_at,
                                    "paused_manually": bool(self._manual_only_pause),
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

                    # Terminalize any in-flight job: a Pause All during an
                    # async job must not strand it as 'running' forever, and a
                    # later resume must not complete or restart it (BUG 2).
                    if self._current_query_id is not None:
                        try:
                            _registry = _get_worker_job_registry()
                            if _registry is not None:
                                _registry.terminalize(
                                    self._current_query_id,
                                    status="paused",
                                    envelope={"content": reply, "status": "paused"},
                                )
                        except Exception:
                            pass

                    # Send the pause response back to the waiting tool call.
                    # A correlated (legacy send_query) caller needs the query id
                    # echoed so it can accept the reply; plain-string producers
                    # (WLM handler, spawn auto-query) keep the raw reply.
                    if self._current_query_id is not None:
                        self._emit_reply(json.dumps({
                            "content": reply,
                            "status": "paused",
                            "query_id": self._current_query_id,
                        }))
                    else:
                        self._emit_reply(reply)

                    # Block until resumed (or stopped)
                    while self._pause_event.is_set() and not self._stop_event.is_set():
                        self._heartbeat_tick()
                        self._resume_event.wait(1.0)

                    if self._stop_event.is_set():
                        break

                    # Resume — transition back to ready
                    self.status = "ready"
                    self._resume_event.clear()
                    self._write_status_file()
                    self._publish_event('worker_resumed', {
                        'status': 'ready',
                        'worker_name': self.worker_name,
                        'session_id': self.session_id or "",
                        'current_context_tokens': self.get_current_context_tokens(),
                        'max_context_tokens': self.max_context_tokens,
                    })
                    if global_event_bus is not None and EventType is not None and create_event is not None:
                        try:
                            evt = create_event(
                                EventType.WORKER_STATUS,
                                data={
                                    "session_id": self.session_id or "",
                                    "worker_name": self.worker_name,
                                    "instance_id": self.instance_id,
                                    "instance_label": self.instance_label,
                                    "status": "ready",
                                    "current_context_tokens": self.get_current_context_tokens(),
                                    "max_context_tokens": self.max_context_tokens,
                                    "started_at": self.started_at,
                                    "last_query_at": self.last_query_at,
                                    "paused_manually": bool(self._manual_only_pause),
                                },
                                source=f"worker:{self.worker_name}",
                                session_id=self.session_id or "",
                            )
                            global_event_bus.publish(evt)
                        except Exception:
                            pass

                    # Drain stale None/unblock signals from input queue.
                    # These are left behind by pause() or _poll_command()
                    # when the worker was busy in _run_tool_loop.  Without
                    # this drain, the main loop interprets them as stop
                    # signals (line 1172) and exits immediately after resume.
                    try:
                        while True:
                            item = self._input_queue.get_nowait()
                            if item is not None:
                                # Real query — put it back in front
                                self._input_queue.put(item)
                                break
                    except queue.Empty:
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
                                "instance_id": self.instance_id,
                                "instance_label": self.instance_label,
                                "status": "ready",
                                "current_context_tokens": self.get_current_context_tokens(),
                                "max_context_tokens": self.max_context_tokens,
                                "started_at": self.started_at,
                                "last_query_at": self.last_query_at,
                                "paused_manually": bool(self._manual_only_pause),
                            },
                            source=f"worker:{self.worker_name}",
                            session_id=self.session_id or "",
                        )
                        global_event_bus.publish(evt)
                    except Exception:
                        pass

                self.current_task = None
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                # F1: mark this query as completed — unless a soft timeout cut
                # it short, in which case the attempt is left incomplete so a
                # later force-respawn can prune it.
                if not self._timeout_triggered:
                    self._last_completed_query = query

                # Compact conversation history after summarization
                # (Agent inserts summary messages but doesn't remove old ones)
                if self._worker_ctx is not None:
                    self._worker_ctx.compact_after_summary()

                # Persist and log
                if self._wlm_flag_enabled() and getattr(self, "_wlm", None) is not None:
                    try:
                        self._last_query_id = self._wlm.current_query_id
                    except Exception:
                        pass
                self._save_context()

                # Send the response back to the waiting tool call

                # Build telemetry
                telemetry = {
                    "elapsed_seconds": round(self._last_elapsed_val, 1) if self._last_elapsed_val else None,
                    "tool_call_count": self._tool_call_count,
                    "timeout_triggered": self._timeout_triggered,
                    "token_usage": self._final_token_usage,
                }

                # Determine status for force-stop cases
                status = self._respond_metadata.get("status")
                if self._timeout_triggered and not status:
                    status = "timeout"

                # Build envelope
                envelope = {
                    "content": reply if reply else "Worker finished with no output.",
                    "status": status,
                    "confidence": self._respond_metadata.get("confidence"),
                    "meta": self._respond_metadata.get("meta"),
                    "telemetry": telemetry,
                }

                # Legacy path: expose this attempt's query id in the envelope
                # so send_query can correlate replies even when multiple
                # callers overlap. The WLM block below keeps precedence when
                # the supervisor is active (plain-string producer, so this
                # legacy stamp stays None in that case anyway).
                if self._current_query_id is not None:
                    envelope["query_id"] = self._current_query_id

                # WLM: expose the supervisor's query id in the envelope so the
                # caller can correlate the reply with the lifecycle manager.
                if self._wlm_flag_enabled() and getattr(self, "_wlm", None) is not None:
                    try:
                        envelope["query_id"] = self._wlm.current_query_id
                    except Exception:
                        pass

                # If reply is already JSON (error case), parse and merge
                if reply:
                    try:
                        parsed_reply = json.loads(reply)
                        if isinstance(parsed_reply, dict):
                            if "error" in parsed_reply:
                                envelope["content"] = parsed_reply.get("error", reply)
                                envelope["status"] = envelope["status"] or ("timeout" if self._timeout_triggered else "error")
                            else:
                                envelope["content"] = reply
                    except (json.JSONDecodeError, TypeError):
                        envelope["content"] = reply

                if envelope.get("status") in ("stopped", "paused", "error", "timeout", "max_turns", "max_turns_reached"):
                    self._publish_global_event(EventType.WORKER_PARTIAL_RESULT, {
                        "status": envelope.get("status"),
                        "reason": envelope.get("status"),
                        "content": str(envelope.get("content", ""))[:2000],
                        "current_context_tokens": self.get_current_context_tokens(),
                        "max_context_tokens": self.max_context_tokens,
                        **({"query_id": self._current_query_id}
                           if self._current_query_id is not None else {}),
                    })

                # Phase 2B: record the completed job (best-effort — never
                # breaks the worker). The registry stores the full envelope so
                # non-blocking callers (submit_query) can retrieve results via
                # job_status.
                if self._current_query_id is not None:
                    try:
                        _registry = _get_worker_job_registry()
                        if _registry is not None:
                            _registry.complete(self._current_query_id, envelope)
                    except Exception:
                        pass

                self._emit_reply(json.dumps(envelope, default=str))

        except Exception as exc:
            logger.exception("Worker thread %s failed", self.worker_name)
            self.status = "error"
            self.error = str(exc)
            self._write_status_file()
            # Put the error into the output queue so any waiting query call gets it
            error_json = json.dumps({
                "error": str(exc),
                **({"query_id": self._current_query_id}
                   if self._current_query_id is not None else {}),
            })
            try:
                self._emit_reply(error_json, nowait=True)
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
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "error": str(exc),
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        },
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
        else:
            # Terminal status on the stop path is 'stopped' (not 'completed');
            # a clean drain with no stop request ends as 'completed'.
            final_status = "stopped" if self._stop_event.is_set() else "completed"
            self.status = final_status
            self._write_status_file()
            self._publish_event('worker_completed', {'status': final_status, 'worker_name': self.worker_name})
            # Also publish to global_event_bus so the bridge receives it
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_COMPLETED,
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "status": final_status,
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        },
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
        finally:
            try:
                unregister_worker_event_bus(self.session_id or "", self.worker_name, instance_id=self.instance_id)
            except Exception:
                pass
            if self._worker_ctx is not None:
                self._save_context()
            # Stop/remove containers this worker created (idempotent).
            self._cleanup_worker_containers()

    # ── persistence ────────────────────────────────────────────────

    def _context_path(self) -> Path:
        return self._worker_dir / "context.json"

    def _allocate_generation(self) -> int:
        """F2: allocate a monotonic per-worker generation token.

        Reads the current generation from a small sidecar file
        (``worker_dir/generation``) plus any generation already persisted in
        context.json, then writes ``max + 1`` back to the sidecar.  Every new
        WorkerThread instance therefore holds a strictly higher generation than
        any thread it replaces, so a still-alive stale thread's
        ``_save_context()`` is rejected by the guard once the replacement has
        persisted.  The context.json fallback keeps the counter monotonic even
        if the sidecar file is lost.
        """
        gen_file = self._worker_dir / "generation"
        base = 0
        try:
            if gen_file.exists():
                base = max(base, int(gen_file.read_text(encoding="utf-8").strip() or 0))
        except (OSError, ValueError):
            pass
        ctx_path = self._context_path()
        if ctx_path.exists():
            try:
                base = max(base, int(json.loads(ctx_path.read_text(encoding="utf-8")).get("generation") or 0))
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        new_gen = base + 1
        try:
            fd, tmp_path_str = tempfile.mkstemp(
                dir=str(self._worker_dir), prefix=".generation_", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(str(new_gen))
                if FileLock is not None:
                    with FileLock(str(gen_file)):
                        os.replace(tmp_path_str, str(gen_file))
                else:
                    os.replace(tmp_path_str, str(gen_file))
            except Exception:
                try:
                    if os.path.exists(tmp_path_str):
                        os.unlink(tmp_path_str)
                except OSError:
                    pass
        except OSError:
            pass
        return new_gen

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
                self._last_query = data.get("last_query")
                self._last_completed_query = data.get("last_completed_query")
                self._last_query_id = data.get("last_query_id")
                try:
                    self._pruned_since_last_query = int(
                        data.get("pruned_since_last_query") or 0
                    )
                except (TypeError, ValueError):
                    self._pruned_since_last_query = 0
                # F2: restore generation; keep the max so this thread's saves
                # are never rejected against a file it just loaded.
                try:
                    file_gen = int(data.get("generation") or 0)
                except (TypeError, ValueError):
                    file_gen = 0
                self._generation = max(self._generation, file_gen)
                return ctx
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to load worker context %s: %s", path, exc
                )
        return None

    def _prune_stale_attempt_before_merge(self, new_query: str) -> None:
        """F1: drop an incomplete attempt for a DIFFERENT query before merging new context.

        Prevents context doubling on force-respawn after a soft timeout: the loaded
        history's last attempt belongs to a previous task (last_query != new_query)
        and never completed (last_query != last_completed_query). Truncate from the
        last "Initial context" boundary so the new query does not execute against
        (and double) the stale partial attempt. Completed attempts are preserved.
        """
        last_query = self._last_query
        last_completed = self._last_completed_query
        if not last_query or last_query == new_query:
            return  # same-task retry (or legacy context w/o marker) — keep history
        if last_query == last_completed:
            return  # last attempt completed — completed work is kept
        history = self._worker_ctx.user_history
        ic_indices = [
            i for i, msg in enumerate(history)
            if msg.get("role") == "system"
            and isinstance(msg.get("content"), str)
            and msg["content"].startswith("Initial context: ")
        ]
        if not ic_indices:
            return
        boundary = ic_indices[-1]
        del history[boundary:]
        self._pruned_since_last_query += 1
        logger.debug(
            "Pruned incomplete attempt for query %r before merging new context (worker '%s')",
            last_query,
            self.worker_name,
        )

    def _prune_abandoned_attempt_via_wlm(self, new_query: str) -> None:
        """WLM variant of F1: prune an attempt the supervisor marked abandoned.

        Uses the supervisor's query outcome log: when the last attempt's query
        id is recorded as abandoned (timed out / stopped mid-query), truncate
        the loaded history from the last "Initial context" boundary before the
        new context is merged. Falls back to the legacy F1 heuristic whenever
        the WLM signal is unavailable (no supervisor, no query id, or the id
        is not in the log).
        """
        wlm = getattr(self, "_wlm", None)
        if wlm is None:
            self._prune_stale_attempt_before_merge(new_query)
            return
        try:
            abandoned = set(wlm.abandoned_query_ids())
            completed = set(wlm.completed_query_ids())
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"WLM query-log read failed for worker '{self.worker_name}' "
                f"(falling back to F1): {exc}")
            self._prune_stale_attempt_before_merge(new_query)
            return
        last_qid = getattr(self, "_last_query_id", None)
        if not last_qid:
            self._prune_stale_attempt_before_merge(new_query)
            return
        if last_qid in completed:
            return  # last attempt completed — completed work is kept
        if last_qid not in abandoned:
            # WLM has no verdict for this id (e.g. pre-WLM context) — fall back.
            self._prune_stale_attempt_before_merge(new_query)
            return
        history = self._worker_ctx.user_history
        ic_indices = [
            i for i, msg in enumerate(history)
            if msg.get("role") == "system"
            and isinstance(msg.get("content"), str)
            and msg["content"].startswith("Initial context: ")
        ]
        if not ic_indices:
            return
        boundary = ic_indices[-1]
        del history[boundary:]
        self._pruned_since_last_query += 1
        logger.debug(
            "Pruned WLM-abandoned attempt (query_id=%s) before merging new context (worker '%s')",
            last_qid,
            self.worker_name,
        )

    def _write_status_file(self) -> None:
        """
        Write runtime status to ``status.json`` so the web API backend
        (which runs in a separate process) can read it.

        This file is read by ``GET /api/workspace/{ws_id}/workers``
        to populate ``runtime_status``, ``current_task``,
        ``last_heartbeat``, ``error``, ``started_at``,
        ``last_query_at`` and ``paused_manually`` for each worker.
        """
        data = {
            "runtime_status": self.status,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "error": self.error,
            "session_id": self.session_id,
            "current_context_tokens": self.get_current_context_tokens(),
            "max_context_tokens": self.max_context_tokens,
            "started_at": self.started_at,
            "last_query_at": self.last_query_at,
            "paused_manually": bool(self._manual_only_pause),
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
        target = self._context_path()
        # F2: generation guard — a stale writer (an older WorkerThread instance
        # still alive after a force-spawn) must not overwrite the replacement
        # thread's context.json.  Skip the save if the file already carries a
        # NEWER generation than this thread's.
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                existing_gen = int(existing.get("generation") or 0)
                if existing_gen > self._generation:
                    logger.debug(
                        "Stale worker '%s' (generation %d) skipped context save: "
                        "file already has generation %d",
                        self.worker_name, self._generation, existing_gen,
                    )
                    return
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        ctx_data = self._worker_ctx.to_persistable_dict()
        data = {
            **ctx_data,
            "status": self.status,
            "error": self.error,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "last_query": self._last_query,
            "last_completed_query": self._last_completed_query,
            "last_query_id": getattr(self, "_last_query_id", None),
            "pruned_since_last_query": getattr(self, "_pruned_since_last_query", 0),
            "generation": self._generation,
        }
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

    def _publish_global_event(self, event_type, data: dict) -> None:
        """Publish a lifecycle event to the *global* event bus (best-effort).

        Auto-injects ``session_id`` and ``worker_name`` so call sites can stay
        minimal. Never raises — failures are swallowed by the module helper.
        """
        _publish_global_worker_event(event_type, {
            "session_id": self.session_id or "",
            "worker_name": self.worker_name,
            "instance_id": self.instance_id,
            "instance_label": self.instance_label,
            **data,
        })

    def _heartbeat_tick(self) -> None:
        """Publish a WORKER_HEARTBEAT if the interval has elapsed (no-op otherwise).

        Updates ``last_heartbeat`` in memory (status.json writes remain the
        responsibility of the existing status-file machinery). Called from the
        idle loop, the pause loop, and the busy query path.
        """
        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < HEARTBEAT_INTERVAL_S:
            return
        self._last_heartbeat_monotonic = now
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
        self._publish_global_event(EventType.WORKER_HEARTBEAT, {
            "status": self.status,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "current_context_tokens": self.get_current_context_tokens(),
            "max_context_tokens": self.max_context_tokens,
        })

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
    # Workers are not gated by session permissions: spawning/running a worker
    # is decoupled from the 'execution' session permission (2026-08-03).
    required_categories: ClassVar[List[str]] = []

    action: str = Field(
        description="Action: list, spawn, check, query, stop, submit_query, job_status. "
        "When action='spawn' and context has a 'query' key, the spawn call "
        "BLOCKS until the worker finishes the task and returns the full result. "
        "Without a 'query' key, spawn returns immediately; use action='query' later. "
        "action='submit_query' enqueues a query and returns immediately with a job_id "
        "(non-blocking); poll progress with action='job_status' (worker_query = job id, "
        "or empty to list this worker's jobs)."
    )

    worker_name: Optional[str] = Field(
        default=None,
        description="Name of the worker",
    )

    worker_query: Optional[str] = Field(
        default=None,
        description="Query string to send to an already-spawned worker. "
        "Only valid with action='query' (the call BLOCKS until the worker responds), "
        "action='submit_query' (non-blocking; returns a job handle immediately), "
        "or action='job_status' (job id to look up; empty lists jobs for the worker).",
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

    instance_id: Optional[int] = Field(
        default=None,
        description="Worker instance ID to address. When omitted, actions that "
        "target a worker by name operate on the single live instance and error "
        "with an ambiguity message if the name maps to multiple live instances. "
        "Spawn with an explicit instance_id to create/replace that instance; "
        "without it a fresh instance id is allocated (1, 2, ...).",
    )

    force: bool = Field(
        default=False,
        description="If True, stop any existing worker instance (across all sessions) before spawning a fresh one.",
    )

    purpose: Optional[str] = Field(
        default=None,
        description="Purpose hint for the worker: 'coding', 'reviewing', 'researching', 'opinion', etc."
    )
    style: Optional[Literal["precise", "exploratory"]] = Field(
        default="precise",
        description="Worker behaviour: 'precise' = focused, minimal output; 'exploratory' = thorough, may digress"
    )
    meta: Optional[dict] = Field(
        default=None,
        description="General-purpose metadata dict forwarded to worker context"
    )

    skip_output_truncation: ClassVar[bool] = True

    VALID_ACTIONS: ClassVar[list[str]] = ["list", "spawn", "check", "query", "stop", "submit_query", "job_status", "join", "wait_for_job", "pause", "resume"]

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

            if self.action in ("spawn", "check", "query", "submit_query", "join", "wait_for_job") and not self.worker_name:
                return json.dumps({
                    "error": f"worker_name is required for action '{self.action}'",
                })

            # === Resolve workspace ID from registries (primary, always correct) ===
            ws_id = None
            workspace_path_for_fallback = getattr(self, 'workspace_path', None)

            # 1. Ask SessionRegistry for the session's workspace_id
            if self.session_id:
                try:
                    from session.session_registry import SessionRegistry
                    session_info = SessionRegistry.get_default().get(self.session_id)
                    if session_info and session_info.get("workspace_id"):
                        ws_id = session_info["workspace_id"]
                except Exception:
                    pass

            # 2. Fallback to deprecated AgentConfig.workspace_path
            if not ws_id and workspace_path_for_fallback and CAPABILITIES_AVAILABLE:
                ws_id = resolve_workspace_id(workspace_path_for_fallback)
                if ws_id:
                    import logging
                    logging.warning(
                        "Worker tool resolved workspace via deprecated AgentConfig.workspace_path. "
                        "Consider ensuring session_id is present."
                    )

            workers = self._load_workers(ws_id)

            handler = {
                "list": lambda: self._action_list(workers),
                "spawn": lambda: self._action_spawn(workers, ws_id),
                "check": lambda: self._action_check(workers),
                "query": lambda: self._action_query(workers),
                "stop": lambda: self._action_stop(workers),
                "submit_query": lambda: self._action_submit_query(workers),
                "job_status": lambda: self._action_job_status(workers),
                "join": lambda: self._action_join(workers),
                "wait_for_job": lambda: self._action_join(workers),
                "pause": lambda: self._action_pause(workers),
                "resume": lambda: self._action_resume(workers),
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

        Falls back to scanning workspace directories or ``_load_template_workers()``
        when the file is missing, empty, fails to parse, or when the workspace ID
        cannot be resolved.

        Template workers are **merged** into the result — any worker template found
        on disk (in ``resources/worker_templates/``) that is not already present in
        the workspace's ``workers.json`` is appended automatically.  This ensures
        newly added templates (e.g. ``default.json``) are always
        available without requiring workspace re-initialisation.
        """
        if not CAPABILITIES_AVAILABLE or not _workspace_dir:
            return []

        if not ws_id:
            scanned = self._scan_workspace_dirs_for_workers()
            if scanned:
                return self._merge_template_workers(scanned)
            logger.info("No workspace ID resolved and no workers.json found in any workspace, falling back to template workers")
            return _load_template_workers()

        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            logger.info(f"workers.json not found at {workers_path}, falling back to templates")
            return _load_template_workers()

        try:
            data = json.loads(workers_path.read_text(encoding="utf-8"))
            if not data:  # empty array
                logger.info(f"workers.json is empty, falling back to templates")
                return _load_template_workers()
            return self._merge_template_workers(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load workers.json: {e}, falling back to templates")
            return _load_template_workers()

    @staticmethod
    def _merge_template_workers(workers: list) -> list:
        """
        Merge template workers from disk into *workers* (in-place of the list
        object, but returned for chaining).

        Any worker definition found in ``_load_template_workers()`` whose
        ``name`` is not already present in *workers* is appended.  This lets
        newly added template files (e.g. ``default.json``)
        become available without requiring workspace re-initialisation.

        Workers from the workspace file take precedence — existing names are
        never overwritten.
        """
        existing_names: set[str] = {
            w["name"] for w in workers
            if isinstance(w, dict) and w.get("name")
        }
        for template in _load_template_workers():
            name = template.get("name")
            if name and name not in existing_names:
                workers.append(template)
                existing_names.add(name)
                logger.info("Merged template worker '%s' into workspace workers", name)
        return workers

    def _scan_workspace_dirs_for_workers(self) -> list:
        """
        Scan ``~/.thoughtmachine/workspaces/<id>/workers.json`` for all workspace
        directories and return the first valid workers list found.

        This is a fallback when ``ws_id`` cannot be resolved (e.g. when the
        session's ``workspace_path`` config is not set).
        """
        import os

        base = Path(os.path.expanduser("~/.thoughtmachine/workspaces"))
        if not base.is_dir():
            return []

        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            workers_path = entry / "workers.json"
            if not workers_path.is_file():
                continue
            try:
                data = json.loads(workers_path.read_text(encoding="utf-8"))
                if data and isinstance(data, list):
                    logger.info(
                        "Loaded %d workers from %s (workspace fallback scan)",
                        len(data), workers_path,
                    )
                    return data
            except (json.JSONDecodeError, OSError):
                continue

        return []

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

        # Resolve workspace path from registries (primary)
        ws_path = self._resolve_registry_workspace()
        if ws_path:
            result["workspace_path"] = ws_path
        else:
            # Last-resort fallback for worker config propagation
            legacy_ws = getattr(self, 'workspace_path', None)
            if legacy_ws:
                result["workspace_path"] = legacy_ws

        # Propagate the workspace-lifecycle-manager feature flag so the
        # WorkerThread's _wlm_flag_enabled() sees it at the top level.
        # ToolExecutor normally injects it already; the nested fallback
        # covers direct callers that pass the legacy ``session_config``
        # shape.
        if "use_workspace_lifecycle_manager" not in result:
            nested_cfg = cfg.get("session_config") or {}
            result["use_workspace_lifecycle_manager"] = bool(
                nested_cfg.get("use_workspace_lifecycle_manager")
            )

        # Propagate the container-registry feature flag the same way.
        # The ToolExecutor normally injects it already; the nested fallback
        # covers direct callers that pass the legacy ``session_config`` shape.
        if "use_container_registry" not in result:
            nested_cfg = cfg.get("session_config") or {}
            result["use_container_registry"] = bool(
                nested_cfg.get("use_container_registry")
            )

        return result

    def _resolve_tool_class(self, tool_name: str) -> Optional[type[ToolBase]]:
        """Resolve a tool name string to its class via _TOOL_REGISTRY.

        Uses ``_get_tool_registry()`` which builds the registry lazily
        on first access to avoid the circular import with ``tools.__init__``.
        """
        cls = _get_tool_registry().get(tool_name)
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
        return _WorkerRegistry.get_instance().find_workers_by_name(worker_name)

    def _effective_max_workers(self) -> int:
        """
        Return this session's worker spawn cap.

        Reads the ``max_workers`` key from the injected ``agent_config``
        (top level first, then nested under ``session_config`` for the legacy
        shape). Invalid values (non-int, unparseable, or <= 0) fall back to
        the safe default ``MAX_WORKERS_PER_SESSION``.
        """
        cfg = self.agent_config or {}
        raw = cfg.get("max_workers")
        if raw is None:
            nested_cfg = cfg.get("session_config")
            if isinstance(nested_cfg, dict):
                raw = nested_cfg.get("max_workers")
        if raw is None:
            return MAX_WORKERS_PER_SESSION
        try:
            cap = int(raw)
        except (TypeError, ValueError):
            return MAX_WORKERS_PER_SESSION
        if cap <= 0:
            return MAX_WORKERS_PER_SESSION
        return cap

    def _live_workers_in_session(self, session_key: str) -> int:
        """
        Count LIVE worker threads currently registered for ``session_key``.

        Only threads whose ``is_alive()`` is True count toward the spawn cap;
        dead (completed/stopped/errored) registry entries do not.
        """
        count = 0
        with _registry_lock:
            for key, thread in _worker_registry.items():
                sid = key[0]
                if sid == session_key and thread.is_alive():
                    count += 1
        return count

    # -- action implementations --------------------------------------

    def _action_list(self, workers: list) -> dict:
        """Return all known worker definitions plus runtime status."""
        session_key = self.session_id or _NO_SESSION_KEY
        augmented = []
        for w in workers:
            name = w.get("name", "")
            base = dict(w)
            # Merge runtime status from registry — one entry per live instance.
            with _registry_lock:
                instances = [
                    (key[2] if len(key) >= 3 else 1, thread)
                    for key, thread in _worker_registry.items()
                    if key[0] == session_key and key[1] == name
                ]
            if instances:
                for iid, thread in sorted(instances, key=lambda item: item[0]):
                    entry = dict(base)
                    entry["instance_id"] = iid
                    entry["instance_label"] = (
                        thread.instance_label
                        if hasattr(thread, "instance_label")
                        else _WorkerRegistry.instance_label(name, iid)
                    )
                    entry["runtime_status"] = thread.status
                    entry["current_task"] = thread.current_task
                    entry["last_heartbeat"] = thread.last_heartbeat
                    entry["error"] = thread.error
                    augmented.append(entry)
            else:
                entry = dict(base)
                entry["instance_id"] = None
                entry["instance_label"] = name
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
                    # F3: compact summarized history before persisting so a
                    # force-stop right after SummarizeTool does not persist ~2x.
                    if thread._worker_ctx is not None:
                        thread._worker_ctx.compact_after_summary()
                    thread._save_context()
                    with _registry_lock:
                        for key, t in list(_worker_registry.items()):
                            if (
                                key[0] == sid
                                and key[1] == self.worker_name
                                and t is thread
                            ):
                                _worker_registry.pop(key, None)
                stopped_info.append({"session_id": sid, "status": thread.status})
            if stopped_info:
                logger.info(
                    "Force-spawn: stopped %d stale instance(s) of worker '%s'",
                    len(stopped_info), self.worker_name,
                )

        # Prevent duplicate spawns (session-scoped key, instance-aware).
        # Name-only spawns allocate a fresh instance id (1, 2, ...); the sole
        # exception is a single PAUSED instance, which is resumed so the main
        # agent can seamlessly re-query it without manual stop/resume steps.
        session_key = self.session_id or _NO_SESSION_KEY
        resume_paused_worker = None
        spawn_iid = None
        explicit_iid = self.instance_id
        with _registry_lock:
            live_same = []
            for key, t in list(_worker_registry.items()):
                if key[0] == session_key and key[1] == self.worker_name:
                    iid = key[2] if len(key) >= 3 else 1
                    if bool(getattr(t, "is_alive", lambda: True)()):
                        live_same.append((iid, t))
            if explicit_iid is not None:
                match = [t for iid, t in live_same if iid == explicit_iid]
                if match:
                    existing = match[0]
                    if existing.status == "paused":
                        if getattr(existing, "_manual_only_pause", False):
                            return {
                                "error": (
                                    f"Worker '{self.worker_name}' instance "
                                    f"{explicit_iid} is paused (manual-only) "
                                    "and will not be auto-resumed by a query; "
                                    "resume it explicitly with the 'resume' action."
                                ),
                                "status": "paused",
                            }
                        # Paused worker — auto-resume and re-route the new query.
                        resume_paused_worker = existing
                    else:
                        return {
                            "error": (
                                f"Worker '{self.worker_name}' instance "
                                f"{explicit_iid} is already running"
                            ),
                            "status": existing.status,
                        }
                else:
                    spawn_iid = explicit_iid
            else:
                if len(live_same) == 1 and live_same[0][1].status == "paused":
                    if getattr(live_same[0][1], "_manual_only_pause", False):
                        return {
                            "error": (
                                f"Worker '{self.worker_name}' is paused "
                                "(manual-only) and will not be auto-resumed by "
                                "a query; resume it explicitly with the "
                                "'resume' action."
                            ),
                            "status": "paused",
                        }
                    resume_paused_worker = live_same[0][1]
                else:
                    spawn_iid = max([iid for iid, _ in live_same], default=0) + 1

        if resume_paused_worker is not None:
            # Resume the paused worker and send the new query.
            # We do this outside the registry lock to avoid holding it
            # while waiting for the output queue (which can take minutes).
            query = (
                self.context.get("query", "")
                if isinstance(self.context, dict)
                else str(self.context or "")
            )
            resume_paused_worker.resume()

            # Drain stale None signals left by pause() in the input queue.
            # If a real query was queued by someone else, keep it — but the
            # NEW spawn query runs FIRST (F4 FIFO): it is enqueued before the
            # stale item, which is re-queued at the tail and processed after.
            stale_query = None
            try:
                while True:
                    item = resume_paused_worker._input_queue.get_nowait()
                    if item is not None:
                        stale_query = item
                        break
            except queue.Empty:
                pass

            resume_paused_worker._input_queue.put(query)
            if stale_query is not None:
                resume_paused_worker._input_queue.put(stale_query)
            try:
                final_result = resume_paused_worker._output_queue.get(timeout=SPAWN_QUEUE_TIMEOUT)
            except queue.Empty:
                final_result = json.dumps({
                    "error": f"Worker '{self.worker_name}' did not respond within {SPAWN_QUEUE_TIMEOUT}s",
                    "note": "Worker timed out. The paused worker was resumed but did not produce a response.",
                })
            try:
                parsed = json.loads(final_result) if final_result else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {"response": final_result}
            if not isinstance(parsed, dict):
                parsed = {"response": str(parsed)}
            parsed.setdefault("worker_name", self.worker_name)
            parsed.setdefault("spawned", True)
            parsed.setdefault("instance_id", resume_paused_worker.instance_id)
            parsed.setdefault("instance_label", resume_paused_worker.instance_label)
            elapsed = resume_paused_worker._last_elapsed()
            if elapsed is not None:
                parsed["elapsed_seconds"] = round(elapsed, 1)
            return parsed

        # ── Session-level spawn cap ──
        # Count LIVE workers already running for this session and refuse the
        # spawn at/above the cap. Force-replacements never reach this point:
        # the force block above already stopped and popped the stale entry.
        max_workers = self._effective_max_workers()
        live_workers = self._live_workers_in_session(session_key)
        if live_workers >= max_workers:
            return {
                "error": (
                    f"Worker spawn limit reached: {live_workers}/{max_workers} "
                    f"workers already running for this session. Stop an existing "
                    f"worker before spawning '{self.worker_name}'."
                ),
                "max_workers": max_workers,
                "live_workers": live_workers,
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
        # ── Fail-closed spawn guard ────────────────────────────────────
        # The worker footprint may only RESTRICT categories the session
        # explicitly exposes.  A footprint that requests a category absent
        # from the session profile is rejected outright: the worker can never
        # grant itself access to a category the session does not expose.
        session_perms = self.session_permissions or {}
        denied_categories = sorted(set(worker_perms) - set(session_perms))
        if denied_categories:
            return {
                "error": (
                    "Cannot create worker: permission footprint requests "
                    "category/categories not exposed by the session: "
                    f"{', '.join(denied_categories)} (fail-closed \u2014 workers "
                    "may only use categories the session explicitly exposes)."
                ),
                "worker_name": self.worker_name,
            }
        # Effective worker profile: session (ceiling) intersected with the
        # worker footprint.  Used for spawn-time tool validation below.
        spawn_effective = _restrictive_merge(session_perms, worker_perms)
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
                        effective=spawn_effective,
                        tool_name=tool_name,
                        tool_args={},
                        description=(
                            f"Worker '{self.worker_name}' footprint validation"
                            f" for {tool_name}"
                        ),
                        # Footprint already intersected into spawn_effective
                        # via _restrictive_merge \u2014 pass None so the gate does
                        # not re-apply the raw footprint on top.
                        permission_footprint=None,
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

        # Build a ContainerManager bound to this session/workspace so the
        # worker can stop/remove the containers it owns at teardown. Falls
        # back to None when Docker/ContainerManager is unavailable — worker
        # teardown then skips container cleanup instead of crashing (mirrors
        # tools/container_control._make_manager).
        container_manager = None
        if _CM is not None:
            try:
                workspace_id = None
                try:
                    from thoughtmachine.workspace_capabilities import resolve_workspace_id
                    workspace_id = resolve_workspace_id(ws_dir)
                except Exception:
                    pass
                container_manager = _CM(
                    workspace_path=ws_dir,
                    session_id=self.session_id,
                    workspace_id=workspace_id or ws_id or "default",
                    session_permissions=self.session_permissions,
                )
            except Exception:
                container_manager = None

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
            instance_id=spawn_iid,
            container_manager=container_manager,
        )

        # Store initial context for the thread to pick up in run()
        if self.context is not None and isinstance(self.context, dict):
            thread._initial_context = self.context
        else:
            thread._initial_context = {}

        # Merge purpose, style, meta into initial context
        if self.purpose is not None:
            thread._initial_context["purpose"] = self.purpose
        if self.style is not None:
            thread._initial_context["style"] = self.style
        if self.meta is not None:
            thread._initial_context["meta"] = self.meta

        # Context is always preserved — context.json is never deleted on spawn.
        # If context.json exists from a previous run, it is loaded in run()
        # and _initial_context is merged into the loaded context rather than
        # replacing it. This ensures worker conversation state persists across
        # spawn/query boundaries.

        # ── Fix 1.1A: Clean up stale command.json before starting ──
        cmd_path = thread._worker_dir / "command.json"
        if cmd_path.exists():
            cmd_path.unlink(missing_ok=True)

        # ── Fix 1 (P0.1): Double-checked locking for spawn concurrency ──
        # Re-check under lock before registering to prevent TOCTOU race:
        # another thread may have spawned the same worker between the early
        # check (above) and this registration point.
        with _registry_lock:
            existing = _worker_registry.get((session_key, self.worker_name, spawn_iid))
            if existing is not None and existing.is_alive():
                # Another thread won the race — don't overwrite.
                return {
                    "error": (
                        f"Worker '{self.worker_name}' instance {spawn_iid} "
                        f"is already running"
                    ),
                    "status": existing.status,
                }
            # Re-check the spawn cap under the lock (TOCTOU guard): another
            # spawn may have registered between the early cap check and here.
            live_now = sum(
                1
                for key, t in _worker_registry.items()
                if key[0] == session_key and t.is_alive()
            )
            if live_now >= max_workers:
                return {
                    "error": (
                        f"Worker spawn limit reached: {live_now}/{max_workers} "
                        f"workers already running for this session. Stop an existing "
                        f"worker before spawning '{self.worker_name}'."
                    ),
                    "max_workers": max_workers,
                    "live_workers": live_now,
                }
            _worker_registry[(session_key, self.worker_name, spawn_iid)] = thread

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
                final_result = thread._output_queue.get(timeout=SPAWN_QUEUE_TIMEOUT)
            except queue.Empty:
                final_result = json.dumps({
                    "error": f"Worker '{self.worker_name}' did not respond "
                             f"within {SPAWN_QUEUE_TIMEOUT}s",
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
            parsed.setdefault("instance_id", thread.instance_id)
            parsed.setdefault("instance_label", thread.instance_label)
            elapsed = thread._last_elapsed()
            if elapsed is not None:
                parsed["elapsed_seconds"] = round(elapsed, 1)
            return parsed
        else:
            # No auto-query — return immediately; worker stays alive
            return {
                "worker_name": self.worker_name,
                "instance_id": thread.instance_id,
                "instance_label": thread.instance_label,
                "spawned": True,
                "status": "ready",
            }

    def _heartbeat_summary(self, hb_iso: Optional[str]):
        """Summarize a worker's heartbeat age for the check payload.

        Returns ``(age_seconds, stale)`` where ``age_seconds`` is rounded to
        one decimal (None when there is no/invalid heartbeat) and ``stale`` is
        True when the heartbeat is older than HEARTBEAT_STALE_AFTER_S.
        """
        if not hb_iso:
            return (None, False)
        try:
            hb_dt = datetime.fromisoformat(hb_iso)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            age = round((datetime.now(timezone.utc) - hb_dt).total_seconds(), 1)
            return (age, age > HEARTBEAT_STALE_AFTER_S)
        except (ValueError, TypeError):
            return (None, False)

    def _action_check(self, workers: list) -> dict:
        """Check on a specific worker by name (+ optional instance_id).

        Resolves live instances for the current session first; when no
        instance_id is given and none are live here, falls back to searching
        across all sessions and reports any foreign-session instances found.
        Name-only lookups against multiple live instances require an explicit
        instance_id.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        _get_worker_lifecycle_observer()  # best-effort: ensure observer subscription
        try:
            thread, sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            msg = str(exc)
            all_instances = []
            if self.instance_id is None and "is not running" in msg:
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

            if "ambiguous" in msg:
                # Name maps to multiple live instances in this session — the
                # caller must pass an explicit instance_id.
                return {
                    "error": msg,
                    "worker_name": self.worker_name,
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

        age, stale = self._heartbeat_summary(thread.last_heartbeat)
        return {
            "worker_name": self.worker_name,
            "instance_id": iid,
            "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
            "status": thread.status,
            "current_task": thread.current_task,
            "last_heartbeat": thread.last_heartbeat,
            "error": thread.error,
            "session_id": sid,
            "current_context_tokens": thread.get_current_context_tokens(),
            "max_context_tokens": thread.max_context_tokens,
            "alive": thread.is_alive(),
            "conversation_length": len(thread._worker_ctx.user_history) if thread._worker_ctx else 0,
            "last_heartbeat_age_s": age,
            "stale": stale,
        }

    def _wait_for_worker_exit(self, thread) -> bool:
        """Wait (bounded) for a cooperatively-stopped worker thread to exit.

        Used by the sync-query timeout path so ``_action_query`` never returns
        while the worker thread is still alive (e.g. stuck inside a
        DockerCodeRunner call): the caller must observe a terminal state, not
        an in-flight thread. Same join-retry pattern as ``_action_stop``
        (budget = max(30, thread timeout)); never Thread.kill. Returns True
        when the thread exited within the budget, False when the budget
        elapsed with the daemon thread still alive (the envelope then reports
        the degraded outcome and the thread is left to terminate on its own).
        """
        _join_budget = max(30, getattr(thread, "_timeout_seconds", 600) or 600)
        _join_elapsed = 0.0
        _join_step = 2.0
        while _join_elapsed < _join_budget:
            thread.join(timeout=_join_step)
            _join_elapsed += _join_step
            if not thread.is_alive():
                return True
            logger.debug(
                "Still waiting for worker '%s' to stop "
                "(%.0f/%ds elapsed)",
                self.worker_name, _join_elapsed, _join_budget,
            )
        if thread.is_alive():
            logger.warning(
                "Worker '%s' did not stop within %ds budget after query "
                "timeout; returning envelope with thread still running "
                "(daemon thread)",
                self.worker_name, _join_budget,
            )
            return False
        return True

    def _action_query(self, workers: list) -> dict:
        """Query a worker and wait for a response (synchronous, blocking)."""
        try:
            thread, _sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=self.session_id or _NO_SESSION_KEY,
            )
        except _WorkerAmbiguityError as exc:
            return {
                "error": str(exc),
                "worker_name": self.worker_name,
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

        if thread.status == "paused":
            # A paused worker must never be queried destructively (BUG 3):
            # the old behavior blocked up to 300s, then stopped the worker
            # and reclaimed its containers. Manual-only pauses reject the
            # query immediately (non-destructively); normal pauses are
            # auto-resumed and the query proceeds normally below.
            if getattr(thread, "_manual_only_pause", False):
                return {
                    "status": "paused",
                    "message": "Worker is manually paused",
                    "worker_name": self.worker_name,
                    "session_id": self.session_id or "",
                }
            try:
                thread.resume()
            except Exception:
                pass

        try:
            # Block until the worker responds
            response = thread.send_query(self.worker_query, timeout=300.0)
            elapsed = thread._last_elapsed()
            result = {
                "worker_name": self.worker_name,
                "instance_id": iid,
                "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                "response": response,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            }
            if thread._last_reasoning is not None:
                result["reasoning"] = thread._last_reasoning
            return result
        except TimeoutError as exc:
            # Cooperative stop: the query deadline elapsed with no reply. Stop
            # the worker thread so its run() loop drains, its terminal path
            # runs, and its containers are reclaimed. Never Thread.kill — the
            # thread always terminates via its own run() teardown (which calls
            # _cleanup_worker_containers). The stop is best-effort: if it
            # raises, run() teardown remains authoritative.
            try:
                thread.stop()
            except Exception:
                pass
            # Synchronous reclaim: the worker may be stuck inside a DockerCodeRunner
            # call, so run()'s finally (the safety net) may not run until that call
            # returns. Reclaim worker-owned containers NOW from the timeout path.
            # If the worker is mid-tool-call using the container, the stop/remove
            # will make that call fail and return — acceptable. No join() here; no
            # Thread.kill; never touches resource containers.
            try:
                thread._cleanup_worker_containers()
            except Exception:
                pass
            # ── Stop guarantee: bounded wait for actual thread exit ──
            # The sync call must not return while the worker thread is still
            # alive (it may be stuck inside a DockerCodeRunner call): the
            # caller would observe a terminal envelope while the thread is
            # still in flight. join() in retry steps (same pattern as
            # _action_stop), bounded by max(30, timeout_seconds). Never
            # Thread.kill — if the budget elapses the daemon thread is left to
            # terminate on its own and the envelope reports the degraded
            # outcome.
            clean_exit = self._wait_for_worker_exit(thread)
            stopped_note = (
                "Worker did not respond in time and was stopped cooperatively "
                "(its containers were reclaimed). Re-spawn the worker before "
                "querying it again."
            )
            degraded_note = (
                "Worker did not respond in time and was stopped cooperatively, "
                "but it did not exit within the stop budget and may still be "
                "terminating in the background. Re-spawn the worker before "
                "querying it again."
            )
            if not clean_exit:
                logger.warning(
                    "Worker '%s' still alive after query-timeout stop budget; "
                    "returning envelope with thread running",
                    self.worker_name,
                )
            # Heartbeat classification now only shapes the message content —
            # both branches terminate the worker, because a timed-out query is
            # final (the worker was stopped and its containers reclaimed).
            if thread.last_heartbeat:
                try:
                    hb_dt = datetime.fromisoformat(thread.last_heartbeat)
                    age_seconds = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                    if age_seconds > 600.0:  # 2× the 300s query timeout
                        _publish_global_worker_event(EventType.WORKER_TIMEOUT, {
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "status": thread.status,
                            "reason": "hung",
                            "last_heartbeat": thread.last_heartbeat,
                            "heartbeat_age_s": round(age_seconds, 1),
                            "current_context_tokens": thread.get_current_context_tokens(),
                            "max_context_tokens": thread.max_context_tokens,
                            **({"query_id": getattr(thread, "_current_query_id", None)}
                               if getattr(thread, "_current_query_id", None) is not None else {}),
                        })
                        return {
                            "error": f"Worker appears hung (last heartbeat: {thread.last_heartbeat}, {age_seconds:.0f}s ago)",
                            "worker_name": self.worker_name,
                            "status": thread.status,
                            "current_task": thread.current_task,
                            "note": degraded_note if not clean_exit else stopped_note,
                        }
                except (ValueError, TypeError):
                    pass  # Malformed heartbeat — fall through to generic timeout
            return {
                "error": str(exc),
                "note": degraded_note if not clean_exit else stopped_note,
                "worker_name": self.worker_name,
                "status": thread.status,
            }

    def _action_submit_query(self, workers: list) -> dict:
        """Submit a query without blocking (Phase 2B).

        Enqueues a (job_id, query, None) tuple: replies are NOT routed to a
        private queue, so they land on the shared _output_queue and are drained
        as stale by any later synchronous send_query call. Returns immediately
        with a job handle; progress/completion arrive via WORKER_* events and
        the job registry.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        try:
            thread, _sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            return {"error": str(exc), "worker_name": self.worker_name}
        if not thread.is_alive():
            return {
                "error": f"Worker '{self.worker_name}' is no longer alive (status: {thread.status}).",
                "status": thread.status,
                "error_detail": thread.error,
            }
        if not self.worker_query:
            return {"error": "worker_query is required for action 'submit_query'"}
        job_id = uuid.uuid4().hex
        try:
            thread._input_queue.put((job_id, self.worker_query, None))
        except Exception as exc:
            return {"error": f"Failed to enqueue query: {exc}", "worker_name": self.worker_name}
        registry = _get_worker_job_registry()
        if registry is not None:
            try:
                registry.register(
                    job_id,
                    self.worker_name,
                    self.session_id or "",
                    instance_id=iid,
                )
            except Exception:
                pass
        return {
            "worker_name": self.worker_name,
            "instance_id": iid,
            "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
            "job_id": job_id,
            "status": "submitted",
            "note": "Query submitted (non-blocking). Progress/completion arrive via "
                    "WORKER_PARTIAL_RESULT / WORKER_COMPLETED events; poll with "
                    "action='job_status'.",
        }

    def _action_job_status(self, workers: list) -> dict:
        """Read job records from the registry (non-blocking, Phase 2B)."""
        registry = _get_worker_job_registry()
        if registry is None:
            return {"error": "Worker job registry unavailable", "worker_name": self.worker_name}
        job_id = (self.worker_query or "").strip() or None
        if job_id:
            rec = registry.job(job_id)
            if rec is None:
                return {"worker_name": self.worker_name, "job_id": job_id, "error": "Job not found"}
            return {
                "worker_name": self.worker_name,
                "job_id": rec.get("job_id"),
                "status": rec.get("status"),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "completed_at": rec.get("completed_at"),
                "has_result": rec.get("result") is not None,
                "preview": rec.get("preview", ""),
            }
        jobs = [
            {
                "job_id": r.get("job_id"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "completed_at": r.get("completed_at"),
                "has_result": r.get("result") is not None,
                "preview": r.get("preview", ""),
            }
            for r in registry.jobs(
                worker_name=self.worker_name or None,
                instance_id=self.instance_id,
            )
        ]
        return {"worker_name": self.worker_name, "jobs": jobs, "count": len(jobs)}

    def _action_join(self, workers: list) -> dict:
        """Block until a submitted job reaches a terminal state (Phase 2B).

        Shared handler for both the ``join`` and ``wait_for_job`` actions.
        ``worker_query`` carries the job_id (same convention as ``job_status``).
        Polls the job registry in 0.25s slices for a total of ``timeout_seconds``
        (default 60, hard cap 300). Returns early on: job terminal state
        (completed / timeout / error), a partial result (status ``partial_result``
        + preview), the worker thread pausing (``status == 'paused'``), a stop
        signal (stop_event set / status stopping / stopped), or thread death.
        """
        _JOB_TERMINAL = ("completed", "timeout", "error")
        job_id = (self.worker_query or "").strip()
        if not job_id:
            return {
                "error": "worker_query is required for action 'join' (pass the job_id)",
                "worker_name": self.worker_name,
            }
        try:
            thread, _sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=self.session_id or _NO_SESSION_KEY,
            )
        except _WorkerAmbiguityError as exc:
            return {"error": str(exc), "worker_name": self.worker_name}
        registry = _get_worker_job_registry()
        if registry is None:
            return {"error": "Worker job registry unavailable", "worker_name": self.worker_name}
        rec = registry.job(job_id)
        if rec is None:
            return {
                "error": "Job not found",
                "job_id": job_id,
                "worker_name": self.worker_name,
            }
        total = min(int(self.timeout_seconds or 60), 300)
        deadline = time.monotonic() + total
        last_status = rec.get("status")
        while True:
            rec = registry.job(job_id)
            status = rec.get("status") if rec is not None else None
            if status is not None:
                last_status = status
            if rec is not None and status in _JOB_TERMINAL:
                out = {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": status,
                    "preview": rec.get("preview", ""),
                    "has_result": rec.get("result") is not None,
                    "completed_at": rec.get("completed_at"),
                }
                if rec.get("result") is not None:
                    out["result"] = rec.get("result")
                return out
            if rec is not None and status == "partial" and rec.get("preview"):
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "partial_result",
                    "preview": rec.get("preview"),
                }
            # Session main-pause (UNIT C): the main agent is paused, so a
            # blocking wait is pointless — return paused promptly. Pause
            # propagates INTO join waits this way.
            if self.session_id and self.session_id in _SESSION_MAIN_PAUSED:
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "paused",
                    "note": "Session main agent is paused; job remains pending.",
                }
            # Worker-side wake conditions.
            if getattr(thread, "status", None) == "paused":
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "paused",
                    "note": "Worker is paused; job remains pending.",
                }
            stop_event = getattr(thread, "_stop_event", None)
            if (stop_event is not None and stop_event.is_set()) or \
                    getattr(thread, "status", None) in ("stopping", "stopped"):
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "stopped",
                    "note": "Worker is stopping/stopped; job did not reach a terminal state.",
                }
            if not thread.is_alive():
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "stopped",
                    "note": "Worker thread is no longer alive; job did not reach a terminal state.",
                }
            if time.monotonic() >= deadline:
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "timeout",
                    "note": f"Timed out after {total}s waiting for job; last known status: {last_status}.",
                }
            time.sleep(0.25)

    def _action_pause(self, workers: list) -> dict:
        """Pause the session's main agent and/or its workers (UNIT C, v2).

        Cooperative only: workers pause at their next safe loop boundary
        (``WorkerThread.pause()`` sets the pause event; the thread flips its own
        ``status`` to ``'paused'``). No thread is killed; a paused idle worker
        stays alive until explicitly stopped or resumed.

        ``worker_query`` is a comma/space-separated mode string:
          - (empty) or ``main`` \u2192 pause the main agent only. Async workers
            keep running; ``join``/``wait_for_job`` return
            ``{"status": "paused"}`` while the main agent is paused.
          - ``all`` \u2192 also cooperatively pause every live worker of this
            session (Pause All).
          - ``manual_only`` \u2192 the workers paused by this call are marked
            manual-only: a later query will NOT auto-resume them; they must be
            resumed explicitly with the ``resume`` action.
        When ``worker_name`` is set, only that worker is paused (the session
        main agent is not paused unless ``main`` is also present).
        """
        session_key = self.session_id or _NO_SESSION_KEY
        spec = (self.worker_query or "").strip().lower()
        tokens = {t for t in spec.replace(",", " ").split() if t}
        want_all = "all" in tokens
        want_main = (not tokens) or ("main" in tokens)
        manual_only = "manual_only" in tokens
        if self.worker_name:
            want_all = True
            want_main = False
        if want_main:
            _SESSION_MAIN_PAUSED.add(session_key)
        paused_workers = []
        with _registry_lock:
            for key, thread in list(_worker_registry.items()):
                if key[0] != session_key:
                    continue
                if self.worker_name is not None and key[1] != self.worker_name:
                    continue
                alive = bool(getattr(thread, "is_alive", lambda: True)())
                if not alive:
                    continue
                if not want_all:
                    continue
                if manual_only:
                    try:
                        thread._manual_only_pause = True
                    except Exception:
                        pass
                try:
                    thread.pause()
                except Exception:
                    pass
                paused_workers.append({
                    "worker_name": key[1],
                    "instance_id": key[2] if len(key) >= 3 else 1,
                })
        if want_main and not want_all:
            note = "Main agent paused; async workers continue."
        elif want_all:
            note = "Main agent and session workers paused (cooperative)."
        else:
            note = "Requested worker(s) paused."
        return {
            "status": "paused",
            "session_id": self.session_id,
            "scope": "all" if want_all else "main",
            "main_agent_paused": want_main,
            "manual_only": manual_only,
            "workers_paused": paused_workers,
            "note": note,
        }

    def _action_resume(self, workers: list) -> dict:
        """Resume a session main-agent and/or worker pause (UNIT C, v2).

        Mirrors ``_action_pause``: clears the session from the main-pause set
        and (with ``all`` in ``worker_query`` or a ``worker_name``) resumes the
        matching paused workers cooperatively via ``WorkerThread.resume()``,
        clearing their manual-only marker.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        spec = (self.worker_query or "").strip().lower()
        tokens = {t for t in spec.replace(",", " ").split() if t}
        want_all = "all" in tokens
        want_main = (not tokens) or ("main" in tokens)
        if self.worker_name:
            want_all = True
            want_main = False
        if want_main:
            _SESSION_MAIN_PAUSED.discard(session_key)
        resumed_workers = []
        with _registry_lock:
            for key, thread in list(_worker_registry.items()):
                if key[0] != session_key:
                    continue
                if self.worker_name is not None and key[1] != self.worker_name:
                    continue
                if getattr(thread, "status", None) != "paused":
                    continue
                if not want_all:
                    continue
                try:
                    thread.resume()
                except Exception:
                    pass
                try:
                    thread._manual_only_pause = False
                except Exception:
                    pass
                resumed_workers.append({
                    "worker_name": key[1],
                    "instance_id": key[2] if len(key) >= 3 else 1,
                })
        return {
            "status": "resumed",
            "session_id": self.session_id,
            "scope": "all" if want_all else "main",
            "main_agent_paused": want_main and session_key in _SESSION_MAIN_PAUSED,
            "workers_resumed": resumed_workers,
            "note": "Main agent resumed." if want_main else "Requested worker(s) resumed.",
        }

    def _action_stop(self, workers: list) -> dict:
        """Stop a running worker and persist its context.

        First tries the current session; if not found, searches across all
        sessions and stops any matching instances.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        try:
            thread, sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            msg = str(exc)
            all_instances = []
            if self.instance_id is None and "is not running" in msg:
                # ── Not found in current session → search across all sessions ──
                all_instances = self._find_all_worker_threads(self.worker_name)
            if "ambiguous" in msg:
                # Name maps to multiple live instances — the caller must pass
                # an explicit instance_id to disambiguate.
                return {
                    "error": msg,
                    "worker_name": self.worker_name,
                }
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
                    if t._worker_ctx is not None:
                        t._worker_ctx.compact_after_summary()
                    t._save_context()
                    t._cleanup_worker_containers()
                    with _registry_lock:
                        for key, existing_t in list(_worker_registry.items()):
                            if (
                                key[0] == sid
                                and key[1] == self.worker_name
                                and existing_t is t
                            ):
                                _worker_registry.pop(key, None)
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
                _worker_registry.pop((sid, self.worker_name, iid), None)
            if thread._worker_ctx is not None:
                thread._worker_ctx.compact_after_summary()
            thread._save_context()
            thread._cleanup_worker_containers()
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
            if thread._worker_ctx is not None:
                thread._worker_ctx.compact_after_summary()
            thread._save_context()
            thread._cleanup_worker_containers()
            # Terminal status: a stopped worker ends as 'stopped' (not
            # 'completed'). Write the status file before removing from the
            # registry so the terminal state remains observable.
            if thread.status != "error":
                thread.status = "stopped"
                thread._write_status_file()
            with _registry_lock:
                _worker_registry.pop((sid, self.worker_name, iid), None)

        return {
            "worker_name": self.worker_name,
            "status": "stopped",
            "message": f"Worker '{self.worker_name}' stopped successfully.",
        }
