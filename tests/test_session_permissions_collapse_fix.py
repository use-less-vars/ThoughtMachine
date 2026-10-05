"""
Regression tests: session_permissions must never collapse to a partial set.

The user-visible collapse was: stored ``session_permissions`` becomes PARTIAL
(e.g. ``{network: 'ask'}``) while legacy ``git_write='write'`` lived as a
top-level ``SessionConfig`` field; ``to_agent_config`` then folded the two
into a literal 2-key effective set.

The canonical permission model is the six resources
``container | network | filesystem | git | mcp | host_bash`` — the split
``git_read``/``git_write`` grains and ``system``/``execution`` resources no
longer exist.  These tests lock the collapse fixes on the canonical set:

- Fix A: ``ConfigManager.apply_config`` MERGES a partial frontend payload over
  the stored permission set instead of wholesale replacing it.
- Fix C: the persistence layer (``save_config_to_session`` /
  ``save_session`` / ``bridge.save_session``) is no longer a grants holder --
  it STRIPS ``session_permissions`` from the persisted ``session_config``
  before writing (the sidecar, not the session record, owns grants).
- Round-trip: ``extract_session_config`` + workspace-ceiling cap preserve the
  full stored set (a capped dump must not drop un-capped keys).
- Locked semantics: ``to_agent_config`` folds a top-level legacy ``git_write``
  into ``session_permissions['git']`` only when the top-level field is set to
  ``'write'`` (other values are dropped, never invented).
"""

import sys
import uuid
from pathlib import Path

# Add project root so that imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.config.session_config import SessionConfig
from session.models import Session
from session.store import FileSystemSessionStore

from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager

FULL_PERMS = {
    "container": False,
    "network": "ask",
    "filesystem": "write",
    "git": "read",
    "mcp": "banned",
    "host_bash": "banned",
}


@pytest.fixture
def temp_store(tmp_path):
    """Return a FileSystemSessionStore rooted in the pytest tmp_path."""
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
        enable_session_history_pruning=False,
    )


def _make_session(metadata_extra=None):
    metadata = {"source": "web_ui"}
    if metadata_extra:
        metadata.update(metadata_extra)
    return Session(
        session_id=str(uuid.uuid4()),
        user_history=[],
        workspace_id=None,
        metadata=metadata,
    )


# ── Fix A: apply_config merges partial payloads ────────────────────────

class TestApplyConfigMerge:
    def test_partial_payload_preserves_stored_keys(self):
        current = SessionConfig(mode="agent", session_permissions=dict(FULL_PERMS))
        _, updated = ConfigManager.apply_config(
            {"session_permissions": {"network": "banned"}},
            current,
        )
        assert updated is not None
        sp = updated.session_permissions
        assert sp["network"] == "banned"          # explicit payload value wins
        assert sp["filesystem"] == "write"         # stored key preserved
        assert sp["git"] == "read"                 # stored key preserved
        assert sp["mcp"] == "banned"               # stored key preserved
        assert sp["host_bash"] == "banned"         # stored key preserved

    def test_full_payload_still_overrides_every_key(self):
        current = SessionConfig(mode="agent", session_permissions=dict(FULL_PERMS))
        new_set = {"network": "banned", "git": "write"}
        _, updated = ConfigManager.apply_config(
            {"session_permissions": dict(new_set)},
            current,
        )
        sp = updated.session_permissions
        assert sp["network"] == "banned"
        assert sp["git"] == "write"
        # keys not in the payload remain stored
        assert sp["filesystem"] == "write"

    def test_payload_none_leaves_stored_untouched(self):
        current = SessionConfig(mode="agent", session_permissions=dict(FULL_PERMS))
        _, updated = ConfigManager.apply_config(
            {"session_permissions": None},
            current,
        )
        assert updated.session_permissions == FULL_PERMS



# ── Fix C: save_config_to_session / save_session strip grants ──────────

