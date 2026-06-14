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

# Optional: LLM client for worker conversations
try:
    from agent.core.llm_client import LLMClient
except ImportError:
    LLMClient = None  # type: ignore

# Optional: security gate for worker permission checks
try:
    from security.security_gate import check_required_categories
    GATE_AVAILABLE = True
except ImportError:
    GATE_AVAILABLE = False
    check_required_categories = None  # type: ignore

# Gate module already imported above — no secondary import needed.


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
    idle  ──spawn──▶  running  ──response──▶  running  ──stop──▶  completed
                        │                                              │
                        └──error──▶  failed                            │
                                                    ◄──  idle on restart
    """

    def __init__(
        self,
        name: str,
        definition: dict,
        llm_client: Any,
        workspace_dir: Path,
        tool_classes: Optional[Dict[str, type[ToolBase]]] = None,
        session_permissions: Optional[Dict[str, Any]] = None,
        project_root: Optional[str] = None,
    ) -> None:
        super().__init__(daemon=True, name=f"worker-{name}")
        self.worker_name = name
        self.definition = definition
        self.llm_client = llm_client
        self._worker_dir = workspace_dir / "workers" / name
        self._worker_dir.mkdir(parents=True, exist_ok=True)

        # Tool classes available to this worker (name -> class)
        self._tool_classes: Dict[str, type[ToolBase]] = tool_classes or {}
        # Tool definitions in OpenAI format (built once)
        self._tool_defs: List[Dict[str, Any]] = []
        self._rebuild_tool_defs()

        # Session permissions for gate-checking tool calls
        self._session_permissions: Dict[str, Any] = session_permissions or {}
        # Worker-level permission footprint from definition
        self._worker_permissions: Dict[str, Any] = definition.get("worker_permissions", {})

        # Project root from the session (resolved from workspace config)
        self._project_root: Optional[str] = project_root

        # Runtime state
        self.status: str = "idle"      # idle | running | completed | failed
        self.current_task: Optional[str] = None
        self.error: Optional[str] = None
        self.last_heartbeat: Optional[str] = None

        # Conversation context (loaded from disk on init)
        self.conversation: List[Dict[str, str]] = []
        self._load_context()

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
        # Unblock the input queue wait
        self._input_queue.put(None)

    def _rebuild_tool_defs(self) -> None:
        """Build OpenAI-style tool definitions from self._tool_classes."""
        defs = []
        for name, cls in self._tool_classes.items():
            try:
                defs.append(model_to_openai_tool(cls))
            except Exception as exc:
                logger.warning(
                    "Failed to build tool def for '%s': %s", name, exc
                )
        self._tool_defs = defs

    def _check_tool_permissions(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
    ) -> Optional[str]:
        """
        Check whether this tool call is allowed by worker + session permissions.

        Routes through ``check_required_categories`` — the single gate
        enforcement point for all tool calls.  Returns ``None`` if allowed,
        or an error message string if denied.
        """
        tool_cls = self._tool_classes.get(tool_name)
        if tool_cls is None:
            return f"Unknown tool: {tool_name}"

        # Get the tool's required categories
        required = tool_cls.get_required_categories(tool_args)
        if not required:
            return None  # no categories needed = always allowed

        # Use session permissions as the effective base;
        # check_required_categories applies worker_permissions internally
        # via _min_permission (more restrictive of the two).
        effective = dict(self._session_permissions)

        if GATE_AVAILABLE and check_required_categories is not None:
            ok, error_msg = check_required_categories(
                required,
                effective,
                tool_name=tool_name,
                tool_args=tool_args,
                description=f"Worker '{self.worker_name}' executing {tool_name}",
                event_bus=None,  # No user to prompt in worker context
                worker_permissions=self._worker_permissions,
            )
            if not ok:
                return error_msg
            return None
        else:
            # Gate not available: deny all (safe default for worker context)
            return (
                f"Permission denied: Gate unavailable for '{tool_name}'. "
                f"Required: {', '.join(required)}"
            )

    def _execute_tool(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
    ) -> str:
        """
        Instantiate and execute a tool by name.

        Returns the tool's output string (JSON).
        """
        tool_cls = self._tool_classes.get(tool_name)
        if tool_cls is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        try:
            tool_instance = tool_cls(**tool_args)
        except Exception as exc:
            return json.dumps({
                "error": f"Failed to instantiate tool '{tool_name}': {exc}",
            })

        # Inject the workspace_path if the tool needs it
        # Use the session's project root so tools can resolve files relative
        # to the project (e.g., secret_alpha.txt -> <project_root>/secret_alpha.txt).
        # Fall back to the worker's own directory only if project_root is unknown.
        if self._project_root:
            tool_instance.workspace_path = self._project_root
        elif self._worker_dir:
            tool_instance.workspace_path = str(self._worker_dir)

        try:
            result = tool_instance.execute()
            return result
        except Exception as exc:
            return json.dumps({
                "error": f"Tool '{tool_name}' execution failed: {exc}",
            })

    def _run_tool_loop(
        self,
        query: str,
    ) -> str:
        """
        Run the tool-use conversation loop for a single query.

        Steps:
          1. Append user query to conversation.
          2. Call LLM with tool definitions.
          3. If response has tool_calls → validate, gate-check, execute, append, loop.
          4. If text response → return it.
          5. On error → return error JSON.
        """
        self.conversation.append({"role": "user", "content": query})

        max_tool_turns = 10  # safety limit to prevent infinite loops
        tool_turn = 0

        while tool_turn < max_tool_turns and not self._stop_event.is_set():
            tool_turn += 1
            self.last_heartbeat = datetime.now(timezone.utc).isoformat()

            try:
                response = self.llm_client.chat_completion(
                    self.conversation,
                    tools=self._tool_defs if self._tool_defs else None,
                )
            except Exception as exc:
                logger.exception("Worker LLM call failed")
                return json.dumps({"error": f"LLM call failed: {exc}"})

            content = response.content or ""
            tool_calls = getattr(response, "tool_calls", None)

            if not tool_calls:
                # Text response — this is the final answer
                self.conversation.append({
                    "role": "assistant",
                    "content": content,
                })
                return content

            # ── Tool call(s) received ─────────────────────────────────
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            }
            self.conversation.append(assistant_msg)

            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "")

                # Parse arguments
                try:
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    tool_args = {}

                tool_call_id = tc.get("id", "")

                # 1. Validate the tool exists
                if tool_name not in self._tool_classes:
                    error_result = json.dumps({
                        "error": f"Unknown tool: '{tool_name}'. "
                                  f"Available tools: {list(self._tool_classes.keys())}",
                    })
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": error_result,
                    })
                    continue

                # 2. Gate check
                gate_error = self._check_tool_permissions(tool_name, tool_args)
                if gate_error is not None:
                    error_result = json.dumps({"error": gate_error})
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": error_result,
                    })
                    continue

                # 3. Execute the tool
                self.current_task = f"Running {tool_name}..."
                result = self._execute_tool(tool_name, tool_args)
                self.current_task = None

                self.conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                })

                # Log the tool event
                self._log_event(
                    "tool_call",
                    {"tool": tool_name, "args": tool_args},
                    {"result": result[:500] if len(result) > 500 else result},
                )

            # Save context after each tool turn
            self._save_context()

            # Loop back for the next LLM response

        if tool_turn >= max_tool_turns:
            return json.dumps({
                "error": f"Worker exceeded maximum tool turns ({max_tool_turns}).",
            })

        return ""  # unreachable, but keep the type checker happy

    # ── thread run loop ────────────────────────────────────────────

    def run(self) -> None:
        """Main worker loop — processes queries until stop is signalled."""
        self.status = "running"
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()

        try:
            # Load system prompt from definition (or use default)
            system_prompt = self.definition.get(
                "system_prompt",
                "You are a helpful worker assistant."
            )
            # Ensure system prompt is first
            if not self.conversation or self.conversation[0].get("role") != "system":
                self.conversation.insert(0, {"role": "system", "content": system_prompt})

            self._save_context()
            self._log_event("started", {}, {})

            while not self._stop_event.is_set():
                # Wait for the next query (blocking)
                query = self._input_queue.get()
                if query is None or self._stop_event.is_set():
                    break

                self.current_task = query[:200]  # truncate display

                # Run the tool-use loop (or plain LLM if no tools configured)
                if self._tool_classes:
                    reply = self._run_tool_loop(query)
                else:
                    # Simple mode: single LLM call, no tools
                    self.conversation.append({"role": "user", "content": query})
                    response = self.llm_client.chat_completion(self.conversation)
                    reply = response.content if response and response.content else ""
                    self.conversation.append({"role": "assistant", "content": reply})

                self.current_task = None
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                # Persist and log
                self._save_context()
                self._log_event("query", query, reply)

                # Send the response back to the waiting tool call
                self._output_queue.put(reply)

        except Exception as exc:
            logger.exception("Worker thread %s failed", self.worker_name)
            self.status = "failed"
            self.error = str(exc)
            # Put the error into the output queue so any waiting query call gets it
            error_json = json.dumps({"error": str(exc)})
            try:
                self._output_queue.put_nowait(error_json)
            except queue.Full:
                pass
            self._log_event("error", {}, {"error": str(exc)})
        else:
            self.status = "completed"
            self._log_event("completed", {}, {})
        finally:
            self._save_context()

    # ── persistence ────────────────────────────────────────────────

    def _context_path(self) -> Path:
        return self._worker_dir / "context.json"

    def _events_path(self) -> Path:
        return self._worker_dir.parent / "events.jsonl"

    def _load_context(self) -> None:
        """Load conversation context from disk, if present."""
        path = self._context_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.conversation = data.get("conversation", [])
                self.status = data.get("status", "idle")
                self.error = data.get("error")
                self.last_heartbeat = data.get("last_heartbeat")
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to load worker context %s: %s", path, exc
                )

    def _save_context(self) -> None:
        """
        Persist conversation context to disk atomically.

        Uses a temp-file + os.replace pattern (same as
        ``FileSystemSessionStore.save_session``) so that a crash
        mid-write never leaves a truncated ``context.json``.
        """
        data = {
            "worker_name": self.worker_name,
            "status": self.status,
            "error": self.error,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "conversation": self.conversation,
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

    action: str = Field(description="Action: list, spawn, check, query")

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

    skip_output_truncation: ClassVar[bool] = True

    VALID_ACTIONS: ClassVar[list[str]] = ["list", "spawn", "check", "query"]

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

    def _build_llm_client(self) -> Any:
        """
        Build an LLMClient from the injected ``agent_config`` dict.

        Returns ``None`` if LLMClient is not importable or config is
        missing required keys.
        """
        if LLMClient is None:
            logger.warning("LLMClient not available — cannot create worker LLM client")
            return None

        cfg = self.agent_config or {}
        provider_type = cfg.get("provider")
        model = cfg.get("model")
        if not provider_type or not model:
            logger.warning(
                "agent_config missing provider (%s) or model (%s)",
                provider_type, model,
            )
            return None

        # Build a minimal object with the attributes LLMClient expects
        class _ConfigProxy:
            """Minimal config proxy with the attributes LLMClient reads."""
            def __init__(self, d: dict):
                self.provider_type = d.get("provider", "")
                self.api_key = d.get("api_key", "")
                self.base_url = d.get("base_url", None)
                self.model = d.get("model", "")
                self.temperature = d.get("temperature", 0.7)
                self.system_prompt = d.get("system_prompt", None)

        proxy = _ConfigProxy(cfg)
        return LLMClient(config=proxy)

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
            event_bus=None,
            worker_permissions=worker_perms,
        )

        if not ok:
            return error_msg
        return None

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

        # Build LLM client for this worker
        llm_client = self._build_llm_client()
        if llm_client is None:
            return {
                "error": "Cannot create worker: LLM client unavailable. "
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
                        event_bus=None,
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

        # Create and start the worker thread
        thread = WorkerThread(
            name=self.worker_name,
            definition=definition,
            llm_client=llm_client,
            workspace_dir=ws_dir,
            tool_classes=tool_classes if tool_classes else None,
            session_permissions=self.session_permissions,
            project_root=project_root,
        )

        # Pass initial context if provided
        if self.context is not None:
            if isinstance(self.context, dict):
                thread.conversation.append({
                    "role": "system",
                    "content": f"Initial context: {json.dumps(self.context, default=str)}",
                })

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
            "conversation_length": len(thread.conversation),
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
            return {
                "worker_name": self.worker_name,
                "response": response,
            }
        except TimeoutError as exc:
            return {
                "error": str(exc),
                "worker_name": self.worker_name,
            }
