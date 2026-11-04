"""Unit tests for the fenced ``inflight.json`` journal.

Pure filesystem tests against ``tmp_path`` -- no worker thread, no container, no
second process, no sleeps. The module's whole contract is one file's atomics
plus one fence parse, so a real directory is the whole fixture.

What is pinned here:

* round trip: what :func:`write_inflight` puts on disk is what
  :func:`read_inflight` gives back, and :func:`clear_inflight` removes it;
* the payload SHAPE (the field list the later write points will speak);
* ``missing``, ``corrupt`` and ``foreign-fenced`` all read as ``None`` -- the
  three cases are indistinguishable in behaviour and distinguishable only in
  the log;
* nothing is deleted that this incarnation does not own: a fenced-away record
  survives both a read and a clear;
* the write is atomic (a failure mid-write leaves the old record intact and no
  temp debris) and interleaved writes still leave one parseable record;
* the ADOPTION reader (:func:`read_inflight_unfenced`) sees a dead
  incarnation's record -- the one case the fenced reader must quarantine --
  while the fenced reader keeps refusing that same record;
* the write FAILS CLOSED on a foreign record that is still work in progress:
  the caller gets a reason and the file stays BYTE-IDENTICAL, so only a
  ``cancelled`` husk and an unusable file are ever displaced;
* :func:`adopt_inflight` is the ONE displacing writer: it takes over a dead
  incarnation's record, verifies the attempt count follows on from the stored
  one, and refuses -- writing nothing -- on nothing-to-adopt, ``cancelled``,
  already-``adopted``, its own record and an unusable journal.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import tools.workspace.inflight_journal as journal
from tools.workspace.command_channel import CommandFence
from tools.workspace.inflight_journal import (
    INFLIGHT_FILENAME,
    MAX_ATTEMPTS,
    SCHEMA_VERSION,
    STATUS_ADOPTED,
    STATUS_CANCELLED,
    STATUS_INFLIGHT,
    STATUS_RECOVERABLE,
    InflightRecord,
    adopt_inflight,
    clear_inflight,
    journal_path,
    read_inflight,
    read_inflight_unfenced,
    write_inflight,
)

FENCE = CommandFence(session_id="sess-a", worker_name="w1", instance_id=1, epoch=3)

#: The SAME worker directory, one generation earlier -- a dead incarnation.
STALE_FENCE = replace(FENCE, epoch=FENCE.epoch - 1)

STAMP = "2026-01-01T00:00:00+00:00"


def _record(**overrides) -> InflightRecord:
    fields = dict(
        query_id="q-1",
        query="do the thing",
        status=STATUS_INFLIGHT,
        attempts=1,
        requested_by="send_query",
        created_at=STAMP,
        updated_at=STAMP,
    )
    fields.update(overrides)
    return InflightRecord(**fields)


def _names(worker_dir: Path) -> list:
    """Every entry in the worker dir -- temp files included."""
    return sorted(p.name for p in worker_dir.iterdir())


def _write_raw(worker_dir: Path, text: str) -> Path:
    path = journal_path(worker_dir)
    path.write_text(text, encoding="utf-8")
    return path


class _Proxy:
    """Stand-in for a module-level name that hooks ONE call of its target.

    ``hooks`` maps an attribute name to a callable; the first access to that
    attribute returns a wrapper that calls the hook and then the real
    attribute. Each hook fires once, so a hook that triggers another
    ``write_inflight`` cannot recurse forever.
    """

    def __init__(self, real, hooks):
        self._real = real
        self._hooks = dict(hooks)

    def __getattr__(self, name):
        target = getattr(self._real, name)
        hook = self._hooks.pop(name, None)
        if hook is None:
            return target

        def wrapper(*args, **kwargs):
            hook(*args, **kwargs)
            return target(*args, **kwargs)

        return wrapper


# -- round trip ---------------------------------------------------------------


def test_write_read_clear_round_trip(tmp_path: Path) -> None:
    record = _record(last_tool={"name": "ReadFile", "args_hash": "ab12"})

    assert write_inflight(tmp_path, record, FENCE) is None
    assert journal_path(tmp_path).is_file()

    read_back, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert read_back == record
    assert read_back.last_tool == {"name": "ReadFile", "args_hash": "ab12"}

    assert clear_inflight(tmp_path, FENCE) is None
    assert not journal_path(tmp_path).exists()
    # ...and clearing an empty journal is idempotent, not an error.
    assert clear_inflight(tmp_path, FENCE) is None


def test_status_and_attempts_survive_the_round_trip(tmp_path: Path) -> None:
    record = _record(status=STATUS_RECOVERABLE, attempts=MAX_ATTEMPTS,
                     reason="parked by detach")
    assert write_inflight(tmp_path, record, FENCE) is None
    read_back, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert read_back.status == STATUS_RECOVERABLE
    assert read_back.attempts == MAX_ATTEMPTS
    assert read_back.reason == "parked by detach"


def test_payload_shape_is_pinned(tmp_path: Path) -> None:
    """The field list the later write points must speak, in one place."""
    assert write_inflight(tmp_path, _record(), FENCE) is None
    payload = json.loads(journal_path(tmp_path).read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema",
        "query_id",
        "query",
        "status",
        "attempts",
        "requested_by",
        "last_tool",
        "reason",
        "created_at",
        "updated_at",
        # the four fence axes, merged in from the CommandFence
        "session_id",
        "worker_name",
        "instance_id",
        "epoch",
    }
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["epoch"] == FENCE.epoch
    assert payload["instance_id"] == FENCE.instance_id
    assert payload["status"] == STATUS_INFLIGHT
    assert payload["last_tool"] is None
    assert payload["reason"] is None


# -- absent / corrupt ---------------------------------------------------------


def test_missing_journal_reads_as_none(tmp_path: Path) -> None:
    assert read_inflight(tmp_path, FENCE) == (None, None)
    assert clear_inflight(tmp_path, FENCE) is None
    assert _names(tmp_path) == []


def test_corrupt_journal_reads_as_none_without_raising(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, '{"query_id": "q-1", "query": "half a rec')

    record, reason = read_inflight(tmp_path, FENCE)

    assert record is None
    assert reason is not None and "corrupt" in reason
    # Evidence, not garbage: the unusable file is left on disk.
    assert path.is_file()
    # ...and an unauthorised delete is refused for the same reason.
    clear_reason = clear_inflight(tmp_path, FENCE)
    assert clear_reason is not None and "corrupt" in clear_reason
    assert path.is_file()


def test_non_object_payload_reads_as_corrupt(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, '["not", "a", "record"]')
    record, reason = read_inflight(tmp_path, FENCE)
    assert record is None
    assert reason is not None
    assert path.is_file()


def test_malformed_record_is_reported_not_used(tmp_path: Path) -> None:
    """A fenced-but-unusable record: right owner, wrong contents."""
    payload = _record().to_payload(FENCE)
    payload["status"] = "interrupted"  # the JOB's status, not a journal one
    _write_raw(tmp_path, json.dumps(payload))

    record, reason = read_inflight(tmp_path, FENCE)
    assert record is None
    assert reason is not None and "malformed" in reason


# -- atomicity ----------------------------------------------------------------


def test_write_is_atomic_and_cleans_up_its_temp_file(tmp_path, monkeypatch) -> None:
    first = _record(query_id="q-1")
    assert write_inflight(tmp_path, first, FENCE) is None

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "os", _Proxy(journal.os, {"fsync": boom}))
    second = _record(query_id="q-2", query="the turn that never landed")
    reason = write_inflight(tmp_path, second, FENCE)

    assert reason is not None and "cannot write" in reason
    # The old record is intact and parseable: no half-written file was exposed.
    read_back, read_reason = read_inflight(tmp_path, FENCE)
    assert read_reason is None
    assert read_back.query_id == "q-1"
    # ...and the failed write left no debris.
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


def test_replace_failure_also_leaves_no_debris(tmp_path, monkeypatch) -> None:
    assert write_inflight(tmp_path, _record(query_id="q-1"), FENCE) is None

    def boom(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(journal, "os", _Proxy(journal.os, {"replace": boom}))
    assert write_inflight(tmp_path, _record(query_id="q-2"), FENCE) is not None
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


def test_temp_file_is_created_in_the_worker_dir(tmp_path, monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        journal,
        "tempfile",
        _Proxy(journal.tempfile, {"mkstemp": lambda *a, **k: captured.update(k)}),
    )
    assert write_inflight(tmp_path, _record(), FENCE) is None
    # Same directory, so os.replace cannot cross a filesystem boundary.
    assert captured["dir"] == str(tmp_path)
    assert captured["prefix"] == ".%s." % INFLIGHT_FILENAME
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


# -- fence -------------------------------------------------------------------


def test_read_quarantines_a_stale_epoch_record(tmp_path: Path) -> None:
    assert write_inflight(tmp_path, _record(), FENCE) is None

    record, reason = read_inflight(tmp_path, STALE_FENCE)

    assert record is None
    # The reason is fence_rejection_reason's own vocabulary, naming the axis.
    assert reason == "epoch %r != %r" % (FENCE.epoch, STALE_FENCE.epoch)
    # Quarantined, NOT deleted: the live incarnation's record survives.
    assert journal_path(tmp_path).is_file()
    assert read_inflight(tmp_path, FENCE)[0] is not None


def test_read_quarantines_a_record_with_no_fence(tmp_path: Path) -> None:
    payload = _record().to_payload(FENCE)
    for axis in ("session_id", "worker_name", "instance_id", "epoch"):
        payload.pop(axis)
    _write_raw(tmp_path, json.dumps(payload))

    record, reason = read_inflight(tmp_path, FENCE)
    assert record is None
    assert reason == "no fence in payload"
    assert journal_path(tmp_path).is_file()


def test_clear_refuses_a_stale_epoch_record_and_leaves_it_intact(tmp_path) -> None:
    record = _record(query_id="q-live")
    assert write_inflight(tmp_path, record, FENCE) is None

    reason = clear_inflight(tmp_path, STALE_FENCE)

    assert reason == "epoch %r != %r" % (FENCE.epoch, STALE_FENCE.epoch)
    read_back, read_reason = read_inflight(tmp_path, FENCE)
    assert read_reason is None
    assert read_back == record


def test_clear_refuses_a_record_with_no_fence(tmp_path: Path) -> None:
    _write_raw(tmp_path, json.dumps({"query_id": "q-1", "status": STATUS_CANCELLED}))
    reason = clear_inflight(tmp_path, FENCE)
    assert reason == "no fence in payload"
    assert journal_path(tmp_path).is_file()


# -- interleaving -------------------------------------------------------------


def test_two_sequential_writes_leave_one_parseable_record(tmp_path: Path) -> None:
    assert write_inflight(tmp_path, _record(query_id="q-1"), FENCE) is None
    assert write_inflight(tmp_path, _record(query_id="q-2"), FENCE) is None
    record, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert record.query_id == "q-2"
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


def test_interleaved_writes_still_leave_one_parseable_record(tmp_path, monkeypatch):
    """Write A is overtaken by a complete write B before its own replace."""

    def overtaken(*args, **kwargs):
        assert write_inflight(tmp_path, _record(query_id="q-b"), FENCE) is None

    monkeypatch.setattr(journal, "os", _Proxy(journal.os, {"replace": overtaken}))
    assert write_inflight(tmp_path, _record(query_id="q-a"), FENCE) is None

    record, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert record.query_id in ("q-a", "q-b")  # last replace wins; both are whole
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


# -- fail-closed displacement --------------------------------------------------


def test_write_displaces_a_foreign_cancelled_husk(tmp_path: Path) -> None:
    """The one live-looking leftover a write may sweep: a killed job's husk."""
    husk = _record(query_id="q-dead", status=STATUS_CANCELLED)
    assert write_inflight(tmp_path, husk, STALE_FENCE) is None
    assert write_inflight(tmp_path, _record(query_id="q-live"), FENCE) is None

    record, axes, reason = read_inflight_unfenced(tmp_path)
    assert reason is None
    assert record.query_id == "q-live"
    assert axes == FENCE.to_payload()

    live, live_reason = read_inflight(tmp_path, FENCE)
    assert live_reason is None
    assert live.query_id == "q-live"
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


