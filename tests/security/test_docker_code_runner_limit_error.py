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

        def start(self, image=None, worker_name=None):
            # Real ContainerManager.start() signature: accepts worker_name
            # (thoughtmachine.worker ownership label on fresh creates).
            return {
                "error": "Workspace container limit (4) reached. "
                         "Stop an unused container first."
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
