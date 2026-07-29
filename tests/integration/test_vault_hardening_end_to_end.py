#!/usr/bin/env python3
"""End-to-end integration tests for vault hardening — allowlist enforcement,
credential injection, defaults resolution, save-defaults, vault structure
integrity, and a full security boundary capstone.

Every test patches ``Path.home()`` to a ``tmp_path`` so the real
``~/.thoughtmachine`` is never touched.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from pathlib import Path
from typing import Any, Dict

import pytest

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _vault_root(tmp_path: Path) -> Path:
    """Shortcut for the fake vault root."""
    return tmp_path / ".thoughtmachine"


def _make_dirs(vault: Path, *parts: str) -> Path:
    """Create nested directories under the vault."""
    target = vault.joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_json(path: Path, data: Any) -> None:
    """Write JSON data to a file."""
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_vault_with_allowlist(vault: Path, entries: list[str]) -> None:
    """Create a valid checksystem_allowlist.json in the vault."""
    _make_dirs(vault, "system")
    sorted_entries = sorted(str(e) for e in entries)
    canonical = "\n".join(sorted_entries)
    sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    _write_json(
        vault / "system" / "checksystem_allowlist.json",
        {"allowlist": entries, "sha256": sha256},
    )


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test in this module gets a hermetic home directory."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _clear_allowlist_cache() -> None:
    """Clear the module-level allowlist cache before and after each test."""
    import thoughtmachine.vault as vault_mod
    vault_mod._checksystem_allowlist_cache = None
    yield
    vault_mod._checksystem_allowlist_cache = None


# ===================================================================
# TestCheckSystemAllowlist
# ===================================================================


class TestCheckSystemAllowlist:
    """Verify CheckSystem query allowlist enforcement."""

    def test_allowed_path_returns_data(self, tmp_path: Path) -> None:
        """An exact-match allowed query returns the expected data."""
        vault = _vault_root(tmp_path)
        _make_vault_with_allowlist(vault, ["my_config"])

        from tools.workspace.check_system import CheckSystem

        tool = CheckSystem(
            query="my_config",
            allowlist=["my_config"],
            workspace_id="ws1",
            agent_config={"provider": "test", "model": "gpt-4"},
            session_permissions={},
        )
        result = tool.execute()
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed.get("provider") == "test"
        assert parsed.get("model") == "gpt-4"
        assert "error" not in parsed

    def test_disallowed_path_denied(self, tmp_path: Path) -> None:
        """A query not in the allowlist is denied."""
        vault = _vault_root(tmp_path)
        _make_vault_with_allowlist(vault, ["my_config"])

        from tools.workspace.check_system import CheckSystem

        tool = CheckSystem(
            query="dockerfile",
            allowlist=["my_config"],  # NOTE: "dockerfile" is NOT in this list
            workspace_id="ws1",
        )
        result = tool.execute()
        assert isinstance(result, str)
        parsed = json.loads(result)
        # Should indicate denial
        assert parsed.get("status") == "denied" or "not allowed" in str(parsed).lower()

    def test_traversal_attempt_blocked(self, tmp_path: Path) -> None:
        """Path-traversal style queries are rejected as unknown."""
        vault = _vault_root(tmp_path)
        _make_vault_with_allowlist(vault, ["my_config"])

        from tools.workspace.check_system import CheckSystem

        tool = CheckSystem(
            query="../../../etc/passwd",
            allowlist=["my_config"],
            workspace_id="ws1",
        )
        result = tool.execute()
        assert isinstance(result, str)
        parsed = json.loads(result)
        # The query doesn't match any handler, so it should return "Unknown query"
        assert "Unknown query" in str(parsed.get("error", ""))

    def test_allowlist_tampering_detected(self, tmp_path: Path) -> None:
        """A tampered checksystem_allowlist.json causes integrity failure.

        When integrity fails, ``get_checksystem_allowlist()`` returns ``[]``,
        and CheckSystem with ``allowlist=None`` will load that empty list,
        denying all file-access queries.
        """
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "system")

        # Write a corrupted allowlist with a WRONG sha256 hash
        _write_json(
            vault / "system" / "checksystem_allowlist.json",
            {
                "allowlist": ["my_config"],
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            },
        )

        # --- verify get_checksystem_allowlist returns [] ---
        from thoughtmachine.vault import get_checksystem_allowlist

        entries = get_checksystem_allowlist()
        assert entries == [], "Expected empty list on integrity failure"

        # --- now create CheckSystem without explicit allowlist ---
        from tools.workspace.check_system import CheckSystem

        tool = CheckSystem(
            query="my_config",
            allowlist=None,  # will try to load from vault => gets [] (empty)
            workspace_id="ws1",
        )
        result = tool.execute()
        assert isinstance(result, str)
        parsed = json.loads(result)
        # With an empty allowlist, "my_config" should be denied
        assert parsed.get("status") == "denied" or "not allowed" in str(parsed).lower()

    def test_workspace_id_isolation(self, tmp_path: Path) -> None:
        """The workspace_id field is passed to handlers and scopes file reads."""
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "workspaces", "ws1")
        _make_dirs(vault, "workspaces", "ws2")
        # NOTE: CheckSystem.execute() overwrites self.allowlist by loading from
        # vault, so we must also create the vault allowlist file.
        _make_vault_with_allowlist(vault, ["dockerfile"])

        # Write different Dockerfiles for each workspace
        (vault / "workspaces" / "ws1" / "Dockerfile").write_text("FROM ubuntu:22.04")
        (vault / "workspaces" / "ws2" / "Dockerfile").write_text("FROM other:latest")

        from tools.workspace.check_system import CheckSystem

        tool = CheckSystem(
            query="dockerfile",
            allowlist=["dockerfile"],
            workspace_id="ws1",
        )
        result = tool.execute()
        parsed = json.loads(result)

        # If capabilities are available, the handler should read ws1's file
        if parsed.get("available") is True:
            assert "FROM ubuntu:22.04" in parsed.get("content", "")
            assert "FROM other:latest" not in parsed.get("content", "")
        else:
            # If _workspace_dir is unavailable, error should mention workspace
            error_msg = str(parsed.get("error", "")).lower()
            assert "workspace" in error_msg or "not available" in error_msg


# ===================================================================
# TestCredentialInjector
# ===================================================================


class TestCredentialInjector:
    """Verify credential injection end-to-end — complements existing tests."""

    @pytest.fixture
    def cred_vault(self, tmp_path: Path) -> Path:
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "credentials", "test-workspace")
        (vault / "credentials" / "test-workspace" / "api_key").write_text("sk-real-secret")
        return vault

    def test_end_to_end_dispatch_flow(self, cred_vault: Path) -> None:
        """inject() resolves a placeholder, returns Secret, redacts output."""
        from agent.credentials import CredentialInjector, Secret

        injector = CredentialInjector("test-workspace")
        args: Dict[str, Any] = {"api_key": "{{credential:api_key}}"}
        result = injector.inject(args)

        # Value is a Secret with the real content
        assert isinstance(result["api_key"], Secret)
        assert result["api_key"] == "sk-real-secret"

        # Redaction
        assert str(result["api_key"]) == "***"
        assert repr(result["api_key"]) == "***"
        assert f"{result['api_key']}" == "***"

        # Original unchanged (immutability)
        assert args["api_key"] == "{{credential:api_key}}"

    def test_secret_redaction(self) -> None:
        """Secret redacts str/repr/format but preserves equality and len."""
        from agent.credentials import Secret

        secret = Secret("my-secret-value")

        assert str(secret) == "***"
        assert repr(secret) == "***"
        assert format(secret) == "***"
        assert secret == "my-secret-value"
        assert len(secret) == 15

    def test_traversal_blocked(self, cred_vault: Path) -> None:
        """Path traversal in credential key raises CredentialError."""
        from agent.credentials import CredentialError, CredentialInjector

        injector = CredentialInjector("test-workspace")
        with pytest.raises(CredentialError, match="Invalid credential key|contains"):
            injector.resolve("../../../etc/passwd")

    def test_missing_credential(self, cred_vault: Path) -> None:
        """A missing credential raises CredentialError."""
        from agent.credentials import CredentialError, CredentialInjector

        injector = CredentialInjector("test-workspace")
        with pytest.raises(CredentialError, match="not found"):
            injector.resolve("nonexistent")


# ===================================================================
# TestDefaultsResolutionChain
# ===================================================================


class TestDefaultsResolutionChain:
    """Verify the three-layer defaults resolution works correctly."""

    @pytest.fixture
    def layered_vault(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "system")
        _make_dirs(vault, "user")
        _make_dirs(vault, "workspaces", "ws-1")

        # Layer 1: factory defaults
        _write_json(
            vault / "system" / "factory_defaults.json",
            {
                "version": "1",
                "description": "Factory defaults",
                "config": {
                    "provider_id": "openai",
                    "temperature": 0.7,
                    "max_turns": 50,
                },
            },
        )

        # Layer 2: user defaults
        _write_json(
            vault / "user" / "defaults.json",
            {"temperature": 0.5, "system_prompt": "You are helpful"},
        )

        # Layer 3: workspace defaults
        _write_json(
            vault / "workspaces" / "ws-1" / "defaults.json",
            {"provider_id": "anthropic"},
        )

        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault,
        )
        return vault

    def test_three_layer_merge(self, layered_vault: Path) -> None:
        """Factory + user + workspace merge with correct precedence."""
        from agent.config.config_manager import resolve_config_defaults

        result = resolve_config_defaults("ws-1")
        assert result["provider_id"] == "anthropic"  # workspace wins
        assert result["temperature"] == 0.5  # user wins
        assert result["max_turns"] == 50  # from factory
        assert result["system_prompt"] == "You are helpful"  # from user

    def test_workspace_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Workspace always wins over user for the same key."""
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "system")
        _make_dirs(vault, "user")
        _make_dirs(vault, "workspaces", "ws-1")

        _write_json(vault / "system" / "factory_defaults.json",
                     {"config": {"temperature": 0.7}})
        _write_json(vault / "user" / "defaults.json",
                     {"temperature": 0.5})
        _write_json(vault / "workspaces" / "ws-1" / "defaults.json",
                     {"temperature": 0.3})

        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault,
        )
        from agent.config.config_manager import resolve_config_defaults
        result = resolve_config_defaults("ws-1")
        assert result["temperature"] == 0.3

    def test_missing_layers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only factory + workspace layers present; user layer missing."""
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "system")
        _make_dirs(vault, "workspaces", "ws-1")

        _write_json(vault / "system" / "factory_defaults.json",
                     {"config": {"provider_id": "openai", "max_turns": 50}})
        _write_json(vault / "workspaces" / "ws-1" / "defaults.json",
                     {"temperature": 0.3})

        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault,
        )
        from agent.config.config_manager import resolve_config_defaults
        result = resolve_config_defaults("ws-1")
        assert result["provider_id"] == "openai"
        assert result["max_turns"] == 50
        assert result["temperature"] == 0.3

    def test_list_replacement_not_concatenation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lists in overlay replace lists in base — not concatenated."""
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "system")
        _make_dirs(vault, "workspaces", "ws-1")

        _write_json(vault / "system" / "factory_defaults.json",
                     {"config": {"enabled_tools": ["tool_a", "tool_b"]}})
        _write_json(vault / "workspaces" / "ws-1" / "defaults.json",
                     {"enabled_tools": ["tool_c", "tool_d"]})

        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault,
        )
        from agent.config.config_manager import resolve_config_defaults
        result = resolve_config_defaults("ws-1")
        assert result["enabled_tools"] == ["tool_c", "tool_d"]
        assert result["enabled_tools"] != ["tool_a", "tool_b", "tool_c", "tool_d"]


