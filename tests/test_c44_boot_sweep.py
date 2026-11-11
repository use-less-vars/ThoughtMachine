"""C44 batch 2 -- boot-time worker catalogue + status-aware stale sweep.

What is pinned here
===================
``infra/container_manager.py`` already knows HOW to spare a parked worker:
``cleanup_stale_worker_containers(docker_client, owner_identity,
worker_status=None)`` skips outright when ``worker_status == "detached"``, and
it evaluates that gate BEFORE listing containers (``container_manager.py:5289``
gate, ``:5312`` listing).  What does not exist is a *second caller* with boot
semantics.  The only production call site is ``container_manager.py:2310``,
inside ``ContainerManager.start()``'s pre-create self-heal, and it must keep
reaping by name: threading ``worker_status="detached"`` into it would disable
the self-heal for the very worker being respawned (design doc
``docs/design-c44-worker-state-reattachment.md`` §5.1.1).

So this file pins the new surface, module level in ``infra.container_manager``
-- next to the primitive it feeds, because ``infra`` must never import
``tools.workspace.worker_thread`` (see the comment at
``container_manager.py:5208-5211``), which is also the answer to the design
doc's open question §8 q2: the boot sweep's *owner* is the supervisor/boot
path, but its *code* can only live where the reap primitive lives, because the
worker layer cannot be imported from here and the primitive must not be
duplicated::

    PersistedWorker                      read-only record of one on-disk worker
    iter_persisted_workers(workers_dir)  the L1 catalogue (a witness, not a worker)
    sweep_stale_worker_containers(docker_client, workers_dir, *,
                                  live_identities=())
                                         the L2 sweep -- the boot-purpose call site

``workers_dir`` is a workspace's ``workers/`` directory and must tolerate BOTH
shipped layouts (``workers/<worker_name>/status.json`` legacy and
``workers/<session_id>/<worker_name>/status.json`` session-scoped), the same
two layouts ``web_ui/backend/health_routes.py::_collect_workers_from_fs``
already walks.  The join key back to Docker is the ``thoughtmachine.worker``
label value: ``"<session_id or 'unknown'>:<worker_name>"``
(``tools/workspace/worker_container.py:30-51``).

Design invariants these tests enforce
-------------------------------------
§I4 **fail closed on unknown.**  A missing / corrupt / key-less ``status.json``
    yields ``"unknown"`` and **no reap**.  This is deliberately the opposite of
    the phrasing in the wave ticket ("malformed status.json => reap"), which
    would destroy a parked worker's containers on evidence it could not read.
    The system's own precedent agrees with the doc: ``_detached_state()`` is
    tri-state and ``None`` means "cannot tell => do not reap"
    (``container_manager.py:5241-5253``).
§I5 **exact-label ownership.**  ``sid:worker2`` is never attributed to
    ``sid:worker`` -- no prefix matching.
§I9 **the catalogue is a witness, not a worker.**  No ``WorkerThread`` is
    constructed and no registry entry is created.
§I10 **cheap gate before expensive work.**  A spared identity is not even
    listed: ``docker_client.containers.list`` is never called for it.
§I11 **half-deleted directories are legal.**  A missing ``context.json`` with a
    live ``status.json`` is a normal teardown intermediate state.
§7 **discipline.**  No sleeps, no ``thread.start()``, no Docker daemon: the
    client is a fake and the "was it reaped?" question is answered from
    recorded ``ContainerIntent``s (``ContainerLifecycle.reconcile`` is the
    observable seam, ``container_manager.py:5343-5350``).  Every "did not
    happen" test has a paired positive (vacuity) partner.

Everything in this file is RED until the surface above exists.
"""

from __future__ import annotations

import json

import pytest

import infra.container_manager as container_manager
from infra.container_lifecycle import ContainerLifecycle

# The missing surface -- imported at module level so the red signal names it.
from infra.container_manager import (  # noqa: F401  (ImportError == red)
    PersistedWorker,
    iter_persisted_workers,
    sweep_stale_worker_containers,
)

