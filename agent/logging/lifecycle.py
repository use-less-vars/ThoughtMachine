"""Lifecycle event streams — canonical structured logs for session, worker,
and container lifecycle events.

Streams (JSONL, written via :class:`~agent.logging.streams.JsonlStreamWriter`)
all live under the canonical vault log directory ``~/.thoughtmachine/logs``:

- ``session.log``          — controller events (wired in ``_emit_event``)
- ``worker_<name>.log``    — per-worker lifecycle (spawned / stopped)
- ``container.log``        — container lifecycle (started / stopped / ...)
- ``provider_raw.jsonl``   — raw provider responses (whole-line redacted)

Every function is best-effort and NEVER raises: lifecycle logging must never
break the caller's control flow.  A concise, secret-free summary line is
also emitted through the stdlib ``logging`` hierarchy (logger
``thoughtmachine.lifecycle``) for human-readable console output when
:func:`agent.logging.console.configure_console_logging` is active.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Dict, Optional

from agent.logging.streams import JsonlStreamWriter

#: Canonical vault log directory (mirrors EventLogger + _AgentLogger).
#: ``THOUGHTMACHINE_VAULT_ROOT`` (when set) redirects the vault root.
_VAULT_ROOT = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
if _VAULT_ROOT:
    LOG_DIR = os.path.join(_VAULT_ROOT, "logs")
else:
    LOG_DIR = os.path.join(os.path.expanduser("~"), ".thoughtmachine", "logs")

#: stdlib logger used for the human-readable console summary lines.
_console_logger = logging.getLogger("thoughtmachine.lifecycle")
_console_logger.setLevel(logging.INFO)

_writers: Dict[str, JsonlStreamWriter] = {}
_writers_lock = threading.Lock()


def get_log_dir() -> str:
    """Return the canonical vault log directory, creating it if needed."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass
    return LOG_DIR


def _safe_name(name: str) -> str:
    """Sanitize a name for use in a file name."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name or ""))


def _writer(filename: str) -> JsonlStreamWriter:
    """Return the lazily-created JsonlStreamWriter for *filename*.

    Writers are cached by full resolved path so a LOG_DIR re-point
    (e.g. hermetic test fixtures) takes effect even when a writer for the
    same file name was already created under a different directory.
    """
    with _writers_lock:
        path = os.path.join(get_log_dir(), filename)
        writer = _writers.get(path)
        if writer is None:
            writer = JsonlStreamWriter(path)
            _writers[path] = writer
        return writer


def log_session_event(
    event_type: str,
    *,
    session_id: str = "",
    workspace_id: str = "",
    data: Optional[dict] = None,
) -> None:
    """Append a session lifecycle event to ``session.log`` (never raises)."""
    try:
        record = {
            "event": event_type,
            "stream": "session",
            "session_id": session_id or "",
            "workspace_id": workspace_id or "",
            "data": data or {},
        }
        _writer("session.log").write(record)
        _console_logger.info(
            "session %s session_id=%s", event_type, session_id or "-"
        )
    except Exception:
        pass


def log_worker_event(
    worker_name: str,
    event_type: str,
    *,
    session_id: str = "",
    worker_id: str = "",
    data: Optional[dict] = None,
) -> None:
    """Append a worker lifecycle event to ``worker_<name>.log`` (never raises)."""
    try:
        record = {
            "event": event_type,
            "stream": "worker",
            "worker_name": worker_name,
            "worker_id": worker_id or "",
            "session_id": session_id or "",
            "data": data or {},
        }
        _writer(f"worker_{_safe_name(worker_name)}.log").write(record)
        _console_logger.info(
            "worker %s name=%s session_id=%s",
            event_type,
            worker_name,
            session_id or "-",
        )
    except Exception:
        pass


def log_container_event(
    event_type: str,
    *,
    container_id: str = "",
    session_id: str = "",
    workspace_id: str = "",
    data: Optional[dict] = None,
) -> None:
    """Append a container lifecycle event to ``container.log`` (never raises)."""
    try:
        record = {
            "event": event_type,
            "stream": "container",
            "container_id": container_id or "",
            "session_id": session_id or "",
            "workspace_id": workspace_id or "",
            "data": data or {},
        }
        _writer("container.log").write(record)
        _console_logger.info(
            "container %s container_id=%s session_id=%s",
            event_type,
            container_id or "-",
            session_id or "-",
        )
    except Exception:
        pass


def log_provider_event(
    *,
    content: str = "",
    model_name: str = "",
    request_id: str = "",
    token_usage: Optional[dict] = None,
    latency: Optional[float] = None,
    finish_reason: str = "",
    stop_reason: str = "",
    tool_call_count: int = 0,
    temperature: Optional[float] = None,
    session_id: str = "",
    worker_id: str = "",
    query_id: str = "",
    correlation_id: str = "",
    container_id: str = "",
) -> None:
    """Append a provider-response record to ``provider_raw.jsonl`` (never raises).

    ``content`` is the raw provider text — only a 500-char preview is stored
    (``content_preview``), and the whole serialized line is redacted before
    writing, so secrets embedded in the preview never reach disk.  Fields
    without a value (request_id, token_usage, latency, finish_reason,
    stop_reason, temperature, model_name) are omitted from the record.
    """
    try:
        raw = content if isinstance(content, str) else ("" if content is None else str(content))
        content_empty = raw == ""
        record = {
            "event": "provider_response",
            "stream": "provider",
            "tool_call_count": int(tool_call_count or 0),
            "content_preview": raw[:500] if not content_empty else "",
            "content_empty": content_empty,
            "empty_content": content_empty,
            "session_id": session_id or "",
            "worker_id": worker_id or "",
            "query_id": query_id or "",
            "correlation_id": correlation_id or "",
            "container_id": container_id or "",
        }
        if model_name:
            record["model_name"] = model_name
        if request_id:
            record["request_id"] = request_id
        if token_usage:
            try:
                record["token_usage"] = dict(token_usage)
            except Exception:
                record["token_usage"] = token_usage
        if latency is not None:
            try:
                record["latency"] = float(latency)
            except (TypeError, ValueError):
                record["latency"] = latency
        if finish_reason:
            record["finish_reason"] = finish_reason
        if stop_reason:
            record["stop_reason"] = stop_reason
        if temperature is not None:
            try:
                record["temperature"] = float(temperature)
            except (TypeError, ValueError):
                record["temperature"] = temperature
        _writer("provider_raw.jsonl").write(record, redact_line=True)
        _console_logger.info(
            "provider response model=%s request_id=%s tool_calls=%d empty=%s",
            model_name or "-",
            request_id or "-",
            int(tool_call_count or 0),
            content_empty,
        )
    except Exception:
        pass


def close_streams() -> None:
    """Best-effort flush and close of all writers (process shutdown hook)."""
    with _writers_lock:
        writers = list(_writers.values())
        _writers.clear()
    for writer in writers:
        writer.close()