class TestPersistenceWriteSites:
    def test_save_config_to_session_strips_session_permissions(self, temp_store):
        mgr = SessionManager(temp_store, ConfigManager())
        session = _make_session(
            {"session_config": {"mode": "agent", "session_permissions": dict(FULL_PERMS)}}
        )
        new_cfg = SessionConfig(mode="agent", session_permissions={"network": "banned"})
        mgr.save_config_to_session(session, new_cfg)

        reloaded = temp_store.load_session(session.session_id)
        assert reloaded is not None
        stored = reloaded.metadata["session_config"]
        # The session record is not a grants holder: the grant is stripped.
        assert "session_permissions" not in stored
        # Non-permission config still persists.
        assert stored["mode"] == "agent"
        assert "agent_config" not in reloaded.metadata

    def test_save_session_strips_session_permissions(self, temp_store):
        mgr = SessionManager(temp_store, ConfigManager())
        session = _make_session(
            {"session_config": {"mode": "agent", "session_permissions": dict(FULL_PERMS)}}
        )
        new_cfg = SessionConfig(mode="agent", session_permissions={"git": "write"})
        mgr.save_session(session, session_config=new_cfg)

        reloaded = temp_store.load_session(session.session_id)
        stored = reloaded.metadata["session_config"]
        assert "session_permissions" not in stored
        assert stored["mode"] == "agent"


# ── Round-trip: extract + ceiling cap preserve the full set ────────────

class TestRoundTrip:
    def test_extract_session_config_ignores_metadata_grant_carrier(
        self, temp_store, hermetic_vault
    ):
        from thoughtmachine.permission_store import (
            PermissionStoreError,
            read_session_permissions,
        )

        mgr = SessionManager(temp_store, ConfigManager())
        session = _make_session(
            {"session_config": {"mode": "agent", "session_permissions": dict(FULL_PERMS)}}
        )
        sc = mgr.extract_session_config(session)
        assert sc is not None
        # (i) the retired metadata carrier no longer seeds the returned config:
        # extract pops the carrier key, so its write grant cannot re-enter.
        assert (sc.session_permissions or {}).get("filesystem") != "write"
        # (iii) non-permission config still survives the pop.
        assert sc.mode == "agent"
        # (ii) the canonical grants are unaffected by the metadata carrier:
        # with no sidecar the canonical read fails CLOSED rather than surfacing
        # the retired carrier grants.
        with pytest.raises(PermissionStoreError):
            read_session_permissions(hermetic_vault, "ws-t5", session.session_id)

    def test_ceiling_cap_lowers_keys_without_dropping_them(self):
        from security.security_gate import apply_workspace_ceiling

        ceiling = {
            "container": False,
            "network": "banned",
            "filesystem": "ask",
            "git": "read",
            "mcp": "banned",
            "host_bash": "banned",
        }
        capped = apply_workspace_ceiling(ceiling, dict(FULL_PERMS))
        assert capped["network"] == "banned"
        # An ask ceiling over the write grant caps to the below-ask tier
        # ('read'), never fabricating an effective 'ask'.
        assert capped["filesystem"] == "read"
        assert capped["git"] == "read"
        assert capped["container"] is False
        # Every stored key survives the cap (no partial collapse), and no
        # legacy resource keys are fabricated.
        assert set(capped.keys()) == set(FULL_PERMS.keys())
        assert set(capped.keys()) == {
            "container", "network", "filesystem", "git", "mcp", "host_bash",
        }

    def test_load_rewrite_skipped_when_config_unchanged(self):
        # Mirrors bridge.load_session's conditional-save decision (Fix B):
        # an unchanged stored config must not trigger a rewrite, because the
        # old unconditional save_config_to_session call was a collapse vector.
        stored_raw = SessionConfig(mode="agent", session_permissions=dict(FULL_PERMS)).model_dump(
            exclude={"api_key"}, exclude_none=True
        )
        new_raw = SessionConfig(mode="agent", session_permissions=dict(FULL_PERMS)).model_dump(
            exclude={"api_key"}, exclude_none=True
        )
        assert stored_raw == new_raw


# ── Locked semantics: to_agent_config fold ─────────────────────────────

class TestToAgentConfigFold:
    def test_session_permissions_passthrough_no_legacy_invention(self):
        sc = SessionConfig(mode="agent", session_permissions={"network": "ask"})
        ac = sc.to_agent_config()
        assert ac.session_permissions.network == "ask"
        assert ac.session_permissions.git == "read"  # canonical default, not invented
        # AgentConfig carries a SessionPermissions instance (exactly the six
        # canonical fields) — a legacy git_write grain cannot exist on it.
        assert "git_write" not in ac.session_permissions.model_dump()

    def test_top_level_git_write_folds_to_git(self):
        sc = SessionConfig(mode="agent", git_write="write")
        ac = sc.to_agent_config()
        assert ac.session_permissions.git == "write"
