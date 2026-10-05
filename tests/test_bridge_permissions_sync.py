"""
Integration tests: session_permissions roundtrip via WebAgentBridge.

Validates:
1.  ``apply_config`` IGNORES a non‑default ``session_permissions`` payload (the
    payload grant is dropped, never stored on the bridge -- sidecar owns grants)
2.  ``save_session()`` → ``load_session()`` roundtrip does NOT persist or
    resurrect ``session_permissions`` (the session record is not a grants holder)
3.  ``ToolExecutor`` enforces the restored permissions:
      - ``FilePreviewTool`` (``filesystem:read``) is *denied* when ``filesystem=banned``
      - and allowed after updating to ``filesystem=read``
"""

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import ClassVar, List

# Add project root so that imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore
from agent.config.models import AgentConfig
from agent.config.loader import validate_config
from agent.core.tool_executor import (
    ToolExecutor,
)
from agent.core.state import AgentState
from tools.file_preview_tool import FilePreviewTool
from thoughtmachine.security import SessionPermissions


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — config roundtrip via bridge
# ══════════════════════════════════════════════════════════════════════════════

class TestBridgePermissionsRoundtrip:
    """Apply, persist, reload — session_permissions is NOT a metadata grant.

    The session record (P2) is no longer a grants holder: ``save_session``
    strips ``session_permissions`` before persisting, so the on-disk
    ``session_config`` carries no grant and a reloaded bridge does not
    resurrect one from metadata.
    """

    @pytest.fixture
    def temp_store(self, tmp_path):
        """Return a FileSystemSessionStore rooted in the pytest tmp_path."""
        sessions_dir = tmp_path / "sessions"
        state_dir = tmp_path / "state"
        return FileSystemSessionStore(
            sessions_dir=str(sessions_dir),
            state_dir=str(state_dir),
        )

    # ------------------------------------------------------------------
    # 1a) apply_config IGNORES the payload permission (P1 — sidecar owns grants)
    # ------------------------------------------------------------------

    def test_apply_config_ignores_custom_permissions(self, temp_store, caplog):
        """apply_config payload session_permissions is IGNORED (P1)."""
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)

        with caplog.at_level(logging.WARNING, logger="web_ui.backend.config_manager"):
            result = bridge.apply_config({
                "session_permissions": {"filesystem": "banned"},
            })
        # apply_config now returns an enriched dict, not {"success": True}
        assert "config" in result and "merged_config" in result, f"apply_config failed: {result}"
        # P1: the payload cannot grant — the canonical grant store is the
        # session permission sidecar, so 'banned' never lands on the bridge.
        config = bridge.get_config()
        assert config is not None
        assert (config.get("session_permissions") or {}).get("filesystem") != "banned", config
        assert any(
            "session_permissions" in r.getMessage() for r in caplog.records
        ), caplog.records

    # ------------------------------------------------------------------
    # 1b) roundtrip: save → load ⇒ permissions preserved
    # ------------------------------------------------------------------

    def test_roundtrip_does_not_persist_permissions_in_metadata(self, temp_store):
        """save_session must NOT persist the grant into metadata.session_config;
        load_session must NOT resurrect it from the record."""
        # ── write ─────────────────────────────────────────────────────
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)

        bridge.apply_config({
            "session_permissions": {"filesystem": "banned"},
        })
        saved = bridge.save_session()
        assert saved is not None, "save_session returned None"
        session_id = saved.session_id

        # ── verify raw JSON on disk ───────────────────────────────────
        path = temp_store._find_session_path(session_id)
        assert path is not None, "session file not found on disk"
        with open(path, "r") as f:
            raw = json.load(f)
        stored_config = raw.get("metadata", {}).get("session_config", {})
        assert "session_permissions" not in stored_config, (
            f"session_permissions must be stripped from metadata, got {stored_config}"
        )

        # ── read ──────────────────────────────────────────────────────
        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        loaded_ok = bridge2.load_session(session_id)
        assert loaded_ok, "load_session returned False"

        config = bridge2.get_config()
        assert config is not None
        loaded_perms = config.get("session_permissions")
        assert not (isinstance(loaded_perms, dict)
                    and loaded_perms.get("filesystem") == "banned"), (
            f"metadata resurrected the filesystem=banned grant after load: {loaded_perms}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — ToolExecutor enforces the restored permissions
# ══════════════════════════════════════════════════════════════════════════════

class TestPermissionEnforcement:
    """Use the permissions that survived the roundtrip to gate actual tools."""

    @pytest.fixture
    def executor_with_perms(self, tmp_path, request):
        """
        Build a ToolExecutor whose config has *filesystem=banned*.

        The state is a minimal AgentState that allows all tools.
        """
        config = AgentConfig(
            session_permissions=SessionPermissions(filesystem="banned"),
        )
        state = AgentState(config=config)
        executor = ToolExecutor(
            tool_classes=[FilePreviewTool],
            config=config,
            state=state,
        )
        return config, state, executor

    # ------------------------------------------------------------------
    # 2a) FilePreviewTool denied when filesystem=banned
    # ------------------------------------------------------------------

    def test_file_preview_denied_when_banned(self, executor_with_perms):
        """FilePreviewTool (filesystem:read) is denied when filesystem=banned."""
        config, state, executor = executor_with_perms

        # Via ToolExecutor._execute_single_tool (integration path)
        result = executor._execute_single_tool(
            FilePreviewTool,
            {"filename": "irrelevant.txt"},
            "FilePreviewTool",
            0,
            lambda: False,
            lambda: "",
            lambda: 0,
        )
        assert "Permission denied" in result.get("result", ""), (
            f"Expected 'Permission denied', got: {result}"
        )

    # ------------------------------------------------------------------
    # 2b) Same tool allowed after updating to filesystem=read
    # ------------------------------------------------------------------

    def test_file_preview_allowed_when_read(self, executor_with_perms):
        """Same tool succeeds after permissions are updated to filesystem=read."""
        config, state, executor = executor_with_perms

        # Lift the restriction
        config.session_permissions = SessionPermissions(filesystem="read")

        # Via ToolExecutor — note: filename points to a non‑existent file,
        # but the permission gate passes first; the tool will then attempt
        # to read the file and fail with a meaningful IO error, which is
        # *not* a permission error — that's the correct behaviour.
        result = executor._execute_single_tool(
            FilePreviewTool,
            {"filename": "/nonexistent/file.txt"},
            "FilePreviewTool",
            0,
            lambda: False,
            lambda: "",
            lambda: 0,
        )
        result_text = result.get("result", "")
        assert "Permission denied" not in result_text, (
            f"Unexpected permission denial after lifting ban: {result}"
        )
        # The tool should fail with a file‑not‑found / path error, not a
        # permissions error — which confirms the permission gate opened.
        assert any(msg in result_text for msg in ("No such file", "not found", "not a file", "not exist")), (
            f"Expected a file-level error (not a permission error), got: {result}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Hot-swap via AgentConfig (end-to-end config update)
# ══════════════════════════════════════════════════════════════════════════════

class TestPermissionsHotSwap:
    """Change session_permissions at runtime and verify enforcement changes."""

    def test_hot_swap_banned_to_read(self, tmp_path):
        """AgentConfig.session_permissions can be replaced at runtime."""
        config = AgentConfig(
            session_permissions=SessionPermissions(filesystem="banned"),
        )
        state = AgentState(config=config)
        executor = ToolExecutor(
            tool_classes=[FilePreviewTool],
            config=config,
            state=state,
        )

        # Should be denied initially
        r1 = executor._execute_single_tool(
            FilePreviewTool, {"filename": "x.txt"}, "FilePreviewTool", 0,
            lambda: False, lambda: "", lambda: 0,
        )
        assert "Permission denied" in r1.get("result", "")

        # Hot-swap
        config.session_permissions = SessionPermissions(filesystem="read")

        # Should now pass the permission gate (file still absent → file error)
        r2 = executor._execute_single_tool(
            FilePreviewTool, {"filename": "/nonexistent/file.txt"}, "FilePreviewTool", 0,
            lambda: False, lambda: "", lambda: 0,
        )
        assert "Permission denied" not in r2.get("result", "")
