"""R13 Step 5b — close the read-asymmetry between the gate path and the
config read path for an EMPTY stored ceiling.

Two routes must agree when nothing is stored:

* Gate path: ``security.security_gate.get_effective_permissions(session,
  caps, ceiling)`` — a ceiling of ``{}`` applies no ceiling, so the session
  grants stand.
* Config path: ``web_ui.backend.workspace_routes._resolve_workspace_permissions(
  cfg, purpose)`` — must return exactly what is stored, or ``{}`` when nothing
  is stored (NO read-time purpose-preset fallback).

Today the config path applies the purpose preset for an empty store, so the
two routes disagree.  After Step 5b the empty store must collapse to ``{}``
and the two routes must return the same effective permissions.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic")

try:
    from security.security_gate import get_effective_permissions
    from thoughtmachine.security import SessionPermissions
    from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
    from web_ui.backend.workspace_routes import _resolve_workspace_permissions
except Exception as exc:  # pragma: no cover - environment dependent
    pytest.skip(
        f"workspace_routes / gate chain could not be imported: {exc}",
        allow_module_level=True,
    )


def _session() -> SessionPermissions:
    return SessionPermissions(
        filesystem="write",
        network="write",
        container=True,
        git="write",
    )


def test_config_path_returns_empty_for_empty_stored_ceiling():
    """No stored ``permissions`` map -> the read path returns ``{}``.

    Reading a ceiling must return what is stored, or ``{}``; it must NOT
    synthesise a purpose preset at read time.
    """
    assert _resolve_workspace_permissions({"purpose": "coding"}, "coding") == {}


def test_gate_and_config_paths_agree_on_empty_stored_ceiling():
    """The two routes must produce the SAME effective permissions for an
    empty stored ceiling.

    The config-path result is fed to the gate as the ceiling; with an empty
    store the config path must yield ``{}`` so the gate result matches the
    gate result for a raw ``{}`` store.
    """
    cfg = {"purpose": "coding"}  # no stored "permissions" key
    resolved = _resolve_workspace_permissions(cfg, "coding")

    workspace = WorkspaceCapabilities()
    eff_from_config_path = get_effective_permissions(_session(), workspace, resolved)
    eff_from_raw_empty = get_effective_permissions(_session(), workspace, {})

    assert eff_from_config_path == eff_from_raw_empty
