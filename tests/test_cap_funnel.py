"""Pin for the R13 step-5 cap funnel (narrowed).

The single public cap funnel is ``security.security_gate.cap`` with the
canonical (session-first) signature::

    cap(session_grants: dict, ceiling_map: dict) -> effective_dict

It is a pure delegator onto the existing cap primitive
``apply_workspace_ceiling(ceiling_map, session_grants)`` -- no file reads,
no folding, no preset fallback (the caller decides folding).  The two
argument orders differ, so this file pins the swap explicitly.

Two properties are pinned:

1. ``cap(g, c) == apply_workspace_ceiling(c, g)`` for a table of inputs
   (direct equivalence; RED on the pre-funnel tree where ``cap`` does not
   exist yet).
2. ``get_effective_permissions`` (the security gate) routes its ceiling
   step through the ``cap`` funnel -- i.e. the delegator re-point is real,
   not cosmetic.  Reverting the gate to call ``apply_workspace_ceiling``
   directly makes this test fail.
"""
from __future__ import annotations

import security.security_gate as sg
from security.security_gate import apply_workspace_ceiling, cap
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities


# (session_grants, ceiling_map) -- covers the docker->container alias, the
# host_bash / mcp own-scale tables, an empty ceiling (no-op passthrough)
# and ordinary string resources.
_CASES = [
    ({"filesystem": "write", "network": "full"}, {"filesystem": "read"}),
    ({"container": True, "mcp": "full"}, {"docker": "write"}),
    ({"host_bash": "allow"}, {"host_bash": "ask"}),
    ({"filesystem": "write"}, {}),
    ({"git": "write", "network": "outbound"}, {"network": "banned"}),
    ({"mcp": "full"}, {"mcp": "connect"}),
    ({"filesystem": "read", "git": "write"}, {"filesystem": "write"}),
]


def test_cap_matches_apply_workspace_ceiling_argument_swap():
    """cap(g, c) is exactly apply_workspace_ceiling(c, g)."""
    for session_grants, ceiling_map in _CASES:
        expected = apply_workspace_ceiling(ceiling_map, session_grants)
        got = cap(session_grants, ceiling_map)
        assert got == expected, (session_grants, ceiling_map, got, expected)


def test_gate_routes_ceiling_step_through_cap(monkeypatch):
    """get_effective_permissions must call the ``cap`` funnel (teeth)."""
    calls = []
    real = sg.apply_workspace_ceiling

    def _spy(session_grants, ceiling_map):
        # Record the argument ORDER the gate used, then behave identically.
        calls.append((dict(session_grants), dict(ceiling_map)))
        return real(ceiling_map, session_grants)

    monkeypatch.setattr(sg, "cap", _spy)

    session = SessionPermissions(filesystem="write")
    workspace = WorkspaceCapabilities.default()
    ceiling = {"filesystem": "read"}

    eff = sg.get_effective_permissions(session, workspace, ceiling)

    assert calls, "gate did not route the ceiling step through cap()"
    grants, cmap = calls[0]
    # Canonical order: session grants FIRST, ceiling map SECOND.
    assert cmap == ceiling
    assert grants.get("filesystem") == "write"
    assert eff["filesystem"] == "read"
