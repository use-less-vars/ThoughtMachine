# tests/test_workspace_routes_runtime_fallback.py
"""The runtime-dispatch seam must FAIL SAFE, not 500.

The seam is one branch at the top of
``workspace_routes._write_worker_command``: ``runtime_handle_for`` resolves a
live ``WorkerRuntimeHandle`` and, when it finds one, the command is delivered
in-process by ``_dispatch_via_runtime`` instead of through ``command.json``.

That branch used to be a bare ``return _dispatch_via_runtime(handle, action)``.
On success the early return is exactly right -- one command, one transport --
but a raise inside the handle skipped the generation read, the fence and
``write_command`` with it, and ``_write_worker_command`` has no try/except of
its own, so the exception left the route as a 500 and the worker never received
the command at all.  The catch is what these tests pin: a failing runtime
degrades to the durable (fenced) channel, which is the only command path a
running worker is guaranteed to read.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.workspace import worker_runtime
from web_ui.backend import workspace_routes
from web_ui.backend.workspace_routes import router


WS_ID = "ws-runtime-fallback"

#: Deliberately not 1: a fence that dropped or defaulted its epoch must fail
#: these assertions rather than pass by accident.
_SEEDED_GENERATION = 11


@pytest.fixture(autouse=True)
def _handle_registry_teardown():
    """Pop handles registered during a test (same shape as the seam suite).

    "No handle registered" is the production state every other test depends on,
    so a handle this file seeds must never be visible to the next test.
    """
    preexisting = set(worker_runtime._RUNTIME_HANDLES.keys())
    yield
    added = [k for k in worker_runtime._RUNTIME_HANDLES.keys() if k not in preexisting]
    for key in added:
        worker_runtime._RUNTIME_HANDLES.pop(key, None)


class _FakeHandle:
    """Stands in for a ``WorkerRuntimeHandle``; the seam only needs identity."""

    def stop(self) -> object:
        return True

    def kill(self) -> object:
        return True

    def pause(self) -> object:
        return True

    def resume(self) -> object:
        return True


def _worker_dir(tmp_path, generation=_SEEDED_GENERATION):
    """A worker directory that takes the FENCED command path."""
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / "generation").write_text(str(generation), encoding="utf-8")
    return worker_dir


def _command_payload(worker_dir):
    return json.loads((worker_dir / "command.json").read_text(encoding="utf-8"))


def _failing_dispatch(monkeypatch):
    """Make the runtime transport raise -- the failure the fallback exists for."""
    monkeypatch.setattr(
        workspace_routes,
        "_dispatch_via_runtime",
        lambda handle, action: (_ for _ in ()).throw(
            RuntimeError("runtime handle exploded")
        ),
    )


# ── unit: the catch lives in _write_worker_command ──────────────────────────


def test_a_raising_handle_falls_back_to_the_fenced_command_file(
    tmp_path, monkeypatch, caplog
) -> None:
    worker_dir = _worker_dir(tmp_path)
    handle = _FakeHandle()
    # FORCING THE RUNTIME BRANCH: the production registry is empty, so
    # ``runtime_handle_for`` would answer None and the branch under test would
    # never run.  Seeding this key is what makes the test mean anything.
    monkeypatch.setitem(worker_runtime._RUNTIME_HANDLES, ("sess-x", "w1", 2), handle)
    _failing_dispatch(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=workspace_routes.__name__):
        # (i) it must NOT raise: reaching the next line is that assertion.
        workspace_routes._write_worker_command(
            worker_dir,
            "kill",
            session_id="sess-x",
            worker_name="w1",
            instance_id=2,
        )

    # (ii) + (iii) the durable channel carried the command, fully fenced --
    # same assertion the seam suite uses for the empty-registry path.
    assert _command_payload(worker_dir) == {
        "action": "kill",
        "session_id": "sess-x",
        "worker_name": "w1",
        "instance_id": 2,
        "epoch": _SEEDED_GENERATION,
    }

    # (iv) the degradation is visible, with the traceback attached.
    fallbacks = [
        record
        for record in caplog.records
        if "falling back to the fenced command channel" in record.getMessage()
    ]
    assert len(fallbacks) == 1, [r.getMessage() for r in caplog.records]
    assert fallbacks[0].levelno == logging.WARNING
    assert fallbacks[0].exc_info is not None


def test_the_fallback_also_covers_an_unmapped_verb(tmp_path, monkeypatch) -> None:
    # ``_dispatch_via_runtime`` raises ValueError for a verb it cannot honour;
    # that is a real production raise, not just a hypothetical handle blow-up.
    worker_dir = _worker_dir(tmp_path)
    monkeypatch.setitem(
        worker_runtime._RUNTIME_HANDLES, ("sess-x", "w1", 1), _FakeHandle()
    )

    workspace_routes._write_worker_command(
        worker_dir,
        "cancel",
        session_id="sess-x",
        worker_name="w1",
        instance_id=1,
    )

    assert _command_payload(worker_dir) == {
        "action": "cancel",
        "session_id": "sess-x",
        "worker_name": "w1",
        "instance_id": 1,
        "epoch": _SEEDED_GENERATION,
    }


def test_a_working_handle_still_returns_without_touching_disk(
    tmp_path, monkeypatch
) -> None:
    # The success path must stay byte-identical to the pre-fix behaviour: one
    # command, one transport, no command file.
    worker_dir = _worker_dir(tmp_path)
    handle = _FakeHandle()
    monkeypatch.setitem(worker_runtime._RUNTIME_HANDLES, ("sess-x", "w1", 2), handle)
    seen = []
    monkeypatch.setattr(
        workspace_routes,
        "_dispatch_via_runtime",
        lambda h, action: seen.append((h, action)),
    )

    workspace_routes._write_worker_command(
        worker_dir,
        "kill",
        session_id="sess-x",
        worker_name="w1",
        instance_id=2,
    )

    assert seen == [(handle, "kill")]
    assert not (worker_dir / "command.json").exists()


# ── route level: the HTTP call must not 500 ─────────────────────────────────
#
# ``tests/test_worker_lifecycle_routes.py`` owns the real-router harness, so
# this is the same harness reduced to the one thing it takes to reach the
# branch: a tmp vault, a seeded worker directory and a registered handle.


def test_pause_route_does_not_500_when_the_runtime_handle_raises(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    ws_dir = tmp_path / "workspaces" / WS_ID
    worker_dir = ws_dir / "workers" / "w1"
    worker_dir.mkdir(parents=True)
    (worker_dir / "generation").write_text(
        str(_SEEDED_GENERATION), encoding="utf-8"
    )
    # Legacy layout: no session directory, so the route resolves session_id=None
    # and instance 1 from the bare name -- that is the key the lookup forms.
    monkeypatch.setitem(worker_runtime._RUNTIME_HANDLES, (None, "w1", 1), _FakeHandle())
    _failing_dispatch(monkeypatch)

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(f"/api/workspace/{WS_ID}/workers/w1/pause")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pausing"
    # The worker still gets its command, fenced, despite the runtime failure.
    assert _command_payload(worker_dir) == {
        "action": "pause",
        "session_id": "",
        "worker_name": "w1",
        "instance_id": 1,
        "epoch": _SEEDED_GENERATION,
    }
