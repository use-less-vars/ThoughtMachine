"""Worker template + active-thread backend tests.

Covers the split between worker config templates and live runtime state:

* ``GET /api/workspace/{ws_id}/workers`` returns the raw config templates
  only (one row per ``workers.json`` entry, no runtime/instance fields);
  the ``?name=`` filter narrows to a single template and 404s on unknown.
* ``GET /api/workspace/{ws_id}/workers/active`` returns compact runtime
  handles ``{worker_name, instance_id, status, elapsed, name, session_id,
  last_heartbeat, container_id}`` for the live threads of the workspace's
  open sessions (falling back to a full-registry scan when no open session
  maps to the workspace); the optional ``?session_id=`` query param narrows
  the result to the threads of a single session.

All workspace filesystem access is redirected to the pytest tmp_path.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from web_ui.backend import workspace_routes


# ── helpers ───────────────────────────────────────────────────────────────────


def _use_tmp_workspace(monkeypatch, tmp_path):
    """Point workspace_routes at the pytest tmp_path."""
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _patch_session_registry(monkeypatch, sessions):
    """Patch ``SessionRegistry.get_default`` (imported lazily inside
    workspace_routes at call time, so patching the module attr is enough)."""
    import session.session_registry as sreg

    fake = SimpleNamespace(get_all=lambda: sessions)
    monkeypatch.setattr(
        sreg.SessionRegistry, "get_default", classmethod(lambda cls: fake)
    )


def _thread(worker_name, instance_id=1, status="ready", started_at=None, session_id="s1"):
    return SimpleNamespace(
        worker_name=worker_name,
        instance_id=instance_id,
        status=status,
        started_at=started_at,
        session_id=session_id,
    )


# ── GET /api/workspace/{ws_id}/workers (templates only) ──────────────────────


def test_get_workers_returns_templates_only(tmp_path, monkeypatch):
    """workers.json entries are returned raw: dicts pass through, legacy
    bare strings become ``{"name": ...}``, and no runtime/instance fields
    are merged in even when per-instance status dirs exist on disk."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "workers.json", [
        {"name": "w1", "system_prompt": "p1"},
        {"name": "w2", "system_prompt": "p2", "tools": ["file_read"]},
        "legacy-w",
    ])
    # On-disk runtime state that the old merged endpoint would fold in -
    # it must be ignored entirely by the templates-only endpoint.
    _write_json(tmp_path / "workers" / "w1" / "status.json", {
        "runtime_status": "busy",
        "current_context_tokens": 123,
        "max_context_tokens": 456,
    })
    _write_json(tmp_path / "workers" / "w1" / "context.json", {
        "pruned_since_last_query": 2,
    })
    _write_json(tmp_path / "workers" / "w2#2" / "status.json", {
        "runtime_status": "busy",
        "current_task": "t2",
    })

    rows = asyncio.run(workspace_routes.get_workers("ws-1"))
    assert rows == [
        {"name": "w1", "system_prompt": "p1"},
        {"name": "w2", "system_prompt": "p2", "tools": ["file_read"]},
        {"name": "legacy-w"},
    ]
    for row in rows:
        assert "instance_id" not in row
        assert "instance_label" not in row
        assert "runtime_status" not in row
        assert "current_context_tokens" not in row
        assert "has_persisted_context" not in row


def test_get_workers_missing_or_broken_config(tmp_path, monkeypatch):
    """No workers.json (or unparseable JSON) -> empty list, no error."""
    _use_tmp_workspace(monkeypatch, tmp_path)

    assert asyncio.run(workspace_routes.get_workers("ws-1")) == []

    # Write raw broken text (NOT via _write_json, which JSON-encodes its
    # payload and would turn "not json" into a parseable JSON string).
    (tmp_path / "workers.json").write_text("not json", encoding="utf-8")
    assert asyncio.run(workspace_routes.get_workers("ws-1")) == []


