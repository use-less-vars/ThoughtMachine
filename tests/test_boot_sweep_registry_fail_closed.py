"""Startup (boot) fail-closed guard for the orphan resource-container sweep.

Bug: ``bug/boot-sweep-registry-empty-not-fail-closed`` — the boot-time sibling
of the merged periodic-sweep fix.

``web_ui.backend.server.lifespan`` runs three one-shot *startup* container
sweeps.  The destructive one, ``_sweep_orphan_resource_containers``, reads the
registered workspace ids and delegates to
``infra.resource_container_manager.sweep_stale_resource_containers(ids)``, whose
callee force-removes EVERY ``thoughtmachine.resource`` container whose workspace
id is ``not in`` the (possibly EMPTY) registered set.

Before this fix the startup pass trusted an invariant: the project root is
auto-registered (``WorkspaceRegistry.register_by_root``) a few lines earlier, so
its registry read could never be empty.  But that auto-registration is wrapped
in a best-effort ``try/except`` that LOGS and CONTINUES on failure.  When it
fails non-fatally the subsequent registry read can legitimately return ``[]``,
and the empty id list turns the destructive sweep into a wipe of every live
resource container.  (A registry read *fault* was already fail-closed inside
``_sweep_orphan_resource_containers`` — see
``tests/docker/test_resource_sweep_registry_fault.py``; the EMPTY, no-exception
case was not.)  The two sibling startup sweeps already degrade safely
(TTL-only / no-op) on a bad registry read; the resource sweep did not.

These tests drive the REAL boot path (``async with server.lifespan(server.app)``)
and pin the fail-closed boot guard:

* an EMPTY registry read at boot must NOT invoke the destructive sweep (RED);
* a registry read FAULT at boot must NOT invoke it either (RED);
* a non-empty registry must still invoke it (guard is not a blanket disable);
* the empty-registry skip must be observable in the log (RED).

No Docker daemon is required: ``_sweep_orphan_resource_containers`` is replaced
with a call-recording spy and the two sibling sweeps are neutralised, so the
real boot path performs no docker/filesystem work.
"""

import asyncio

import pytest  # noqa: F401  (kept for symmetry with sibling sweep tests)

import web_ui.backend.server as server
from thoughtmachine.workspace_registry import WorkspaceRegistryUnavailable


class _FakeEntry:
    def __init__(self, workspace_id):
        self.id = workspace_id


class _FakeRegistry:
    """Minimal stand-in for ``WorkspaceRegistry``.

    ``entries`` is either a list of workspace ids (``list_workspaces`` returns
    fresh ``_FakeEntry`` objects) or an exception instance (``list_workspaces``
    raises it).  ``register_by_root`` always raises so the lifespan's
    best-effort auto-registration takes its non-fatal failure branch — the
    precondition under which the boot vulnerability is reachable.
    """

    def __init__(self, entries):
        self._entries = entries

    def list_workspaces(self):
        if isinstance(self._entries, BaseException):
            raise self._entries
        return [_FakeEntry(wid) for wid in self._entries]

    def register_by_root(self, root):  # pragma: no cover - failure path
        raise RuntimeError(f"registry unavailable for {root}")


def _fake_registry(monkeypatch, entries):
    """Fake ``server.WorkspaceRegistry.get_default`` for the whole boot pass."""
    registry = _FakeRegistry(entries)
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
    )
    return registry


def _record_log(monkeypatch):
    """Capture ``server.log(level, category, message, ...)`` calls.

    ``server.log`` is the unified ``agent.logging.log`` facade: it prints to
    stderr and forwards to a JSONL ``AgentLogger``, emitting NO stdlib
    ``logging`` records, so ``caplog`` cannot see it.  Monkeypatching the
    module-level ``log`` is the same seam the sibling sweep tests use.
    """
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(server, "log", recorder)
    return events


def _neutralise_sibling_sweeps(monkeypatch):
    """Keep the two non-resource boot sweeps off the docker daemon/filesystem."""
    monkeypatch.setattr(server, "_sweep_exited_workspace_containers", lambda: None)
    monkeypatch.setattr(server, "_sweep_orphan_container_records", lambda: None)


def _spy_resource_sweep(monkeypatch):
    """Replace the boot resource sweep with a call-recording spy."""
    calls = []

    def spy():
        calls.append(True)

    monkeypatch.setattr(server, "_sweep_orphan_resource_containers", spy)
    return calls


def _run_boot():
    """Drive the REAL lifespan startup/shutdown once."""

    async def scenario():
        async with server.lifespan(server.app):
            pass

    asyncio.run(scenario())


def test_boot_empty_registry_skips_resource_sweep(monkeypatch):
    """An EMPTY registry read at boot must NOT invoke the destructive sweep.

    RED on the unfixed tree: ``lifespan`` calls
    ``_sweep_orphan_resource_containers()`` unconditionally, so the spy records
    a call even though the registry is empty.
    """
    _fake_registry(monkeypatch, [])
    _neutralise_sibling_sweeps(monkeypatch)
    calls = _spy_resource_sweep(monkeypatch)
    _record_log(monkeypatch)

    _run_boot()

    assert calls == [], f"boot resource sweep ran on empty registry: {calls}"


def test_boot_registry_fault_skips_resource_sweep(monkeypatch):
    """A registry read FAULT at boot must NOT invoke the destructive sweep.

    RED on the unfixed tree: the sweep is invoked unconditionally at boot.
    """
    _fake_registry(monkeypatch, WorkspaceRegistryUnavailable("registry corrupt"))
    _neutralise_sibling_sweeps(monkeypatch)
    calls = _spy_resource_sweep(monkeypatch)
    _record_log(monkeypatch)

    _run_boot()

    assert calls == [], f"boot resource sweep ran on registry fault: {calls}"


def test_boot_non_empty_registry_still_sweeps(monkeypatch):
    """A non-empty registry at boot still invokes the resource sweep.

    Pins that the guard is not a blanket disable.
    """
    _fake_registry(monkeypatch, ["ws-1"])
    _neutralise_sibling_sweeps(monkeypatch)
    calls = _spy_resource_sweep(monkeypatch)
    _record_log(monkeypatch)

    _run_boot()

    assert calls == [True], f"boot resource sweep did not run: {calls}"


def test_boot_empty_registry_skip_is_observable(monkeypatch):
    """The boot registry-empty skip is observable in the log (WARNING).

    The skip is invisible to the sweep spy, so a log line is the ONLY signal
    that the guard fired.

    RED on the unfixed tree: no line names an (orphan-resource) *skip*.
    """
    _fake_registry(monkeypatch, [])
    _neutralise_sibling_sweeps(monkeypatch)
    _spy_resource_sweep(monkeypatch)
    events = _record_log(monkeypatch)

    _run_boot()

    skip_events = [
        (level, message)
        for level, _category, message in events
        if "orphan-resource" in message and "skipped" in message
    ]
    assert skip_events, f"no observable skip event: {events}"
    assert any(level == "WARNING" for level, _ in skip_events), skip_events
