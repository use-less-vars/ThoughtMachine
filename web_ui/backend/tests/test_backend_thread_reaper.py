"""The backend autouse reaper must actually REAP a worker thread.

``web_ui/backend/tests/conftest.py`` reaps what a test leaves behind so that the
census assertion below it scores state that was already cleared. Nothing proved
the reaper did anything, and it did not: ``_webui_reap_worker_threads`` looped
``for ident, thread in threading.enumerate():``, but ``threading.enumerate()``
yields ``Thread`` objects, so the first iteration raised ``TypeError: cannot
unpack non-iterable _MainThread object``. The fixture catches that
(``except Exception``) and prints ``thread census: thread reaping failed: ...``
to stderr, so reaping was inert on every teardown and the failure was silent:
the census still ran, over state that had never been cleared.

This test is red against that code and green against the fix. It drives the
reaper directly with the same ``threads_before`` shape the fixture builds, and
it leaks a thread the registry does not know about, so the ``enumerate()`` loop
is the only branch that can find it -- the branch that was broken.

Run:  python3 -m pytest web_ui/backend/tests/test_backend_thread_reaper.py -q
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tools.workspace.worker import WorkerThread


def test_reaper_kills_and_joins_a_leaked_worker_thread(
    tmp_path, request, _webui_reaper
):
    """The reaper must reap, driven directly with the fixture's own shapes.

    The reaper is handed over by the ``_webui_reaper`` fixture rather than
    looked up in ``sys.modules``: this root has no ``__init__.py``, so its
    conftest is imported under the bare name ``conftest`` and that key belongs
    to whichever non-package conftest was imported last -- see the fixture's
    docstring.
    """
    # The reaper only protects tests it actually runs for.
    assert "_webui_thread_bridge_census" in request.fixturenames

    threads_before = {id(t) for t in threading.enumerate()}
    try:
        keys_before = set(_webui_reaper.worker_registry().get_all_workers())
    except Exception:
        keys_before = set()

    worker = WorkerThread(
        name="reaper-probe",
        definition={},
        agent_config={},
        workspace_dir=Path(tmp_path),
    )
    worker.start()
    try:
        assert worker.is_alive(), "the probe worker did not start"
        assert any(t is worker for t in threading.enumerate()), (
            "the probe worker is not visible to threading.enumerate()"
        )
        assert id(worker) not in threads_before, (
            "the probe worker must be new since the snapshot, or the reaper is "
            "right to leave it alone"
        )
        assert not any(t is worker for t in keys_before or ()), (
            "the probe worker must stay out of the worker registry, so that the "
            "enumerate() loop is the only branch that can find it"
        )

        survivors = _webui_reaper.reap_worker_threads(threads_before, keys_before)

        assert survivors == [], "the reaper reported survivor(s) after kill()+join"
        assert not worker.is_alive(), "the leaked worker is still alive after reaping"
        assert not any(t is worker for t in threading.enumerate()), (
            "the leaked worker is still in threading.enumerate() after reaping"
        )
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(5.0)


@pytest.mark.thread_census
def test_the_census_assertion_half_runs_on_a_clean_test():
    """The ENFORCED census must be exercised by some test, on every run.

    ``_webui_thread_bridge_census`` returns early unless the test carries the
    ``thread_census`` marker, so without a marked test the assertion half is
    dead code that no run of this root ever executes -- which is how its
    ``for ident, thread in threading.enumerate():`` stayed broken and untested.
    A clean test must reach the census comparison and pass it; if the loop
    regresses, this test errors at teardown instead of passing.
    """
    assert True