@pytest.mark.parametrize(
    "status",
    [STATUS_INFLIGHT, STATUS_RECOVERABLE, STATUS_ADOPTED],
)
def test_write_refuses_to_displace_work_in_progress(
    tmp_path: Path,
    status: str,
) -> None:
    """Fail closed: a dead incarnation's record is WORK, not litter."""
    dead = _record(query_id="q-dead", status=status)
    assert write_inflight(tmp_path, dead, STALE_FENCE) is None
    path = journal_path(tmp_path)
    before = path.read_bytes()

    reason = write_inflight(tmp_path, _record(query_id="q-live"), FENCE)

    assert reason is not None
    assert reason.startswith(journal._REFUSAL_PREFIX)
    assert "status=%r" % (status,) in reason
    assert "epoch %r != %r" % (STALE_FENCE.epoch, FENCE.epoch) in reason
    # Nothing was written: not even a temp file, not one byte.
    assert path.read_bytes() == before
    assert _names(tmp_path) == [INFLIGHT_FILENAME]

    stored, axes, read_reason = read_inflight_unfenced(tmp_path)
    assert read_reason is None
    assert stored == dead
    assert axes == STALE_FENCE.to_payload()


def test_write_displaces_an_unusable_journal(tmp_path: Path) -> None:
    """A corrupt payload carries no fence, so it defends no rights."""
    _write_raw(tmp_path, '{"query_id": "q-dead", "query": "half a rec')

    assert write_inflight(tmp_path, _record(query_id="q-live"), FENCE) is None

    record, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert record.query_id == "q-live"
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


