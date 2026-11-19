"""Session-scoping contract for worker JOB READS.

The worker job registry is a single process-wide store keyed by ``job_id``
(``query_id``).  Job *writes* already carry identity (``worker_name`` /
``session_id`` / ``instance_id``); the *reads* are what this module pins.

CONTRACT
--------
1. ``job_status`` with a ``worker_query`` (a job id) is a read scoped to the
   CALLER'S SESSION.  Asking for a job that belongs to another session must be
   indistinguishable from asking for a job id that was never registered: same
   response keys, same values, same "Job not found" error.  A differing answer
   would turn ``job_status`` into an existence oracle for foreign job ids.
2. A foreign read is still RECORDED: the registry logs a WARNING naming the
   session mismatch (caller session versus record session).  Silence would
   make cross-session probing invisible to operators.  The log lives in
   ``tools.workspace.job_registry`` -- the module that owns the records.
3. ``job_status`` with an EMPTY ``worker_query`` (enumerate a worker's jobs)
   returns only jobs the caller's session owns.  Another session's jobs of the
   same worker/instance must not appear, and ``count`` must match the list.
4. The positive direction keeps working: an owner reading its own job still
   gets its status and ``has_result``.
5. FAIL-SAFE DIRECTION: a record with no session attribution (legacy records,
   records created on demand by ``complete()``/``terminalize()``, jobs created
   outside a session) stays readable from a sessioned caller.  Tightening the
   scope must never silently start denying records it cannot attribute --
   denying them would hide real jobs behind "Job not found" forever.
6. Registry-level: ``jobs()`` filters by session ONLY when a ``session_id`` is
   supplied.  ``jobs()`` with no session returns everything; with
   ``session_id="s1"`` it returns the s1 records plus the unattributed ones and
   excludes s2's.

Hermetic: no LLM, no Docker, no ~/.thoughtmachine, no sleeps.  Only the public
surfaces are used -- the ``Worker`` tool and ``WorkerJobRegistry``.

Run:  python3 -m pytest tests/test_job_session_scoping.py -q
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import Worker  # noqa: E402
from tools.workspace.job_registry import WorkerJobRegistry  # noqa: E402

#: Logger the registry-level mismatch warning must be emitted on.
REGISTRY_LOGGER = "tools.workspace.job_registry"

WORKER = "w-scope"
IID = 1
OWNER = "s1"
FOREIGN = "s2"
OWN_JOB = "job-own-s1"
FOREIGN_JOB = "job-foreign-s2"
NO_SESSION_JOB = "job-no-session"


class _SilentBus:
    """Minimal event bus stand-in: accepts subscriptions, never publishes."""

    def __init__(self, *a, **k):
        self._callbacks = []

    def subscribe(self, event_type, callback):
        self._callbacks.append(callback)

    def publish(self, *a, **k):
        return None


def _normalised(response, job_id):
    """Echoed job_id replaced by a placeholder, so two answers can be compared."""
    out = dict(response)
    if out.get("job_id") == job_id:
        out["job_id"] = "<JOB_ID>"
    return out


class _JobReadBase(unittest.TestCase):
    """Fresh registry per test, installed as the SINGLETON the Worker tool reads.

    ``tools.workspace.worker_thread._get_worker_job_registry`` adopts the value
    of ``tools.workspace.worker._WORKER_JOB_REGISTRY`` when it is not None, so
    patching the facade attribute is how a test pins the registry the action
    code sees (same pattern as tests/test_worker_jobs.py).
    """

    def setUp(self):
        self.registry = WorkerJobRegistry(event_bus=_SilentBus())
        self.registry.ensure_subscribed()
        reg_patcher = mock.patch("tools.workspace.worker._WORKER_JOB_REGISTRY",
                                 new=self.registry)
        reg_patcher.start()
        self.addCleanup(reg_patcher.stop)
        super().setUp()

    # -- seeding -------------------------------------------------------

    def _seed(self, job_id, session_id, worker_name=WORKER, instance_id=IID):
        rec = self.registry.register(
            job_id, worker_name, session_id=session_id, instance_id=instance_id,
        )
        self.assertIsNotNone(rec)
        return rec

    # -- driving the tool ----------------------------------------------

    def _job_status(self, session_id, worker_query="", instance_id=IID):
        """One job_status call; returns the parsed response dict."""
        return json.loads(Worker(
            action="job_status",
            worker_name=WORKER,
            worker_query=worker_query,
            session_id=session_id,
            instance_id=instance_id,
        ).execute())


class TestForeignJobIdIsNotAnOracle(_JobReadBase):
    """1. + 4. A foreign job id must look exactly like an unknown one."""

    def test_foreign_job_id_is_indistinguishable_from_unknown(self):
        self._seed(FOREIGN_JOB, FOREIGN)

        foreign = self._job_status(OWNER, FOREIGN_JOB)
        unknown = self._job_status(OWNER, "job-never-registered")

        self.assertEqual(
            set(foreign), {"worker_name", "job_id", "error"},
            f"foreign read must carry NO status/preview/timestamps: {foreign}",
        )
        self.assertEqual(set(unknown), {"worker_name", "job_id", "error"})
        self.assertEqual(
            _normalised(foreign, FOREIGN_JOB),
            _normalised(unknown, "job-never-registered"),
            "a foreign job must be byte-for-byte the unknown-job answer",
        )
        self.assertEqual(foreign.get("error"), "Job not found")

    def test_same_session_read_still_works(self):
        # POSITIVE CONTROL: must pass before AND after the scoping fix.
        self._seed(OWN_JOB, OWNER)

        read = self._job_status(OWNER, OWN_JOB)

        self.assertIsNone(read.get("error"), read)
        self.assertEqual(read.get("job_id"), OWN_JOB)
        self.assertEqual(read.get("status"), "submitted")
        self.assertIs(read.get("has_result"), False)
        self.assertEqual(read.get("worker_name"), WORKER)

    def test_unattributed_record_readable_from_sessioned_caller(self):
        # FAIL-SAFE DIRECTION: no attribution must mean "open", never "denied".
        self._seed(NO_SESSION_JOB, None)

        read = self._job_status(OWNER, NO_SESSION_JOB)

        self.assertIsNone(read.get("error"), read)
        self.assertEqual(read.get("job_id"), NO_SESSION_JOB)
        self.assertEqual(read.get("status"), "submitted")


class TestForeignReadIsLogged(_JobReadBase):
    """2. The denial must be observable: a WARNING naming caller and owner."""

    def test_foreign_job_read_logs_session_mismatch(self):
        self._seed(FOREIGN_JOB, FOREIGN)

        with self.assertLogs(REGISTRY_LOGGER, level="WARNING") as captured:
            self._job_status(OWNER, FOREIGN_JOB)

        matching = [
            rec.getMessage() for rec in captured.records
            if "session" in rec.getMessage().lower()
            and "mismatch" in rec.getMessage().lower()
            and OWNER in rec.getMessage()
            and FOREIGN in rec.getMessage()
        ]
        self.assertTrue(
            matching,
            "expected a WARNING mentioning the session mismatch with both "
            f"'{OWNER}' and '{FOREIGN}'; got {[r.getMessage() for r in captured.records]}",
        )


class TestEnumerationIsSessionScoped(_JobReadBase):
    """3. Empty ``worker_query`` users list only their own session's jobs."""

    def test_job_enumeration_excludes_other_sessions(self):
        self._seed(OWN_JOB, OWNER)
        self._seed(FOREIGN_JOB, FOREIGN)
        self._seed(NO_SESSION_JOB, None)

        response = self._job_status(OWNER, worker_query="")
        self.assertIsNone(response.get("error"), response)

        ids = [job.get("job_id") for job in response["jobs"]]
        self.assertIn(OWN_JOB, ids)
        self.assertNotIn(FOREIGN_JOB, ids, "another session's job leaked")
        self.assertEqual(response["count"], len(response["jobs"]))
        self.assertEqual(response["count"], len(set(ids)))


