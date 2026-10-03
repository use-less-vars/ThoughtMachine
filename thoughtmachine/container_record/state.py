"""Pure container-state resolution (container-record subsystem).

:func:`resolve_container_state` is the *reader* of a record's ``state`` field
(``models.Record.state``).  Given a record and the live Docker state of its
container, it returns ``(state, fresh)``:

* ``state`` -- the record's OWN effective state: ``"running"``, ``"creating"``,
  or ``""`` (empty = the record is unset / makes no claim).  These are
  RECORD-subsystem values, NOT rendered view states: this core module never
  invents a display label.  Turning an unset ``""`` into the display label
  ``"stopped"`` is the DISPLAY layer's job -- the workspace container-view
  path does exactly that in
  ``web_ui.backend.server._map_container_view_state``.
* ``fresh`` -- whether that reading can still be trusted.  ``False`` marks a
  recorded state the live world has contradicted: a container the record calls
  ``running`` that is absent, or a record whose recorded ``creating`` claim is
  contradicted by the live container (no container past its window, or a
  container that exists in a non-``creating`` state); ``True`` means fresh /
  not (yet) known-stale.

Both :func:`resolve_container_state` and :func:`creating_drift_finding` are
**pure and read-only**: neither performs any IO -- no Docker, no filesystem, no
record store, no event emission, no clock (``now`` is injected).  All IO (the
``docker inspect`` / record load) is the caller's.  :func:`creating_drift_finding`
*returns* a :class:`~thoughtmachine.container_record.drift.DriftFinding` for the
stuck-``creating`` case; it never emits it (emission stays in ``drift.py``).

Input domain -- the live-container report
-----------------------------------------
The live world is reported to the resolver through the single ``live_state``
keyword:

* ``None`` -- NO live container exists (the ONLY way to signal absence).
* any non-``None`` string -- a live container EXISTS; the value is its Docker
  state, compared trimmed and case-insensitively.  The recognised values
  ``{"created", "creating"}`` mean the container is still being created
  (LIVE_CREATING); every other value (``"running"``, ``"restarting"``,
  ``"exited"``, ``"dead"``, ``"paused"``, ``"removing"``, ``"stopped"``,
  ``"error"``, ...) is exists-but-not-creating.
* an empty string ``""`` is normalised to the sentinel :data:`LIVE_STATE_UNKNOWN`
  (a live container whose state could not be read) so that "absent" is only
  ever signalled by ``None``, never by an empty string.

Freshness model
---------------
Only two recorded states carry a freshness judgement; every other value is
returned unchanged with ``fresh=True``.  The verdict depends on the recorded
state *and* on the live report:

* ``running`` -- fresh iff a live container exists (``live_state is not None``);
  a record that says ``running`` while no container exists is stale.  Any
  existing container satisfies the claim regardless of its own live state.
* ``creating`` -- fresh iff the live container is itself still creating
  (normalised ``live_state`` in LIVE_CREATING).  When NO container exists the
  record is stale only once it is past :data:`CREATING_EXPIRY_SECONDS` of
  ``created_at`` (inside the window it is still fresh); when a container DOES
  exist but is in any other state the record is immediately stale -- the live
  world contradicts the recorded ``creating`` claim.
* unset (``""``) or any other/unknown value -- the record makes no claim we
  can contradict, so nothing can be stale: the value is returned as-is with
  ``fresh=True``.  We deliberately do NOT synthesise a staleness signal from an
  absence of information (that would flag every record created before its
  container is attached, and every legacy record that predates the field).

Age is measured from ``created_at``: ``api.begin_record`` writes
``state="creating"`` and ``created_at=now`` in the same call, so ``created_at``
is the moment the record entered ``creating``.  The age/expiry rule applies
ONLY to the absent case (``live_state is None``); a live container is judged on
its live state alone.  A missing or malformed ``created_at`` yields an
*indeterminate* age, which fails toward ``fresh=True``: an unreadable
timestamp must never manufacture a "stuck" signal.

Emission decision
-----------------
This module only *builds* findings (:func:`creating_drift_finding`); deciding
whether and when to emit them -- including the age/expiry policy -- belongs to
the CALLER.  A stale ``creating`` reading (``fresh=False``) yields a finding
whose ``actual`` is ``""`` for the absent cause and the normalised live state
for the contradiction cause, so the two causes carry DIFFERENT dedup
signatures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import drift
from .drift import DriftFinding
from .models import STATE_CREATING, STATE_RUNNING

__all__ = [
    "CREATING_EXPIRY_SECONDS",
    "LIVE_STATE_UNKNOWN",
    "resolve_container_state",
    "creating_drift_finding",
]

#: How long a record may stay in ``creating`` with no live container before it
#: is considered STUCK (seconds).
#:
#: **Judgement call -- no repo provenance.**  No create/start budget, admission
#: timeout or start-script timeout is documented anywhere in the tree (grepped
#: ``thoughtmachine/`` and ``docs/``); the only related figure the repo pins is
#: ``timeout_constants.IDLE_TIMEOUT_SECONDS = 600`` -- an *idle* constant, not a
#: create budget.  The legitimate slowest create is a cold image build/pull,
#: which can run for many minutes, so we bias CONSERVATIVE (err against false
#: "stuck" findings) at 15 minutes -- comfortably above a typical cold build and
#: far below the 24 h GC sweeps.  Treat this as a tunable, not a fact.
CREATING_EXPIRY_SECONDS = 900

#: Sentinel for a live container whose Docker state string could not be read
#: (an empty string in the live report).  Note: ABSENCE is signalled by
#: ``None`` only -- this sentinel always means a container EXISTS.
LIVE_STATE_UNKNOWN = "unknown"

#: Normalised live states meaning "the container is still being created".
_LIVE_CREATING = frozenset({"created", "creating"})


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` when absent/malformed.

    A naive timestamp (no offset) is assumed UTC.  Never raises.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(record: Any, now: datetime | None) -> float | None:
    """Return the record's age in seconds, or ``None`` when indeterminate.

    ``None`` means the timestamp or the clock could not be read; callers fail
    toward ``fresh=True`` in that case.
    """
    created = _parse_iso(getattr(record, "created_at", None))
    if created is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()


def _normalise_live_state(live_state: Any) -> str | None:
    """Normalise a raw live-state report; ``None`` means "no live container".

    A non-``None`` value is treated as a string, trimmed and lower-cased; an
    empty result becomes the sentinel :data:`LIVE_STATE_UNKNOWN` (a container
    that exists but whose state is unreadable).  Never raises.
    """
    if live_state is None:
        return None
    text = str(live_state).strip().lower()
    return text or LIVE_STATE_UNKNOWN


def resolve_container_state(
    record: Any, *, live_state: str | None, now: datetime | None = None
) -> tuple[str, bool]:
    """Resolve ``(state, fresh)`` for *record* given the live report *live_state*.

    Returns the record's OWN effective state -- one of :data:`STATE_RUNNING`,
    :data:`STATE_CREATING` or ``""`` (unset/unknown) -- plus a freshness flag;
    no display label is synthesised here (rendering is the display layer's job).
    ``live_state`` is ``None`` when no live container exists, else a live Docker
    state string (trimmed, case-insensitive; ``""`` -> :data:`LIVE_STATE_UNKNOWN`).
    Pure and total: reads only its arguments and never raises (an absent or
    malformed ``created_at`` degrades to an indeterminate age).  ``now`` is
    injected so the function is deterministic under test.  See the module
    docstring for the freshness model.
    """
    recorded = getattr(record, "state", "") or ""
    live = _normalise_live_state(live_state)
    if recorded == STATE_RUNNING:
        return STATE_RUNNING, live is not None
    if recorded == STATE_CREATING:
        if live is None:
            age = _age_seconds(record, now)
            if age is None:
                return STATE_CREATING, True
            return STATE_CREATING, age <= CREATING_EXPIRY_SECONDS
        return STATE_CREATING, live in _LIVE_CREATING
    return "", True


def creating_drift_finding(
    record: Any, *, live_state: str | None, now: datetime | None = None
) -> DriftFinding | None:
    """Build (never emit) the drift finding for a stale ``creating`` record.

    Returns a :class:`~thoughtmachine.container_record.drift.DriftFinding`
    (``CLASS_LIFECYCLE`` / ``EVENT_CONTAINER_STUCK_CREATING``) exactly when the
    resolver reports the record STALE in ``creating`` --
    ``resolve_container_state(record, live_state=..., now=...)`` equals
    ``(STATE_CREATING, False)``, i.e. no live container past its expiry window,
    or a live container that contradicts the recorded ``creating`` claim.
    ``actual`` is ``""`` for the absent cause and the normalised live state for
    the contradiction cause, so the two causes carry DIFFERENT dedup signatures
    (the absent case is byte-identical to the historical digest).  Returns
    ``None`` otherwise.  Pure and total; the finding is only *returned* --
    emitting it is the caller's decision.

    The finding has the same shape as ``drift.py``'s own findings
    (``drift_class``, ``event_type``, ``expected``, ``actual`` and a
    ``signature_for`` digest).
    """
    resolved, fresh = resolve_container_state(record, live_state=live_state, now=now)
    if resolved != STATE_CREATING or fresh:
        return None
    normalised = _normalise_live_state(live_state)
    actual = "" if normalised is None else normalised
    return DriftFinding(
        drift_class=drift.CLASS_LIFECYCLE,
        event_type=drift.EVENT_CONTAINER_STUCK_CREATING,
        expected=STATE_CREATING,
        actual=actual,
        signature=drift.signature_for(
            drift.EVENT_CONTAINER_STUCK_CREATING, STATE_CREATING, actual
        ),
        fresh=False,
    )
