"""Cancellation and ``tools/visual_tool.py``: what an interrupted render does.

``VisualTool.execute()`` shells out to ``ContainerManager.exec`` for the render
itself (``tools/visual_tool.py:486-491``).  Once that exec becomes interruptible
(C11-prime, ``infra/container_manager.py``), a cancelled turn can make it return
an INTERRUPTED payload instead of a normal ``{"exit_code": ...}`` result::

    {"status": "interrupted", "interrupted": True, "reason": ...,
     "exit_code": None, "stdout": "", "stderr": "", ...}

This file pins -- and only pins, it does not change -- what the visual tool does
with such a payload today.  The tool has no cancellation branch of its own: it
reads ``exit_code = render.get("exit_code")`` (line 500), finds ``None != 0``,
and returns its ordinary failure envelope
``"visual: the renderer exited with code None."``.  Two consequences matter and
are asserted here:

1. **Teardown still runs.**  The whole render block sits inside
   ``try: ... finally:`` (line 482/569) whose teardown calls
   ``manager.remove(container_id)`` (line 576) inside a log-only guard, so an
   interrupted render still removes the ephemeral render container exactly once
   -- no leaked container per cancelled turn.
2. **The artifact is never read.**  The ``base64`` fetch (line 515) is only
   reached when ``exit_code == 0``, so an interrupted render issues exactly one
   container exec and writes nothing into the workspace.

Hermetic and mock-based, mirroring ``tests/test_visual_tool_contract.py``: a
recording ``ContainerManager`` stand-in monkeypatched onto the tool module, no
Docker daemon, no render image, no network.  The fakes are copied rather than
imported so this file stands alone and edits to the contract test cannot
silently change what is pinned here.
"""

import base64
import json
import sys
from pathlib import Path

# ``tools`` must be imported BEFORE ``security.security_gate``: see
# ``tests/security/test_gate_import_order.py`` for the order-dependent import
# chain these two packages share.  Keep this import above everything else.
import tools as tools_pkg  # noqa: E402,F401  (order-sensitive; see above)

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402  (import order forced by the sys.path shim)

import tools.visual_tool as visual_tool  # noqa: E402
from tools.visual_tool import VisualTool  # noqa: E402

#: A minimal, structurally valid PNG (signature + filler); irrelevant here (the
#: interrupted path never decodes an artifact) but needed by the happy control.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"render" * 8

#: Grant set that gets a render past both gate surfaces: ``visual:view`` and
#: ``container:true`` for the outer gate, ``filesystem:write`` for the in-tool
#: re-check that runs before any host-side mutation.
WRITE_GRANTS = {"visual": "view", "container": True, "filesystem": "write"}

#: Image reference admitted by the fake "provisioned" build (the repo ships
#: ``VISUAL_RENDER_IMAGE = None``, so every execution test must provision one).
FAKE_RENDER_IMAGE = "render-image:v1"
FAKE_ALLOWLIST = ("render-image",)

#: The payload ``ResourceContainerManager.exec`` returns when the turn is
#: cancelled mid-command: the wait ends, the container is left untouched, and
#: ``exit_code`` is ``None`` because the command never reported one.
INTERRUPTED_RENDER = {
    "status": "interrupted",
    "interrupted": True,
    "reason": "stopped_by_operator",
    "exec": "container_exec",
    "container_id": "render-container-1",
    "command": "render ...",
    "elapsed": 0.5,
    "exit_code": None,
    "stdout": "",
    "stderr": "",
    "container_killed": False,
    "container_removed": False,
    "note": (
        "Stopped WAITING for the render command because the turn was cancelled. "
        "The container was NOT killed and NOT removed; the command may still be "
        "running inside it."
    ),
}


# ---------------------------------------------------------------------------
# Fakes (a recording ContainerManager stand-in, as in the contract test)
# ---------------------------------------------------------------------------
class _FakeManager:
    """Recording ``ContainerManager`` stand-in.

    ``exec_payload`` overrides what a NON-``base64`` exec returns; setting it to
    :data:`INTERRUPTED_RENDER` is how the cancellation cases are driven.
    """

    instances = []
    exec_payload = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.start_calls = []
        self.exec_calls = []
        self.remove_calls = []
        _FakeManager.instances.append(self)

    def start(self, **kwargs):
        self.start_calls.append(kwargs)
        return {"id": "render-container-1"}

    def exec(self, container_id, command="", **kwargs):
        self.exec_calls.append((container_id, command, kwargs))
        if _FakeManager.exec_payload is not None and not command.startswith("base64 "):
            return dict(_FakeManager.exec_payload)
        if command.startswith("base64 "):
            return {
                "exit_code": 0,
                "stdout": base64.b64encode(PNG_BYTES).decode("ascii"),
                "stderr": "",
            }
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def remove(self, container_id):
        self.remove_calls.append(container_id)


