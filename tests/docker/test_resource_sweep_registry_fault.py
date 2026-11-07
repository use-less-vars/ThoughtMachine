"""Fail-closed guard: the startup resource sweep on a registry read *fault*.

``web_ui.backend.server._sweep_orphan_resource_containers`` reads the
registered workspace ids and delegates to
``infra.resource_container_manager.sweep_stale_resource_containers(ids)``.

That callee builds ``registered = {str(ws) for ws in (ids or [])}`` and then
force-removes every ``thoughtmachine.resource`` container for which
``str(ws_id) not in registered``.  Against an EMPTY id list the membership test
is True for *every* container, so a registry fault turned into an empty id list
would over-broadly delete all live resource containers.

A registry read FAULT (exception) must therefore skip the sweep entirely, while
a clean, successful read that legitimately returns zero workspaces must still
sweep.  These tests pin that distinction.

No Docker daemon required — the sweep/prune helpers of
``infra.resource_container_manager`` are replaced with spies.
"""

import infra.resource_container_manager as rcm
import web_ui.backend.server as server


class _FakeEntry:
    def __init__(self, workspace_id, root_path="/root"):
        self.id = workspace_id
        self.root_path = root_path


def _record_log(monkeypatch):
    """Capture ``server.log(level, category, message, ...)`` calls."""
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(server, "log", recorder)
    return events


def _install_spies(monkeypatch):
    """Install recording spies for the rcm sweep + prune helpers."""
    sweep_calls = []
    prune_calls = []

    def _sweep(ids):
        sweep_calls.append(list(ids))
        return {"removed": 0, "skipped_in_use": 0, "detail": ""}

    def _prune():
        prune_calls.append(True)
        return {"removed_images": [], "remaining_containers": 0, "detail": ""}

    monkeypatch.setattr(rcm, "sweep_stale_resource_containers", _sweep)
    monkeypatch.setattr(rcm, "prune_unreferenced_resource_images", _prune)
    return sweep_calls, prune_calls


def _fake_registry(monkeypatch, entry_ids):
    registry = server.WorkspaceRegistry.__new__(server.WorkspaceRegistry)
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
    )
    monkeypatch.setattr(
        server.WorkspaceRegistry,
        "list_workspaces",
        staticmethod(lambda: [_FakeEntry(wid) for wid in entry_ids]),
    )
    return registry


def test_registry_fault_skips_sweep(monkeypatch):
    """A registry read exception must NOT trigger an empty-id (over-broad) sweep.

    RED on the unfixed tree: the ``except`` branch sets ``ids = []`` and calls
    the sweep anyway, so the spy is hit with ``[]``.
    """

    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(server.WorkspaceRegistry, "get_default", staticmethod(_boom))

    sweep_calls, prune_calls = _install_spies(monkeypatch)
    events = _record_log(monkeypatch)

    # Must not raise: the sweep stays best-effort.
    server._sweep_orphan_resource_containers()

    assert sweep_calls == [], f"sweep called on registry fault: {sweep_calls}"
    assert prune_calls == [], f"prune called on registry fault: {prune_calls}"
    assert any(level == "WARNING" for level, _, _ in events), events


def test_registry_fault_in_list_workspaces_skips_sweep(monkeypatch):
    """Same guard when only ``list_workspaces`` raises."""

    registry = server.WorkspaceRegistry.__new__(server.WorkspaceRegistry)
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
    )

    def _boom():
        raise RuntimeError("registry file unreadable")

    monkeypatch.setattr(
        server.WorkspaceRegistry, "list_workspaces", staticmethod(_boom)
    )

    sweep_calls, prune_calls = _install_spies(monkeypatch)
    events = _record_log(monkeypatch)

    server._sweep_orphan_resource_containers()

    assert sweep_calls == []
    assert prune_calls == []
    assert any(level == "WARNING" for level, _, _ in events), events


def test_valid_registry_still_sweeps(monkeypatch):
    """A successful non-empty registry read still sweeps the real ids."""

    _fake_registry(monkeypatch, ["ws-1"])
    sweep_calls, prune_calls = _install_spies(monkeypatch)
    _record_log(monkeypatch)

    server._sweep_orphan_resource_containers()

    assert sweep_calls == [["ws-1"]]
    assert prune_calls == [True]


def test_valid_empty_registry_still_sweeps(monkeypatch):
    """A clean, successful read returning zero workspaces is NOT a fault.

    It must still sweep (with an empty id list) — the sweep reaps orphans left
    by deleted workspaces.  This pins the fault-empty vs valid-empty distinction.
    """

    _fake_registry(monkeypatch, [])
    sweep_calls, prune_calls = _install_spies(monkeypatch)
    _record_log(monkeypatch)

    server._sweep_orphan_resource_containers()

    assert sweep_calls == [[]]
    assert prune_calls == [True]
