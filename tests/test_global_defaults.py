"""
Tests for global-default config persistence (config-ownership Path A).

Proofs covered:
    T1  ``server.save_global_defaults`` never writes ``agent_config.json``
        and persists only the ``GLOBAL_DEFAULT_KEYS`` subset.
    T2  The only vault file it writes is ``user/defaults.json``.
    T3  It delegates to ``agent.config.config_manager.save_config_defaults``
        with ``global_scope=True`` and a merged dict (Path A call args).
    T4  ``SessionManager.create_session`` applies saved global defaults to
        new session configs.
    T5  Absent global-default keys fall back to constructor defaults.

See ``docs/architecture/config_ownership.md`` for the ownership model.
"""

import json
import tempfile
from pathlib import Path

import web_ui.backend.server as server_mod
from session.store import FileSystemSessionStore
from web_ui.backend.config_manager import ConfigManager, GLOBAL_DEFAULT_KEYS
from web_ui.backend.session_manager import SessionManager


def _read_json(path: Path):
    with open(path) as f:
        return json.load(f)


def _vault_file_snapshot(vault: Path) -> dict:
    """Map relative path -> raw bytes for every file under the vault."""
    snapshot = {}
    for p in sorted(vault.rglob("*")):
        if p.is_file():
            snapshot[str(p.relative_to(vault))] = p.read_bytes()
    return snapshot


class TestSaveGlobalDefaults:
    """``server.save_global_defaults`` write-path proofs."""

    def test_t1_does_not_write_agent_config_json(self, hermetic_vault):
        """agent_config.json untouched; only allowlist keys persisted."""
        sentinel = {"sentinel": True}
        agent_cfg = hermetic_vault / "agent_config.json"
        agent_cfg.write_text(json.dumps(sentinel))

        payload = {
            "provider_id": "p1",
            "model": "m1",
            "base_url": "http://localhost:11434/v1",
            "temperature": 0.4,
            "max_turns": 5,
            "system_prompt": "sys prompt",
            # Junk that must NOT persist into the global-default layer:
            "workspace_path": "/some/workspace",
            "workspace_id": "ws_1",
            "mode": "agent",
        }

        saved_path = server_mod.save_global_defaults(payload)

        # Proof 1: agent_config.json untouched
        assert json.loads(agent_cfg.read_text()) == sentinel

        # Returned path is the global-defaults file
        assert saved_path == hermetic_vault / "user" / "defaults.json"

        defaults = _read_json(hermetic_vault / "user" / "defaults.json")
        # Proofs 2-5: every allowlist value persisted
        assert defaults["provider_id"] == "p1"
        assert defaults["model"] == "m1"
        assert defaults["base_url"] == "http://localhost:11434/v1"
        assert defaults["temperature"] == 0.4
        assert defaults["max_turns"] == 5
        assert defaults["system_prompt"] == "sys prompt"
        # Proof 6: junk keys NOT persisted
        assert "workspace_path" not in defaults
        assert "workspace_id" not in defaults
        assert "mode" not in defaults

    def test_t2_writes_only_user_defaults_json(self, hermetic_vault):
        """The only vault file changed is user/defaults.json."""
        before = _vault_file_snapshot(hermetic_vault)

        server_mod.save_global_defaults({
            "provider_id": "p1",
            "model": "m1",
            "base_url": "http://b",
            "temperature": 0.2,
            "max_turns": 9,
            "system_prompt": "sp",
        })

        after = _vault_file_snapshot(hermetic_vault)
        changed = {k for k in after if after[k] != before.get(k)}
        assert changed == {"user/defaults.json"}

    def test_t3_path_a_call_args(self, hermetic_vault, monkeypatch):
        """Delegates to save_config_defaults(global_scope=True) with merged dict."""
        calls = []

        def fake_save_config_defaults(config_dict, workspace_id, *, global_scope=False):
            calls.append((config_dict, workspace_id, global_scope))
            return Path(hermetic_vault) / "user" / "defaults.json"

        monkeypatch.setattr(server_mod, "save_config_defaults", fake_save_config_defaults)
        monkeypatch.setattr(
            server_mod,
            "load_global_defaults",
            lambda: {"temperature": 0.5, "old_key": "keep"},
        )

        payload = {
            "provider_id": "p2",
            "model": "m2",
            "base_url": "http://b",
            "temperature": 0.9,
            "max_turns": 12,
            "system_prompt": "sys",
            "workspace_id": "ws_9",
            "mode": "agent",
        }
        server_mod.save_global_defaults(payload)

        assert len(calls) == 1
        config_dict, workspace_id, global_scope = calls[0]
        assert global_scope is True
        assert workspace_id == "ws_9"
        assert config_dict == {
            "temperature": 0.9,
            "old_key": "keep",
            "provider_id": "p2",
            "model": "m2",
            "base_url": "http://b",
            "max_turns": 12,
            "system_prompt": "sys",
        }

        # Second call without workspace_id -> ""
        calls.clear()
        server_mod.save_global_defaults({"temperature": 0.1})
        assert calls[0][1] == ""


class TestSessionManagerGlobalDefaults:
    """``SessionManager.create_session`` global-default application proofs."""

    @staticmethod
    def _make_manager():
        store = FileSystemSessionStore(
            sessions_dir=tempfile.mkdtemp(prefix="test_global_defaults_")
        )
        return store, SessionManager(session_store=store, config_manager=ConfigManager())

    def test_t4_session_honors_saved_defaults(self, hermetic_vault):
        """All six saved global-default keys land in the session config."""
        (hermetic_vault / "user" / "defaults.json").write_text(json.dumps({
            "provider_id": "p1",
            "model": "m1",
            "base_url": "http://localhost:11434/v1",
            "temperature": 0.3,
            "max_turns": 7,
            "system_prompt": "Saved prompt",
        }))

        store, manager = self._make_manager()
        session_id, _ = manager.create_session(mode="custom")
        loaded = store.load_session(session_id)
        assert loaded is not None
        cfg = loaded.metadata["session_config"]

        assert cfg["provider_id"] == "p1"
        assert cfg["model"] == "m1"
        assert cfg["base_url"] == "http://localhost:11434/v1"
        assert cfg["temperature"] == 0.3
        assert cfg["max_turns"] == 7
        assert cfg["system_prompt"] == "Saved prompt"

    def test_t5_absent_keys_fall_back(self, hermetic_vault):
        """Empty defaults.json -> constructor fallbacks, no system_prompt key."""
        # The hermetic_vault fixture writes user/defaults.json = {} already.
        store, manager = self._make_manager()
        session_id, _ = manager.create_session(mode="custom")
        loaded = store.load_session(session_id)
        assert loaded is not None
        cfg = loaded.metadata["session_config"]

        assert cfg["max_turns"] == 100
        assert cfg["temperature"] == 0.7
        assert cfg["provider_id"] == ""
        assert cfg["model"] == ""
        assert cfg["base_url"] == ""
        assert "system_prompt" not in cfg