_WORKER_LABEL = "thoughtmachine.worker"
_RESOURCE_LABEL = "thoughtmachine.resource"


# ---------------------------------------------------------------------------
# Fakes -- shaped like the docker SDK (see tests/test_container_manager_funnel_t1c.py)
# ---------------------------------------------------------------------------
class FakeContainer:
    def __init__(self, *, name="c1", cid="c1", status="exited", labels=None):
        self.name = name
        self.id = cid
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {
            "State": {
                "Status": status,
                "Running": status == "running",
                "FinishedAt": "2020-01-01T00:00:00Z",
            }
        }
        self.remove_calls = []

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class FakeContainers:
    """Returns every container regardless of the filter, and records the calls.

    Ignoring ``filters`` is deliberate: it forces the exact-label re-check
    (``container_manager.py:5324``) to do the attribution work, which is what
    §I5 is about.
    """

    def __init__(self, containers=None):
        self._containers = list(containers or [])
        self.list_calls = []

    def list(self, all=False, filters=None):  # noqa: A002 - docker SDK signature
        self.list_calls.append({"all": all, "filters": filters})
        return list(self._containers)


class FakeClient:
    def __init__(self, containers=None):
        self.containers = FakeContainers(containers)


def _reconcile_spy(monkeypatch):
    """Record every ``ContainerIntent`` that reaches the lifecycle funnel."""
    intents = []
    real = ContainerLifecycle.reconcile

    def spy(container, session, intent=None):
        intents.append(intent)
        return real(container, session, intent)

    monkeypatch.setattr(ContainerLifecycle, "reconcile", spy)
    return intents


def _reap_intents(intents):
    return [i for i in intents if getattr(i, "kind", None) == "reap"]


def _identity_call(call):
    """``(owner_identity, worker_status)`` from a recorded cleanup call."""
    args = list(call.args)
    kwargs = dict(call.kwargs)
    identity = kwargs.get("owner_identity", args[1] if len(args) > 1 else None)
    if "worker_status" in kwargs:
        status = kwargs["worker_status"]
    elif len(args) > 2:
        status = args[2]
    else:
        status = None
    return identity, status


# ---------------------------------------------------------------------------
# On-disk fixtures -- both shipped layouts
# ---------------------------------------------------------------------------
def _write_status(worker_dir, payload):
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return worker_dir


def _status_payload(session_id, runtime_status, **extra):
    payload = {"session_id": session_id, "runtime_status": runtime_status}
    payload.update(extra)
    return payload


def _session_scoped_worker(workers_dir, session_id, worker_name, runtime_status, **extra):
    """``workers/<session_id>/<worker_name>/status.json`` -- session-scoped."""
    return _write_status(
        workers_dir / session_id / worker_name,
        _status_payload(session_id, runtime_status, **extra),
    )


def _legacy_worker(workers_dir, worker_name, runtime_status, session_id="sess-1"):
    """``workers/<worker_name>/status.json`` -- legacy, no session directory."""
    return _write_status(
        workers_dir / worker_name, _status_payload(session_id, runtime_status)
    )


def _worker_container(cid, owner_identity, status="exited", extra_labels=None):
    labels = {_WORKER_LABEL: owner_identity}
    labels.update(extra_labels or {})
    return FakeContainer(name=cid, cid=cid, status=status, labels=labels)


# ---------------------------------------------------------------------------
# Isolation: no detached registry leakage, no resource skipping, silent log
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(container_manager, "_DETACHED_WORKER_IDENTITIES", set())
    monkeypatch.setattr(container_manager, "_gc_should_skip", lambda c: False)
    monkeypatch.setattr(container_manager, "log", lambda *a, **k: None)


