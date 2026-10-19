"""
Pinning tests: a session permission change takes effect IMMEDIATELY for an
ALREADY-RUNNING worker sub-agent -- in both directions.

Background (the fourth gate)
----------------------------
Doctrine says permissions are resolved through THREE gates on every tool call:

    declared grant  x  workspace ceiling  x  (worker) declared footprint

and the authority for the first gate is the session's ON-DISK record (vault
sidecar), re-read on every tool call.  That makes both a revocation AND a
grant visible to a running worker without a respawn.

Before the fix a fourth, undocumented gate sat between the disk record and the
gate: the worker's spawn-time *snapshot* of the parent session's permissions
(``_restrictive_merge`` -> ``AgentConfig.session_permissions``) was passed to
``check_required_categories`` as the ``permission_footprint`` cap.  Because
that snapshot is frozen at spawn, the outcome was asymmetric:

  * REVOCATION on disk -> immediate (disk says read, snapshot said write,
    min() gives read) -> denied at once;
  * GRANT on disk -> NEVER applied (disk says write, snapshot said read,
    min() gives read) -> the worker stayed locked out until it was respawned.

The fix drops the snapshot as a cap.  The only worker cap is now the DECLARED
footprint from the worker definition (``AgentConfig.permission_footprint``,
empty/absent = no cap), so per tool call:

    effective = min(declared_footprint, live_session_minus_workspace_ceiling)

Cases pinned here
-----------------
A. GRANT is immediate: one executor instance, first call denied, the sidecar
   is rewritten with a wider grant WHILE that instance is alive, second call
   on the SAME instance is allowed (no re-instantiation, no respawn).
B. REVOCATION is immediate: mirror image of A on the same instance.
C. The DECLARED footprint still caps from the executor level (a worker can
   never be narrower than declared... or wider), and a footprint can never
   grant what the session does not hold.
D. Widening observability: a post-spawn grant emits exactly ONE
   ``worker_permission_widened`` audit record with the six observation keys;
   a narrowing and a first-ever observation emit none.
E. Ceiling still wins for a worker exactly as for a main agent: a session
   grant above the workspace ceiling is denied for both.
"""

import json
from typing import ClassVar, List

import pytest

from agent.core import tool_executor as tool_executor_module
from agent.core.tool_executor import ToolExecutor
from thoughtmachine.permission_store import write_session_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.base import ToolBase

SESSION = "user-session-abc123"
WORKSPACE = "ws-x"


class FilesystemWriteTool(ToolBase):
    """A tool that requires filesystem write access."""
    tool: str = "FilesystemWriteTool"
    required_categories: ClassVar[List[str]] = ["filesystem:write"]

    def execute(self) -> str:
        return "FS OK"


class FakeConfig:
    """Minimal config stub (no workspace_path -> ws_id comes from the arg).

    ``permission_footprint`` mirrors AgentConfig.permission_footprint: the
    worker definition's DECLARED cap (empty/None = no cap).  It is not the
    session snapshot -- ``session_permissions`` is only the in-memory mirror
    the executor must NOT treat as a ceiling in disk-authoritative mode.
    """
    workspace_path = None
    tool_output_token_limit = None
    permission_footprint = None

    def __init__(self, permissions=None, permission_footprint=None):
        self.session_permissions = permissions
        self.permission_footprint = permission_footprint


class FakeState:
    security_config = None


@pytest.fixture
def gate_env(monkeypatch):
    """Force permissive workspace capabilities (only the gate-mode decision is
    under test; the fail-closed capability fallback would confound it)."""
    monkeypatch.setattr(
        tool_executor_module,
        "get_workspace_capabilities",
        lambda ws_id: WorkspaceCapabilities(),
    )


