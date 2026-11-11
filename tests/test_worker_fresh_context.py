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

Hermetic: ``tmp_path`` only, no ``start()``, no network, no Docker, no LLM.

Run:  python -m pytest tests/test_worker_fresh_context.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.core.worker_context import WorkerContext  # noqa: E402
from tools.workspace.worker_runtime_process import spec_from_worker  # noqa: E402
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
