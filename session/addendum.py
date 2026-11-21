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
#: A mutation that targets ONE entry by id, where no entry carries that id. The
#: id came from the caller, so this is a caller/serialisation error (the
#: transport renders it as a 404), never a write refused for size.
DROP_ENTRY_NOT_FOUND = 'entry_not_found'


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


def _dropped(session, reason: str, message: str, notify: bool = True, **extra) -> dict:
    """Build the rejection result and report it on the runtime channel.

    ``notify=False`` suppresses the ``runtime_notice`` (used by the HTTP write
    route, whose own thread must not push a payload onto a live Agent's queue:
    the running turn drains that queue between stream events, so a request
    thread firing into it would inject a mid-turn event for a write that
    already has an HTTP response of its own).

    ``**extra`` are boundary-measured numbers (``attempted_chars``,
    ``budget_chars``, ``attempted_entries``, ``max_entries``, ``total_chars``)
    copied onto the RESULT so a caller (the 409 body) can report the refusal
    without recomputing ``sum(len(text))`` a second time.  They are deliberately
    NOT added to the runtime notice: the notice's key set is fixed by design
    doc §7.3.

    ``message`` is the boundary's own sentence, returned on the RESULT so a
    transport that renders a refusal (the 409 body) reuses the very words the
    boundary logged and measured instead of re-deriving them -- recomputing
    them in a second place is how a report comes to disagree with its verdict.
    """
    try:
        if notify:
            _enqueue(session, _notice_payload(session, reason, message))
    except Exception as exc:  # notification must never make the write raise
        log('WARNING', 'core.addendum', f'[addendum] could not enqueue notice: {exc}')
    log('WARNING', 'core.addendum', f'[addendum] write dropped ({reason}): {message}')
    return {'status': 'dropped', 'entry': None, 'reason': reason,
            'message': message, 'entries': [], **extra}


def _find_entry_index(entries, entry_id):
    """Position of the entry carrying ``entry_id``, or ``None`` when absent.

    Identity is the stored id alone. ``==`` is used rather than a dict lookup so
    an unhashable ``entry_id`` from a request body is simply "not found" instead
    of raising inside the boundary.
    """
    for index, item in enumerate(entries):
        if item['id'] == entry_id:
            return index
    return None


def addendum_rev(addendum) -> int:
    """The GENERATION of a (normalised or raw) addendum: its mutation counter.

    ``rev`` counts the mutations committed to this addendum, so comparing two
    copies answers "which one is newer" without diffing entry sets.  A copy
    written before the counter existed -- and a freshly created addendum, which
    deliberately does not carry the key (``session/models.py:normalize_addendum``)
    -- is generation 0.  Anything that is not a non-negative int is 0 too: the
    counter is advisory, and an unreadable one must degrade to "no idea", never
    to an exception on a save path.
    """
    if not isinstance(addendum, dict):
        return 0
    rev = addendum.get('rev', 0)
    if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0:
        return rev
    return 0


def bump_addendum_rev(addendum: dict) -> int:
    """Set and return ``addendum['rev']`` = the next generation (prev + 1).

    The ONE place the counter is advanced, called from the shared commit tail
    (:func:`_commit_mutation`) and from the one mutator that builds the
    persisted addendum by hand (the DELETE-all route, whose semantics are
    "replace the entry set" rather than "mutate one entry", so it does not go
    through the entry boundary).  Duplicating the increment in a second place is
    how two writers come to disagree about which generation is newer -- the very
    disagreement this counter exists to settle.
    """
    addendum['rev'] = addendum_rev(addendum) + 1
    return addendum['rev']


