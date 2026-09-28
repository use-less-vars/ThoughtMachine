"""Periodic-sweep registry re-check (bug/periodic-sweep-no-registry-recheck).

``web_ui.backend.server._periodic_container_sweep_loop`` re-runs
``_run_container_sweeps`` on a fixed interval.  Before the fix the periodic
path re-ran ``_sweep_orphan_resource_containers`` unconditionally.  That
wrapper reads the workspace registry and delegates to
``infra.resource_container_manager.sweep_stale_resource_containers(ids)``,
whose callee force-removes EVERY ``thoughtmachine.resource`` container whose
workspace id is not in the (possibly EMPTY) registered set.  A transiently
empty (or faulting) registry at a periodic tick therefore wiped every live
resource container.

The one-shot *startup* pass already mitigates this: a registry read fault is
FAIL-CLOSED (``_sweep_orphan_resource_containers`` returns early) and the
project root is auto-registered before the sweep runs, so the startup read is
never empty in practice.  The *periodic* tick had neither protection: it never
re-read/validated the registry before delegating.

These tests pin the periodic-only guard:

* an EMPTY registry at tick time must skip the orphan-resource sweep AND its
  image prune entirely (the destructive no-op-safe sweep is NOT invoked);
* a non-empty registry must still sweep + prune (the guard is not a
  blanket disable);
* the skip must be observable in the log (it is silent to the sweep spies).

No Docker daemon is required: the ``infra.resource_container_manager`` helpers
are replaced with spies and the workspace registry is faked in-process.
"""

import pytest  # noqa: F401  (kept for symmetry with sibling sweep tests)

import infra.resource_container_manager as rcm
import web_ui.backend.server as server


class _FakeEntry:
    def __init__(self, workspace_id, root_path="/root"):
        self.id = workspace_id
        self.root_path = root_path


def _record_log(monkeypatch):
    """Capture ``server.log(level, category, message, ...)`` calls.

    ``server.log`` is the unified ``agent.logging.log`` facade, which prints to
    stderr and forwards to a JSONL ``AgentLogger`` — it emits NO stdlib
    ``logging`` records, so pytest's ``caplog`` cannot see it.  Monkeypatching
    the module-level ``log`` is the same seam the sibling sweep tests use
    (``tests/docker/test_resource_sweep_registry_fault.py``).
    """
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
    """Fake ``server.WorkspaceRegistry.get_default().list_workspaces()``."""
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


def _neutralise_sibling_sweeps(monkeypatch):
    """Keep the two non-resource sweeps off the docker daemon/filesystem."""
    monkeypatch.setattr(server, "_sweep_exited_workspace_containers", lambda: None)
    monkeypatch.setattr(server, "_sweep_orphan_container_records", lambda: None)


def test_periodic_tick_empty_registry_skips_resource_sweep(monkeypatch):
    """A periodic tick that reads an EMPTY registry must NOT sweep resources.

    RED on the unfixed tree: ``_run_container_sweeps`` delegates straight to
    ``_sweep_orphan_resource_containers``, which passes the empty id list to a
    callee that force-removes every resource container.
    """
    _fake_registry(monkeypatch, [])
    _neutralise_sibling_sweeps(monkeypatch)
    sweep_calls, prune_calls = _install_spies(monkeypatch)
    _record_log(monkeypatch)

    server._run_container_sweeps()

    assert sweep_calls == [], f"resource sweep ran on empty registry: {sweep_calls}"
    assert prune_calls == [], f"image prune ran on empty registry: {prune_calls}"


def test_periodic_tick_non_empty_registry_still_sweeps(monkeypatch):
    """A periodic tick with a non-empty registry still sweeps + prunes."""
    _fake_registry(monkeypatch, ["ws-1"])
    _neutralise_sibling_sweeps(monkeypatch)
    sweep_calls, prune_calls = _install_spies(monkeypatch)
    _record_log(monkeypatch)

    server._run_container_sweeps()

    assert sweep_calls == [["ws-1"]]
    assert prune_calls == [True]


def test_periodic_tick_empty_registry_skip_is_observable(monkeypatch):
    """The registry-empty skip is observable in the log (WARNING).

    The skip is deliberately invisible to the sweep spies, so a log line is the
    ONLY signal that the guard fired.  Deleting that log line makes this test
    fail — see the mutation note in the result doc.

    RED on the unfixed tree: no line names an (orphan-resource) *skip*.
    """
    _fake_registry(monkeypatch, [])
    _neutralise_sibling_sweeps(monkeypatch)
    _install_spies(monkeypatch)
    events = _record_log(monkeypatch)

    server._run_container_sweeps()

    skip_events = [
        (level, message)
        for level, _category, message in events
        if "orphan-resource" in message and "skipped" in message
    ]
    assert skip_events, f"no observable skip event: {events}"
    assert any(level == "WARNING" for level, _ in skip_events), skip_events
