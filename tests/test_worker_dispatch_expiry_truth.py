"""A dispatch that EXPIRED has to be visible, not just cleaned up.

When a query deadline elapsed, the timeout path killed the worker and reclaimed
its containers -- correct behaviour, and unchanged here -- but everything it
*reported* afterwards was the BODY's last own status. A worker killed mid-query
therefore kept reading ``busy`` (or ``ready``) to every ``action='list'`` and
``action='inspect'`` caller, as if the dispatch it never answered were still
healthy. These tests pin the additive record that makes the expiry visible:
``_record_dispatch_expiry`` stamps the thread, ``_list_entry_for`` reports
``runtime_status='orphaned'`` for it, and ``_clear_dispatch_expiry`` removes the
marker on the paths where a delivery succeeded.

Nothing here spawns a process: the helpers under test are pure or take stubs.
"""

from __future__ import annotations

from tools.workspace.worker_thread import (
    _clear_dispatch_expiry,
    _list_entry_for,
    _record_dispatch_expiry,
)


class _StubHandle:
    """The handle facts ``_list_entry_for`` reads, and nothing else."""

    def __init__(self, *, alive=None, process_start_error=None, mode=None) -> None:
        self.alive = alive
        self.process_start_error = process_start_error
        self.mode = mode


class _StubThread:
    """A registry thread as ``action='list'`` sees it."""

    def __init__(self, *, status: str = "ready", error=None) -> None:
        self.status = status
        self.current_task = None
        self.last_heartbeat = None
        self.error = error
        self._current_query_id = None


class _SlottedThread:
    """A thread double that refuses attributes: recording must not blow up."""

    __slots__ = ()


def _list_row(thread) -> dict:
    """The row ``action='list'`` would emit for *thread*.

    ``alive=True`` keeps the module's own liveness predicate out of the way --
    this file is about the expiry marker, not about probing.
    """
    return _list_entry_for(
        {"name": "architect"},
        3,
        thread,
        _StubHandle(alive=True, mode="thread"),
    )


def test_recording_an_expiry_returns_the_facts_it_kept() -> None:
    """The recorder hands back what it stamped, so a caller can report it."""
    thread = _StubThread(status="busy")

    kept = _record_dispatch_expiry(
        thread, query_id="job-7", action="query", stage="query_timeout"
    )

    assert kept["dispatch_expired_at"] == thread._dispatch_expired_at
    assert isinstance(kept["dispatch_expired_at"], float)
    assert kept["dispatch_expired_query_id"] == "job-7"
    assert kept["dispatch_expired_action"] == "query"

    # An explicit ``when`` is honoured (and coerced), not silently replaced by
    # "now": the clock that matters is the one the caller measured.
    kept = _record_dispatch_expiry(thread, query_id="job-8", when=1234)
    assert kept["dispatch_expired_at"] == 1234.0


def test_recording_never_raises_on_a_thread_that_refuses_attributes() -> None:
    """A diagnostic must not be able to break the timeout path that calls it."""
    thread = _SlottedThread()

    assert _record_dispatch_expiry(thread, query_id="job-1") == {}
    # Clearing is equally harmless on the same object.
    assert _clear_dispatch_expiry(thread) is None


def test_a_list_row_reports_an_expired_dispatch_as_orphaned() -> None:
    """This is the whole point: the row stops claiming the dispatch is healthy.

    ``thread.status`` still says ``busy`` (the body never got to report
    anything else), so without the marker the row is indistinguishable from a
    worker that is genuinely mid-query.
    """
    thread = _StubThread(status="busy")
    _record_dispatch_expiry(
        thread, query_id="job-9", action="query", stage="query_timeout"
    )

    entry = _list_row(thread)

    assert entry["runtime_status"] == "orphaned"
    assert entry["dispatch_expired_query_id"] == "job-9"
    assert entry["dispatch_expired_at"] == thread._dispatch_expired_at

    # Nothing that was there before may disappear; the body's own report is
    # still carried, it is just no longer passed off as the runtime status.
    assert entry["name"] == "architect"
    assert entry["instance_id"] == 3
    assert entry["instance_label"] == "architect#3"
    assert entry["alive"] is True
    assert entry["current_task"] is None
    assert entry["last_heartbeat"] is None


def test_a_list_row_without_an_expiry_is_unchanged() -> None:
    """The marker must be invisible to every worker that never expired."""
    entry = _list_row(_StubThread(status="ready"))

    assert entry["runtime_status"] == "ready"
    assert "dispatch_expired_at" not in entry
    assert "dispatch_expired_query_id" not in entry
    assert "orphaned" not in entry


def test_clearing_the_expiry_restores_the_bodys_own_status() -> None:
    """A later successful delivery must erase the marker, not inherit it."""
    thread = _StubThread(status="ready")
    _record_dispatch_expiry(thread, query_id="job-2", stage="query_timeout")
    assert _list_row(thread)["runtime_status"] == "orphaned"

    _clear_dispatch_expiry(thread)

    assert thread._dispatch_expired_at is None
    assert thread._dispatch_expired_query_id is None
    assert thread._dispatch_expired_action is None
    entry = _list_row(thread)
    assert entry["runtime_status"] == "ready"
    assert "dispatch_expired_at" not in entry


def test_a_child_that_never_started_is_still_failed_not_orphaned() -> None:
    """``failed`` outranks the marker: nothing was ever dispatched to report.

    The two overrides answer different questions ("this worker never ran" vs
    "this dispatch expired"), and the stronger evidence wins.
    """
    thread = _StubThread(status="ready")
    _record_dispatch_expiry(thread, query_id="job-3", stage="query_timeout")
    handle = _StubHandle(
        alive=False,
        process_start_error="WorkerIpcUnavailableError: no socket pair",
        mode="process",
    )

    entry = _list_entry_for({"name": "architect"}, 3, thread, handle)

    assert entry["runtime_status"] == "failed"
    assert entry["alive"] is False
    assert "WorkerIpcUnavailableError" in entry["process_start_error"]
    # The expiry is not reported on a row that already has better evidence.
    assert "dispatch_expired_at" not in entry
