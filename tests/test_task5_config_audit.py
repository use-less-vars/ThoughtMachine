"""Task 5 — config audit logging (agent/config/audit.py).

Covers:
1. ``redact_config``: recursive redaction of sensitive keys (variants and
   suffixes), Path -> str conversion, scalar passthrough.
2. ``log_config_audit``: JSONL record written with the documented keys,
   secrets redacted, source validated, and the never-raises contract.
3. ``restart_required_for``: mirrors the agent's hot-swap blocking fields
   (provider_type/model/api_key/base_url/system_prompt/workspace_path/
   provider_config).
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import agent.config.audit as audit_module
from agent.config.audit import (
    REDACTED,
    VALID_SOURCES,
    log_config_audit,
    redact_config,
    restart_required_for,
)


# ─────────────────────────────────────────────────────────────────────
# 1. redact_config
# ─────────────────────────────────────────────────────────────────────

class TestRedactConfig:

    def test_redacts_direct_api_key(self):
        assert redact_config({"api_key": "sk-secret"}) == {"api_key": REDACTED}

    def test_redacts_key_variants_and_suffixes(self):
        data = {
            "apiKey": "a",
            "API-Key": "b",
            "openai_api_key": "c",
            "my_secret": "d",
            "auth_password": "e",
            "authorization": "f",
            "model": "gpt-4",  # safe key must survive untouched
        }
        out = redact_config(data)
        assert out["apiKey"] == REDACTED
        assert out["API-Key"] == REDACTED
        assert out["openai_api_key"] == REDACTED
        assert out["my_secret"] == REDACTED
        assert out["auth_password"] == REDACTED
        assert out["authorization"] == REDACTED
        assert out["model"] == "gpt-4"

    def test_redacts_nested_structures(self):
        out = redact_config({
            "provider": {"api_key": "x", "timeout": 10},
            "list": [{"token": "y"}, "plain"],
        })
        assert out["provider"]["api_key"] == REDACTED
        assert out["provider"]["timeout"] == 10
        assert out["list"][0]["token"] == REDACTED
        assert out["list"][1] == "plain"

    def test_path_becomes_str(self):
        out = redact_config({"workspace_path": Path("/tmp/ws")})
        assert out["workspace_path"] == "/tmp/ws"
        assert isinstance(out["workspace_path"], str)

    def test_scalars_passthrough(self):
        assert redact_config(None) is None
        assert redact_config(42) == 42
        assert redact_config("hello") == "hello"
        assert redact_config(True) is True


# ─────────────────────────────────────────────────────────────────────
# 2. log_config_audit
# ─────────────────────────────────────────────────────────────────────

class TestLogConfigAudit:

    @pytest.fixture(autouse=True)
    def _audit_path(self, tmp_path, monkeypatch):
        audit_file = tmp_path / "config_audit.jsonl"
        monkeypatch.setattr(audit_module, "AUDIT_LOG_PATH", audit_file)
        return audit_file

    def _read_records(self, audit_file):
        assert audit_file.exists(), "audit file was not created"
        return [json.loads(line) for line in
                audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_writes_jsonl_record_with_all_keys(self, _audit_path):
        log_config_audit(
            source="user",
            component="config.apply",
            old={"model": "a"},
            new={"model": "b", "api_key": "sk-secret"},
            restart_required=True,
            injected={"api_key": "sk-secret"},
            extra={"session_id": "s1"},
        )
        records = self._read_records(_audit_path)
        assert len(records) == 1
        rec = records[0]
        assert rec["source"] == "user"
        assert rec["component"] == "config.apply"
        assert rec["old"] == {"model": "a"}
        assert rec["new"] == {"model": "b", "api_key": REDACTED}
        assert rec["restart_required"] is True
        assert rec["injected"] == {"api_key": REDACTED}
        assert rec["extra"] == {"session_id": "s1"}
        assert rec["timestamp"]

    def test_secrets_never_leave_the_process(self, _audit_path):
        log_config_audit(new={"api_key": "sk-super-secret"}, old=None)
        rec = self._read_records(_audit_path)[0]
        blob = json.dumps(rec)
        assert "sk-super-secret" not in blob
        assert rec["new"]["api_key"] == REDACTED

    def test_invalid_source_falls_back_to_system(self, _audit_path):
        log_config_audit(source="nonsense")
        rec = self._read_records(_audit_path)[0]
        assert rec["source"] == "system"

    def test_restart_required_none_serialised_as_null(self, _audit_path):
        log_config_audit(old=None, new=None, restart_required=None)
        rec = self._read_records(_audit_path)[0]
        assert rec["restart_required"] is None

    def test_never_raises_on_unwritable_path(self, tmp_path, monkeypatch):
        # Parent is a regular file -> mkdir/open must fail.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setattr(
            audit_module, "AUDIT_LOG_PATH", blocker / "nested" / "audit.jsonl")
        # Must not raise despite the failing write.
        log_config_audit(source="user", new={"model": "a"})

    def test_file_mode_is_0600(self, _audit_path):
        log_config_audit(new={"model": "a"})
        mode = os.stat(str(_audit_path)).st_mode & 0o777
        assert mode == 0o600


# ─────────────────────────────────────────────────────────────────────
# 3. restart_required_for
# ─────────────────────────────────────────────────────────────────────

class TestRestartRequiredFor:

    def test_identical_configs_no_restart(self):
        assert restart_required_for({"model": "a"}, {"model": "a"}) is False

    def test_blocking_field_change_requires_restart(self):
        for field in ("provider_type", "model", "api_key", "base_url",
                      "system_prompt", "workspace_path", "provider_config"):
            assert restart_required_for(
                {field: "x"}, {field: "y"}) is True, field

    def test_non_blocking_change_no_restart(self):
        assert restart_required_for(
            {"temperature": 0.1}, {"temperature": 0.9}) is False

    def test_none_handling(self):
        assert restart_required_for(None, None) is None
        assert restart_required_for(None, {"model": "a"}) is True
        assert restart_required_for({"model": "a"}, None) is True

    def test_equal_api_key_is_not_a_restart(self):
        cfg1 = {"api_key": "same", "model": "a"}
        cfg2 = {"api_key": "same", "model": "a"}
        assert restart_required_for(cfg1, cfg2) is False

    def test_never_raises_on_junk(self):
        assert restart_required_for(object(), object()) is None
        assert restart_required_for(42, "nope") is None
