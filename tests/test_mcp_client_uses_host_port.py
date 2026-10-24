# tests/test_mcp_client_uses_host_port.py
"""RED-first pin for a migration that has NOT been done yet (MCP stdio spawn).

The host-execution chokepoint is ``thoughtmachine/host_port.py`` (+
``host_run``), which owns permission resolution, workspace policy and the
host-execution audit trail.  ``tools/mcp_client.py`` predates it: it imports
``subprocess`` at module scope and ``StdioMCPClient.start()`` spawns the MCP
server itself with::

    self.process = subprocess.Popen([self.command] + self.args, ...)

That is an unaudited host process launch that bypasses the chokepoint entirely.

Migration (NOT DONE yet): ``StdioMCPClient.start()`` must spawn through
``host_run(..., grain="mcp", client_type=<mcp transport>)`` -- ``grain="mcp"``
requires an explicit ``client_type`` in the port contract -- and
``tools/mcp_client.py`` must lose its ``subprocess`` import.

``thoughtmachine/host_port.py`` does not exist yet (it is pinned by
``tests/test_host_port.py``), so the behavioural tests import it lazily and
fail loudly when it is missing; nothing here spawns a real process, opens a
socket or touches the network.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from types import SimpleNamespace

import pytest

from tools import mcp_client
from tools.mcp_client import StdioMCPClient


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _host_port():
    """Import the (not yet existing) host port chokepoint, or fail loudly."""
    try:
        return importlib.import_module("thoughtmachine.host_port")
    except ModuleNotFoundError as exc:  # pragma: no cover - RED path today
        pytest.fail(
            "thoughtmachine.host_port does not exist yet: StdioMCPClient.start() must "
            "route its spawn through host_port.host_run(..., grain='mcp', "
            "client_type=...) instead of subprocess.Popen (%s)" % exc
        )


class _Permissive:
    """Attribute-absorbing stand-in for whatever handle ``host_run`` returns.

    The port's return shape from inside ``StdioMCPClient`` is not fixed yet, so
    every attribute answers something inert: streams read empty, ``poll()``
    reports the child already gone (the reader thread exits immediately),
    ``ok`` is True.
    """

    ok = True
    timed_out = False
    exit_code = 0
    returncode = 0
    duration_ms = 0.0

    def __init__(self) -> None:
        self.stdin = self
        self.stdout = self
        self.stderr = self

    def poll(self):
        return 0

    def readline(self, *args, **kwargs):
        return ""

    def read(self, *args, **kwargs):
        return ""

    def write(self, *args, **kwargs):
        return 0

    def flush(self, *args, **kwargs):
        return None

    def wait(self, *args, **kwargs):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None

    def __getattr__(self, name):
        return _Permissive()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return True

    def __str__(self):
        return ""


def _install_spawn(monkeypatch):
    """Record ``host_port.host_run`` calls and forbid direct ``subprocess`` use.

    Called from the test body (not a fixture) so a missing chokepoint reports as
    a clean FAILED with the migration message instead of a fixture ERROR.
    """
    host_port = _host_port()
    calls = []

    def fake_host_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _Permissive()

    # Import-style agnosticism: patch the chokepoint on its own module AND, when
    # the client bound the name directly, the client's copy of it.
    monkeypatch.setattr(host_port, "host_run", fake_host_run)
    monkeypatch.setattr(mcp_client, "host_run", fake_host_run, raising=False)

    forbidden = []

    def forbidden_popen(*args, **kwargs):
        forbidden.append((args, kwargs))
        raise AssertionError("StdioMCPClient must not call subprocess.Popen")

    subprocess_module = getattr(mcp_client, "subprocess", None)
    if subprocess_module is not None:
        # Kept tolerant on purpose: after the migration the module no longer
        # imports subprocess at all (see the AST test below).
        monkeypatch.setattr(subprocess_module, "Popen", forbidden_popen)

    # start() initialises the session after spawning; the JSON-RPC handshake is
    # not what this file pins, so keep it off the fake handle's stdin/stdout.
    monkeypatch.setattr(StdioMCPClient, "initialize", lambda self: {}, raising=False)

    return SimpleNamespace(calls=calls, forbidden=forbidden)


def _spawned_command(args, kwargs) -> str:
    """The argv the client asked the port to run, flattened for assertions."""
    if "command" in kwargs:
        command = kwargs["command"]
    elif "argv" in kwargs:
        command = kwargs["argv"]
    else:
        command = args[0] if args else ""
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


def _subprocess_imports(module) -> list:
    """Every ``subprocess`` import found in *module*'s source (comments ignored)."""
    tree = ast.parse(inspect.getsource(module))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                alias.name for alias in node.names if alias.name.split(".")[0] == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                found.append(node.module or "subprocess")
    return found


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def test_mcp_client_module_imports_no_subprocess():
    """The stdio spawn must go through the chokepoint, not a private Popen."""
    assert _subprocess_imports(mcp_client) == []


def test_stdio_start_spawns_through_host_port(monkeypatch):
    spawn = _install_spawn(monkeypatch)
    client = StdioMCPClient("my-mcp-server", ["--stdio"])

    client.start()

    assert spawn.forbidden == []
    assert len(spawn.calls) == 1
    args, kwargs = spawn.calls[0]
    command = _spawned_command(args, kwargs)
    assert "my-mcp-server" in command
    assert "--stdio" in command
    assert kwargs.get("grain") == "mcp"
    # grain="mcp" is only resolvable with an explicit transport client_type.
    client_type = kwargs.get("client_type")
    assert client_type
    assert "mcp" in str(client_type)
    assert kwargs.get("shell", False) is False


def test_stdio_start_forwards_the_client_env(monkeypatch):
    spawn = _install_spawn(monkeypatch)
    client = StdioMCPClient("my-mcp-server", [], env={"MCP_TOKEN": "tok-123456"})

    client.start()

    assert len(spawn.calls) == 1
    args, kwargs = spawn.calls[0]
    passed_env = json.dumps(kwargs.get("env"), default=str)
    assert "MCP_TOKEN" in passed_env
    assert "tok-123456" in passed_env
