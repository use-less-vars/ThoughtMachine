"""Contract tests for ``thoughtmachine.host_port`` — the single host-execution port.

``thoughtmachine/host_port.py`` is the ONLY module in ``thoughtmachine/`` or
``tools/`` permitted to import ``subprocess``; everything that wants to run a
process on the host must go through :func:`host_run`.  These tests pin that
port's *contract* (the fixed check order, the refusal messages that matter, the
audit JSONL shape and the timeout/kill semantics), not its implementation.

The authoritative contract is the design-contract section of the Gen 49
working document ``.thoughtmachine/working_docs/192b086357c9.json``.

THIS FILE IS DELIBERATELY RED.  ``thoughtmachine.host_port`` does not exist
yet, so the module-level import below makes collection fail with
``ModuleNotFoundError: No module named 'thoughtmachine.host_port'``.  That
failure is the deliverable of the RED step; the GREEN step adds the module.

Isolation: every filesystem write goes under ``tmp_path`` and the vault is
redirected with ``THOUGHTMACHINE_VAULT_ROOT`` (the idiom used by
``tests/test_host_resource_policy.py`` and ``tests/test_host_bash_tool.py``);
``SessionRegistry.get_default`` is patched so no real registry state is read.
No network, no real vault access.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import time

import pytest

from thoughtmachine.host_port import (
    AUTHORIZED_VALUES,
    HostRunHandle,
    HostRunResult,
    host_run,
    host_run_async,
)

from security.resource_catalog import RESOURCE_CATALOG
from session.session_registry import SessionRegistry
from tools.host_resource_policy import workspace_allows_host_resources


# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

AUDIT_FILENAME = "host_port_audit.jsonl"

#: PRE line fields, exactly as the contract lists them (no extras, no omissions).
PRE_FIELDS = {
    "timestamp",
    "event",
    "workspace_id",
    "session_id",
    "grain",
    "argv",
    "shell",
    "timeout",
    "cwd",
    "decision",
}

#: POST line fields, exactly as the contract lists them.
POST_FIELDS = {
    "timestamp",
    "event",
    "workspace_id",
    "session_id",
    "grain",
    "argv",
    "exit_code",
    "duration_ms",
}

WS_ID = "ws-a"
SESSION_ID = "session-of-a"
OTHER_WS_ID = "ws-b"
OTHER_SESSION_ID = "session-of-b"

#: Grain names the contract explicitly says must NOT exist anywhere.
FORBIDDEN_GRAIN_NAMES = (
    "execution",
    "system",
    "execution_full",
    "mcp_connect",
    "network_outbound",
    "filesystem_read",
    "allow_session",
    "deny",
)

#: Env keys that match the contract's CREDENTIAL_SHAPED regex.
CREDENTIAL_ENV_KEYS = (
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DB_PASSWORD",
    "SSH_KEY",
    "GITHUB_PAT",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

_DEFAULT_CONFIG = object()


# ---------------------------------------------------------------------------
# Shared sandbox helper / fixture
# ---------------------------------------------------------------------------


class _StubRegistry:
    """Minimal stand-in for :class:`SessionRegistry` (only ``get`` is used)."""

    def __init__(self, entries):
        self._entries = entries

    def get(self, session_id):
        return self._entries.get(session_id)

    def get_all(self):
        return dict(self._entries)


class Sandbox:
    """Hermetic vault + workspace + session registry for one host_run call.

    Everything the port consults is redirected into ``tmp_path``:

    * ``THOUGHTMACHINE_VAULT_ROOT`` -> ``<tmp>/vault`` (read by
      ``thoughtmachine.vault.vault_root`` on every call),
    * ``<tmp>/vault/workspaces/<id>/config.json`` -> the operator kill switch
      plus the workspace root used for cwd containment,
    * ``SessionRegistry.get_default`` -> :class:`_StubRegistry`,
    * ``audit_dir`` -> ``<tmp>/audit`` so audit assertions are deterministic.

    A test therefore overrides only the axis it is about.
    """

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        *,
        workspace_id=WS_ID,
        config=_DEFAULT_CONFIG,
        entries=None,
    ):
        self.tmp_path = pathlib.Path(tmp_path)
        self.vault = self.tmp_path / "vault"
        self.ws_root = self.tmp_path / "ws_root"
        self.audit_dir = self.tmp_path / "audit"
        self.workspace_id = workspace_id
        self.session_id = SESSION_ID
        self._monkeypatch = monkeypatch
        self._entries = {}

        self.ws_root.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(self.vault))

        self.config_path = self.vault / "workspaces" / workspace_id / "config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        if config is _DEFAULT_CONFIG:
            config = {"allow_host_resources": True, "root": str(self.ws_root)}
        self.set_config(config)

        if entries is None:
            entries = {
                SESSION_ID: {"session_id": SESSION_ID, "workspace_id": workspace_id},
                OTHER_SESSION_ID: {
                    "session_id": OTHER_SESSION_ID,
                    "workspace_id": OTHER_WS_ID,
                },
            }
        self.set_registry(entries)

    # -- state ------------------------------------------------------------

    def set_config(self, payload):
        """Rewrite ``config.json``; *payload* may be a dict, a raw str or None.

        ``None`` means "no config file at all" (the fail-closed case).
        """
        if payload is None:
            if self.config_path.exists():
                self.config_path.unlink()
            return self.config_path
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        self.config_path.write_text(raw, encoding="utf-8")
        return self.config_path

    def set_registry(self, entries):
        """Patch ``SessionRegistry.get_default`` to serve *entries*."""
        self._entries = dict(entries)
        entries_ref = self._entries
        self._monkeypatch.setattr(
            SessionRegistry,
            "get_default",
            classmethod(lambda cls: _StubRegistry(entries_ref)),
            raising=True,
        )

    # -- calls ------------------------------------------------------------

    def kwargs(self, **overrides):
        """Baseline, fully-permitted ``host_run`` keyword arguments."""
        base = {
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "grain": "host_bash",
            "effective_permissions": {"host_bash": "allow"},
            "audit_dir": self.audit_dir,
            "cwd": str(self.ws_root),
            "timeout": 10,
        }
        base.update(overrides)
        return base

    def run(self, command, **overrides):
        return host_run(command, **self.kwargs(**overrides))

    # -- audit ------------------------------------------------------------

    @property
    def audit_path(self):
        return self.audit_dir / AUDIT_FILENAME

    def audit_lines(self):
        """Return the audit JSONL file parsed into a list of dicts."""
        if not self.audit_path.exists():
            return []
        text = self.audit_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def permitted(tmp_path, monkeypatch):
    """A workspace in which ``host_run`` is fully permitted."""
    return Sandbox(tmp_path, monkeypatch)


def _noop():
    """A command that does nothing and exits 0 (no shell features needed)."""
    return [sys.executable or "python3", "-c", "pass"]


def _expect_refusal(sandbox, command=None, **overrides):
    """Call ``host_run`` expecting a refusal; return the message text."""
    with pytest.raises(PermissionError) as excinfo:
        sandbox.run(command if command is not None else _noop(), **overrides)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# 1-3: grain vocabulary
# ---------------------------------------------------------------------------


def test_host_run_refuses_when_grain_not_authorized(permitted):
    """``host_bash`` at ``ask`` is below the port's authorized value (``allow``)."""
    message = _expect_refusal(permitted, effective_permissions={"host_bash": "ask"})
    assert "host_bash" in message