# ===========================================================================
# T4 -- a worker parked on disk keeps its containers
# ===========================================================================
def test_t4_detached_worker_on_disk_is_not_reaped(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "detached")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    intents = _reconcile_spy(monkeypatch)

    removed = sweep_stale_worker_containers(client, workers_dir)

    assert removed == []
    assert container.remove_calls == []
    # §I10: the spare is decided BEFORE the container listing.
    assert client.containers.list_calls == []
    # §7.2 T4: zero reap intents recorded.
    assert _reap_intents(intents) == []


# ===========================================================================
# T5 -- VACUITY partner: the same tree, one status change, is reaped
# ===========================================================================
def test_t5_crashed_worker_on_disk_is_reaped(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    intents = _reconcile_spy(monkeypatch)

    removed = sweep_stale_worker_containers(client, workers_dir)

    assert removed == ["c1"]
    assert container.remove_calls == [{"force": True}]
    reaps = _reap_intents(intents)
    assert len(reaps) == 1
    assert reaps[0].explicit is True
    assert reaps[0].reason == "stale_worker_cleanup"
    assert reaps[0].force is True


def test_t5_sweep_still_lists_by_exact_worker_label(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    client = FakeClient([_worker_container("c1", "sess-1:w1")])
    _reconcile_spy(monkeypatch)

    sweep_stale_worker_containers(client, workers_dir)

    assert len(client.containers.list_calls) == 1
    assert client.containers.list_calls[0]["all"] is True
    assert client.containers.list_calls[0]["filters"] == {
        "label": {_WORKER_LABEL: "sess-1:w1"}
    }


# ===========================================================================
# The on-disk status is handed to the existing primitive, not re-implemented.
# (Delegation is what keeps ONE gate; re-deriving "detached" here would fork it.)
# ===========================================================================
@pytest.mark.parametrize("runtime_status", ["detached", "failed", "ready", "paused"])
def test_sweep_hands_the_on_disk_status_to_cleanup(
    tmp_path, monkeypatch, runtime_status
):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", runtime_status)
    client = FakeClient([])
    calls = []
    monkeypatch.setattr(
        container_manager,
        "cleanup_stale_worker_containers",
        lambda *a, **k: (calls.append(_RecordedCall(a, k)), [])[1],
    )

    sweep_stale_worker_containers(client, workers_dir)

    assert len(calls) == 1
    identity, status = _identity_call(calls[0])
    assert identity == "sess-1:w1"
    assert status == runtime_status


class _RecordedCall:
    """A ``mock.call``-shaped recorder that does not require ``unittest.mock``."""

    def __init__(self, args, kwargs):
        self.args = tuple(args)
        self.kwargs = dict(kwargs)


# ===========================================================================
# The decision is driven by the FILE, not by the directory name / identity
# ===========================================================================
def test_decision_follows_the_status_file_not_the_directory(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    worker_dir = _session_scoped_worker(workers_dir, "sess-1", "w1", "detached")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == []
    assert container.remove_calls == []

    # Same directory, same identity, same container -- only the file changed.
    _write_status(worker_dir, _status_payload("sess-1", "failed"))

    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]
    assert container.remove_calls == [{"force": True}]


def test_catalogue_reads_runtime_status_not_status(tmp_path):
    """``status.json`` carries the frame under ``runtime_status``.

    A payload that spells the key ``status`` must NOT be read as a status --
    reading the wrong key is how a parked worker would look reapable (§I4).
    """
    workers_dir = tmp_path / "workers"
    worker_dir = workers_dir / "sess-1" / "w1"
    _write_status(worker_dir, {"session_id": "sess-1", "status": "detached"})

    catalogue = iter_persisted_workers(workers_dir)

    assert len(catalogue) == 1
    assert catalogue[0].identity == "sess-1:w1"
    assert catalogue[0].status == "unknown"


# ===========================================================================
# T6 -- "unknown" fails closed (§I4)
# ===========================================================================
def test_t6_absent_status_json_fails_closed(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    (workers_dir / "sess-1" / "w1").mkdir(parents=True)
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    calls = []
    monkeypatch.setattr(
        container_manager,
        "cleanup_stale_worker_containers",
        lambda *a, **k: (calls.append(_RecordedCall(a, k)), [])[1],
    )

    removed = sweep_stale_worker_containers(client, workers_dir)

    assert removed == []
    assert container.remove_calls == []
    # Not even consulted: passing "unknown" through would trip the helper's
    # ``worker_status is not None => detached=False`` branch and reap.
    assert calls == []
    assert client.containers.list_calls == []


def test_t6_corrupt_status_json_fails_closed(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    worker_dir = workers_dir / "sess-1" / "w1"
    worker_dir.mkdir(parents=True)
    (worker_dir / "status.json").write_text("{not json", encoding="utf-8")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == []
    assert container.remove_calls == []
    assert client.containers.list_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "sess-1"},
        {"session_id": "sess-1", "runtime_status": None},
        {"session_id": "sess-1", "runtime_status": ""},
    ],
)
def test_t6_statusless_payload_fails_closed(tmp_path, monkeypatch, payload):
    workers_dir = tmp_path / "workers"
    _write_status(workers_dir / "sess-1" / "w1", payload)
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == []
    assert container.remove_calls == []
    assert client.containers.list_calls == []


def test_t6_unknown_never_raises_and_keeps_a_corrupt_dir_out_of_the_way(
    tmp_path, monkeypatch
):
    """A corrupt directory must not stop its healthy sibling from being swept.

    "Fails closed" means *this* identity is spared -- not that the whole boot
    sweep aborts and every crashed worker's containers survive.
    """
    workers_dir = tmp_path / "workers"
    broken = workers_dir / "sess-1" / "broken"
    broken.mkdir(parents=True)
    (broken / "status.json").write_text("]]not json[[", encoding="utf-8")
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]


