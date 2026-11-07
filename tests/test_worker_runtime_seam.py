# tests/test_worker_runtime_seam.py
"""Unit proof for the S3b runtime-handle seam (\u00a710).

The seam is a registry (``worker_runtime._RUNTIME_HANDLES``) plus one branch at
the top of ``workspace_routes._write_worker_command``. In production the
registry is empty -- nothing outside the tests constructs a
``WorkerRuntimeHandle`` -- so this file has three jobs: pin the lookup
contract, prove the branch is real (not dead code) when a handle IS
registered, and prove the command-file path is unchanged when it is not.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tools.workspace import worker_runtime
from tools.workspace.worker_runtime_protocol import Heartbeat, WorkerState
from tools.workspace.worker_thread import WorkerThread
from web_ui.backend import workspace_routes


@pytest.fixture(autouse=True)
def _handle_registry_teardown():
    """Snapshot the seam registry and pop whatever the test added.

    Same shape as the ``_registry_teardown`` fixture in
    ``tests/test_worker_lifecycle_routes.py``: a handle registered by a test
    must never be visible to the next one, because "no handle registered" is
    the production state every other test depends on.
    """
    preexisting = set(worker_runtime._RUNTIME_HANDLES.keys())
    yield
    added = [k for k in worker_runtime._RUNTIME_HANDLES.keys() if k not in preexisting]
    for key in added:
        worker_runtime._RUNTIME_HANDLES.pop(key, None)


class _FakeHandle:
    """Records the verb the seam asked for; nothing else is touched."""

    def __init__(self, verdict: object = True) -> None:
        self.calls = []
        self._verdict = verdict

    def _verb(self, name: str) -> object:
        self.calls.append(name)
        return self._verdict

    def stop(self) -> object:
        return self._verb("stop")

    def pause(self) -> object:
        return self._verb("pause")

    def resume(self) -> object:
        return self._verb("resume")

    def kill(self) -> object:
        return self._verb("kill")


def _worker_dir(tmp_path, generation=None):
    """A worker directory; with *generation* the command takes the FENCED path."""
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    if generation is not None:
        (worker_dir / "generation").write_text(str(generation), encoding="utf-8")
    return worker_dir


def _command_payload(worker_dir):
    return json.loads((worker_dir / "command.json").read_text(encoding="utf-8"))


# \u2500\u2500 lookup contract \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_lookup_misses_return_none() -> None:
    # The production case: nothing registered, so every caller keeps the path it
    # had before the seam existed.
    assert worker_runtime.runtime_handle_for("sess", "w1", 1) is None
    assert worker_runtime.runtime_handle_for("sess", "w1") is None


def test_register_then_lookup_finds_the_handle() -> None:
    handle = _FakeHandle()

    key = worker_runtime.register_runtime_handle(
        handle, session_id="sess", worker_name="w1", instance_id=3
    )

    assert key == ("sess", "w1", 3)
    assert worker_runtime.runtime_handle_for("sess", "w1", 3) is handle
    # An instance-scoped key is not a name-scoped one: the lookup never guesses
    # which incarnation a caller meant.
    assert worker_runtime.runtime_handle_for("sess", "w1") is None


def test_register_without_an_instance_id_uses_the_two_tuple_key() -> None:
    handle = _FakeHandle()

    key = worker_runtime.register_runtime_handle(
        handle, session_id=None, worker_name="w1"
    )

    assert key == (None, "w1")
    assert worker_runtime.runtime_handle_for(None, "w1") is handle


def test_unregister_removes_the_handle() -> None:
    handle = _FakeHandle()
    worker_runtime.register_runtime_handle(
        handle, session_id="sess", worker_name="w1", instance_id=1
    )

    assert worker_runtime.unregister_runtime_handle("sess", "w1", 1) is handle
    assert worker_runtime.runtime_handle_for("sess", "w1", 1) is None
    # Idempotent: the handle's owner may be gone before its exit path runs.
    assert worker_runtime.unregister_runtime_handle("sess", "w1", 1) is None


def test_garbage_arguments_never_raise() -> None:
    # An unhashable session id cannot even form a key; "no handle" is still the
    # only honest answer, and a lookup must never break a supervisor endpoint.
    assert worker_runtime.runtime_handle_for(["not", "hashable"], "w1", 1) is None


@pytest.mark.parametrize("registry", [None, [], ["a"], 7, ("sess", "w1")])
def test_garbage_registry_contents_never_raise(registry, monkeypatch) -> None:
    monkeypatch.setattr(worker_runtime, "_RUNTIME_HANDLES", registry)

    assert worker_runtime.runtime_handle_for("sess", "w1", 1) is None


# \u2500\u2500 the dispatch branch \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_write_worker_command_dispatches_to_a_registered_handle(tmp_path) -> None:
    worker_dir = _worker_dir(tmp_path, generation=11)
    handle = _FakeHandle()
    worker_runtime.register_runtime_handle(
        handle, session_id="sess-x", worker_name="w1", instance_id=2
    )

    workspace_routes._write_worker_command(
        worker_dir, "kill", session_id="sess-x", worker_name="w1", instance_id=2
    )

    assert handle.calls == ["kill"]
    # One command, one transport: a routed command must not ALSO land on disk.
    assert not (worker_dir / "command.json").exists()


def test_the_branch_calls_dispatch_via_runtime(tmp_path, monkeypatch) -> None:
    # Proves the branch is live code rather than a comment: with a handle it is
    # ``_dispatch_via_runtime`` that runs, before any file is touched.
    worker_dir = _worker_dir(tmp_path, generation=4)
    handle = _FakeHandle()
    monkeypatch.setitem(worker_runtime._RUNTIME_HANDLES, ("sess-x", "w1", 1), handle)
    seen = []
    monkeypatch.setattr(
        workspace_routes,
        "_dispatch_via_runtime",
        lambda h, action: seen.append((h, action)),
    )

    workspace_routes._write_worker_command(
        worker_dir, "stop", session_id="sess-x", worker_name="w1", instance_id=1
    )

    assert seen == [(handle, "stop")]
    assert not (worker_dir / "command.json").exists()


@pytest.mark.parametrize(
    "action,verb",
    [("stop", "stop"), ("kill", "kill"), ("pause", "pause"), ("resume", "resume")],
)
def test_every_command_verb_maps_to_its_handle_verb(action, verb) -> None:
    handle = _FakeHandle()

    workspace_routes._dispatch_via_runtime(handle, action)

    assert handle.calls == [verb]


@pytest.mark.parametrize("action", ["cancel", "self_destruct", ""])
def test_a_verb_with_no_handle_equivalent_raises(action) -> None:
    # ``cancel`` travels on the frame, not the command channel, so it has no
    # verb here: refusing loudly beats answering None for something unhonoured.
    handle = _FakeHandle()

    with pytest.raises(ValueError) as raised:
        workspace_routes._dispatch_via_runtime(handle, action)

    assert action in str(raised.value)
    assert handle.calls == []


# \u2500\u2500 empty registry: the pre-seam behaviour \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_empty_registry_writes_the_fenced_command_as_before(tmp_path) -> None:
    worker_dir = _worker_dir(tmp_path, generation=11)

    workspace_routes._write_worker_command(
        worker_dir, "stop", session_id="sess-x", worker_name="w1", instance_id=2
    )

    assert _command_payload(worker_dir) == {
        "action": "stop",
        "session_id": "sess-x",
        "worker_name": "w1",
        "instance_id": 2,
        "epoch": 11,
    }


def test_empty_registry_still_degrades_to_the_bare_action(tmp_path) -> None:
    # No generation on disk: the documented UNFENCED degradation is untouched.
    worker_dir = _worker_dir(tmp_path)

    workspace_routes._write_worker_command(
        worker_dir, "resume", session_id=None, worker_name="w1", instance_id=1
    )

    assert _command_payload(worker_dir) == {"action": "resume"}


# \u2500\u2500 mode reporting \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_runtime_mode_report_agrees_with_resolve_runtime_mode() -> None:
    report = worker_runtime.runtime_mode_report({})
    assert report["mode"] == worker_runtime.MODE_THREAD
    assert report["error"] is None
    # S7a-3 makes the spawn path register for real, so this count is process
    # history (whoever this process spawned and has not torn down), not a
    # world-state fact.  Pin agreement with the store instead of a zero.
    assert isinstance(report["registered_handles"], int)
    assert report["registered_handles"] == len(worker_runtime._RUNTIME_HANDLES)
    process = {worker_runtime.WORKER_RUNTIME_MODE_ENV: worker_runtime.MODE_PROCESS}
    assert worker_runtime.runtime_mode_report(process)["mode"] == (
        worker_runtime.MODE_PROCESS
    )
    # The flag still fails closed at spawn time; the report says so instead.
    bad = worker_runtime.runtime_mode_report(
        {worker_runtime.WORKER_RUNTIME_MODE_ENV: "nonsense"}
    )
    assert bad["mode"] is None
    assert "nonsense" in bad["error"]

# ──────────────────────────────────────── S4a: the runtime -> supervisor frame sink ────────


def _make_worker(workspace_dir, frame_sink=None):
    """A real ``WorkerThread`` with hermetic defaults (no ``Agent`` built).

    ``__init__`` only lays down ``workers/<label>/`` and never starts the
    thread, so a per-test instance is cheap.  Same construction as
    ``tests/test_worker_budgets.py``.
    """
    return WorkerThread(
        name="seam-w",
        definition={},
        agent_config={"provider": "openai", "model": "gpt-4"},
        workspace_dir=workspace_dir,
        session_permissions={},
        instance_id=1,
        frame_sink=frame_sink,
    )


def _status_on_disk(worker):
    return json.loads(
        (worker._worker_dir / "status.json").read_text(encoding="utf-8")
    )


def test_status_write_emits_one_worker_state_frame(tmp_path) -> None:
    # One status write == one frame, and the four fields that exist on both
    # sides agree with the file the supervisor mirrors.
    frames = []
    worker = _make_worker(tmp_path, frames.append)
    worker.status = "working"
    worker.current_task = "read the docs"
    worker.error = None

    worker._write_status_file()

    assert len(frames) == 1
    frame = frames[0]
    assert isinstance(frame, WorkerState)
    assert frame.KIND == "worker_state"
    data = _status_on_disk(worker)
    assert frame.to_payload() == {
        "status": data["runtime_status"],
        "current_task": data["current_task"],
        "error": data["error"],
        "paused_manually": data["paused_manually"],
    }
    assert frame.status == "working"
    assert frame.current_task == "read the docs"


def test_frame_carries_error_and_paused_manually_verbatim(tmp_path) -> None:
    frames = []
    worker = _make_worker(tmp_path, frames.append)
    worker.status = "error"
    worker.error = "boom"
    worker._manual_only_pause = True

    worker._write_status_file()

    frame = frames[-1]
    assert frame.status == "error"
    assert frame.error == "boom"
    # ``paused_manually`` is the bool() of ``_manual_only_pause``; the frame
    # carries the coerced value, not the raw attribute.
    assert frame.paused_manually is True


def test_frame_is_emitted_only_after_status_json_is_in_place(tmp_path) -> None:
    # The emit lives inside the ``try`` *after* the FileLock block, so a sink
    # never sees a half-written file and never runs while the lock is held.
    seen = []

    def sink(frame):
        seen.append((frame.status, _status_on_disk(worker)["runtime_status"]))

    worker = _make_worker(tmp_path, sink)
    worker.status = "running"

    worker._write_status_file()

    assert seen == [("running", "running")]


def test_no_sink_writes_the_same_file_and_emits_nothing(tmp_path) -> None:
    # The production default (``frame_sink=None``) must leave status.json
    # byte-for-byte what it was before the seam existed.
    worker = _make_worker(tmp_path)
    assert worker._frame_sink is None
    worker.status = "paused"
    worker._manual_only_pause = True

    worker._write_status_file()

    expected = {
        "runtime_status": "paused",
        "current_task": None,
        "last_heartbeat": None,
        "error": None,
        "session_id": None,
        "current_context_tokens": 0,
        "max_context_tokens": worker.max_context_tokens,
        "started_at": None,
        "last_query_at": None,
        "paused_manually": True,
    }
    text = (worker._worker_dir / "status.json").read_text(encoding="utf-8")
    assert text == json.dumps(expected, indent=2, default=str)
    # No sink: _emit_frame is a silent no-op rather than an error.
    worker._emit_frame(object())


def test_raising_sink_is_swallowed_and_the_write_still_lands(tmp_path) -> None:
    # A broken sink must not take the worker down with it.
    def sink(frame):
        raise RuntimeError("sink exploded")

    worker = _make_worker(tmp_path, sink)
    worker.status = "error"
    worker.error = "kaput"

    worker._write_status_file()

    assert _status_on_disk(worker)["runtime_status"] == "error"
    assert _status_on_disk(worker)["error"] == "kaput"


# ────────────────────────────── S4a (B): the supervisor-side state cache ──────────────────────────────


def test_detaching_is_terminal_for_cancel() -> None:
    # ``detaching`` is a status WorkerThread really assigns
    # (worker_thread.py:1553), so a cancel there has no turn left to interrupt
    # -- it belongs with the other terminal statuses.
    assert "detaching" in worker_runtime.CANCEL_TERMINAL_STATUSES
    worker = SimpleNamespace(status="detaching", _turn_cancel=threading.Event())
    handle = worker_runtime.WorkerRuntimeHandle(worker)

    assert handle.cancel() == "refused"
    assert worker._turn_cancel.is_set() is False


def _write_mirror(directory, **overrides):
    """The full 10-key ``status.json`` ``_write_status_file`` lays down."""
    data = {
        "runtime_status": "busy",
        "current_task": "task-1",
        "last_heartbeat": "2026-01-01T00:00:00+00:00",
        "error": None,
        "session_id": "s-1",
        "current_context_tokens": 7,
        "max_context_tokens": 8192,
        "started_at": "2026-01-01T00:00:00+00:00",
        "last_query_at": None,
        "paused_manually": True,
    }
    data.update(overrides)
    (directory / "status.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _process_handle(worker):
    return worker_runtime.WorkerRuntimeHandle(
        worker, mode=worker_runtime.MODE_PROCESS
    )


def test_state_reads_the_status_json_mirror_in_process_mode(tmp_path) -> None:
    # No frame yet: the fallback is the very file the web backend already
    # reads, so the supervisor needs no new source of truth.
    _write_mirror(tmp_path)
    handle = _process_handle(SimpleNamespace(_worker_dir=tmp_path))

    state = handle.state()

    assert state == WorkerState(
        status="busy", current_task="task-1", error=None, paused_manually=True
    )
    # Only the four fields WorkerState declares survive; the other six keys of
    # status.json have no field to land in.
    assert len(state.to_payload()) == 4


def test_status_json_path_resolves_the_real_worker_thread_mirror(tmp_path) -> None:
    # The production shape. A process-mode handle wraps a real ``WorkerThread``
    # (the documented contract, and what ``spec_from_worker`` requires), so the
    # mirror has to be found through the same ``workers/<label>/`` directory the
    # thread itself writes to -- not through an attribute only the doubles have.
    worker = _make_worker(tmp_path)
    handle = _process_handle(worker)

    assert worker._worker_dir == tmp_path / "workers" / "seam-w"
    assert handle._status_json_path() == worker._worker_dir / "status.json"

    # ...and the file the thread writes is the file the handle reads back, so
    # the fallback is wired end to end rather than merely path-shaped.
    worker.status = "working"
    worker.current_task = "read the docs"
    worker.error = None
    worker._write_status_file()

    assert handle.state() == WorkerState(
        status="working",
        current_task="read the docs",
        error=None,
        paused_manually=False,
    )


def test_state_without_cache_or_mirror_reports_unknown(tmp_path) -> None:
    # Neither sight nor file: say so, and say it with a status no worker can
    # be in, rather than raising or inventing ``ready``.
    handle = _process_handle(SimpleNamespace(_worker_dir=tmp_path))

    state = handle.state()

    assert state == WorkerState(
        status=worker_runtime.UNKNOWN_WORKER_STATUS,
        current_task=None,
        error=None,
        paused_manually=False,
    )
    assert state.status == "unknown"


def test_corrupt_mirror_falls_back_to_unknown(tmp_path) -> None:
    (tmp_path / "status.json").write_text("{not json", encoding="utf-8")
    handle = _process_handle(SimpleNamespace(_worker_dir=tmp_path))

    assert handle.state().status == worker_runtime.UNKNOWN_WORKER_STATUS


def test_state_prefers_the_cached_frame_over_a_stale_mirror(tmp_path) -> None:
    # The cache is what the worker reported; the file is a mirror that may
    # lag. Observed beats mirrored.
    _write_mirror(tmp_path)
    handle = _process_handle(SimpleNamespace(_worker_dir=tmp_path))
    frame = WorkerState(
        status="paused", current_task="t2", error=None, paused_manually=False
    )

    handle.on_frame(frame)

    assert handle.state() == frame


def test_on_frame_ignores_garbage_and_never_raises() -> None:
    handle = _process_handle(SimpleNamespace())

    handle.on_frame(object())
    handle.on_frame("nonsense")

    # An unknown kind is ignored, not interpreted, and nothing was cached --
    # neither clock, since neither frame was one of ours.
    assert handle._last_worker_state is None
    assert handle._last_heartbeat_at is None
    assert handle._last_received_at is None


def test_on_frame_never_raises_even_when_the_clock_does(monkeypatch) -> None:
    handle = _process_handle(SimpleNamespace())

    def boom():
        raise RuntimeError("clock exploded")

    monkeypatch.setattr(worker_runtime.time, "time", boom)

    # This runs on the read loop: a frame that surprises us must not be able
    # to take the transport down.
    handle.on_frame(
        WorkerState(
            status="ready", current_task=None, error=None, paused_manually=False
        )
    )

    # The state half was cached before the clock was read, and the clock half
    # stayed empty rather than raising: a broken read loop is worse than a
    # missing timestamp.
    assert handle._last_worker_state.status == "ready"
    assert handle._last_received_at is None


def test_on_frame_caches_a_heartbeat_timestamp() -> None:
    handle = _process_handle(SimpleNamespace())

    handle.on_frame(
        Heartbeat(
            at="2026-01-01T00:00:00+00:00",
            status="ready",
            current_context_tokens=0,
            max_context_tokens=8192,
        )
    )

    # A heartbeat is freshness, not state: the state cache stays empty and the
    # arrival time is what gets recorded.
    assert handle._last_worker_state is None
    assert isinstance(handle._last_received_at, float)
    assert (
        worker_runtime.frame_arrival_age_seconds(
            handle._last_received_at, now=handle._last_received_at + 5.0
        )
        == 5.0
    )


def test_cached_frame_age_seconds() -> None:
    assert worker_runtime.cached_frame_age_seconds(100.0, now=130.0) == 30.0
    # No frame yet is not an age of zero -- it is ignorance.
    assert worker_runtime.cached_frame_age_seconds(None) is None
    # Skew clamps at 0.0 instead of going negative.
    assert worker_runtime.cached_frame_age_seconds(200.0, now=100.0) == 0.0

def _heartbeat(at: str) -> Heartbeat:
    return Heartbeat(
        at=at,
        status="ready",
        current_context_tokens=0,
        max_context_tokens=8192,
    )


def test_heartbeat_caches_the_reported_tick_and_the_arrival() -> None:
    # D1's primary reading: ``at`` is the worker's own statement of when it
    # ticked, so the handle keeps it as epoch seconds -- the same value
    # status.json carries as text, read the way ``worker_manager._heartbeat_age``
    # reads it (fromisoformat, naive-means-UTC).
    handle = _process_handle(SimpleNamespace())

    handle.on_frame(_heartbeat("2026-01-01T00:00:00+00:00"))

    assert handle._last_heartbeat_at == datetime(
        2026, 1, 1, tzinfo=timezone.utc
    ).timestamp()
    # ...and the receipt clock moved too: one frame, two clocks.
    assert isinstance(handle._last_received_at, float)
    assert handle._last_worker_state is None

    # A naive timestamp is UTC, not local time, exactly as in the thread-side
    # helpers -- otherwise the age shifts by the host's offset.
    handle.on_frame(_heartbeat("2026-01-01T00:00:00"))
    assert handle._last_heartbeat_at == datetime(
        2026, 1, 1, tzinfo=timezone.utc
    ).timestamp()


def test_a_stale_tick_with_a_fresh_delivery_reads_both_ways() -> None:
    # The reason both readings exist rather than one: a worker that says it
    # ticked long ago but whose frames are arriving right now is stale by its
    # own clock and alive by the transport's. Collapsing them would have to
    # pick a lie.
    handle = _process_handle(SimpleNamespace())

    handle.on_frame(_heartbeat("2020-01-01T00:00:00+00:00"))

    now = handle._last_received_at
    heartbeat_age = worker_runtime.heartbeat_age_seconds(
        handle._last_heartbeat_at, now=now
    )
    arrival_age = worker_runtime.frame_arrival_age_seconds(now, now=now)

    assert heartbeat_age != arrival_age
    assert heartbeat_age > 3600.0
    assert arrival_age == 0.0


def test_a_malformed_heartbeat_timestamp_costs_only_the_tick_reading() -> None:
    # ``at`` is the child's text, and text can be wrong. The frame still proves
    # delivery: reading an unparseable timestamp must not lose that, and must
    # not raise on the read loop.
    handle = _process_handle(SimpleNamespace())

    handle.on_frame(_heartbeat("not-a-timestamp"))

    assert handle._last_heartbeat_at is None
    assert worker_runtime.heartbeat_age_seconds(handle._last_heartbeat_at) is None
    assert isinstance(handle._last_received_at, float)


def test_every_age_reading_reports_ignorance_rather_than_zero() -> None:
    # "Nothing cached" is not "just now": 0.0 is the freshest possible reading
    # and would be a lie about a worker we have never heard from.
    empty = _process_handle(SimpleNamespace())

    assert worker_runtime.heartbeat_age_seconds(empty._last_heartbeat_at) is None
    assert worker_runtime.frame_arrival_age_seconds(empty._last_received_at) is None
    assert worker_runtime.heartbeat_age_seconds(None) is None
    assert worker_runtime.frame_arrival_age_seconds(None) is None
    # A WorkerState frame moves only the receipt clock: it carries no tick.
    empty.on_frame(
        WorkerState(
            status="ready", current_task=None, error=None, paused_manually=False
        )
    )
    assert empty._last_heartbeat_at is None
    assert isinstance(empty._last_received_at, float)
