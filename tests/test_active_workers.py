"""Active worker instance handles vs. worker config templates.

Covers the runtime/template split from the live-thread side:

* ``GET /api/workspace/{ws_id}/workers/active`` returns per-instance runtime
  handles ``{worker_name, instance_id, status, elapsed, name, session_id,
  last_heartbeat, container_id}`` and never leaks template fields; the
  optional ``?session_id=`` query param narrows to a single session.
* ``GET /api/workspace/{ws_id}/workers`` returns the raw ``workers.json``
  template dicts and never merges in runtime/instance fields, even when
  live threads exist for the same workspace.

All workspace filesystem access is redirected to the pytest tmp_path.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Query

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


def _patch_manager(monkeypatch, manager):
    monkeypatch.setattr(workspace_routes, "_get_worker_manager", lambda: manager)


def _thread(worker_name, instance_id=1, status="ready", started_at=None,
            session_id="s1", last_heartbeat=None, container_id=None):
    return SimpleNamespace(
        worker_name=worker_name,
        instance_id=instance_id,
        status=status,
        started_at=started_at,
        session_id=session_id,
        last_heartbeat=last_heartbeat,
        container_id=container_id,
    )


# ── GET /api/workspace/{ws_id}/workers/active ─────────────────────────────────


def test_active_workers_returns_instances_not_templates(tmp_path, monkeypatch):
    """Live threads are returned as runtime handles: identity + session +
    heartbeat/container fields only, with template fields from workers.json
    never merged in."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    _write_json(tmp_path / "workers.json", [
        {
            "name": "w1",
            "system_prompt": "p1",
            "tool_classes": ["file_ops"],
            "tools": ["file_read"],
        },
        {
            "name": "w2",
            "system_prompt": "p2",
            "instance_label": "w2",
            "runtime_status": "busy",
        },
    ])

    manager = SimpleNamespace(list_workers=lambda sid: [
        _thread("w1", instance_id=1, status="ready", started_at=started,
                last_heartbeat=started, container_id="c1"),
        _thread("w2", instance_id=1, status="busy", started_at=None),
    ])
    _patch_manager(monkeypatch, manager)
    _patch_session_registry(monkeypatch, {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": True},
    })

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert [e["worker_name"] for e in rows] == ["w1", "w2"]

    for entry in rows:
        assert set(entry.keys()) == {
            "worker_name", "instance_id", "status", "elapsed",
            "name", "session_id", "last_heartbeat", "container_id",
        }, entry
        for template_key in ("system_prompt", "tool_classes", "tools",
                             "instance_label", "runtime_status"):
            assert template_key not in entry

    by_name = {e["worker_name"]: e for e in rows}
    assert by_name["w1"]["name"] == "w1"
    assert by_name["w1"]["session_id"] == "s1"
    assert by_name["w1"]["last_heartbeat"] == started
    assert by_name["w1"]["container_id"] == "c1"
    assert by_name["w1"]["elapsed"] is not None

    # Bare thread: no heartbeat yet, no attributable container.
    assert by_name["w2"]["last_heartbeat"] is None
    assert by_name["w2"]["container_id"] is None
    assert by_name["w2"]["elapsed"] is None


def test_workers_templates_endpoint_does_not_return_active_instances(
        tmp_path, monkeypatch):
    """The templates endpoint stays template-only: raw workers.json dicts,
    with no runtime/instance fields even when live threads exist."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    _write_json(tmp_path / "workers.json", [
        {"name": "w1", "system_prompt": "p1", "tools": ["file_read"]},
        {"name": "w2", "system_prompt": "p2"},
    ])

    manager = SimpleNamespace(list_workers=lambda sid: [
        _thread("w1", instance_id=1, status="ready", started_at=started,
                last_heartbeat=started, container_id="c1"),
        _thread("w2", instance_id=1, status="busy", started_at=None),
    ])
    _patch_manager(monkeypatch, manager)
    _patch_session_registry(monkeypatch, {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": True},
    })

    rows = asyncio.run(workspace_routes.get_workers("ws-1"))
    assert rows == [
        {"name": "w1", "system_prompt": "p1", "tools": ["file_read"]},
        {"name": "w2", "system_prompt": "p2"},
    ]
    for row in rows:
        for runtime_key in ("session_id", "status", "last_heartbeat",
                            "container_id", "instance_id", "elapsed"):
            assert runtime_key not in row


def test_active_workers_filter_by_session(tmp_path, monkeypatch):
    """?session_id= narrows to one session's threads; absent or Query(None)
    returns every open session's threads."""
    _use_tmp_workspace(monkeypatch, tmp_path)

    manager = SimpleNamespace(list_workers=lambda sid: {
        "s1": [_thread("w1", instance_id=1, status="ready", session_id="s1")],
        "s2": [
            _thread("w2", instance_id=1, status="ready", session_id="s2"),
            _thread("w3", instance_id=1, status="busy", session_id="s2"),
        ],
    }.get(sid, []))
    _patch_manager(monkeypatch, manager)
    _patch_session_registry(monkeypatch, {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": True},
        "s2": {"session_id": "s2", "workspace_id": "ws-1", "is_open": True},
    })

    only_s2 = asyncio.run(
        workspace_routes.get_active_workers("ws-1", session_id="s2")
    )
    assert [e["worker_name"] for e in only_s2] == ["w2", "w3"]
    assert {e["session_id"] for e in only_s2} == {"s2"}

    unfiltered = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert {e["worker_name"] for e in unfiltered} == {"w1", "w2", "w3"}
    assert {e["session_id"] for e in unfiltered} == {"s1", "s2"}

    # FastAPI binds Query(None) defaults only while serving HTTP; a direct
    # call receives the sentinel and must behave exactly like None.
    via_query_sentinel = asyncio.run(
        workspace_routes.get_active_workers("ws-1", session_id=Query(None))
    )
    assert via_query_sentinel == unfiltered
