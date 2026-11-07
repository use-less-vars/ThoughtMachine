"""Wave 10a (session 10a-ii) commit 2b — ``_server_version()`` alignment.

The runtime version triple reported by CheckSystem (``revision``, ``branch``,
``dirty``) must come from ONE import-time snapshot captured by
``web_ui.backend.server``.  The old implementation shelled out to
``git rev-parse``/``git status`` a second time inside CheckSystem, which could
disagree with the revision the web UI actually serves.  These tests pin the
import-based contract and the no-subprocess guarantee.
"""
from __future__ import annotations

import builtins
import subprocess

import pytest

import tools.workspace.check_system as check_system


def _server_module():
    import web_ui.backend.server as server  # imported lazily; needs sys.path

    return server


def test_server_version_reads_server_snapshot(monkeypatch):
    server = _server_module()
    monkeypatch.setattr(server, "_SERVER_REVISION", "abc123")
    monkeypatch.setattr(server, "_SERVER_BRANCH", "fix/wave10a-container-truth")
    monkeypatch.setattr(server, "_SERVER_DIRTY", True)

    info = check_system._server_version()

    assert info == {
        "revision": "abc123",
        "branch": "fix/wave10a-container-truth",
        "dirty": True,
    }
    assert set(info) == {"revision", "branch", "dirty"}
    assert isinstance(info["revision"], str)
    assert isinstance(info["branch"], str)
    assert isinstance(info["dirty"], bool)


def test_server_version_dirty_is_coerced_to_bool(monkeypatch):
    server = _server_module()
    monkeypatch.setattr(server, "_SERVER_BRANCH", "main")
    # A truthy-but-not-bool value must still surface as a bool.
    monkeypatch.setattr(server, "_SERVER_DIRTY", "non-empty")

    info = check_system._server_version()
    assert info["dirty"] is True


def test_server_version_does_not_shell_out(monkeypatch):
    server = _server_module()
    monkeypatch.setattr(server, "_SERVER_REVISION", "rev-snapshot")
    monkeypatch.setattr(server, "_SERVER_BRANCH", "br-snapshot")
    monkeypatch.setattr(server, "_SERVER_DIRTY", False)

    def _boom(*args, **kwargs):  # pragma: no cover - must never fire
        raise AssertionError("_server_version() must not spawn a git subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    info = check_system._server_version()

    assert info == {
        "revision": "rev-snapshot",
        "branch": "br-snapshot",
        "dirty": False,
    }
    # The module must not carry a top-level ``subprocess`` binding that the
    # function could fall back on.
    assert not hasattr(check_system, "subprocess")


def test_server_version_falls_back_when_server_unimportable(monkeypatch):
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "web_ui.backend.server":
            raise ImportError("web ui not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    info = check_system._server_version()

    assert info["revision"] == "unknown"
    assert info["branch"] == "unknown"
    assert info["dirty"] is False
