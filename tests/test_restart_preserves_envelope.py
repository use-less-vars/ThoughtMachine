"""The operator RECREATE rebuild RESTORES the record's recorded envelope.

PROVEN FROM CODE -- the two verbs are NOT symmetric:

* ``recreate`` -> ``manager.remove(container_id, keep_record=True)``
  (``web_ui/backend/server.py:4389``) then ``start(allow_fresh=True)``
  (``:4395-4399``).  ``ContainerManager.start`` then calls ``_fresh_start``
  (``infra/container_manager.py:1627`` / ``:1657``) -- **this is the real
  rebuild path and this is the bug**: the web UI's manager carries no session
  (``session_permissions=None``), so an envelope resolved from the CURRENT
  policy is the most restrictive pair (``none``/``ro``) and the rebuilt
  container is silently downgraded from what the record was created with.
* ``restart`` -> ``manager.stop(container_id)`` (``:4385``) then
  ``start(allow_fresh=False)``.  ``ContainerManager.start`` REUSES the existing
  container (``infra/container_manager.py:1654``) and cannot fresh-create
  without ``allow_fresh``; a missing container is a refusal, not a rebuild.
  So on ``restart`` the envelope kwargs are forwarded but **inert**.

The fix under test is the ``**_recorded_envelope(getattr(record,
"intent_snapshot", None))`` kwarg at ``server.py:4399``.  Tests that do NOT
fail when it is removed are labelled ``# guard:`` -- they are negative /
forwarding guards, not fix proofs.

Coverage, by strength:

1. REAL ``ContainerManager`` driven through the recreate ENDPOINT with only the
   Docker seams stubbed (``test_real_manager_recreate_*``): the fresh-create
   construction site receives the RECORDED pair, not the policy pair.  This is
   the test that proves the silent downgrade cannot happen.
2. FAKE manager, recreate (the bug path): both axes, each axis alone, and the
   no-recorded-envelope negative guard.
3. FAKE manager, restart: the kwargs are forwarded (inert), restart still
   STOPS and never removes.
4. A destroy whose rebuild returns no container id is a 503 with the verb in
   the message, never a silent rebind to the dead handle.
5. ``created_by``: the rebuild stamps the container's ORIGIN label (read
   BEFORE the destroy -- ``remove`` makes the live label unreadable), not the
   acting operator.  The record has NO creator field, so a container that is
   already gone before the read falls back to ``operator_created_by(actor)``
   (documented guard, not a fix proof).

Run:  python3 -m pytest tests/test_restart_preserves_envelope.py -q
"""

from __future__ import annotations

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
RESTART = "/api/container-records/{rid}/restart"
RECREATE = "/api/container-records/{rid}/recreate"


# ── Route harness (mirrors tests/test_resize_preserves_envelope.py) ──────

@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolate the container-record store in a temp vault root."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)
    return root


def _seed(vault, *, intent_snapshot=None, docker_id=DOCKER_ID):
    cr_api.create_record(WS, "ephemeral", "workspace-owned", id=RID, name=NAME,
                         intent_snapshot=intent_snapshot, vault_root=vault)
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


def _stopped(container_id=DOCKER_ID):
    return {"status": "stopped", "container_id": container_id}


def _removed(container_id=DOCKER_ID):
    return {"status": "removed", "container_id": container_id}


def _started(docker_id="docker-new"):
    return {"id": docker_id, "name": NAME, "status": "created", "note": ""}


