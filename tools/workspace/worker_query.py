# tools/workspace/worker_query.py
"""Strict sync-query contract helpers (worker lifecycle trust, W2).

Defines the canonical timeout envelope and validation rules for the
synchronous worker query path, plus a bounded ``strict_wait_for_reply``
that guarantees a caller always receives a contract-conforming envelope
dict instead of a raw ``TimeoutError``.

Envelope contract
-----------------
A strict timeout envelope is a dict with:

    {"status": "timeout",
     "note": <non-empty str>,
     "worker_name": <str>,
     "instance_label": <str | None>,
     "elapsed_seconds": <numeric, not bool>,
     "response": <Any | None>}

A successful reply envelope carries ``status`` plus the response payload:

    {"status": "ok", "response": ..., "worker_name": ...,
     "instance_label": ..., "elapsed_seconds": <numeric>}

The wait-time math is single-sourced in ``tools.workspace.worker_timeout``
(``_worker_query_wait_timeout`` = effective timeout + grace); this module
re-exports the timeout helpers so importers do not need to reach into
worker_timeout directly.

``deliver_query_and_block`` is the full sync-call contract on top of
``strict_wait_for_reply``: it BLOCKS until the worker replies OR reaches a
terminal state and is cleaned up — an early return is impossible. A busy
worker is rejected loudly with ``WorkerBusyError`` (W2 has no query
queueing; ``SPAWN_QUEUE_TIMEOUT`` is reserved for a future queued variant),
and a timeout is resolved into a TERMINAL envelope carrying a ``cleanup``
report (``stop_called`` / ``exited``) instead of returning early.
"""

from __future__ import annotations

import json
import numbers
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from agent.config.defaults import QUERY_WAIT_GRACE_SECONDS
from tools.workspace.worker_timeout import (
    _worker_timeout_detected,
    _worker_query_wait_timeout,
    clamped_wait_for_job_timeout,
    wait_for_worker_exit,
)

__all__ = [
    "QUERY_WAIT_GRACE_SECONDS",
    "WorkerBusyError",
    "deliver_query_and_block",
    "_worker_timeout_detected",
    "_worker_query_wait_timeout",
    "clamped_wait_for_job_timeout",
    "wait_for_worker_exit",
    "REQUIRED_REPLY_KEYS",
    "build_timeout_envelope",
    "is_timeout_envelope",
    "envelope_status",
    "validate_query_reply",
    "strict_wait_for_reply",
]

# Every strict-contract reply (timeout or success) must carry a status.
REQUIRED_REPLY_KEYS: Tuple[str, ...] = ("status",)


def _is_numeric(value: Any) -> bool:
    """True for genuine numbers (int/float, not bool)."""
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def build_timeout_envelope(
    worker_name: str,
    note: Optional[str] = None,
    instance_label: Optional[str] = None,
    elapsed_seconds: float = 0.0,
    response: Any = None,
    status: str = "timeout",
) -> Dict[str, Any]:
    """Build a strict-contract timeout envelope dict.

    Raises ``ValueError`` when *elapsed_seconds* is not numeric (bool
    excluded) or *worker_name* / *status* are not non-empty strings.
    """
    if not isinstance(worker_name, str) or not worker_name.strip():
        raise ValueError("worker_name must be a non-empty string")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    if not _is_numeric(elapsed_seconds):
        raise ValueError(
            f"elapsed_seconds must be numeric, got {type(elapsed_seconds).__name__}"
        )
    if note is None:
        note = (
            f"Worker {worker_name!r} did not respond within the query window."
        )
    return {
        "status": status,
        "note": note,
        "worker_name": worker_name,
        "instance_label": instance_label,
        "elapsed_seconds": float(elapsed_seconds),
        "response": response,
    }


def is_timeout_envelope(reply: Any) -> bool:
    """True when *reply* satisfies the strict timeout-envelope contract.

    Requires a dict with ``status == "timeout"``, a non-empty string
    ``note``, and a numeric (non-bool) ``elapsed_seconds``.
    """
    if not isinstance(reply, dict):
        return False
    if reply.get("status") != "timeout":
        return False
    note = reply.get("note")
    if not isinstance(note, str) or not note.strip():
        return False
    return _is_numeric(reply.get("elapsed_seconds"))


def envelope_status(reply: Any) -> Optional[str]:
    """Return the reply's ``status`` value, or None when absent/not a dict."""
    if isinstance(reply, dict) and "status" in reply:
        return reply.get("status")
    return None


