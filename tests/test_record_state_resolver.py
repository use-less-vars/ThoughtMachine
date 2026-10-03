"""Tests for the pure container-state resolver (``state.py``).

``state.py`` is the *reader* of a record's ``state`` field: a pure, read-only
function that reports a record's OWN effective state plus a freshness verdict
derived from the *live-container report* and, for the absent case, the record's
age.  Rendering that vocabulary into a view state is the display layer's job.
This module also builds (but never emits) the drift finding for a record whose
recorded ``creating`` claim is stale.

The live world reaches the resolver through a single ``live_state`` keyword:
``None`` = no container exists; a non-``None`` string = a container exists whose
Docker state is that string (trimmed, case-insensitive; empty -> the unknown
sentinel).

These tests are *pure*: no vault, no Docker, no clock -- ``now`` is injected.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from thoughtmachine.container_record import drift, state
from thoughtmachine.container_record.models import STATE_CREATING, STATE_RUNNING

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _record(**overrides):
    """Build a minimal record-like object for the pure resolver."""
    base = {"id": "rec-1", "state": "", "created_at": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _old(**kwargs):
    """A ``created_at`` comfortably PAST the creating-expiry window."""
    return _iso(NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS + 1))


def _recent(**kwargs):
    """A ``created_at`` comfortably WITHIN the creating-expiry window."""
    return _iso(NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS - 1))


# ── Constants & exports ───────────────────────────────────────────────────────

def test_creating_expiry_constant_present():
    assert isinstance(state.CREATING_EXPIRY_SECONDS, int)
    assert state.CREATING_EXPIRY_SECONDS > 0


def test_live_state_unknown_sentinel_present():
    assert state.LIVE_STATE_UNKNOWN == "unknown"


def test_new_drift_constants_present():
    assert drift.CLASS_LIFECYCLE == "lifecycle"
    assert drift.EVENT_CONTAINER_STUCK_CREATING == "drift.container_stuck_creating"


def test_new_drift_constants_exported():
    assert "CLASS_LIFECYCLE" in drift.__all__
    assert "EVENT_CONTAINER_STUCK_CREATING" in drift.__all__


def test_public_api_is_exported():
    assert state.__all__ == [
        "CREATING_EXPIRY_SECONDS",
        "LIVE_STATE_UNKNOWN",
        "resolve_container_state",
        "creating_drift_finding",
    ]


# ── Truth table: recorded running ─────────────────────────────────────────────

@pytest.mark.parametrize("live", [None, "running", "created", "exited", "unknown"])
def test_running_record_fresh_iff_container_present(live):
    rec = _record(state=STATE_RUNNING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_state=live, now=NOW) == (
        STATE_RUNNING,
        live is not None,
    )


# ── Truth table: recorded creating ────────────────────────────────────────────

def test_creating_absent_within_expiry_is_fresh():
    rec = _record(state=STATE_CREATING, created_at=_recent())
    assert state.resolve_container_state(rec, live_state=None, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_creating_absent_at_exact_expiry_is_fresh():
    edge = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS)
    rec = _record(state=STATE_CREATING, created_at=_iso(edge))
    assert state.resolve_container_state(rec, live_state=None, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_creating_absent_past_expiry_is_stale():
    rec = _record(state=STATE_CREATING, created_at=_old())
    assert state.resolve_container_state(rec, live_state=None, now=NOW) == (
        STATE_CREATING,
        False,
    )


@pytest.mark.parametrize("live", ["created", "creating", "CREATING", "  created "])
def test_creating_with_creating_container_is_fresh(live):
    rec = _record(state=STATE_CREATING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_state=live, now=NOW) == (
        STATE_CREATING,
        True,
    )


@pytest.mark.parametrize("live", ["running", "exited", "dead", "restarting"])
def test_creating_with_non_creating_container_is_stale(live):
    # A live container in any non-creating state CONTRADICTS a ``creating``
    # record -- and the expiry window no longer applies once a container exists.
    rec = _record(state=STATE_CREATING, created_at=_recent())
    assert state.resolve_container_state(rec, live_state=live, now=NOW) == (
        STATE_CREATING,
        False,
    )


def test_creating_empty_string_live_state_is_stale():
    # An empty string is the unknown sentinel (exists, unreadable), NOT absence.
    rec = _record(state=STATE_CREATING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_state="", now=NOW) == (
        STATE_CREATING,
        False,
    )


@pytest.mark.parametrize("live", ["running", "created"])
def test_running_record_any_present_container_is_fresh(live):
    rec = _record(state=STATE_RUNNING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_state=live, now=NOW) == (
        STATE_RUNNING,
        True,
    )


# ── Truth table: unset / unknown recorded state ───────────────────────────────

@pytest.mark.parametrize("live", [None, "running", ""])
def test_unset_state_returns_empty_string(live):
    rec = _record(state="", created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_state=live, now=NOW) == ("", True)


@pytest.mark.parametrize("live", [None, "running", ""])
def test_unknown_recorded_state_returns_empty_string(live):
    rec = _record(state="paused", created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_state=live, now=NOW) == ("", True)


# ── Malformed / absent timestamps never raise (absent case only) ──────────────

@pytest.mark.parametrize("bad", ["", "not-a-date", None, "2026-13-40T99:99:99"])
def test_malformed_created_at_never_raises(bad):
    rec = _record(state=STATE_CREATING, created_at=bad)
    # indeterminate age (absent container) -> fail toward *fresh*
    assert state.resolve_container_state(rec, live_state=None, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_naive_created_at_is_accepted():
    naive = NOW.replace(tzinfo=None)
    rec = _record(state=STATE_CREATING, created_at=naive.isoformat())
    assert state.resolve_container_state(rec, live_state=None, now=NOW) == (
        STATE_CREATING,
        True,
    )


# ── Drift companion (builds, never emits) ─────────────────────────────────────

def test_creating_drift_finding_for_absent_past_expiry():
    rec = _record(state=STATE_CREATING, created_at=_old())
    finding = state.creating_drift_finding(rec, live_state=None, now=NOW)
    assert isinstance(finding, drift.DriftFinding)
    assert finding.drift_class == drift.CLASS_LIFECYCLE
    assert finding.event_type == drift.EVENT_CONTAINER_STUCK_CREATING
    assert finding.expected == STATE_CREATING
    assert finding.actual == ""
    assert finding.fresh is False
    # HISTORICAL signature: byte-identical to the pre-change absent digest.
    assert finding.signature == drift.signature_for(
        drift.EVENT_CONTAINER_STUCK_CREATING, STATE_CREATING, ""
    )


def test_creating_drift_finding_for_live_running_has_distinct_signature():
    rec = _record(state=STATE_CREATING, created_at=_iso(NOW))
    finding = state.creating_drift_finding(rec, live_state="running", now=NOW)
    assert isinstance(finding, drift.DriftFinding)
    assert finding.actual == "running"
    assert finding.fresh is False
    assert finding.signature == drift.signature_for(
        drift.EVENT_CONTAINER_STUCK_CREATING, STATE_CREATING, "running"
    )
    # The two causes MUST carry DIFFERENT dedup signatures.
    absent = state.creating_drift_finding(
        _record(state=STATE_CREATING, created_at=_old()), live_state=None, now=NOW
    )
    assert finding.signature != absent.signature


def test_creating_drift_finding_normalises_live_state():
    rec = _record(state=STATE_CREATING, created_at=_iso(NOW))
    finding = state.creating_drift_finding(rec, live_state="  RUNNING  ", now=NOW)
    assert finding is not None
    assert finding.actual == "running"


@pytest.mark.parametrize(
    "record,live",
    [
        (_record(state=STATE_CREATING, created_at=_iso(NOW)), "created"),
        (_record(state=STATE_CREATING, created_at=_recent()), None),
        (_record(state=STATE_RUNNING, created_at=_iso(NOW)), None),
        (_record(state="", created_at=_iso(NOW)), None),
    ],
)
def test_creating_drift_finding_is_none_when_not_stale(record, live):
    assert state.creating_drift_finding(record, live_state=live, now=NOW) is None


def test_finding_is_frozen():
    rec = _record(state=STATE_CREATING, created_at=_old())
    finding = state.creating_drift_finding(rec, live_state=None, now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.expected = "changed"  # type: ignore[misc]


# ── Purity ────────────────────────────────────────────────────────────────────

def test_resolver_is_deterministic_and_does_not_mutate_record():
    rec = _record(state=STATE_CREATING, created_at=_old())
    before = dict(vars(rec))
    first = state.resolve_container_state(rec, live_state=None, now=NOW)
    second = state.resolve_container_state(rec, live_state=None, now=NOW)
    assert first == second
    assert dict(vars(rec)) == before