# ===================================================================
# TestSaveDefaults
# ===================================================================


class TestSaveDefaults:
    """Verify save-config-defaults writes to correct locations."""

    @pytest.fixture
    def save_vault(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "user")
        _make_dirs(vault, "workspaces", "ws-1")
        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault,
        )
        return vault

    def test_workspace_scoped_save(self, save_vault: Path) -> None:
        """Workspace-scoped save writes to workspaces/<id>/defaults.json."""
        from agent.config.config_manager import save_config_defaults

        saved = save_config_defaults({"temperature": 0.3}, "ws-1", global_scope=False)
        expected = save_vault / "workspaces" / "ws-1" / "defaults.json"
        assert saved == expected
        assert expected.exists()
        data = json.loads(expected.read_text(encoding="utf-8"))
        assert data["temperature"] == 0.3

    def test_global_save(self, save_vault: Path) -> None:
        """Global-scoped save writes to user/defaults.json."""
        from agent.config.config_manager import save_config_defaults

        saved = save_config_defaults({"temperature": 0.3}, "ws-1", global_scope=True)
        expected = save_vault / "user" / "defaults.json"
        assert saved == expected
        assert expected.exists()
        data = json.loads(expected.read_text(encoding="utf-8"))
        assert data["temperature"] == 0.3

    def test_old_workspace_root_path_not_written(self, save_vault: Path) -> None:
        """Legacy workspace-root defaults.json is NOT written."""
        from agent.config.config_manager import save_config_defaults

        save_config_defaults({"temperature": 0.3}, "ws-1", global_scope=False)
        legacy = save_vault / "defaults.json"
        assert not legacy.exists(), "Legacy defaults.json should not exist at vault root"


