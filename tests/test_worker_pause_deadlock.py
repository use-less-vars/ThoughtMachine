"""Deadlock regression tests: the worker pause path and the status.json write.

WHY the production code is quoted in each docstring:

  (1) ``web_ui/backend/workspace_routes.py:1888-1898`` signals the in-memory
      worker *inside* ``with _registry_lock:``.  ``_registry_lock`` is a plain
      ``threading.Lock`` (``tools/workspace/worker_registry.py:162``) and
      ``WorkerThread.pause()`` (:2549-2575) waits on the lifecycle manager plus
      the agent's reading mode, so every registry reader --
      ``WorkerRegistry.get_all_workers`` (:316-319), the stop/kill route
      (``workspace_routes.py:1375``) and the resume route (:2026) -- stalls
      behind a worker that is slow to park.

  (2) ``tools/workspace/worker_thread.py:5304-5321`` must keep
      ``FileLock(status.json)`` OFF the frame-sink call.  ``_emit_frame``
      (:5246-5269) deliberately has no re-entrancy guard, so a sink that calls
      back into ``_write_status_file`` re-enters.  ``FileLock`` is only
      re-entrant per *instance* (``session/lock.py:103-104``), so the inner call
      creates a second instance on the same path and self-blocks for
      ``DEFAULT_TIMEOUT`` (10s, ``session/lock.py:51``) before raising
      ``FileLockTimeoutError`` -- which ``_write_status_file`` SWALLOWS in
      ``except (OSError, Exception)`` (:5322-5323) with only a ``logger.error``.
      The failure therefore has to be detected by ELAPSED TIME, not by an
      exception, which is what these tests assert.

The real code paths are exercised: the real route handler (only the
workspace-dir lookup and the cross-process command-file write are stubbed; the
in-memory fast path under ``_registry_lock`` runs for real) and the real
``WorkerThread._write_status_file`` (the thread object is built with
``object.__new__`` so no agent / session / WLM machinery is started).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from tools.workspace.worker_registry import WorkerRegistry

# Budget for "did not block".  Deliberately a full order of magnitude below the
# 10s FileLock timeout and well above a scheduling hiccup on a loaded CI box.
PROMPT_BUDGET = 1.0


class _BlockingPauseWorker:
    """Registry entry whose ``pause()`` parks until the test releases it.

    Models a real ``WorkerThread.pause()`` that cannot return quickly (WLM
    transition + agent reading-mode handshake), without dragging an agent into
    the test.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.pause_calls = 0

    def pause(self) -> None:
        self.pause_calls += 1
        self.entered.set()
        self.release.wait(20.0)


def _stub_worker_thread(tmp_path):
    """A ``WorkerThread`` shell that can run the REAL ``_write_status_file``.

    ``object.__new__`` bypasses ``__init__`` (no agent, no WLM, no thread
    start); only the attributes ``_write_status_file`` reads are set.
    """
    from tools.workspace import worker_thread as wt_mod

    if wt_mod.FileLock is None:  # pragma: no cover - platform without flock
        pytest.skip("session.lock.FileLock unavailable on this platform")

    thread = object.__new__(wt_mod.WorkerThread)
    thread.status = "running"
    thread.current_task = None
    thread.last_heartbeat = "2024-01-01T00:00:00+00:00"
    thread.error = None
    thread.session_id = "sess-1"
    # ``max_context_tokens`` is a read-only property derived from the
    # injected agent config (:1991-2003): give it the backing dict.
    thread._agent_config_dict = {"model": "gpt-4o"}
    thread.started_at = "2024-01-01T00:00:00+00:00"
    thread.last_query_at = None
    thread._manual_only_pause = False
    thread._worker_dir = tmp_path
    thread._frame_sink = None
    thread.get_current_context_tokens = lambda: 0
    return thread


