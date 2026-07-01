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
from typing import Any, ClassVar, Dict, List, Optional
from pydantic import Field

from tools.base import ToolBase
from tools.utils import model_to_openai_tool

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


logger = logging.getLogger(__name__)

# Tools excluded from workers for safety reasons
# Workers could spawn other workers (recursion), manage containers, or
# modify workspace infrastructure — all operations reserved for the
# main agent / human user.
_WORKER_BLOCKLIST: frozenset[str] = frozenset({
    "Worker",           # recursion: worker spawning workers
    "DockerCodeRunner",  # container execution
    "EditDockerfile",    # container configuration
    "MCPValidator",      # MCP server management
})

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
        names = list(_worker_registry.keys())
    for name in names:
        with _registry_lock:
            thread = _worker_registry.get(name)
        if thread is None or not thread.is_alive():
            continue
        logger.info("Shutting down worker '%s' (status=%s)", name, thread.status)
        try:
            thread.stop()
            thread.join(timeout=timeout)
        except Exception:
            logger.exception("Error joining worker '%s' during shutdown", name)
        finally:
            try:
                thread._save_context()
            except Exception:
                logger.exception("Error saving context for worker '%s' during shutdown", name)


# Register atexit handler
atexit.register(shutdown_workers)


# ---------------------------------------------------------------------------
# Module-level worker registry  (persists across tool calls)
# ---------------------------------------------------------------------------