def _seed_vault(
    monkeypatch,
    tmp_path,
    ws_id,
    session_id,
    filesystem_level,
    ceiling_level="write",
):
    """Write the canonical session sidecar + workspace ceiling.

    ``filesystem_level`` is the SESSION grant; ``ceiling_level`` is the
    workspace ceiling written into the workspace ``config.json`` and defaults
    to ``write`` (unlimited for this category) so the session grant can be
    varied up and down WITHOUT ever exceeding the ceiling: a session value
    above the ceiling is a hard deny of its own ("The session exceeds the
    ceiling"), which would mask the behaviour under test.  Case E passes an
    explicitly stricter ceiling to construct exactly that state.
    """
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    root = vault_root()
    ws_dir = root / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    write_session_permissions(
        root,
        ws_id,
        session_id,
        {"filesystem": filesystem_level, "network": "banned"},
    )
    (ws_dir / "config.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "filesystem": ceiling_level or filesystem_level,
                    "network": "banned",
                }
            }
        ),
        encoding="utf-8",
    )


def _grant(monkeypatch, tmp_path, filesystem_level):
    """Rewrite ONLY the session sidecar (a live grant/revocation).

    The workspace ceiling is left exactly as seeded, so this is the on-disk
    write a UI permission change performs against a running session.
    """
    write_session_permissions(
        vault_root(),
        WORKSPACE,
        SESSION,
        {"filesystem": filesystem_level, "network": "banned"},
    )


def _make_executor(
    permissions, is_worker_context, event_bus=None, permission_footprint=None
):
    return ToolExecutor(
        tool_classes=[FilesystemWriteTool],
        config=FakeConfig(permissions, permission_footprint=permission_footprint),
        state=FakeState(),
        logger=None,
        security_available=False,
        agent=None,
        event_bus=event_bus,
        is_worker_context=is_worker_context,
    )


def _run_write_tool(executor, session_id=SESSION, workspace_id=WORKSPACE):
    return executor._execute_single_tool(
        FilesystemWriteTool, {}, "FilesystemWriteTool", 0,
        lambda: False, lambda: None, lambda: 0,
        session_id=session_id,
        workspace_id=workspace_id,
    )


class TestGrantAndRevocationAreImmediate:
    """Disk is the authority in BOTH directions, on one live executor."""

    def test_grant_is_immediate_on_the_same_executor(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Case A -- a GRANT must reach an already-running worker.

        One executor instance is built while the session holds filesystem:read
        and its first write attempt is denied.  The sidecar is then rewritten
        to filesystem:write (a live grant, no respawn) and the SECOND call on
        the very SAME instance is allowed.  No new ToolExecutor is constructed
        between the two calls: the only thing that changed is the on-disk
        record.
        """
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        # In-memory mirror frozen at spawn: read (matches the seed).
        executor = _make_executor(
            SessionPermissions(filesystem="read"), is_worker_context=True
        )
        # The instance whose lifetime spans the grant below.
        live_executor = executor
        first = _run_write_tool(live_executor)
        assert "Permission denied" in first["result"], first["result"]
        assert "filesystem:write" in first["result"], first["result"]

        # ── live grant on disk (the executor is NOT re-created) ──────────
        _grant(monkeypatch, tmp_path, "write")
        assert vault_root()  # sanity: same vault still in use

        second = _run_write_tool(live_executor)
        assert second["result"] == "FS OK", (
            "a post-spawn GRANT must apply to an already-running worker "
            f"without respawn; got {second['result']!r}"
        )
        # Proof of no re-instantiation: same object, never rebuilt.
        assert live_executor is executor
        # The in-memory mirror is STILL the stale read snapshot -- the
        # permission came from disk, not from the mirror.
        assert live_executor.config.session_permissions.filesystem == "read"

    def test_revocation_is_immediate_on_the_same_executor(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Case B -- the mirror image of A: a revocation lands immediately.

        The executor is built while the session holds filesystem:write (and
        the stale mirror still says write); the sidecar is rewritten to read
        and the second call is denied.
        """
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "write")
        executor = _make_executor(
            SessionPermissions(filesystem="write"), is_worker_context=True
        )
        first = _run_write_tool(executor)
        assert first["result"] == "FS OK", first["result"]

        # ── live revocation on disk (the executor is NOT re-created) ─────
        _grant(monkeypatch, tmp_path, "read")
        # The mirror is deliberately left permissive (stale): it must not
        # resurrect the revoked grant.
        assert executor.config.session_permissions.filesystem == "write"

        second = _run_write_tool(executor)
        assert "Permission denied" in second["result"], second["result"]
        assert "filesystem:write" in second["result"], second["result"]


