"""Follow-up ``context`` delivery for the agent-facing ``Worker`` tool.

The defect these pin: ``context`` (the briefing channel) was honoured only on
``action='spawn'``.  On ``action='query'`` / ``'submit_query'`` a supplied
context was SILENTLY ignored -- no error, no warning -- so a follow-up query
lost the briefing and the worker had to rediscover state it was already given.

Contract under test (tools/workspace/worker_thread.py):

* a follow-up context is PREPENDED to ``worker_query`` as one string, labelled
  with ``FOLLOWUP_CONTEXT_PREFIX`` -- never with ``INITIAL_CONTEXT_PREFIX``,
  which the spawn-side prune/dedupe boundaries reserve;
* ``worker_query`` is authoritative: a ``query`` key inside the context is
  dropped and reported via ``context_note``;
* a plain string context is raw briefing text (verbatim);
* actions that cannot carry a context report ``context_ignored`` instead of
  dropping it silently;
* with NO context, every delivered string and envelope is unchanged.

The worker thread/transport is stubbed; nothing here spawns a real worker.
"""

import json

import pytest

from tools.workspace import worker_thread as wt

STATUS_QUERY = "status?"


class _FakeThread:
    """Duck-typed stand-in for WorkerThread: records what it is sent."""

    def __init__(self, status="running", manual_only=False):
        self.status = status
        self._manual_only_pause = manual_only
        self._timeout_seconds = 10
        self._last_reasoning = None
        self.sent = []
        self.enqueued = []
        self.resume_calls = 0

    def is_alive(self):
        return True

    def send_query(self, query, timeout=None):
        self.sent.append(query)
        return "done"

    def _last_elapsed(self):
        return 1.0

    def enqueue_query(self, job_id, query):
        self.enqueued.append(query)

    def resume(self):
        self.resume_calls += 1
        return True


@pytest.fixture
def harness(monkeypatch):
    """Install *thread* as the resolved worker and neutralise disk lookups."""

    def _install(thread):
        monkeypatch.setattr(
            wt, "_resolve_worker_thread", lambda *a, **k: (thread, "sid", 1)
        )
        monkeypatch.setattr(wt, "_get_worker_job_registry", lambda: None)
        monkeypatch.setattr(
            wt.Worker, "_resolve_worker_workspace_id", lambda self: "ws-test"
        )
        monkeypatch.setattr(wt.Worker, "_load_workers", lambda self, ws_id: [])
        return thread

    return _install


def _run(action, **kwargs):
    return json.loads(wt.Worker(action=action, **kwargs).execute())


# ── the delivered string ──────────────────────────────────────────────────

def test_dict_context_is_prepended_on_query(harness):
    thread = harness(_FakeThread())

    _run("query", worker_query=STATUS_QUERY, context={"briefing": "already read X"})

    assert len(thread.sent) == 1
    sent = thread.sent[0]
    assert sent.startswith(wt.FOLLOWUP_CONTEXT_PREFIX + "\n")
    # The reserved initial-context marker must NOT be reused.
    assert wt.INITIAL_CONTEXT_PREFIX not in sent
    assert "already read X" in sent
    assert sent.endswith("\n\n" + STATUS_QUERY)


def test_dict_context_does_not_leak_its_query_key(harness):
    thread = harness(_FakeThread())

    _run("query", worker_query=STATUS_QUERY, context={"briefing": "b"})

    block = thread.sent[0].split("\n\n" + STATUS_QUERY)[0]
    assert '"query"' not in block


def test_string_context_is_used_verbatim_as_briefing(harness):
    thread = harness(_FakeThread())

    _run("query", worker_query=STATUS_QUERY, context="plain brief")

    assert thread.sent == [
        wt.FOLLOWUP_CONTEXT_PREFIX + "\nplain brief\n\n" + STATUS_QUERY
    ]


def test_no_context_delivers_worker_query_unchanged(harness):
    thread = harness(_FakeThread())

    _run("query", worker_query=STATUS_QUERY)

    assert thread.sent == [STATUS_QUERY]
    assert wt.FOLLOWUP_CONTEXT_PREFIX not in thread.sent[0]


def test_submit_query_prepends_context_to_the_queued_string(harness):
    thread = harness(_FakeThread())

    _run("submit_query", worker_query=STATUS_QUERY, context={"briefing": "b"})

    assert len(thread.enqueued) == 1
    assert thread.enqueued[0].startswith(wt.FOLLOWUP_CONTEXT_PREFIX + "\n")
    assert thread.enqueued[0].endswith("\n\n" + STATUS_QUERY)


def test_submit_query_without_context_is_byte_identical(harness):
    thread = harness(_FakeThread())

    _run("submit_query", worker_query=STATUS_QUERY)

    assert thread.enqueued == [STATUS_QUERY]


# ── worker_query wins ─────────────────────────────────────────────────────

def test_worker_query_beats_context_query_and_says_so(harness):
    thread = harness(_FakeThread())

    envelope = _run(
        "query",
        worker_query=STATUS_QUERY,
        context={"query": "some other task", "briefing": "b"},
    )

    assert thread.sent[0].endswith("\n\n" + STATUS_QUERY)
    assert "some other task" not in thread.sent[0]
    assert "worker_query wins" in envelope["context_note"]


def test_context_query_only_renders_no_block_but_notes_it(harness):
    thread = harness(_FakeThread())

    envelope = _run("query", worker_query=STATUS_QUERY, context={"query": "other"})

    assert thread.sent == [STATUS_QUERY]
    assert "worker_query wins" in envelope["context_note"]


# ── actions that cannot carry a context ───────────────────────────────────

@pytest.mark.parametrize("action", ["list", "check"])
def test_context_on_other_actions_is_reported_not_raised(harness, action):
    harness(_FakeThread())

    envelope = _run(action, worker_name="default", context={"briefing": "b"})

    assert envelope["context_ignored"] == (
        "context is only used by spawn/query/submit_query"
    )
    assert "error" not in envelope or envelope["context_ignored"]


def test_no_context_on_other_actions_has_no_context_ignored(harness):
    harness(_FakeThread())

    envelope = _run("list")

    assert "context_ignored" not in envelope


# ── paused path: never silently drop the context ──────────────────────────

def test_manually_paused_query_warns_that_context_was_not_delivered(harness):
    thread = harness(_FakeThread(status="paused", manual_only=True))

    envelope = _run("query", worker_query=STATUS_QUERY, context={"briefing": "b"})

    assert envelope["status"] == "paused"
    assert "NOT delivered" in envelope["context_note"]
    assert thread.sent == []


def test_manually_paused_query_without_context_envelope_unchanged(harness):
    harness(_FakeThread(status="paused", manual_only=True))

    envelope = _run("query", worker_query=STATUS_QUERY)

    assert envelope == {
        "status": "paused",
        "message": "Worker is manually paused",
        "worker_name": "default",
        "session_id": "",
    }
