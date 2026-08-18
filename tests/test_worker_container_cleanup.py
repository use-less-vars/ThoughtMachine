"""
TESTS-FIRST — worker container cleanup on timeout/error (RED on
feat/wlm-phase1-safe-foundation).

Contracts under test (B1–B3 are now satisfied by prod; contract C is RED
on the current branch):

  B1. A worker that hits a soft timeout (state CRITICAL + restriction_reason
      "timeout") stops+removes its worker-owned containers via the container
      manager (label thoughtmachine.worker=<session_id>:<worker_name>).
  B2. A worker that errors out of run() (exception propagating out of
      _run_tool_loop) stops+removes its worker-owned containers.
  B3. Cleanup actually runs AND never touches resource containers
      (thoughtmachine.resource label / tm-res-* name / tm-resource-git image)
      — the exclusion contract.
  C.  Ownership is EXACT-VALUE: a container belongs to a worker only when
      thoughtmachine.worker == "<session_id>:<worker_name>". Teardown must
      never touch sibling workers' containers in the same session (RED
      today: label PRESENCE decides ownership, so worker A's teardown also
      stops+removes worker B's containers) and never reclaims stale
      bare-name labels from the old format.

Mocks only — no real Docker, no sleeps, no production code changes. run() is
executed inline with a patched _run_tool_loop (same pattern as
tests/test_worker_timeout_audit.py TestSoftTimeoutEnvelope).

Run:  python3 -m pytest tests/test_worker_container_cleanup.py -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import WorkerThread  # noqa: E402

SYS_PROMPT = "You are a helpful worker assistant."


def make_fake_agent(time_state_value="LOW", restriction_reason=None):
    """Minimal fake agent whose .state mirrors what run() resets per query."""
    state = SimpleNamespace(
        current_turn=0,
        turn_state=None,
        last_turn_warning_state=None,
        restrictions_active=False,
        restrictions_pending=False,
        restriction_reason=restriction_reason,
        time_start=0.0,
        last_time_warning_state=None,
        time_state=SimpleNamespace(value=time_state_value),
    )
    return SimpleNamespace(state=state)


def make_thread(workspace_dir, name="w-test", timeout=60, session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": SYS_PROMPT},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=timeout,
    )


class _FakeEventBus:
    """Callable stand-in for agent.events.EventBus."""

    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


class _RunSafetyPatches(unittest.TestCase):
    """Patches event plumbing so WorkerThread.run() can execute headlessly."""

    def setUp(self):
        patchers = [
            mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
            mock.patch("tools.workspace.worker.register_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.unregister_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.global_event_bus", new=None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        super().setUp()


# Container fakes — the mocked container manager only records calls; prod
# does the discovery. Labels encode the NEW contract: worker-owned containers
# carry thoughtmachine.worker=<session_id>:<worker_name> (the owning worker's
# identity — "s1:w-test" matches make_thread's session_id="s1" + name
# "w-test"); resource containers carry thoughtmachine.resource and/or
# tm-res-* / tm-resource-git names.
WORKER_CONTAINER = SimpleNamespace(
    id="c-w1",
    name="tm-worker-w-test",
    labels={"thoughtmachine.worker": "s1:w-test"},
)
RESOURCE_CONTAINER = SimpleNamespace(
    id="c-res",
    name="tm-resource-git",
    labels={"thoughtmachine.resource": "git"},
)


def _called_with(mock_obj, container):
    """True if any recorded call passed the container object, its id or name."""
    for call in mock_obj.call_args_list:
        values = list(call.args or ()) + list((call.kwargs or {}).values())
        for v in values:
            if v == container or v == container.id or v == container.name:
                return True
    return False


class TestWorkerContainerCleanup(_RunSafetyPatches):
    """B1/B2/B3 — container cleanup on the timeout and error paths."""

    def _run_timeout(self, tmp, cm):
        """Drive run() inline through a soft-timeout _run_tool_loop."""
        # Identity "s1:w-test" (session_id="s1" + name "w-test") must equal
        # the WORKER_CONTAINER label under the exact-value ownership contract.
        thread = make_thread(tmp, name="w-test")
        thread._agent = make_fake_agent("CRITICAL", "timeout")
        thread._state_bridge = SimpleNamespace(context_length=0)
        # NEW prod contract: WorkerThread gains a container_manager
        # constructor param stored as self._container_manager.
        thread._container_manager = cm
        thread._input_queue.put("query-1")
        thread._input_queue.put(None)

        def fake_run_tool_loop(self, query):
            # run()'s per-query reset cleared restriction_reason to None;
            # simulate the agent's timeout restriction being applied before
            # the timeout-detection snippet runs (worker.py L1306-1317).
            self._agent.state.restriction_reason = "timeout"
            agent_state = self._agent.state
            if (
                hasattr(agent_state, "time_state")
                and hasattr(agent_state.time_state, "value")
                and agent_state.time_state.value == "CRITICAL"
                and getattr(agent_state, "restriction_reason", None) == "timeout"
            ):
                self._timeout_triggered = True
            self._last_elapsed_val = 1.0
            self._final_token_usage = self.get_current_context_tokens()
            return None

        with mock.patch.object(
            thread, "_run_tool_loop",
            new=fake_run_tool_loop.__get__(thread, WorkerThread),
        ):
            thread.run()
        return thread

    def _run_error(self, tmp, cm):
        """Drive run() inline through an exploding _run_tool_loop."""
        # Identity "s1:w-test" (session_id="s1" + name "w-test") must equal
        # the WORKER_CONTAINER label under the exact-value ownership contract.
        thread = make_thread(tmp, name="w-test")
        thread._agent = make_fake_agent()
        thread._state_bridge = SimpleNamespace(context_length=0)
        thread._container_manager = cm
        thread._input_queue.put("query-1")
        thread._input_queue.put(None)

        def fake_run_tool_loop(self, query):
            raise RuntimeError("worker exploded")

        with mock.patch.object(
            thread, "_run_tool_loop",
            new=fake_run_tool_loop.__get__(thread, WorkerThread),
        ):
            thread.run()
        return thread

    def test_worker_timeout_cleans_up_worker_owned_containers(self):
        cm = mock.Mock()
        # Harness fix: configure discovery so the cleanup path has containers
        # to iterate (a plain unconfigured Mock is non-iterable — harness
        # bug, not a contract weakening). Contract asserts unchanged.
        cm.list_containers.return_value = [WORKER_CONTAINER, RESOURCE_CONTAINER]
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._run_timeout(tmp, cm)
            # Sanity: the driver really exercised the timeout path (envelope
            # status 'timeout', mirroring TestSoftTimeoutEnvelope).
            raw = thread._output_queue.get_nowait()
            envelope = json.loads(raw)
            self.assertEqual(envelope["status"], "timeout")
            # RED today: the timeout path never calls the container manager.
            self.assertTrue(
                _called_with(cm.stop, WORKER_CONTAINER),
                msg=f"timeout path never stopped worker container; "
                    f"stop calls={cm.stop.call_args_list}",
            )
            self.assertTrue(
                _called_with(cm.remove, WORKER_CONTAINER),
                msg=f"timeout path never removed worker container; "
                    f"remove calls={cm.remove.call_args_list}",
            )
            # Exclusion contract: resource containers never touched.
            self.assertFalse(_called_with(cm.stop, RESOURCE_CONTAINER))
            self.assertFalse(_called_with(cm.remove, RESOURCE_CONTAINER))

    def test_worker_error_cleans_up_worker_owned_containers(self):
        cm = mock.Mock()
        # Harness fix: configure discovery so the cleanup path has containers
        # to iterate (a plain unconfigured Mock is non-iterable — harness
        # bug, not a contract weakening). Contract asserts unchanged.
        cm.list_containers.return_value = [WORKER_CONTAINER, RESOURCE_CONTAINER]
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._run_error(tmp, cm)
            # Sanity: the error path really sets status='error' (passes today).
            self.assertEqual(thread.status, "error")
            # RED today: the error path never calls the container manager.
            self.assertTrue(
                _called_with(cm.stop, WORKER_CONTAINER),
                msg=f"error path never stopped worker container; "
                    f"stop calls={cm.stop.call_args_list}",
            )
            self.assertTrue(
                _called_with(cm.remove, WORKER_CONTAINER),
                msg=f"error path never removed worker container; "
                    f"remove calls={cm.remove.call_args_list}",
            )

    def test_resource_containers_never_touched_by_worker_cleanup(self):
        cm = mock.Mock()
        # Harness fix: configure discovery so the cleanup path has containers
        # to iterate (a plain unconfigured Mock is non-iterable — harness
        # bug, not a contract weakening). Contract asserts unchanged.
        cm.list_containers.return_value = [WORKER_CONTAINER, RESOURCE_CONTAINER]
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._run_error(tmp, cm)
            self.assertEqual(thread.status, "error")
            # Cleanup must actually RUN (RED today: zero container calls)...
            self.assertTrue(
                cm.stop.called,
                msg="worker cleanup never invoked container_manager.stop",
            )
            self.assertTrue(
                cm.remove.called,
                msg="worker cleanup never invoked container_manager.remove",
            )
            # ...and must exclude resource containers by contract.
            self.assertFalse(_called_with(cm.stop, RESOURCE_CONTAINER))
            self.assertFalse(_called_with(cm.remove, RESOURCE_CONTAINER))


class TestCrossWorkerIsolation(_RunSafetyPatches):
    """C — cross-worker container isolation (exact-value ownership).

    Two workers in the SAME session share one container manager. Worker A's
    teardown must stop+remove ONLY containers whose
    ``thoughtmachine.worker`` label equals A's identity
    (``"<session_id>:<worker_name>"``). RED today: ownership is decided by
    label PRESENCE, so A's teardown also stops+removes sibling worker B's
    containers.
    """

    def _drive_cleanup(self, tmp, cm, name, listed):
        """Build a worker thread (identity s1:<name>), attach the manager and
        run worker-scoped cleanup directly (the same method run()'s finally
        and _action_stop invoke)."""
        thread = make_thread(tmp, name=name)
        thread._container_manager = cm
        cm.list_containers.return_value = listed
        thread._cleanup_worker_containers()
        return thread

    def test_worker_teardown_leaves_sibling_containers_untouched(self):
        cm = mock.Mock()
        worker_a = SimpleNamespace(
            id="c-wa",
            name="tm-worker-w-a",
            labels={"thoughtmachine.worker": "s1:w-a"},
        )
        worker_b = SimpleNamespace(
            id="c-wb",
            name="tm-worker-w-b",
            labels={"thoughtmachine.worker": "s1:w-b"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            self._drive_cleanup(
                tmp, cm, name="w-a",
                listed=[worker_a, worker_b, RESOURCE_CONTAINER],
            )
            # A's own container is stopped and removed.
            self.assertTrue(
                _called_with(cm.stop, worker_a),
                msg=f"A's own container never stopped; "
                    f"stop calls={cm.stop.call_args_list}",
            )
            self.assertTrue(
                _called_with(cm.remove, worker_a),
                msg=f"A's own container never removed; "
                    f"remove calls={cm.remove.call_args_list}",
            )
            # Sibling worker B's container must survive A's teardown. RED
            # today: presence-based matching treats B's label as owned by A.
            self.assertFalse(
                _called_with(cm.stop, worker_b),
                msg=f"sibling worker container was stopped; "
                    f"stop calls={cm.stop.call_args_list}",
            )
            self.assertFalse(
                _called_with(cm.remove, worker_b),
                msg=f"sibling worker container was removed; "
                    f"remove calls={cm.remove.call_args_list}",
            )
            # Resource containers remain excluded.
            self.assertFalse(_called_with(cm.stop, RESOURCE_CONTAINER))
            self.assertFalse(_called_with(cm.remove, RESOURCE_CONTAINER))

    def test_worker_teardown_ignores_stale_bare_name_labels(self):
        cm = mock.Mock()
        # A container labelled with the OLD bare-name format (no session
        # prefix) must NOT be reclaimed by an identity-scoped teardown.
        stale = SimpleNamespace(
            id="c-stale",
            name="tm-worker-w-a",
            labels={"thoughtmachine.worker": "w-a"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            self._drive_cleanup(
                tmp, cm, name="w-a",
                listed=[stale, RESOURCE_CONTAINER],
            )
            self.assertFalse(
                _called_with(cm.stop, stale),
                msg=f"stale bare-name label was stopped; "
                    f"stop calls={cm.stop.call_args_list}",
            )
            self.assertFalse(
                _called_with(cm.remove, stale),
                msg=f"stale bare-name label was removed; "
                    f"remove calls={cm.remove.call_args_list}",
            )
            self.assertFalse(_called_with(cm.stop, RESOURCE_CONTAINER))
            self.assertFalse(_called_with(cm.remove, RESOURCE_CONTAINER))


if __name__ == "__main__":
    unittest.main()
