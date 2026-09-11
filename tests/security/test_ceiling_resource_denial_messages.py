"""
Resource-bound and execution-path denials carry "(workspace ceiling: ...)".

When a hidden-resource gate (``check_requires_resource``), the container git
gate inside ``GitReadTool._exec_container_raw``, or the sandbox execution
permission check (``SandboxedExecution``) denies because the WORKSPACE
permission ceiling capped a session grant, the raised message/denial must
name the ceiling level.  Session-level denials (no ceiling involved, or an
equal ceiling that did not change anything) must stay byte-identical to the
historical text -- no "workspace ceiling" phrase.

Mechanism under test: ``get_effective_permissions`` returns a plain-dict
subclass (``_CeilingAnnotatedDict``) carrying ``_ceiling_annotations``
{category: {"pre": <pre-ceiling value>, "level": <label>}} for exactly the
categories the ceiling restricted; each denial site consults
``_ceiling_denial_note`` and appends the suffix only when the pre-ceiling
value alone satisfies the requirement.

Full battery (this file + the neighbouring ceiling suites) is green:

    python -m pytest -q tests/security/test_ceiling_resource_denial_messages.py \
        tests/security/test_ceiling_prompt_gating.py \
        tests/security/test_ceiling_denial_messages.py \
        tests/security/test_security_gate.py tests/test_security_gate_disk.py \
        tests/test_workspace_permissions.py tests/test_tool_executor.py \
        tests/test_worker_disk_mode_inheritance.py \
        tests/test_host_bash_tool.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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

from security.security_gate import (
    REASON_SESSION_EXCEEDS_WORKSPACE_CEILING,
    ceiling_contradictions,
    check_requires_resource,
    get_effective_permissions,
)
from security.sandboxed_execution import SandboxedExecution
from tools.git_info_tool import GitReadTool
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

_PERMISSIVE_CAPS = WorkspaceCapabilities()  # fully permissive defaults


def _ceiling_banned_git_eff():
    """Session git:write grant capped to banned by the workspace ceiling."""
    eff = get_effective_permissions(
        SessionPermissions(git="write"),
        _PERMISSIVE_CAPS,
        {"git": "banned"},
    )
    assert eff["git"] == "banned"
    assert getattr(eff, "_ceiling_annotations", {}).get("git") == {
        "pre": "write",
        "level": "banned",
    }
    return eff


# ──────────────────────────────────────────────────────────────────────────
# 1. Resource-bound gate (check_requires_resource)
# ──────────────────────────────────────────────────────────────────────────
class TestResourceGateDenialMessages:
    def test_ceiling_caused_resource_denial_names_ceiling(self):
        eff = _ceiling_banned_git_eff()
        ok, msg = check_requires_resource("git", eff, tool_name="GitReadTool")
        assert ok is False
        assert msg == (
            "Permission denied: Session permission for git is write, but "
            "workspace ceiling is banned. The session exceeds the ceiling. "
            "Correct either the workspace ceiling or the session grant."
        )
        # First-class structured reason (not a string match on the message).
        assert ceiling_contradictions(eff) == [
            {
                "resource": "git",
                "session_value": "write",
                "workspace_value": "banned",
                "reason": REASON_SESSION_EXCEEDS_WORKSPACE_CEILING,
                "guidance": "Correct either the workspace ceiling or the session grant.",
            }
        ]
        assert json.loads(json.dumps(msg)) == msg

    def test_session_grant_denial_without_ceiling_is_unchanged(self):
        eff = get_effective_permissions(
            SessionPermissions(git="banned"), _PERMISSIVE_CAPS
        )
        assert type(eff) is dict, "no ceiling -> plain dict"
        ok, msg = check_requires_resource("git", eff, tool_name="GitReadTool")
        assert ok is False
        assert msg == (
            "Permission denied: Tool requires resource 'git' "
            "(permission git:read), but session does not allow it."
        )
        assert "workspace ceiling" not in msg

    def test_equal_ceiling_denial_is_not_attributed_to_ceiling(self):
        # Session already banned and the ceiling says banned: the ceiling
        # changed nothing, so no phrase may appear.
        eff = get_effective_permissions(
            SessionPermissions(git="banned"),
            _PERMISSIVE_CAPS,
            {"git": "banned"},
        )
        assert type(eff) is dict, "equal ceiling -> no change -> plain dict"
        ok, msg = check_requires_resource("git", eff, tool_name="GitReadTool")
        assert ok is False
        assert msg == (
            "Permission denied: Tool requires resource 'git' "
            "(permission git:read), but session does not allow it."
        )
        assert "workspace ceiling" not in msg


# ──────────────────────────────────────────────────────────────────────────
# 2. GitReadTool container gate (in-tool atomic re-check)
# ──────────────────────────────────────────────────────────────────────────
class TestGitReadToolContainerGate:
    def test_container_gate_raises_permission_error_with_ceiling_note(self):
        eff = _ceiling_banned_git_eff()
        # effective_permissions is assigned post-construction (ToolExecutor
        # pattern, validate_assignment=False): the pydantic __init__ copy would
        # strip the _CeilingAnnotatedDict subclass/annotations.
        tool = GitReadTool(
            operation="status",
            session_id="s",
            workspace_id="w",
            session_permissions={},  # non-None -> in-tool gate active
        )
        tool.effective_permissions = eff
        with pytest.raises(PermissionError) as excinfo:
            tool._exec_container_raw(Path("."), ["status"], manager=object())
        assert str(excinfo.value) == (
            "Permission denied: git:read required for this operation "
            "(workspace ceiling: banned)"
        )

    def test_container_gate_denial_without_ceiling_is_unchanged(self):
        eff = get_effective_permissions(
            SessionPermissions(git="banned"), _PERMISSIVE_CAPS
        )
        tool = GitReadTool(
            operation="status",
            session_id="s",
            workspace_id="w",
            session_permissions={},
            effective_permissions=eff,
        )
        with pytest.raises(PermissionError) as excinfo:
            tool._exec_container_raw(Path("."), ["status"], manager=object())
        assert str(excinfo.value) == (
            "Permission denied: git:read required for this operation"
        )
        assert "workspace ceiling" not in str(excinfo.value)


# ──────────────────────────────────────────────────────────────────────────
# 3. SandboxedExecution permission check
# ──────────────────────────────────────────────────────────────────────────
class TestSandboxedExecutionDenialMessages:
    def test_ceiling_caused_sandbox_denial_names_ceiling(self):
        eff = _ceiling_banned_git_eff()
        with pytest.raises(PermissionError) as excinfo:
            SandboxedExecution(session_permissions=eff).run(
                ["git", "status"], required_category="git:read"
            )
        msg = str(excinfo.value)
        assert msg == (
            "Permission denied: Session permission for git is write, but "
            "workspace ceiling is banned. The session exceeds the ceiling. "
            "Correct either the workspace ceiling or the session grant."
        )

    def test_session_denial_without_ceiling_is_unchanged(self):
        with pytest.raises(PermissionError) as excinfo:
            SandboxedExecution(
                session_permissions={"git": "banned"}
            ).run(["git", "status"], required_category="git:read")
        msg = str(excinfo.value)
        assert msg == (
            "Permission denied: requires git:read, but session allows "
            "git:banned"
        )
        assert "workspace ceiling" not in msg