_worker_registry: Dict[str, "WorkerThread"] = {}
_registry_lock = threading.Lock()


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
      ▲                    │                                  │
      │                    ├──stop──▶  completed               │
      │                    └──error──▶  error                  │
      └───────────────────────────  spawn again  ◄─────────────┘
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
    ) -> None:
        super().__init__(daemon=True, name=f"worker-{name}")
        self.worker_name = name
        self.definition = definition
        self._agent_config_dict = agent_config
        self._worker_dir = workspace_dir / "workers" / name
        self._worker_dir.mkdir(parents=True, exist_ok=True)

        # Tool classes available to this worker (name -> class)
        self._tool_classes: Dict[str, type[ToolBase]] = tool_classes or {}

        # Session permissions for gate-checking tool calls
        self._session_permissions: Dict[str, Any] = session_permissions or {}
        # Worker-level permission footprint from definition
        self._worker_permissions: Dict[str, Any] = definition.get("worker_permissions", {})

        # Project root from the session (resolved from workspace config)
        self._project_root: Optional[str] = project_root

        # Override timeout (from spawn parameter, else from definition, else 600)
        self._timeout_seconds: int = (
            timeout_seconds
            if timeout_seconds is not None
            else definition.get("timeout_seconds", 600)
        )

        # Runtime state
        self.status: str = "ready"      # ready | busy | completed | error
        self.current_task: Optional[str] = None
        self.error: Optional[str] = None
        self.last_heartbeat: Optional[str] = None
        self._last_reasoning: Optional[str] = None

        # Agent instance + WorkerContext (created lazily in run())
        self._agent: Optional[Any] = None
        self._worker_ctx: Optional[Any] = None
        self._initial_context: Optional[Dict[str, Any]] = None

        # Inter-thread communication
        self._input_queue: queue.Queue = queue.Queue()
        self._output_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()

    # ── public API called from the tool thread ─────────────────────

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        """Send a query to this worker and block for a response."""
        self._input_queue.put(query)
        try:
            response = self._output_queue.get(timeout=timeout)
            return response
        except queue.Empty:
            raise TimeoutError(
                f"Worker '{self.worker_name}' did not respond within {timeout}s"
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
            if data.get("action") == "stop":
                cmd_path.unlink(missing_ok=True)
                self._stop_event.set()
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
            cfg.get("system_prompt", "You are a helpful worker assistant."),
        )
        worker_cfg["enabled_tools"] = (
            list(self._tool_classes.keys()) if self._tool_classes else []
        )
        worker_cfg["max_turns"] = self.definition.get(
            "max_turns", cfg.get("max_turns", 100)
        )
        worker_cfg["timeout_seconds"] = self._timeout_seconds
        # Warn at 80% of timeout (minimum 5s) so CRITICAL triggers before
        # the hard cutoff when using the worker tool's timeout window.
        worker_cfg["time_warning_threshold"] = max(
            5, int(self._timeout_seconds * 0.8)
        )
        worker_cfg["time_monitor_enabled"] = True
        worker_cfg["stop_check"] = self._stop_event.is_set

        # ── Inject session permissions ─────────────────────────────────
        worker_cfg["session_permissions"] = self._session_permissions

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

        # Log the user query as a user_message event
        self._log_event(
            "user_message",
            {"query": query[:500]},
            {},
        )

        try:
            for event in self._agent.process_query(query):
                # Fix 1.1B: Poll for stop command on every event
                self._poll_command()

                # Log heartbeat for liveliness checks
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                # Check stop signal
                if self._stop_event.is_set():
                    self._agent.request_pause()
                    final_content = json.dumps({
                        "status": "stopped",
                        "message": "Worker stopped by user",
                    })
                    break

                event_type = event.get("type", "")

                # Capture final response content and reasoning
                if event_type == "agent_responded":
                    final_content = event.get("content", "")
                    self._last_reasoning = event.get("reasoning")
                    # Log final_response event
                    self._log_event(
                        "final_response",
                        {},
                        {
                            "content": str(final_content)[:1000],
                            "reasoning": bool(self._last_reasoning),
                            "response_type": event.get("response_type", "answer"),
                        },
                    )

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

                # --- Rich event logging for worker output panel ---

                # Log tool calls (agent yields these before executing each tool)
                if event_type == "tool_call":
                    tool_name = event.get("tool_name", "")
                    arguments = event.get("arguments", {})
                    self._log_event(
                        "tool_call",
                        {"tool": tool_name, "args": arguments},
                        {},
                    )

                # Log tool results (agent yields these after each tool completes)
                if event_type == "tool_result":
                    tool_name = event.get("tool_name", "")
                    result = event.get("result", "")
                    success = event.get("success", True)
                    error = event.get("error")
                    self._log_event(
                        "tool_result",
                        {"tool": tool_name, "success": success, "error": error},
                        {"result": str(result)[:1000] if result else ""},
                    )

                # Log token warnings as system notifications
                if event_type == "token_warning":
                    message = event.get("message", "") or event.get("warning_message", "")
                    token_count = event.get("token_count", 0)
                    self._log_event(
                        "system_notification",
                        {},
                        {
                            "type": "token_warning",
                            "message": str(message)[:500],
                            "token_count": token_count,
                        },
                    )

                # Log turn warnings as system notifications
                if event_type == "turn_warning":
                    message = event.get("message", "") or event.get("warning", "")
                    turn_count = event.get("turn_count", 0)
                    self._log_event(
                        "system_notification",
                        {},
                        {
                            "type": "turn_warning",
                            "message": str(message)[:500],
                            "turn_count": turn_count,
                        },
                    )

                # Log time warnings as system notifications
                if event_type == "time_warning":
                    message = event.get("message", "") or event.get("warning_message", "")
                    elapsed = event.get("elapsed_seconds", 0)
                    self._log_event(
                        "system_notification",
                        {},
                        {
                            "type": "time_warning",
                            "message": str(message)[:500],
                            "elapsed_seconds": elapsed,
                        },
                    )

                # Keep legacy tool_execution handler for backward compatibility
                if event_type == "tool_execution":
                    tool_name = event.get("tool_name", "")
                    tool_args = event.get("tool_args", {})
                    result = event.get("result", "")
                    self._log_event(
                        "tool_call",
                        {"tool": tool_name, "args": tool_args},
                        {"result": str(result)[:500] if result else ""},
                    )

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
                self._worker_ctx = WorkerContext(
                    worker_name=self.worker_name,
                    user_history=user_history,
                )
            self._save_context()
            self._log_event("started", {}, {})

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
                    self._agent = Agent(
                        config=agent_cfg,
                        session=self._worker_ctx,
                    )

                # ── Fix 1.2: Set busy before processing query ───────────
                self.status = "busy"
                self._write_status_file()
                reply = self._run_tool_loop(query)
                # Back to ready after query completes
                self.status = "ready"
                self._write_status_file()

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
            self._log_event("error", {}, {"error": str(exc)})
        else:
            self.status = "completed"
            self._write_status_file()
            self._log_event("completed", {}, {})
        finally:
            if self._worker_ctx is not None:
                self._save_context()

    # ── persistence ────────────────────────────────────────────────

    def _context_path(self) -> Path:
        return self._worker_dir / "context.json"

    def _events_path(self) -> Path:
        return self._worker_dir.parent / "events.jsonl"

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
        }
        target = self._worker_dir / "status.json"
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

    def _log_event(self, event_type: str, request: Any, response: Any) -> None:
        """Append a timestamped event to the workspace events.jsonl."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "worker_name": self.worker_name,
            "event": event_type,
            "request": request,
            "response": response,
        }
        events_path = self._events_path()
        try:
            with open(events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            logger.error("Failed to log worker event: %s", exc)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class Worker(ToolBase):
    """Manage background or child worker processes."""

    tool: str = "Worker"
    required_categories: ClassVar[List[str]] = ["execution:read"]

    action: str = Field(description="Action: list, spawn, check, query, stop")

    worker_name: Optional[str] = Field(
        default=None,
        description="Name of the worker",
    )

    worker_query: Optional[str] = Field(
        default=None,
        description="Query to send to worker",
    )

    context: Optional[Dict] = Field(
        default=None,
        description="Optional context passed on spawn",
    )

    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Override the worker's default timeout in seconds. "
                      "If not set, the worker definition's timeout is used.",
    )

    skip_output_truncation: ClassVar[bool] = True

    VALID_ACTIONS: ClassVar[list[str]] = ["list", "spawn", "check", "query", "stop"]

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
        """Load workers list from workers.json in workspace dir."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir or not ws_id:
            return []

        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            logger.warning(f"workers.json not found at {workers_path}")
            return []

        try:
            return json.loads(workers_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load workers.json: {e}")
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

        worker_perms = definition.get("worker_permissions", {})

        ok, error_msg = check_required_categories(
            required=required,
            effective=session_permissions or {},
            tool_name="Worker",
            tool_args={"action": self.action, "worker_name": self.worker_name},
            description=f"Spawn worker '{self.worker_name}'",
            event_bus=_NULL_EVENT_BUS,
            worker_permissions=worker_perms,
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
        """
        cfg = self.agent_config or {}
        return dict(cfg)

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

    # -- action implementations --------------------------------------

    def _action_list(self, workers: list) -> dict:
        """Return all known worker definitions plus runtime status."""
        augmented = []
        for w in workers:
            name = w.get("name", "")
            entry = dict(w)
            # Merge runtime status from registry
            with _registry_lock:
                thread = _worker_registry.get(name)
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

        # Prevent duplicate spawns
        with _registry_lock:
            existing = _worker_registry.get(self.worker_name)
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
        worker_perms = definition.get("worker_permissions", {})
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
                        effective={},
                        tool_name=tool_name,
                        tool_args={},
                        description=(
                            f"Worker '{self.worker_name}' footprint validation"
                            f" for {tool_name}"
                        ),
                        event_bus=_NULL_EVENT_BUS,
                        worker_permissions=worker_perms,
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
        )

        # Store initial context for the thread to pick up in run()
        if self.context is not None and isinstance(self.context, dict):
            thread._initial_context = self.context

        # ── Fix 1.1A: Clean up stale command.json before starting ──
        cmd_path = thread._worker_dir / "command.json"
        if cmd_path.exists():
            cmd_path.unlink(missing_ok=True)
        
        # ── Preserve persisted context for resume across sessions ──
        # WorkerThread.run() will call _load_context() which reads
        # context.json from disk. If it exists (from a previous session
        # or a completed run), the thread resumes that conversation.
        # If it doesn't exist, the thread creates a fresh context.
        #
        # To force a clean start, delete the worker's directory
        # via the filesystem tool before spawning.

        with _registry_lock:
            _worker_registry[self.worker_name] = thread

        thread.start()

        result: dict[str, Any] = {
            "spawned": True,
            "worker_name": self.worker_name,
            "status": thread.status,
            "message": f"Worker '{self.worker_name}' started.",
        }
        if missing_tools:
            result["missing_tools"] = missing_tools
            result["message"] += (
                f" Warning: tool(s) not available: {', '.join(missing_tools)}."
            )
            logger.warning(
                "Worker '%s' spawned but tool(s) not found: %s",
                self.worker_name, missing_tools,
            )
        return result

    def _action_check(self, workers: list) -> dict:
        """Check on a specific worker by name."""
        with _registry_lock:
            thread = _worker_registry.get(self.worker_name)

        if thread is None:
            # Worker exists in definition but was never spawned
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
            "alive": thread.is_alive(),
            "conversation_length": len(thread._worker_ctx.user_history) if thread._worker_ctx else 0,
        }

    def _action_query(self, workers: list) -> dict:
        """Query a worker and wait for a response (synchronous, blocking)."""
        with _registry_lock:
            thread = _worker_registry.get(self.worker_name)

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
            }
            if elapsed is not None:
                result["elapsed_seconds"] = round(elapsed, 1)
            if thread._last_reasoning is not None:
                result["reasoning"] = thread._last_reasoning
            return result
        except TimeoutError as exc:
            return {
                "error": str(exc),
                "worker_name": self.worker_name,
            }

    def _action_stop(self, workers: list) -> dict:
        """Stop a running worker and persist its context."""
        with _registry_lock:
            thread = _worker_registry.get(self.worker_name)

        if thread is None:
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

        if not thread.is_alive():
            with _registry_lock:
                _worker_registry.pop(self.worker_name, None)
            thread._save_context()
            return {
                "worker_name": self.worker_name,
                "status": thread.status,
                "message": f"Worker '{self.worker_name}' was already stopped.",
            }

        try:
            thread.stop()
            thread.join(timeout=10.0)
        except Exception as exc:
            logger.exception("Error joining worker '%s' during stop", self.worker_name)
            return {
                "error": f"Error stopping worker '{self.worker_name}': {exc}",
            }
        finally:
            thread._save_context()
            with _registry_lock:
                _worker_registry.pop(self.worker_name, None)

        return {
            "worker_name": self.worker_name,
            "status": "stopped",
            "message": f"Worker '{self.worker_name}' stopped successfully.",
        }
