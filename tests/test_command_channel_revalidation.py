"""A claimed control command must never act on state that moved after the claim.

``WorkerThread._poll_command`` is two steps, not one: the poller CLAIMS the
live ``command.json`` (``command_channel.claim_command`` renames it away) and
then dispatches on ``action``.  In between, any in-process control write --
``pause()``, ``resume()``, ``detach()``/``stop()`` -- can land and commit NEWER
state.  The dispatch was blind to that: a resume claimed a moment before an
operator's manual-only pause would clear ``_manual_only_pause`` and lift the
park, and a stop claimed a moment before a resume would re-detach a worker that
had just been resumed.  Both are the poller overwriting a decision that is
younger than the command it is acting on.

The property pinned here: a claimed command is obeyed only while no LATER
in-process control write has happened since it was issued.  The tests drive the
REAL poller with the REAL claim (no stubs for either): the interleaving is made
deterministic by hooking ``claim_command`` in ``worker_thread``'s namespace, so
the newer control write lands exactly where the production window is -- after
the claim, before the action.

Compatibility (also pinned here): a payload that carries no ordering stamp is
written by ANOTHER process (``web_ui/backend/workspace_routes.py`` builds its
own fence and has no access to this process's counter), so it has no local
ordering to be compared against and keeps behaving exactly as it does today.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import tools.workspace.worker_thread as worker_thread_module
from tools.workspace.command_channel import COMMAND_FILENAME, write_command
from tools.workspace.worker_thread import WorkerThread

WORKER = "w-revalidation"
SESSION = "sess-cmd-revalidation"


@pytest.fixture(autouse=True)
def _isolated_operator_latch():
    """``pause()`` writes the module-global operator-pause record; reset it."""
    worker_thread_module._MANUAL_ONLY_PAUSED.clear()
    yield
    worker_thread_module._MANUAL_ONLY_PAUSED.clear()


def _thread(tmp_path, name: str = WORKER, session_id: str = SESSION) -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed.

    One lifecycle transition is driven synchronously per test; a live ``run()``
    loop would only add noise by polling the channel on its own cadence.
    """
    return WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id=session_id,
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def _claim_then_newer_control_write(monkeypatch, newer_control_write) -> list:
    """Make the poller claim the live command, then land a NEWER control write.

    This is the defect's window, made deterministic: the real
    ``claim_command`` runs first (so the payload the poller is about to dispatch
    on is genuinely its own), the newer control write runs second (committing
    the state that must survive), and only then does ``_poll_command`` reach its
    action branch.  Returns the list of claims, so a test can prove the hook
    actually fired rather than passing vacuously.
    """
    real_claim = worker_thread_module.claim_command
    claims: list = []

    def _claim(worker_dir):
        claimed = real_claim(worker_dir)
        if claimed is not None and not claims:
            claims.append(claimed)
            newer_control_write()
        return claimed

    monkeypatch.setattr(worker_thread_module, "claim_command", _claim)
    return claims


def test_a_claimed_resume_cannot_clear_a_newer_manual_only_pause(tmp_path, monkeypatch):
    """The flake, as a property: resume claimed, then the operator pauses.

    ``_manual_only_pause`` is the tray's ``paused_manually`` truth field, so
    acting on the superseded resume both re-starts a worker the operator just
    parked and reports it as not-manually-paused.
    """
    thread = _thread(tmp_path)
    # Writer: a 'resume' is issued and sits in the channel.
    assert thread.resume() is True
    claims = _claim_then_newer_control_write(
        monkeypatch, lambda: thread.pause(manual_only=True)
    )

    thread._poll_command()

    assert claims, "the poller never claimed the resume -- the test did not reach the window"
    # The newer control write is the operator's manual-only pause.  It must
    # survive: the poller was acting on a command that is older than it.
    assert thread._manual_only_pause is True, (
        "a claimed 'resume' cleared a manual-only pause written AFTER the claim"
    )
    assert thread._park_event.is_set() is True, (
        "a claimed 'resume' un-parked a worker the operator paused AFTER the claim"
    )


def test_a_claimed_stop_cannot_re_detach_after_a_newer_resume(tmp_path, monkeypatch):
    """Mirror window: the stop is claimed, a resume lands, the stale stop acts."""
    thread = _thread(tmp_path)
    thread.detach()  # writer: a 'stop' is issued and sits in the channel
    assert thread._detach_requested is True
    claims = _claim_then_newer_control_write(monkeypatch, thread.resume)

    thread._poll_command()

    assert claims, "the poller never claimed the stop -- the test did not reach the window"
    assert thread._detach_requested is False, (
        "a claimed 'stop' re-detached a worker that was resumed AFTER the claim"
    )
    assert thread.status != "detached", (
        f"a claimed 'stop' published status={thread.status!r} after a newer resume"
    )
    assert thread._park_event.is_set() is False, (
        "a claimed 'stop' re-parked a worker that was resumed AFTER the claim"
    )


def test_an_unstamped_payload_is_obeyed_exactly_as_before(tmp_path, monkeypatch):
    """No ordering stamp => no comparison => today's behaviour, unchanged.

    ``web_ui/backend/workspace_routes.py`` writes from another process and
    cannot stamp this process's counter, and payloads written by an older
    build carry no stamp at all.  Neither may become un-actable.
    """
    thread = _thread(tmp_path)
    thread.pause(manual_only=True)
    # The cross-process shape: fenced, but no ordering stamp.
    assert write_command(thread._worker_dir, "resume", thread._fence()) is True
    claims = _claim_then_newer_control_write(
        monkeypatch, lambda: thread.pause(manual_only=True)
    )

    thread._poll_command()

    assert claims, "the poller never claimed the unstamped command"
    assert thread._manual_only_pause is False, (
        "an unstamped 'resume' must still be obeyed even though a later "
        "in-process control write exists -- it carries no local ordering"
    )
    # The newer in-process write must not have been eaten by the poll.
    assert (thread._worker_dir / COMMAND_FILENAME).is_file() is True, (
        "the claim consumed the newer in-process command instead of leaving it live"
    )


def test_a_channel_resume_releases_the_operator_latch_too(tmp_path):
    """The latch and the attribute must not diverge.

    The channel 'resume' cleared ``_manual_only_pause`` only.  The session
    record (``_MANUAL_ONLY_PAUSED``) kept claiming the worker was
    operator-paused, so a later release walked a worker that had already been
    resumed -- and every reader of the record disagreed with every reader of
    the attribute.
    """
    thread = _thread(tmp_path)
    thread.pause(manual_only=True)
    assert thread._manual_only_pause is True
    assert (WORKER, 1) in worker_thread_module._MANUAL_ONLY_PAUSED[SESSION]

    assert write_command(thread._worker_dir, "resume", thread._fence()) is True
    thread._poll_command()

    assert thread._manual_only_pause is False
    assert (WORKER, 1) not in worker_thread_module._MANUAL_ONLY_PAUSED.get(SESSION, set()), (
        "the channel 'resume' released the attribute but left the session latch set"
    )
