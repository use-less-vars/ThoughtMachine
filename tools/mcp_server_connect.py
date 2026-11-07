"""
MCPServerConnect Tool

Connects to an MCP (Model Context Protocol) server registered in the vault
registry and performs a JSON-RPC 2.0 ``initialize`` handshake over stdio.

Registry format (``~/.thoughtmachine/mcp_servers.json``)::

    {
        "servers": {
            "my-server": {
                "command": "python",
                "args": ["-m", "my_mcp_server"],
                "transport": "stdio",
                "env": {"KEY": "value"}
            }
        }
    }

The registry lives in the vault (``~/.thoughtmachine/``); the agent cannot
write to it. The file is a template concept for operators -- this tool only
ever reads it.
"""
import json
import os
import subprocess
from typing import ClassVar, List, Optional

from pydantic import Field

from .base import ToolBase
from security.sandboxed_execution import SandboxedExecution

# Path to the MCP server registry in the vault. Tests monkeypatch this module
# constant -- never hardcode the path anywhere else.
REGISTRY_PATH = os.path.expanduser("~/.thoughtmachine/mcp_servers.json")

# JSON-RPC 2.0 ``initialize`` request per the MCP spec (protocol 2024-11-05),
# piped to the server's stdin via ``SandboxedExecution.run(input=...)``
# (text mode). The server must answer on stdout with a JSON-RPC ``initialize``
# result.
_INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "thoughtmachine", "version": "0.1.0"},
    },
}


class MCPServerConnect(ToolBase):
    """Connect to a registered MCP server and verify its initialize response."""

    required_categories: ClassVar[List[str]] = ["mcp:connect"]

    server_name: str = Field(
        description="Name of the server to connect to, as registered in the vault MCP registry.",
    )

    # ToolExecutor injects workspace_id into tools that declare it (same
    # pattern as GitInfoTool). Needed for vault-aware context.
    workspace_id: Optional[str] = Field(
        default=None,
        description="Workspace identifier injected by ToolExecutor when available.",
    )

    def execute(self) -> str:
        """Connect to the registered server and verify its initialize response."""
        if not os.path.exists(REGISTRY_PATH):
            return "Error: No MCP server registry found. Add servers to ~/.thoughtmachine/mcp_servers.json"

        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                registry = json.load(f)
        except json.JSONDecodeError:
            return f"Error: Invalid JSON in MCP server registry: {REGISTRY_PATH}"

        servers = registry.get("servers", {})
        if self.server_name not in servers:
            return (
                f"Error: Server '{self.server_name}' not found in registry. "
                f"Available: {list(servers.keys())}"
            )

        cfg = servers[self.server_name]
        command = cfg["command"]
        args = list(cfg.get("args", []))
        env = cfg.get("env", {}) or {}

        executor = SandboxedExecution(
            session_permissions=self.session_permissions,
            workspace_id=self.workspace_id,
            logger=self._logger,
        )
        try:
            result = executor.run(
                [command] + args,
                timeout=30,
                required_category="mcp:connect",
                extra_env=env,
                input=json.dumps(_INITIALIZE_REQUEST),
            )
        except subprocess.TimeoutExpired:
            return f"Error: Server '{self.server_name}' timed out after 30 seconds"
        # PermissionError propagates (fail-closed) -- the executor surfaces it.

        if result.returncode != 0:
            return f"Error: Server exited with code {result.returncode}: {result.stderr}"

        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            return f"Error: Invalid response from server: {result.stdout[:200]}"

        result_payload = response.get("result", {})
        capabilities = result_payload.get("capabilities", {})
        server_info = result_payload.get("serverInfo")

        return json.dumps(
            {
                "status": "connected",
                "server": self.server_name,
                "capabilities": capabilities,
                "serverInfo": server_info,
            },
            indent=2,
        )