def _commit_mutation(session, normalized: dict, entries: list, entry: dict,
                     notify: bool, log_detail: str, boundary: dict, **extra) -> dict:
    """Persist ``entries`` on ``session``, notify the queue, return the result.

    The ONE commit tail shared by every mutation (append / update / remove), so
    the boundaries cannot drift on what "persisted" means: the previous addendum
    dict is preserved (a later version's unknown top-level key is not erased),
    the normalised caps are restated, the resulting entry set is committed IN
    PLACE on the session, and only THEN is the notification enqueued.

    ``entry`` is the entry the mutation is about (the accepted one, or -- for a
    removal -- the entry that was removed, which is what makes the
    ``addendum_updated`` payload name the entry that disappeared); ``boundary``
    is the number dict the caller already measured; ``**extra`` are result-only
    fields (``removed_id``), appended exactly as ``_dropped`` appends its
    boundary numbers.
    """
    current = getattr(session, 'addendum', None)
    persisted = dict(current) if isinstance(current, dict) else {}
    persisted['entries'] = entries
    persisted['budget_chars'] = normalized['budget_chars']
    persisted['max_entries'] = normalized['max_entries']
    # The generation advance is part of "persisted", not a separate step: every
    # committed mutation is one generation, for every mutation kind, in one
    # place.  A reader of the stored record can then tell this addendum from a
    # snapshot taken before the mutation (see ``SessionManager.save_session``).
    persisted['rev'] = bump_addendum_rev(persisted)
    session.addendum = persisted

    if notify:
        _enqueue(session, _updated_payload(session, entry, entries))
    log('DEBUG', 'core.addendum', log_detail)
    return {'status': 'ok', 'entry': entry, 'reason': None,
            'entries': [dict(item) for item in entries],
            'attempted_chars': boundary['attempted_chars'],
            'budget_chars': normalized['budget_chars'],
            'attempted_entries': boundary['attempted_entries'],
            'max_entries': normalized['max_entries'],
            'total_chars': boundary['total_chars'],
            **extra}


