"""
Regression test: DockerCodeRunner must surface ContainerManager.start()'s
``{"error": ...}`` result (no ``"id"`` key) as a clear error, not as a
KeyError('id') wrapped in "Unexpected error: 'id'".

Background
----------
``ContainerManager.start()`` returns ``{"error": ...}`` (instead of an id
payload) when the per-workspace container limit is reached or any pre-create
check fails. Before the guard, ``execute()`` did ``info["id"]`` unconditionally,
so the limit condition blew up as ``KeyError('id')`` and the generic
``except Exception`` handler turned it into the misleading
``"Unexpected error: 'id'"``.

This test is fully mocked: no Docker daemon or container is required.
"""

import json

import tools.docker_code_runner as dcr_module


def _make_runner(tmp_path):
    """Build a DockerCodeRunner that resolves the workspace from the
    deprecated ``workspace_path`` field (no session registries needed)."""
    return dcr_module.DockerCodeRunner(
        command="echo hello",
        workspace_path=str(tmp_path),
    )


def test_start_error_dict_surfaces_limit_message(tmp_path, monkeypatch):
    """start() returning {"error": "container limit ..."} must surface that
    message in the response error and never reach exec/stop."""

    class FakeManager:
        """ContainerManager stand-in whose start() hits the limit branch."""

        def __init__(self, **kwargs):
            pass

        def start(self, image=None, worker_name=None, *, lifecycle_class=None):
            # Real ContainerManager.start() signature: accepts worker_name
            # (thoughtmachine.worker ownership label on fresh creates).
            return {
                "error": "Workspace container limit (4) reached "
                         "(4 active container(s)). "
                         "Stop or remove a running container to free a slot."
            }

        def exec(self, *args, **kwargs):
            raise AssertionError("exec must not be called when start() errored")

        def stop(self, *args, **kwargs):
            raise AssertionError("stop must not be called when start() errored")

    monkeypatch.setattr(dcr_module, "ContainerManager", FakeManager)

    result = json.loads(_make_runner(tmp_path).execute())

    assert result["success"] is False
    assert result["exit_code"] == -1
    # The surfaced error is the limit message, not a KeyError-style "'id'".
    assert "container limit" in result["error"]
    assert result["error"] != "'id'"
    assert "Unexpected error" not in result["error"]


def test_drift_deny_removes_and_restarts_exactly_once(tmp_path, monkeypatch):
    """A drift DENY must remove the drifted container EXACTLY once and re-start
    EXACTLY once (a single recovery attempt, never a retry loop)."""

    calls = {"start": 0, "removed": []}

    class FakeManager:
        """First start() denies on drift; second (post-remove) create succeeds."""

        def __init__(self, **kwargs):
            pass

        def start(self, image=None, worker_name=None, *, lifecycle_class=None):
            calls["start"] += 1
            if calls["start"] == 1:
                return {
                    "error": "Container isolation is MORE PERMISSIVE than the "
                             "session policy (network_more_permissive).",
                    "drift": {
                        "drifted": True,
                        "decision": "deny",
                        "reason": "network_more_permissive",
                        "network_mode": "host",
                        "workspace_mode": "rw",
                        "source": "workspace-label",
                        "container_id": "c" * 16,
                    },
                }
            return {"id": "n" * 16, "name": "agent-x",
                    "status": "created", "note": ""}

        def remove(self, container_id):
            calls["removed"].append(container_id)
            return {"status": "removed", "container_id": container_id}

        def exec(self, *args, **kwargs):
            return {"stdout": "ok", "stderr": "", "exit_code": 0}

        def stop(self, *args, **kwargs):
            return {"status": "stopped"}

    monkeypatch.setattr(dcr_module, "ContainerManager", FakeManager)

    result = json.loads(_make_runner(tmp_path).execute())

    # (a) remove() called EXACTLY once, with the drifted id read from
    #     drift["container_id"] (nested, not the top level).
    assert calls["removed"] == ["c" * 16]
    assert len(calls["removed"]) == 1
    # (b) start() called EXACTLY twice: the original + one recovery re-start.
    assert calls["start"] == 2
    assert result["success"] is True


def test_drift_deny_without_container_id_raises(tmp_path, monkeypatch):
    """A drift DENY whose drift dict LACKS ``container_id`` must RAISE rather
    than silently proceeding: remove() and a second start() must NOT run."""

    calls = {"start": 0, "removed": 0}

    class FakeManager:
        def __init__(self, **kwargs):
            pass

        def start(self, image=None, worker_name=None, *, lifecycle_class=None):
            calls["start"] += 1
            return {
                "error": "Container isolation is MORE PERMISSIVE than the "
                         "session policy (network_more_permissive).",
                "drift": {
                    # NOTE: no "container_id" key on purpose.
                    "drifted": True,
                    "decision": "deny",
                    "reason": "network_more_permissive",
                    "network_mode": "host",
                    "workspace_mode": "rw",
                    "source": "workspace-label",
                },
            }

        def remove(self, container_id):
            calls["removed"] += 1
            return {"status": "removed"}

        def exec(self, *args, **kwargs):
            raise AssertionError("exec must not run when start was denied")

        def stop(self, *args, **kwargs):
            raise AssertionError("stop must not run when start was denied")

    monkeypatch.setattr(dcr_module, "ContainerManager", FakeManager)

    result = json.loads(_make_runner(tmp_path).execute())

    # The deny branch RAISED a clear RuntimeError (surfaced as the error field)
    # instead of silently skipping recovery.
    assert result["success"] is False
    assert "container_id" in result["error"]
    # The absent-id case must NOT remove, and must NOT attempt a second start.
    assert calls["removed"] == 0
    assert calls["start"] == 1
