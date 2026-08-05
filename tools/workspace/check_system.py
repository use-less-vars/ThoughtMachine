# tools/workspace/check_system.py
"""
CheckSystem — inspect the runtime environment and report diagnostics.

Queries
-------
effective_permissions:
    Return the merged session permissions + workspace capabilities dict.

container_status:
    Return Docker container status for the current workspace.

workspace_info:
    Return workspace ID, capabilities, domain_allowlist, workers, mcp_tools.

my_config:
    Return the agent-config snapshot injected by ToolExecutor.

network_diagnostics:
    Quick connectivity check to common endpoints (runs inside container if available).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional
from pydantic import Field

from tools.base import ToolBase

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

try:
    from security.security_gate import get_effective_permissions
    GATE_AVAILABLE = True
except ImportError:
    GATE_AVAILABLE = False
    get_effective_permissions = None

try:
    from docker_executor import get_container_status as _get_docker_status
    DOCKER_EXECUTOR_AVAILABLE = True
except ImportError:
    DOCKER_EXECUTOR_AVAILABLE = False
    _get_docker_status = None

try:
    from docker_executor import DockerExecutor
    DOCKER_EXECUTOR_CLS_AVAILABLE = True
except ImportError:
    DOCKER_EXECUTOR_CLS_AVAILABLE = False
    DockerExecutor = None

try:
    from tools.container_manager import (
        ContainerManager as _ContainerManager,
        DOCKER_AVAILABLE as CONTAINER_MANAGER_AVAILABLE,
    )
except ImportError:
    _ContainerManager = None
    CONTAINER_MANAGER_AVAILABLE = False

try:
    from docker.errors import ImageNotFound as _ImageNotFound
except ImportError:
    _ImageNotFound = None

# Worker registry access (for running_workers query)
try:
    from tools.workspace.worker_registry import WorkerRegistry as _WorkerRegistry
    _registry_instance = _WorkerRegistry.get_instance()
    _worker_registry = _registry_instance._worker_registry
    _registry_lock = _registry_instance._registry_lock
    WORKER_REGISTRY_AVAILABLE = True
except ImportError:
    _worker_registry = None
    _registry_lock = None
    WORKER_REGISTRY_AVAILABLE = False

# Vault allowlist support (CheckSystem query allowlist)
try:
    from thoughtmachine.vault import get_checksystem_allowlist
    VAULT_AVAILABLE = True
except ImportError:
    get_checksystem_allowlist = None
    VAULT_AVAILABLE = False


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class CheckSystem(ToolBase):
    """Inspect the runtime environment — permissions, container, workspace, config, and network."""

    tool: str = "CheckSystem"
    # CheckSystem inspects host state and can run subprocesses - always gate it.
    required_categories: ClassVar[List[str]] = ["system:read"]

    query: str = Field(
        description="What to check. Valid values: 'my_config' (full agent config), "
                      "'workers' (all worker definitions), 'running_workers' (active worker statuses), "
                      "'worker/<name>' (specific worker config), 'capabilities' (workspace features), "
                      "'dockerfile' (container environment), 'mcp_servers' (external tool servers), "
                      "'effective_permissions' (session × workspace permissions), "
                      "'container_status' (Docker status), 'workspace_info' (workspace metadata), "
                      "'network_diagnostics' (connectivity checks), "
                      "'event_bus_status' (EventBus subscriber info), "
                      "'event_log' (tail recent EventLogger entries).",
    )

    skip_output_truncation: ClassVar[bool] = True

    allowlist: Optional[list] = Field(default=None, description="Loaded allowlist from vault (list of allowed query names)")

    workspace_id: Optional[str] = Field(
        default=None,
        description="Explicit workspace ID override. If set, used directly instead of resolving from session.",
    )

    # ------------------------------------------------------------------
    def _check_path_allowed(self, query_name: str) -> bool:
        """Check if a CheckSystem query name is allowed by the vault allowlist.

        Returns True if allowed or if vault is unavailable (degraded mode).
        Logs a warning if the query is denied.
        """
        if self.allowlist is None:
            return True  # degraded mode: vault not available, assume allowed
        if query_name in self.allowlist:
            return True
        logger.warning("CheckSystem: query '%s' not in allowlist", query_name)
        return False

    # ------------------------------------------------------------------
    def _load_allowlist_from_vault(self) -> Optional[list]:
        """Load and verify the allowlist from vault.

        Returns the allowlist list, or None if vault is unavailable.
        Uses get_checksystem_allowlist() which handles integrity verification internally.
        """
        if get_checksystem_allowlist is None:
            return None
        try:
            entries = get_checksystem_allowlist()
            logger.info(
                "CheckSystem: loaded allowlist with %d allowed queries",
                len(entries),
            )
            return entries
        except Exception as exc:
            logger.exception("CheckSystem: failed to load allowlist: %s", exc)
            return None

    # ------------------------------------------------------------------
    def execute(self) -> str:
        try:
            # === Resolve workspace path from registries (primary, always correct) ===
            workspace_path = None
            if self.session_id:
                try:
                    from session.session_registry import SessionRegistry
                    session_info = SessionRegistry.get_default().get(self.session_id)
                    ws_id_from_registry = session_info.get("workspace_id") if session_info else None
                    if ws_id_from_registry:
                        from thoughtmachine.workspace_registry import WorkspaceRegistry
                        entry = WorkspaceRegistry.get_default().get_workspace(ws_id_from_registry)
                        workspace_path = entry.root_path if entry else None
                except Exception:
                    pass

            # Fallback to deprecated self.workspace_path
            if not workspace_path:
                workspace_path = getattr(self, 'workspace_path', None)
                if workspace_path:
                    logging.warning(
                        "CheckSystem falling back to deprecated AgentConfig.workspace_path")

            # Resolve workspace ID — explicit field takes priority
            ws_id = self.workspace_id
            if not ws_id and workspace_path and resolve_workspace_id:
                ws_id = resolve_workspace_id(workspace_path)

            # Load vault allowlist
            self.allowlist = self._load_allowlist_from_vault()

            # Allowlist enforcement for ALL queries (not just a subset)
            query = self.query
            if self.allowlist is not None:
                # Handle worker/<name> prefix: check base "workers" against allowlist
                check_query = query
                if query.startswith("worker/"):
                    check_query = "workers"
                allowed = self._check_path_allowed(check_query)
                if not allowed:
                    return json.dumps({
                        'error': f'Query "{query}" not allowed by system configuration',
                        'status': 'denied',
                    }, indent=2)

            # Dynamic handlers: worker/<name> needs special handling
            if query.startswith("worker/"):
                worker_name = query[len("worker/"):]
                result = self._query_worker_detail(ws_id, worker_name)
                return json.dumps(result, indent=2, default=str)

            handler_map = {
                "effective_permissions": lambda: self._query_permissions(ws_id),
                "container_status": lambda: self._query_container_status(workspace_path),
                "workspace_info": lambda: self._query_workspace_info(ws_id),
                "my_config": lambda: self._query_my_config(),
                "network_diagnostics": lambda: self._query_network_diagnostics(ws_id, workspace_path),
                "workers": lambda: self._query_workers(ws_id),
                "running_workers": lambda: self._query_running_workers(),
                "capabilities": lambda: self._query_capabilities(ws_id),
                "dockerfile": lambda: self._query_dockerfile(ws_id),
                "mcp_servers": lambda: self._query_mcp_servers(ws_id),
                "event_bus_status": lambda: self._query_event_bus_status(),
                "event_log": lambda: self._query_event_log(),
            }

            handler = handler_map.get(query)
            if handler is None:
                return json.dumps({
                    "error": f"Unknown query: {query}",
                    "valid_queries": list(handler_map.keys()),
                })

            result = handler()
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.exception("CheckSystem failed")
            return json.dumps(
                {"error": str(exc), "query": getattr(self, 'query', 'unknown')}, indent=2
            )

    # -- query implementations -------------------------------------------

    def _query_permissions(self, ws_id: Optional[str]) -> dict:
        """Return effective permissions (session × workspace)."""
        # Load workspace capabilities from file
        workspace_capabilities = {}
        if CAPABILITIES_AVAILABLE and _workspace_dir and ws_id:
            caps_path = _workspace_dir(ws_id) / "capabilities.json"
            if caps_path.exists():
                try:
                    workspace_capabilities = json.loads(caps_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    workspace_capabilities = {}

        effective = {}
        if GATE_AVAILABLE and get_effective_permissions and self.session_permissions:
            # Build a simple SessionPermissions object or use raw dict
            try:
                from thoughtmachine.security import SessionPermissions
                session_obj = SessionPermissions(**self.session_permissions)
                from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
                caps_obj = WorkspaceCapabilities(**{
                    k: v for k, v in workspace_capabilities.items()
                    if k in [f.name for f in __import__('dataclasses').fields(WorkspaceCapabilities)]
                })
                effective = get_effective_permissions(session_obj, caps_obj)
            except Exception:
                effective = dict(self.session_permissions)
        elif self.session_permissions:
            effective = dict(self.session_permissions)

        return {
            "effective_permissions": effective,
            "workspace_capabilities": workspace_capabilities,
            "workspace_id": ws_id,
            "source": "gate" if GATE_AVAILABLE else "session_fallback",
        }

    def _query_container_status(self, ws_path: Optional[str]) -> dict:
        """Return Docker container status for the workspace."""
        ws_path = ws_path or getattr(self, 'workspace_path', None)
        if not ws_path:
            return {"status": "unavailable", "reason": "No workspace path"}

        if DOCKER_EXECUTOR_AVAILABLE and _get_docker_status:
            try:
                result = _get_docker_status(workspace_path=ws_path)
                return result
            except Exception as e:
                return {"status": "error", "error": str(e)}

        return {"status": "unavailable", "reason": "Docker executor not available"}

    def _query_workspace_info(self, ws_id: Optional[str]) -> dict:
        """Return workspace info including workers and MCP tools."""
        capabilities = {}
        domain_allowlist = []
        workers = []
        mcp_tools = []

        if CAPABILITIES_AVAILABLE and _workspace_dir and ws_id:
            ws_dir = _workspace_dir(ws_id)

            # Load config.json
            config_path = ws_dir / "config.json"
            if config_path.exists():
                try:
                    config_data = json.loads(config_path.read_text(encoding="utf-8"))
                    capabilities = config_data.get("capabilities", {})
                    domain_allowlist = config_data.get("domain_allowlist", [])
                except (json.JSONDecodeError, OSError):
                    pass

            # Load workers.json
            workers_path = ws_dir / "workers.json"
            if workers_path.exists():
                try:
                    workers = json.loads(workers_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    workers = []

            # Load mcp_servers.json
            mcp_path = ws_dir / "mcp_servers.json"
            if mcp_path.exists():
                try:
                    mcp_tools = json.loads(mcp_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    mcp_tools = []

        return {
            "workspace_id": ws_id,
            "capabilities": capabilities,
            "domain_allowlist": domain_allowlist,
            "workers": workers,
            "mcp_tools": mcp_tools,
        }

    def _query_my_config(self) -> dict:
        """Return the agent-config snapshot injected by ToolExecutor (clean JSON)."""
        if self.agent_config is not None:
            cfg = dict(self.agent_config)
            # Redact API key to prevent security leak inside raw_config
            raw_cfg = cfg.copy()
            raw_cfg["api_key"] = "***" if raw_cfg.get("api_key") else None
            # Ensure key fields are always present
            result = {
                "provider": cfg.get("provider", cfg.get("provider_type", "")),
                "model": cfg.get("model", ""),
                "timeout_seconds": cfg.get("timeout_seconds", 600),
                "max_turns": cfg.get("max_turns", 50),
                "enabled_tools": cfg.get("enabled_tools", []),
                "temperature": cfg.get("temperature", 0.7),
                "system_prompt": cfg.get("system_prompt", ""),
                "session_permissions": cfg.get("session_permissions", {}),
                "token_monitor_warning_threshold": cfg.get("token_monitor_warning_threshold", None),
                "token_monitor_critical_threshold": cfg.get("token_monitor_critical_threshold", None),
                "restriction_reason": cfg.get("restriction_reason", None),
                "reasoning_effort": cfg.get("reasoning_effort", None),
                "base_url": cfg.get("base_url", None),
                "api_key": "***" if cfg.get("api_key") else None,
                # Redact API key inside raw_config to prevent security leak
                "raw_config": raw_cfg,
            }
            return result
        return {"error": "agent_config not available"}

    def _query_network_diagnostics(self, ws_id: Optional[str], ws_path: Optional[str]) -> dict:
        """Quick connectivity check inside a throwaway Docker probe container.

        The probe container is created with the SAME isolation the session's
        sandboxes get (network_mode/workspace_mode derived from session
        permissions via ContainerManager), so the result reflects what
        containers in this session actually see. The probe container is
        always stopped afterwards (finally-cleanup). No image pull is
        performed - the probe only runs if ``python:3.11-slim`` is already
        present locally, so a down daemon can never hang this query.
        """
        ws_path = ws_path or getattr(self, 'workspace_path', None)
        if not ws_path:
            return {"container": False, "message": "No workspace path"}

        # Keep the existing guard: the legacy test contract expects a clean
        # {"container": False} when the Docker executor is unavailable.
        if not (DOCKER_EXECUTOR_CLS_AVAILABLE and DockerExecutor):
            return {"container": False, "message": "Docker executor not available"}

        if not CONTAINER_MANAGER_AVAILABLE or _ContainerManager is None:
            return {"container": False, "message": "Container manager not available"}

        import uuid
        name = f"tm-net-diag-{uuid.uuid4().hex[:8]}"
        result = {
            "daemon": False,
            "container": False,
            "container_id": None,
            "container_status": None,
            "image_present": False,
            "probe": None,
            "egress": False,
            "message": "",
        }
        manager = None
        container_id = None
        try:
            manager = _ContainerManager(
                workspace_path=ws_path,
                session_permissions=self.session_permissions or {},
            )
            # Daemon reachability + image presence in one API call. No pull:
            # a missing image is reported and the probe is skipped.
            try:
                manager.client.images.get("python:3.11-slim")
                result["daemon"] = True
                result["image_present"] = True
            except Exception as e:
                if _ImageNotFound is not None and isinstance(e, _ImageNotFound):
                    result["message"] = (
                        "python:3.11-slim image not present locally; probe skipped"
                    )
                else:
                    result["message"] = f"Docker daemon unreachable: {e}"
                return result

            started = manager.start(image="python:3.11-slim", name=name)
            result["container"] = True
            container_id = started.get("id")
            result["container_id"] = container_id
            result["container_status"] = started.get("status")

            probe_cmd = (
                "python3 - <<'PY'\n"
                "import urllib.request\n"
                "try:\n"
                "    r = urllib.request.urlopen('https://example.com', timeout=8)\n"
                "    print('EGRESS_OK', r.status)\n"
                "except Exception as e:\n"
                "    print('EGRESS_BLOCKED', type(e).__name__, e)\n"
                "PY\n"
            )
            out = manager.exec(container_id, probe_cmd, timeout=30)
            result["probe"] = {
                "exit_code": out.get("exit_code"),
                "stdout": out.get("stdout", ""),
                "stderr": out.get("stderr", ""),
            }
            egress = (
                out.get("exit_code") == 0
                and "EGRESS_OK" in (out.get("stdout") or "")
            )
            result["egress"] = egress
            result["message"] = (
                "egress OK (bridge network)" if egress
                else "egress blocked (no network / DNS failure)"
            )
        except Exception as e:
            result["message"] = f"Network diagnostics failed: {e}"
        finally:
            if manager is not None and container_id:
                try:
                    manager.stop(container_id)
                except Exception:
                    pass
        return result

    # ── new query handlers ─────────────────────────────────────────

    def _query_workers(self, ws_id: Optional[str]) -> dict:
        """Return all worker definitions from workers.json."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir:
            return {"workers": [], "count": 0}
        if not ws_id:
            scanned = self._scan_workspace_dirs_for_workers()
            if scanned:
                return {"workers": scanned, "count": len(scanned)}
            return {"workers": [], "count": 0}
        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            return {"workers": [], "count": 0}
        try:
            workers = json.loads(workers_path.read_text(encoding="utf-8"))
            return {"workers": workers, "count": len(workers)}
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"Failed to read workers.json: {e}", "workers": [], "count": 0}

    def _query_worker_detail(self, ws_id: Optional[str], worker_name: str) -> dict:
        """Return full definition of a specific worker by name."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir:
            return {"error": "workspace not available"}
        if not ws_id:
            scanned = self._scan_workspace_dirs_for_workers()
            for w in scanned:
                if isinstance(w, dict) and w.get("name") == worker_name:
                    return w
            return {"error": f"worker '{worker_name}' not found"}
        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            return {"error": "worker not found"}
        try:
            workers = json.loads(workers_path.read_text(encoding="utf-8"))
            for w in workers:
                if isinstance(w, dict) and w.get("name") == worker_name:
                    return w
            return {"error": f"worker '{worker_name}' not found"}
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"Failed to read workers.json: {e}"}

    def _query_running_workers(self) -> dict:
        """Return list of currently running workers with status details."""
        running = []
        if WORKER_REGISTRY_AVAILABLE and _worker_registry is not None and _registry_lock is not None:
            with _registry_lock:
                for key, thread in list(_worker_registry.items()):
                    entry = {
                        "name": key[1] if isinstance(key, tuple) else str(key),
                        "session_id": key[0] if isinstance(key, tuple) else None,
                        "status": thread.status,
                        "alive": thread.is_alive(),
                        "current_task": thread.current_task,
                        "last_heartbeat": thread.last_heartbeat,
                        "error": thread.error,
                        "conversation_length": len(thread._worker_ctx.user_history) if thread._worker_ctx else 0,
                    }
                    elapsed = thread._last_elapsed()
                    if elapsed is not None:
                        entry["elapsed_seconds"] = round(elapsed, 1)
                    running.append(entry)
        return {"running_workers": running, "count": len(running)}

    def _query_capabilities(self, ws_id: Optional[str]) -> dict:
        """Return workspace capabilities: provider, model, tools, docker, git, OS, token limits."""
        result = {
            "provider": None,
            "model": None,
            "enabled_tools": [],
            "has_docker": False,
            "has_git": False,
            "os": None,
            "token_limits": {},
        }

        # Try to get from agent_config first
        if self.agent_config:
            cfg = dict(self.agent_config)
            result["provider"] = cfg.get("provider", cfg.get("provider_type"))
            result["model"] = cfg.get("model")
            result["enabled_tools"] = cfg.get("enabled_tools", [])

        # Check Docker availability
        try:
            import shutil
            result["has_docker"] = shutil.which("docker") is not None
        except Exception:
            pass

        # Check git availability
        try:
            import shutil
            result["has_git"] = shutil.which("git") is not None
        except Exception:
            pass

        result["os"] = os.name

        # Try to read capabilities.json for token limits
        if CAPABILITIES_AVAILABLE and _workspace_dir and ws_id:
            caps_path = _workspace_dir(ws_id) / "capabilities.json"
            if caps_path.exists():
                try:
                    caps_data = json.loads(caps_path.read_text(encoding="utf-8"))
                    result["token_limits"] = {
                        "max_context_length": caps_data.get("max_context_length", 0),
                        "max_conversation_turns": caps_data.get("max_conversation_turns", 0),
                        "max_file_size_bytes": caps_data.get("max_file_size_bytes", 0),
                    }
                    result["has_docker"] = caps_data.get("allow_docker", result["has_docker"])
                    result["has_git"] = caps_data.get("git_available", result["has_git"])
                except (json.JSONDecodeError, OSError):
                    pass

        return result

    def _scan_workspace_dirs_for_workers(self) -> list:
        """
        Scan ``~/.thoughtmachine/workspaces/<id>/workers.json`` for all workspace
        directories and return the first valid workers list found.

        This is a fallback when ``ws_id`` cannot be resolved.
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
                    return data
            except (json.JSONDecodeError, OSError):
                continue

        return []

    def _query_dockerfile(self, ws_id: Optional[str]) -> dict:
        """Return current Dockerfile content as a string."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir or not ws_id:
            return {"available": False, "error": "workspace not available"}
        dockerfile_path = _workspace_dir(ws_id) / "Dockerfile"
        if not dockerfile_path.exists():
            return {"available": False, "error": "Dockerfile not found"}
        try:
            content = dockerfile_path.read_text(encoding="utf-8")
            return {"available": True, "content": content}
        except OSError as e:
            return {"available": False, "error": str(e)}

    def _query_event_bus_status(self) -> dict:
        """Return EventBus subscriber info for diagnostics."""
        try:
            from agent.events import global_event_bus
            with global_event_bus._lock:
                subscribers = {k.value: len(v) for k, v in global_event_bus._subscribers.items()}
                wildcard_count = len(global_event_bus._wildcard_subscribers)
            return {
                "subscribers_by_type": subscribers,
                "wildcard_subscribers": wildcard_count,
                "total_subscriber_types": len(subscribers),
            }
        except Exception as e:
            return {"error": f"Failed to get event bus status: {e}"}

    def _query_event_log(self) -> str:
        """Tail the EventLogger JSONL file and return a formatted string."""
        try:
            from agent.logging.event_logger import EventLogger
            file_path = EventLogger.instance().file_path
            if not os.path.exists(file_path):
                return f"[event_log] No log file found at: {file_path}"

            import subprocess
            result = subprocess.run(
                ["tail", "-n", "50", file_path],
                capture_output=True, text=True, timeout=5,
            )
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

            parts = [f"Event log ({len(lines)} recent entries):", f"Path: {file_path}", ""]
            for i, line in enumerate(lines, 1):
                try:
                    parsed = json.loads(line)
                    parts.append(f"  #{i}: [{parsed.get('event_type', '?')}] {parsed.get('source', '?')} \u2014 {json.dumps(parsed.get('data', {}), default=str)[:200]}")
                except json.JSONDecodeError:
                    parts.append(f"  #{i}: {line[:200]}")

            return "\n".join(parts)
        except Exception as e:
            return f"[event_log] Error: {e}"

    def _query_mcp_servers(self, ws_id: Optional[str]) -> dict:
        """Return list of configured MCP servers."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir or not ws_id:
            return {"mcp_servers": [], "count": 0}
        mcp_path = _workspace_dir(ws_id) / "mcp_servers.json"
        if not mcp_path.exists():
            return {"mcp_servers": [], "count": 0}
        try:
            servers = json.loads(mcp_path.read_text(encoding="utf-8"))
            return {"mcp_servers": servers, "count": len(servers)}
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"Failed to read mcp_servers.json: {e}", "mcp_servers": [], "count": 0}
