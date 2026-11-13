"""Category denials name the layer that actually denies the call.

The historical denial text always said ``but session allows <category>:<value>``
-- which turned self-contradictory the moment the effective value was a REFUSAL
(``host_bash:banned``): a session that allows ``banned`` allows nothing, and
when a workspace layer (the ``allow_host_resources`` host-resource policy, or
the workspace permission ceiling) is what forced the value, the sentence also
charged the wrong layer for the denial.

Contract pinned here:

* a refusal grant is rendered ``but the effective grant is <category>:<value>
  (denied by: <layer>)`` -- never "session allows";
* the layer names the session grant, the workspace ceiling (with its level) or
  the workspace host-resource policy -- whichever is the value's real cause;
* a fail-closed profile keeps naming its CAUSE (``(fail-closed: <cause>)``)
  instead of inventing a layer;
* a lower-but-usable grant (``read`` where ``write`` is required) keeps the
  historical wording byte-identical;
* the same wording is mirrored by ``security.sandboxed_execution``.
"""

from __future__ import annotations

import json
import sys

# ── Fix sys.path for the Docker sandbox (same preamble as test_security_gate.py) ──
_bad_prefix = "/workspace/tests"
sys.path = [p for p in sys.path if not p.startswith(_bad_prefix)]
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _stubs_path)
sys.path.insert(1, "/workspace")

import pytest

from agent.events import NullEventBus
from security.security_gate import (
    _CeilingAnnotatedDict,
    _FailClosedReasonDict,
    _denial_clause,
    check_required_categories,
    get_effective_permissions,
)
from security.sandboxed_execution import SandboxedExecution
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

_PERMISSIVE_CAPS = WorkspaceCapabilities()  # fully permissive defaults


def _run_gate(required, effective):
    """Run a single-requirement gate check through the worker NullEventBus."""
    return check_required_categories(
        required,
        effective,
        tool_name="ProbeTool",
        tool_args={},
        description="probe",
        event_bus=NullEventBus(),
        is_worker_context=True,
    )


# ──────────────────────────────────────────────────────────────────────────
# 1. A refusal grant blames the session grant -- and never "allows" it.
# ──────────────────────────────────────────────────────────────────────────
class TestSessionGrantRefusal:
    def test_banned_grant_names_the_session_grant(self):
        eff = get_effective_permissions(
            SessionPermissions(filesystem="banned"), _PERMISSIVE_CAPS
        )
        assert type(eff) is dict, "plain dict on the ordinary path"

        ok, msg = _run_gate(["filesystem:write"], eff)
        assert ok is False
        assert msg == (
            "Permission denied: Tool requires filesystem:write, "
            "but the effective grant is filesystem:banned "
            "(denied by: session grant)"
        )
        # The self-contradictory clause is gone.
        assert "session allows" not in msg
        assert "workspace ceiling" not in msg
        assert "fail-closed" not in msg

    def test_lower_usable_grant_keeps_the_historical_wording(self):
        # 'read' is a usable grant, not a refusal: the session really does
        # allow the category at read, so the historical clause stays truthful
        # (and byte-identical).
        eff = get_effective_permissions(
            SessionPermissions(filesystem="read"), _PERMISSIVE_CAPS
        )
        ok, msg = _run_gate(["filesystem:write"], eff)
        assert ok is False
        assert msg == (
            "Permission denied: Tool requires filesystem:write, "
            "but session allows filesystem:read"
        )


# ──────────────────────────────────────────────────────────────────────────
# 2. The workspace ceiling is named (with its level) when it is the cause.
# ──────────────────────────────────────────────────────────────────────────
class TestWorkspaceCeilingLayer:
    def test_ceiling_caused_refusal_names_the_ceiling(self):
        # A ceiling-annotated mapping whose pre-ceiling grant satisfied the
        # requirement: removing the ceiling would flip the denial into an
        # allow, so the ceiling -- not the session grant -- is the denier.
        annotated = _CeilingAnnotatedDict({"filesystem": "banned"})
        annotated._ceiling_annotations["filesystem"] = {
            "pre": "write",
            "level": "banned",
        }

        clause = _denial_clause("filesystem", "banned", annotated, "banned")
        assert clause == (
            "but the effective grant is filesystem:banned "
            "(denied by: workspace ceiling (banned))"
        )


