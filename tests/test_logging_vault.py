"""Chunk 4 - vault-resolved agent logger tests.

Proves the structured agent logger (``agent.logging.AgentLogger`` and
``agent.logging.create_logger``) writes to the canonical vault log root:

    THOUGHTMACHINE_VAULT_ROOT set  ->  $THOUGHTMACHINE_VAULT_ROOT/logs
    unset                          ->  ~/.thoughtmachine/logs

Covers:
(a) ``AgentLogger`` with no explicit ``log_dir`` resolves to the vault
    override (``$THOUGHTMACHINE_VAULT_ROOT/logs``) and never writes to the
    repo root.
(b) ``create_logger`` (the production entry point) lands in the same root.
(c) Default fallback to ``~/.thoughtmachine/logs`` when the env var is unset.
(d) Explicit ``log_dir`` (constructor arg or ``config.log_dir`` attribute)
    still wins over the vault root.

Hermetic by construction: tmp_path + monkeypatch only, no real vault, no
network, no codebase log writes.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agent.logging import AgentLogger, create_logger


def _read_jsonl(path: str) -> list:
    """Read a JSONL file; every non-empty line must parse."""
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert lines, f"expected at least one line in {path}"
    return [json.loads(ln) for ln in lines]


def _stub_config(**overrides) -> SimpleNamespace:
    """Minimal logger config WITHOUT a ``log_dir`` attribute.

    Mirrors agent/config/models.py where ``log_dir`` defaults to ``None``,
    so the production path is ``log_dir=None -> get_log_root()``.
    """
    cfg = SimpleNamespace(
        enable_logging=True,
        enable_file_logging=True,
        jsonl_format=True,
        max_file_size_mb=10,
        log_level="INFO",
        file_log_level="DEBUG",
        log_categories=["SESSION", "LLM", "TOOLS"],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point HOME + THOUGHTMACHINE_VAULT_ROOT at tmp_path; neutralise env
    overrides; close any logger file handles on teardown."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENT_LOG_CATEGORIES", raising=False)
    monkeypatch.delenv("TM_LOG_FILE_LEVEL", raising=False)
    created: list = []
    yield tmp_path, created
    for logger in created:
        handle = getattr(logger, "_file_handle", None)
        if handle is not None and not handle.closed:
            handle.close()


def _make(created: list, config, **kwargs) -> AgentLogger:
    logger = AgentLogger(config, **kwargs)
    created.append(logger)
    return logger


class TestAgentLoggerVaultRoot:
    def test_resolves_to_vault_override_and_writes_there(self, hermetic):
        tmp_path, created = hermetic
        repo_before = set(os.listdir(os.getcwd()))
        logger = _make(created, _stub_config(), session_id="vault-test-1")
        logger.log_info("test", "hello from vault root")

        assert os.path.abspath(logger.log_dir) == str(tmp_path / "logs")
        log_file = os.path.join(logger.log_dir, "agent_vault-test-1.jsonl")
        assert os.path.isfile(log_file)

        (record,) = _read_jsonl(log_file)
        assert record["type"] == "agent_start"
        assert record["level"] == "INFO"
        assert record["session_id"] == "vault-test-1"
        assert record["data"]["message"] == "hello from vault root"

        # Nothing new appeared at the repo root.
        new_entries = set(os.listdir(os.getcwd())) - repo_before
        assert "logs" not in new_entries, f"repo-root logs/ created: {new_entries}"
        assert new_entries == set(), f"new entries at repo root: {new_entries}"

    def test_create_logger_lands_in_vault_root(self, hermetic):
        tmp_path, created = hermetic
        logger = create_logger(_stub_config(session_id="create-test-1"))
        assert logger is not None
        created.append(logger)
        logger.log_info("test", "hello via create_logger")

        assert os.path.abspath(logger.log_dir) == str(tmp_path / "logs")
        log_file = os.path.join(logger.log_dir, "agent_create-test-1.jsonl")
        assert os.path.isfile(log_file)
        assert _read_jsonl(log_file)[0]["session_id"] == "create-test-1"

    def test_defaults_to_home_vault_when_env_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("THOUGHTMACHINE_VAULT_ROOT", raising=False)
        monkeypatch.delenv("AGENT_LOG_CATEGORIES", raising=False)
        monkeypatch.delenv("TM_LOG_FILE_LEVEL", raising=False)
        logger = AgentLogger(_stub_config(), session_id="fallback-test-1")
        try:
            logger.log_info("test", "fallback to home vault")
            expected_dir = str(tmp_path / ".thoughtmachine" / "logs")
            assert os.path.abspath(logger.log_dir) == expected_dir
            log_file = os.path.join(logger.log_dir, "agent_fallback-test-1.jsonl")
            assert os.path.isfile(log_file)
            assert _read_jsonl(log_file)[0]["session_id"] == "fallback-test-1"
        finally:
            handle = getattr(logger, "_file_handle", None)
            if handle is not None and not handle.closed:
                handle.close()

    def test_explicit_log_dir_wins(self, hermetic):
        tmp_path, created = hermetic
        custom = str(tmp_path / "custom")
        logger = _make(created, _stub_config(), log_dir=custom, session_id="explicit-1")
        logger.log_info("test", "explicit dir")
        assert os.path.abspath(logger.log_dir) == custom
        assert os.path.isfile(os.path.join(custom, "agent_explicit-1.jsonl"))

    def test_config_log_dir_attribute_wins_in_create_logger(self, hermetic):
        tmp_path, created = hermetic
        cfg_dir = str(tmp_path / "cfgdir")
        logger = create_logger(_stub_config(session_id="cfgdir-1", log_dir=cfg_dir))
        assert logger is not None
        created.append(logger)
        logger.log_info("test", "config log_dir")
        assert os.path.abspath(logger.log_dir) == cfg_dir
        assert os.path.isfile(os.path.join(cfg_dir, "agent_cfgdir-1.jsonl"))
