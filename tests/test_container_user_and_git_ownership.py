"""B-04 — container user (uid:gid) ownership + git safe.directory tests.

Tests only: no production code is modified.  Every test drives a real
production seam with fakes mirroring the patterns in
``tests/test_registry_wiring.py`` and ``tests/test_container_record_hook.py``.

Coverage:
1. ``ContainerManager._run_container`` passes the host ``uid:gid`` as the
   container user.
2. The same run call keeps git's ``safe.directory`` out of the container
   (ownership is matched instead of being bypassed).
3. ``ContainerProfile`` derives its ``tmpfs`` ``/home/agent`` uid/gid from the
   live host user (fresh, call-time) rather than a hard-coded value.
4. Every container-create site refuses to run from the host root user (uid 0):
   ``ContainerManager.start``, ``ResourceContainerManager._create_resource_container``
   and ``docker_executor.DockerExecutor._ensure_container``.
5. The record store binds a container's ``user`` to ``intent_snapshot["user"]``
   on the live recreate (reuse) path.
6. User drift is detected: a live container whose ``Config.User`` differs from
   the policy user is refused with a ``user`` drift detail.
"""

import json
import os
import sys
import types

import pytest

import docker

import agent.config.defaults as defaults
from agent.config.defaults import host_user

import security.admission_gate as admission_gate
from security.admission_gate import AdmissionDenied

import docker_executor
from docker_executor import DockerExecutor

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager, _user_drift_axis

from infra.resource_container_manager import ResourceContainerManager

from infra.container_registry import ContainerProfile

from thoughtmachine.container_record import (
    INTENT_SNAPSHOT_KEYS,
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    create_record,
    load_record,
)
from thoughtmachine.container_record.hook import record_creation


# ── shared fakes (mirror tests/test_registry_wiring.py, test_container_record_hook.py) ──


class _FakeCtr:
    """Minimal docker container stand-in."""

    def __init__(self, id="ctr-1", name="c1", labels=None, attrs=None,
                 status="running"):
        self.id = id
        self.name = name
        self.labels = labels or {}
        self.attrs = attrs or {}
        self.status = status

    def reload(self):
        return None

    def stop(self, timeout=None):
        return None

    def remove(self, force=False):
        return None


class _FakeContainers:
    def __init__(self, run_result=None, get_result=None, get_raises=False):
        self.run_calls = []
        self._run_result = run_result
        self._get_result = get_result
        self._get_raises = get_raises

    def list(self, all=False, filters=None):
        return []

    def get(self, name):
        if self._get_raises:
            raise docker.errors.NotFound(name)
        return self._get_result

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        return self._run_result


class _FakeVolumes:
    def get_or_create(self, name):
        return None

    def create(self, name):
        return None


class _FakeClient:
    def __init__(self, run_result=None, get_result=None, get_raises=False):
        self.containers = _FakeContainers(
            run_result=run_result, get_result=get_result, get_raises=get_raises
        )
        self.volumes = _FakeVolumes()

    def ping(self):
        return True


def _make_container_manager(client, workspace_id="ws-1", session_id="s1",
                            tmp_path=None):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = str(tmp_path) if tmp_path else "/tmp/ws"
    cm.image = "agent-executor"
    cm.network = "none"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = {}
    cm._containers = {}
    cm.client = client
    cm._compute_config = lambda: ("none", "ro")
    cm.vault_root = str(tmp_path) if tmp_path else "/tmp/ws"
    cm.container_notes = {}
    cm.max_containers = 10
    cm.workspace_config = {}
    cm.dockerfile_path = None
    return cm


def _evidence_attrs(user=None):
    """A live-inspect ``attrs`` payload that carries intent-snapshot evidence."""
    config = {"Image": "sha256:execimg"}
    if user is not None:
        config["User"] = user
    return {
        "Id": "sha256:deadbeef",
        "Image": "sha256:execimg",
        "Config": config,
        "HostConfig": {
            "NetworkMode": "none",
            "Memory": 1073741824,
            "CpuQuota": 100000,
            "OomScoreAdj": 1000,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
        },
        "Mounts": [],
    }