def test_write_updates_its_own_record(tmp_path: Path) -> None:
    """A record fenced to the caller is an ordinary update, not a displacement."""
    assert write_inflight(tmp_path, _record(status=STATUS_INFLIGHT), FENCE) is None
    assert write_inflight(tmp_path, _record(status=STATUS_RECOVERABLE), FENCE) is None

    record, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert record.status == STATUS_RECOVERABLE


def test_write_into_a_fresh_dir_is_allowed(tmp_path: Path) -> None:
    assert _names(tmp_path) == []
    assert write_inflight(tmp_path, _record(query_id="q-live"), FENCE) is None

    record, reason = read_inflight(tmp_path, FENCE)
    assert reason is None
    assert record.query_id == "q-live"


# -- record shape -------------------------------------------------------------


def test_interrupted_is_not_a_journal_status() -> None:
    """R3: ``interrupted`` is the JOB's terminal status, never a record's."""
    with pytest.raises(ValueError):
        _record(status="interrupted")
    with pytest.raises(ValueError):
        _record(status="abandoned")


def test_attempts_are_capped() -> None:
    with pytest.raises(ValueError):
        _record(attempts=MAX_ATTEMPTS + 1)
    payload = _record().to_payload(FENCE)
    payload["attempts"] = MAX_ATTEMPTS + 1
    assert InflightRecord.from_payload(payload) is None