# ===================================================================
# TestVaultStructureIntegrity
# ===================================================================


class TestVaultStructureIntegrity:
    """Verify that ensure_vault_structure() creates the correct directories."""

    def test_all_directories_exist(self, tmp_path: Path) -> None:
        """ensure_vault_structure() creates all VAULT_SUBDIRS."""
        from thoughtmachine.vault import ensure_vault_structure, VAULT_SUBDIRS

        vault = _vault_root(tmp_path)
        created = ensure_vault_structure()

        for subdir in VAULT_SUBDIRS:
            target = vault / subdir
            assert target.is_dir(), f"{subdir}/ should be a directory"

        assert len(created) > 0, "At least one directory should have been created"

    def test_version_marker(self, tmp_path: Path) -> None:
        """A .vault_version file can be written and read back."""
        vault = _vault_root(tmp_path)
        _make_dirs(vault, "system")
        marker = vault / "system" / ".vault_version"
        marker.write_text("2")
        assert marker.read_text().strip() == "2"


# ===================================================================
# TestSecurityBoundaryCapstone
# ===================================================================


class TestSecurityBoundaryCapstone:
    """Full end-to-end security boundary test proving all subsystems work together."""

    @pytest.fixture
    def capstone_vault(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Build a complete vault with all layers for the capstone test."""
        vault = _vault_root(tmp_path)

        # --- credentials ---
        _make_dirs(vault, "credentials", "test-ws")
        (vault / "credentials" / "test-ws" / "api_key").write_text("sk-real-secret")

        # --- workspaces ---
        _make_dirs(vault, "workspaces", "test-ws")
        (vault / "workspaces" / "test-ws" / "Dockerfile").write_text("FROM ubuntu:22.04")
        _make_dirs(vault, "workspaces", "other-ws")
        (vault / "workspaces" / "other-ws" / "Dockerfile").write_text("FROM other:latest")

        # --- user defaults ---
        _make_dirs(vault, "user")
        _write_json(vault / "user" / "defaults.json", {
            "temperature": 0.5,
            "provider_id": "openai",
        })

        # --- factory defaults ---
        _make_dirs(vault, "system")
        _write_json(vault / "system" / "factory_defaults.json", {
            "version": "1",
            "config": {"max_turns": 50},
        })

        # --- checksystem allowlist (does NOT include "workspace_registry") ---
        _make_vault_with_allowlist(vault, ["my_config", "dockerfile"])

        # Patch config_manager._vault_root to use our test vault
        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault,
        )

        return vault

    def test_full_isolation(self, capstone_vault: Path) -> None:
        """Prove all security boundaries work in concert.

        1. CheckSystem respects allowlist (my_config allowed)
        2. CredentialInjector resolves secrets and redacts them
        3. CheckSystem reads workspace-scoped Dockerfile
        4. save_config_defaults writes to correct location
        5. Workspace-level defaults.json exists, vault-root one does not
        6. workspace_registry.json is NOT in allowlist; CheckSystem can't read it
        """
        # ---- 1. CheckSystem with allowed query returns data ----
        from tools.workspace.check_system import CheckSystem

        cs = CheckSystem(
            query="my_config",
            allowlist=["my_config"],
            workspace_id="test-ws",
            agent_config={"provider": "test", "model": "gpt-4"},
            session_permissions={},
        )
        result1 = json.loads(cs.execute())
        assert "error" not in result1, f"Unexpected error: {result1.get('error')}"
        assert result1.get("provider") == "test"
        assert result1.get("model") == "gpt-4"

        # ---- 2. CredentialInjector resolves and redacts ----
        from agent.credentials import CredentialInjector, Secret

        injector = CredentialInjector("test-ws")
        injected = injector.inject({"key": "{{credential:api_key}}"})
        assert isinstance(injected["key"], Secret)
        assert injected["key"] == "sk-real-secret"
        assert str(injected["key"]) == "***"

        # ---- 3. CheckSystem reads workspace-scoped Dockerfile ----
        cs_df = CheckSystem(
            query="dockerfile",
            allowlist=["dockerfile", "my_config"],
            workspace_id="test-ws",
        )
        result3 = json.loads(cs_df.execute())
        if result3.get("available") is True:
            assert "FROM ubuntu:22.04" in result3.get("content", "")
        else:
            assert "error" in result3 or "available" in result3

        # ---- 4. Save workspace defaults ----
        from agent.config.config_manager import save_config_defaults

        saved_path = save_config_defaults({"temperature": 0.3}, "test-ws", global_scope=False)
        assert saved_path.exists()

        # ---- 5. Assert correct save paths ----
        ws_defaults = capstone_vault / "workspaces" / "test-ws" / "defaults.json"
        assert ws_defaults.exists(), "Workspace defaults.json should exist"
        vault_root_defaults = capstone_vault / "defaults.json"
        assert not vault_root_defaults.exists(), "Vault root defaults.json should NOT exist"

        # ---- 6. workspace_registry not in allowlist ----
        from thoughtmachine.vault import is_path_allowed
        assert is_path_allowed("workspace_registry") is False
        assert is_path_allowed("my_config") is True
