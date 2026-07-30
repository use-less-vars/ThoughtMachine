# Root conftest — shared fixtures for the test suite

import json
import pathlib

import pytest


@pytest.fixture
def hermetic_vault(tmp_path, monkeypatch):
    """
    Hermetic vault fixture for integration tests.

    Creates a complete, isolated vault at ``tmp_path /.thoughtmachine`` that
    complies with the design spec.  Includes all 8 required subdirectories,
    factory defaults, and user defaults, but **no** workspace-specific defaults.

    Patches:
    - ``pathlib.Path.home()`` → ``tmp_path`` (catches all code using ``~/.thoughtmachine``)
    - ``thoughtmachine.vault.vault_root()`` → vault path (catches direct callers)
    """
    # Patch Path.home() BEFORE any vault modules are imported at test time
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    # Reload modules whose module-level PATH constants are set at import time
    import importlib
    import thoughtmachine.bootstrap
    import agent.knowledge.global_kb
    importlib.reload(thoughtmachine.bootstrap)
    importlib.reload(agent.knowledge.global_kb)

    vault_path = tmp_path / ".thoughtmachine"
    vault_path.mkdir(parents=True, exist_ok=True)

    # 1. Create all 8 required spec directories
    for subdir in ("credentials", "global", "logs", "sessions", "state", "system", "user", "workspaces"):
        (vault_path / subdir).mkdir(parents=True, exist_ok=True)

    # 2. Write factory defaults with the exact schema expected by load_factory_defaults()
    factory_defaults = {
        "version": "1",
        "description": "System factory defaults — immutable base configuration for ThoughtMachine vault.",
        "config": {
            "max_turns": 50,
            "temperature": 0.7,
            "provider_id": "",
            "model": "",
            "system_prompt": "",
        },
    }
    (vault_path / "system" / "factory_defaults.json").write_text(
        json.dumps(factory_defaults, indent=2)
    )

    # 3. Write user defaults with minimal config
    (vault_path / "user" / "defaults.json").write_text(
        json.dumps({}, indent=2)
    )

    # 4. Monkeypatch vault_root() for code that uses it directly
    import thoughtmachine.vault
    monkeypatch.setattr(thoughtmachine.vault, "vault_root", lambda: vault_path)

    yield vault_path
