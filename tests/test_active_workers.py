"""Active worker instance handles vs. worker config templates.

Covers the runtime/template split from the live-thread side:

* ``GET /api/workspace/{ws_id}/workers/active`` returns per-instance runtime
  handles ``{worker_name, instance_id, status, elapsed, name, session_id,
  last_heartbeat, container_id, queued_query_count, queued_query_ids}`` and
  never leaks template fields; the optional ``?session_id=`` query param
  narrows to a single session.
* The same handles report the queries a worker has accepted but not yet
  dequeued (``queued_query_count``/``queued_query_ids``, FIFO order), sourced
  from ``WorkerThread.pending_query_jobs()`` and degrading to an empty queue
  for threads (or double) that cannot report one.
* ``GET /api/workspace/{ws_id}/workers`` returns the raw ``workers.json``
  template dicts and never merges in runtime/instance fields, even when
  live threads exist for the same workspace.

All workspace filesystem access is redirected to the pytest tmp_path.
"""

import asyncio
import json
import logging
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
            "queued_query_count", "queued_query_ids",
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


# ── queued (accepted-but-not-dequeued) query surface ─────────────────────────


def _real_thread(tmp_path, name="w-queue", instance_id=1, session_id="s1"):
    """An unstarted REAL WorkerThread — constructing one starts no thread."""
    from tools.workspace.worker import WorkerThread

    return WorkerThread(
        name=name,
        definition={"name": name},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=session_id,
        instance_id=instance_id,
    )


def _serve_threads(monkeypatch, threads):
    """Route ``active`` through *threads* for session s1 of workspace ws-1."""
    _patch_manager(
        monkeypatch, SimpleNamespace(list_workers=lambda sid: list(threads))
    )
    _patch_session_registry(monkeypatch, {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": True},
    })


def test_active_workers_surface_queued_query_count_and_ids_in_fifo_order(
        tmp_path, monkeypatch):
    """Queries a worker accepted but has not dequeued are visible per instance:
    ``queued_query_count`` plus the job ids in strict FIFO order."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    thread = _real_thread(tmp_path)
    thread.enqueue_query("job-a", "first query")
    thread.enqueue_query("job-b", "second query")
    _serve_threads(monkeypatch, [thread])

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert len(rows) == 1, rows
    entry = rows[0]
    assert entry["worker_name"] == "w-queue"
    assert entry["queued_query_count"] == 2
    assert entry["queued_query_ids"] == ["job-a", "job-b"]
    assert isinstance(entry["queued_query_ids"], list)


def test_active_workers_empty_queue_reports_zero(tmp_path, monkeypatch):
    """A worker with nothing queued reports an explicit zero / empty list."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    thread = _real_thread(tmp_path)
    _serve_threads(monkeypatch, [thread])

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert len(rows) == 1, rows
    entry = rows[0]
    assert entry["queued_query_count"] == 0
    assert entry["queued_query_ids"] == []


def test_active_workers_entry_without_pending_query_jobs_does_not_fail_route(
        tmp_path, monkeypatch, caplog):
    """Duck-typed thread doubles (missing / exploding / non-sequence getter)
    report an empty queue instead of failing the whole request — but an
    UNREADABLE queue (raise / non-sequence) is warned about, because a silent
    zero reads as an idle worker, while a genuinely absent getter is silent."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    caplog.set_level(logging.WARNING, logger=workspace_routes.logger.name)

    def _explode():
        raise RuntimeError("pending_query_jobs boom")

    no_attr = _thread("w1")
    exploding = _thread("w2")
    exploding.pending_query_jobs = _explode
    non_sequence = _thread("w3")
    non_sequence.pending_query_jobs = lambda: 42
    _serve_threads(monkeypatch, [no_attr, exploding, non_sequence])

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert [e["worker_name"] for e in rows] == ["w1", "w2", "w3"], rows
    for entry in rows:
        assert entry["queued_query_count"] == 0, entry
        assert entry["queued_query_ids"] == [], entry

    # The degraded payload is unchanged — the visibility comes from the log.
    queue_warnings = [
        record for record in caplog.records
        if record.name == workspace_routes.logger.name
        and record.levelno >= logging.WARNING
        and "queued" in record.getMessage().lower()
    ]

    def _warning_mentioning(worker_name):
        matches = [r for r in queue_warnings if worker_name in r.getMessage()]
        assert len(matches) == 1, [r.getMessage() for r in queue_warnings]
        return matches[0]

    # Raising getter: warned with the traceback attached.
    exploding_warning = _warning_mentioning("w2")
    assert "as 0" in exploding_warning.getMessage()
    assert exploding_warning.exc_info is not None, (
        "the raise path must log exc_info=True"
    )

    # Non-sequence getter: warned (no exception) and names the type received.
    non_sequence_warning = _warning_mentioning("w3")
    assert "as 0" in non_sequence_warning.getMessage()
    assert "int" in non_sequence_warning.getMessage(), (
        non_sequence_warning.getMessage()
    )
    assert non_sequence_warning.exc_info is None, (
        "the non-sequence path has no exception to attach"
    )

    # Missing attribute = genuinely no queue: must stay silent, or a future
    # "log everything" change turns the healthy empty case into noise.
    assert not any("w1" in r.getMessage() for r in queue_warnings), (
        [r.getMessage() for r in queue_warnings]
    )
    assert len(queue_warnings) == 2, [r.getMessage() for r in queue_warnings]


def test_active_workers_stopped_or_killed_worker_reports_empty_queue(
        tmp_path, monkeypatch):
    """Killing a worker rejects its pending queries, so a killed entry reports
    an empty queue rather than ids that can never run."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    thread = _real_thread(tmp_path)
    thread.enqueue_query("job-a", "queued then killed")
    assert thread.pending_query_jobs() == ["job-a"]

    # Keep the rejection path hermetic: no job registry, no global event bus,
    # no audit row (the rejection itself is covered by its own phase tests).
    reject_globals = type(thread).reject_queued_queries.__globals__
    monkeypatch.setitem(reject_globals, "_get_worker_job_registry", lambda: None)
    monkeypatch.setitem(
        reject_globals, "_publish_global_worker_event", lambda *a, **kw: None
    )
    import thoughtmachine.audit_logger as audit_logger

    monkeypatch.setattr(audit_logger, "audit_event", lambda *a, **kw: None,
                        raising=False)

    assert thread.reject_queued_queries("killed") == ["job-a"]
    thread.status = "killed"
    _serve_threads(monkeypatch, [thread])

    rows = asyncio.run(workspace_routes.get_active_workers("ws-1"))
    assert len(rows) == 1, rows
    entry = rows[0]
    assert entry["status"] == "killed"
    assert entry["queued_query_count"] == 0
    assert entry["queued_query_ids"] == []
