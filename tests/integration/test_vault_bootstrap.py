"""Integration tests: Vault bootstrap with patched Path.home().

Tests that ``thoughtmachine.bootstrap.ensure_user_defaults()`` correctly
creates the ``~/.thoughtmachine/`` directory structure and deploys all
resource files from MANIFEST.json when ``Path.home()`` is redirected to a
temporary directory.
"""

import json
import sys
from pathlib import Path

import pytest


# The module-level constant USER_DIR = Path.home() / ".thoughtmachine" in
# thoughtmachine/bootstrap.py is evaluated at import time.  We must patch
# Path.home() *before* the module is first imported (or use reload).
# The strategy: patch in the fixture, then import / reload bootstrap.


_ENGINEER_HINT = "AI software engineer"
_AGENT_HINT = "AI agent"


class TestVaultBootstrap:
    """Bootstrap the vault under a temp home directory."""

    @pytest.fixture
    def fake_home(self, monkeypatch, tmp_path: Path) -> Path:
        """Redirect ``Path.home()`` to a tmp_path and yield it."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        return tmp_path

    @pytest.fixture
    def bootstrap(self, fake_home: Path):
        """Return a *fresh* ``thoughtmachine.bootstrap`` module.

        Because ``USER_DIR`` is set at import time we reload the module so
        it picks up the monkeypatched ``Path.home()``.
        """
        import importlib
        from thoughtmachine import bootstrap

        # Reload to re-evaluate the module-level USER_DIR constant
        bootstrap = importlib.reload(bootstrap)
        return bootstrap

    # ── directory structure ──────────────────────────────────────────────

    def test_directories_created(self, bootstrap, fake_home: Path) -> None:
        """ensure_user_defaults() creates the required subdirectory tree."""
        vault = fake_home / ".thoughtmachine"
        assert not vault.exists(), "Precondition: vault should not exist yet"

        bootstrap.ensure_user_defaults(overwrite_existing=False)

        assert vault.is_dir(), f"Vault root not created at {vault}"
        assert (vault / "system").is_dir()
        assert (vault / "user").is_dir()
        assert (vault / "credentials").is_dir()
        assert (vault / "workspaces").is_dir()
        assert (vault / "global").is_dir()
        assert (vault / "state").is_dir()
        assert (vault / "logs").is_dir()

    def test_resource_files_deployed(self, bootstrap, fake_home: Path) -> None:
        """All non-internal manifest files are copied to the vault."""
        bootstrap.ensure_user_defaults(overwrite_existing=False)
        vault = fake_home / ".thoughtmachine"

        # Files from MANIFEST: default_system_prompt.txt -> default_system_prompt.txt
        #                      default_config.json -> agent_config.json
        #                      default_providers.json -> providers.json
        #                      engineer_system_prompt.txt -> engineer_system_prompt.txt
        assert (vault / "default_system_prompt.txt").is_file()
        assert (vault / "agent_config.json").is_file()
        assert (vault / "providers.json").is_file()
        assert (vault / "engineer_system_prompt.txt").is_file()

        # .version is marked "internal": true and should NOT be deployed
        assert not (vault / ".version").exists()

    def test_resource_file_content(self, bootstrap, fake_home: Path) -> None:
        """Deployed resource files contain the expected content."""
        bootstrap.ensure_user_defaults(overwrite_existing=False)
        vault = fake_home / ".thoughtmachine"

        agent_prompt = (vault / "default_system_prompt.txt").read_text()
        assert _AGENT_HINT in agent_prompt

        engineer_prompt = (vault / "engineer_system_prompt.txt").read_text()
        assert _ENGINEER_HINT in engineer_prompt

        config = json.loads((vault / "agent_config.json").read_text())
        assert "model" in config
        assert "provider_id" in config
        assert "temperature" in config
        assert "enabled_tools" in config

    # ── overwrite_existing behaviour ─────────────────────────────────────

    def test_overwrite_false_skips_existing(self, bootstrap, fake_home: Path) -> None:
        """With overwrite_existing=False, existing files are not touched."""
        vault = fake_home / ".thoughtmachine"
        vault.mkdir(parents=True, exist_ok=True)
        # Write a stale config
        (vault / "agent_config.json").write_text('{"stale": true}')

        touched = bootstrap.ensure_user_defaults(overwrite_existing=False)
        # The stale file should NOT have been touched
        assert str(vault / "agent_config.json") not in touched
        assert json.loads((vault / "agent_config.json").read_text()) == {"stale": True}

    def test_overwrite_true_replaces_existing(self, bootstrap, fake_home: Path) -> None:
        """With overwrite_existing=True, existing files are replaced."""
        vault = fake_home / ".thoughtmachine"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "agent_config.json").write_text('{"stale": true}')

        touched = bootstrap.ensure_user_defaults(overwrite_existing=True)
        # The file SHOULD have been replaced
        assert str(vault / "agent_config.json") in touched
        restored = json.loads((vault / "agent_config.json").read_text())
        assert "model" in restored
        assert restored.get("model"), "Overwritten config should have real content"

    # ── load_user_config ─────────────────────────────────────────────────

    def test_load_user_config_returns_defaults(self, bootstrap, fake_home: Path) -> None:
        """After bootstrap, load_user_config() returns the deployed config."""
        bootstrap.ensure_user_defaults(overwrite_existing=False)
        cfg = bootstrap.load_user_config()
        assert isinstance(cfg, dict)
        assert "model" in cfg
        assert "provider_id" in cfg

    def test_load_user_config_falls_back(self, bootstrap, fake_home: Path) -> None:
        """If no user config exists, load_user_config() returns built-in defaults."""
        vault = fake_home / ".thoughtmachine"
        vault.mkdir(parents=True, exist_ok=True)
        # Do NOT call ensure_user_defaults — no config file should exist
        assert not (vault / "user" / "defaults.json").exists()

        cfg = bootstrap.load_user_config()
        assert isinstance(cfg, dict)
        assert "model" in cfg  # falls back to resources/default_config.json

    # ── Integration: creating a SessionConfig from bootstrap ─────────────

    def test_session_config_from_bootstrap(self, bootstrap, fake_home: Path) -> None:
        """A SessionConfig can be created from the bootstrap default config."""
        bootstrap.ensure_user_defaults(overwrite_existing=False)
        cfg_dict = bootstrap.load_user_config()

        from agent.config.session_config import SessionConfig

        config = SessionConfig(
            mode="agent",
            provider_id=cfg_dict.get("provider_id", "v4_flash"),
            model=cfg_dict.get("model", "deepseek-v4-flash"),
            temperature=cfg_dict.get("temperature", 0.7),
        )
        assert config.mode == "agent"
        assert config.provider_id == cfg_dict.get("provider_id")
        assert config.model == cfg_dict.get("model")
