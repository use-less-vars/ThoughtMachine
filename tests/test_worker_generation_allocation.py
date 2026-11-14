"""W8 — worker generation (epoch) allocation: single-lock RMW + fail-closed.

Targets ``WorkerThread._allocate_generation`` in
``tools/workspace/worker_thread.py``. Each WorkerThread instance owns a
monotonic per-worker epoch (sidecar ``worker_dir/generation``, folded together
with any ``generation`` already persisted in ``context.json``) so a still-alive
stale thread's ``_save_context()`` can be rejected once a replacement has
persisted a higher one.

What is pinned here:

  1. CONCURRENCY — the whole read-modify-write runs under ONE lock, so 8
     threads x 5 spawns against a shared worker_dir produce the epochs 1..40
     with no duplicates. Two spawners that read the counter before taking the
     lock would both persist the same epoch (the pre-W8 race).
  2. MONOTONICITY — a later instance always gets ``>`` the persisted epoch,
     from the sidecar and from a context.json-only counter (sidecar lost).
  3. FAIL-CLOSED on a sidecar write failure — the allocation raises and the
     sidecar keeps its old value (no phantom increment, no leftover temp file).
  4. FAIL-CLOSED on a lock timeout — ``FileLockTimeoutError`` propagates
     (nothing swallows it); it is not an ``OSError``.
  5. Caller level — both call sites (``__init__``/spawn and ``reset()``) let
     the failure through instead of handing out an unpersisted epoch.

Hermetic: ``tmp_path`` only, no sleeps, no network, no Docker, no LLM.

Run:  python -m pytest tests/test_worker_generation_allocation.py -q
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from session.lock import FileLockTimeoutError  # noqa: E402
from tools.workspace import worker_thread as worker_thread_mod  # noqa: E402
from tools.workspace.worker_registry import WorkerRegistry  # noqa: E402
from tools.workspace.worker_thread import WorkerThread  # noqa: E402

SYS_PROMPT = "You are a test worker."
WORKER_NAME = "w-gen"
SESSION_ID = "sess-generation"


def _make_thread(tmp_path, name=WORKER_NAME, session_id=SESSION_ID):
    """A REAL ``WorkerThread`` (never ``start()``ed) whose epoch is already
    allocated by ``__init__`` — the production spawn path."""
    return WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": SYS_PROMPT},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=session_id,
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def _worker_dir(tmp_path, name=WORKER_NAME, session_id=SESSION_ID) -> Path:
    """The worker directory this workspace/session/name really resolves to.

    Delegates to the production builder ``WorkerRegistry.worker_dir``
    (``workers/<session_id>/<instance_label>``) rather than re-deriving the
    layout here: the private copy this replaced kept pointing at the legacy
    ``workers/<label>`` path after production grew the session segment, so
    every assertion looked at a directory the worker never used.
    """
    return WorkerRegistry.worker_dir(tmp_path, session_id, name, 1)


def _sidecar(tmp_path, name=WORKER_NAME) -> Path:
    return _worker_dir(tmp_path, name) / "generation"


def _sidecar_value(tmp_path, name=WORKER_NAME) -> int:
    return int(_sidecar(tmp_path, name).read_text(encoding="utf-8").strip())


def _fail_generation_replace(monkeypatch, exc_factory):
    """Make ``os.replace`` fail ONLY for the generation sidecar.

    Every other atomic write (context.json / status.json use the same
    ``os.replace`` in this module) keeps working, so a caller-level test fails
    for the intended reason and not because status persistence broke.
    """
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.basename(str(dst)) == "generation":
            raise exc_factory()
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(worker_thread_mod.os, "replace", failing_replace)


# ── 1. concurrency ──────────────────────────────────────────────────────────


def test_concurrent_spawns_allocate_unique_contiguous_epochs(tmp_path):
    """8 threads x 5 spawns on ONE shared worker_dir, sidecar starting absent.

    Each epoch must be observed persisted before it is returned, so the 40
    results are exactly 1..40 and the sidecar ends at 40. A read taken outside
    the lock yields duplicates here (see the module docstring).
    """
    worker_dir = _worker_dir(tmp_path)
    worker_dir.mkdir(parents=True, exist_ok=True)
    assert not _sidecar(tmp_path).exists(), "the sidecar must start absent"

    n_threads, per_thread = 8, 5
    epochs: list = []
    errors: list = []
    collect_lock = threading.Lock()
    start_together = threading.Barrier(n_threads)

    def spawn_repeatedly():
        try:
            start_together.wait()
            mine = [_make_thread(tmp_path)._generation for _ in range(per_thread)]
        except BaseException as exc:  # surfaced by the assertion below
            with collect_lock:
                errors.append(exc)
            return
        with collect_lock:
            epochs.extend(mine)

    threads = [threading.Thread(target=spawn_repeatedly) for _ in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"spawning raised: {errors!r}"
    assert len(epochs) == n_threads * per_thread
    assert sorted(epochs) == list(range(1, n_threads * per_thread + 1)), (
        f"duplicate/skipped epochs: {sorted(epochs)}"
    )
    assert _sidecar_value(tmp_path) == n_threads * per_thread


# ── 2. monotonicity across instances ────────────────────────────────────────


def test_later_instance_gets_strictly_greater_epoch(tmp_path):
    first = _make_thread(tmp_path)
    assert first._worker_dir == _worker_dir(tmp_path)
    assert _sidecar_value(tmp_path) == first._generation

    second = _make_thread(tmp_path)
    third = _make_thread(tmp_path)
    assert second._generation > first._generation
    assert third._generation > second._generation
    assert _sidecar_value(tmp_path) == third._generation


def test_context_json_fallback_when_sidecar_lost(tmp_path):
    """A lost sidecar must not restart the counter: context.json's generation
    still bounds the next epoch."""
    _make_thread(tmp_path)  # materialises the worker dir, sidecar = 1
    worker_dir = _worker_dir(tmp_path)

    context_path = worker_dir / "context.json"
    context_path.write_text(json.dumps({"generation": 17}), encoding="utf-8")
    _sidecar(tmp_path).unlink()

    replacement = _make_thread(tmp_path)
    assert replacement._generation > 17
    assert _sidecar_value(tmp_path) == replacement._generation


# ── 3. fail-closed on a sidecar write failure ───────────────────────────────


def test_write_failure_raises_and_sidecar_is_unchanged(tmp_path, monkeypatch):
    thread = _make_thread(tmp_path)
    persisted = _sidecar_value(tmp_path)
    assert persisted == thread._generation

    _fail_generation_replace(
        monkeypatch, lambda: OSError(28, "No space left on device")
    )

    with pytest.raises(OSError):
        thread._allocate_generation()

    # No phantom increment: the sidecar still holds the last persisted epoch.
    assert _sidecar_value(tmp_path) == persisted
    assert thread._generation == persisted
    # ... and the temp file the allocation created was cleaned up.
    assert list(_worker_dir(tmp_path).glob(".generation_*.tmp")) == []


# ── 4. fail-closed on a lock timeout ────────────────────────────────────────


def test_lock_timeout_propagates(tmp_path, monkeypatch):
    """``FileLockTimeoutError`` is an ``Exception``, not an ``OSError``, so a
    bare ``except OSError`` would silently hand out an unpersisted epoch."""
    thread = _make_thread(tmp_path)
    persisted = _sidecar_value(tmp_path)

    def acquire_times_out(self):
        raise FileLockTimeoutError(f"could not acquire {self._lock_path} within 0.0s")

    monkeypatch.setattr(worker_thread_mod.FileLock, "acquire", acquire_times_out)

    with pytest.raises(FileLockTimeoutError):
        thread._allocate_generation()

    assert _sidecar_value(tmp_path) == persisted
    assert thread._generation == persisted


# ── 5. caller level: spawn and reset() both propagate ───────────────────────


def test_spawn_fails_when_epoch_cannot_be_persisted(tmp_path, monkeypatch):
    _fail_generation_replace(
        monkeypatch, lambda: OSError(28, "No space left on device")
    )

    with pytest.raises(OSError):
        _make_thread(tmp_path)

    assert not _sidecar(tmp_path).exists()


def test_reset_fails_when_epoch_cannot_be_persisted(tmp_path, monkeypatch):
    thread = _make_thread(tmp_path)
    assert thread.status == "ready"
    persisted = _sidecar_value(tmp_path)
    context_path = thread._context_path()
    context_path.write_text(json.dumps({"generation": persisted}), encoding="utf-8")

    _fail_generation_replace(
        monkeypatch, lambda: OSError(28, "No space left on device")
    )

    with pytest.raises(OSError):
        thread.reset()

    # The reset did not silently "succeed" with the old epoch, and the
    # context.json unlink (which follows the allocation) never ran.
    assert thread._generation == persisted
    assert _sidecar_value(tmp_path) == persisted
    assert context_path.exists()
