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


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class CheckSystem(ToolBase):
    """Inspect the runtime environment — permissions, container, workspace, config, and network."""

    tool: str = "CheckSystem"
    required_categories: ClassVar[List[str]] = []

    query: str = Field(
        description="What to inspect: effective_permissions, container_status, workspace_info, my_config, network_diagnostics",
    )

    skip_output_truncation: ClassVar[bool] = True

    # ------------------------------------------------------------------
    def execute(self) -> str:
        try:
            ws_id = None
            if resolve_workspace_id and self.workspace_path:
                ws_id = resolve_workspace_id(self.workspace_path)

            handler_map = {
                "effective_permissions": lambda: self._query_permissions(ws_id),
                "container_status": lambda: self._query_container_status(),
                "workspace_info": lambda: self._query_workspace_info(ws_id),
                "my_config": lambda: self._query_my_config(),
                "network_diagnostics": lambda: self._query_network_diagnostics(ws_id),
            }

            handler = handler_map.get(self.query)
            if handler is None:
                return json.dumps({
                    "error": f"Unknown query: {self.query}",
                    "available_queries": list(handler_map.keys()),
                })

            result = handler()
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.exception("CheckSystem failed")
            return json.dumps(
                {"error": str(exc), "query": self.query}, indent=2
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

    def _query_container_status(self) -> dict:
        """Return Docker container status for the workspace."""
        ws_path = self.workspace_path
        if not ws_path:
            return {"status": "unavailable", "reason": "No workspace path"}

        if DOCKER_EXECUTOR_AVAILABLE and _get_docker_status:
            try:
                result = _get_docker_status(workspace_path=ws_path, session_permissions=self.session_permissions)
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
        """Return the agent-config snapshot injected by ToolExecutor."""
        if self.agent_config is not None:
            return dict(self.agent_config)
        return {"error": "agent_config not available"}

    def _query_network_diagnostics(self, ws_id: Optional[str]) -> dict:
        """Quick connectivity checks, running inside container if available."""
        ws_path = self.workspace_path
        if not ws_path:
            return {"container": False, "message": "No workspace path"}

        if DOCKER_EXECUTOR_CLS_AVAILABLE and DockerExecutor:
            try:
                executor = DockerExecutor(workspace_path=ws_path)
                result = {}

                # DNS checks
                for host in ["pypi.org", "api.github.com"]:
                    dns_result = executor.run_command(f"nslookup {host}")
                    http_result = executor.run_command(
                        f"curl -s -o /dev/null -w '%{{http_code}}' https://{host}"
                    )
                    result[host] = {
                        "dns": "ok" if dns_result else "error",
                        "http": http_result.strip() if http_result else "error",
                    }

                return result
            except Exception as e:
                return {"container": False, "message": f"No container running: {e}"}

        return {"container": False, "message": "No container running"}