def write_addendum_entry(session, text, source: str = 'operator',
                         notify: bool = True) -> dict:
    """Append exactly ONE entry to ``session``'s addendum and notify the queue.

    Args:
        session: The session to write to (``session.models.Session``, or any
            object exposing ``addendum`` / ``session_id`` / optionally
            ``addendum_queue``).
        text: The entry text. Must be a non-empty string after stripping; the
            stored text is NOT stripped, so an operator's formatting survives.
        source: Who is writing -- ``'operator'`` or ``'mailbox'`` (ruling D-4).
            Nothing else is legal: C2 forbids the model writing its own memory.
        notify: Whether to report on the session's ``addendum_queue`` (the
            runtime channel a running Agent drains).  ``True`` for in-process
            writers; the HTTP write route passes ``False`` because its request
            thread must not fire turn-loop events -- it answers on HTTP instead.

    Returns:
        ``{'status': 'ok'|'dropped', 'entry': dict|None, 'reason': str|None,
        'entries': list[dict]}`` where ``entries`` is the full resulting entry
        set (empty on a drop). Never raises: a rejection is reported through the
        return value and a ``runtime_notice`` on the queue, because the slice-1
        contract is that one bad write must not cost the session.

        Slice 5 also carries the boundary-measured numbers on the result --
        ``attempted_chars`` / ``attempted_entries`` (the RESULTING set that was
        measured, or the accepted set), ``budget_chars`` / ``max_entries`` (the
        caps in force) and ``total_chars`` (the set as persisted; on a drop, the
        unchanged set) -- so no caller has to recompute ``sum(len(text))`` and
        risk disagreeing with the verdict.

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
        # The unchanged set's own size, measured once, here: it is what the
        # caller reports on a refusal, so the HTTP layer never re-summarises.
        prior_chars = sum(len(item['text']) for item in addendum['entries'])
        boundary = {
            'attempted_chars': total_chars,
            'budget_chars': normalized['budget_chars'],
            'attempted_entries': len(entries),
            'max_entries': normalized['max_entries'],
            'total_chars': prior_chars,
        }
        if total_chars > normalized['budget_chars']:
            return _dropped(
                session, DROP_BUDGET_EXCEEDED,
                f"addendum budget exceeded: {total_chars} > "
                f"{normalized['budget_chars']} chars",
                notify=notify, **boundary)
        if len(entries) > normalized['max_entries']:
            return _dropped(
                session, DROP_MAX_ENTRIES_EXCEEDED,
                f"addendum entry limit exceeded: {len(entries)} > "
                f"{normalized['max_entries']} entries",
                notify=notify, **boundary)

        # Persist + notify + build the result through the shared commit tail (the
        # same tail every mutation uses).
        stored = dict(entries[-1])
        return _commit_mutation(
            session, normalized, entries, stored, notify,
            f"[addendum] appended entry {stored['id']} from {source} "
            f'(entries={len(entries)})',
            {'attempted_chars': total_chars,
             'attempted_entries': len(entries),
             'total_chars': total_chars},
            notified=notify)
    except Exception as exc:
        # The slice-1 contract, restated for the write path: a failed write is
        # reported, never raised into the caller's turn.
        log('ERROR', 'core.addendum', f'[addendum] write failed unexpectedly: {exc}')
        return {'status': 'dropped', 'entry': None, 'reason': 'exception', 'entries': []}


def update_addendum_entry(session, entry_id, text, source: str = 'operator',
                          notify: bool = True) -> dict:
    """Replace the TEXT of the entry with ``entry_id``, in place.

    An entry's ``id``, ``ts``, ``pinned`` and ``source`` are its identity and are
    PRESERVED; only ``text`` changes. No new field is introduced: slice-1's entry
    normaliser drops unknown keys, so a new field would silently vanish on the
    next read.

    Refusals are the append path's, in the same order -- ``no_session``,
    ``invalid_source``, ``invalid_text``, ``empty_text`` -- followed by
    ``entry_not_found`` and then the budget check. The update is ALL-OR-NOTHING:
    on any refusal the stored entry text is byte-identical to what it was.

    The budget total is the RESULTING set with the old text SWAPPED OUT, not
    added to (an entry's own text is never counted twice), which is what lets a
    too-long entry be shortened on an addendum that is already full.

    Returns the same shape as :func:`write_addendum_entry`; ``entry`` is the
    updated entry and ``entries`` the full resulting set.
    """
    try:
        if session is None:
            return _dropped(None, DROP_NO_SESSION,
                            'no session to update the addendum entry in')
        if source not in ADDENDUM_SOURCES:
            return _dropped(session, DROP_INVALID_SOURCE,
                            f"source {source!r} is not one of {ADDENDUM_SOURCES}")
        if not isinstance(text, str):
            return _dropped(session, DROP_INVALID_TEXT,
                            f'addendum text must be a string, got {type(text).__name__}')
        if not text.strip():
            return _dropped(session, DROP_EMPTY_TEXT, 'addendum text is empty')

        addendum = normalize_addendum(getattr(session, 'addendum', None))
        existing_entries = addendum['entries']
        target = _find_entry_index(existing_entries, entry_id)
        if target is None:
            return _dropped(session, DROP_ENTRY_NOT_FOUND,
                            f'no addendum entry with id {entry_id!r}',
                            notify=notify)

        # Swap the text into a COPY of the stored entry, so the old text is
        # replaced rather than appended to.
        replacement = dict(existing_entries[target])
        replacement['text'] = text
        candidate = dict(addendum)
        candidate['entries'] = (existing_entries[:target] + [replacement]
                                + existing_entries[target + 1:])
        # Validate through slice-1's own normaliser, exactly as the append does:
        # if it does not survive that, it is not a legal entry.
        normalized = normalize_addendum(candidate)
        entries = normalized['entries']
        if len(entries) != len(existing_entries):
            return _dropped(session, DROP_REJECTED_BY_SCHEMA,
                            f'entry rejected by the addendum schema: {replacement!r}',
                            notify=notify)

        # --- budget enforcement, same verdict and same numbers as the append ---
        # Measured on the RESULTING set, in which the replaced entry already
        # carries the new text: the swap is the measurement, so an update can
        # never be charged for both the old and the new text.
        total_chars = sum(len(item['text']) for item in entries)
        prior_chars = sum(len(item['text']) for item in existing_entries)
        boundary = {
            'attempted_chars': total_chars,
            'budget_chars': normalized['budget_chars'],
            'attempted_entries': len(entries),
            'max_entries': normalized['max_entries'],
            'total_chars': prior_chars,
        }
        if total_chars > normalized['budget_chars']:
            return _dropped(
                session, DROP_BUDGET_EXCEEDED,
                f"addendum budget exceeded: {total_chars} > "
                f"{normalized['budget_chars']} chars",
                notify=notify, **boundary)

        stored = dict(entries[target])
        return _commit_mutation(
            session, normalized, entries, stored, notify,
            f"[addendum] updated entry {stored['id']} from {source} "
            f'(entries={len(entries)})',
            {'attempted_chars': total_chars,
             'attempted_entries': len(entries),
             'total_chars': total_chars},
            notified=notify)
    except Exception as exc:
        log('ERROR', 'core.addendum', f'[addendum] update failed unexpectedly: {exc}')
        return {'status': 'dropped', 'entry': None, 'reason': 'exception', 'entries': []}


def remove_addendum_entry(session, entry_id, notify: bool = True) -> dict:
    """Remove the entry with ``entry_id`` from ``session``'s addendum.

    Refusals: ``no_session``, then ``entry_not_found``. There is NO budget check:
    a removal only ever shrinks the measured set, so it cannot be the operation
    that exceeds a cap -- and removing entries is the RECOVERY path, the way a
    set that is already over budget (for whatever reason) becomes writable again.

    Returns the append path's shape, plus ``removed_id``: ``entry`` is the
    removed entry (so the ``addendum_updated`` payload names the entry that
    disappeared) and ``entries`` the remaining set, with the updated totals.
    """
    try:
        if session is None:
            return _dropped(None, DROP_NO_SESSION,
                            'no session to remove the addendum entry from')

        addendum = normalize_addendum(getattr(session, 'addendum', None))
        existing_entries = addendum['entries']
        target = _find_entry_index(existing_entries, entry_id)
        if target is None:
            return _dropped(session, DROP_ENTRY_NOT_FOUND,
                            f'no addendum entry with id {entry_id!r}',
                            notify=notify)

        removed = dict(existing_entries[target])
        candidate = dict(addendum)
        candidate['entries'] = (existing_entries[:target]
                                + existing_entries[target + 1:])
        normalized = normalize_addendum(candidate)
        entries = normalized['entries']
        if len(entries) != len(existing_entries) - 1:
            return _dropped(session, DROP_REJECTED_BY_SCHEMA,
                            f'entry rejected by the addendum schema: {removed!r}',
                            notify=notify)

        total_chars = sum(len(item['text']) for item in entries)
        return _commit_mutation(
            session, normalized, entries, removed, notify,
            f"[addendum] removed entry {removed['id']} "
            f'(entries={len(entries)})',
            {'attempted_chars': total_chars,
             'attempted_entries': len(entries),
             'total_chars': total_chars},
            removed_id=removed['id'],
            notified=notify)
    except Exception as exc:
        log('ERROR', 'core.addendum', f'[addendum] remove failed unexpectedly: {exc}')
        return {'status': 'dropped', 'entry': None, 'reason': 'exception', 'entries': []}


def addendum_total_chars(session) -> int:
    """Exact character count of the session's CURRENT entry set.

    The boundary measures this number itself; exposing it keeps the HTTP layer
    free of ``sum(len(...))`` arithmetic, which is what makes a disagreement
    between the displayed total and the enforced budget impossible.
    """
    addendum = normalize_addendum(getattr(session, 'addendum', None))
    return sum(len(item['text']) for item in addendum['entries'])


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
