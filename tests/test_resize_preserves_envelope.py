"""The resources PATCH recreate RESTORES the record's recorded envelope.

The operator resource edit (``PATCH .../containers/{record_id}/resources``)
persists the new intent onto the record and then calls
``ContainerManager.start(..., allow_fresh=True)``, which REBUILDS the container
from that record.  The web UI's manager carries no session
(``session_permissions=None``), so an envelope resolved from the CURRENT policy
is the most restrictive pair (``none``/``ro``) -- a rebuild would silently
downgrade a container that was originally created with a wider, recorded
envelope.

Two things pin the fix:

1. ROUTE level (``_EnvelopeManager`` records the kwargs of every ``start``): a
   record whose ``intent_snapshot`` carries ``network_mode``/``workspace_mode``
   hands that recorded envelope to both recreate attempts (the edit and the
   ``_resize_failed`` recovery); a record with NO recorded envelope hands over
   NEITHER key, so the fail-closed policy apply for an ordinary fresh create is
   untouched.
2. MANAGER level (a real ``ContainerManager`` with Docker seams stubbed): an
   explicit override wins over the computed policy at the ``allow_fresh`` create
   site, and the admission gate's policy NARROWING (``Transform``) is not applied
   to a restored envelope (it would put ``none`` back) -- while a ``Deny`` still
   refuses and a create with no override keeps the narrowing verbatim.

Run:  python3 -m pytest tests/test_resize_preserves_envelope.py -q
"""

from __future__ import annotations

import dataclasses
import types

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from thoughtmachine.container_record import api as cr_api

client = TestClient(app)

WS = "ws1"
RID = "rec-a"
NAME = "c-a"
DOCKER_ID = "docker-aaa"
RESIZE = "/api/workspace/{ws}/containers/{rid}/resources"


# ── Route harness (mirrors tests/test_container_resources_resize_route.py) ──

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


def _removed(container_id=DOCKER_ID):
    return {"status": "removed", "container_id": container_id}


def _started(docker_id="docker-new"):
    return {"id": docker_id, "name": NAME, "status": "created", "note": ""}


class _EnvelopeManager:
    """``_FakeManager`` variant that RECORDS the kwargs of every ``start``.

    The real ``ContainerManager.start`` gained ``network_mode`` /
    ``workspace_mode`` (keyword-only, envelope restore for an operator
    recreate); this double accepts them via ``**kwargs`` and keeps them in
    ``start_kwargs`` (one dict per ``start`` call, in call order) so a test can
    prove exactly WHICH envelope each rebuild was handed -- and that a create
    with no recorded envelope is handed NEITHER key.
    """

    def __init__(self, *, start_results=None, remove_results=None,
                 record_probe=None):
        self.calls = []
        self.remove_kwargs = []
        self.start_kwargs = []
        self.start_snapshots = []
        self.start_results = list(start_results or [])
        self.remove_results = list(remove_results or [])
        self.record_probe = record_probe
        self._containers = [{"name": NAME, "container_id": DOCKER_ID,
                             "status": "running"}]

    def list_containers(self):
        return list(self._containers)

    def status(self, container_id):
        return {"status": "running", "id": container_id}

    @staticmethod
    def _next(queue, what):
        assert queue, (
            f"_EnvelopeManager: no queued {what} result; enqueue one "
            "explicitly instead of relying on a silent default")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def remove(self, container_id, keep_record=False, **kwargs):
        self.calls.append(("remove", container_id, keep_record))
        self.remove_kwargs.append(dict(kwargs))
        return self._next(self.remove_results, "remove")

    def start(self, name=None, note=None, allow_fresh=False, created_by=None,
              **kwargs):
        self.calls.append(("start", name, note, allow_fresh, created_by))
        self.start_kwargs.append(dict(kwargs))
        if self.record_probe is not None:
            record = self.record_probe()
            self.start_snapshots.append(
                dict(getattr(record, "intent_snapshot", None) or {})
                if record is not None else {})
        return self._next(self.start_results, "start")

    @property
    def removes(self):
        return [call for call in self.calls if call[0] == "remove"]

    @property
    def starts(self):
        return [call for call in self.calls if call[0] == "start"]


def _patch(record_id, body, *, workspace_id=WS, actor="tester"):
    headers = {"X-Actor": actor} if actor else {}
    return client.patch(RESIZE.format(ws=workspace_id, rid=record_id),
                        headers=headers, json=body)


