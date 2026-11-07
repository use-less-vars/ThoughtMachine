"""Regression: DockerCodeRunner scratch scripts must live on the exec tmpfs,
not inside the bind-mounted workspace.

Background
----------
``DockerCodeRunner._prepare_script_command`` builds the shell command that
writes the user's script to a scratch file and runs it with the chosen
interpreter. The scratch directory was hard-coded to ``/workspace/tmp``.
Because ``/workspace`` is the *bind mount of the session workspace* (the repo
in dev), every run deposited a ``script_<uuid8>.sh`` file straight into the
repo's ``./tmp/`` and never removed it -- so the repo slowly filled with
``script_*.sh`` litter.

Fix
---
Route the scratch dir to ``/home/agent/tmp``. ``/home/agent`` is a per-container
tmpfs mounted ``rw`` and exec-capable (the mount options carry no ``noexec``),
writable by the sandbox user (uid 1000), and it is discarded with the
container -- so the workspace bind stays clean.

Tests are hermetic: no Docker daemon is required (ContainerManager is mocked,
matching tests/security/test_docker_code_runner_limit_error.py).
"""

import json
import re
from pathlib import Path

import tools.docker_code_runner as dcr_module

# repo root: this file is tests/security/<name>.py -> parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]

# /home/agent/tmp/script_<8 hex>.sh
_SCRATCH_RE = re.compile(r"/home/agent/tmp/script_[0-9a-f]{8}\.sh")


def _make_runner(tmp_path, **kwargs):
    """Build a DockerCodeRunner resolving its workspace from ``workspace_path``."""
    return dcr_module.DockerCodeRunner(workspace_path=str(tmp_path), **kwargs)


# ---------------------------------------------------------------------------
# Test A -- the scratch dir is the exec tmpfs, never the workspace bind.
# ---------------------------------------------------------------------------


def test_prepare_script_command_writes_to_exec_tmpfs(tmp_path):
    runner = _make_runner(tmp_path, script="echo hi", interpreter="bash")
    command = runner._prepare_script_command("echo hi", "bash")

    # The scratch directory is the /home/agent tmpfs ...
    assert 'mkdir -p "/home/agent/tmp"' in command
    assert _SCRATCH_RE.search(command), command
    # ... and the command must not target the bind-mounted workspace at all.
    assert "/workspace" not in command


# ---------------------------------------------------------------------------
# Test B -- a run leaves the workspace bind clean.
# ---------------------------------------------------------------------------


def test_run_does_not_litter_the_workspace_bind(tmp_path, monkeypatch):
    """Execute the tool end-to-end (mocked manager) and assert a real run of
    its command leaves the workspace's ``tmp/`` free of stray scripts."""

    captured = {}

    class FakeManager:
        def __init__(self, **kwargs):
            pass

        def start(self, image=None, worker_name=None, *, lifecycle_class=None,
                  heal_missing=False):
            return {"id": "a" * 16, "name": "agent-scratch",
                    "status": "created", "note": ""}

        def exec(self, container_id, command=None, workdir=None, timeout=None,
                 environment=None):
            captured["command"] = command
            return {"stdout": "", "stderr": "", "exit_code": 0}

        def stop(self, *args, **kwargs):
            return {"status": "stopped"}

    monkeypatch.setattr(dcr_module, "ContainerManager", FakeManager)

    # The workspace bind is the repo itself in the sandbox; its tmp/ is exactly
    # where the old hard-coded ``/workspace/tmp`` scratch dir dropped scripts.
    workspace_tmp = _REPO_ROOT / "tmp"
    scratch_tmp = Path("/home/agent/tmp")

    def _snapshot(d):
        return set(d.glob("script_*.sh")) if d.is_dir() else set()

    ws_before = _snapshot(workspace_tmp)
    scratch_before = _snapshot(scratch_tmp)

    runner = _make_runner(tmp_path, script="echo hello-from-script",
                          interpreter="bash")
    result = json.loads(runner.execute())
    assert result["success"] is True

    command = captured["command"]

    # Run the generated command for real. In the sandbox /workspace *is* the
    # repo bind, so the old scratch dir would litter the repo right here.
    import subprocess
    subprocess.run(command, shell=True, capture_output=True, text=True)

    ws_after = _snapshot(workspace_tmp)
    scratch_after = _snapshot(scratch_tmp)

    try:
        # (1) The scratch script is addressed on the exec-capable tmpfs ...
        assert _SCRATCH_RE.search(command), command
        # (2) ... and the command never targets the bind-mounted workspace.
        assert "/workspace" not in command
        # (3) a real run therefore leaves the workspace's tmp/ untouched.
        assert ws_after - ws_before == set()
    finally:
        # Never leave litter behind, whichever branch ran.
        for p in (ws_after - ws_before) | (scratch_after - scratch_before):
            try:
                p.unlink()
            except OSError:
                pass
