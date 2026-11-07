"""B6 — workspace scoping of the active-worker listing (RED regression).

Shape (A) defect: a worker registered with an empty ``session_id`` (``""``) has
no workspace attribution, yet ``_collect_active_workers(ws_id)`` falls back to a
full-registry scan whose guard

    if sid and owner_ws != ws_id:
        continue

treats ``sid == ""`` as falsy, so the unscoped worker is appended for *every*
``ws_id``.  The hidden invariant — an empty-session worker must not be listed in
*any* workspace — is asserted here against the real production function.

``test_empty_session_worker_not_listed_in_any_workspace`` is the RED test; the
two characterisation tests pin the current 3-tuple registry surface (no
``workspace_id`` in the key) and the session-agnostic ``find_workers_by_name``.

Fakes follow ``tests/test_active_workers.py``: lightweight ``SimpleNamespace``
threads, with ``SessionRegistry.get_default`` and ``_get_worker_manager`` patched
at the module attribute the production code reads lazily at call time.
"""

from types import SimpleNamespace

import pytest

from tools.workspace.worker_registry import WorkerRegistry
from web_ui.backend import workspace_routes


# ── helpers (mirroring tests/test_active_workers.py) ────────────────────────


def _thread(worker_name, instance_id=1, status="ready", started_at=None,
            session_id="", last_heartbeat=None, container_id=None):
    """Dict-free stand-in for a live WorkerThread (no real thread)."""
    return SimpleNamespace(
        worker_name=worker_name,
        instance_id=instance_id,
        status=status,
        started_at=started_at,
        session_id=session_id,
        last_heartbeat=last_heartbeat,
        container_id=container_id,
    )


def _patch_session_registry(monkeypatch, sessions):
    """Patch ``SessionRegistry.get_default`` (imported lazily inside
    workspace_routes at call time, so patching the module attr is enough)."""
    import session.session_registry as sreg

    fake = SimpleNamespace(get_all=lambda: sessions)
    monkeypatch.setattr(
        sreg.SessionRegistry, "get_default", classmethod(lambda cls: fake)
    )


def _patch_manager(monkeypatch, manager):
    monkeypatch.setattr(workspace_routes, "_get_worker_manager", lambda: manager)


@pytest.fixture(autouse=True)
def _isolate_worker_registry():
    """Snapshot/restore the shared registry so no test pollutes its peers."""
    reg = WorkerRegistry.get_instance()
    with reg._registry_lock:
        before = dict(reg._worker_registry)
    yield
    with reg._registry_lock:
        reg._worker_registry.clear()
        reg._worker_registry.update(before)


# ── RED: an unscoped worker leaks into every workspace ────────────────────


def test_empty_session_worker_not_listed_in_any_workspace(monkeypatch):
    """An empty-session worker must not appear for *any* ``ws_id``.

    Forces the fallback branch of ``_collect_active_workers`` (no open session
    maps to the workspace) and drives the real production function against the
    real (shared) WorkerRegistry.
    """
    reg = WorkerRegistry.get_instance()
    reg.register_worker("", "ghost", _thread("ghost", session_id=""))

    manager = SimpleNamespace(_registry=reg)
    _patch_manager(monkeypatch, manager)
    _patch_session_registry(monkeypatch, {})  # no open session -> fallback path

    ws_a = workspace_routes._collect_active_workers("ws-A")
    ws_b = workspace_routes._collect_active_workers("ws-B")

    names_a = [e["worker_name"] for e in ws_a]
    names_b = [e["worker_name"] for e in ws_b]
    assert "ghost" not in names_a, (
        "empty-session worker leaked into workspace ws-A active workers: "
        f"{names_a}"
    )
    assert "ghost" not in names_b, (
        "empty-session worker leaked into workspace ws-B active workers: "
        f"{names_b}"
    )


# ── characterisation: current registry surface ──────────────────────────────


def test_registry_key_has_no_workspace_scope():
    """Registry keys are 3-tuples (session_id, worker_name, instance_id) with
    no workspace component — the missing scope that shape (A) exposes."""
    reg = WorkerRegistry.get_instance()
    reg.register_worker("sess-2", "scoped",
                        _thread("scoped", session_id="sess-2"), instance_id=1)

    all_workers = reg.get_all_workers()
    key = ("sess-2", "scoped", 1)
    assert key in all_workers
    assert key == ("sess-2", "scoped", 1)
    assert len(key) == 3  # (session_id, worker_name, instance_id) — no ws_id


def test_find_workers_by_name_is_session_agnostic():
    """``find_workers_by_name`` matches on the worker name alone, so the same
    name under two session ids yields both entries."""
    reg = WorkerRegistry.get_instance()
    reg.register_worker("sess-a", "dup", _thread("dup", session_id="sess-a"))
    reg.register_worker("sess-b", "dup",
                        _thread("dup", session_id="sess-b"), instance_id=1)

    found = reg.find_workers_by_name("dup")
    assert len(found) == 2
    assert {sid for sid, _t in found} == {"sess-a", "sess-b"}
