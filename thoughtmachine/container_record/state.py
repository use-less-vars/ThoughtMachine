"""Pure container-state resolution (container-record subsystem).

:func:`resolve_container_state` is the *reader* of a record's ``state`` field
(``models.Record.state``).  Given a record and whether its container is
currently present live, it returns ``(state, fresh)``:

* ``state`` -- the record's OWN effective state: ``"running"``, ``"creating"``,
  or ``""`` (empty = the record is unset / makes no claim).  These are
  RECORD-subsystem values, NOT rendered view states: this core module never
  invents a display label.  Turning an unset ``""`` into the display label
  ``"stopped"`` is the DISPLAY layer's job -- the workspace container-view
  path does exactly that in
  ``web_ui.backend.server._map_container_view_state``.
* ``fresh`` -- whether that reading can still be trusted.  ``False`` marks a
  recorded state the live world has contradicted (a container the record calls
  ``running`` that is absent, or a record stuck in ``creating`` past its
  window); ``True`` means fresh / not (yet) known-stale.

Both :func:`resolve_container_state` and :func:`stuck_creating_finding` are
**pure and read-only**: neither performs any IO -- no Docker, no filesystem, no
record store, no event emission, no clock (``now`` is injected).  All IO (the
``docker inspect`` / record load) is the caller's.  :func:`stuck_creating_finding`
*returns* a :class:`~thoughtmachine.container_record.drift.DriftFinding` for the
stuck-``creating`` case; it never emits it (emission stays in ``drift.py``).

Freshness model
---------------
Only two recorded states carry a freshness judgement; every other value is
returned unchanged with ``fresh=True``:

* ``running`` -- fresh iff a live container is present; a record that says
  ``running`` while no container exists is stale.
* ``creating`` -- fresh while the container is present, or (when absent) while
  the record is still inside :data:`CREATING_EXPIRY_SECONDS` of ``created_at``.
  Past that window the record is *stuck*: ``fresh=False``.
* unset (``""``) or any other/unknown value -- the record makes no claim we
  can contradict, so nothing can be stale: the value is returned as-is with
  ``fresh=True``.  We deliberately do NOT synthesise a staleness signal from an
  absence of information (that would flag every record created before its
  container is attached, and every legacy record that predates the field).

Age is measured from ``created_at``: ``api.begin_record`` writes
``state="creating"`` and ``created_at=now`` in the same call, so ``created_at``
is the moment the record entered ``creating``.  A missing or malformed
``created_at`` yields an *indeterminate* age, which fails toward ``fresh=True``:
an unreadable timestamp must never manufacture a "stuck" signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import drift
from .drift import DriftFinding
from .models import STATE_CREATING, STATE_RUNNING

__all__ = [
    "CREATING_EXPIRY_SECONDS",
    "resolve_container_state",
    "stuck_creating_finding",
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


def resolve_container_state(
    record: Any, *, live_present: bool, now: datetime | None = None
) -> tuple[str, bool]:
    """Resolve ``(state, fresh)`` for *record* given *live_present*.

    Returns the record's OWN effective state -- one of :data:`STATE_RUNNING`,
    :data:`STATE_CREATING` or ``""`` (unset/unknown) -- plus a freshness flag;
    no display label is synthesised here (rendering is the display layer's job).
    Pure and total: reads only its arguments and never raises (an absent or
    malformed ``created_at`` degrades to an indeterminate age).  ``now`` is
    injected so the function is deterministic under test.  See the module
    docstring for the freshness model.
    """
    recorded = getattr(record, "state", "") or ""
    if recorded == STATE_RUNNING:
        return STATE_RUNNING, bool(live_present)
    if recorded == STATE_CREATING:
        if live_present:
            return STATE_CREATING, True
        age = _age_seconds(record, now)
        if age is None:
            return STATE_CREATING, True
        return STATE_CREATING, age <= CREATING_EXPIRY_SECONDS
    return "", True


def stuck_creating_finding(
    record: Any, *, live_present: bool, now: datetime | None = None
) -> DriftFinding | None:
    """Build (never emit) the drift finding for a record stuck in ``creating``.

    Returns a :class:`~thoughtmachine.container_record.drift.DriftFinding`
    (``CLASS_LIFECYCLE`` / ``EVENT_CONTAINER_STUCK_CREATING``) when the record
    still records ``creating`` while no live container is present and its age is
    past :data:`CREATING_EXPIRY_SECONDS`; otherwise ``None``.  Pure and total.

    The finding has the same shape as ``drift.py``'s own findings
    (``drift_class``, ``event_type``, ``expected``, ``actual`` and a
    ``signature_for`` digest) but is only *returned* -- emitting it is the
    caller's decision, keeping this module IO-free.
    """
    if live_present:
        return None
    resolved, fresh = resolve_container_state(
        record, live_present=live_present, now=now
    )
    if resolved != STATE_CREATING or fresh:
        return None
    return DriftFinding(
        drift_class=drift.CLASS_LIFECYCLE,
        event_type=drift.EVENT_CONTAINER_STUCK_CREATING,
        expected=STATE_CREATING,
        actual="",
        signature=drift.signature_for(
            drift.EVENT_CONTAINER_STUCK_CREATING, STATE_CREATING, ""
        ),
    )
