"""Focused tests for reproduce_p0.py's real_vault_readonly() write barrier.

These tests load the (non-package) reproducer script by path and exercise the
guard directly. They never run the reproducer's main(), never touch the
operator's real vault, and skip cleanly if the docker SDK is unavailable.
"""
import importlib.util
import os
import pathlib

import pytest


_ENV_KEYS = ("THOUGHTMACHINE_VAULT_ROOT", "CONTAINER_AUDIT_LOG_PATH")


def _load_reproducer():
    p = (
        pathlib.Path(__file__).resolve().parents[1]
        / ".thoughtmachine"
        / "working_docs"
        / "reproduce_p0.py"
    )
    spec = importlib.util.spec_from_file_location("reproduce_p0", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _restore_env():
    """Snapshot the two env vars the reproducer/guard mutate, restore after."""
    before = {k: os.environ.get(k) for k in _ENV_KEYS}
    yield
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture()
def repro(tmp_path, monkeypatch):
    """Load the reproducer and point it at throwaway real/scratch dirs."""
    mod = _load_reproducer()
    real_vault = tmp_path / "real_vault"
    scratch = tmp_path / "scratch"
    real_vault.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(mod, "P0_REAL_VAULT_ROOT", str(real_vault))
    monkeypatch.setattr(mod, "P0_SCRATCH", str(scratch))
    monkeypatch.setattr(mod, "REAL_MODE", True)
    monkeypatch.setenv("P0_REAL_VAULT_ROOT", str(real_vault))
    monkeypatch.setenv("P0_SCRATCH", str(scratch))
    return mod, real_vault, scratch


def test_guard_blocks_persist_write_under_real_vault(repro):
    mod, real_vault, scratch = repro
    pytest.importorskip("docker")
    from infra.container_manager import ContainerManager

    repo_root = pathlib.Path(__file__).resolve().parents[1]

    # Preferred path: construct a REAL ContainerManager against the real vault.
    # Its __init__ calls docker.from_env(), so this needs a live docker daemon;
    # when the daemon is absent (as in this sandbox) that raises and we fall
    # back below. On a real docker host this branch is the one that runs.
    mgr = None
    try:
        mgr = ContainerManager(
            workspace_path=str(repo_root),
            session_id="t",
            workspace_id="p0-ws",
            session_permissions={},
            vault_root=str(real_vault),
            image="alpine:latest",
        )
    except Exception:
        # Fallback (no docker daemon): construct WITHOUT running __init__ so we
        # can still exercise the guard's write-barrier code path. The guard only
        # inspects self.vault_root, so a bare instance with that attribute set
        # is sufficient to reach the barrier.
        mgr = object.__new__(ContainerManager)
        mgr.vault_root = str(real_vault)
        mgr.workspace_id = "p0-ws"

    with mod.real_vault_readonly():
        with pytest.raises(RuntimeError, match="SAFETY VIOLATION"):
            mgr._save_container_notes()

    # Nothing may have been created anywhere under the real vault.
    assert list(real_vault.rglob("*")) == []


def test_guard_rejects_scratch_inside_real_vault(tmp_path, monkeypatch):
    mod = _load_reproducer()
    real_vault = tmp_path / "real_vault"
    real_vault.mkdir()
    scratch = real_vault / "scratch"  # configured INSIDE the real vault
    monkeypatch.setattr(mod, "P0_REAL_VAULT_ROOT", str(real_vault))
    monkeypatch.setattr(mod, "P0_SCRATCH", str(scratch))
    monkeypatch.setattr(mod, "REAL_MODE", True)
    monkeypatch.setenv("P0_REAL_VAULT_ROOT", str(real_vault))

    with pytest.raises(RuntimeError):
        with mod.real_vault_readonly():
            pass


def test_guard_restores_env(repro, monkeypatch):
    mod, real_vault, scratch = repro
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", "sentinel-root")
    monkeypatch.setenv("CONTAINER_AUDIT_LOG_PATH", "sentinel-audit")

    with mod.real_vault_readonly():
        assert os.environ["THOUGHTMACHINE_VAULT_ROOT"] == str(real_vault)
        assert os.environ["CONTAINER_AUDIT_LOG_PATH"] == os.path.join(
            str(scratch), "real-mode-scratch", "container_audit.log"
        )

    assert os.environ["THOUGHTMACHINE_VAULT_ROOT"] == "sentinel-root"
    assert os.environ["CONTAINER_AUDIT_LOG_PATH"] == "sentinel-audit"