class TestDeclaredFootprintCap:
    """The ONLY worker cap is the declared definition footprint (case C)."""

    def test_declared_footprint_caps_a_wider_live_grant(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Vault grants write, worker definition declares filesystem:read ->
        denied.  This is the cap that must NOT come from the snapshot."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "write")
        executor = _make_executor(
            SessionPermissions(filesystem="write"),
            is_worker_context=True,
            permission_footprint={"filesystem": "read"},
        )
        result = _run_write_tool(executor)
        assert "Permission denied" in result["result"], result["result"]
        assert "filesystem:write" in result["result"], result["result"]

    def test_declared_footprint_cannot_grant_beyond_the_session(
        self, gate_env, tmp_path, monkeypatch
    ):
        """A wider footprint never widens: vault says read, definition says
        write -> still denied (min() semantics, the footprint is a cap only)."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        executor = _make_executor(
            SessionPermissions(filesystem="read"),
            is_worker_context=True,
            permission_footprint={"filesystem": "write"},
        )
        result = _run_write_tool(executor)
        assert "Permission denied" in result["result"], result["result"]
        assert "filesystem:write" in result["result"], result["result"]

    def test_declared_footprint_survives_a_live_grant(
        self, gate_env, tmp_path, monkeypatch
    ):
        """The cap is stable across a grant: read -> write on disk widens a
        worker whose declared footprint is write, and the same worker with a
        declared footprint of read stays denied before AND after."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        capped = _make_executor(
            SessionPermissions(filesystem="read"),
            is_worker_context=True,
            permission_footprint={"filesystem": "read"},
        )
        assert "Permission denied" in _run_write_tool(capped)["result"]

        _grant(monkeypatch, tmp_path, "write")
        after = _run_write_tool(capped)
        assert "Permission denied" in after["result"], after["result"]
        assert "filesystem:write" in after["result"], after["result"]

    def test_main_agent_has_no_declared_footprint_cap(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Sanity leg: the footprint kwarg is worker-only -- a main executor
        with a footprint set on its config still executes (the executor passes
        permission_footprint=None when is_worker_context is False)."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "write")
        executor = _make_executor(
            SessionPermissions(filesystem="write"),
            is_worker_context=False,
            permission_footprint={"filesystem": "read"},
        )
        result = _run_write_tool(executor)
        assert result["result"] == "FS OK", result["result"]


class TestWideningAudit:
    """Case D -- a post-spawn widening leaves exactly one audit record."""

    @staticmethod
    def _record(monkeypatch):
        records = []

        def _fake_audit_event(event_type, details):
            records.append((event_type, details))

        # tool_executor binds ``audit_event`` at import time, so the module
        # attribute is what the widening observation reaches.
        monkeypatch.setattr(tool_executor_module, "audit_event", _fake_audit_event)
        return records

    def test_first_observation_is_a_baseline_not_a_widening(
        self, gate_env, tmp_path, monkeypatch
    ):
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        records = self._record(monkeypatch)
        executor = _make_executor(
            SessionPermissions(filesystem="read"), is_worker_context=True
        )
        _run_write_tool(executor)
        assert records == [], (
            "the first observation only establishes the baseline; "
            f"got {records!r}"
        )

    def test_single_grant_emits_exactly_one_record(
        self, gate_env, tmp_path, monkeypatch
    ):
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        records = self._record(monkeypatch)
        executor = _make_executor(
            SessionPermissions(filesystem="read"), is_worker_context=True
        )
        _run_write_tool(executor)          # baseline
        assert records == []

        _grant(monkeypatch, tmp_path, "write")
        second = _run_write_tool(executor)  # the widening observation
        assert second["result"] == "FS OK", second["result"]

        assert len(records) == 1, records
        event_type, details = records[0]
        assert event_type == "worker_permission_widened"
        payload = json.loads(details)
        assert set(payload) == {
            "worker_id",
            "session_id",
            "category",
            "old_value",
            "new_value",
            "actor",
        }, payload
        assert payload["category"] == "filesystem"
        assert payload["old_value"] == "read"
        assert payload["new_value"] == "write"
        assert payload["session_id"] == SESSION
        assert payload["actor"] == "unknown"

        # A third call at the SAME level is not a widening.
        _run_write_tool(executor)
        assert len(records) == 1, records

    def test_masked_widening_emits_no_record(
        self, gate_env, tmp_path, monkeypatch
    ):
        """A declared footprint that swallows a grant masks the widening.

        The observation runs on the POST-cap resolution, so a grant the cap
        clamps back down is not a change in the worker's effective authority
        and stays silent (contrast ``test_single_grant_emits_exactly_one_record``
        -- same setup without a footprint -- which emits exactly one record).
        """
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        records = self._record(monkeypatch)
        executor = _make_executor(
            SessionPermissions(filesystem="read"),
            is_worker_context=True,
            permission_footprint={"filesystem": "read"},
        )
        first = _run_write_tool(executor)   # denied; establishes the baseline
        assert "Permission denied" in first["result"], first["result"]
        assert records == [], records

        _grant(monkeypatch, tmp_path, "write")
        second = _run_write_tool(executor)  # the grant, capped back to read
        assert "Permission denied" in second["result"], second["result"]
        assert "filesystem:write" in second["result"], second["result"]
        assert records == [], (
            "a grant masked by the declared footprint is not a widening; "
            f"got {records!r}"
        )

        # The cap is unchanged, so a third call stays silent too.
        _run_write_tool(executor)
        assert records == [], records

    def test_narrowing_is_never_reported_as_widening(
        self, gate_env, tmp_path, monkeypatch
    ):
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "write")
        records = self._record(monkeypatch)
        executor = _make_executor(
            SessionPermissions(filesystem="write"), is_worker_context=True
        )
        _run_write_tool(executor)          # baseline
        assert records == []

        _grant(monkeypatch, tmp_path, "read")
        denied = _run_write_tool(executor)
        assert "Permission denied" in denied["result"], denied["result"]
        assert records == [], (
            "a narrowing must not be reported as a widening; "
            f"got {records!r}"
        )

    def test_main_agent_widening_is_not_audited(
        self, gate_env, tmp_path, monkeypatch
    ):
        """The observation is worker-scoped (main agents have no spawn-time
        mirror to go stale)."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        records = self._record(monkeypatch)
        executor = _make_executor(
            SessionPermissions(filesystem="read"), is_worker_context=False
        )
        _run_write_tool(executor)
        _grant(monkeypatch, tmp_path, "write")
        _run_write_tool(executor)
        assert records == [], records


class TestCeilingStillWins:
    """Case E -- the workspace ceiling is unaffected by the fix."""

    def test_session_grant_above_ceiling_denied_for_worker_and_main(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Session holds filesystem:write but the workspace ceiling is banned:
        denied for a worker AND for a main agent (same gate, same ceiling)."""
        _seed_vault(
            monkeypatch, tmp_path, WORKSPACE, SESSION, "write", ceiling_level="banned"
        )
        for is_worker in (True, False):
            executor = _make_executor(
                SessionPermissions(filesystem="write"),
                is_worker_context=is_worker,
            )
            result = _run_write_tool(executor)
            assert "Permission denied" in result["result"], (
                is_worker,
                result["result"],
            )
            # The gate reports this as the dedicated exceeds-ceiling denial
            # (not the plain ``filesystem:write`` category denial), and it is
            # identical for a worker and for a main agent.
            assert "exceeds the ceiling" in result["result"], result["result"]
            assert "filesystem" in result["result"], result["result"]

    def test_ceiling_read_caps_session_write_for_a_worker(
        self, gate_env, tmp_path, monkeypatch
    ):
        """A read ceiling caps a write grant to read -- for a worker too."""
        _seed_vault(
            monkeypatch, tmp_path, WORKSPACE, SESSION, "write", ceiling_level="read"
        )
        executor = _make_executor(
            SessionPermissions(filesystem="write"), is_worker_context=True
        )
        result = _run_write_tool(executor)
        assert "Permission denied" in result["result"], result["result"]
        assert "exceeds the ceiling" in result["result"], result["result"]
        assert "filesystem" in result["result"], result["result"]
