"""Chunk 4 - logging foundation tests.

Covers:
(a) JSON validity + envelope fields for the five structured streams
    (session.log, worker_<safe>.log, container.log, provider_raw.jsonl via
    agent.logging.lifecycle; event_log.jsonl via
    agent.logging.event_logger.EventLogger).
(b) Redaction unit tests for agent.logging.redaction.redact().
(c) JsonlStreamWriter rotation (size-based, keep 1 backup, no lost records).
(d) Hermeticity: lifecycle + EventLogger activity never writes to the repo
    root (HOME / THOUGHTMACHINE_VAULT_ROOT are patched to tmp_path; the log
    root is resolved dynamically from the env var via
    agent._log_root.get_log_root - no import-time binding).

Hermetic by construction: tmp_path + monkeypatch only, no real vault, no
network, no codebase log writes.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import datetime

import pytest

from agent.logging import lifecycle
from agent.logging.redaction import redact
from agent.logging.streams import (
    DEFAULT_KEEP_BACKUPS,
    DEFAULT_MAX_BYTES,
    JsonlStreamWriter,
)

#: Envelope fields injected by JsonlStreamWriter.write() on every record.
ENVELOPE_FIELDS = (
    "timestamp", "level", "logger", "event",
    "session_id", "worker_id", "query_id", "correlation_id", "container_id",
    "pid", "thread_id",
)

#: ISO-8601 UTC with milliseconds and a trailing Z (lifecycle format).
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _read_jsonl(path: str) -> list:
    """Read a JSONL file; every non-empty line must parse."""
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert lines, f"expected at least one line in {path}"
    return [json.loads(ln) for ln in lines]


def _assert_lifecycle_envelope(
    rec: dict, *, event: str | None = None, stream: str | None = None
) -> None:
    """Assert the full lifecycle envelope on a parsed record."""
    for field in ENVELOPE_FIELDS:
        assert field in rec, f"missing envelope field {field!r} in {rec!r}"
    assert _TS_RE.match(rec["timestamp"]), f"bad timestamp {rec['timestamp']!r}"
    dt = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
    assert dt.utcoffset() is not None, "timestamp must carry a UTC offset"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "thoughtmachine.lifecycle"
    assert isinstance(rec["pid"], int)
    assert isinstance(rec["thread_id"], int)
    if event is not None:
        assert rec["event"] == event
    if stream is not None:
        assert rec["stream"] == stream


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point HOME + THOUGHTMACHINE_VAULT_ROOT at tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    yield tmp_path
    lifecycle.close_streams()
    from agent.logging.event_logger import EventLogger
    EventLogger._instance = None


# ---------------------------------------------------------------------------
# (a) stream JSON validity + envelope
# ---------------------------------------------------------------------------


class TestStreamJsonEnvelope:
    def test_session_stream_json_envelope(self, hermetic):
        lifecycle.log_session_event(
            "session_started", session_id="sess-1", workspace_id="ws-1"
        )
        lifecycle.log_session_event(
            "session_ended", session_id="sess-1", workspace_id="ws-1"
        )
        lifecycle.close_streams()
        records = _read_jsonl(os.path.join(str(hermetic), "logs", "session.log"))
        assert [r["event"] for r in records] == ["session_started", "session_ended"]
        for rec in records:
            _assert_lifecycle_envelope(rec, stream="session")
            assert rec["session_id"] == "sess-1"
            assert rec["workspace_id"] == "ws-1"
            assert rec["data"] == {}

    def test_worker_stream_json_envelope(self, hermetic):
        lifecycle.log_worker_event(
            "researcher-1", "worker_spawned", session_id="sess-1", worker_id="w-1"
        )
        lifecycle.log_worker_event(
            "researcher-1", "worker_completed", session_id="sess-1", worker_id="w-1"
        )
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "worker_researcher-1.log")
        records = _read_jsonl(path)
        assert [r["event"] for r in records] == ["worker_spawned", "worker_completed"]
        for rec in records:
            _assert_lifecycle_envelope(rec, stream="worker")
            assert rec["worker_name"] == "researcher-1"
            assert rec["worker_id"] == "w-1"
            assert rec["session_id"] == "sess-1"

    def test_worker_stream_safe_filename(self, hermetic):
        lifecycle.log_worker_event("My Worker/1!", "worker_spawned")
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "worker_My_Worker_1_.log")
        assert os.path.isfile(path)
        assert len(_read_jsonl(path)) == 1

    def test_container_stream_json_envelope(self, hermetic):
        lifecycle.log_container_event(
            "container_started", container_id="c-1",
            session_id="sess-1", workspace_id="ws-1",
        )
        lifecycle.log_container_event(
            "container_stopped", container_id="c-1",
            session_id="sess-1", workspace_id="ws-1",
        )
        lifecycle.close_streams()
        records = _read_jsonl(os.path.join(str(hermetic), "logs", "container.log"))
        assert [r["event"] for r in records] == [
            "container_started", "container_stopped"
        ]
        for rec in records:
            _assert_lifecycle_envelope(rec, stream="container")
            assert rec["container_id"] == "c-1"
            assert rec["session_id"] == "sess-1"

    def test_provider_stream_json_envelope_and_redaction(self, hermetic):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        lifecycle.log_provider_event(
            content=f"response with {secret} embedded",
            model_name="gpt-4o",
            request_id="req-1",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
            latency=1.25,
            finish_reason="stop",
            tool_call_count=2,
            temperature=0.7,
            session_id="sess-1", worker_id="w-1", query_id="q-1",
            correlation_id="corr-1", container_id="c-1",
        )
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "provider_raw.jsonl")
        records = _read_jsonl(path)
        assert len(records) == 1
        rec = records[0]
        _assert_lifecycle_envelope(rec, event="provider_response", stream="provider")
        assert rec["model_name"] == "gpt-4o"
        assert rec["request_id"] == "req-1"
        assert rec["token_usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
        assert rec["latency"] == 1.25
        assert rec["finish_reason"] == "stop"
        assert rec["tool_call_count"] == 2
        assert rec["temperature"] == 0.7
        assert rec["content_empty"] is False
        # whole line is redacted before hitting disk
        raw = open(path, encoding="utf-8").read()
        assert secret not in raw
        assert "sk-<REDACTED>" in raw
        assert "sk-<REDACTED>" in rec["content_preview"]

    def test_event_log_stream_json_schema(self, hermetic):
        from agent.events import BaseEvent, EventBus, EventMetadata, EventType
        from agent.logging.event_logger import EventLogger

        EventLogger._instance = None
        bus = EventBus()
        logger = EventLogger(event_bus=bus)
        try:
            logger.start()
            bus.publish(BaseEvent(
                type=EventType.LLM_RESPONSE,
                metadata=EventMetadata(source="test-src"),
                data={"text": "hello"},
            ))
            bus.publish(BaseEvent(
                type=EventType.TOOL_CALL,
                metadata=EventMetadata(source="test-src"),
                data={"text": "sk-abcdefghijklmnopqrstuvwxyz123456 leaked"},
            ))
            logger.stop()
        finally:
            EventLogger._instance = None
        path = os.path.join(str(hermetic), "logs", "event_log.jsonl")
        assert os.path.isfile(path)
        records = _read_jsonl(path)
        assert len(records) == 2
        # event_log records carry the EventLogger schema, NOT the lifecycle
        # envelope (documented deviation): valid JSON + parseable timestamp
        # + event_type / event_id / source / data.
        for rec in records:
            assert isinstance(rec["timestamp"], str)
            datetime.fromisoformat(rec["timestamp"])
            assert "event_type" in rec
            assert "event_id" in rec
            assert rec["source"] == "test-src"
            assert isinstance(rec["data"], dict)
        assert [r["event_type"] for r in records] == ["llm_response", "tool_call"]
        raw = open(path, encoding="utf-8").read()
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in raw
        assert "sk-<REDACTED>" in raw

    def test_lifecycle_session_data_redacted_at_write_time(self, hermetic):
        """Whole-line redaction applies to lifecycle streams by default.

        A secret placed in caller-supplied ``data`` must never reach disk in
        any form.  Note: for ``api_key`` key=value pairs the redaction
        patterns fully replace the value with ``<REDACTED>`` (the ``sk-``
        prefix is consumed by the later key=value pattern), while free-text
        occurrences keep the ``sk-<REDACTED>`` form (covered by the provider
        stream test above).
        """
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        lifecycle.log_session_event(
            "session_started",
            session_id="sess-redact",
            data={"api_key": secret},
        )
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "session.log")
        raw = open(path, encoding="utf-8").read()
        assert secret not in raw
        assert "<REDACTED>" in raw
        for line in raw.splitlines():
            if line.strip():
                rec = json.loads(line)
                assert rec["data"]["api_key"] == "<REDACTED>"


class TestClosedConsoleStream:
    """log_session_event must never propagate ValueError when the console
    stream has been closed (e.g. stderr torn down during logging teardown)."""

    def test_closed_console_stream_does_not_raise(self, hermetic):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        stream.close()  # simulate teardown closing the handler stream
        console = logging.getLogger("thoughtmachine.lifecycle")
        console.addHandler(handler)
        try:
            # must not raise ValueError: I/O operation on closed file
            lifecycle.log_session_event("session_started", session_id="s-1")
        finally:
            console.removeHandler(handler)
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "session.log")
        assert os.path.isfile(path)
        assert len(_read_jsonl(path)) == 1


class TestToolCallRawStream:
    """tool_calls_raw_debug.log: structured JSONL, whole-line redacted, rotating."""

    def test_tool_call_raw_redacted_envelope_and_json(self, hermetic):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        lifecycle.log_tool_call_raw(
            tool_name="ReadFile",
            tool_call_id="call_1",
            arguments_raw='{"path": "%s"}' % secret,
            json_repair_needed=True,
            session_id="sess-1",
        )
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "tool_calls_raw_debug.log")
        raw = open(path, encoding="utf-8").read()
        assert secret not in raw
        assert "sk-<REDACTED>" in raw
        records = _read_jsonl(path)
        assert len(records) == 1
        rec = records[0]
        _assert_lifecycle_envelope(rec, event="tool_call_raw", stream="tool_call")
        assert rec["tool_name"] == "ReadFile"
        assert rec["tool_call_id"] == "call_1"
        assert rec["json_repair_needed"] is True
        assert rec["session_id"] == "sess-1"
        assert rec["arguments_truncated"] is False
        assert "sk-<REDACTED>" in rec["arguments_preview"]

    def test_tool_call_raw_rotation(self, hermetic):
        payload = "界" * 500  # ~1500 B/line once serialized
        for _ in range(5000):
            lifecycle.log_tool_call_raw(tool_name="ReadFile", arguments_raw=payload)
        lifecycle.close_streams()
        path = os.path.join(str(hermetic), "logs", "tool_calls_raw_debug.log")
        assert os.path.isfile(path)
        assert os.path.isfile(path + ".1")
        assert not os.path.isfile(path + ".2")
        for rec in _read_jsonl(path):
            assert rec["event"] == "tool_call_raw"
            assert rec["arguments_truncated"] is False


# ---------------------------------------------------------------------------
# (b) redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redact_openai_sk_keys(self):
        assert redact("sk-abcdefghijklmnopqrstuvwxyz123456") == "sk-<REDACTED>"
        assert redact("sk-or-abcdefghijklmnopqrstuvwxyz123") == "sk-or-<REDACTED>"
        assert redact("sk-ant-abcdefghijklmnopqrstuvwxyz123") == "sk-ant-<REDACTED>"
        assert redact("token sk-abcdefghijklmnopqrstuvwxyz123456 end") == (
            "token sk-<REDACTED> end"
        )
        assert redact("a sk-1111111111 b sk-2222222222 c") == (
            "a sk-<REDACTED> b sk-<REDACTED> c"
        )

    def test_redact_github_pats(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "gh2_"):
            token = prefix + "A" * 40
            assert redact(token) == prefix + "<REDACTED>", prefix
        assert redact("token ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ end") == (
            "token ghp_<REDACTED> end"
        )

    def test_redact_bearer_tokens(self):
        assert redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz.123456") == (
            "Authorization: Bearer <REDACTED>"
        )
        assert redact("Bearer abcdefghijklmnopqrstuvwxyz") == "Bearer <REDACTED>"

    def test_redact_aws_access_keys(self):
        assert redact("AKIAIOSFODNN7EXAMPLE") == "AKIA<REDACTED>"
        assert redact("key=AKIAIOSFODNN7EXAMPLE") == "key=AKIA<REDACTED>"

    def test_redact_key_value_pairs(self):
        assert redact("api_key=abcdef123456") == "api_key=<REDACTED>"
        assert redact("api_key: abcdef123456") == "api_key: <REDACTED>"
        assert redact('{"secret": "hunter2"}') == '{"secret": "<REDACTED>"}'
        assert redact("password=hunter2") == "password=<REDACTED>"
        assert redact("auth_token=abc123") == "auth_token=<REDACTED>"
        assert redact("API_KEY=abcdef123456") == "API_KEY=<REDACTED>"

    def test_redact_pem_blocks(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1Q=="
            "\n-----END RSA PRIVATE KEY-----"
        )
        assert redact(pem) == "<REDACTED>"
        pem2 = "-----BEGIN PRIVATE KEY-----\nabcdef\n-----END PRIVATE KEY-----"
        assert redact(pem2) == "<REDACTED>"

    def test_redact_vault_secret_path(self):
        path = "/home/u/.thoughtmachine/secrets/sk-abcdefghijklmnopqrstuvwxyz123456"
        out = redact(path)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
        assert "sk-<REDACTED>" in out
        assert "/home/u/.thoughtmachine/secrets/" in out

    def test_redact_short_tokens_untouched(self):
        assert redact("sk-abc") == "sk-abc"
        assert redact("ghp_abc") == "ghp_abc"

    def test_redact_never_raises(self):
        for bad in (None, 123, 4.5, {"a": 1}, [1, 2], b"sk-abcdefghijklmnopqrstuvwxyz", True, object()):
            result = redact(bad)
            assert isinstance(result, str)

    def test_redact_keeps_json_parseable(self):
        payload = json.dumps({
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
            "ok": True,
            "nested": {"password": "hunter2"},
        })
        out = redact(payload)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
        assert "hunter2" not in out
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["api_key"].endswith("<REDACTED>")
        assert parsed["nested"]["password"] == "<REDACTED>"


# ---------------------------------------------------------------------------
# (c) rotation
# ---------------------------------------------------------------------------


class TestRotation:
    def test_writer_defaults(self, tmp_path):
        assert DEFAULT_MAX_BYTES == 5 * 1024 * 1024
        assert DEFAULT_KEEP_BACKUPS == 1
        writer = JsonlStreamWriter(str(tmp_path / "defaults.jsonl"))
        assert writer.max_bytes == 5 * 1024 * 1024
        assert writer.keep_backups == 1
        writer.close()

    def test_rotation_contiguous_no_lost_records(self, tmp_path):
        max_bytes = 1024
        path = str(tmp_path / "rot.jsonl")
        writer = JsonlStreamWriter(path, max_bytes=max_bytes, keep_backups=1)
        n = 50
        for i in range(1, n + 1):
            writer.write({"seq": i, "payload": "x" * 180})
        writer.close()

        # exactly main + one backup; no .2
        assert os.path.isfile(path)
        assert os.path.isfile(path + ".1")
        assert not os.path.exists(path + ".2")

        def seqs(p):
            return [json.loads(ln)["seq"] for ln in open(p, encoding="utf-8") if ln.strip()]

        main_seqs = seqs(path)
        back_seqs = seqs(path + ".1")
        assert main_seqs and back_seqs
        # keep_backups=1 discards older backups, so surviving records are a
        # contiguous strictly-increasing *suffix* of the written sequence:
        # no gaps/duplicates inside the retained window, newest record kept.
        combined = sorted(back_seqs + main_seqs)
        assert combined[-1] == n
        assert combined == list(range(combined[0], n + 1))
        # backup is strictly older than main
        assert max(back_seqs) < min(main_seqs)

        # probe: size of a single record line (use 2-digit seq like 50)
        probe = JsonlStreamWriter(str(tmp_path / "probe.jsonl"))
        probe.write({"seq": 99, "payload": "x" * 180})
        probe.close()
        one_record = os.path.getsize(str(tmp_path / "probe.jsonl"))
        total = os.path.getsize(path) + os.path.getsize(path + ".1")
        # tight bound: each of the two files < max_bytes + one record
        assert total <= 2 * max_bytes + 2 * one_record


# ---------------------------------------------------------------------------
# (d) hermeticity - no writes to the repo root
# ---------------------------------------------------------------------------


class TestHermeticity:
    def test_no_codebase_log_writes(self, hermetic):
        """Lifecycle + EventLogger activity must not create anything at repo root."""
        cwd = os.getcwd()
        before = set(os.listdir(cwd))

        lifecycle.log_session_event("session_started", session_id="s-1")
        lifecycle.log_worker_event("w1", "worker_spawned", session_id="s-1")
        lifecycle.log_container_event("container_started", container_id="c-1")
        lifecycle.log_provider_event(content="hello", finish_reason="stop")
        lifecycle.close_streams()

        from agent.events import BaseEvent, EventBus, EventMetadata, EventType
        from agent.logging.event_logger import EventLogger

        EventLogger._instance = None
        bus = EventBus()
        logger = EventLogger(event_bus=bus)
        try:
            logger.start()
            bus.publish(BaseEvent(
                type=EventType.LLM_RESPONSE,
                metadata=EventMetadata(source="hermetic"),
                data={"text": "hi"},
            ))
            logger.stop()
        finally:
            EventLogger._instance = None

        after = set(os.listdir(cwd))
        new_entries = after - before
        assert "logs" not in new_entries, f"repo-root logs/ created: {new_entries}"
        assert new_entries == set(), f"new entries at repo root: {new_entries}"