def validate_query_reply(
    reply: Any, expected_keys: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """Validate a reply against the strict sync-query contract.

    Contract:
      * *reply* must be a dict, containing every key in *expected_keys*
        (default ``REQUIRED_REPLY_KEYS`` = ``("status",)``), each present.
      * A reply whose status is ``"timeout"`` must additionally satisfy
        ``is_timeout_envelope`` (non-empty note + numeric elapsed_seconds).

    Raises ``ValueError`` on the first violation; returns the dict unchanged
    when valid.
    """
    if not isinstance(reply, dict):
        raise ValueError(
            f"query reply must be a dict, got {type(reply).__name__}"
        )
    keys = tuple(expected_keys) if expected_keys is not None else REQUIRED_REPLY_KEYS
    for key in keys:
        if key not in reply:
            raise ValueError(f"query reply missing required key {key!r}")
    if reply.get("status") == "timeout" and not is_timeout_envelope(reply):
        raise ValueError(
            "timeout reply violates the strict envelope contract "
            "(requires non-empty 'note' and numeric 'elapsed_seconds')"
        )
    return reply


def strict_wait_for_reply(
    thread: Any,
    query: str,
    timeout_seconds: Any = None,
    fallback: float = 300.0,
    grace: Optional[float] = None,
    worker_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Send *query* via ``thread.send_query`` and return a contract envelope.

    The wall-clock wait is ``_worker_query_wait_timeout(timeout_seconds,
    fallback)`` (effective timeout + ``QUERY_WAIT_GRACE_SECONDS``) so the
    worker's own cooperative timeout envelope arrives before we give up;
    when the caller overrides *grace*, the wait is recomputed as
    ``timeout_seconds + grace``.

    Guarantees:
      * The call never surfaces a raw ``TimeoutError``: a timeout is
        converted into a strict timeout envelope (``build_timeout_envelope``)
        with the measured ``elapsed_seconds``.
      * A successful reply is wrapped in a ``{"status": "ok", ...}``
        envelope carrying the response, worker identity and elapsed seconds.
    """
    effective = _worker_query_wait_timeout(timeout_seconds, fallback=fallback)
    if (
        grace is not None
        and grace != QUERY_WAIT_GRACE_SECONDS
        and timeout_seconds is not None
        and _is_numeric(timeout_seconds)
        and float(timeout_seconds) > 0
    ):
        effective = float(timeout_seconds) + float(grace)

    name = worker_name or getattr(thread, "worker_name", "unknown")
    label = getattr(thread, "instance_label", None)
    t0 = time.monotonic()
    try:
        response = thread.send_query(query, timeout=effective)
    except TimeoutError as exc:
        elapsed = time.monotonic() - t0
        return build_timeout_envelope(
            worker_name=name,
            note=(
                f"Worker {name!r} did not respond within {effective:.0f}s "
                f"({exc})."
            ),
            instance_label=label,
            elapsed_seconds=round(elapsed, 3),
        )
    elapsed = time.monotonic() - t0
    return {
        "status": "ok",
        "response": response,
        "worker_name": name,
        "instance_label": label,
        "elapsed_seconds": round(elapsed, 3),
    }


class WorkerBusyError(Exception):
    """Raised when a sync query targets a worker that is already busy.

    W2 has no query queueing: a worker processes one query at a time and a
    caller must never pile another query onto a busy worker. This is a loud,
    explicit rejection (never a silent drop, never an unbounded wait).
    ``SPAWN_QUEUE_TIMEOUT`` is reserved for a future queued variant; the
    sync path refuses instead.
    """


def _is_alive(thread: Any) -> bool:
    """Defensive liveness check (same fallback as worker.py)."""
    alive = getattr(thread, "is_alive", None)
    if callable(alive):
        try:
            return bool(alive())
        except Exception:
            return False
    return True


def _thread_busy(thread: Any) -> bool:
    """Best-effort busy detection on a duck-typed worker thread.

    Checks, in order: a callable ``busy`` attribute, a truthy ``busy``
    attribute, and ``status == 'busy'`` (worker.py sets status to ``busy``
    while processing a query).
    """
    busy = getattr(thread, "busy", None)
    if callable(busy):
        try:
            return bool(busy())
        except Exception:
            pass
    elif busy:
        return True
    return getattr(thread, "status", None) == "busy"


def _normalize_reply(
    response: Any,
    worker_name: str,
    instance_label: Optional[str],
    elapsed_seconds: float,
) -> Dict[str, Any]:
    """Wrap a successful reply in the canonical ok envelope.

    worker.py's ``send_query`` returns a JSON-string envelope
    (``{"content", "status", "confidence", "meta", "telemetry"}``); when the
    payload parses as a dict those fields are merged into the envelope so
    every caller sees one uniform shape. Non-JSON payloads (test doubles,
    plain strings) keep ``status == "ok"`` with the raw response preserved.
    """
    payload = response
    if isinstance(response, str):
        try:
            payload = json.loads(response)
        except (ValueError, TypeError):
            payload = None
    if isinstance(payload, dict):
        status = payload.get("status") or "ok"
        content = payload.get("content")
        confidence = payload.get("confidence")
        meta = payload.get("meta")
        telemetry = payload.get("telemetry")
    else:
        status, content, confidence, meta, telemetry = "ok", None, None, None, None
    return {
        "status": status,
        "content": content,
        "confidence": confidence,
        "meta": meta,
        "telemetry": telemetry,
        "response": response,
        "worker_name": worker_name,
        "instance_label": instance_label,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _resolve_terminal(
    thread: Any,
    worker_name: str,
    instance_label: Optional[str],
    elapsed_seconds: float,
    stop_on_timeout: bool,
) -> Dict[str, Any]:
    """Classify a timed-out worker's terminal state and clean it up.

    Priority: an already-paused worker stays ``paused`` (pause is explicit
    and intentional), an already-stopped/errored worker reports its terminal
    status, a dead thread is reported as its last status (or ``stopped``),
    and a worker that already reports ``timeout`` stays ``timeout``. Anything
    else (idle/ready but unresponsive) gets a cooperative ``stop()`` +
    bounded join when *stop_on_timeout* is True: ``stopped`` if it exited,
    ``timeout`` if it ignored the stop and is still alive (daemon thread left
    to terminate on its own — never ``Thread.kill``).
    """
    status = getattr(thread, "status", None)
    if status == "paused":
        terminal = "paused"
    elif status == "stopped":
        terminal = "stopped"
    elif status in ("error", "errored"):
        terminal = "error"
    elif status == "timeout":
        terminal = "timeout"
    elif not _is_alive(thread):
        terminal = status or "stopped"
    elif not stop_on_timeout:
        terminal = status or "stopped"
    else:
        stop_called = True
        try:
            thread.stop()
        except Exception:
            pass
        exited = wait_for_worker_exit(thread, worker_name)
        return {
            "status": "stopped" if exited else "timeout",
            "note": (
                f"Worker {worker_name!r} did not reply within the query "
                f"window; cooperatively stopped "
                f"({'exited' if exited else 'still alive after stop budget'})."
            ),
            "content": None,
            "response": None,
            "worker_name": worker_name,
            "instance_label": instance_label,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "cleanup": {"stop_called": stop_called, "exited": exited},
        }
    return {
        "status": terminal,
        "note": (
            f"Worker {worker_name!r} did not reply within the query window; "
            f"terminal state {terminal!r} (no forced stop needed)."
        ),
        "content": None,
        "response": None,
        "worker_name": worker_name,
        "instance_label": instance_label,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "cleanup": {"stop_called": False, "exited": not _is_alive(thread)},
    }


def deliver_query_and_block(
    worker_thread: Any,
    query: str,
    timeout: Any = None,
    fallback: float = 300.0,
    grace: Optional[float] = None,
    worker_name: Optional[str] = None,
    busy_guard: bool = True,
    stop_on_timeout: bool = True,
) -> Dict[str, Any]:
    """Deliver *query* to a worker and BLOCK until reply or cleanup.

    Contract (W2 trust):
      * BLOCKS until the worker replies OR reaches a terminal state and is
        cleaned up — an early return is impossible. The wall-clock wait is
        ``_worker_query_wait_timeout(timeout, fallback)`` (effective timeout
        + ``QUERY_WAIT_GRACE_SECONDS``) so the worker's own cooperative
        timeout envelope arrives before we classify it.
      * A busy worker is rejected loudly with ``WorkerBusyError`` (no
        queueing in W2; ``SPAWN_QUEUE_TIMEOUT`` is reserved for a future
        queued variant). The guard can be disabled with *busy_guard* False.
      * On success returns the canonical ok envelope (status/content/
        confidence/meta/telemetry merged from the worker's JSON-string
        envelope) plus worker identity and elapsed seconds.
      * On timeout returns a TERMINAL envelope: the worker is classified
        (paused/stopped/error/timeout) and — unless *stop_on_timeout* is
        False or the worker already reached a terminal state — cooperatively
        stopped and joined; the envelope carries a ``cleanup`` dict
        (``stop_called`` / ``exited``).
    """
    name = worker_name or getattr(worker_thread, "worker_name", "unknown")
    label = getattr(worker_thread, "instance_label", None)
    if busy_guard and _thread_busy(worker_thread):
        raise WorkerBusyError(
            f"Worker {name!r} is busy "
            f"(status={getattr(worker_thread, 'status', '?')!r}); sync queries "
            f"are not queued in W2 — spawn a new worker or retry when it is "
            f"ready (SPAWN_QUEUE_TIMEOUT is reserved for a future queued "
            f"variant)."
        )
    reply = strict_wait_for_reply(
        worker_thread,
        query,
        timeout_seconds=timeout,
        fallback=fallback,
        grace=grace,
        worker_name=name,
    )
    if reply.get("status") == "timeout":
        return _resolve_terminal(
            worker_thread,
            name,
            label,
            float(reply.get("elapsed_seconds") or 0.0),
            stop_on_timeout,
        )
    return _normalize_reply(
        reply.get("response"),
        name,
        label,
        float(reply.get("elapsed_seconds") or 0.0),
    )

