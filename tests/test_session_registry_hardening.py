"""Hardening guards for the global session registry (spec 40fb6b44ad27 §spec).

Three guards are pinned here:

1. ``SessionRegistry._load`` quarantines a registry file that EXISTS but is
   unusable (corrupt JSON, unreadable, or not a JSON object) into a sibling
   ``quarantine/`` directory instead of silently dropping its contents.
2. ``SessionRegistry.rebuild_from_disk`` refuses (raises) to overwrite a
   populated registry with an empty disk scan.
3. The read paths (``GET /api/session/list`` and the startup log in
   ``web_ui.backend.server``) no longer trigger an automatic rebuild.

Everything is confined to ``tmp_path``; the real vault is never touched.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from session.session_registry import SessionRegistry

LOGGER_NAME = "session.session_registry"


def _registry(tmp_path: Path) -> SessionRegistry:
    return SessionRegistry(path=tmp_path / "state" / "session_registry.json")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hermetic_home(monkeypatch, tmp_path: Path) -> Path:
    """Point the rebuild scan at an empty directory (no ~/.thoughtmachine)."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _quarantined(reg: SessionRegistry) -> list[Path]:
    return sorted((reg._path.parent / "quarantine").glob("session_registry.corrupt-*.json"))


def _write_session_file(home: Path, sid: str, workspace=None, **extra) -> Path:
    """Drop a session JSON file where ``rebuild_from_disk`` will scan for it."""
    data = {"session_id": sid, "metadata": {"name": sid}, **extra}
    if workspace is None:
        path = home / ".thoughtmachine" / "sessions" / f"{sid}.json"
    else:
        path = home / ".thoughtmachine" / "workspaces" / workspace / "sessions" / f"{sid}.json"
    _write(path, json.dumps(data))
    return path


def test_corrupt_registry_is_quarantined_and_load_returns_empty(tmp_path, caplog):
    """Guard 1: unparseable JSON is moved aside, its bytes preserved."""
    reg = _registry(tmp_path)
    raw = '{"s1": {"session_id": "s1"}, '  # truncated JSON
    _write(reg._path, raw)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert reg._load() == {}

    files = _quarantined(reg)
    assert len(files) == 1, files
    assert files[0].read_text(encoding="utf-8") == raw
    assert not reg._path.exists()
    assert files[0].name in caplog.text


def test_non_dict_payload_is_quarantined_and_load_returns_empty(tmp_path, caplog):
    """Guard 1: a valid-JSON non-object payload is quarantined too."""
    reg = _registry(tmp_path)
    raw = json.dumps([])
    _write(reg._path, raw)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert reg._load() == {}

    files = _quarantined(reg)
    assert len(files) == 1, files
    assert files[0].read_text(encoding="utf-8") == raw
    assert not reg._path.exists()


def test_missing_registry_returns_empty_without_quarantine(tmp_path, caplog):
    """Guard 1: a missing file is normal -- no error log, no artifact."""
    reg = _registry(tmp_path)
    assert not reg._path.exists()

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert reg._load() == {}

    assert not (reg._path.parent / "quarantine").exists()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_empty_scan_refuses_to_overwrite_populated_registry(tmp_path, monkeypatch):
    """Guard 2: 0 scanned entries + non-empty registry on disk => RuntimeError."""
    _hermetic_home(monkeypatch, tmp_path)
    reg = _registry(tmp_path)
    payload = {"s1": {"session_id": "s1"}, "s2": {"session_id": "s2"}}
    _write(reg._path, json.dumps(payload))
    before = reg._path.read_bytes()

    with pytest.raises(RuntimeError) as excinfo:
        reg.rebuild_from_disk()

    assert reg._path.read_bytes() == before
    message = str(excinfo.value)
    assert "2 entries" in message
    assert "data loss" in message


