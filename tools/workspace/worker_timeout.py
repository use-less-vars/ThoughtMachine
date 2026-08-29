"""Worker timeout math and soft-timeout detection.

A worker's agent soft-times-out at ``timeout_seconds`` and then replies with
its timeout envelope. A caller waiting for that reply must therefore wait at
least ``timeout_seconds + QUERY_WAIT_GRACE_SECONDS`` so the worker's OWN
timeout envelope arrives before the caller force-stops it. Without the grace
the caller would preempt the worker's cooperative timeout with a destructive
stop and would surface a raw TimeoutError instead of the worker's timeout
envelope.

Extracted from ``tools.workspace.worker`` so the timeout-detection predicate,
the wait-time math and the bounded join helper can be reused without
importing the full worker runtime.
"""

from __future__ import annotations

import logging
import numbers
import time
from typing import Any, Dict, Optional

from agent.config.defaults import EXEC_KILL_GRACE, QUERY_WAIT_GRACE_SECONDS

logger = logging.getLogger(__name__)


def _worker_timeout_detected(agent_state: Any, last_elapsed_val: Any, timeout_seconds: Any) -> bool:
    """Return True when the agent state indicates a soft timeout (D3).

    The production TimeState enum values are LOWERCASE ('critical' — see
    agent/core/state.py), so the legacy uppercase-only comparison never
    matched and soft timeouts were missed. Detection is robust via any of:
      * restriction_reason == 'timeout' (state.py sets this when the agent's
        time monitor crosses the timeout threshold), or
      * time_state.value case-insensitively equal to 'CRITICAL', or
      * the loop actually ran >= timeout_seconds (belt-and-braces fallback
        for when the time monitor is disabled but the budget was exceeded).
    """
    if agent_state is None:
        return False
    reason = getattr(agent_state, "restriction_reason", None)
    time_value = None
    if hasattr(agent_state, "time_state") and hasattr(agent_state.time_state, "value"):
        time_value = getattr(agent_state.time_state, "value", None)
    if reason == "timeout":
        return True
    if time_value is not None and str(time_value).upper() == "CRITICAL":
        return True
    elapsed_over_budget = False
    if isinstance(last_elapsed_val, (int, float)) and not isinstance(last_elapsed_val, bool):
        try:
            budget = float(timeout_seconds)
        except (TypeError, ValueError):
            budget = None
        if budget is not None and budget > 0:
            elapsed_over_budget = last_elapsed_val >= budget
    return elapsed_over_budget


def _worker_query_wait_timeout(timeout_seconds: Any, fallback: float = 300.0) -> float:
    """Return the wall-clock wait a caller should allow for a worker reply.

    A worker whose agent soft-times-out at ``timeout_seconds`` replies with
    its own timeout envelope at roughly that deadline, so the caller must wait
    ``timeout_seconds + QUERY_WAIT_GRACE_SECONDS``. Non-numeric or non-positive
    values (e.g. ``None``, a mock's auto-attribute) fall back to ``fallback``
    (the legacy fixed wait).
    """
    if timeout_seconds is None:
        return fallback
    # isinstance check rather than try/float(): a MagicMock auto-attribute
    # supports __float__ (returning 1.0), which would silently bypass the
    # fallback. numbers.Real is True only for genuine numeric types.
    if not isinstance(timeout_seconds, numbers.Real):
        return fallback
    t = float(timeout_seconds)
    if t <= 0:
        return fallback
    return t + QUERY_WAIT_GRACE_SECONDS


def clamped_wait_for_job_timeout(requested_timeout: Any, cap: float = 300.0) -> int:
    """Clamp a ``wait_for_job`` timeout to the cap (default 300s).

    Same math as ``Worker._action_join``: ``int(requested_timeout or 60)``
    then ``min(..., cap)``. Returns an int number of seconds suitable for
    ``time.monotonic() + total`` deadline math.
    """
    requested = int(requested_timeout or 60)
    capped = int(cap)
    if requested > capped:
        logger.info(
            "wait_for_job: requested timeout %ds exceeds %ds cap; clamped to %ds",
            requested,
            capped,
            capped,
        )
    return min(requested, capped)


