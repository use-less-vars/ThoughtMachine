"""Tests: 'running_workers' is scoped to the calling session (fail-closed).

Covers the leak fixed in CheckSystem._query_running_workers: it iterates the
PROCESS-GLOBAL worker registry (session_id, worker_name, instance_id), so
without a session filter one session's worker names and verbatim current_task
strings showed up in another session's payload. The query now filters on
``self.session_id or ""`` and fails closed (no session => no entries).

Harness mirrors tests/test_check_system_runtime.py (allowlist pinned via
patching, no live vault/Docker daemon needed). The registry globals are
patched on the LIVE module object rather than the singleton, because other
suites purge/replace tools.workspace.check_system in sys.modules and a
collection-time import can point at a stale module whose globals a
string-based patch no longer reaches.
"""

import json
import threading
from unittest.mock import MagicMock, patch

MINE = "session-mine"
FOREIGN = "session-foreign"

BASE_KWARGS = {
    "session_permissions": {"filesystem": "read", "network": "write"},
}


def _fake_thread(name: str, current_task: str) -> MagicMock:
    """Thread stub exposing exactly the attributes _query_running_workers reads."""
    t = MagicMock()
    t.worker_name = name
    t.status = "running"
    t.is_alive.return_value = True
    t.current_task = current_task
    t.last_heartbeat = "2026-10-20T00:00:00Z"
    t.error = None
    t._worker_ctx = None
    t._last_elapsed.return_value = None
    return t


def _seed_registry() -> dict:
    """Two workers of DIFFERENT sessions in one process-global registry dict."""
    return {
        (MINE, "my-worker", 1): _fake_thread("my-worker", "my private task"),
        (FOREIGN, "foreign-worker", 1): _fake_thread(
            "foreign-worker", "FOREIGN SECRET TASK"
        ),
    }


def _run_query(cs_module, **kwargs) -> dict:
    with patch.object(
        cs_module.CheckSystem,
        "_load_allowlist_from_vault",
        return_value=["running_workers"],
    ):
        tool = cs_module.CheckSystem(query="running_workers", **BASE_KWARGS, **kwargs)
        return json.loads(tool.execute())


def test_running_workers_scoped_to_calling_session():
    """Only the calling session's workers appear; foreign data never leaks."""
    import tools.workspace.check_system as cs_module

    with patch.object(cs_module, "_worker_registry", _seed_registry()), patch.object(
        cs_module, "_registry_lock", threading.Lock()
    ), patch.object(cs_module, "WORKER_REGISTRY_AVAILABLE", True):
        result = _run_query(cs_module, session_id=MINE)

    workers = result["running_workers"]
    assert len(workers) == 1
    assert workers[0]["name"] == "my-worker"
    assert workers[0]["session_id"] == MINE
    assert workers[0]["current_task"] == "my private task"
    # The foreign worker's name and task never reach the payload.
    assert "FOREIGN SECRET TASK" not in json.dumps(result)
    assert "foreign-worker" not in json.dumps(result)
    assert result["count"] == 1


def test_running_workers_fail_closed_without_session():
    """Deliberate fail-closed contract: no session => empty list, not global view."""
    import tools.workspace.check_system as cs_module

    # ``session_id=None`` makes the filter key "", which only matches
    # session-less registrations — so a caller that lost its identity sees
    # NOTHING rather than everyone else's workers.
    with patch.object(cs_module, "_worker_registry", _seed_registry()), patch.object(
        cs_module, "_registry_lock", threading.Lock()
    ), patch.object(cs_module, "WORKER_REGISTRY_AVAILABLE", True):
        result = _run_query(cs_module, session_id=None)

    assert result["running_workers"] == []
    assert result["count"] == 0