# ── 1 ────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins POSIX uid:gid derivation; host_user() returns None on Windows",
)
def test_container_user_matches_host_uid(monkeypatch):
    """_run_container must pass the host ``uid:gid`` as the container user."""
    client = _FakeClient(run_result=_FakeCtr())
    cm = _make_container_manager(client)
    monkeypatch.setattr(container_manager, "host_user", lambda: "1234:5678")

    cm._run_container(
        image="img", name="c1", labels={}, mounts=[], tmpfs={},
        network_mode="none", lifecycle_class=LIFECYCLE_PERSISTENT,
    )

    assert client.containers.run_calls, "containers.run was never called"
    kwargs = client.containers.run_calls[-1]["kwargs"]
    assert kwargs["user"] == "1234:5678"

    # pins the resolver's uid-derivation, not the patched seam
    monkeypatch.setattr(os, "getuid", lambda: 1234)
    monkeypatch.setattr(os, "getgid", lambda: 5678)
    assert defaults.host_user() == "1234:5678"
    assert "uid=1234" in defaults.host_tmpfs()["/home/agent"]
    assert "gid=5678" in defaults.host_tmpfs()["/home/agent"]


# ── 2 ────────────────────────────────────────────────────────────────────────


def test_git_status_succeeds_without_safe_directory(monkeypatch):
    """git ownership is matched, not bypassed: no ``safe.directory`` injection."""
    client = _FakeClient(run_result=_FakeCtr())
    cm = _make_container_manager(client)

    cm._run_container(
        image="img", name="c1", labels={}, mounts=[], tmpfs={},
        network_mode="none", lifecycle_class=LIFECYCLE_PERSISTENT,
    )

    kwargs = client.containers.run_calls[-1]["kwargs"]
    assert kwargs["user"] == host_user()
    blob = json.dumps(
        {
            "user": kwargs["user"],
            "environment": kwargs.get("environment"),
            "tmpfs": kwargs.get("tmpfs"),
            "labels": kwargs.get("labels"),
            "command": kwargs.get("command"),
            "image": kwargs.get("image"),
        },
        default=str,
    )
    assert "safe.directory" not in blob


# ── 3 ────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only uid:gid mapping; host_tmpfs() omits uid/gid on Windows",
)
def test_macos_uid_501_not_1000_maps(monkeypatch):
    """ContainerProfile().tmpfs /home/agent must reflect the live host uid/gid."""
    monkeypatch.setattr(os, "getuid", lambda: 501)
    monkeypatch.setattr(os, "getgid", lambda: 20)

    profile = ContainerProfile()
    home = profile.tmpfs["/home/agent"]

    assert "uid=501" in home
    assert "gid=20" in home
    assert "1000:1000" not in repr(profile.tmpfs)


