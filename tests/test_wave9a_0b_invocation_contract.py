"""Wave 9a-0b contract tests: invocation unification of resolve_container_config.

These pin the RULED CONTRACT (spec
``.thoughtmachine/working_docs/wave9a-0b-spec.md``):

* every production caller passes ``use_disk=True`` — no production caller
  passes ``use_disk=False``;
* ``resolve_container_config(..., use_disk=True)`` REFUSES (never fabricates)
  when ``workspace_id`` is ``None`` — ``ContainerConfigError("workspace_id_required")``;
* ``session_id=None`` resolves to the workspace-only envelope FROM DISK, and the
  in-memory ``permissions`` mirror can never override disk truth;
* a store read failure fails CLOSED (deny-all), never the in-memory mirror;
* the shared adapter used by creation and verification resolves the same disk
  truth (no fabricated ``("none","ro")`` under a real envelope).

The disk seam is monkeypatched / pinned so the suite stays hermetic; no real
vault is read for the positive cases.
"""
from __future__ import annotations

import os
import pathlib
import re
import uuid

import security.security_gate as sg
from security.security_gate import (
    ContainerConfig,
    ContainerConfigError,
    resolve_container_config,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

import infra.container_registry as container_registry

# The REAL disk-read seam, captured at import time so a test can force it back
# on (defensive against any session-wide autouse monkeypatch).
_REAL_DISK_SEAM = sg._read_disk_permission_sources

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_EXCLUDE_DIRS = {".git", "__pycache__", ".thoughtmachine", "tests"}

# ``use_disk`` explicitly assigned a ``False`` value (the forbidden invocation).
_USE_DISK_FALSE_RE = re.compile(r"use_disk\s*=\s*False")
# Any ``use_disk = <value>`` assignment (inline kwarg or prose in a docstring).
_USE_DISK_ASSIGN_RE = re.compile(r"use_disk\s*=\s*(\w+)")


def _iter_production_python_files():
    """Yield every ``*.py`` file in the repo EXCEPT those under ``tests/``."""
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_EXCLUDE_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield pathlib.Path(dirpath) / name


# ─═ (a) zero production callers pass use_disk=False ═════════════════════════
def test_no_production_caller_passes_use_disk_false():
    offenders = []
    assigned_values = set()
    for path in _iter_production_python_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file is not our concern
            continue
        rel = path.relative_to(_REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), 1):
            if _USE_DISK_FALSE_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
            assigned_values.update(_USE_DISK_ASSIGN_RE.findall(line))

    assert offenders == [], (
        "production callers must never pass use_disk=False:\n"
        + "\n".join(offenders)
    )
    # Every explicit use_disk=<value> literal in production code is True.
    assert assigned_values <= {"True"}, (
        f"unexpected use_disk assignment values in production code: "
        f"{sorted(assigned_values - {'True'})}"
    )
    # Sanity: the scan actually saw the callers (guards against a broken scan).
    assert "True" in assigned_values, (
        "scan found no use_disk=True literal in production code — the "
        "invocation contract may have been refactored away"
    )


# ─═ (b) use_disk=True refuses a None workspace_id ═══════════════════════════
def test_use_disk_refuses_none_workspace_id():
    result = resolve_container_config(
        {},
        WorkspaceCapabilities.default(),
        "persistent",
        workspace_id=None,
        use_disk=True,
    )
    assert isinstance(result, ContainerConfigError)
    assert result.code == "workspace_id_required"


# ─═ (c1) session_id=None resolves the workspace-only envelope FROM DISK ═════
def test_none_session_id_resolves_workspace_envelope_from_disk(monkeypatch):
    # Disk grants network=write; the caller's in-memory mirror says "banned".
    monkeypatch.setattr(
        sg,
        "_read_disk_permission_sources",
        lambda ws, sid: ({"network": "write"}, None),
    )
    cfg = resolve_container_config(
        {"network": "banned"},
        WorkspaceCapabilities.default(),
        "persistent",
        session_id=None,
        workspace_id="ws-9a0b-disk-truth",
        use_disk=True,
    )
    assert isinstance(cfg, ContainerConfig)
    # DISK truth ("write" -> bridge) wins over the in-memory mirror ("banned").
    assert cfg.network_mode == "bridge"
    assert cfg.workspace_mode == "ro"


# ─═ (c2) a missing store fails CLOSED — never fabricated from memory ════════
def test_missing_disk_store_fails_closed_not_fabricated(monkeypatch):
    monkeypatch.setattr(sg, "_read_disk_permission_sources", _REAL_DISK_SEAM)
    workspace_id = "ws-9a0b-nonexistent-" + uuid.uuid4().hex
    result = resolve_container_config(
        {"network": "write"},  # would be "bridge" if fabricated from memory
        WorkspaceCapabilities.default(),
        "persistent",
        session_id=None,
        workspace_id=workspace_id,
        use_disk=True,
    )
    if isinstance(result, ContainerConfigError):
        return  # refusing is a valid fail-closed outcome
    assert isinstance(result, ContainerConfig)
    # Fail-closed deny-all, NOT the in-memory mirror's "write".
    assert result.network_mode == "none"
    assert result.workspace_mode == "ro"


# ─═ (d) the shared adapter resolves the SAME disk truth ═════════════════════
def test_shared_adapter_resolves_disk_truth(monkeypatch):
    monkeypatch.setattr(
        sg,
        "_read_disk_permission_sources",
        lambda ws, sid: ({"network": "write"}, None),
    )
    monkeypatch.setattr(
        sg,
        "get_workspace_capabilities",
        lambda ws: WorkspaceCapabilities.default(),
    )
    mode = container_registry._resolve_network_mode_via_gate(
        "ws-9a0b-adapter", {}, session_id=None
    )
    # Disk grants network=write -> bridge (not the fabricated "none").
    assert mode == "bridge"
