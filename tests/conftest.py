# Root conftest — shared fixtures for the test suite

import pytest


@pytest.fixture
def hermetic_vault(tmp_path, monkeypatch):
    import pathlib
    # Patch Path.home() BEFORE importing vault modules
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    # Now import - module-level constants use patched home
    import importlib
    import thoughtmachine.bootstrap
    import agent.knowledge.global_kb
    # Reload to force re-evaluation of module-level PATH constants
    importlib.reload(thoughtmachine.bootstrap)
    importlib.reload(agent.knowledge.global_kb)
    fake_vault = tmp_path / ".thoughtmachine"
    fake_vault.mkdir(parents=True, exist_ok=True)
    thoughtmachine.bootstrap.ensure_user_defaults()
    yield fake_vault