@pytest.fixture
def fake_manager(monkeypatch):
    """Replace the tool's ContainerManager with the recording fake."""
    _FakeManager.instances.clear()
    _FakeManager.exec_payload = None
    monkeypatch.setattr(visual_tool, "ContainerManager", _FakeManager)
    return _FakeManager


@pytest.fixture
def provisioned_image(monkeypatch):
    """Provision a render image the admission allow-list admits."""
    monkeypatch.setattr(visual_tool, "VISUAL_RENDER_IMAGE", FAKE_RENDER_IMAGE)
    monkeypatch.setattr(visual_tool, "ADMISSION_IMAGE_ALLOWLIST", FAKE_ALLOWLIST)


@pytest.fixture
def hermetic_ids(monkeypatch):
    """Keep workspace-id and worker-name resolution off the vault."""
    monkeypatch.setattr(visual_tool, "_resolve_workspace_id", lambda workspace: None)
    monkeypatch.setattr(visual_tool, "current_worker_name", lambda: None)


@pytest.fixture
def workspace(tmp_path):
    """A throwaway workspace holding one renderable file."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "page.html").write_text("<html>hi</html>", encoding="utf-8")
    return ws


def _tool(workspace, target="file:///workspace/page.html", **params):
    """Construct a VisualTool bound to ``workspace`` (registry fallback path)."""
    return VisualTool(target=target, workspace_path=str(workspace), **params)


def _result(out):
    """Parse the tool's JSON envelope."""
    return json.loads(out)


# ---------------------------------------------------------------------------
# Control: the same harness completes a render (so the cases below mean something)
# ---------------------------------------------------------------------------
def test_control_render_completes_without_an_interrupted_payload(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """Baseline: no interrupted payload -> success, artifact on disk, one remove."""
    result = _result(
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
    )

    assert result["success"] is True
    assert (workspace / result["artifact_path"]).exists()
    assert fake_manager.instances[0].remove_calls == ["render-container-1"]


# ---------------------------------------------------------------------------
# 1. An interrupted render still tears the container down
# ---------------------------------------------------------------------------
def test_interrupted_render_still_tears_the_container_down(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """The ``finally`` teardown runs on the interrupted path too.

    An interrupted payload carries ``exit_code=None``, so the tool takes its
    ordinary failure branch (``tools/visual_tool.py:500-505``).  That return is
    still inside the ``try`` whose ``finally`` (line 569-581) removes the
    ephemeral render container, so a cancelled turn must not leak one.
    """
    fake_manager.exec_payload = INTERRUPTED_RENDER

    result = _result(
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
    )

    # Two independent ways the container could leak, both pinned:
    assert len(fake_manager.instances) == 1, "exactly one render container"
    assert fake_manager.instances[0].remove_calls == ["render-container-1"]

    # The turn was cancelled, so nothing may be reported as a delivered render.
    assert result["success"] is False
    assert "artifact_path" not in result
    # The artifact DIRECTORY is created before the container starts (step 4), so
    # it exists on every path; what must not exist is a written PNG.
    assert list((workspace / "artifacts").glob("*.png")) == []


# ---------------------------------------------------------------------------
# 2. An interrupted render never reaches the artifact read
# ---------------------------------------------------------------------------
def test_interrupted_render_short_circuits_before_the_artifact_read(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """Only ONE container exec happens: the ``base64`` fetch is never issued.

    Also records, as a fact rather than a wish, that the tool has no
    cancellation branch today: ``exit_code`` is ``None``, so the model is told
    ``"the renderer exited with code None"`` rather than that the turn was
    cancelled.  The interrupted payload's ``reason`` is not surfaced by the
    tool (``reason`` never appears in the error string); this test fails if
    that changes either way, which is what makes it a contract.
    """
    fake_manager.exec_payload = INTERRUPTED_RENDER

    result = _result(
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
    )

    manager = fake_manager.instances[0]
    assert len(manager.exec_calls) == 1, (
        "the artifact read must not run after an interrupted render: "
        f"{manager.exec_calls}"
    )
    assert not manager.exec_calls[0][1].startswith("base64 ")

    assert result["success"] is False
    assert "the renderer exited with code None" in result["error"]

    # Teardown ran for this path as well (same finally block).
    assert manager.remove_calls == ["render-container-1"]
