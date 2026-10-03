"""Tests for the pure container-state resolver (``state.py``).

``state.py`` is the *reader* of a record's ``state`` field: a pure, read-only
function that reports a record's OWN effective state plus whether its container
is currently present live, and whether that reading is still *fresh*.
Rendering that vocabulary into a view state is the display layer's job.  This
module also builds (but never emits) the drift finding for a record stuck in
``creating``.

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


# ── Constants & exports ───────────────────────────────────────────────────────

def test_creating_expiry_constant_present():
    assert isinstance(state.CREATING_EXPIRY_SECONDS, int)
    assert state.CREATING_EXPIRY_SECONDS > 0


def test_new_drift_constants_present():
    assert drift.CLASS_LIFECYCLE == "lifecycle"
    assert drift.EVENT_CONTAINER_STUCK_CREATING == "drift.container_stuck_creating"


def test_new_drift_constants_exported():
    assert "CLASS_LIFECYCLE" in drift.__all__
    assert "EVENT_CONTAINER_STUCK_CREATING" in drift.__all__


def test_public_api_is_exported():
    for name in (
        "CREATING_EXPIRY_SECONDS",
        "resolve_container_state",
        "stuck_creating_finding",
    ):
        assert name in state.__all__


# ── Required resolver cases ───────────────────────────────────────────────────

def test_running_with_live_container_is_fresh():
    rec = _record(state=STATE_RUNNING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_present=True, now=NOW) == (
        STATE_RUNNING,
        True,
    )


def test_running_without_live_container_is_not_fresh():
    rec = _record(state=STATE_RUNNING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        STATE_RUNNING,
        False,
    )


def test_creating_past_expiry_without_live_container_is_not_fresh():
    old = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS + 1)
    rec = _record(state=STATE_CREATING, created_at=_iso(old))
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        STATE_CREATING,
        False,
    )


# ── Remaining creating / unset cases ──────────────────────────────────────────

def test_creating_within_expiry_without_live_container_is_fresh():
    recent = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS - 1)
    rec = _record(state=STATE_CREATING, created_at=_iso(recent))
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_creating_at_exact_expiry_is_fresh():
    edge = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS)
    rec = _record(state=STATE_CREATING, created_at=_iso(edge))
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_creating_with_live_container_is_fresh():
    rec = _record(state=STATE_CREATING, created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_present=True, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_unset_state_returns_empty_string():
    rec = _record(state="", created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        "",
        True,
    )


def test_unknown_recorded_state_returns_empty_string():
    rec = _record(state="paused", created_at=_iso(NOW))
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        "",
        True,
    )


# ── Malformed / absent timestamps never raise ─────────────────────────────────

@pytest.mark.parametrize("bad", ["", "not-a-date", None, "2026-13-40T99:99:99"])
def test_malformed_created_at_never_raises(bad):
    rec = _record(state=STATE_CREATING, created_at=bad)
    # indeterminate age -> fail toward *fresh* (never manufacture a stuck signal)
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        STATE_CREATING,
        True,
    )


def test_naive_created_at_is_accepted():
    naive = NOW.replace(tzinfo=None)
    rec = _record(state=STATE_CREATING, created_at=naive.isoformat())
    assert state.resolve_container_state(rec, live_present=False, now=NOW) == (
        STATE_CREATING,
        True,
    )


# ── Drift companion (builds, never emits) ─────────────────────────────────────

def test_stuck_creating_builds_drift_finding():
    old = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS + 1)
    rec = _record(state=STATE_CREATING, created_at=_iso(old))
    finding = state.stuck_creating_finding(rec, live_present=False, now=NOW)
    assert isinstance(finding, drift.DriftFinding)
    assert finding.drift_class == drift.CLASS_LIFECYCLE
    assert finding.event_type == drift.EVENT_CONTAINER_STUCK_CREATING
    assert finding.expected == STATE_CREATING
    assert finding.actual == ""
    assert finding.signature == drift.signature_for(
        drift.EVENT_CONTAINER_STUCK_CREATING, STATE_CREATING, ""
    )


def test_stuck_creating_is_none_when_not_stuck():
    recent = _record(
        state=STATE_CREATING, created_at=_iso(NOW - timedelta(seconds=1))
    )
    assert state.stuck_creating_finding(recent, live_present=False, now=NOW) is None
    running = _record(state=STATE_RUNNING, created_at=_iso(NOW))
    assert state.stuck_creating_finding(running, live_present=False, now=NOW) is None
    unset = _record(state="", created_at=_iso(NOW))
    assert state.stuck_creating_finding(unset, live_present=False, now=NOW) is None


def test_finding_is_frozen():
    old = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS + 1)
    rec = _record(state=STATE_CREATING, created_at=_iso(old))
    finding = state.stuck_creating_finding(rec, live_present=False, now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.expected = "changed"  # type: ignore[misc]


# ── Purity ────────────────────────────────────────────────────────────────────

def test_resolver_is_deterministic_and_does_not_mutate_record():
    old = NOW - timedelta(seconds=state.CREATING_EXPIRY_SECONDS + 1)
    rec = _record(state=STATE_CREATING, created_at=_iso(old))
    before = dict(vars(rec))
    first = state.resolve_container_state(rec, live_present=False, now=NOW)
    second = state.resolve_container_state(rec, live_present=False, now=NOW)
    assert first == second
    assert dict(vars(rec)) == before
