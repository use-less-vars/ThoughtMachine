"""W3b -- ``git_environment`` is a real seam: setter plus BOTH read paths.

``design_credential_resource.md`` L63 requires ``git_environment`` to stop being
inert: "make git_environment a real seam (setter + host read; currently inert)".
Before this wave the key was read in exactly one place
(``tools/git_info_tool.py`` container path), never written by anything, and the
host path did not read it at all.

What is pinned here
-------------------
1. One normaliser (``normalize_git_environment``) decides what a seam entry is:
   a ``{plain str: plain str}`` pair, or nothing.  A ``Secret`` value is dropped,
   not stringified -- ``str(Secret)`` is a mask.
2. One merge (``merge_git_environment``) layers the seam UNDER
   ``GIT_ENV_PINS``, so a session can never disable the hardening pins
   (``GIT_CONFIG_SYSTEM=/dev/null``, ``GIT_PAGER=cat``).
3. ``set_git_environment()`` round-trips to the read half
   (``GitReadTool._git_environment()``) through ``agent_config``, the same key
   the container path already consulted, and clears the seam on an empty
   mapping.
4. The host path (``_exec_host_raw``) and the container path
   (``_exec_container_raw``) forward the seam and apply the identical pins, so
   the two backends cannot drift on what a session may configure.

THESE TESTS WERE RED FIRST: ``GIT_ENV_PINS`` / ``normalize_git_environment`` /
``merge_git_environment`` did not exist and ``set_git_environment`` was absent,
so this module failed collection with ``ImportError`` -- that failure is the RED
deliverable for W3b.

No real git, no container, no host process: ``SandboxedExecution`` and the
resource manager are replaced by recorders and only their kwargs are asserted.
"""

from __future__ import annotations

import pytest

from tools.git_info_tool import (
    GIT_ENV_PINS,
    GitReadTool,
    merge_git_environment,
    normalize_git_environment,
)

from agent.credentials.injector import Secret


#: A syntactically plausible, obviously fake token.  Never a real credential.
FAKE_TOKEN_VALUE = "ghp_this_is_a_fake_value_2222"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tool(effective=None):
    """Build a GitReadTool with a post-construction effective-permission dict."""
    tool = GitReadTool(
        operation="status",
        session_id="s",
        workspace_id="w",
        session_permissions={},
    )
    tool.effective_permissions = effective or {"git": "read", "filesystem": "read"}
    return tool


class _FakeExecResult:
    def __init__(self, returncode=0, stdout="OUT", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_sandbox(calls):
    """A ``SandboxedExecution`` stand-in that records every run's kwargs."""

    class _Recorder:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, argv, **kwargs):
            calls.append({"argv": list(argv), **kwargs})
            return _FakeExecResult()

    return _Recorder