def test_unknown_schema_is_refused() -> None:
    payload = _record().to_payload(FENCE)
    payload["schema"] = SCHEMA_VERSION + 1
    assert InflightRecord.from_payload(payload) is None


def test_bool_is_not_an_attempt_count() -> None:
    payload = _record().to_payload(FENCE)
    payload["attempts"] = True
    assert InflightRecord.from_payload(payload) is None


def test_from_payload_matches_the_constructor() -> None:
    record = _record(last_tool={"name": "SearchCodebaseTool", "args_hash": "ff00"})
    assert InflightRecord.from_payload(record.to_payload(FENCE)) == record


# -- unfenced (adoption) read -------------------------------------------------


def test_unfenced_read_sees_a_dead_incarnations_record(tmp_path: Path) -> None:
    """The whole point: adoption is cross-epoch, so the fence must not gate it."""
    assert write_inflight(tmp_path, _record(query_id="q-dead"), STALE_FENCE) is None

    record, axes, reason = read_inflight_unfenced(tmp_path)

    assert reason is None
    assert record is not None and record.query_id == "q-dead"
    # Axes are REPORTED, raw -- the adopter must be able to name the dead one.
    assert axes == STALE_FENCE.to_payload()
    assert axes["epoch"] == STALE_FENCE.epoch
    assert journal_path(tmp_path).is_file()


