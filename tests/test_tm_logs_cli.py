"""Chunk 4 - CLI tests for ``tm-logs`` (agent.cli.logs), run in-process.

Hermetic: THOUGHTMACHINE_VAULT_ROOT is set per-test to a tmp_path, the
lifecycle LOG_DIR module attribute (import-time bound) is re-pointed there,
and sys.argv is monkeypatched before calling agent.cli.logs.main().  The CLI
resolves its log root at runtime from THOUGHTMACHINE_VAULT_ROOT.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from agent.logging import lifecycle


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _seed_all_streams() -> None:
    lifecycle.log_session_event("session_started", session_id="sess-1", workspace_id="ws-1")
    lifecycle.log_session_event("session_ended", session_id="sess-1", workspace_id="ws-1")
    lifecycle.log_session_event("session_started", session_id="sess-2", workspace_id="ws-2")
    lifecycle.log_worker_event("worker-alpha", "worker_spawned", session_id="sess-1", worker_id="w-1")
    lifecycle.log_worker_event("worker-alpha", "worker_completed", session_id="sess-1", worker_id="w-1")
    lifecycle.log_worker_event("worker-beta", "worker_spawned", session_id="sess-2", worker_id="w-2")
    lifecycle.log_container_event("container_started", container_id="c-1", session_id="sess-1", workspace_id="ws-1")
    lifecycle.log_container_event("container_stopped", container_id="c-1", session_id="sess-1", workspace_id="ws-1")
    lifecycle.log_provider_event(
        content="hello from provider",
        model_name="gpt-4o",
        request_id="req-1",
        finish_reason="stop",
        session_id="sess-1", worker_id="w-1", query_id="q-1", correlation_id="corr-1",
    )
    lifecycle.log_provider_event(
        content="a very long output that hit the length limit",
        finish_reason="length",
        stop_reason="max_tokens",
        session_id="sess-1", worker_id="w-1", query_id="q-2",
    )


@pytest.fixture
def seeded_vault(tmp_path, monkeypatch):
    """Seed all four lifecycle streams under a temp vault root."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(lifecycle, "LOG_DIR", str(tmp_path / "logs"))
    _seed_all_streams()
    lifecycle.close_streams()
    return tmp_path


def run_cli(monkeypatch, capsys, *argv):
    """Run tm-logs in-process; return (rc, stdout, stderr)."""
    import agent.cli.logs as cli
    monkeypatch.setattr(sys, "argv", ["tm-logs", *argv])
    rc = cli.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# formats: session / worker / container / stop-reasons in table + json (human once)
# ---------------------------------------------------------------------------