def test_host_run_refuses_when_grain_banned(permitted):
    message = _expect_refusal(permitted, effective_permissions={"host_bash": "banned"})
    assert "host_bash" in message


def test_host_run_refuses_unknown_grain(permitted):
    """An unknown grain is refused by name and the allowed names are listed."""
    message = _expect_refusal(permitted, grain="execution_full")
    assert "execution_full" in message
    for grain in AUTHORIZED_VALUES:
        assert grain in message, message


# ---------------------------------------------------------------------------
# 4: workspace operator kill switch
# ---------------------------------------------------------------------------


def test_host_run_refuses_when_kill_switch_off(permitted):
    permitted.set_config(
        {"allow_host_resources": False, "root": str(permitted.ws_root)}
    )
    message = _expect_refusal(permitted)
    assert "allow_host_resources" in message


# ---------------------------------------------------------------------------
# 5: session/workspace identity
# ---------------------------------------------------------------------------


def test_host_run_identity_mismatch_raises(permitted):
    """A session recorded against workspace B cannot be used to run in A."""
    message = _expect_refusal(
        permitted,
        workspace_id=permitted.workspace_id,  # ws-a
        session_id=OTHER_SESSION_ID,  # registered against ws-b
    )
    assert OTHER_SESSION_ID in message
    assert permitted.workspace_id in message
    assert "does not belong" in message


