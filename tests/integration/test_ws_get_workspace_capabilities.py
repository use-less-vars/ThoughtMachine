"""The ``/ws`` ``get_workspace_capabilities`` command must never answer with a
permissive stand-in.

The handler used to fall back to ``WorkspaceCapabilities.default()`` when the
workspace had no ``capabilities.json`` -- i.e. it advertised network / docker /
filesystem-write / git as ALLOWED for a workspace whose capabilities the runtime
gate resolves to the deny-everything profile
(``security.security_gate.get_workspace_capabilities`` is fail-closed).  The
display therefore out-claimed the gate, and a client that trusted the frame saw
"enabled" for resources the runtime refused.

The handler now resolves through the gate, so:

1. a workspace with no capabilities file answers with the FAIL-CLOSED profile
   (all four booleans false);
2. a workspace with a saved capabilities file answers with exactly that file's
   booleans -- the fail-closed answer of (1) is a real resolution, not blanket
   denial;
3. an unavailable gate is an EXPLICIT error frame, never permissive defaults.

Harness: ``tests/integration/harness.py`` (``start_backend`` + ``make_client`` +
``ws_connect`` + ``receive_until_type``) -- the hermetic full-server fixture the
other ``/ws`` command-contract tests use.  There is no frontend consumer of this
reply (grep: none), so the contract is pinned here, on the backend.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_INTEGRATION_DIR = (
    _HERE / "integration" if (_HERE / "integration" / "harness.py").is_file() else _HERE
)
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

import harness as H  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.trust_harness]

#: Workspace id that is never created, so it has no ``capabilities.json``.
ABSENT_WS = "ws-caps-never-created"

#: The four capability booleans the gate merges into the effective profile.
CAP_KEYS = ("allow_network", "allow_docker", "filesystem_write", "git_available")

FAIL_CLOSED_BOOLEANS = {key: False for key in CAP_KEYS}


@pytest.fixture(scope="module")
def caps_server():
    app, tmp_home, stop_fn = H.start_backend()
    try:
        H.register_mock_provider()
        yield app, tmp_home
    finally:
        stop_fn(rmtree=True, restore_env=True)


def _command(server, command_payload, expected_type):
    """Send one ``/ws`` command and return ``(reply_frame, all_frames)``."""
    app, _tmp_home = server
    client = H.make_client(app)
    with H.ws_connect(client) as ws:
        ws.send_json(command_payload)
        evt, events = H.receive_until_type(ws, expected_type, timeout=20.0)
    return evt, events


def _capabilities_reply(server, workspace_id):
    return _command(
        server,
        {"command": "get_workspace_capabilities", "workspace_id": workspace_id},
        "workspace_capabilities",
    )[0]


# ── 1. no capabilities file → the gate's fail-closed profile ───────────────


def test_workspace_without_capabilities_file_is_reported_fail_closed(caps_server):
    """A workspace with no ``capabilities.json`` must NOT be reported as
    permissive.

    RED before the fix: the handler fell back to
    ``WorkspaceCapabilities.default()`` and answered every boolean ``true``
    while the runtime gate denied all four.
    """
    reply = _capabilities_reply(caps_server, ABSENT_WS)

    assert reply["workspace_id"] == ABSENT_WS
    caps = reply["capabilities"]
    assert {key: caps[key] for key in CAP_KEYS} == FAIL_CLOSED_BOOLEANS, (
        "display out-claimed the gate: no capabilities.json must resolve to the "
        f"fail-closed profile, got { {k: caps[k] for k in CAP_KEYS}!r}"
    )


# ── 2. a saved file is echoed exactly (not blanket denial) ────────────────


def test_saved_capabilities_file_is_echoed_exactly(caps_server):
    """A readable ``capabilities.json`` is reported as saved -- mixed values,
    so neither a permissive nor an all-deny constant can pass this."""
    app, tmp_home = caps_server
    client = H.make_client(app)
    info = H.create_workspace(client, tmp_home, name="caps_echo_ws")
    ws_id = info.get("id") or info.get("workspace_id")
    assert ws_id, f"workspace id missing: {info!r}"

    saved = {
        "allow_network": False,
        "allow_docker": True,
        "filesystem_write": True,
        "git_available": False,
    }
    caps_path = Path(tmp_home) / ".thoughtmachine" / "workspaces" / ws_id / "capabilities.json"
    caps_path.parent.mkdir(parents=True, exist_ok=True)
    caps_path.write_text(json.dumps(saved), encoding="utf-8")

    caps = _capabilities_reply(caps_server, ws_id)["capabilities"]

    assert {key: caps[key] for key in CAP_KEYS} == saved


# ── 3. an unavailable gate is an explicit error, never defaults ───────────


def test_unimportable_gate_reports_an_explicit_error(caps_server, monkeypatch):
    """When the gate cannot be imported the handler must answer with an error
    frame -- never ``WorkspaceCapabilities.default()``.

    RED before the fix: the old body read
    ``thoughtmachine.workspace_capabilities`` directly and answered a
    ``workspace_capabilities`` frame with all four booleans ``true``.
    """
    # ``from security.security_gate import get_workspace_capabilities`` raises
    # ImportError when the module object lacks the name.
    monkeypatch.setitem(
        sys.modules, "security.security_gate", types.ModuleType("security.security_gate")
    )

    reply, events = _command(
        caps_server,
        {"command": "get_workspace_capabilities", "workspace_id": ABSENT_WS},
        "status_message",
    )

    assert "workspace_capabilities" not in [e.get("type") for e in events], (
        "a permissive/derived capabilities frame was emitted despite an "
        f"unavailable gate: {events!r}"
    )
    assert reply["type"] == "status_message"
    text = reply.get("text", "")
    assert "capabilit" in text.lower(), f"error frame names no cause: {text!r}"