class _EnvelopeManager:
    """Double that RECORDS the kwargs of every ``start`` (and every verb).

    The real ``ContainerManager.start`` accepts ``network_mode`` /
    ``workspace_mode`` (keyword-only, envelope restore for an operator
    rebuild); this double accepts them via ``**kwargs`` and keeps them in
    ``start_kwargs`` (one dict per ``start`` call, in call order) so a test can
    prove exactly WHICH envelope each rebuild was handed -- and that a rebuild
    with no recorded envelope is handed NEITHER key.
    """

    def __init__(self, *, start_results=None, remove_results=None,
                 stop_results=None):
        self.calls = []
        self.start_kwargs = []
        self.remove_kwargs = []
        self.start_results = list(start_results or [])
        self.remove_results = list(remove_results or [])
        self.stop_results = list(stop_results or [])
        self._containers = [{"name": NAME, "container_id": DOCKER_ID,
                             "status": "running"}]

    def list_containers(self):
        return list(self._containers)

    def status(self, container_id):
        return {"status": "running", "id": container_id}

    @staticmethod
    def _next(queue, what, default):
        if queue:
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return default

    def stop(self, container_id):
        self.calls.append(("stop", container_id))
        return self._next(self.stop_results, "stop", _stopped(container_id))

    def remove(self, container_id, keep_record=False, **kwargs):
        self.calls.append(("remove", container_id, keep_record))
        self.remove_kwargs.append(dict(kwargs))
        return self._next(self.remove_results, "remove", _removed(container_id))

    def start(self, name=None, note=None, allow_fresh=False, created_by=None,
              **kwargs):
        self.calls.append(("start", name, note, allow_fresh, created_by))
        self.start_kwargs.append(dict(kwargs))
        return self._next(self.start_results, "start", _started())

    @property
    def stops(self):
        return [call for call in self.calls if call[0] == "stop"]

    @property
    def removes(self):
        return [call for call in self.calls if call[0] == "remove"]

    @property
    def starts(self):
        return [call for call in self.calls if call[0] == "start"]


def _post(route, record_id=RID, *, workspace_id=WS, actor="tester"):
    headers = {"X-Actor": actor} if actor else {}
    params = {} if workspace_id is None else {"workspace_id": workspace_id}
    return client.post(route.format(rid=record_id), params=params,
                       headers=headers)


def _user_actions(vault, record_id=RID):
    return [e for e in cr_api.read_event_log(WS, record_id, vault_root=vault)
            if e.get("event_type") == "RECORD_USER_ACTION"]


# ── FAKE manager: RECREATE is the rebuild path (the bug) ────────────────

