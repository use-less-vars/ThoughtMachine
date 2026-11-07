"""A recreate PRESERVES the container's ``thoughtmachine.created_by`` origin.

The ``thoughtmachine.created_by`` Docker label is the single source of truth for
who created a container: the record has no creator field, so the label on the
LIVE container is the only place that fact lives.  The operator resource edit
(``PATCH .../containers/{record_id}/resources``) re-creates the container --
``remove()`` drops the live container (and copies no label back), then
``start(..., allow_fresh=True)`` builds a new one from the record.  Stamping the
rebuilt container with the ACTING operator would therefore silently rewrite the
origin of an agent-created container on its first resize.

Three things pin the fix:

1. ROUTE level, positive: a live container labelled
   ``agent:session:sess-X`` is rebuilt with ``created_by="agent:session:sess-X"``
   -- and the label is read BEFORE the destroy (``remove`` would make it
   unreadable).
2. ROUTE level, fallback: an absent/blank label, or a daemon read that raises,
   falls back to the acting operator (``operator_created_by(actor)``) and the
   resize still succeeds.
3. MANAGER level: an ordinary (non-recreate) fresh create is unchanged -- with
   no explicit ``created_by`` the session identity is stamped, and an explicit
   one is honoured verbatim.

Run:  python3 -m pytest tests/test_created_by_preserved_on_recreate.py -q
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from thoughtmachine.container_record import api as cr_api
from thoughtmachine.container_record.hook import CREATED_BY_LABEL

client = TestClient(app)

WS = "ws1"
RID = "rec-a"
NAME = "c-a"
DOCKER_ID = "docker-aaa"
RESIZE = "/api/workspace/{ws}/containers/{rid}/resources"


# ── Route harness (mirrors tests/test_resize_preserves_envelope.py) ──────────

@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolate the container-record store in a temp vault root."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)
    return root


def _seed(vault, *, docker_id=DOCKER_ID):
    cr_api.create_record(WS, "ephemeral", "workspace-owned", id=RID, name=NAME,
                         intent_snapshot={"mem_limit": "256m"},
                         vault_root=vault)
    if docker_id:
        cr_api.attach_container(WS, RID, docker_id, vault_root=vault)
    return cr_api.load_record(WS, RID, vault_root=vault)


def _allow(monkeypatch):
    monkeypatch.setattr(server_module, "_container_write_allowed",
                        lambda workspace_id: (True, "allowed"))


def _install(monkeypatch, manager):
    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": manager)
    return manager


def _removed(container_id=DOCKER_ID):
    return {"status": "removed", "container_id": container_id}


def _started(docker_id="docker-new"):
    return {"id": docker_id, "name": NAME, "status": "created", "note": ""}


class _LiveContainer:
    """Stand-in for a ``docker`` container handle: only ``labels`` is read."""

    def __init__(self, labels):
        self.labels = labels


class _ContainersApi:
    """``manager.client.containers`` -- ``.get(id) -> container``.

    Appends to a shared ``timeline`` so a test can prove the label read happens
    BEFORE the destroy (after ``remove()`` the handle -- and the label -- is
    gone).
    """

    def __init__(self, labels=None, error=None, timeline=None):
        self._labels = labels
        self._error = error
        self.timeline = [] if timeline is None else timeline
        self.requested = []

    def get(self, container_id):
        self.requested.append(container_id)
        self.timeline.append(("label_read", container_id))
        if self._error is not None:
            raise self._error
        return _LiveContainer(self._labels)


class _DockerClient:
    def __init__(self, containers):
        self.containers = containers


class _OriginManager:
    """Route double that RECORDS the ``created_by`` of every ``start``.

    Also exposes ``client.containers.get`` (the seam the route uses to recover
    the original creator from the live container).
    """

    def __init__(self, *, labels=None, get_error=None, start_results=None,
                 remove_results=None):
        self.calls = []
        self.timeline = []
        self.containers_api = _ContainersApi(labels, get_error, self.timeline)
        self.client = _DockerClient(self.containers_api)
        self.start_results = list(start_results or [])
        self.remove_results = list(remove_results or [])
        self._containers = [{"name": NAME, "container_id": DOCKER_ID,
                             "status": "running"}]

    def list_containers(self):
        return list(self._containers)

    def status(self, container_id):
        return {"status": "running", "id": container_id}

    @staticmethod
    def _next(queue, what):
        assert queue, (
            f"_OriginManager: no queued {what} result; enqueue one explicitly "
            "instead of relying on a silent default")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def remove(self, container_id, keep_record=False, **kwargs):
        self.calls.append(("remove", container_id, keep_record))
        self.timeline.append(("remove", container_id))
        return self._next(self.remove_results, "remove")

    def start(self, name=None, note=None, allow_fresh=False, created_by=None,
              **kwargs):
        self.calls.append(("start", name, note, allow_fresh, created_by))
        return self._next(self.start_results, "start")

    @property
    def starts(self):
        return [call for call in self.calls if call[0] == "start"]

    @property
    def created_by(self):
        return [call[4] for call in self.starts]


def _patch(record_id, body, *, workspace_id=WS, actor="tester"):
    headers = {"X-Actor": actor} if actor else {}
    return client.patch(RESIZE.format(ws=workspace_id, rid=record_id),
                        headers=headers, json=body)


# ── Route: the ORIGIN is stamped on the rebuilt container ───────────────────

def test_recreate_stamps_the_origin_not_the_acting_operator(vault, monkeypatch):
    _seed(vault)
    manager = _install(monkeypatch, _OriginManager(
        labels={CREATED_BY_LABEL: "agent:session:sess-X"},
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"}, actor="alice")

    assert resp.status_code == 200, resp.text
    # The rebuild carries the container's ORIGIN, not the operator resizing it.
    assert manager.created_by == ["agent:session:sess-X"]
    # ... and it was read off the live container BEFORE the destroy.
    assert manager.containers_api.requested == [DOCKER_ID]
    kinds = [kind for kind, _ in manager.timeline]
    assert kinds == ["label_read", "remove"]


def test_origin_is_stamped_on_the_resize_failed_recovery_too(vault, monkeypatch):
    """The ``_resize_failed`` rollback rebuild also preserves the origin."""
    from infra import container_manager as cm

    _seed(vault)
    manager = _install(monkeypatch, _OriginManager(
        labels={CREATED_BY_LABEL: "agent:session:sess-X"},
        start_results=[{"error": "Minimum memory limit allowed is 6MB"},
                       _started("docker-restored")],
        remove_results=[_removed()]))
    _allow(monkeypatch)
    monkeypatch.setattr(cm, "_audit", lambda event, data="": None)

    resp = _patch(RID, {"mem_limit": "8m"}, actor="alice")

    assert resp.status_code == 400, resp.text
    assert len(manager.starts) == 2
    assert manager.created_by == ["agent:session:sess-X",
                                  "agent:session:sess-X"]


@pytest.mark.parametrize("case", ["missing", "blank", "unreadable"])
def test_fallback_to_the_acting_operator_when_the_label_is_unavailable(
        vault, monkeypatch, case):
    """Nothing readable => the acting operator (never a bare ``"operator"``)."""
    _seed(vault)
    labels = {CREATED_BY_LABEL: ""} if case == "blank" else {}
    manager = _install(monkeypatch, _OriginManager(
        labels=labels,
        get_error=RuntimeError("daemon down") if case == "unreadable" else None,
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"}, actor="alice")

    assert resp.status_code == 200, resp.text
    assert manager.created_by == ["alice"]


# ── Manager: an ordinary fresh create is unchanged ──────────────────────────

class _Abort(Exception):
    """Sentinel: stop ``_fresh_start`` right after the daemon call's kwargs."""


