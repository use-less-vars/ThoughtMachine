"""Opt-in blank-slate worker spawn (``fresh_context``).

Pins the spawn contract in ``tools/workspace/worker_thread.py``:

  * ``fresh_context=False`` (the default) resumes the persisted
    ``workers/<label>/context.json`` transcript -- today's behaviour, untouched.
  * ``fresh_context=True`` refuses to load that transcript, so ``run()`` seeds a
    blank history (the worker's own system prompt plus this spawn's
    ``Initial context:`` brief only) and the next ``_save_context()`` overwrites
    the stale file.

The flag is opt-in everywhere it has to cross -- the ``Worker`` tool field, the
``WorkerThread`` constructor, and the process-mode runtime spec -- and no path
may drop a conversation without it.

At the spawn decision point the flag also IMPLIES ``force``: a blank-slate
spawn may neither reuse a live instance nor auto-resume a paused one, because
either would hand the caller back the conversation it asked not to have.

Hermetic: ``tmp_path`` only, no ``start()``, no network, no Docker, no LLM.

Run:  python -m pytest tests/test_worker_fresh_context.py -q
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.core.worker_context import WorkerContext  # noqa: E402
from tools.workspace.worker_runtime_process import spec_from_worker  # noqa: E402
from tools.workspace.worker_registry import WorkerRegistry  # noqa: E402
from tools.workspace.worker_thread import Worker, WorkerThread  # noqa: E402

SYS_PROMPT = "You are a test worker."
WORKER_NAME = "w-fresh"
SESSION_ID = "sess-fresh"
PRIOR_TASK = "PRIOR-TASK-SENTINEL-3f9a: report DONE"
NEW_TASK = "NEW-TASK-SENTINEL-7c1b: report DONE"


def _make_thread(tmp_path, *, fresh_context=False, name=WORKER_NAME):
    """A REAL ``WorkerThread`` (never ``start()``ed), built the production way."""
    return WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": SYS_PROMPT},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=SESSION_ID,
        fresh_context=fresh_context,
    )


def _persist_prior_transcript(tmp_path):
    """Write a realistic prior spawn (brief + one finished turn) to context.json."""
    prior = _make_thread(tmp_path)
    prior._worker_ctx = WorkerContext(
        session_id=SESSION_ID,
        worker_name=WORKER_NAME,
        user_history=[
            {"role": "system", "content": SYS_PROMPT},
            {
                "role": "system",
                "content": f"Initial context: {json.dumps({'query': PRIOR_TASK})}",
            },
            {"role": "user", "content": PRIOR_TASK},
            {"role": "assistant", "content": "DONE (prior task)"},
        ],
    )
    prior._save_context()
    persisted = json.loads(
        prior._context_path().read_text(encoding="utf-8")
    )["conversation"]
    assert len(persisted) == 4, "the prior transcript must be real, not empty"
    return persisted


# ── 1. default (fresh_context=False) is a byte-for-byte resume ──────────────


def test_default_spawn_resumes_the_persisted_transcript(tmp_path):
    persisted = _persist_prior_transcript(tmp_path)

    resumed = _make_thread(tmp_path)
    assert resumed.fresh_context is False, "resume must stay the default"

    ctx = resumed._load_context()
    assert ctx is not None, "a default spawn must load context.json"
    assert ctx.user_history == persisted, "the transcript must load unchanged"
    assert ctx.user_history[-1]["content"] == "DONE (prior task)"


# ── 2. opt-in (fresh_context=True) refuses it and overwrites at save ───────


def test_fresh_spawn_loads_nothing_and_overwrites_the_stale_transcript(tmp_path):
    persisted = _persist_prior_transcript(tmp_path)

    fresh = _make_thread(tmp_path, fresh_context=True)
    assert fresh._load_context() is None, "fresh_context must not load the file"
    assert fresh.status == "ready", "no stale status may be restored either"

    fresh._initial_context = {"query": NEW_TASK}
    fresh._worker_ctx = WorkerContext(
        session_id=SESSION_ID,
        workspace_id=fresh.workspace_id,
        worker_name=WORKER_NAME,
        user_history=fresh._seed_user_history(),
    )
    fresh._save_context()

    conv = json.loads(
        fresh._context_path().read_text(encoding="utf-8")
    )["conversation"]
    assert conv == [
        {"role": "system", "content": SYS_PROMPT},
        {
            "role": "system",
            "content": f"Initial context: {json.dumps({'query': NEW_TASK})}",
        },
    ], "a fresh spawn persists only its own prompt + brief"
    assert PRIOR_TASK not in json.dumps(conv), "the old task must be gone"
    assert len(conv) < len(persisted)

    # The opt-in is one spawn long: a later DEFAULT spawn resumes the
    # overwritten file, so the flag never doubles as a persistent reset.
    later_ctx = _make_thread(tmp_path)._load_context()
    assert later_ctx is not None
    assert later_ctx.user_history == conv
    assert PRIOR_TASK not in json.dumps(later_ctx.user_history)


# ── 3. seed shape ─────────────────────────────────────────────────────────


def test_seed_history_is_prompt_plus_this_spawns_brief(tmp_path):
    thread = _make_thread(tmp_path)
    assert thread._seed_user_history() == [
        {"role": "system", "content": SYS_PROMPT}
    ], "no brief in, no brief out"

    thread._initial_context = {"query": NEW_TASK}
    seeded = thread._seed_user_history()
    assert len(seeded) == 2
    assert seeded[0] == {"role": "system", "content": SYS_PROMPT}
    assert seeded[1]["role"] == "system"
    assert seeded[1]["content"] == (
        f"Initial context: {json.dumps(thread._initial_context)}"
    )
    assert PRIOR_TASK not in json.dumps(seeded)


# ── 4. the flag is opt-in at every boundary it has to cross ────────────────


def test_fresh_context_is_opt_in_on_the_tool_and_the_thread(tmp_path):
    assert Worker(action="spawn", worker_name=WORKER_NAME).fresh_context is False
    assert Worker(
        action="spawn", worker_name=WORKER_NAME, fresh_context=True
    ).fresh_context is True
    assert _make_thread(tmp_path).fresh_context is False
    assert _make_thread(tmp_path, fresh_context=True).fresh_context is True


def test_process_mode_rebuild_carries_the_blank_slate_flag(tmp_path):
    """A process-mode rebuild is the same spawn: the flag must cross with it."""
    fresh_kwargs = spec_from_worker(
        _make_thread(tmp_path, fresh_context=True)
    )["worker_kwargs"]
    assert fresh_kwargs["fresh_context"] is True

    resume_kwargs = spec_from_worker(_make_thread(tmp_path))["worker_kwargs"]
    assert resume_kwargs["fresh_context"] is False

# ── 5. decision point: fresh_context implies force ───────────────────────────
#
# The flag has to be honoured where the spawn actually decides reuse vs.
# replace, not only in the plumbing pinned above.  These cases drive
# ``_action_spawn`` with a REAL ``WorkerRegistry`` seeded with a live instance,
# so a reuse, an auto-resume or a replacement is observed as a registry effect
# (``kill`` / ``resume`` / a newly constructed ``WorkerThread``) instead of
# being inferred from the returned envelope alone.


def _live_instance(iid=1, status="ready"):
    """A registered instance that ``_action_spawn`` may reuse, resume or kill."""
    live = MagicMock()
    live.is_alive.return_value = True
    live.status = status
    live.instance_id = iid
    live.instance_label = WorkerRegistry.instance_label(WORKER_NAME, iid)
    live._manual_only_pause = False
    live._timeout_seconds = 30
    live._worker_ctx = None
    live._worker_dir = MagicMock()
    live._generation = 1
    live._stop_event = threading.Event()
    live._input_queue = queue.Queue()
    live._output_queue = MagicMock()
    live._output_queue.get.return_value = json.dumps(
        {"response": "stale run", "status": "completed"}
    )
    live._last_elapsed = MagicMock(return_value=None)
    live.send_query.return_value = "stale run"

    def _kill(*_args, **_kwargs):
        live.status = "killed"
        live.is_alive.return_value = False

    live.kill.side_effect = _kill
    return live


@contextmanager
def _spawn_harness(*live_instances):
    """Seed the registry with *live_instances* and patch every spawn edge.

    Yields the list of ``(thread, ctor_kwargs)`` this spawn actually created,
    which stays empty whenever the call reused / resumed instead of spawning.
    """
    registry = WorkerRegistry.get_instance()
    with registry._registry_lock:
        saved_registry = dict(registry._worker_registry)
        registry._worker_registry.clear()
    with registry._bus_registry_lock:
        saved_buses = dict(registry._worker_event_bus_registry)
        registry._worker_event_bus_registry.clear()

    created = []

    def _factory(*_args, **kwargs):
        thread = MagicMock()
        thread.instance_id = kwargs.get("instance_id", 1)
        thread.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", WORKER_NAME), thread.instance_id
        )
        thread.status = "ready"
        thread._timeout_seconds = 30
        thread._stop_event = threading.Event()
        thread._worker_ctx = None
        thread._worker_dir = MagicMock()
        thread._output_queue = MagicMock()
        thread._output_queue.get.return_value = json.dumps(
            {"response": "fresh run", "status": "completed"}
        )
        thread._last_elapsed = MagicMock(return_value=None)
        created.append((thread, kwargs))
        return thread

    mock_ws_dir = MagicMock()
    workers_json = MagicMock()
    workers_json.exists.return_value = True
    workers_json.read_text.return_value = json.dumps(
        [{"name": WORKER_NAME, "status": "ready"}]
    )
    mock_ws_dir.__truediv__.return_value = workers_json

    try:
        with registry._registry_lock:
            for live in live_instances:
                registry._worker_registry[
                    (SESSION_ID, WORKER_NAME, live.instance_id)
                ] = live
        with mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value="ws_fresh"
        ), mock.patch(
            "tools.workspace.worker._workspace_dir", return_value=mock_ws_dir
        ), mock.patch(
            "tools.workspace.worker.WorkerThread", side_effect=_factory
        ):
            yield created
    finally:
        with registry._registry_lock:
            registry._worker_registry.clear()
            registry._worker_registry.update(saved_registry)
        with registry._bus_registry_lock:
            registry._worker_event_bus_registry.clear()
            registry._worker_event_bus_registry.update(saved_buses)


def _run_spawn(**flags):
    """Execute a real ``Worker(action="spawn")`` against the harness."""
    tool = Worker(
        action="spawn",
        worker_name=WORKER_NAME,
        session_id=SESSION_ID,
        workspace_path="/tmp/test_ws",
        agent_config={"provider": "openai", "model": "gpt-4"},
        context={"query": NEW_TASK},
        **flags,
    )
    return json.loads(tool.execute())


def test_default_spawn_still_reuses_a_live_instance():
    """Control: without the flag, a live instance is continued, not replaced."""
    live = _live_instance()

    with _spawn_harness(live) as created:
        result = _run_spawn()

    assert result["spawned"] is False, result
    assert result["response"] == "stale run", "the query must go to the live instance"
    assert result["instance_id"] == 1, result
    live.send_query.assert_called_once_with(NEW_TASK, timeout=mock.ANY)
    assert created == [], "a reuse must not construct a second WorkerThread"
    live.kill.assert_not_called()


def test_fresh_context_replaces_a_live_instance_instead_of_reusing_it():
    """fresh_context=True over a live instance: kill it, then spawn fresh."""
    live = _live_instance()

    with _spawn_harness(live) as created:
        result = _run_spawn(fresh_context=True)

    live.kill.assert_called_once()
    live.send_query.assert_not_called()
    assert result["spawned"] is True, result
    assert "reusing" not in json.dumps(result), result
    assert result["response"] == "fresh run", result
    assert len(created) == 1, created
    assert created[0][1]["fresh_context"] is True, (
        "the replacement must itself be a blank-slate spawn"
    )


def test_default_spawn_still_auto_resumes_a_paused_instance():
    """Control: without the flag, a paused instance is resumed (not replaced)."""
    paused = _live_instance(status="paused")

    with _spawn_harness(paused) as created:
        _ = _run_spawn()

    paused.resume.assert_called_once()
    assert created == [], "a resume must not construct a second WorkerThread"


def test_fresh_context_does_not_auto_resume_a_paused_instance():
    """fresh_context=True over a paused instance: never resume the old run."""
    paused = _live_instance(status="paused")

    with _spawn_harness(paused) as created:
        result = _run_spawn(fresh_context=True)

    paused.resume.assert_not_called()
    assert result.get("status") != "paused", result
    assert result["spawned"] is True, result
    assert len(created) == 1, created
    assert created[0][1]["fresh_context"] is True