def test_unfenced_read_with_no_journal(tmp_path: Path) -> None:
    assert read_inflight_unfenced(tmp_path) == (None, None, None)
    assert _names(tmp_path) == []


def test_unfenced_read_reports_a_corrupt_journal_and_keeps_it(tmp_path) -> None:
    path = _write_raw(tmp_path, '{"query_id": "q-1", "query": "half a rec')

    record, axes, reason = read_inflight_unfenced(tmp_path)

    assert record is None
    assert axes is None  # nothing extractable, and nothing guessed
    assert reason is not None and "corrupt" in reason
    assert path.is_file()


def test_unfenced_read_reports_a_malformed_record_but_names_its_fence(
    tmp_path: Path,
) -> None:
    """A fenced-but-unusable record: no record, but the axes still come back."""
    payload = _record().to_payload(STALE_FENCE)
    payload["attempts"] = MAX_ATTEMPTS + 1
    path = _write_raw(tmp_path, json.dumps(payload))

    record, axes, reason = read_inflight_unfenced(tmp_path)

    assert record is None
    assert reason == journal._MALFORMED_REASON
    assert axes == STALE_FENCE.to_payload()
    assert path.is_file()


def test_only_adoption_can_read_what_the_fenced_reader_quarantines(tmp_path) -> None:
    """Both halves of one record: visible to the adopter, refused when fenced."""
    parked = _record(query_id="q-dead", status=STATUS_RECOVERABLE)
    assert write_inflight(tmp_path, parked, STALE_FENCE) is None
    path = journal_path(tmp_path)

    fenced_record, fenced_reason = read_inflight(tmp_path, FENCE)

    assert fenced_record is None
    assert fenced_reason == "epoch %r != %r" % (STALE_FENCE.epoch, FENCE.epoch)
    assert path.is_file()

    record, axes, reason = read_inflight_unfenced(tmp_path)

    assert reason is None
    assert record == parked
    assert record.status == STATUS_RECOVERABLE
    assert axes["epoch"] == STALE_FENCE.epoch
    # Reading is not taking over: neither reader moved or removed the record.
    assert path.is_file()


# -- adoption ----------------------------------------------------------------


def _parked(**overrides) -> InflightRecord:
    """What a dead incarnation leaves behind: a restartable record, unparked."""
    fields = dict(query_id="q-dead", status=STATUS_RECOVERABLE, attempts=0)
    fields.update(overrides)
    return _record(**fields)


def test_adopt_takes_over_a_dead_incarnations_record(tmp_path: Path) -> None:
    """The one displacing writer: restamped to the successor, status adopted."""
    parked = _parked()
    assert write_inflight(tmp_path, parked, STALE_FENCE) is None

    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(query_id="q-dead", attempts=1))

    assert reason is None
    assert adopted is not None
    assert adopted.query_id == "q-dead"
    assert adopted.status == STATUS_ADOPTED
    assert adopted.attempts == 1  # the caller counted; the primitive verified
    assert adopted.updated_at != STAMP  # refreshed, not carried over

    stored, axes, read_reason = read_inflight_unfenced(tmp_path)
    assert read_reason is None
    assert stored == adopted
    assert axes == FENCE.to_payload()

    live, live_reason = read_inflight(tmp_path, FENCE)
    assert live_reason is None
    assert live == adopted
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