def wait_for_worker_exit(thread: Any, worker_name: str) -> bool:
    """Wait (bounded) for a cooperatively-stopped worker thread to exit.

    Same join-retry pattern as ``Worker._wait_for_worker_exit`` (budget =
    max(30, thread timeout)); never ``Thread.kill``. Returns True when the
    thread exited within the budget, False when the budget elapsed with the
    daemon thread still alive (the envelope then reports the degraded outcome
    and the thread is left to terminate on its own).
    """
    # The thread may be a test double (or otherwise lack a numeric
    # ``_timeout_seconds``); fall back to the module default so the join
    # budget stays a bounded number.
    _timeout_seconds = getattr(thread, "_timeout_seconds", 600)
    if not isinstance(_timeout_seconds, (int, float)):
        _timeout_seconds = 600
    _join_budget = max(30, int(_timeout_seconds or 600))
    _join_elapsed = 0.0
    _join_step = 2.0
    while _join_elapsed < _join_budget:
        thread.join(timeout=_join_step)
        _join_elapsed += _join_step
        if not thread.is_alive():
            return True
        logger.debug(
            "Still waiting for worker '%s' to stop "
            "(%.0f/%ds elapsed)",
            worker_name, _join_elapsed, _join_budget,
        )
    if thread.is_alive():
        logger.warning(
            "Worker '%s' did not stop within %ds budget after query "
            "timeout; returning envelope with thread still running "
            "(daemon thread)",
            worker_name, _join_budget,
        )
        return False
    return True


def _hard_deadline_fallback(
    thread: Any,
    deadline: float,
    join_bound: float = EXEC_KILL_GRACE,
    worker_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Stop a worker that passed its hard deadline (last-resort fallback).

    Applies ONLY after the soft timeout has expired and cooperative
    execution interruption failed: the caller has already tried
    ``_terminate_tracked_executions()`` / ``ExecutionTracker.terminate_all``
    and the in-flight blocking call is still wedged. This fallback calls the
    thread's own ``stop()`` (never ``Thread.kill``), then ``join(join_bound)``
    so the wait is BOUNDED - a wedged daemon thread is reported, not hung
    on. Returns a terminal envelope:

    ``{'status': 'stopped'|'timeout', 'content': ..., 'meta':
    {'hard_deadline': True, 'elapsed_seconds': ...}, 'cleanup':
    {'stop_called': bool, 'join_bounded': float}}``

    Dependency-light (duck-typed thread: ``stop()`` / ``join(timeout)`` /
    ``is_alive()``), so it is unit-testable with fakes.
    """
    name = worker_name or getattr(thread, "worker_name", None) or "worker"
    stop_called = False
    stop_method = getattr(thread, "stop", None)
    if callable(stop_method):
        try:
            stop_method()
            stop_called = True
        except Exception as exc:
            logger.warning(
                "Hard-deadline stop() failed for worker '%s': %s", name, exc
            )
    try:
        thread.join(timeout=join_bound)
    except Exception as exc:
        logger.warning(
            "Hard-deadline join failed for worker '%s': %s", name, exc
        )

    alive = True
    is_alive = getattr(thread, "is_alive", None)
    if callable(is_alive):
        try:
            alive = bool(is_alive())
        except Exception:
            alive = True
    status = "timeout" if alive else "stopped"
    if status == "stopped":
        content = (
            f"Worker '{name}' reached its hard deadline after cooperative "
            "interruption failed; the worker was force-stopped."
        )
    else:
        content = (
            f"Worker '{name}' reached its hard deadline and did not exit "
            "within the bounded join; it is left as a daemon thread."
        )
    return {
        "status": status,
        "content": content,
        "meta": {
            "hard_deadline": True,
            "elapsed_seconds": round(max(0.0, time.monotonic() - deadline), 3),
        },
        "cleanup": {
            "stop_called": stop_called,
            "join_bounded": float(join_bound),
            "exited": not alive,
        },
    }