# ---------------------------------------------------------------------------
# 6: cwd containment
# ---------------------------------------------------------------------------


def test_host_run_refuses_cwd_outside_workspace(permitted):
    outside = permitted.tmp_path / "outside"
    outside.mkdir()

    for cwd in ("/root", str(outside)):
        message = _expect_refusal(permitted, cwd=cwd)
        assert cwd in message or os.path.realpath(cwd) in message, message


def test_host_run_cwd_before_workspace_check_is_symlink_resolved(permitted):
    """A symlink *inside* the workspace that points outside must be refused."""
    outside = permitted.tmp_path / "outside-target"
    outside.mkdir()
    link = permitted.ws_root / "link-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    message = _expect_refusal(permitted, cwd=str(link))
    assert str(outside) in message or os.path.realpath(str(outside)) in message, message


# ---------------------------------------------------------------------------
# 7: shell is off by default
# ---------------------------------------------------------------------------


def test_host_run_defaults_to_no_shell(permitted):
    if shutil.which("echo") is None:
        pytest.skip("no echo binary available")

    argv = ["echo", "a;b"]
    result = permitted.run(argv)

    assert isinstance(result, HostRunResult)
    assert result.argv == argv
    assert result.grain == "host_bash"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.ok is True
    # Without a shell the semicolon is literal data, never a command separator.
    assert result.stdout.strip() == "a;b"


# ---------------------------------------------------------------------------
# 8-9: audit trail
# ---------------------------------------------------------------------------


def test_host_run_writes_audit_line(permitted):
    argv = _noop()
    permitted.run(argv, timeout=10)

    lines = permitted.audit_lines()
    assert lines, "expected a %s entry" % AUDIT_FILENAME

    pre = lines[0]
    assert set(pre) == PRE_FIELDS
    assert pre["event"] == "pre"
    assert pre["decision"] == "allow"
    assert pre["workspace_id"] == permitted.workspace_id
    assert pre["session_id"] == permitted.session_id
    assert pre["grain"] == "host_bash"
    assert pre["argv"] == argv
    assert pre["shell"] is False
    assert pre["timeout"] == 10
    assert os.path.realpath(pre["cwd"]) == os.path.realpath(str(permitted.ws_root))
    assert isinstance(pre["timestamp"], str) and pre["timestamp"]


def test_host_run_writes_exit_code_to_audit_log(permitted):
    argv = [sys.executable or "python3", "-c", "import sys; sys.exit(3)"]
    result = permitted.run(argv)
    assert result.exit_code == 3

    lines = permitted.audit_lines()
    assert len(lines) == 2, lines
    assert lines[0]["event"] == "pre"

    post = lines[-1]
    assert post["event"] == "post"
    assert set(post) == POST_FIELDS
    assert post["exit_code"] == result.exit_code == 3
    assert isinstance(post["duration_ms"], int)
    assert post["duration_ms"] >= 0
    assert post["workspace_id"] == permitted.workspace_id
    assert post["session_id"] == permitted.session_id
    assert post["grain"] == "host_bash"
    assert post["argv"] == argv


# ---------------------------------------------------------------------------
# 10-11: timeout and process-group kill
# ---------------------------------------------------------------------------


def test_host_run_timeout_sends_sigterm_then_sigkill(permitted, tmp_path):
    """The whole process group dies, not just the direct child."""
    if shutil.which("sh") is None or shutil.which("sleep") is None:
        pytest.skip("sh/sleep unavailable on this platform")

    marker = tmp_path / "survivor.marker"
    script = '( sleep 0.8; touch "%s" ) & wait' % marker

    result = permitted.run(["sh", "-c", script], timeout=0.3)

    assert result.timed_out is True
    assert result.exit_code == -9
    assert result.ok is False

    # The backgrounded subshell lives in the same process group; it would
    # still create the marker if only the direct child had been signalled.
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        assert not marker.exists(), "background process survived the kill"
        time.sleep(0.05)
    assert not marker.exists()


def test_host_run_timeout_writes_exit_code_minus_nine_to_audit(permitted):
    timeout = 0.2
    result = permitted.run(
        [sys.executable or "python3", "-c", "import time; time.sleep(5)"],
        timeout=timeout,
    )
    assert result.timed_out is True
    assert result.exit_code == -9

    lines = permitted.audit_lines()
    assert len(lines) == 2, lines
    post = lines[-1]
    assert post["event"] == "post"
    assert post["exit_code"] == -9
    assert post["duration_ms"] == int(timeout * 1000) == 200