def test_adopt_refuses_when_there_is_nothing_to_adopt(tmp_path: Path) -> None:
    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(attempts=1))

    assert adopted is None
    assert reason == journal._ADOPT_NOTHING_REASON
    assert _names(tmp_path) == []  # a read, not a write: no dir, no file


def test_adopt_refuses_a_cancelled_record(tmp_path: Path) -> None:
    """R3/R6: a killed job is never resurrected, not even by an adopter."""
    killed = _record(query_id="q-killed", status=STATUS_CANCELLED, attempts=1)
    assert write_inflight(tmp_path, killed, STALE_FENCE) is None
    path = journal_path(tmp_path)
    before = path.read_bytes()

    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(query_id="q-killed", attempts=2))

    assert adopted is None
    assert reason == journal._ADOPT_CANCELLED_REASON
    assert path.read_bytes() == before


def test_adopt_refuses_an_already_adopted_record(tmp_path: Path) -> None:
    """Somebody else took it: double-adopting would replay the turn twice."""
    taken = _record(query_id="q-taken", status=STATUS_ADOPTED, attempts=1)
    assert write_inflight(tmp_path, taken, STALE_FENCE) is None
    path = journal_path(tmp_path)
    before = path.read_bytes()

    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(query_id="q-taken", attempts=2))

    assert adopted is None
    assert reason == journal._ADOPT_ALREADY_REASON
    assert path.read_bytes() == before


def test_adopt_refuses_its_own_record(tmp_path: Path) -> None:
    """Same-incarnation adoption is a caller bug -- write_inflight covers that."""
    mine = _record(query_id="q-mine", status=STATUS_RECOVERABLE, attempts=0)
    assert write_inflight(tmp_path, mine, FENCE) is None
    path = journal_path(tmp_path)
    before = path.read_bytes()

    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(query_id="q-mine", attempts=1))

    assert adopted is None
    assert reason == journal._ADOPT_OWN_REASON
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "passed, stored",
    [(1, 1), (0, 3), (2, 0)],
)
def test_adopt_refuses_an_attempt_count_that_does_not_follow_on(
    tmp_path: Path,
    passed: int,
    stored: int,
) -> None:
    """A stale or jumping count is a lost update: refuse and name both numbers."""
    assert write_inflight(tmp_path, _parked(attempts=stored), STALE_FENCE) is None
    path = journal_path(tmp_path)
    before = path.read_bytes()

    adopted, reason = adopt_inflight(
        tmp_path,
        FENCE,
        _record(query_id="q-dead", attempts=passed),
    )

    assert adopted is None
    assert reason == journal._ADOPT_ATTEMPTS_REASON % (passed, stored)
    assert str(passed) in reason and str(stored) in reason
    assert path.read_bytes() == before


def test_adopt_refuses_an_unusable_journal(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, '{"query_id": "q-dead", "query": "half a rec')
    before = path.read_bytes()

    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(attempts=1))

    assert adopted is None
    assert reason == journal._ADOPT_UNUSABLE_REASON % journal._CORRUPT_JSON_REASON
    assert path.read_bytes() == before
    assert _names(tmp_path) == [INFLIGHT_FILENAME]


def test_writes_still_work_after_an_adoption(tmp_path: Path) -> None:
    """The take-over leaves the adopter's own fenced writes untouched."""
    assert write_inflight(tmp_path, _parked(), STALE_FENCE) is None
    adopted, reason = adopt_inflight(tmp_path, FENCE, _record(query_id="q-dead", attempts=1))
    assert reason is None and adopted is not None

    assert write_inflight(tmp_path, _parked(attempts=1), FENCE) is None

    record, read_reason = read_inflight(tmp_path, FENCE)
    assert read_reason is None
    assert record.status == STATUS_RECOVERABLE
    assert _names(tmp_path) == [INFLIGHT_FILENAME]