# ──────────────────────────────────────────────────────────────────────────
# 3. The workspace host-resource policy (allow_host_resources) is named when
#    IT is what forced host_bash to banned.
# ──────────────────────────────────────────────────────────────────────────
class TestHostResourcePolicyLayer:
    def test_policy_veto_is_named_when_it_forces_the_value(self, monkeypatch):
        monkeypatch.setattr(
            "tools.host_resource_policy.workspace_allows_host_resources",
            lambda _ws: False,
        )
        eff = get_effective_permissions(
            SessionPermissions(host_bash="allow"),
            _PERMISSIVE_CAPS,
            workspace_id="ws-veto",
        )
        assert eff["host_bash"] == "banned"
        # Lossless: plain JSON content, no attribute leakage, host_bash is
        # the only category the veto touched.
        assert dict(eff)["host_bash"] == "banned"
        assert all(
            isinstance(v, (str, bool)) for v in dict(eff).values()
        )
        assert json.loads(json.dumps(eff))["host_bash"] == "banned"

        ok, msg = _run_gate(["host_bash:allow"], eff)
        assert ok is False
        assert msg == (
            "Permission denied: Tool requires host_bash:allow, "
            "but the effective grant is host_bash:banned "
            "(denied by: workspace host-resource policy)"
        )
        # Neither the session grant nor the permissions ceiling is blamed.
        assert "session allows" not in msg
        assert "workspace ceiling" not in msg

    def test_policy_veto_does_not_steal_blame_from_a_refusing_session(
        self, monkeypatch
    ):
        # The session grant already refuses host_bash: the veto changed
        # nothing, so the profile stays a plain dict and the session grant
        # keeps the blame (first consulted layer wins).
        monkeypatch.setattr(
            "tools.host_resource_policy.workspace_allows_host_resources",
            lambda _ws: False,
        )
        eff = get_effective_permissions(
            SessionPermissions(host_bash="banned"),
            _PERMISSIVE_CAPS,
            workspace_id="ws-veto",
        )
        assert type(eff) is dict

        ok, msg = _run_gate(["host_bash:allow"], eff)
        assert ok is False
        assert msg == (
            "Permission denied: Tool requires host_bash:allow, "
            "but the effective grant is host_bash:banned "
            "(denied by: session grant)"
        )


# ──────────────────────────────────────────────────────────────────────────
# 4. A fail-closed profile names its CAUSE, not a fabricated layer.
# ──────────────────────────────────────────────────────────────────────────
class TestFailClosedProfile:
    def test_fail_closed_cause_is_named_instead_of_a_layer(self):
        eff = _FailClosedReasonDict({"mcp": "banned"})
        eff._fail_closed_reason = "_ResolveBoom: simulated resolve store failure"

        ok, msg = _run_gate(["mcp:connect"], eff)
        assert ok is False
        assert "but the effective grant is mcp:banned" in msg
        assert "(fail-closed: _ResolveBoom: simulated resolve store failure)" in msg
        assert "(denied by:" not in msg
        assert "session allows" not in msg


# ──────────────────────────────────────────────────────────────────────────
# 5. The sandboxed-execution mirror uses the same wording.
# ──────────────────────────────────────────────────────────────────────────
class TestSandboxedExecutionMirror:
    def test_refusal_names_the_session_grant(self):
        with pytest.raises(PermissionError) as excinfo:
            SandboxedExecution(
                session_permissions={"git": "banned"}
            ).run(["git", "status"], required_category="git:read")
        msg = str(excinfo.value)
        assert msg == (
            "Permission denied: requires git:read, "
            "but the effective grant is git:banned (denied by: session grant)"
        )
        assert "session allows" not in msg

    def test_lower_usable_grant_keeps_the_historical_wording(self):
        with pytest.raises(PermissionError) as excinfo:
            SandboxedExecution(
                session_permissions={"git": "read"}
            ).run(["git", "push"], required_category="git:write")
        assert str(excinfo.value) == (
            "Permission denied: requires git:write, "
            "but session allows git:read"
        )