class _RecordingManager:
    """A resource-manager stand-in that records ``exec`` kwargs."""

    def __init__(self):
        self.calls = []

    def exec(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return {"exit_code": 0, "stdout": "OUT", "stderr": ""}


# ---------------------------------------------------------------------------
# 1: the normaliser and the merge
# ---------------------------------------------------------------------------


def test_pins_are_exactly_the_historical_hardening_env():
    # Moving the pins into one place must not change them.
    assert GIT_ENV_PINS == {
        "GIT_PAGER": "cat",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def test_normalize_keeps_plain_str_pairs():
    seam = {"GIT_ASKPASS": "/tmp/askpass.sh", "GIT_TERMINAL_PROMPT": "0"}

    assert normalize_git_environment(seam) == seam


def test_normalize_returns_a_copy_not_the_caller_mapping():
    seam = {"GIT_PAGER": "less"}

    normalized = normalize_git_environment(seam)

    assert normalized == seam
    assert normalized is not seam


@pytest.mark.parametrize("value", [None, 0, b"bytes", Secret(FAKE_TOKEN_VALUE)])
def test_normalize_drops_non_plain_str_values(value):
    assert normalize_git_environment({"GIT_ASKPASS": value}) == {}


@pytest.mark.parametrize("key", [None, 7, b"GIT_PAGER"])
def test_normalize_drops_non_str_keys(key):
    assert normalize_git_environment({key: "value"}) == {}


@pytest.mark.parametrize("seam", [None, "GIT_PAGER=cat", ["GIT_PAGER", "cat"], 5])
def test_normalize_rejects_non_mapping(seam):
    assert normalize_git_environment(seam) == {}


def test_merge_layers_the_seam_under_the_pins():
    merged = merge_git_environment({"TM_GIT_ASKPASS_SECRET": FAKE_TOKEN_VALUE})

    assert merged["TM_GIT_ASKPASS_SECRET"] == FAKE_TOKEN_VALUE
    assert merged["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert merged["GIT_PAGER"] == "cat"


@pytest.mark.parametrize("key, value", sorted(GIT_ENV_PINS.items()))
def test_merge_never_lets_the_seam_override_a_pin(key, value):
    merged = merge_git_environment({key: "/tmp/hijacked"})

    assert merged[key] == value


def test_merge_returns_pins_even_without_a_seam():
    assert merge_git_environment(None) == GIT_ENV_PINS


# ---------------------------------------------------------------------------
# 2: the setter round-trips to the read half
# ---------------------------------------------------------------------------


def test_set_git_environment_round_trips_to_the_read_path():
    tool = _tool()
    seam = {"TM_GIT_ASKPASS_SECRET": FAKE_TOKEN_VALUE, "GIT_ASKPASS": "/tmp/a.sh"}

    stored = tool.set_git_environment(seam)

    assert stored == seam
    assert tool._git_environment() == seam
    assert tool.agent_config == {"git_environment": seam}


def test_set_git_environment_normalises_before_storing():
    tool = _tool()

    stored = tool.set_git_environment(
        {"GIT_ASKPASS": "/tmp/a.sh", "TM_GIT_ASKPASS_SECRET": Secret(FAKE_TOKEN_VALUE)}
    )

    # The Secret is refused at write time, so no read path can stringify it.
    assert stored == {"GIT_ASKPASS": "/tmp/a.sh"}
    assert tool._git_environment() == {"GIT_ASKPASS": "/tmp/a.sh"}
    assert FAKE_TOKEN_VALUE not in repr(tool.agent_config)


def test_set_git_environment_clears_the_seam_on_empty_mapping():
    tool = _tool()
    tool.set_git_environment({"GIT_ASKPASS": "/tmp/a.sh"})

    assert tool.set_git_environment({}) == {}
    assert tool._git_environment() == {}
    assert "git_environment" not in tool.agent_config


def test_set_git_environment_preserves_other_agent_config_keys():
    tool = _tool()
    tool.agent_config = {"unrelated": "kept"}

    tool.set_git_environment({"GIT_ASKPASS": "/tmp/a.sh"})

    assert tool.agent_config["unrelated"] == "kept"


def test_read_path_tolerates_invalid_agent_config():
    tool = _tool()
    tool.agent_config = {"git_environment": "not-a-mapping"}

    assert tool._git_environment() == {}


# ---------------------------------------------------------------------------
# 3: both execution paths forward the seam, with the pins winning
# ---------------------------------------------------------------------------


def test_host_path_forwards_the_git_environment_seam(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _recording_sandbox(calls)
    )
    tool = _tool()
    tool._resolved_workspace_path = str(tmp_path)
    tool.set_git_environment(
        {"TM_GIT_ASKPASS_SECRET": FAKE_TOKEN_VALUE, "GIT_ASKPASS": "/tmp/a.sh"}
    )

    tool._exec_host_raw(tmp_path, ["status"])

    extra_env = calls[0]["extra_env"]
    assert extra_env["TM_GIT_ASKPASS_SECRET"] == FAKE_TOKEN_VALUE
    assert extra_env["GIT_ASKPASS"] == "/tmp/a.sh"
    assert extra_env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert extra_env["GIT_PAGER"] == "cat"


def test_host_path_pins_win_over_the_seam(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _recording_sandbox(calls)
    )
    tool = _tool()
    tool._resolved_workspace_path = str(tmp_path)
    tool.set_git_environment({"GIT_CONFIG_SYSTEM": "/tmp/hijacked", "GIT_PAGER": "less"})

    tool._exec_host_raw(tmp_path, ["status"])

    extra_env = calls[0]["extra_env"]
    assert extra_env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert extra_env["GIT_PAGER"] == "cat"


def test_container_path_forwards_the_git_environment_seam(tmp_path):
    tool = _tool()
    tool._resolved_workspace_path = str(tmp_path)
    tool.set_git_environment(
        {"TM_GIT_ASKPASS_SECRET": FAKE_TOKEN_VALUE, "GIT_ASKPASS": "/tmp/a.sh"}
    )
    manager = _RecordingManager()

    tool._exec_container_raw(tmp_path, ["status"], manager=manager)

    environment = manager.calls[0]["environment"]
    assert environment["TM_GIT_ASKPASS_SECRET"] == FAKE_TOKEN_VALUE
    assert environment["GIT_ASKPASS"] == "/tmp/a.sh"
    assert environment["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert environment["GIT_PAGER"] == "cat"


def test_container_path_pins_win_over_the_seam(tmp_path):
    tool = _tool()
    tool._resolved_workspace_path = str(tmp_path)
    tool.set_git_environment({"GIT_CONFIG_SYSTEM": "/tmp/hijacked", "GIT_PAGER": "less"})
    manager = _RecordingManager()

    tool._exec_container_raw(tmp_path, ["status"], manager=manager)

    environment = manager.calls[0]["environment"]
    assert environment["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert environment["GIT_PAGER"] == "cat"


def test_host_and_container_paths_agree_on_the_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", _recording_sandbox(calls)
    )
    tool = _tool()
    tool._resolved_workspace_path = str(tmp_path)
    tool.set_git_environment(
        {"TM_GIT_ASKPASS_SECRET": FAKE_TOKEN_VALUE, "GIT_ASKPASS": "/tmp/a.sh"}
    )
    manager = _RecordingManager()

    tool._exec_host_raw(tmp_path, ["status"])
    tool._exec_container_raw(tmp_path, ["status"], manager=manager)

    assert calls[0]["extra_env"] == manager.calls[0]["environment"]
