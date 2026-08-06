"""
Contract tests for the MCPServerConnect tool (Phase 2 D3 -- MCP vector closure).

Covers:
- Construction-time rejection of arbitrary command/args parameters (extra='forbid')
- Fail-closed gate denial when session mcp='banned'
- Successful initialize handshake: real inline mock MCP server verifies the
  request on stdin before responding (real SandboxedExecution subprocess)
- Unknown server error reporting
- MCPValidator deprecation contract
- Registry location constraint (vault, outside the workspace tree)

All tests run on the host -- no docker, no network.
"""
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import tools.mcp_server_connect as mcp_server_connect
from security.security_gate import check_required_categories
from tools.mcp_server_connect import MCPServerConnect, REGISTRY_PATH
from tools.mcp_validator import MCPValidator

# A tiny stdio MCP server: reads one stdin line, verifies it carries the
# JSON-RPC "initialize" method (else writes MOCK_ERROR to stderr and exits
# non-zero), then prints one JSON-RPC initialize response and exits 0.
# Must not contain shell metacharacters (SandboxedExecution rejects them).
_MOCK_SERVER_SCRIPT = (
    "import sys, json\n"
    "line = sys.stdin.readline()\n"
    "if '\"method\": \"initialize\"' not in line:\n"
    "    sys.stderr.write('MOCK_ERROR: expected initialize method, got: %r\\n' % line)\n"
    "    sys.exit(3)\n"
    "print(json.dumps({'jsonrpc': '2.0', 'id': 1, "
    "'result': {'capabilities': {'tools': {}}, "
    "'serverInfo': {'name': 'mock', 'version': '1.0'}}}))\n"
)

_CONNECT_PERMS = {
    "mcp": "connect",
    "container": False,
    "network": "banned",
    "filesystem": "read",
    "system": "read",
    "git": "read",
    "execution": "banned",
}

_BANNED_PERMS = dict(_CONNECT_PERMS, mcp="banned")


def _write_registry(tmp_path: Path, servers: dict) -> str:
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return str(path)


def test_rejects_arbitrary_command():
    """MCPServerConnect exposes no command/args params -- extra='forbid'."""
    assert "command" not in MCPServerConnect.model_fields
    assert "args" not in MCPServerConnect.model_fields
    with pytest.raises(ValidationError):
        MCPServerConnect(server_name="x", command="rm -rf /")


def test_denied_with_mcp_banned(tmp_path, monkeypatch):
    """mcp='banned' fails closed: gate denies AND sandbox raises PermissionError."""
    # Gate level: check_required_categories denies 'mcp:connect' with banned.
    ok, msg = check_required_categories(
        ["mcp:connect"], {"mcp": "banned"}, "MCPServerConnect", {}, "", None
    )
    assert ok is False
    assert "Permission denied" in msg

    # Sandbox level: real SandboxedExecution raises PermissionError fail-closed.
    monkeypatch.setattr(
        mcp_server_connect,
        "REGISTRY_PATH",
        _write_registry(
            tmp_path,
            {"mock": {"command": sys.executable, "args": ["-c", _MOCK_SERVER_SCRIPT]}},
        ),
    )
    tool = MCPServerConnect(
        server_name="mock",
        session_permissions=_BANNED_PERMS,
        effective_permissions=_BANNED_PERMS,
        workspace_id="test-ws",
    )
    with pytest.raises(PermissionError):
        tool.execute()


def test_succeeds_with_valid_server(tmp_path, monkeypatch):
    """Real SandboxedExecution spawns the mock server with the initialize request
    on stdin; the mock verifies the handshake ("method": "initialize") and
    responds with a canned JSON-RPC result."""
    monkeypatch.setattr(
        mcp_server_connect,
        "REGISTRY_PATH",
        _write_registry(
            tmp_path,
            {"mock": {"command": sys.executable, "args": ["-c", _MOCK_SERVER_SCRIPT]}},
        ),
    )
    tool = MCPServerConnect(
        server_name="mock",
        session_permissions=_CONNECT_PERMS,
        effective_permissions=_CONNECT_PERMS,
        workspace_id="test-ws",
    )
    out = tool.execute()
    assert '"status": "connected"' in out
    assert '"name": "mock"' in out
    assert '"tools": {}' in out
    assert '"serverInfo"' in out


def test_error_on_unknown_server(tmp_path, monkeypatch):
    """Unknown server name yields a clear registry error."""
    monkeypatch.setattr(
        mcp_server_connect,
        "REGISTRY_PATH",
        _write_registry(
            tmp_path,
            {"other": {"command": sys.executable, "args": ["-c", _MOCK_SERVER_SCRIPT]}},
        ),
    )
    tool = MCPServerConnect(server_name="missing", session_permissions=_CONNECT_PERMS)
    out = tool.execute()
    assert "not found in registry" in out
    assert "Available" in out
    assert "other" in out


def test_old_validator_returns_deprecation_error():
    """MCPValidator.execute() returns the deprecation error string."""
    assert MCPValidator().execute() == (
        "Error: MCPValidator is deprecated. Use MCPServerConnect with a registered server name instead."
    )


def test_registry_outside_workspace():
    """REGISTRY_PATH lives in the vault (~/.thoughtmachine), not the workspace."""
    repo_root = Path(__file__).resolve().parents[2]
    reg = str(REGISTRY_PATH)
    assert ".thoughtmachine" in reg
    assert not reg.startswith(str(repo_root))
    # No registry file exists anywhere in the workspace tree.
    for dirpath, dirnames, filenames in os.walk(str(repo_root)):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in (".git", ".venv", "node_modules", "__pycache__", ".pytest_cache")
        ]
        assert "mcp_servers.json" not in filenames
