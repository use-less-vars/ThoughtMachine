"""``_runtime_mode`` is a STAMP: only a real mode value counts as one.

The runtime-mode helpers in :mod:`tools.workspace.worker_thread` used to read
the stamp with a bare ``getattr(worker, "_runtime_mode", None)`` and compare it
only against ``None`` / ``MODE_THREAD``. That is fine for a real ``WorkerThread``
and wrong for every double: ``unittest.mock.MagicMock`` answers *any* attribute
access, so the read hands back a ``Mock`` -- not ``None``, not ``MODE_THREAD`` --
and the helper concludes "some non-thread mode". Two real regressions followed
from that conclusion, and both are pinned here without spawning anything:

* the delivery helpers raised :class:`WorkerQueryTransportMissing` naming
  ``<MagicMock name='mock._runtime_mode'>`` as the worker's runtime mode, and
* a spawn tried to carry the double's spec across a process boundary it never
  had, failing with ``PicklingError`` on the mock.

``declared_runtime_mode`` is the single place that decides what a stamp is, so
the double cases are asserted against it directly and then once per caller --
including the two entry points that exist only on the async-enqueue branch
(``worker_is_dispatchable`` and ``enqueue_query_to_worker``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.workspace.worker_runtime import MODE_PROCESS, MODE_THREAD, RUNTIME_MODES
from tools.workspace.worker_thread import (
    WorkerQueryTransportMissing,
    declared_runtime_mode,
    deliver_query_to_worker,
    enqueue_query_to_worker,
    runtime_dispatch_handle,
    worker_is_dispatchable,
)


class TestDeclaredRuntimeMode:
    """What counts as a stamp."""

    def test_real_thread_stamp_is_reported(self):
        worker = SimpleNamespace(_runtime_mode=MODE_THREAD)
        assert declared_runtime_mode(worker) == MODE_THREAD

    def test_real_process_stamp_is_reported(self):
        worker = SimpleNamespace(_runtime_mode=MODE_PROCESS)
        assert declared_runtime_mode(worker) == MODE_PROCESS

    def test_absent_attribute_is_unstamped(self):
        assert declared_runtime_mode(SimpleNamespace()) is None

    def test_magicmock_is_unstamped_not_a_mode(self):
        """The regression: a Mock read as if it were the worker's mode."""
        worker = MagicMock()
        # The trap is real -- the bare read does hand back a Mock ...
        assert getattr(worker, "_runtime_mode", None) is not None
        # ... and the helper must still call that unstamped.
        assert declared_runtime_mode(worker) is None

    @pytest.mark.parametrize("value", ["bogus", "", 0, 123, b"thread", ["thread"], None])
    def test_non_vocabulary_values_are_unstamped(self, value):
        """A stamp from a build this one does not implement is not a stamp."""
        worker = SimpleNamespace(_runtime_mode=value)
        assert declared_runtime_mode(worker) is None

    def test_every_vocabulary_member_round_trips(self):
        for mode in RUNTIME_MODES:
            assert declared_runtime_mode(SimpleNamespace(_runtime_mode=mode)) == mode


class TestCallersTreatADoubleAsUnstamped:
    """Each caller's unstamped branch is the pre-C3-1 behaviour: the queues."""

    def test_runtime_dispatch_handle_finds_no_handle(self):
        assert runtime_dispatch_handle(MagicMock()) is None

    def test_delivery_does_not_route_a_double_over_a_missing_transport(self):
        worker = MagicMock()
        worker.send_query.return_value = "answered in-process"
        # Would raise WorkerQueryTransportMissing before the stamp check.
        assert deliver_query_to_worker(worker, "q", timeout=1.0) == (
            "answered in-process"
        )
        worker.send_query.assert_called_once_with("q", timeout=1.0)

    def test_enqueue_does_not_refuse_a_double(self):
        worker = MagicMock()
        # Would raise WorkerQueryTransportMissing before the stamp check.
        enqueue_query_to_worker(worker, "job-1", "q", timeout=1.0)
        worker.enqueue_query.assert_called_once_with("job-1", "q")
        worker._input_queue.put.assert_not_called()

    def test_enqueue_falls_back_to_the_raw_queue(self):
        worker = SimpleNamespace(_input_queue=MagicMock())
        enqueue_query_to_worker(worker, "job-2", "q", timeout=1.0)
        worker._input_queue.put.assert_called_once_with(("job-2", "q", None))

    def test_a_double_is_dispatchable(self):
        """Liveness is asked of the objects that have it, not invented."""
        assert worker_is_dispatchable(MagicMock()) is True

    def test_a_broken_liveness_probe_is_not_death_evidence(self):
        worker = MagicMock()
        worker.is_alive.side_effect = RuntimeError("boom")
        assert worker_is_dispatchable(worker) is True

    def test_a_dead_thread_is_still_not_dispatchable(self):
        worker = SimpleNamespace(_runtime_mode=MODE_THREAD, is_alive=lambda: False)
        assert worker_is_dispatchable(worker) is False


class TestGenuineMisconfigurationStillFailsLoudly:
    """The guard must not be loosened into silence for real process workers."""

    def test_process_stamp_without_a_handle_refuses_delivery(self):
        worker = SimpleNamespace(
            _runtime_mode=MODE_PROCESS, worker_name="w-process"
        )
        with pytest.raises(WorkerQueryTransportMissing) as excinfo:
            deliver_query_to_worker(worker, "q", timeout=1.0)
        assert "w-process" in str(excinfo.value)
        assert MODE_PROCESS in str(excinfo.value)

    def test_process_stamp_without_a_handle_refuses_enqueue(self):
        worker = SimpleNamespace(
            _runtime_mode=MODE_PROCESS, worker_name="w-process"
        )
        with pytest.raises(WorkerQueryTransportMissing) as excinfo:
            enqueue_query_to_worker(worker, "job-3", "q", timeout=1.0)
        assert "w-process" in str(excinfo.value)

    def test_a_stale_handle_for_another_mode_is_not_used(self):
        worker = SimpleNamespace(
            _runtime_mode=MODE_PROCESS,
            _runtime_handle=SimpleNamespace(mode=MODE_THREAD),
        )
        assert runtime_dispatch_handle(worker) is None
