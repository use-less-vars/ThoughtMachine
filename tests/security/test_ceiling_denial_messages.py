"""
Ceiling-caused gate denials mention "(workspace ceiling: <level>)".

Phase 1 of the message-clarification task: when a ``check_required_categories``
denial is CAUSED by the workspace-permission ceiling (the
``workspace_permissions`` layer), the returned message must carry a
``(workspace ceiling: <level>)`` suffix so the user understands the session
grant alone would have allowed the tool.  All other denials must stay
byte-identical to the historical text.

Mechanism under test: ``get_effective_permissions`` returns a plain-dict
subclass (``_CeilingAnnotatedDict``) carrying ``_ceiling_annotations``
{category: {"pre": <pre-ceiling value>, "level": <label>}} for exactly the
categories the ceiling restricted; ``check_required_categories`` appends the
suffix only when the pre-ceiling value satisfies the requirement and the
worker permission footprint (if any) does not independently deny it.

NOTE: verified against HEAD 87d62dc + the uncommitted Phase-1 edits in
security/security_gate.py.  Full battery (this file + the neighbouring gate
suites) is green:

    python -m pytest -q tests/security/test_ceiling_denial_messages.py \
        tests/security/test_security_gate.py tests/test_security_gate_disk.py \
        tests/test_workspace_permissions.py tests/test_tool_executor.py \
        tests/test_worker_disk_mode_inheritance.py
    # 131 passed
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

from security.security_gate import (
    _CeilingAnnotatedDict,
    ceiling_contradictions,
    check_required_categories,
    get_effective_permissions,
)
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

_PERMISSIVE_CAPS = WorkspaceCapabilities()  # fully permissive defaults


def _deny(required, effective, **kwargs):
    """Run a single-requirement gate check that must hit the deny branch."""
    ok, message = check_required_categories(
        required,
        effective,
        tool_name="ProbeTool",
        tool_args={},
        description="probe",
        **kwargs,
    )
    assert ok is False, "expected denial"
    return message


# ─────────────────────────────────────────────────────────────────────────────
# 1. Ceiling-caused filesystem denial carries the phrase + level
# ─────────────────────────────────────────────────────────────────────────────
class TestCeilingCausedFilesystemDenial:
    def test_write_denied_by_read_ceiling_names_ceiling(self):
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"),
            _PERMISSIVE_CAPS,
            {"filesystem": "read"},
        )
        # Content identical to a plain dict merge result — the effective
        # dict carries exactly the six canonical category keys.
        assert eff["filesystem"] == "read"
        assert eff == {
            "filesystem": "read",
            "network": "banned",  # session default
            "container": False,
            "git": "read",  # session default
            "mcp": "banned",
            "host_bash": "banned",
        }
        assert json.loads(json.dumps(eff)) == dict(eff)  # JSON-safe subclass
        # … and it carries ceiling provenance for the capped category.
        ann = getattr(eff, "_ceiling_annotations", None)
        assert ann is not None
        assert ann["filesystem"] == {"pre": "write", "level": "read"}

        msg = _deny(["filesystem:write"], eff)
        assert msg == (
            "Permission denied: Session permission for filesystem is write, "
            "but workspace ceiling is read. The session exceeds the ceiling. "
            "Correct either the workspace ceiling or the session grant."
        )
        # The reason is first-class (structured), not a string match: the same
        # provenance yields the canonical reason payload.
        assert ceiling_contradictions(eff) == [
            {
                "resource": "filesystem",
                "session_value": "write",
                "workspace_value": "read",
                "reason": "session_exceeds_workspace_ceiling",
                "guidance": "Correct either the workspace ceiling or the session grant.",
            }
        ]

    def test_ask_ceiling_caps_to_read_but_labels_ask(self):
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"),
            _PERMISSIVE_CAPS,
            {"filesystem": "ask"},
        )
        assert eff["filesystem"] == "read"
        msg = _deny(["filesystem:write"], eff)
        assert (
            "Session permission for filesystem is write, but workspace ceiling "
            "is ask. The session exceeds the ceiling." in msg
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Non-ceiling denials stay byte-identical
# ─────────────────────────────────────────────────────────────────────────────
class TestNonCeilingDenialsByteIdentical:
    def test_session_grant_denial_without_ceiling_is_unchanged(self):
        eff = get_effective_permissions(
            SessionPermissions(filesystem="read"), _PERMISSIVE_CAPS
        )
        assert type(eff) is dict, "no ceiling -> plain dict on the common path"
        msg = _deny(["filesystem:write"], eff)
        assert msg == (
            "Permission denied: Tool requires filesystem:write, "
            "but session allows filesystem:read"
        )
        assert "workspace ceiling" not in msg

    def test_capability_cap_denial_is_not_attributed_to_ceiling(self):
        # filesystem_write=False caps the session grant; the workspace
        # ceiling (here: none) is NOT the binder, so no phrase may appear.
        caps = WorkspaceCapabilities(filesystem_write=False)
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"), caps
        )
        assert eff["filesystem"] == "read"
        msg = _deny(["filesystem:write"], eff)
        assert msg == (
            "Permission denied: Tool requires filesystem:write, "
            "but session allows filesystem:read"
        )

    def test_capability_and_ceiling_both_present_capability_is_binder(self):
        # Ceiling read + filesystem_write=False: the capability already caps
        # the pre-ceiling merge to read, so the ceiling changed nothing and
        # the denial must not be attributed to it.
        caps = WorkspaceCapabilities(filesystem_write=False)
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"), caps, {"filesystem": "read"}
        )
        assert eff["filesystem"] == "read"
        assert getattr(eff, "_ceiling_annotations", None) in (None, {})
        msg = _deny(["filesystem:write"], eff)
        assert "workspace ceiling" not in msg

    def test_ceiling_equal_to_session_grant_changes_nothing(self):
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"),
            _PERMISSIVE_CAPS,
            {"filesystem": "write"},
        )
        assert type(eff) is dict
        assert eff["filesystem"] == "write"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Container boolean ceiling
# ─────────────────────────────────────────────────────────────────────────────
class TestContainerCeilingDenial:
    def test_container_true_denied_by_false_ceiling_names_ceiling(self):
        eff = get_effective_permissions(
            SessionPermissions(container=True),
            _PERMISSIVE_CAPS,
            {"container": False},
        )
        assert eff["container"] is False
        assert getattr(eff, "_ceiling_annotations", {}).get("container") == {
            "pre": True,
            "level": "false",
        }
        msg = _deny(["container:true"], eff)
        assert msg == (
            "Permission denied: Session permission for container is True, "
            "but workspace ceiling is false. The session exceeds the ceiling. "
            "Correct either the workspace ceiling or the session grant."
        )

    def test_legacy_docker_ceiling_alias_annotates_container(self):
        eff = get_effective_permissions(
            SessionPermissions(container=True),
            _PERMISSIVE_CAPS,
            {"docker": "read"},  # legacy alias normalised onto container
        )
        assert eff["container"] is False
        msg = _deny(["container:true"], eff)
        assert (
            "Session permission for container is True, but workspace ceiling "
            "is read. The session exceeds the ceiling." in msg
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Worker-footprint-caused denial must NOT name the ceiling
# ─────────────────────────────────────────────────────────────────────────────
class TestWorkerFootprintAttribution:
    def test_footprint_denial_is_not_attributed_to_ceiling(self):
        # Disk mode shape: session grant 'write' (disk record), ceiling
        # 'read', worker footprint 'read'.  The footprint independently
        # denies the write, so the ceiling phrase must NOT appear.
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"),
            _PERMISSIVE_CAPS,
            {"filesystem": "read"},
        )
        assert getattr(eff, "_ceiling_annotations", {}).get("filesystem") == {
            "pre": "write",
            "level": "read",
        }
        msg = _deny(
            ["filesystem:write"],
            eff,
            permission_footprint={"filesystem": "read"},
            is_worker_context=True,
        )
        assert msg == (
            "Permission denied: Tool requires filesystem:write, "
            "but session allows filesystem:read"
        )

    def test_footprint_allows_write_ceiling_gets_the_blame(self):
        # Footprint 'write' does not deny; removing the ceiling would allow
        # the call, so the ceiling is named even in worker context.
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write"),
            _PERMISSIVE_CAPS,
            {"filesystem": "read"},
        )
        msg = _deny(
            ["filesystem:write"],
            eff,
            permission_footprint={"filesystem": "write"},
            is_worker_context=True,
        )
        assert (
            "Session permission for filesystem is write, but workspace ceiling "
            "is read. The session exceeds the ceiling." in msg
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Git ceiling denials
# ─────────────────────────────────────────────────────────────────────────────
class TestGitCeilingDenials:
    def test_git_write_denial_by_read_ceiling_annotates_category(self):
        eff = get_effective_permissions(
            SessionPermissions(git="write"),
            _PERMISSIVE_CAPS,
            {"git": "read"},
        )
        assert eff["git"] == "read"
        ann = getattr(eff, "_ceiling_annotations", {})
        # The single canonical ``git`` category is annotated when the
        # ceiling caps it; the legacy git_read/git_write split grains are
        # no longer emitted, so the git:write requirement is what the
        # ceiling binds.
        assert ann.get("git") == {"pre": "write", "level": "read"}
        msg = _deny(["git:write"], eff)
        assert (
            "Session permission for git is write, but workspace ceiling "
            "is read. The session exceeds the ceiling." in msg
        )

    def test_git_write_denial_by_banned_ceiling(self):
        """A ``banned`` git ceiling caps the write grant and is named in the
        denial message (git_write ceiling grains were removed in the
        permission-schema unification)."""
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
        msg = _deny(["git:write"], eff)
        assert (
            "Session permission for git is write, but workspace ceiling "
            "is banned. The session exceeds the ceiling." in msg
        )

    def test_plain_git_write_denial_by_read_ceiling(self):
        eff = get_effective_permissions(
            SessionPermissions(git="write"),
            _PERMISSIVE_CAPS,
            {"git": "read"},
        )
        msg = _deny(["git:write"], eff)
        assert (
            "Session permission for git is write, but workspace ceiling "
            "is read. The session exceeds the ceiling." in msg
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Subclass carries through the worker-footprint in-place mutation
# ─────────────────────────────────────────────────────────────────────────────
class TestAnnotationSurvival:
    def test_footprint_mutation_keeps_annotations_and_type(self):
        eff = get_effective_permissions(
            SessionPermissions(filesystem="write", network="write"),
            _PERMISSIVE_CAPS,
            {"filesystem": "read"},
        )
        assert isinstance(eff, _CeilingAnnotatedDict)
        # Simulate the footprint loop: in-place min on an UNRELATED category
        # (network) must not disturb filesystem provenance.
        eff["network"] = "banned"
        assert eff["filesystem"] == "read"
        assert getattr(eff, "_ceiling_annotations", {}).get("filesystem") == {
            "pre": "write",
            "level": "read",
        }
