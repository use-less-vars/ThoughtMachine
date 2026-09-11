"""Tests for the periodic container-sweep scheduler (Phase 1.1).

Verifies that ``web_ui.backend.server`` wires a background task that re-runs
both best-effort container sweeps on an interval, and that the task is
cancelled cleanly on lifespan shutdown.

These tests never require the docker daemon: the two sweep wrappers are
monkeypatched to thread-safe counters / no-ops.
"""
import asyncio
import threading

import pytest


def _load_server():
    from web_ui.backend import server
    return server


def test_periodic_sweep_loop_runs_both_sweeps_and_cancels(monkeypatch):
    """The loop re-invokes BOTH sweeps repeatedly and honours cancellation."""
    server = _load_server()

    lock = threading.Lock()
    counts = {"exited": 0, "orphan": 0}

    def fake_exited():
        with lock:
            counts["exited"] += 1

    def fake_orphan():
        with lock:
            counts["orphan"] += 1

    monkeypatch.setattr(
        server, "_sweep_exited_workspace_containers", fake_exited)
    monkeypatch.setattr(
        server, "_sweep_orphan_resource_containers", fake_orphan)

    async def scenario():
        task = asyncio.create_task(
            server._periodic_container_sweep_loop(interval_s=0.02))
        # Let the loop iterate several times.
        await asyncio.sleep(0.2)
        assert not task.done(), "sweep loop should still be running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(scenario())

    assert task.cancelled()
    assert counts["exited"] >= 2, counts
    assert counts["orphan"] >= 2, counts
    # Both sweeps must be invoked equally often (one full pass per iteration).
    assert counts["exited"] == counts["orphan"], counts


def test_periodic_sweep_loop_swallows_sweep_errors(monkeypatch):
    """A raising sweep must not kill the loop (best-effort, never fatal)."""
    server = _load_server()

    lock = threading.Lock()
    counts = {"exited": 0, "orphan": 0}

    def boom_exited():
        with lock:
            counts["exited"] += 1
        raise RuntimeError("boom")

    def ok_orphan():
        with lock:
            counts["orphan"] += 1

    monkeypatch.setattr(
        server, "_sweep_exited_workspace_containers", boom_exited)
    monkeypatch.setattr(
        server, "_sweep_orphan_resource_containers", ok_orphan)

    async def scenario():
        task = asyncio.create_task(
            server._periodic_container_sweep_loop(interval_s=0.02))
        await asyncio.sleep(0.15)
        # Loop must survive the raising sweep.
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # The sibling sweep still ran despite the other raising.
    assert counts["orphan"] >= 2, counts


def test_lifespan_creates_and_cancels_sweep_task(monkeypatch):
    """Startup creates app.state.container_sweep_task; shutdown cancels it."""
    server = _load_server()

    # Neutralise the real sweeps so startup does no docker/filesystem work.
    monkeypatch.setattr(
        server, "_sweep_exited_workspace_containers", lambda: None)
    monkeypatch.setattr(
        server, "_sweep_orphan_resource_containers", lambda: None)

    captured = {}

    async def scenario():
        async with server.lifespan(server.app):
            task = getattr(server.app.state, "container_sweep_task", None)
            assert task is not None, "lifespan did not create sweep task"
            assert not task.done(), "sweep task should be pending at startup"
            captured["task"] = task
        return captured["task"]

    task = asyncio.run(scenario())

    # After lifespan shutdown the task must be finished (cancelled).
    assert task.done(), "sweep task not finished after shutdown"
    assert task.cancelled() or task.exception() is None