# ---------------------------------------------------------------------------
# 12: credential-shaped env is refused (deferred to W3 credential injection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", CREDENTIAL_ENV_KEYS)
def test_host_run_refuses_credential_shaped_env(permitted, key):
    message = _expect_refusal(permitted, env={key: "value"})
    assert "looks credential-shaped" in message
    assert "credential_inject=True (W3)" in message


# ---------------------------------------------------------------------------
# 13: mcp grain needs an mcp client
# ---------------------------------------------------------------------------


def test_host_run_requires_client_type_for_mcp_grain(permitted):
    message = _expect_refusal(
        permitted, grain="mcp", effective_permissions={"mcp": "connect"}
    )
    assert "client_type" in message

    result = permitted.run(
        _noop(),
        grain="mcp",
        effective_permissions={"mcp": "connect"},
        client_type="mcp",
    )
    assert result.grain == "mcp"
    assert result.ok is True


# ---------------------------------------------------------------------------
# 14: identity may not be empty
# ---------------------------------------------------------------------------


def test_host_run_rejects_empty_identity(permitted):
    assert "workspace" in _expect_refusal(permitted, workspace_id="").lower()
    assert "session" in _expect_refusal(permitted, session_id="").lower()


# ---------------------------------------------------------------------------
# 15: the async surface is a signature-only stub (W3)
# ---------------------------------------------------------------------------


def test_host_run_async_is_signature_only(permitted):
    with pytest.raises(NotImplementedError) as excinfo:
        host_run_async(_noop(), **permitted.kwargs())
    assert "W3" in str(excinfo.value)

    assert isinstance(HostRunHandle, type)
    handle = HostRunHandle.__new__(HostRunHandle)  # bypass the unknown __init__
    for method in ("poll", "wait", "kill"):
        assert callable(getattr(handle, method))
        with pytest.raises(NotImplementedError) as excinfo:
            getattr(handle, method)()
        assert "W3" in str(excinfo.value), method


# ---------------------------------------------------------------------------
# 16: AUTHORIZED_VALUES must speak the real resource-catalog vocabulary
# ---------------------------------------------------------------------------


def test_authorized_values_use_real_categories():
    assert set(AUTHORIZED_VALUES) <= set(RESOURCE_CATALOG)
    assert set(AUTHORIZED_VALUES) <= {"host_bash", "mcp", "network"}

    known_levels = {
        level
        for values in RESOURCE_CATALOG.values()
        for level in values
        if isinstance(level, str)
    }

    for grain, values in AUTHORIZED_VALUES.items():
        assert isinstance(values, frozenset), grain
        for level in values:
            assert level in known_levels, (grain, level)
            if (grain, level) == ("network", "full"):
                # Documented divergence: the PORT authorizes network "full"
                # even though ``RESOURCE_CATALOG["network"]`` has no such
                # level ("full" exists only on mcp).  The two assertions
                # below make the exception self-checking: it is provably
                # needed, and cannot silently outlive the conflict.
                continue
            assert level in RESOURCE_CATALOG[grain], (grain, level)

    assert "full" in known_levels
    assert "full" not in RESOURCE_CATALOG["network"]

    for name in FORBIDDEN_GRAIN_NAMES:
        assert name not in AUTHORIZED_VALUES, name


# ---------------------------------------------------------------------------
# 17: the port's kill switch agrees with tools.host_resource_policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case,payload,expected",
    [
        ("json-true", {"allow_host_resources": True}, True),
        ("string-true", {"allow_host_resources": "true"}, False),
        ("int-one", {"allow_host_resources": 1}, False),
        ("absent-key", {}, False),
        ("missing-file", None, False),
        ("malformed-json", "{not json", False),
    ],
)
def test_host_port_kill_switch_agrees_with_host_resource_policy(
    permitted, case, payload, expected
):
    if isinstance(payload, dict) and payload.get("allow_host_resources") is True:
        payload = dict(payload, root=str(permitted.ws_root))
    permitted.set_config(payload)

    policy_verdict = workspace_allows_host_resources(permitted.workspace_id)

    try:
        port_verdict = permitted.run(_noop()).ok
    except PermissionError:
        port_verdict = False

    assert policy_verdict is expected, case
    assert port_verdict is expected, case
    assert port_verdict == policy_verdict, case