def test_empty_scan_over_missing_or_empty_registry_saves_and_returns_zero(
    tmp_path, monkeypatch
):
    """Guard 2: nothing to lose => behave as before (save, return 0)."""
    _hermetic_home(monkeypatch, tmp_path)
    reg = _registry(tmp_path)

    # Missing file.
    assert reg.rebuild_from_disk() == 0
    assert json.loads(reg._path.read_text(encoding="utf-8")) == {}

    # Explicitly empty file.
    _write(reg._path, "{}")
    assert reg.rebuild_from_disk() == 0
    assert json.loads(reg._path.read_text(encoding="utf-8")) == {}


def test_list_route_never_calls_rebuild_from_disk(monkeypatch):
    """Guard 3a: the read path must not rebuild (it would raise here)."""
    from web_ui.backend import session_routes

    class _Registry:
        def get_all(self):
            return {}

        def rebuild_from_disk(self):  # pragma: no cover - must never run
            raise AssertionError("list_sessions must not rebuild from disk")

    class _RegistryType:
        _instance = _Registry()

        @classmethod
        def get_default(cls):
            return cls._instance

    class _Store:
        def load_sessions_metadata_batch(self, session_ids, workspace_id=None):
            return {}

    monkeypatch.setattr(session_routes, "SessionRegistry", _RegistryType)
    monkeypatch.setattr(session_routes, "_get_store", lambda: _Store())

    assert asyncio.run(session_routes.list_sessions(workspace_id=None)) == []


def test_rebuild_preserves_open_flag_of_known_session(tmp_path, monkeypatch):
    """Fix 1: a session persisted as open must survive the rebuild as open."""
    home = _hermetic_home(monkeypatch, tmp_path)
    reg = _registry(tmp_path)
    _write_session_file(home, "s1")
    _write(
        reg._path,
        json.dumps({"s1": {"session_id": "s1", "workspace_id": "ws-a", "is_open": True}}),
    )

    assert reg.rebuild_from_disk() == 1

    saved = json.loads(reg._path.read_text(encoding="utf-8"))
    assert saved["s1"]["is_open"] is True


def test_rebuild_does_not_force_closed_session_open(tmp_path, monkeypatch):
    """Fix 1: the carry must not invent open sessions either."""
    home = _hermetic_home(monkeypatch, tmp_path)
    reg = _registry(tmp_path)
    _write_session_file(home, "s1")
    _write(
        reg._path,
        json.dumps({"s1": {"session_id": "s1", "workspace_id": "ws-a", "is_open": False}}),
    )

    assert reg.rebuild_from_disk() == 1

    saved = json.loads(reg._path.read_text(encoding="utf-8"))
    assert saved["s1"]["is_open"] is False


def test_rebuild_defaults_unknown_session_to_closed(tmp_path, monkeypatch):
    """Fix 1: sessions never seen before keep the scan default (closed)."""
    home = _hermetic_home(monkeypatch, tmp_path)
    reg = _registry(tmp_path)
    _write_session_file(home, "s1")
    _write(reg._path, json.dumps({"s1": {"session_id": "s1", "is_open": True}}))
    _write_session_file(home, "s2")

    assert reg.rebuild_from_disk() == 2

    saved = json.loads(reg._path.read_text(encoding="utf-8"))
    assert saved["s1"]["is_open"] is True
    assert saved["s2"]["is_open"] is False


def test_rebuild_carries_workspace_id_when_file_lacks_one(tmp_path, monkeypatch):
    """Fix 1b: the scan cannot infer a workspace, so inherit the persisted one."""
    home = _hermetic_home(monkeypatch, tmp_path)
    reg = _registry(tmp_path)
    _write_session_file(home, "s1")  # file records no workspace_id
    _write(
        reg._path,
        json.dumps({"s1": {"session_id": "s1", "workspace_id": "ws-a", "is_open": True}}),
    )

    assert reg.rebuild_from_disk() == 1

    saved = json.loads(reg._path.read_text(encoding="utf-8"))
    assert saved["s1"]["workspace_id"] == "ws-a"