# ── 4 ────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "root-host refusal is POSIX-only: _host_ids() returns None on Windows "
        "(no uid concept), so there is no root host to refuse"
    ),
)
def test_root_host_keeps_nonroot_container_user(monkeypatch):
    """No create site may run a container from the host root user (uid 0).

    POSIX-only: the refusal keys off ``_host_ids()``, which returns ``None`` on
    Windows (no uid concept) -- there is no root host to refuse there, so this
    test is skipped.  On POSIX the ``raising=False`` injection of host uid
    ``0`` drives every create site's real ``_host_ids()[0] == 0`` refusal branch
    end to end (the container is never created), keeping the assertions below
    meaningful rather than vacuous.
    """
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)

    # (a) ContainerManager.start refuses up front.
    client = _FakeClient(run_result=_FakeCtr())
    cm = _make_container_manager(client)
    result = cm.start(image="img", name="c1")
    assert result.get("code") == "root_host_unsupported"
    assert client.containers.run_calls == []

    # (b) ResourceContainerManager refuses with a coded AdmissionDenied.
    rm = ResourceContainerManager.__new__(ResourceContainerManager)
    with pytest.raises(AdmissionDenied) as excinfo:
        rm._create_resource_container()
    assert excinfo.value.code == "root_host_unsupported"

    # (c) DockerExecutor._ensure_container refuses before creating a container.
    ex = DockerExecutor.__new__(DockerExecutor)
    ex.workspace_path = "/tmp/ws"
    ex.image = "agent-executor"
    ex.mem_limit = "1g"
    ex.cpu_quota = 100000
    ex.session_permissions = {}
    ex.workspace_id = "ws-1"
    ex._session_config = None
    ex.container = None
    ex.force_rebuild = False
    ex._ensure_image = lambda: None
    ex._compute_container_config = lambda: ("none", "ro")
    ex.client = _FakeClient(get_raises=True)

    monkeypatch.setattr(admission_gate, "admit", lambda *a, **k: None)
    monkeypatch.setattr(admission_gate, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(
        docker_executor, "_load_admission_capabilities",
        lambda *a, **k: None, raising=False,
    )

    with pytest.raises(RuntimeError, match="root_host_unsupported"):
        ex._ensure_container()
    assert ex.client.containers.run_calls == []


# ── 5 ────────────────────────────────────────────────────────────────────────


def test_intent_snapshot_carries_user(monkeypatch, tmp_path):
    """The refreshed intent snapshot records the OBSERVED user, never a
    fabricated policy value: the reuse/recreate path binds the snapshot's
    ``user`` to the live container's inspect ``Config.User``, and the seed
    attach binds ``record.user`` to that same observed value."""
    assert "user" in INTENT_SNAPSHOT_KEYS

    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    # The observed user is the REAL, unmocked host uid:gid.  The reuse path's
    # user-drift axis compares the live user against the policy user
    # (host_user()), so a fabricated mismatch would legitimately DENY and the
    # refreshed snapshot would never be attached.  No host_user() mock here.
    observed = container_manager.host_user()

    # Seed record.user through the REAL two-phase create/attach lifecycle.
    # The hook takes ``Config.User`` first, so carrying a real Config.User makes
    # the record hold the OBSERVED value (never a fabricated policy value).
    with record_creation(
        workspace_id="ws-5", lifecycle_class=LIFECYCLE_PERSISTENT, name="c1",
    ) as handle:
        rid = handle.id
        handle.attach(
            _FakeCtr(id="seed-docker", attrs={"Config": {"User": observed}}),
            state="running")

    # Drive the live recreate (reuse) path: snapshot["user"] from live attrs.
    monkeypatch.setattr(container_manager, "admit", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(
        container_manager, "_load_capabilities", lambda *a, **k: None,
    )

    reused = _FakeCtr(id="cid-reuse", attrs=_evidence_attrs(user=observed))
    client = _FakeClient(run_result=reused)
    cm = _make_container_manager(client, workspace_id="ws-5", tmp_path=tmp_path)

    cm._fresh_start(
        image="img", name="c1", note=None, worker_name=None,
        lifecycle_class=LIFECYCLE_PERSISTENT, network_mode="none",
        workspace_mode="ro", reuse_record_id=rid,
    )

    record = load_record("ws-5", rid)
    assert record is not None
    # Both fields read the OBSERVED user (host path takes Config.User first).
    # Windows: host_user() is None and the store coerces an absent user to "".
    expected = observed if observed is not None else ""
    assert record.user == expected
    assert record.intent_snapshot["user"] == expected
    assert record.user == record.intent_snapshot["user"]


# ── 6 ────────────────────────────────────────────────────────────────────────


def test_user_drift_refuses_when_live_user_differs(monkeypatch):
    """A live Config.User that differs from the policy user is refused."""
    # Controls: equal / blank / absent are NOT drift.
    equal = types.SimpleNamespace(attrs={"Config": {"User": "1000:1000"}})
    assert _user_drift_axis(equal, "1000:1000") is None
    blank = types.SimpleNamespace(attrs={"Config": {"User": ""}})
    assert _user_drift_axis(blank, "1000:1000") is None
    absent = types.SimpleNamespace(attrs={"Config": {}})
    assert _user_drift_axis(absent, "1000:1000") is None

    drifted = types.SimpleNamespace(
        id="cid-9", name="c9", labels={}, status="running",
        attrs={"Config": {"User": "999:999"}},
    )
    axis = _user_drift_axis(drifted, "1000:1000")
    assert axis is not None
    assert axis[0] == "deny"
    assert axis[2] == {"expected": "1000:1000", "actual": "999:999"}

    monkeypatch.setattr(container_manager, "host_user", lambda: "1000:1000")
    client = _FakeClient()
    cm = _make_container_manager(client)
    # Isolate the user axis from the network/workspace axis.
    cm._config_matches = lambda *a, **k: True

    decision, payload = cm._start_drift_decision(
        drifted, "none", "ro", "live", lifecycle_class=None,
    )
    assert decision == "deny"
    assert payload["drift"]["user"] == {"expected": "1000:1000", "actual": "999:999"}

    exec_decision, exec_payload = cm._check_exec_drift(drifted, "cid-9")
    assert exec_decision == "deny"
    assert exec_payload["exit_code"] == 126
    assert exec_payload["drift"]["user"] == {
        "expected": "1000:1000",
        "actual": "999:999",
    }


# ── 7 (optional) ─────────────────────────────────────────────────────────────
# Real, pre-existing ownership guard (NOT authored here): the record-keyed
# user-action route in ``web_ui/backend/server.py::_record_user_action``
# Guard 3 refuses a record that belongs to a FOREIGN workspace with 403
# ("...does not belong to workspace '<ws>'"), and only yields 404 when the
# record is unknown everywhere.  A container-user/ownership change must not
# weaken this foreign-ownership refusal.


def test_foreign_owner_still_refuses(monkeypatch, tmp_path):
    """A record owned by a foreign workspace is still refused (403, not 404)."""
    from fastapi.testclient import TestClient

    from web_ui.backend.server import app
    from thoughtmachine.container_record import api as cr_api

    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)

    # The record exists in ws1 only.
    cr_api.create_record("ws1", "ephemeral", "workspace-owned",
                         id="rec-a", name="c-a", vault_root=root)

    client = TestClient(app)

    # (a) request scoped to a FOREIGN workspace -> 403 ownership refusal.
    resp = client.post("/api/container-records/rec-a/kill",
                       params={"workspace_id": "ws2"},
                       headers={"X-Actor": "tester"})
    assert resp.status_code == 403, resp.text
    assert "does not belong to workspace" in resp.text

    # (b) a genuinely unknown record -> 404 (distinguishable from the 403).
    resp2 = client.post("/api/container-records/does-not-exist/kill",
                        params={"workspace_id": "ws2"},
                        headers={"X-Actor": "tester"})
    assert resp2.status_code == 404, resp2.text


# ── 8 (Windows guard: host has no uid:gid) ───────────────────────────────────
# ``os.getuid``/``os.getgid`` do not exist on Windows, so there is no host
# uid:gid to pin the container to.  The resolver must return ``None`` (never a
# bogus ``0:0``), and ``create_hardened_container`` must then omit ``--user``
# (docker-py drops ``User`` for ``None``) so the container runs as the image
# default instead of being pinned to root.


class _NtOSView:
    """An ``os`` view whose ``name`` is ``"nt"``; everything else delegates.

    Simulates Windows without mutating the process-global ``os.name`` (which
    would make pytest's failure-repr build a ``WindowsPath`` on POSIX and abort
    the run).  ``getuid``/``getgid`` stay the live ones, so reverting the guard
    reddens on an ``assert`` (AssertionError), proving the guard exists.
    """

    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


def test_host_user_is_none_on_windows(monkeypatch):
    """``os.name == "nt"``: ``host_user()`` returns ``None`` and the tmpfs
    recipe omits the uid/gid parameters (assertion-shaped RED: ``getuid`` still
    resolves, so a reverted guard fails the ``assert`` below)."""
    monkeypatch.setattr(defaults, "os", _NtOSView())

    assert defaults.host_user() is None

    recipe = defaults.host_tmpfs()
    assert recipe["/tmp"] == "rw,noexec,nosuid,size=64m"
    assert "/home/agent" in recipe
    assert "uid=" not in recipe["/home/agent"]
    assert "gid=" not in recipe["/home/agent"]


def test_host_user_none_on_windows_without_getuid(monkeypatch):
    """The faithful Windows shape: ``os.name == "nt"`` with NO ``getuid``
    (it does not exist on Windows).  Pre-fix this raised
    ``AttributeError: module 'os' has no attribute 'getuid'`` -- the original
    Windows CI crash; the guard must short-circuit to ``None`` first.

    Reverting the guard makes THIS node fail with AttributeError (the bug),
    distinct from the AssertionError of the delegating-view node above.
    """
    monkeypatch.setattr(defaults, "os", types.SimpleNamespace(name="nt"))

    assert defaults.host_user() is None

    recipe = defaults.host_tmpfs()
    assert "uid=" not in recipe["/home/agent"]
    assert "gid=" not in recipe["/home/agent"]


def test_container_registry_omits_user_when_host_user_is_none(monkeypatch):
    """With no host user (Windows), ``create_hardened_container`` runs as the
    image default: the docker create kwargs carry no ``user``.

    Drives the whole create path against the realistic Windows-shaped ``os``
    (``name == "nt"`` and no ``getuid``) so a missing guard is exercised end to
    end, not just in the resolver.
    """
    from infra.container_registry import create_hardened_container

    monkeypatch.setattr(defaults, "os", types.SimpleNamespace(name="nt"))

    client = _FakeClient(run_result=_FakeCtr())
    create_hardened_container(client, ContainerProfile(), "c-win")

    kwargs = client.containers.run_calls[-1]["kwargs"]
    assert kwargs.get("user") is None