class TestRegistryJobsSessionFilter(_JobReadBase):
    """6. ``jobs()`` filters by session exactly when one is supplied."""

    def test_registry_jobs_filter_only_when_session_provided(self):
        self._seed(OWN_JOB, OWNER)
        self._seed(FOREIGN_JOB, FOREIGN)
        self._seed(NO_SESSION_JOB, None)

        everything = [job["job_id"] for job in self.registry.jobs()]
        self.assertEqual(
            sorted(everything), sorted([OWN_JOB, FOREIGN_JOB, NO_SESSION_JOB]),
            "no session_id must mean NO session filter",
        )

        scoped = [job["job_id"] for job in self.registry.jobs(session_id=OWNER)]
        self.assertEqual(
            sorted(scoped), sorted([OWN_JOB, NO_SESSION_JOB]),
            "scoped read = own records + unattributed, never another session's",
        )


class TestCompletedJobsListIsSessionScoped(_JobReadBase):
    """7. The interrupted-join ``completed_jobs`` list is the caller's own view.

    Produced by the REAL ``Worker._join_completed_jobs`` -- the fifth read of the
    registry. The interrupt machinery itself (bound cancel token, live fake
    thread) lives in ``tests/test_worker_join_interrupt.py``; only the SCOPING of
    the resulting list is pinned here, so no thread and no sleep are needed.
    """

    def test_completed_jobs_exclude_other_sessions(self):
        for job_id, owner in ((OWN_JOB, OWNER),
                              (FOREIGN_JOB, FOREIGN),
                              (NO_SESSION_JOB, None)):
            self._seed(job_id, owner)
            self.registry.update(job_id, status="completed")

        tool = Worker(
            action="join",
            worker_name=WORKER,
            worker_query=OWN_JOB,
            session_id=OWNER,
            instance_id=IID,
        )
        ids = [job["job_id"] for job in tool._join_completed_jobs(self.registry)]

        self.assertIn(OWN_JOB, ids)
        self.assertNotIn(FOREIGN_JOB, ids,
                         "another session's job leaked into completed_jobs")
        self.assertIn(NO_SESSION_JOB, ids,
                      "unattributed records must stay visible (fail-safe)")


if __name__ == "__main__":
    unittest.main()
