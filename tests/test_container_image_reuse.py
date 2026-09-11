"""Bug 3: container image-reuse honesty.

An explicitly-requested image that differs from an existing container's image
can never be honoured by reuse (the image is fixed at create), so
``ContainerManager.start(image=...)`` must reject the reuse with an error
instead of silently returning a container running the wrong image. Matching
images still reuse; an *unreadable* container image is treated conservatively
(a reuse is allowed); and a ``None`` image never triggers the guard.

It also pins the de-duplicated default image SSOT (``DEFAULT_IMAGE``).
"""
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.config.defaults import DEFAULT_IMAGE
from infra.container_manager import ContainerManager


def _mgr():
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_path = "/tmp/ws"
    mgr.workspace_id = "ws-1"
    mgr.session_permissions = None
    mgr.session_id = "sess-1"
    mgr.image = DEFAULT_IMAGE
    mgr._containers = {}
    mgr.container_notes = {}
    mgr._session_config = None
    return mgr


class _FakeContainer:
    """Minimal docker Container stand-in for the workspace-label reuse path."""

    def __init__(self, cid, tags=None, config_image=None, with_image=True):
        self.id = cid
        self.attrs = {
            "HostConfig": {"NetworkMode": "none"},
            "Mounts": [{"Destination": "/workspace", "RW": True}],
        }
        if config_image is not None:
            self.attrs["Config"] = {"Image": config_image}
        if with_image:
            self.image = SimpleNamespace(tags=list(tags or []))

    def reload(self):
        pass

    @property
    def status(self):
        return "running"


def _drive(fake_container, monkeypatch, image=None):
    """Drive the workspace-label reuse path with an explicit ``image``."""
    import infra.container_manager as cm

    mgr = _mgr()
    mgr.list_containers = lambda: [
        {"name": "agent-x", "container_id": "c1", "note": ""}
    ]
    client = MagicMock()
    client.containers.get = lambda cid: fake_container
    mgr.client = client
    mgr._remove_container = MagicMock()
    mgr._find_by_labels = lambda n: None
    mgr._get_max_containers = lambda: 0
    mgr._active_containers = lambda cs: []
    # Deterministic desired config so the fake container's none/rw matches.
    mgr._compute_config = lambda *a, **k: ("none", "rw")
    monkeypatch.setattr(cm, "is_registry_active", lambda *a, **k: False)
    return mgr, mgr.start(name="agent-x", image=image)


def test_different_explicit_image_is_rejected_not_reused(monkeypatch):
    fake = _FakeContainer("c" * 16, tags=["nginx:latest"])
    mgr, result = _drive(fake, monkeypatch, image="ubuntu:latest")
    assert "error" in result
    assert "cannot reuse with image" in result["error"]
    assert result.get("status") != "reused"
    # A mismatch is surfaced, never "fixed" by removing the user's container.
    assert not mgr._remove_container.called


def test_matching_explicit_image_is_reused(monkeypatch):
    fake = _FakeContainer("d" * 16, tags=[f"{DEFAULT_IMAGE}:latest"])
    mgr, result = _drive(fake, monkeypatch, image=DEFAULT_IMAGE)
    assert result.get("status") == "reused"
    assert not mgr._remove_container.called


def test_unreadable_container_image_is_conservatively_reused(monkeypatch):
    # No .image and no Config.Image -> image undeterminable -> allow reuse.
    fake = _FakeContainer("e" * 16, with_image=False)
    mgr, result = _drive(fake, monkeypatch, image="ubuntu:latest")
    assert result.get("status") == "reused"


def test_none_image_reuses_despite_different_container_image(monkeypatch):
    fake = _FakeContainer("f" * 16, tags=["nginx:latest"])
    mgr, result = _drive(fake, monkeypatch, image=None)
    assert result.get("status") == "reused"


def test_default_image_is_shared_ssot():
    from tools.container_control import ContainerStartTool

    param = inspect.signature(ContainerManager.__init__).parameters["image"]
    assert param.default == DEFAULT_IMAGE
    assert ContainerStartTool().image == DEFAULT_IMAGE