class _Computed(tuple):
    """Stand-in for the resolved-config object: ``(network, workspace)``."""

    effective = None

    def __new__(cls, network, workspace):
        return super().__new__(cls, (network, workspace))


def _bare_manager(monkeypatch):
    """A ``ContainerManager`` with every Docker/record seam stubbed out."""
    from infra import container_manager as cm

    manager = cm.ContainerManager.__new__(cm.ContainerManager)
    manager.workspace_path = "/tmp/ws"
    manager.workspace_id = WS
    manager.session_id = None
    manager.session_permissions = None      # the web UI's manager: no session
    manager.image = "img"
    manager.mem_limit = 1 << 30
    manager.cpu_quota = 50000
    manager.client = object()
    manager._containers = {}
    manager._migrate_legacy_notes_once = lambda: None
    manager._ensure_name_index = lambda: None
    manager._migrate_records_v3_once = lambda: None
    manager._name_collision = lambda name: False
    manager._record_for_name = lambda name: RID
    manager._reuse_container = lambda docker_id: (None, False)
    manager._compute_config = lambda *a, **k: _Computed("none", "ro")

    monkeypatch.setattr(cm, "_host_ids", lambda: (1000, 1000))
    monkeypatch.setattr(
        cm, "load_record",
        lambda workspace_id, record_id: types.SimpleNamespace(
            intent_snapshot={}, docker_id="",
            lifecycle_class=cm.LIFECYCLE_PERSISTENT))
    return manager


def _fresh_start_labels(monkeypatch, *, created_by, session_id="sess-9"):
    """Run the REAL ``_fresh_start`` up to the daemon call; return its labels."""
    from infra import container_manager as cm
    from security.admission_gate import Allow

    manager = _bare_manager(monkeypatch)
    manager.session_id = session_id
    # ``_bare_manager`` does not shadow ``_fresh_start``; drive it for real.
    manager.__dict__.pop("_fresh_start", None)
    monkeypatch.setattr(
        cm, "git_shadow_for_workspace",
        lambda *a, **k: types.SimpleNamespace(tmpfs={}, mounts=[]))
    monkeypatch.setattr(cm, "git_shadow_mask_path", lambda *a, **k: None)
    monkeypatch.setattr(
        cm, "admit", lambda request, probes=None: Allow(spec=request.spec))
    monkeypatch.setattr(cm, "_audit", lambda event, data="": None)
    captured = {}

    def _run_container(**kwargs):
        captured.update(kwargs)
        raise _Abort()

    manager._run_container = _run_container
    with pytest.raises(_Abort):
        manager._fresh_start(
            image="img", name=NAME, note="", worker_name=None,
            lifecycle_class=cm.LIFECYCLE_PERSISTENT,
            network_mode="none", workspace_mode="ro",
            reuse_record_id=RID, permissions=None, created_by=created_by)
    return captured["labels"]


def test_plain_create_defaults_to_the_session_identity(monkeypatch):
    """No explicit creator => ``agent:session:<session_id>``, unchanged."""
    labels = _fresh_start_labels(monkeypatch, created_by=None)

    assert labels[CREATED_BY_LABEL] == "agent:session:sess-9"


def test_plain_create_honours_an_explicit_creator_verbatim(monkeypatch):
    """An explicit ``created_by`` is stamped as given (the recreate path)."""
    labels = _fresh_start_labels(monkeypatch, created_by="agent:session:orig")

    assert labels[CREATED_BY_LABEL] == "agent:session:orig"