def test_pause_does_not_hold_the_registry_lock(tmp_path, monkeypatch):
    """The pause route must not signal the worker while holding _registry_lock.

    Production code under test (quoted)::

        # web_ui/backend/workspace_routes.py:1888-1898
        with _registry_lock:
            for key, thread in list(_worker_registry.items()):
                ...
                try:
                    thread.pause()
                except Exception:
                    pass  # File-based pause will still work

    Design: registry entry is a stub whose ``pause()`` blocks on an Event; the
    real handler runs on a background thread; a concurrent
    ``WorkerRegistry.get_all_workers()`` (the same lock the workers-list /
    stop / resume routes take) must return within ``PROMPT_BUDGET``.
    """
    routes = pytest.importorskip("web_ui.backend.workspace_routes")

    monkeypatch.setattr(routes, "ensure_workspace_dirs", lambda ws_id: None)
    monkeypatch.setattr(routes, "_workspace_dir", lambda ws_id: tmp_path)
    # Legacy layout: workers/<name>/ (empty dir -> not a session directory).
    (tmp_path / "workers" / "alpha").mkdir(parents=True)
    # The cross-process command file is not the subject here (and would need a
    # real worker dir + fence); the in-memory fast path below it is real.
    monkeypatch.setattr(routes, "_write_worker_command", lambda *a, **k: None)

    stub = _BlockingPauseWorker()
    registry = WorkerRegistry.get_instance()
    saved = dict(registry._worker_registry)
    registry._worker_registry.clear()
    registry._worker_registry[("sess-1", "alpha")] = stub

    box: dict[str, object] = {}

    def _run_route() -> None:
        try:
            box["result"] = asyncio.run(
                routes.pause_worker(None, "ws-1", "alpha", None)
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the asserts
            box["error"] = exc

    route_thread = threading.Thread(
        target=_run_route, name="pause-route", daemon=True
    )
    probe_result: dict[str, object] = {}
    probe: threading.Thread | None = None
    try:
        route_thread.start()
        assert stub.entered.wait(5.0), (
            "pause route never reached thread.pause() -- the route shape "
            "changed, this test no longer exercises the lock block"
        )

        def _read_registry() -> None:
            started = time.monotonic()
            try:
                probe_result["value"] = registry.get_all_workers()
            finally:
                probe_result["elapsed"] = time.monotonic() - started

        probe = threading.Thread(
            target=_read_registry, name="registry-reader", daemon=True
        )
        probe.start()
        probe.join(PROMPT_BUDGET)
        elapsed = probe_result.get("elapsed")

        assert not probe.is_alive(), (
            "WorkerRegistry.get_all_workers() was still blocked after "
            f"{PROMPT_BUDGET}s while the pause handler was inside "
            "thread.pause(): the route holds _registry_lock across a call "
            "that can block (web_ui/backend/workspace_routes.py:1888-1898)."
        )
        assert elapsed is not None and elapsed < PROMPT_BUDGET, (
            f"registry read took {elapsed}s (> {PROMPT_BUDGET}s): the pause "
            "handler serialised it behind thread.pause()"
        )
    finally:
        stub.release.set()
        route_thread.join(10.0)
        if probe is not None:
            probe.join(10.0)
        registry._worker_registry.clear()
        registry._worker_registry.update(saved)

    assert "error" not in box, box.get("error")
    assert box.get("result", {}).get("status") == "accepted"
    assert stub.pause_calls == 1


def test_nested_status_write_does_not_self_block(tmp_path):
    """A frame sink that writes status again must not self-block.

    Production code under test (quoted)::

        # tools/workspace/worker_thread.py:5303-5321
        if FileLock is not None:
            with FileLock(str(target)):
                os.replace(tmp_path_str, str(target))
        ...
        if self._frame_sink is not None:
            self._emit_frame(...)

    Design: install a sink that calls ``_write_status_file()`` from inside the
    first emission.  If the lock were still held across the sink, the nested
    call's SECOND ``FileLock`` instance would block for ``DEFAULT_TIMEOUT``
    (10s) and the raise would be swallowed (:5322-5323) -- so the assertion is
    on ELAPSED TIME, not on an exception.
    """
    thread = _stub_worker_thread(tmp_path)
    frames: list[object] = []
    nested: dict[str, float] = {}

    def _sink(frame) -> None:
        frames.append(frame)
        if len(frames) == 1:
            started = time.monotonic()
            thread._write_status_file()
            nested["elapsed"] = time.monotonic() - started

    thread._frame_sink = _sink

    started = time.monotonic()
    thread._write_status_file()
    elapsed = time.monotonic() - started

    assert nested, "frame sink was never invoked -- no re-entry was exercised"
    assert nested["elapsed"] < PROMPT_BUDGET, (
        "the status write triggered from inside the frame sink took "
        f"{nested['elapsed']:.2f}s (> {PROMPT_BUDGET}s): _write_status_file "
        "held FileLock(status.json) while emitting the frame, so the re-entrant "
        "call self-blocked and swallowed FileLockTimeoutError "
        "(tools/workspace/worker_thread.py:5304-5323)."
    )
    assert elapsed < PROMPT_BUDGET, (
        f"the outer status write took {elapsed:.2f}s (> {PROMPT_BUDGET}s): the "
        "nested write blocked on the status.json FileLock"
    )
    data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert data["runtime_status"] == "running"


def test_status_lock_is_free_while_frame_sink_emits(tmp_path):
    """``FileLock(status.json)`` must already be released when the sink runs.

    Production code under test (quoted)::

        # tools/workspace/worker_thread.py:5309-5312
        # in place and the FileLock is released (see _emit_frame for why
        # the lock must not be held across the sink).

    Design: the sink tries to acquire the very same lock from a fresh
    ``FileLock`` instance with a short timeout.  A lock still held by the
    emitting frame makes that acquire time out (``session/lock.py:143-150``).
    """
    from session.lock import FileLock

    thread = _stub_worker_thread(tmp_path)
    seen: dict[str, object] = {}

    def _sink(frame) -> None:
        started = time.monotonic()
        lock = FileLock(str(tmp_path / "status.json"), timeout=0.5)
        try:
            lock.acquire()
            seen["acquired"] = True
        except Exception as exc:  # noqa: BLE001 - the recorded evidence
            seen["error"] = exc
        finally:
            lock.release()
            seen["elapsed"] = time.monotonic() - started

    thread._frame_sink = _sink
    thread._write_status_file()

    assert seen.get("acquired") is True, (
        "the status.json FileLock was still held while the frame sink ran: "
        f"{seen.get('error')!r} (tools/workspace/worker_thread.py:5304-5321)"
    )
    assert seen["elapsed"] < 0.5, (
        f"a fresh FileLock acquisition inside the sink took {seen['elapsed']:.2f}s"
    )
