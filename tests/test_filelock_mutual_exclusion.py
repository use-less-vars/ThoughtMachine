"""``FileLock`` — one lock path must really mean one holder at a time.

``session.lock.FileLock`` is an *inode* lock: ``flock``/``msvcrt.locking``
guard the open file description, not the path.  The lock file therefore has
to survive ``release()`` — it is the rendezvous point every acquirer must
open.  If ``release()`` unlinks it, a waiter that already holds the OLD inode
open can win a lock on an orphaned inode while the next caller creates the
path afresh (new inode) and locks that one: two concurrent holders by
construction, with no crash and no error to show for it.

What is pinned here:

  1. MUTUAL EXCLUSION — 8 threads x 5 iterations hammering ONE lock path never
     observe more than one holder inside the critical section.  The detector
     tallies simultaneous holders under a plain ``threading.Lock``, so it is an
     overlap assertion, not a timing heuristic: a green run cannot be luck.
  2. STEADY STATE — after a release the ``.lock`` file persists and keeps the
     same inode across acquire/release cycles, so every acquirer meets the same
     rendezvous point.

This file FAILS against the old implementation that unlinked the lock file on
release — verified by re-inserting ``self._lock_path.unlink(missing_ok=True)``
after the unlock in ``release()`` and re-running this file three times: both
tests fail, and the first one reports 8 / 19 / 11 overlapping windows out of 40
acquisitions respectively.  With the unlink removed the same file reports 0
overlaps and both tests pass.  No timing assertion is involved — only the
overlap counter — so the red run is the race itself, not a flaky deadline.
Do not put the unlink back.

Hermetic: ``tmp_path`` only, no network, no Docker, no LLM.  The single
0.5 ms sleep sits *inside* the critical section on purpose: it widens the
window a racy implementation falls into instead of hiding it.

Run:  python -m pytest tests/test_filelock_mutual_exclusion.py -q
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from session.lock import FileLock  # noqa: E402

# ── Contention shape ──────────────────────────────────────────────────────────
# Enough threads that a re-created lock path is practically certain to be
# observed, and enough iterations to hit the release window many times.
N_THREADS = 8
N_ITERATIONS = 5
HOLD_SECONDS = 0.0005
ACQUIRE_TIMEOUT = 30.0  # generous: a timeout here is a different failure mode


def _contend(lock_path: Path):
    """Hammer *lock_path* from ``N_THREADS`` threads; return observations.

    Returns ``(overlaps, acquisitions, errors)``: *overlaps* lists every
    observation of more than one simultaneous holder, *acquisitions* lists the
    holder set recorded at each entry into the critical section, and *errors*
    collects whatever a contender raised.
    """
    book = threading.Lock()  # the DETECTOR's lock, never the FileLock under test
    inside: dict[int, int] = {}
    overlaps: list[tuple[int, ...]] = []
    acquisitions: list[tuple[int, ...]] = []
    errors: list[str] = []
    gate = threading.Barrier(N_THREADS)

    def contender(idx: int) -> None:
        try:
            gate.wait()
            for _ in range(N_ITERATIONS):
                # A FRESH instance per iteration: instance re-entrancy must not
                # mask the inter-thread exclusion this test is about.
                with FileLock(str(lock_path), timeout=ACQUIRE_TIMEOUT):
                    with book:
                        inside[idx] = inside.get(idx, 0) + 1
                        live = tuple(sorted(i for i, n in inside.items() if n))
                        acquisitions.append(live)
                        if len(live) > 1:
                            overlaps.append(live)
                    time.sleep(HOLD_SECONDS)
                    with book:
                        inside[idx] -= 1
        except BaseException as exc:  # noqa: BLE001 - reported as a failure
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=contender, args=(i,), name=f"lock-{i}")
        for i in range(N_THREADS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return overlaps, acquisitions, errors


# ── 1. Mutual exclusion on one lock path ─────────────────────────────────────

def test_one_lock_path_admits_one_holder(tmp_path):
    """No two threads may ever be inside the critical section together."""
    overlaps, acquisitions, errors = _contend(tmp_path / "context.json")

    assert errors == [], f"contenders failed instead of excluding: {errors}"
    assert len(acquisitions) == N_THREADS * N_ITERATIONS, (
        f"expected {N_THREADS * N_ITERATIONS} entrances, got {len(acquisitions)}"
    )
    assert overlaps == [], (
        f"{len(overlaps)} overlapping window(s) out of {len(acquisitions)} "
        f"acquisitions on ONE lock path (holders seen together: {overlaps[:5]}) "
        "— FileLock.release() must not unlink the lock file"
    )
    assert max(len(holders) for holders in acquisitions) == 1, (
        "the overlap detector never saw exactly one holder"
    )


# ── 2. The lock file is the stable rendezvous point ──────────────────────────

def test_lock_file_is_the_stable_rendezvous_point(tmp_path):
    """Release must leave the lock file in place, on the same inode."""
    target = tmp_path / "context.json"
    lock_path = tmp_path / "context.json.lock"

    first = FileLock(str(target))
    first.acquire()
    assert lock_path.exists()
    held_inode = lock_path.stat().st_ino
    first.release()

    assert first.is_locked is False
    assert lock_path.exists(), (
        "release() unlinked the lock file: a waiter parked on the old inode "
        "would then own an orphaned inode while the next caller locks a fresh one"
    )
    assert lock_path.stat().st_ino == held_inode, (
        "release() removed and re-created the rendezvous point instead of "
        "leaving the inode alone"
    )

    second = FileLock(str(target))
    second.acquire()
    assert lock_path.stat().st_ino == held_inode
    second.release()
    assert lock_path.stat().st_ino == held_inode
