"""Portability tests for ``thoughtmachine/audit_logger.py``.

Regression guard for the Windows breakage where the audit log defaulted to
the POSIX-only path ``/tmp/container_audit.log`` and the rotating handler was
constructed without any error handling.  On Windows there is no ``/tmp``, so
``_get_handler()`` raised and — because ``infra/containers/executor.py`` calls
``audit_event()`` at module import time — the whole container-execution import
chain died.

The module under test is loaded fresh (private module object, see
:func:`_load_module`) so every test starts with no cached handler and without
touching any other module's state.  The real ``~/.thoughtmachine`` tree is
never written to: a temporary vault root is injected per test.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "thoughtmachine" / "audit_logger.py"
REPO_ROOT = MODULE_PATH.parents[1]
DEFAULT_NAME = "container_audit.log"

# Production call sites that used to append to a hardcoded /tmp path by hand.
AUDITED_CALL_SITES = (
    ("tools.docker_code_runner", Path("tools") / "docker_code_runner.py"),
    ("thoughtmachine.security", Path("thoughtmachine") / "security.py"),
)


def _load_module():
    """Import a private, isolated instance of ``thoughtmachine.audit_logger``."""
    spec = importlib.util.spec_from_file_location("audit_logger_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must never raise (Windows: no /tmp)
    return module


def test_default_path_follows_portable_log_root(monkeypatch, tmp_path):
    """The default path is the vault log root — never a hardcoded /tmp path."""
    module = _load_module()
    monkeypatch.delenv("CONTAINER_AUDIT_LOG_PATH", raising=False)

    monkeypatch.delenv("THOUGHTMACHINE_VAULT_ROOT", raising=False)
    assert Path(module._resolve_log_path()) == (
        Path(os.path.expanduser("~")) / ".thoughtmachine" / "logs" / DEFAULT_NAME
    )

    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    assert Path(module._resolve_log_path()) == vault / "logs" / DEFAULT_NAME

    # The original defect was a quoted POSIX path literal baked into this module.
    assert re.search(r"""["']/tmp/""", MODULE_PATH.read_text(encoding="utf-8")) is None


def test_missing_log_root_is_created_and_event_is_written(monkeypatch, tmp_path):
    """A missing log directory is created on demand and the event lands in it."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    monkeypatch.delenv("CONTAINER_AUDIT_LOG_PATH", raising=False)
    module = _load_module()

    log_path = vault / "logs" / DEFAULT_NAME
    assert not (vault / "logs").exists()

    module.audit_event("CONTAINER_CREATE", "image=agent-exec-abc123 network=bridge")

    assert log_path.is_file()
    (line,) = log_path.read_text(encoding="utf-8").splitlines()
    epoch, _, payload = line.partition(" | ")
    assert float(epoch) > 0
    assert payload == "CONTAINER_CREATE | image=agent-exec-abc123 network=bridge"
    assert module._sink_failed is False


def test_unusable_sink_never_raises_and_warns_once(monkeypatch, tmp_path):
    """When the log dir cannot be created the logger degrades to a silent no-op."""
    vault = tmp_path / "vault"
    vault.mkdir()
    blocker = vault / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    monkeypatch.delenv("CONTAINER_AUDIT_LOG_PATH", raising=False)

    module = _load_module()  # import itself must survive an unusable sink
    warnings: list[str] = []
    monkeypatch.setattr(module, "_warn_once", warnings.append)

    module.audit_event("CONTAINER_CREATE", "image=x")
    module.audit_event("CONTAINER_DESTROY", "id=y")

    assert blocker.is_file()
    assert module._sink_failed is True
    assert len(warnings) == 1


def test_env_override_wins_over_portable_default(monkeypatch, tmp_path):
    """``CONTAINER_AUDIT_LOG_PATH`` still takes precedence (back-compat)."""
    vault = tmp_path / "vault"
    override = tmp_path / "custom" / "audit.log"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    monkeypatch.setenv("CONTAINER_AUDIT_LOG_PATH", str(override))
    module = _load_module()

    module.audit_event("NETWORK_DECISION", "network=bridge")

    assert module._resolve_log_path() == str(override)
    assert override.is_file()
    assert override.read_text(encoding="utf-8").partition(" | ")[2] == (
        "NETWORK_DECISION | network=bridge\n"
    )
    assert not (vault / "logs" / DEFAULT_NAME).exists()


def test_blank_env_override_falls_back_to_default(monkeypatch, tmp_path):
    """An empty/whitespace override must not become a relative path in cwd."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    monkeypatch.setenv("CONTAINER_AUDIT_LOG_PATH", "   ")
    module = _load_module()

    assert Path(module._resolve_log_path()) == vault / "logs" / DEFAULT_NAME


def test_no_raw_open_of_hardcoded_tmp_path():
    """No production call site may open a hardcoded ``/tmp`` path any more.

    ``tools/docker_code_runner.py`` and ``thoughtmachine/security.py`` used to
    append audit lines with ``open("/tmp/container_audit.log", "a")``, which
    raises ``FileNotFoundError`` on Windows (no ``C:\\tmp``).  The regex only
    matches an ``open(`` call whose first argument is a quoted ``/tmp``/``\\tmp``
    literal, so prose mentioning ``/tmp`` in a docstring cannot false-positive.
    """
    pattern = re.compile(r"""open\(\s*["'`][^"'`]*[\\/]tmp[\\/]""")
    for _, rel_path in AUDITED_CALL_SITES:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert pattern.search(source) is None, f"{rel_path} still opens a hardcoded /tmp path"


def test_audit_writes_route_through_shared_audit_event():
    """Both call sites use the shared rotating sink instead of a local write."""
    sink = importlib.import_module("thoughtmachine.audit_logger").audit_event
    for module_name, rel_path in AUDITED_CALL_SITES:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "from thoughtmachine.audit_logger import audit_event" in source, rel_path
        assert importlib.import_module(module_name).audit_event is sink, module_name


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