def test_get_workers_name_filter_and_404(tmp_path, monkeypatch):
    """?name= returns the single raw template dict; unknown names 404."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "workers.json", [
        {"name": "w1", "system_prompt": "p1"},
        {"name": "w2", "system_prompt": "p2"},
    ])

    entry = asyncio.run(workspace_routes.get_workers("ws-1", name="w1"))
    assert entry == {"name": "w1", "system_prompt": "p1"}
    assert "instance_id" not in entry
    assert "instance_label" not in entry

    with pytest.raises(HTTPException) as exc:
        asyncio.run(workspace_routes.get_workers("ws-1", name="nope"))
    assert exc.value.status_code == 404
    assert "nope" in str(exc.value.detail)


# ── GET /api/workspace/{ws_id}/workers/active ────────────────────────────────


def test_get_active_workers_session_scoped(tmp_path, monkeypatch):
    """Active workers come from the open sessions of this workspace only:
    each handle carries exactly {worker_name, instance_id, status, elapsed,
    name, session_id, last_heartbeat, container_id}, duplicates are deduped,
    and the rows are sorted by name then instance."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

    manager = SimpleNamespace(
        list_workers=lambda sid: [
            _thread("w2", instance_id=2, status="busy", started_at=started),
            _thread("w1", instance_id=1, status="ready", started_at=started),
            _thread("w2", instance_id=2, status="busy", started_at=started),  # dup
            _thread("w3", instance_id=1, status="error", started_at=None),
        ],
        _registry=SimpleNamespace(get_all_workers=lambda: {}),
    )
    monkeypatch.setattr(workspace_routes, "_get_worker_manager", lambda: manager)
    _patch_session_registry(monkeypatch, {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": True},
        "s2": {"session_id": "s2", "workspace_id": "ws-1", "is_open": True},
        "s9": {"session_id": "s9", "workspace_id": "ws-other", "is_open": True},
    })

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert len(rows) == 3, f"expected 3 rows (deduped), got {rows}"

    for entry in rows:
        assert set(entry.keys()) == {
            "worker_name", "instance_id", "status", "elapsed",
            "name", "session_id", "last_heartbeat", "container_id",
        }, entry

    by_key = {(e["worker_name"], e["instance_id"]): e for e in rows}
    assert by_key[("w1", 1)]["name"] == "w1"
    assert by_key[("w1", 1)]["session_id"] == "s1"
    assert by_key[("w1", 1)]["last_heartbeat"] is None
    assert by_key[("w1", 1)]["container_id"] is None
    assert by_key[("w1", 1)]["status"] == "ready"
    assert abs(by_key[("w1", 1)]["elapsed"] - 10.0) < 3.0
    assert by_key[("w2", 2)]["status"] == "busy"
    assert abs(by_key[("w2", 2)]["elapsed"] - 10.0) < 3.0
    assert by_key[("w3", 1)]["status"] == "error"
    assert by_key[("w3", 1)]["elapsed"] is None

    assert [e["worker_name"] for e in rows] == ["w1", "w2", "w3"]


def test_get_active_workers_fallback_all_registry(tmp_path, monkeypatch):
    """No open session maps to the workspace -> fall back to scanning the
    whole registry, keeping threads whose session maps to this workspace
    (or whose session id is empty) and skipping other workspaces' threads."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    manager = SimpleNamespace(
        list_workers=lambda sid: [],
        _registry=SimpleNamespace(get_all_workers=lambda: {
            ("s1", "w1", 1): _thread("w1", instance_id=1, status="ready",
                                     started_at=started, session_id="s1"),
            ("s1", "w2", 2): _thread("w2", instance_id=2, status="paused",
                                     started_at=started, session_id="s1"),
            ("s9", "w9", 1): _thread("w9", instance_id=1, status="ready",
                                     started_at=started, session_id="s9"),
            ("", "orphan", 1): _thread("orphan", instance_id=1, status="ready",
                                       started_at=started, session_id=""),
        }),
    )
    monkeypatch.setattr(workspace_routes, "_get_worker_manager", lambda: manager)
    # Session s1 exists for this workspace but is CLOSED -> session path is
    # skipped (is_open False) and the fallback keeps s1's workers.
    _patch_session_registry(monkeypatch, {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": False},
        "s9": {"session_id": "s9", "workspace_id": "ws-other", "is_open": True},
    })

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert {e["worker_name"] for e in rows} == {"w1", "w2", "orphan"}, rows
    for entry in rows:
        assert set(entry.keys()) == {
            "worker_name", "instance_id", "status", "elapsed",
            "name", "session_id", "last_heartbeat", "container_id",
        }, entry


def test_get_active_workers_no_manager(tmp_path, monkeypatch):
    """WorkerManager unavailable -> empty list, never an exception."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(workspace_routes, "_get_worker_manager", None)

    assert asyncio.run(workspace_routes.get_active_workers("ws-1")) == []


def test_worker_elapsed_seconds_unit():
    """_worker_elapsed_seconds parses ISO timestamps (with/without tz and
    the Z suffix), never goes negative, and returns None on garbage."""
    recent = datetime.now(timezone.utc) - timedelta(seconds=3)

    with_z = workspace_routes._worker_elapsed_seconds(
        recent.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    assert with_z is not None and abs(with_z - 3.0) < 2.0

    naive = recent.replace(tzinfo=None).isoformat()
    assert abs(workspace_routes._worker_elapsed_seconds(naive) - 3.0) < 2.0

    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    assert workspace_routes._worker_elapsed_seconds(future) == 0.0

    assert workspace_routes._worker_elapsed_seconds(None) is None
    assert workspace_routes._worker_elapsed_seconds("") is None
    assert workspace_routes._worker_elapsed_seconds("garbage") is None
