"""THE FINALLY PIN — a cancelled exec must still stop its container (C11).

C11's suspicion was "a cancelled exec leaves a live container burning CPU".
That is FALSE on the ``DockerCodeRunner`` path, but only because the caller
happens to have one line (``tools/docker_code_runner.py:449-458``)::

    try:
        result = manager.exec(
            info["id"], command=actual_command, timeout=self.timeout,
            workdir=workdir, environment=self.environment,
        )
    finally:
        manager.stop(info["id"])

That ``finally`` runs BEFORE the interrupted payload is built (``:465`` /
``:484``), so the container is reclaimed even though the wait was abandoned
and the in-container process was never killed.  NO TEST PINS IT: delete those
two lines and C11 silently flips from "callers must clean up" to "callers
leak", with nothing going red.  This file is that pin.

Hermetic: no Docker daemon, no network, no real container, no meaningful
sleep -- the fake manager answers immediately.
"""

import json
import threading
import time

import pytest

from tools import cancellation
import tools.docker_code_runner as dcr

CONTAINER_ID = "c-pin-1"
CANCEL_REASON = "stopped_by_operator"

#: Every ``_RecordingManager`` built during a test, in creation order.
_MANAGERS = []


class _RecordingManager:
    """Stand-in for ``ContainerManager`` that RECORDS the lifecycle calls.

    ``exec`` consults the REAL production cancellation module
    (``tools.cancellation``) rather than a flag this fake also owns, so the
    "the wait was abandoned" input is the same one production reads.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = []
        self.execs = []
        self.stopped = []
        _MANAGERS.append(self)

    def start(self, **kwargs):
        self.started.append(CONTAINER_ID)
        return {"id": CONTAINER_ID, "status": "created"}

    def exec(self, container_id, **kwargs):
        self.execs.append(container_id)
        if cancellation.is_interrupted():
            # The exact shape ContainerManager.exec returns when it abandons
            # the wait (infra/container_manager.py) -- exit_code is None.
            return {
                "status": "interrupted",
                "interrupted": True,
                "reason": cancellation.cancel_reason(),
                "exec": "container_exec",
                "container_id": container_id,
                "command": kwargs.get("command"),
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "note": "Stopped WAITING for the command. NOT killed.",
            }
        return {"stdout": "out", "stderr": "err", "exit_code": 0}

    def stop(self, container_id):
        self.stopped.append(container_id)

    def remove(self, container_id):
        self.removed = getattr(self, "removed", [])
        self.removed.append(container_id)

    def build_image(self, tag=None):
        return None


@pytest.fixture(autouse=True)
def _clean_cancel_token():
    """This test owns the current thread's cancel token; never leak it.

    ``unbind()`` alone is NOT enough when a token was ever raised through the
    FALLBACK event: that event is per-thread and sticky, so leaving it set
    would make every later test on this thread look cancelled (it did:
    ``test_timeout_path_still_kills_and_drops`` in the sibling file stopped
    raising TimeoutError when this file ran first).  ``reset()`` clears
    whichever event is active after the unbind -- both halves are
    load-bearing.
    """
    cancellation.unbind()
    cancellation.reset()
    _MANAGERS.clear()
    yield
    cancellation.unbind()
    cancellation.reset()
    _MANAGERS.clear()


def test_cancelled_exec_still_stops_the_container(monkeypatch, tmp_path):
    """A turn cancelled BEFORE the call still stops the container it started.

    The token is PRE-FIRED (the turn was already cancelled before
    ``execute()`` ran), which is the state a Stop pressed mid-``exec`` leaves
    behind: the wait is abandoned at once and ``exec`` returns the
    ``interrupted`` payload instead of a normal exit.

    # catches: C11 — the only thing stopping a cancelled exec from leaving a
    # live container is the caller's finally, which nothing currently tests.
    # Removing it would turn C11 from "callers must clean up" into "callers
    # leak".
    """
    # PRE-FIRED: the turn was already cancelled before execute() ran.  Bind
    # FIRST: interrupt() raises whatever event is active, and with nothing
    # bound that is the per-thread FALLBACK event, which stays set for the
    # rest of the session.
    token = threading.Event()
    cancellation.bind_turn_cancel(token)
    cancellation.interrupt(CANCEL_REASON)
    assert cancellation.is_interrupted() is True  # precondition, not a pin
    assert token.is_set() is True  # precondition: the BOUND token was raised

    monkeypatch.setattr(dcr, "DOCKER_AVAILABLE", True)
    monkeypatch.setattr(dcr, "ContainerManager", _RecordingManager)
    monkeypatch.setattr(dcr, "audit_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        dcr.DockerCodeRunner,
        "_resolve_registry_workspace",
        lambda self: str(tmp_path),
    )

    runner = dcr.DockerCodeRunner(command="sleep 30")
    started = time.monotonic()
    payload = json.loads(runner.execute())
    elapsed = time.monotonic() - started

    cm = _MANAGERS[0]

    # (a) THE PIN on the ``finally`` at docker_code_runner.py:457-458.
    assert cm.stopped == [CONTAINER_ID], (
        "the container started by a CANCELLED exec was never stopped: the "
        "caller's try/finally around manager.exec is the only thing that "
        "reclaims it (docker_code_runner.py:457-458)"
    )
    # catches: C11 — dropping ``finally: manager.stop(info["id"])`` leaves
    # ``stopped`` empty and the cancelled exec's container running forever.
    assert cm.execs == [CONTAINER_ID]
    # catches: the pin above is vacuous if exec never actually ran.
    assert cm.stopped == cm.started, (
        "the id that was stopped is not the id that was started"
    )
    # catches: stopping some other container (or a hard-coded id) instead of
    # the one this call created.

    # (b) the returned payload is the interrupted one, not a clean run.
    assert payload["status"] == "interrupted"
    # catches: reading the abandoned wait as a normal exit -- exit_code is None
    # on that path, so a status check is the only thing that separates them.
    assert payload["interrupted"] is True
    # catches: dropping the explicit ``interrupted`` marker (line 469).
    assert payload["exit_code"] is None
    # catches: a cancelled turn being reported with a real exit code (0/-1/-2),
    # which callers would read as success / timeout / failure (line 470).
    assert payload["success"] is False
    # catches: a cancelled exec being reported as a success (line 467).
    assert payload["reason"] == CANCEL_REASON
    # catches: the manager's cancellation reason being swallowed instead of
    # surfaced verbatim (line 471).
    assert payload["timed_out"] is False
    # catches: a cancel being mislabelled as a timeout (line 476).

    # The whole thing is a cancellation, not a wait: it must be prompt.
    assert elapsed < 2.0
    # catches: the fix regressing into "let the cancelled wait run to its
    # timeout" -- the fake answers at once, so any delay is the caller's.
