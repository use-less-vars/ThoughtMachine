"""Write boundary and pending queue for the session addendum (W1 Slice 4).

The addendum is a first-class session field (``session/models.py``, slice 1) that
reaches the model by being composed into the single system-prompt string
(``agent/core/llm_client.py``, slice 2 / ruling D-2). This module adds the
*write* half:

  * :func:`write_addendum_entry` -- the ONE write boundary. Both writers ruling
    D-4 allows (the operator and the config mailbox) come through here, so
    validate + append + persist + notify happens in exactly one place, and the
    entry shape can never drift between the two callers.
  * :class:`AddendumQueue` -- the pending queue a running ``Agent`` drains at the
    per-stream-event poll (ruling D-5, design doc §7.2), so a Tier-1 change made
    mid-turn is delivered *between* stream events instead of at the turn
    boundary.

Nothing here is typed as a ``StreamEvent``: the queue carries plain
runtime-channel dicts (design doc §7.3), which the turn loop re-yields verbatim.

Budget enforcement (slice 5) lives here too, at this same boundary: a write that
would push the RESULTING entry set past the session's ``budget_chars`` (measured
as an exact character count) or ``max_entries`` is REFUSED -- never truncated,
never compensated for by evicting an existing entry -- and reported as a
``runtime_notice`` with code ``budget_exceeded`` / ``max_entries_exceeded``.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import uuid

from agent.logging import log
from session.models import ADDENDUM_SOURCES, normalize_addendum

#: Runtime-channel event kinds (design doc §7.3). Plain strings, because every
#: other runtime event on this channel is a string-keyed dict.
ADDENDUM_UPDATED = 'addendum_updated'
RUNTIME_NOTICE = 'runtime_notice'

#: Why a write was dropped. The write never raises -- ``normalize_addendum``
#: establishes that a bad entry is reported and discarded, never thrown -- so a
#: rejection is a value with one of these codes.
DROP_NO_SESSION = 'no_session'
DROP_INVALID_SOURCE = 'invalid_source'
DROP_INVALID_TEXT = 'invalid_text'
DROP_EMPTY_TEXT = 'empty_text'
DROP_REJECTED_BY_SCHEMA = 'rejected_by_schema'
#: Slice 5: the resulting entry set would not fit the addendum's budget. The
#: verdict is refusal only (never truncation, never eviction), which is what
#: keeps the refusal atomic: nothing is persisted and nothing is enqueued.
DROP_BUDGET_EXCEEDED = 'budget_exceeded'
DROP_MAX_ENTRIES_EXCEEDED = 'max_entries_exceeded'


def _now_iso() -> str:
    """Timestamp for a new entry: UTC, ISO-8601, second-resolution-neutral."""
    return datetime.now(timezone.utc).isoformat()


def _new_entry_id() -> str:
    """A fresh entry id.

    There is no id helper to reuse: the codebase mints ids inline with
    ``uuid.uuid4()`` (``session/models.py:242`` for ``session_id``) and
    ``uuid.uuid4().hex[:12]`` (``agent/core/worker_context.py:73``). This follows
    the short-hex idiom with an ``a-`` prefix so an addendum id is recognisable
    in a log line. Unique by construction; stable once returned, because an
    entry id is written once and never regenerated.
    """
    return f'a-{uuid.uuid4().hex[:12]}'


def _session_id(session) -> str:
    """The session id for a payload, or ``''`` when the session has none."""
    session_id = getattr(session, 'session_id', '')
    return session_id if isinstance(session_id, str) else ''


def _updated_payload(session, entry, entries) -> dict:
    """The ``addendum_updated`` runtime dict (design doc §7.3 + ruling D-6).

    D-6 requires the notification to be self-describing and idempotent, so it
    carries ``entry_id``, ``ts`` and the FULL resulting entry set: a client that
    receives two of these out of order converges on the same display without a
    diff to replay. ``entries`` is copied, so a consumer cannot mutate the
    session's own list through the event.
    """
    return {
        'type': ADDENDUM_UPDATED,
        'session_id': _session_id(session),
        'entry_id': entry['id'],
        'ts': entry['ts'],
        'source': entry['source'],
        'entries': [dict(item) for item in entries],
    }


def _notice_payload(session, code: str, message: str) -> dict:
    """The Tier-2 ``runtime_notice`` runtime dict for a rejected write."""
    return {
        'type': RUNTIME_NOTICE,
        'session_id': _session_id(session),
        'level': 'warning',
        'code': code,
        'message': message,
    }


def _enqueue(session, payload: dict) -> bool:
    """Hand ``payload`` to the session's queue if an Agent attached one.

    A session with no queue (a plain ``Session`` in a unit test, a store round
    trip, a writer in a process with no Agent) simply does not notify: the entry
    is already persisted, and the queue is a delivery detail, never a
    precondition for the write.
    """
    queue = getattr(session, 'addendum_queue', None)
    enqueue = getattr(queue, 'enqueue', None)
    if not callable(enqueue):
        return False
    enqueue(payload)
    return True


def _dropped(session, reason: str, message: str) -> dict:
    """Build the rejection result and report it on the runtime channel."""
    try:
        _enqueue(session, _notice_payload(session, reason, message))
    except Exception as exc:  # notification must never make the write raise
        log('WARNING', 'core.addendum', f'[addendum] could not enqueue notice: {exc}')
    log('WARNING', 'core.addendum', f'[addendum] write dropped ({reason}): {message}')
    return {'status': 'dropped', 'entry': None, 'reason': reason, 'entries': []}


def write_addendum_entry(session, text, source: str = 'operator') -> dict:
    """Append exactly ONE entry to ``session``'s addendum and notify the queue.

    Args:
        session: The session to write to (``session.models.Session``, or any
            object exposing ``addendum`` / ``session_id`` / optionally
            ``addendum_queue``).
        text: The entry text. Must be a non-empty string after stripping; the
            stored text is NOT stripped, so an operator's formatting survives.
        source: Who is writing -- ``'operator'`` or ``'mailbox'`` (ruling D-4).
            Nothing else is legal: C2 forbids the model writing its own memory.

    Returns:
        ``{'status': 'ok'|'dropped', 'entry': dict|None, 'reason': str|None,
        'entries': list[dict]}`` where ``entries`` is the full resulting entry
        set (empty on a drop). Never raises: a rejection is reported through the
        return value and a ``runtime_notice`` on the queue, because the slice-1
        contract is that one bad write must not cost the session.

        A write that would not fit the addendum's budget is one of those drops:
        ``reason`` is ``'budget_exceeded'`` or ``'max_entries_exceeded'``, and
        the session's addendum is left exactly as it was.
    """
    try:
        if session is None:
            return _dropped(None, DROP_NO_SESSION, 'no session to write the addendum entry to')
        if source not in ADDENDUM_SOURCES:
            return _dropped(session, DROP_INVALID_SOURCE,
                            f"source {source!r} is not one of {ADDENDUM_SOURCES}")
        if not isinstance(text, str):
            return _dropped(session, DROP_INVALID_TEXT,
                            f'addendum text must be a string, got {type(text).__name__}')
        if not text.strip():
            return _dropped(session, DROP_EMPTY_TEXT, 'addendum text is empty')

        addendum = normalize_addendum(getattr(session, 'addendum', None))
        entry = {
            'id': _new_entry_id(),
            'ts': _now_iso(),
            'source': source,
            'text': text,
            'pinned': False,
        }
        # Validate through slice-1's own normaliser rather than duplicating its
        # rules here: if it does not survive that, it is not a legal entry.
        candidate = dict(addendum)
        candidate['entries'] = addendum['entries'] + [entry]
        normalized = normalize_addendum(candidate)
        entries = normalized['entries']
        if len(entries) != len(addendum['entries']) + 1:
            return _dropped(session, DROP_REJECTED_BY_SCHEMA,
                            f'entry rejected by the addendum schema: {entry!r}')

        # --- Slice 5: budget enforcement -------------------------------------
        # Measured on the RESULTING set (existing + candidate), as an exact
        # character count; there is no estimator and no token proxy. The check
        # runs before anything is persisted or enqueued, which is what makes the
        # refusal atomic. There is no grandfathering either: a session that is
        # already over budget can never accept another write, because its
        # resulting set is over budget too. ``pinned`` exempts nothing.
        total_chars = sum(len(item['text']) for item in entries)
        if total_chars > normalized['budget_chars']:
            return _dropped(
                session, DROP_BUDGET_EXCEEDED,
                f"addendum budget exceeded: {total_chars} > "
                f"{normalized['budget_chars']} chars")
        if len(entries) > normalized['max_entries']:
            return _dropped(
                session, DROP_MAX_ENTRIES_EXCEEDED,
                f"addendum entry limit exceeded: {len(entries)} > "
                f"{normalized['max_entries']} entries")

        # Persist on the session's addendum, keeping any key this module does not
        # know about so a later version's field is not silently erased.
        current = getattr(session, 'addendum', None)
        persisted = dict(current) if isinstance(current, dict) else {}
        persisted['entries'] = entries
        persisted['budget_chars'] = normalized['budget_chars']
        persisted['max_entries'] = normalized['max_entries']
        session.addendum = persisted

        stored = dict(entries[-1])
        _enqueue(session, _updated_payload(session, stored, entries))
        log('DEBUG', 'core.addendum',
            f"[addendum] appended entry {stored['id']} from {source} (entries={len(entries)})")
        return {'status': 'ok', 'entry': stored, 'reason': None,
                'entries': [dict(item) for item in entries]}
    except Exception as exc:
        # The slice-1 contract, restated for the write path: a failed write is
        # reported, never raised into the caller's turn.
        log('ERROR', 'core.addendum', f'[addendum] write failed unexpectedly: {exc}')
        return {'status': 'dropped', 'entry': None, 'reason': 'exception', 'entries': []}


class AddendumQueue:
    """Pending Tier-1 notifications, drained by the running turn loop.

    ``enqueue`` is called from whatever thread made the write (the operator's
    request thread, or the mailbox) and ``drain`` from the controller worker
    thread. ``deque.append`` / ``popleft`` are atomic, which is the same footing
    the existing ``_next_query_queue`` mailbox stands on; ruling D-5 explicitly
    rules out a new thread, a new channel and any lock contention, and the drain
    only ever runs *between* ``__anext__`` calls.

    Draining pops, so a payload is delivered at most once even if the turn loop
    drains again later (or twice in one turn).
    """

    def __init__(self):
        self._pending = deque()

    def enqueue(self, payload: dict) -> None:
        """Queue one runtime dict for the next drain."""
        self._pending.append(payload)

    def drain(self) -> list:
        """Pop and return every pending payload, oldest first."""
        drained = []
        while True:
            try:
                drained.append(self._pending.popleft())
            except IndexError:
                return drained

    def __len__(self) -> int:
        return len(self._pending)