# ===========================================================================
# A worker that still has a live thread must not be swept at all
# ===========================================================================
def test_live_worker_identity_is_skipped(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    calls = []
    monkeypatch.setattr(
        container_manager,
        "cleanup_stale_worker_containers",
        lambda *a, **k: (calls.append(_RecordedCall(a, k)), [])[1],
    )

    removed = sweep_stale_worker_containers(
        client, workers_dir, live_identities={"sess-1:w1"}
    )

    assert removed == []
    assert calls == []
    assert container.remove_calls == []
    assert client.containers.list_calls == []


def test_live_worker_vacuity_partner_same_tree_without_the_live_hint(
    tmp_path, monkeypatch
):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(
        client, workers_dir, live_identities=()
    ) == ["c1"]
    assert container.remove_calls == [{"force": True}]


def test_live_identities_only_spare_the_named_worker(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    _session_scoped_worker(workers_dir, "sess-1", "w2", "failed")
    live = _worker_container("c1", "sess-1:w1")
    dead = _worker_container("c2", "sess-1:w2")
    client = FakeClient([live, dead])
    _reconcile_spy(monkeypatch)

    removed = sweep_stale_worker_containers(
        client, workers_dir, live_identities={"sess-1:w1"}
    )

    assert removed == ["c2"]
    assert live.remove_calls == []
    assert dead.remove_calls == [{"force": True}]


# ===========================================================================
# Both shipped directory layouts, and the label join key
# ===========================================================================
def test_session_scoped_layout_identity(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-9", "w1", "failed")
    container = _worker_container("c1", "sess-9:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    catalogue = iter_persisted_workers(workers_dir)
    assert [(w.session_id, w.worker_name, w.status) for w in catalogue] == [
        ("sess-9", "w1", "failed")
    ]

    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]


def test_legacy_layout_identity(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _legacy_worker(workers_dir, "w1", "failed", session_id="sess-1")
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    catalogue = iter_persisted_workers(workers_dir)
    assert [(w.session_id, w.worker_name, w.status) for w in catalogue] == [
        ("sess-1", "w1", "failed")
    ]

    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]


def test_legacy_layout_without_session_id_uses_the_unknown_owner(tmp_path, monkeypatch):
    """The label value is ``"<session_id or 'unknown'>:<worker_name>"``.

    A legacy ``workers/<name>/`` directory whose ``status.json`` carries no
    ``session_id`` must therefore be joined as ``unknown:<name>`` -- anything
    else silently loses the ownership link and spares a crashed worker forever.
    """
    workers_dir = tmp_path / "workers"
    _write_status(workers_dir / "w1", {"runtime_status": "failed"})
    container = _worker_container("c1", "unknown:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]
    assert container.remove_calls == [{"force": True}]


# ===========================================================================
# T8 -- exact-label discipline (§I5)
# ===========================================================================
def test_t8_sibling_worker_prefixed_label_is_not_attributed(tmp_path, monkeypatch):
    """``sess-1:worker2`` is not ``sess-1:worker`` -- and vice versa."""
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "worker", "failed")
    sibling = _worker_container("c-sibling", "sess-1:worker2")
    client = FakeClient([sibling])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == []
    assert sibling.remove_calls == []


def test_t8_exact_label_vacuity_partner(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "worker", "failed")
    own = _worker_container("c-own", "sess-1:worker")
    sibling = _worker_container("c-sibling", "sess-1:worker2")
    client = FakeClient([own, sibling])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == ["c-own"]
    assert own.remove_calls == [{"force": True}]
    assert sibling.remove_calls == []


def test_bare_worker_name_label_is_not_attributed(tmp_path, monkeypatch):
    """A stale bare-name label (no session) is not the owner identity."""
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    stale = _worker_container("c-stale", "w1")
    client = FakeClient([stale])
    _reconcile_spy(monkeypatch)

    assert sweep_stale_worker_containers(client, workers_dir) == []
    assert stale.remove_calls == []


# ===========================================================================
# §I11 -- half-deleted directories are legal
# ===========================================================================
def test_half_deleted_directory_is_still_catalogued(tmp_path, monkeypatch):
    """Missing ``context.json`` + live ``status.json`` is a teardown state."""
    workers_dir = tmp_path / "workers"
    worker_dir = _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    assert not (worker_dir / "context.json").exists()
    container = _worker_container("c1", "sess-1:w1")
    client = FakeClient([container])
    _reconcile_spy(monkeypatch)

    catalogue = iter_persisted_workers(workers_dir)
    assert [w.worker_name for w in catalogue] == ["w1"]

    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]


def test_empty_workers_dir_is_a_noop(tmp_path, monkeypatch):
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir()
    client = FakeClient([_worker_container("c1", "sess-1:w1")])
    _reconcile_spy(monkeypatch)

    assert iter_persisted_workers(workers_dir) == []
    assert sweep_stale_worker_containers(client, workers_dir) == []
    assert client.containers.list_calls == []


def test_absent_workers_dir_never_raises(tmp_path, monkeypatch):
    client = FakeClient([_worker_container("c1", "sess-1:w1")])
    _reconcile_spy(monkeypatch)

    assert iter_persisted_workers(tmp_path / "no-such-workers") == []
    assert sweep_stale_worker_containers(client, tmp_path / "no-such-workers") == []


# ===========================================================================
# §I9 -- the catalogue is a witness, not a worker
# ===========================================================================
def test_neither_catalogue_nor_sweep_constructs_a_worker_thread(
    tmp_path, monkeypatch
):
    from tools.workspace import worker_thread as worker_thread_module

    def _boom(*args, **kwargs):
        raise AssertionError(
            "the boot sweep constructed a WorkerThread (design §I9: the "
            "catalogue is a witness, not a worker)"
        )

    monkeypatch.setattr(worker_thread_module, "WorkerThread", _boom)
    workers_dir = tmp_path / "workers"
    _session_scoped_worker(workers_dir, "sess-1", "w1", "failed")
    client = FakeClient([_worker_container("c1", "sess-1:w1")])
    _reconcile_spy(monkeypatch)

    assert [w.identity for w in iter_persisted_workers(workers_dir)] == ["sess-1:w1"]
    assert sweep_stale_worker_containers(client, workers_dir) == ["c1"]
