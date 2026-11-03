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

import pytest

from tools.workspace import worker_runtime
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
    assert worker_runtime.runtime_mode_report({}) == {
        "mode": worker_runtime.MODE_THREAD,
        "error": None,
        "registered_handles": 0,
    }
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