def test_recreate_restores_both_recorded_axes(vault, monkeypatch):
    """The recreate rebuild receives BOTH recorded axes, not the policy pair."""
    _seed(vault, intent_snapshot={"network_mode": "bridge",
                                  "workspace_mode": "rw"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    assert [call[2] for call in manager.removes] == [True]  # keep_record=True
    assert manager.starts[0][3] is True                     # allow_fresh=True
    assert manager.start_kwargs == [{"network_mode": "bridge",
                                     "workspace_mode": "rw"}]


def test_recreate_workspace_mode_only(vault, monkeypatch):
    """Each axis is independent: a workspace-only record passes one key."""
    _seed(vault, intent_snapshot={"workspace_mode": "rw"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{"workspace_mode": "rw"}]


def test_recreate_network_mode_only(vault, monkeypatch):
    """Each axis is independent: a network-only record passes one key."""
    _seed(vault, intent_snapshot={"network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{"network_mode": "bridge"}]


def test_recreate_without_recorded_envelope_passes_no_override(vault,
                                                               monkeypatch):
    # guard: passes with or without the fix; asserts start_kwargs == [{}] only.
    # (Fail-closed: an unrecorded envelope must not be overridden at all.)
    _seed(vault)
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{}]


def test_recreate_rebinds_record_to_the_rebuilt_container(vault, monkeypatch):
    """Recreate is the verb where a genuinely NEW container id appears.

    (A restart rebind test would encode double-only behaviour: the real
    ``ContainerManager.start`` REUSES the existing id on ``allow_fresh=False``.)
    """
    _seed(vault, intent_snapshot={"network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.docker_id == "docker-new"
    assert after.state == "running"


# ── FAKE manager: RESTART only forwards (stop + reuse, not a rebuild) ───

def test_restart_forwards_recorded_envelope(vault, monkeypatch):
    """``restart`` FORWARDS the envelope; the reuse path never consumes it."""
    _seed(vault, intent_snapshot={"network_mode": "bridge",
                                  "workspace_mode": "rw"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RESTART)

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{"network_mode": "bridge",
                                     "workspace_mode": "rw"}]


def test_restart_without_recorded_envelope_passes_no_override(vault,
                                                              monkeypatch):
    # guard: passes with or without the fix; asserts start_kwargs == [{}] only.
    _seed(vault)
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RESTART)

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{}]


def test_restart_only_stops_and_never_removes(vault, monkeypatch):
    # guard: passes with or without the fix; asserts the VERB SHAPE only
    # (restart = stop, no remove, allow_fresh=False), never the envelope.
    _seed(vault, intent_snapshot={"network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _allow(monkeypatch)

    resp = _post(RESTART)

    assert resp.status_code == 200, resp.text
    assert manager.stops == [("stop", DOCKER_ID)]
    assert manager.removes == []
    assert manager.starts[0][3] is False  # allow_fresh=False


# ── Route: a rebuild with no container id is an ERROR, not a rebind ─────

def test_recreate_without_rebuild_id_errors_and_keeps_old_handle(vault,
                                                                 monkeypatch):
    _seed(vault, intent_snapshot={"network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager(start_results=[{}]))
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 503, resp.text
    assert resp.json().get("error") == "recreate did not return a container id"
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.docker_id == DOCKER_ID
    decisions = [a["payload"]["decision"] for a in _user_actions(vault)]
    assert "applied" not in decisions, decisions
    assert decisions[-1] == "error", decisions


def test_restart_without_rebuild_id_errors_and_keeps_old_handle(vault,
                                                                monkeypatch):
    _seed(vault, intent_snapshot={"network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager(start_results=[{}]))
    _allow(monkeypatch)

    resp = _post(RESTART)

    assert resp.status_code == 503, resp.text
    assert resp.json().get("error") == "restart did not return a container id"
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.docker_id == DOCKER_ID
    decisions = [a["payload"]["decision"] for a in _user_actions(vault)]
    assert decisions == ["error"], decisions


# ── REAL manager: the recreate rebuild cannot be silently downgraded ────

class _Computed(tuple):
    """Stand-in for the resolved-config object: ``(network, workspace)``."""

    effective = None

    def __new__(cls, network, workspace):
        return super().__new__(cls, (network, workspace))


def _real_manager(monkeypatch, vault):
    """The REAL ``ContainerManager`` with the Docker seams stubbed out.

    Everything on the rebuild path is REAL -- ``start``, the record-first
    identity ladder, the envelope restore (``_recreate_network`` /
    ``_recreate_workspace``) and the fresh-create construction site
    (``_fresh_start``, captured).  Only the Docker verbs the route touches are
    stubbed: ``list_containers`` (name index), ``remove`` (the destroy) and
    ``_reuse_container`` (the container is gone, as after a real remove).
    ``_compute_config`` returns the session-less policy pair ``none``/``ro`` --
    exactly the silent downgrade this fix must prevent.
    """
    from infra import container_manager as cm

    record = cr_api.load_record(WS, RID, vault_root=vault)
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
    manager._reconcile_record_unbound = lambda *a, **k: None
    manager._warn_note_once = lambda *a, **k: None
    manager._compute_config = lambda *a, **k: _Computed("none", "ro")
    manager.list_containers = lambda: [{"name": NAME,
                                        "container_id": DOCKER_ID,
                                        "status": "running"}]
    manager.status = lambda container_id: {"status": "running",
                                           "id": container_id}
    manager.remove = lambda container_id, keep_record=False, **kw: {
        "status": "removed", "container_id": container_id}

    monkeypatch.setattr(cm, "_host_ids", lambda: (1000, 1000))
    monkeypatch.setattr(cm, "load_record",
                        lambda workspace_id, record_id: record)
    captured = {}

    def _fresh_start(**kwargs):
        captured.update(kwargs)
        return {"id": "docker-new", "name": NAME, "status": "created",
                "note": ""}

    manager._fresh_start = _fresh_start
    return manager, captured


def test_real_manager_recreate_rebuilds_with_both_recorded_axes(vault,
                                                               monkeypatch):
    """The REAL rebuild carries BOTH recorded axes, not ``none``/``ro``."""
    _seed(vault, intent_snapshot={"network_mode": "bridge",
                                  "workspace_mode": "rw"})
    manager, captured = _real_manager(monkeypatch, vault)
    _install(monkeypatch, manager)
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    assert captured["network_mode"] == "bridge"
    assert captured["workspace_mode"] == "rw"
    assert captured["restored_envelope"] is True
    assert captured["reuse_record_id"] == RID


def test_real_manager_recreate_proves_the_downgrade_without_the_envelope(
        vault, monkeypatch):
    """Same real harness, an UNRECORDED envelope: policy decides (none/ro).

    This is the downgrade the fix removes -- asserted here so the both-axes
    test above cannot pass for the wrong reason (e.g. a harness whose
    ``_compute_config`` already produced ``bridge``/``rw``).
    """
    _seed(vault)
    manager, captured = _real_manager(monkeypatch, vault)
    _install(monkeypatch, manager)
    _allow(monkeypatch)

    resp = _post(RECREATE)

    assert resp.status_code == 200, resp.text
    assert captured["network_mode"] == "none"
    assert captured["workspace_mode"] == "ro"
    assert captured["restored_envelope"] is False


# ── created_by: the rebuild stamps the ORIGIN, never the acting operator ─────
#
# ``thoughtmachine.created_by`` is a DOCKER LABEL, not a record field: the
# record schema carries no creator (``thoughtmachine/container_record/models.py``
# ``SCHEMA_FIELD_NAMES`` / ``INTENT_SNAPSHOT_KEYS``), which is exactly why
# ``hook.CREATED_BY_LABEL``'s own docstring says the label "lives on the
# container, not in the record".  So the origin has to be read off the LIVE
# container BEFORE the pre-step destroys it (``restart`` stops at
# ``server.py:4423``, ``recreate`` removes at ``:4427``), otherwise the rebuild
# is stamped with ``operator_created_by(actor)`` and an agent-created container
# silently changes owner.  These tests fail without that read.

ORIGIN = "agent:session:XYZ"
OPERATOR = "alice@example.com"


class _LabelledContainer:
    """A live container: only ``labels`` is ever read off it."""

    def __init__(self, labels):
        self.labels = labels


class _Containers:
    """Stand-in for ``manager.client.containers``: ``get`` answers or raises.

    *labels_fn* returns the live label dict, or ``None`` when the container is
    gone (``get`` then raises, as the Docker SDK does for a removed container);
    *error* makes the read raise instead (daemon/transport failure).
    """

    def __init__(self, labels_fn=None, error=None):
        self._labels_fn = labels_fn
        self._error = error

    def get(self, container_id):
        if self._error is not None:
            raise self._error
        labels = self._labels_fn() if self._labels_fn is not None else None
        if labels is None:
            raise KeyError(container_id)
        return _LabelledContainer(labels)


class _Client:
    """Stand-in for the Docker client: only ``containers.get`` is used."""

    def __init__(self, containers):
        self.containers = containers


def _origin(manager, labels, *, dies_on_remove=True):
    """Give the double a live-container label read that DIES on ``remove``.

    ``manager.remove`` is the destroy: after it runs the container is gone, so
    the label becomes unreadable.  A route that reads the label after the
    pre-step -- or does not read it at all -- therefore cannot return the
    origin, which is the field behaviour this file pins.
    """
    live = {"alive": True}

    def _labels():
        return dict(labels) if live["alive"] else None

    manager.client = _Client(_Containers(_labels))
    if dies_on_remove:
        inner_remove = manager.remove

        def remove(container_id, keep_record=False, **kwargs):
            result = inner_remove(container_id, keep_record=keep_record,
                                  **kwargs)
            live["alive"] = False
            return result

        manager.remove = remove
    return manager


def test_recreate_rebuilds_with_the_origin_created_by(vault, monkeypatch):
    """The rebuild is stamped with the ORIGIN label, not the acting operator.

    The lane-1 envelope assertions ride along on the SAME rebuild, so this can
    never pass by proving the label at the cost of dropping the recorded pair.
    """
    _seed(vault, intent_snapshot={"network_mode": "bridge",
                                  "workspace_mode": "rw"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _origin(manager, {CREATED_BY_LABEL: ORIGIN})
    _allow(monkeypatch)

    resp = _post(RECREATE, actor=OPERATOR)

    assert resp.status_code == 200, resp.text
    assert manager.removes, "recreate must destroy before it rebuilds"
    assert manager.starts[0][4] == ORIGIN                 # created_by kwarg
    assert manager.starts[0][4] != OPERATOR               # not the operator
    # lane-1 envelope: the recorded pair still reaches the rebuild
    assert manager.start_kwargs == [{"network_mode": "bridge",
                                     "workspace_mode": "rw"}]


def test_restart_forwards_the_origin_created_by(vault, monkeypatch):
    """``restart`` stops only, so the live label is still readable: forwarded."""
    _seed(vault, intent_snapshot={"network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager())
    _origin(manager, {CREATED_BY_LABEL: ORIGIN})
    _allow(monkeypatch)

    resp = _post(RESTART, actor=OPERATOR)

    assert resp.status_code == 200, resp.text
    assert manager.removes == []
    assert manager.starts[0][4] == ORIGIN
    assert manager.start_kwargs == [{"network_mode": "bridge"}]


@pytest.mark.parametrize("read", ["gone", "unreadable"])
def test_recreate_with_the_container_already_gone_falls_back_to_the_operator(
        vault, monkeypatch, read):
    # guard: passes with or without the fix -- it documents the RESIDUAL gap.
    # There is NO PERSISTED origin to prefer: the record has no creator field
    # (models.SCHEMA_FIELD_NAMES / INTENT_SNAPSHOT_KEYS) and intent_snapshot
    # carries no creator key, so a container that is already gone (or whose
    # label read raises) has no surviving origin -- the documented fallback is
    # operator_created_by(actor).  Closing this gap needs a schema field, which
    # is out of scope here.
    _seed(vault)
    manager = _install(monkeypatch, _EnvelopeManager())
    if read == "gone":
        manager.client = _Client(_Containers(lambda: None))      # 404 on get
    else:
        manager.client = _Client(
            _Containers(error=RuntimeError("500 Server Error")))
    _allow(monkeypatch)

    resp = _post(RECREATE, actor=OPERATOR)

    assert resp.status_code == 200, resp.text
    assert manager.starts[0][4] == OPERATOR


def test_recreate_without_an_origin_label_falls_back_to_the_operator(
        vault, monkeypatch):
    # guard: passes with or without the fix -- a live container that carries no
    # created_by label at all (predates the label / blank value) legitimately
    # falls back to the acting operator.
    _seed(vault)
    manager = _install(monkeypatch, _EnvelopeManager())
    _origin(manager, {})
    _allow(monkeypatch)

    resp = _post(RECREATE, actor=OPERATOR)

    assert resp.status_code == 200, resp.text
    assert manager.starts[0][4] == OPERATOR


def test_real_manager_recreate_stamps_the_origin_created_by(vault,
                                                           monkeypatch):
    """Same real harness: ``_fresh_start`` receives the ORIGIN + the envelope."""
    _seed(vault, intent_snapshot={"network_mode": "bridge",
                                  "workspace_mode": "rw"})
    manager, captured = _real_manager(monkeypatch, vault)
    _origin(manager, {CREATED_BY_LABEL: ORIGIN})
    _install(monkeypatch, manager)
    _allow(monkeypatch)

    resp = _post(RECREATE, actor=OPERATOR)

    assert resp.status_code == 200, resp.text
    assert captured["created_by"] == ORIGIN
    assert captured["network_mode"] == "bridge"
    assert captured["workspace_mode"] == "rw"
    assert captured["restored_envelope"] is True


def test_recreate_rebind_payload_carries_no_creator_field(vault, monkeypatch):
    """WHY the label -- and not the record -- has to carry the origin.

    The rebind payload the route writes is EXACTLY

        {"docker_id": "docker-new", "state": "running"}

    with no ``created_by`` to write: the record schema has no creator field
    (``thoughtmachine/container_record/models.py`` ``SCHEMA_FIELD_NAMES`` and
    ``INTENT_SNAPSHOT_KEYS`` both carry none), so the preserved origin survives
    ONLY as the Docker label stamped by ``start``.  That is why the route must
    read it and pass it explicitly -- it can never be re-derived later.
    """
    _seed(vault)
    payloads = []
    real_update = cr_api.update_record

    def _record_update(workspace_id, record_id, **kwargs):
        payloads.append(dict(kwargs))
        return real_update(workspace_id, record_id, **kwargs)

    monkeypatch.setattr(cr_api, "update_record", _record_update)
    manager = _install(monkeypatch, _EnvelopeManager())
    _origin(manager, {CREATED_BY_LABEL: ORIGIN})
    _allow(monkeypatch)

    resp = _post(RECREATE, actor=OPERATOR)

    assert resp.status_code == 200, resp.text
    assert payloads == [{"docker_id": "docker-new", "state": "running"}]
    assert "created_by" not in payloads[0]
    # ...and the preserved origin went to the CONTAINER instead.
    assert manager.starts[0][4] == ORIGIN
