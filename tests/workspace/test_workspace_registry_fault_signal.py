"""Fault signal: ``list_workspaces`` must distinguish corruption from empty.

``WorkspaceRegistry.list_workspaces`` historically collapsed every failure
(malformed JSON, unreadable file, or valid-but-non-object JSON) into an empty
list — indistinguishable from the legitimate "no workspaces registered yet"
state.  Callers that fail *closed* on a fault (e.g. the startup orphan-resource
sweep) therefore could not tell a corrupt registry from an empty one.

These tests pin the new contract:
  * corruption      -> raise ``WorkspaceRegistryUnavailable`` (a ``RuntimeError``)
  * missing file     -> legitimate empty registry (``[]``)
  * valid empty ``{}`` -> legitimate empty registry (``[]``)

``WorkspaceRegistry`` and ``WorkspaceRegistryUnavailable`` are bound once at
module-import (collection) time so they always reference the *same* module
object's classes, even after other tests purge ``thoughtmachine.workspace_registry``
from ``sys.modules``.
"""

from __future__ import annotations

import logging

import pytest

from thoughtmachine.workspace_registry import (
    WorkspaceRegistry,
    WorkspaceRegistryUnavailable,
)

import infra.resource_container_manager as rcm
import web_ui.backend.server as server


# ── Corruption raises ───────────────────────────────────────────────────


def test_corrupt_json_raises(tmp_path):
    """Malformed JSON raises WorkspaceRegistryUnavailable."""
    reg_path = tmp_path / "state" / "workspace_registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text("not valid json{{{", encoding="utf-8")

    reg = WorkspaceRegistry(path=reg_path)
    with pytest.raises(WorkspaceRegistryUnavailable):
        reg.list_workspaces()


def test_non_object_json_raises(tmp_path):
    """Valid JSON that is not an object raises WorkspaceRegistryUnavailable."""
    reg_path = tmp_path / "state" / "workspace_registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text("[]", encoding="utf-8")

    reg = WorkspaceRegistry(path=reg_path)
    with pytest.raises(WorkspaceRegistryUnavailable):
        reg.list_workspaces()


def test_unreadable_path_raises(tmp_path):
    """An OSError on read (path is a directory) raises WorkspaceRegistryUnavailable."""
    reg_dir = tmp_path / "state" / "workspace_registry.json"
    reg_dir.mkdir(parents=True, exist_ok=True)  # a directory, not a file

    reg = WorkspaceRegistry(path=reg_dir)
    with pytest.raises(WorkspaceRegistryUnavailable):
        reg.list_workspaces()


# ── Legitimate empty states do NOT raise ────────────────────────────────


def test_valid_empty_object_is_empty_not_fault(tmp_path):
    """A valid empty object is a legitimate empty registry (no raise)."""
    reg_path = tmp_path / "state" / "workspace_registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text("{}", encoding="utf-8")

    reg = WorkspaceRegistry(path=reg_path)
    assert reg.list_workspaces() == []


def test_missing_file_is_empty_not_fault(tmp_path):
    """A missing registry file is the legitimate empty state (no raise)."""
    reg_path = tmp_path / "state" / "workspace_registry.json"
    assert not reg_path.exists()

    reg = WorkspaceRegistry(path=reg_path)
    assert reg.list_workspaces() == []


def test_missing_file_emits_log_record(tmp_path, caplog):
    """A missing registry file returns ``[]`` AND emits a log record.

    Regression guard for the observability gap: ``list_workspaces`` now calls
    ``_load_strict``, so the missing-file branch there must log — otherwise an
    absent file flowing through ``list_workspaces`` is silent and a reader
    cannot tell that ``[]`` means "the file was absent".
    """
    reg_path = tmp_path / "state" / "workspace_registry.json"
    assert not reg_path.exists()

    reg = WorkspaceRegistry(path=reg_path)
    with caplog.at_level(logging.INFO, logger="thoughtmachine.workspace_registry"):
        result = reg.list_workspaces()

    assert result == []
    matching = [
        rec
        for rec in caplog.records
        if rec.name == "thoughtmachine.workspace_registry"
    ]
    assert matching, "no log record emitted for the missing registry file"


# ── Integration: corrupt registry must skip the fail-closed sweep ───────


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


def test_corrupt_registry_real_sweep_skips_sweep(tmp_path, monkeypatch):
    """A REAL corrupt registry drives the sweep guard to skip the sweep.

    Unlike ``tests/docker/test_resource_sweep_registry_fault.py`` (which
    monkeypatches ``list_workspaces`` to raise), this wires a genuine
    ``WorkspaceRegistry`` whose backing file is corrupt through
    ``server.WorkspaceRegistry.get_default`` and asserts the fault propagates
    through the real code path to the fail-closed early return.
    """
    reg_path = tmp_path / "state" / "workspace_registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text("not valid json{{{", encoding="utf-8")

    registry = WorkspaceRegistry(path=reg_path)
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
    )

    sweep_calls, prune_calls = _install_spies(monkeypatch)
    events = _record_log(monkeypatch)

    # Must not raise: the sweep stays best-effort.
    server._sweep_orphan_resource_containers()

    assert sweep_calls == [], f"sweep called on corrupt registry: {sweep_calls}"
    assert prune_calls == [], f"prune called on corrupt registry: {prune_calls}"
    assert any(level == "WARNING" for level, _, _ in events), events
