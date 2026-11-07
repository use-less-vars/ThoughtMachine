"""RED-first pin: the exec gate must REFUSE a container whose HARDENING recipe
has drifted, exactly as it already refuses an isolation or a user mismatch.

``ContainerManager._check_exec_drift`` today admits (runs) a container whose
network + ``/workspace`` isolation MATCH the session policy but whose docker
HARDENING axes are weaker than the recipe returned by
``infra.container_create.py::_expected_hardening_recipe()`` -- e.g. a writable
root filesystem, dropped caps, or a missing ``no-new-privileges`` security
option.  A weaker hardening recipe is strictly MORE PERMISSIVE, so exec must
FAIL CLOSED: refuse (exit 126, the command is NEVER run) and surface the failing
axes.

The unreadable-attrs sentinel (``["attrs_unreadable"]``) is deliberately NOT
handled here -- that condition is already refused by the pre-existing
``attrs_unresolved`` branch, so this new gate only acts on REAL failing axes and
never masks a genuine drift.

Daemon-free: the manager is built via ``ContainerManager.__new__`` and the
policy/isolation collaborators are monkeypatched, mirroring
``tests/test_container_exec_drift.py``.
"""

import pytest

import infra.container_create as cc
import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import RECORD_LABEL_KEY


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeImageRef:
    def __init__(self, image_id="sha256:deadbeef"):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeContainer:
    def __init__(self, container_id, attrs, labels=None, name=None):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = "running"
        self.labels = dict(labels or {})
        self.attrs = attrs
        self.removed = []
        self.exec_calls = []

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        return (0, (b"out", b"err"))


class _FakeContainers:
    def __init__(self, containers):
        self.containers = list(containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)


class _FakeDockerClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOST_USER = "1000:1000"
WANT = ("none", "ro")


def _attrs(host_overrides=None, user=HOST_USER):
    """attrs for a FULLY HARDENED container whose isolation matches WANT.

    ``host_overrides`` lets a test weaken ONE docker hardening axis; the rest
    stay conformant so the failing-axis list is a single, unambiguous element.
    """
    host = {
        "NetworkMode": WANT[0],
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": True,
    }
    host.update(host_overrides or {})
    return {
        "State": {"Status": "running"},
        "HostConfig": host,
        "Config": {"User": user},
        "Mounts": [{"Destination": "/workspace", "RW": WANT[1] == "rw"}],
    }


def _make_cm(client):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = "s1"
    cm.workspace_id = "w1"
    cm.session_permissions = {}
    cm.client = client
    cm.workspace_config = {"disk_quota_mb": 0}
    # Policy resolution is stubbed so the test isolates the HARDENING gate; the
    # isolation axes MATCH, so today's code early-returns ("run", None).
    cm._compute_config = lambda *a, **k: WANT
    return cm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_memo():
    container_manager._EXEC_DRIFT_SEEN.clear()
    yield
    container_manager._EXEC_DRIFT_SEEN.clear()


@pytest.fixture(autouse=True)
def _pin_host_user(monkeypatch):
    """Pin BOTH host_user seams to a fixed value so CI's uid never varies the
    recipe user nor the user axis (recipe and policy then agree)."""
    monkeypatch.setattr(cc, "host_user", lambda: HOST_USER, raising=True)
    monkeypatch.setattr(container_manager, "host_user", lambda: HOST_USER,
                        raising=True)


@pytest.fixture
def events(monkeypatch):
    import thoughtmachine.container_record as cr

    recorded = []

    def _fake_append(workspace_id, record_id, event_type, actor,
                     vault_root=None, **payload):
        recorded.append({
            "workspace_id": workspace_id,
            "record_id": record_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        })

    monkeypatch.setattr(cr, "append_event", _fake_append, raising=True)
    return recorded


@pytest.fixture
def warnings(monkeypatch):
    calls = []

    def _fake_log(level, component, message, *args, **kwargs):
        calls.append((level, component, message))

    monkeypatch.setattr(container_manager, "log", _fake_log, raising=True)
    return calls


# ---------------------------------------------------------------------------
# (a) matching isolation but a WRITABLE rootfs -> REFUSE (RED today)
# ---------------------------------------------------------------------------


def test_writable_rootfs_with_matching_isolation_refuses(events, warnings):
    """Isolation matches, rootfs is writable (``read_only`` axis drifted).

    RED today: the isolation matches so ``_check_exec_drift`` returns
    ``("run", None)`` -- the weaker hardening is admitted.  After the fix it must
    REFUSE (deny, exit 126) and surface ``["read_only"]``.
    """
    ctr = _FakeContainer(
        "c1", labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_attrs({"ReadonlyRootfs": False}),
    )
    cm = _make_cm(_FakeDockerClient([ctr]))

    decision, payload = cm._check_exec_drift(ctr, "c1")

    assert decision == "deny", (
        "writable-rootfs container with matching isolation must be refused"
    )
    assert payload["exit_code"] == 126
    assert payload["stdout"] == ""
    assert payload["drift"]["decision"] == "deny"
    assert payload["drift"]["reason"] == "hardening_drift"
    assert payload["drift"]["hardening"]["failed"] == ["read_only"]
    assert "reason=hardening_drift" in payload["stderr"]
    # A WARNING + an audit record event fire for the refusal.
    assert any(lvl == "WARNING" for lvl, *_ in warnings)
    assert events != []
    assert events[0]["payload"]["reason"] == "hardening_drift"


def test_dropped_caps_axis_refuses(events):
    """``HostConfig.CapDrop`` present but lacking ``"ALL"`` -> drift."""
    ctr = _FakeContainer("c1", attrs=_attrs({"CapDrop": ["NET_RAW"]}))
    cm = _make_cm(_FakeDockerClient([ctr]))

    decision, payload = cm._check_exec_drift(ctr, "c1")

    assert decision == "deny"
    assert payload["drift"]["hardening"]["failed"] == ["cap_drop"]


def test_missing_no_new_privileges_axis_refuses(events):
    """``HostConfig.SecurityOpt`` present but lacking ``no-new-privileges``."""
    ctr = _FakeContainer("c1", attrs=_attrs({"SecurityOpt": []}))
    cm = _make_cm(_FakeDockerClient([ctr]))

    decision, payload = cm._check_exec_drift(ctr, "c1")

    assert decision == "deny"
    assert payload["drift"]["hardening"]["failed"] == ["security_opt"]


def test_exec_refuses_and_never_runs_the_command(events):
    """End-to-end via ``exec``: the command is NEVER executed on hardening drift."""
    ctr = _FakeContainer(
        "c1", labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_attrs({"ReadonlyRootfs": False}),
    )
    cm = _make_cm(_FakeDockerClient([ctr]))

    result = cm.exec("c1", "rm -rf /")

    assert result["exit_code"] == 126
    assert result["stdout"] == ""
    assert result["drift"]["reason"] == "hardening_drift"
    assert ctr.exec_calls == []
    assert ctr.removed == []


# ---------------------------------------------------------------------------
# (b) a FULLY hardened, matching container is STILL a clean RUN (no over-refusal)
# ---------------------------------------------------------------------------


def test_fully_hardened_container_still_runs(events):
    """A conformant container with matching isolation must NOT be refused."""
    ctr = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"}, attrs=_attrs())
    cm = _make_cm(_FakeDockerClient([ctr]))

    decision, payload = cm._check_exec_drift(ctr, "c1")

    assert decision == "run"
    assert payload is None
    assert events == []