class TestFormats:
    def test_session_json(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session", "--format", "json")
        assert rc == 0
        records = json.loads(out)
        assert len(records) == 3
        assert {r["event"] for r in records} == {"session_started", "session_ended"}
        assert all(r["session_id"] in ("sess-1", "sess-2") for r in records)
        assert all(r["level"] == "INFO" and "timestamp" in r for r in records)

    def test_session_table(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session", "--format", "table")
        assert rc == 0
        assert "EVENT" in out and "TIMESTAMP" in out and "LEVEL" in out
        assert "SESSION_ID" in out and "WORKSPACE_ID" in out
        assert "session_started" in out and "sess-1" in out

    def test_session_human(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session")
        assert rc == 0
        assert "session/session_started" in out
        assert "session_id=sess-1" in out
        assert "workspace_id=ws-1" in out

    def test_worker_json(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "worker",
                               "--worker-name", "worker-alpha", "--format", "json")
        assert rc == 0
        records = json.loads(out)
        assert len(records) == 2
        assert all(r["worker_name"] == "worker-alpha" for r in records)
        assert all(r["worker_id"] == "w-1" for r in records)
        assert {r["event"] for r in records} == {"worker_spawned", "worker_completed"}

    def test_worker_table(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "worker",
                               "--worker-name", "worker-alpha", "--format", "table")
        assert rc == 0
        assert "WORKER_NAME" in out and "WORKER_ID" in out and "SESSION_ID" in out
        assert "worker-alpha" in out

    def test_worker_safe_name_missing_file(self, seeded_vault, monkeypatch, capsys):
        # 'worker beta!' sanitizes to worker_worker_beta_.log which we never seeded
        rc, out, err = run_cli(monkeypatch, capsys, "worker",
                               "--worker-name", "worker beta!")
        assert rc == 0
        assert out == ""
        assert "stream file not found" in err

    def test_container_json(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "container", "--format", "json")
        assert rc == 0
        records = json.loads(out)
        assert len(records) == 2
        assert {r["event"] for r in records} == {"container_started", "container_stopped"}
        assert all(r["container_id"] == "c-1" for r in records)

    def test_container_table(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "container", "--format", "table")
        assert rc == 0
        assert "CONTAINER_ID" in out and "WORKSPACE_ID" in out
        assert "container_started" in out

    def test_stop_reasons_json(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "stop-reasons", "--format", "json")
        assert rc == 0
        parsed = json.loads(out)
        assert parsed == {
            "finish_reason": {"stop": 1, "length": 1},
            "stop_reason": {"max_tokens": 1},
            "total": 2,
        }

    def test_stop_reasons_table(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "stop-reasons", "--format", "table")
        assert rc == 0
        assert "finish_reason stop" in out
        assert "finish_reason length" in out
        assert "stop_reason max_tokens" in out
        assert "TOTAL" in out


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_filter_since_until(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--since", "2000-01-01T00:00:00Z", "--format", "json")
        assert len(json.loads(out)) == 3
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--until", "2000-01-01T00:00:00Z", "--format", "json")
        assert json.loads(out) == []
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--since", "2999-01-01T00:00:00Z")
        assert "(no matching records)" in out

    def test_filter_session_id(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--session-id", "sess-1", "--format", "json")
        records = json.loads(out)
        assert len(records) == 2
        assert all(r["session_id"] == "sess-1" for r in records)

    def test_filter_level_case_insensitive(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--level", "INFO", "--format", "json")
        assert len(json.loads(out)) == 3
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--level", "info", "--format", "json")
        assert len(json.loads(out)) == 3
        rc, out, err = run_cli(monkeypatch, capsys, "session", "--level", "error")
        assert "(no matching records)" in out

    def test_filter_event_type_case_insensitive(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--event-type", "session_started", "--format", "json")
        records = json.loads(out)
        assert len(records) == 2
        assert all(r["event"] == "session_started" for r in records)
        rc, out, err = run_cli(monkeypatch, capsys, "session",
                               "--event-type", "SESSION_STARTED", "--format", "json")
        assert len(json.loads(out)) == 2

    def test_filter_stop_reason(self, seeded_vault, monkeypatch, capsys):
        rc, out, err = run_cli(monkeypatch, capsys, "stop-reasons",
                               "--stop-reason", "stop", "--format", "json")
        parsed = json.loads(out)
        assert parsed["total"] == 1
        assert parsed["finish_reason"] == {"stop": 1}
        rc, out, err = run_cli(monkeypatch, capsys, "stop-reasons",
                               "--stop-reason", "max_tokens", "--format", "json")
        parsed = json.loads(out)
        assert parsed["total"] == 1
        assert parsed["stop_reason"] == {"max_tokens": 1}


# ---------------------------------------------------------------------------
# missing stream file + bad args
# ---------------------------------------------------------------------------


class TestErrorsAndBadArgs:
    def test_missing_stream_file_exits_zero(self, tmp_path, monkeypatch, capsys):
        # empty vault root: stream file does not exist
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        rc, out, err = run_cli(monkeypatch, capsys, "session")
        assert rc == 0
        assert out == ""
        assert "stream file not found" in err

    def test_bad_args_worker_without_worker_name(self, seeded_vault, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, capsys, "worker")
        assert exc.value.code == 2

    def test_bad_args_worker_name_on_session(self, seeded_vault, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, capsys, "session", "--worker-name", "x")
        assert exc.value.code == 2

    def test_bad_args_invalid_since(self, seeded_vault, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, capsys, "session", "--since", "not-a-date")
        assert exc.value.code == 2

    def test_bad_args_stop_reason_on_session(self, seeded_vault, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, capsys, "session", "--stop-reason", "stop")
        assert exc.value.code == 2