def _collect_audits(monkeypatch):
    """Capture ``infra.container_manager._audit(event, data)`` calls."""
    from infra import container_manager as cm

    seen = []
    monkeypatch.setattr(cm, "_audit",
                        lambda event, data="": seen.append((event, data)))
    return seen


# ── Route: a recorded envelope is handed to the recreate ────────────────────

def test_recorded_network_mode_is_restored_on_recreate(vault, monkeypatch):
    _seed(vault, intent_snapshot={"mem_limit": "256m",
                                  "network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager(
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{"network_mode": "bridge"}]
    # The resize only touched the limit: the recorded envelope is preserved.
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.intent_snapshot == {"mem_limit": "512m",
                                     "network_mode": "bridge"}


def test_recorded_workspace_mode_is_restored_on_recreate(vault, monkeypatch):
    _seed(vault, intent_snapshot={"mem_limit": "256m",
                                  "workspace_mode": "rw"})
    manager = _install(monkeypatch, _EnvelopeManager(
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 200, resp.text
    assert manager.start_kwargs == [{"workspace_mode": "rw"}]


def test_both_recorded_axes_and_the_new_limits_coexist(vault, monkeypatch):
    _seed(vault, intent_snapshot={"mem_limit": "256m", "cpu_quota": 50000,
                                  "network_mode": "bridge",
                                  "workspace_mode": "rw"})
    manager = _install(monkeypatch, _EnvelopeManager(
        start_results=[_started()], remove_results=[_removed()],
        record_probe=lambda: cr_api.load_record(WS, RID, vault_root=vault)))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 200, resp.text
    # The envelope override never displaces the (unchanged) limit half: the
    # rebuild still reads the NEW limits from the record.
    assert manager.start_snapshots == [
        {"mem_limit": "512m", "cpu_quota": 50000,
         "network_mode": "bridge", "workspace_mode": "rw"}]
    assert manager.start_kwargs == [{"network_mode": "bridge",
                                     "workspace_mode": "rw"}]


@pytest.mark.parametrize("snapshot", [
    {"mem_limit": "256m"},
    {"mem_limit": "256m", "network_mode": "", "workspace_mode": ""},
    {},
])
def test_no_recorded_envelope_passes_no_override(vault, monkeypatch, snapshot):
    """Fail-closed survives: with nothing recorded the policy still decides."""
    _seed(vault, intent_snapshot=dict(snapshot) or None)
    manager = _install(monkeypatch, _EnvelopeManager(
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 200, resp.text
    # NEITHER key is passed, so ``start`` computes the envelope from policy.
    assert manager.start_kwargs == [{}]


def test_failed_recreate_rebuild_restores_the_recorded_envelope(
        vault, monkeypatch):
    """The ``_resize_failed`` recovery start also restores the envelope."""
    _seed(vault, intent_snapshot={"mem_limit": "256m",
                                  "network_mode": "bridge"})
    manager = _install(monkeypatch, _EnvelopeManager(
        start_results=[
            {"error": "Minimum memory limit allowed is 6MB"},
            _started("docker-restored"),
        ],
        remove_results=[_removed()]))
    _allow(monkeypatch)
    _collect_audits(monkeypatch)

    resp = _patch(RID, {"mem_limit": "8m"})

    assert resp.status_code == 400, resp.text
    assert len(manager.starts) == 2
    # Both attempts (the edit and the rollback recovery) restore the envelope.
    assert manager.start_kwargs == [{"network_mode": "bridge"},
                                    {"network_mode": "bridge"}]
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.intent_snapshot == {"mem_limit": "256m",
                                     "network_mode": "bridge"}
    assert after.docker_id == "docker-restored"


# ── Manager: the override reaches the allow_fresh create site ───────────────

class _Computed(tuple):
    """Stand-in for the resolved-config object: ``(network, workspace)``."""

    effective = None

    def __new__(cls, network, workspace):
        return super().__new__(cls, (network, workspace))


def _bare_manager(monkeypatch, *, computed=("none", "ro"), record=None):
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
    manager._compute_config = lambda *a, **k: _Computed(*computed)

    monkeypatch.setattr(cm, "_host_ids", lambda: (1000, 1000))
    monkeypatch.setattr(
        cm, "load_record",
        lambda workspace_id, record_id: record if record is not None
        else types.SimpleNamespace(intent_snapshot={}, docker_id="",
                                   lifecycle_class=cm.LIFECYCLE_PERSISTENT))
    captured = {}

    def _fresh_start(**kwargs):
        captured.update(kwargs)
        return {"id": "docker-new", "name": NAME, "status": "created"}

    manager._fresh_start = _fresh_start
    return manager, captured


def test_start_override_reaches_the_allow_fresh_create_site(monkeypatch):
    manager, captured = _bare_manager(monkeypatch)

    result = manager.start(name=NAME, allow_fresh=True,
                           network_mode="bridge")

    assert result["status"] == "created"
    assert captured["network_mode"] == "bridge"          # recorded envelope
    assert captured["workspace_mode"] == "ro"            # computed half kept
    assert captured["restored_envelope"] is True


def test_start_workspace_override_only(monkeypatch):
    manager, captured = _bare_manager(monkeypatch)

    manager.start(name=NAME, allow_fresh=True, workspace_mode="rw")

    assert captured["network_mode"] == "none"
    assert captured["workspace_mode"] == "rw"
    assert captured["restored_envelope"] is True


def test_start_without_override_keeps_the_fail_closed_policy(monkeypatch):
    """No override => the computed (most restrictive) envelope, unchanged."""
    manager, captured = _bare_manager(monkeypatch)

    manager.start(name=NAME, allow_fresh=True)

    assert captured["network_mode"] == "none"
    assert captured["workspace_mode"] == "ro"
    assert captured["restored_envelope"] is False


# ── Manager: admission narrowing does not undo a restored envelope ──────────

class _Abort(Exception):
    """Sentinel: stop ``_fresh_start`` right after the envelope is resolved."""


def _fresh_start_capture(monkeypatch, *, restored_envelope,
                         network_mode="bridge", workspace_mode="ro"):
    """Run the real ``_fresh_start`` up to the daemon call.

    Admission is stubbed to return a policy ``Transform`` that narrows the
    network mode to ``none`` (exactly what a session-less manager produces), so
    the captured ``network_mode`` proves whether that narrowing was applied.
    """
    from infra import container_manager as cm

    manager, _ = _bare_manager(monkeypatch)
    # ``_bare_manager`` shadows ``_fresh_start`` with a capture stub; this test
    # drives the REAL method, so drop the instance-level shadow.
    manager.__dict__.pop("_fresh_start", None)
    monkeypatch.setattr(
        cm, "git_shadow_for_workspace",
        lambda *a, **k: types.SimpleNamespace(tmpfs={}, mounts=[]))
    monkeypatch.setattr(cm, "git_shadow_mask_path", lambda *a, **k: None)
    monkeypatch.setattr(
        cm, "admit",
        lambda request, probes=None: cm.Transform(
            spec=dataclasses.replace(request.spec, network_mode="none"),
            code="narrowed_to_policy"))
    audits = _collect_audits(monkeypatch)
    captured = {}

    def _run_container(**kwargs):
        captured.update(kwargs)
        raise _Abort()

    manager._run_container = _run_container
    with pytest.raises(_Abort):
        manager._fresh_start(
            image="img", name=NAME, note="", worker_name=None,
            lifecycle_class=cm.LIFECYCLE_PERSISTENT,
            network_mode=network_mode, workspace_mode=workspace_mode,
            reuse_record_id=RID, permissions=None,
            restored_envelope=restored_envelope)
    return captured, audits


def test_restored_envelope_is_not_narrowed_by_admission(monkeypatch):
    captured, audits = _fresh_start_capture(monkeypatch,
                                            restored_envelope=True)

    # The rebuilt container keeps the envelope it was originally admitted with.
    assert captured["network_mode"] == "bridge"
    tags = [tag for tag, _ in audits]
    assert tags == ["CONTAINER_CREATE", "CONTAINER_CREATE_ENVELOPE_RESTORED"]
    assert "network=bridge" in audits[1][1]
    assert "policy_narrowed_to=none" in audits[1][1]


def test_plain_create_still_applies_the_admission_narrowing(monkeypatch):
    captured, audits = _fresh_start_capture(monkeypatch,
                                            restored_envelope=False)

    # Byte-identical to the pre-fix behaviour: the Transform narrows to none.
    assert captured["network_mode"] == "none"
    assert [tag for tag, _ in audits] == ["CONTAINER_CREATE"]
