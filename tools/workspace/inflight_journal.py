# tools/workspace/inflight_journal.py
"""Fenced ``inflight.json`` journal: the in-flight turn, made recoverable.

One journal file per worker instance, next to ``command.json`` and
``status.json`` in the worker's directory
(``<workspace>/workers/<instance_label>/inflight.json``). It records the turn
that is currently running -- query id, query text, attempt count, the last tool
call seen -- so that a turn abandoned by ``stop``/``detach`` can be replayed
instead of silently lost.

This module is the PRIMITIVES ONLY (read / write / clear + the record shape).
It has no callers in the worker yet: the write points, the park flip and the
replay live in later slices. Nothing here changes the behaviour of any existing
path.

Identity fence
--------------
The journal reuses :class:`tools.workspace.command_channel.CommandFence` and
:func:`tools.workspace.command_channel.fence_rejection_reason` rather than
inventing a second fencing scheme. The same four axes
(``session_id`` / ``worker_name`` / ``instance_id`` / ``epoch``) that address a
command address a journal record, and ``epoch`` is again the worker's real
generation counter -- there is still exactly ONE source for it.

That reuse is the whole argument against a new high-water map: **a record
written by a dead incarnation is quarantined by ``fence_rejection_reason``
exactly like a stale command**, so the fence already covers the failure mode a
high-water map would be added for. Read reports the reason and leaves the
record on disk; clear refuses to remove a record it does not own. Nothing is
silently deleted.

Exactly two readers follow from that, and the difference between them is the
point:

* :func:`read_inflight` (FENCED) is the SAME-INCARNATION reader. The write
  points, the park flip and the terminal clear all use it, and a foreign fence
  there is a quarantine.
* :func:`read_inflight_unfenced` is the ADOPTION reader. Adoption is
  CROSS-EPOCH BY DEFINITION -- a successor instance 1 (new epoch) must take
  over the record a killed incarnation (old epoch) left behind -- so that one
  path needs a reader that does not consult the fence at all. It is a
  SEPARATE, explicitly named function rather than a bypass flag on the fenced
  reader, because the name is the safety property: a same-incarnation caller
  of it is a bug, and a call site that reads "unfenced" is a review flag.

Single writer (Option K)
------------------------
Under Option K a ``stop``/``detach`` journals the turn and then KILLS the
instance, while ``pause`` keeps a parked, resumable instance and never
journals. So at most ONE live incarnation per ``(session, worker)`` writes this
file at a time: the writer is the thread that owns the directory.

The directory is per instance (``_WorkerRegistry.instance_label``:
``architect`` for instance 1, ``architect#2`` for instance 2), so two instances
of one name do not share a journal. Instance 1 keeps the bare name, so a
SUCCESSOR instance 1 does reuse the directory -- with a new generation, i.e. a
new ``epoch``. That is why a write always stamps the caller's fence and always
wins: refusing to write over a foreign fence would wedge exactly the
crash-recovery path this journal exists for. The destructive operations carry
the fence instead (read quarantines, clear refuses).

Record statuses
---------------
``inflight``    -- written before the turn runs.
``recoverable`` -- flipped at the park; the turn can be replayed.
``cancelled``   -- the operator killed it; must never be resurrected.
``adopted``     -- a successor instance took it over (later slice).

These are the JOURNAL's statuses. ``interrupted`` is NOT one of them: it is the
JOB's terminal status (``WorkerJobRegistry.terminalize``), which a later slice
sets alongside a ``reason``. R3 caps ``attempts`` at
:data:`MAX_ATTEMPTS`; there is deliberately no ``abandoned`` status anywhere.

Atomicity and failure policy
----------------------------
The write is temp-file + ``os.replace`` in the same directory (so the replace
cannot cross a filesystem boundary), ``flush`` + ``fsync`` before the replace,
and the temp file is removed on every failure path -- a reader polling at any
instant sees either the old record or the new one, never a half-written file.

Like :mod:`tools.workspace.command_channel`, nothing here raises for the
caller: write and clear return ``None`` on success or a short reason string on
refusal/failure, and read returns ``(record, reason)``. A missing file, an
unparseable file and a foreign-fenced file all read as ``None`` -- they are
indistinguishable in behaviour, and distinguishable only in the log, which is
the cheapest honest split. A failed write must be treated as "carry on without
journaling" by the caller, never as "refuse the query".

Imports: this module adds no import-time dependency beyond
:mod:`tools.workspace.command_channel`, and the ``agent.logging`` handle is
imported lazily inside :func:`_warn` -- so a logging failure can never
propagate into a journal operation, and the primitives stay usable from a test
or a script that has not stood the logging stack up.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from tools.workspace.command_channel import CommandFence, fence_rejection_reason

#: Name of the live journal file inside a worker directory.
INFLIGHT_FILENAME = "inflight.json"

#: Shape version of the payload below. A record carrying a version this module
#: does not know is refused (fail closed) rather than half-interpreted.
SCHEMA_VERSION = 1

#: R3: a recovered turn is replayed at most this many times, then the job is
#: terminalized as ``interrupted`` with a ``reason``. Not an ``abandoned``.
MAX_ATTEMPTS = 3

#: Record lifecycle statuses (see the module docstring). ``interrupted`` is the
#: JOB's terminal status and must never appear here.
STATUS_INFLIGHT = "inflight"
STATUS_RECOVERABLE = "recoverable"
STATUS_CANCELLED = "cancelled"
STATUS_ADOPTED = "adopted"
JOURNAL_STATUSES: Tuple[str, ...] = (
    STATUS_INFLIGHT,
    STATUS_RECOVERABLE,
    STATUS_CANCELLED,
    STATUS_ADOPTED,
)

#: Reason strings this module reports for a record it cannot use. A fence
#: disagreement is reported in ``fence_rejection_reason``'s own vocabulary
#: (``"epoch 3 != 4"``, ``"no fence in payload"``), never re-worded here.
_CORRUPT_JSON_REASON = "corrupt-json in inflight journal"
_MALFORMED_REASON = "malformed inflight record"

#: Log channel used by the worker for the same bookkeeping warnings.
_LOG_CHANNEL = "tools.worker"


def _warn(message: str) -> None:
    """Emit a worker-channel warning of the repo's usual shape.

    The ``agent.logging`` handle is imported HERE, not at module scope, so this
    module stays importable without the logging stack (same "stdlib only"
    property :mod:`tools.workspace.command_channel` keeps) and a logging
    failure can never propagate into a journal operation.
    """
    try:
        from agent.logging import log as _log
    except Exception:
        return
    try:
        _log("WARNING", _LOG_CHANNEL, message)
    except Exception:
        pass


def utcnow_iso() -> str:
    """UTC timestamp in the form the rest of the worker records use."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class InflightRecord:
    """One in-flight turn, as journalled.

    The four fence axes are NOT fields: :meth:`to_payload` merges them in from
    the :class:`CommandFence` the caller passes, so identity has exactly one
    source and a record can never be stamped with a fence that disagrees with
    the writer's.

    Construction validates (a ``ValueError`` here is a caller bug, not a data
    condition -- the data-driven path is :meth:`from_payload`, which returns
    ``None`` instead of raising):

    * ``status`` must be one of :data:`JOURNAL_STATUSES`;
    * ``attempts`` must be ``0 <= attempts <= MAX_ATTEMPTS`` (R3);
    * ``last_tool``, when present, is ``{"name": <tool>, "args_hash": <hex>}``
      -- R2's replacement for a per-tool idempotency ledger.
    """

    query_id: str
    query: str
    status: str
    attempts: int
    requested_by: str
    created_at: str
    updated_at: str
    last_tool: Optional[Dict[str, str]] = None
    reason: Optional[str] = None
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in JOURNAL_STATUSES:
            raise ValueError(
                "invalid journal status %r (expected one of %s; 'interrupted' "
                "is the JOB's terminal status, not a journal status)"
                % (self.status, ", ".join(JOURNAL_STATUSES))
            )
        if type(self.attempts) is not int or not 0 <= self.attempts <= MAX_ATTEMPTS:
            raise ValueError(
                "invalid attempts %r (expected an int in 0..%d)"
                % (self.attempts, MAX_ATTEMPTS)
            )
        if self.schema != SCHEMA_VERSION:
            raise ValueError(
                "unsupported journal schema %r (this module writes %d)"
                % (self.schema, SCHEMA_VERSION)
            )
        for name in ("query_id", "query", "requested_by", "created_at", "updated_at"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("%s must be a str" % name)
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("reason must be a str or None")
        if self.last_tool is not None:
            last_tool = self.last_tool
            if not isinstance(last_tool, dict):
                raise ValueError("last_tool must be a dict or None")
            if not isinstance(last_tool.get("name"), str) or not isinstance(
                last_tool.get("args_hash"), str
            ):
                raise ValueError(
                    "last_tool needs a str 'name' and a str 'args_hash'"
                )

    def to_payload(self, fence: CommandFence) -> Dict[str, Any]:
        """The on-disk payload: record fields plus the four fence axes."""
        payload: Dict[str, Any] = {
            "schema": self.schema,
            "query_id": self.query_id,
            "query": self.query,
            "status": self.status,
            "attempts": self.attempts,
            "requested_by": self.requested_by,
            "last_tool": dict(self.last_tool) if self.last_tool is not None else None,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        payload.update(fence.to_payload())
        return payload

    @classmethod
    def from_payload(cls, data: object) -> Optional["InflightRecord"]:
        """Parse a record out of *data*, or return ``None``.

        STRICT and never raises: a record that is "close enough" would be
        replayed as a turn, which is worse than not replaying at all. Every
        field is required except ``last_tool`` and ``reason``; types are
        checked with ``type(v) is int`` where a bool must not pass (``True`` IS
        an int, and ``attempts=True`` must not become attempt 1); an unknown
        ``schema`` -- including a NEWER one -- is refused rather than guessed
        at.
        """
        if not isinstance(data, dict):
            return None
        if type(data.get("schema")) is not int or data.get("schema") != SCHEMA_VERSION:
            return None
        for name in ("query_id", "query", "requested_by", "created_at", "updated_at"):
            if not isinstance(data.get(name), str):
                return None
        if data.get("status") not in JOURNAL_STATUSES:
            return None
        attempts = data.get("attempts")
        if type(attempts) is not int or not 0 <= attempts <= MAX_ATTEMPTS:
            return None
        reason = data.get("reason")
        if reason is not None and not isinstance(reason, str):
            return None
        last_tool = data.get("last_tool")
        if last_tool is not None:
            if not isinstance(last_tool, dict):
                return None
            if not isinstance(last_tool.get("name"), str) or not isinstance(
                last_tool.get("args_hash"), str
            ):
                return None
            last_tool = {
                "name": last_tool["name"],
                "args_hash": last_tool["args_hash"],
            }
        try:
            return cls(
                query_id=data["query_id"],
                query=data["query"],
                status=data["status"],
                attempts=attempts,
                requested_by=data["requested_by"],
                created_at=data["created_at"],
                updated_at=data["updated_at"],
                last_tool=last_tool,
                reason=reason,
            )
        except (ValueError, TypeError):
            return None


def journal_path(worker_dir: Path) -> Path:
    """The journal file for *worker_dir* (same directory as ``command.json``)."""
    return Path(worker_dir) / INFLIGHT_FILENAME


def _read_payload(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read the raw JSON object at *path*, or ``(None, reason)``.

    ``(None, None)`` means "no journal", which is the common case and must stay
    as cheap as an ``is_file()`` check. A corrupt or non-object payload reports
    its own reason and leaves the file ALONE: it is the only evidence of a
    record that could not be used.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "cannot read inflight journal: %s" % exc
    except UnicodeDecodeError:
        return None, _CORRUPT_JSON_REASON
    try:
        data = json.loads(text)
    except ValueError:
        return None, _CORRUPT_JSON_REASON
    if not isinstance(data, dict):
        return None, _CORRUPT_JSON_REASON
    return data, None


def _raw_fence_axes(data: object) -> Optional[Dict[str, Any]]:
    """The fence axes present in *data*, RAW and unvalidated, or ``None``.

    Used only by :func:`read_inflight_unfenced`, whose caller needs to know
    whose record it found rather than whether it may act on it: nothing here is
    type-checked or compared, and an axis missing from *data* is missing from
    the result instead of being filled in -- the gap is the diagnosis. ``None``
    means "no fence axes at all", which only a non-dict payload produces.
    """
    if not isinstance(data, dict):
        return None
    axes = {
        name: data[name]
        for name in ("session_id", "worker_name", "instance_id", "epoch")
        if name in data
    }
    return axes or None


def write_inflight(
    worker_dir: Path,
    record: InflightRecord,
    fence: CommandFence,
) -> Optional[str]:
    """Atomically write *record* (stamped with *fence*) into *worker_dir*.

    Returns ``None`` on success, or a short reason on failure. Never raises,
    and never leaves a partial ``inflight.json``: the payload goes to a temp
    file in the SAME directory, is flushed and fsynced, and is then
    ``os.replace``-d over the live name; the temp file is unlinked on every
    failure path.

    The write is fenced in the sense that matters here: the record is STAMPED
    with the caller's four fence axes, so a dead incarnation cannot write a
    record that claims to belong to a live one. It deliberately does not
    REFUSE a stale record on disk -- a successor instance 1 reuses the same
    directory with a new epoch, and refusing there would wedge the recovery
    path. A displaced record whose fence disagrees is reported (not silently
    dropped): the reason names the disagreeing axes in
    ``fence_rejection_reason``'s vocabulary.
    """
    worker_dir = Path(worker_dir)
    target = journal_path(worker_dir)
    try:
        worker_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "cannot create worker dir: %s" % exc

    existing, existing_reason = _read_payload(target)
    if existing is not None:
        displaced = fence_rejection_reason(fence, existing)
        if displaced is not None:
            # A record for another incarnation is being replaced. Report it:
            # the fence quarantines it exactly like a stale command, so nothing
            # here needs a high-water map of its own.
            _warn(
                "Replacing an inflight journal record addressed to another "
                "incarnation (worker '%s', instance_id=%s, epoch=%s): %s"
                % (
                    fence.worker_name,
                    fence.instance_id,
                    fence.epoch,
                    displaced,
                )
            )
    elif existing_reason is not None:
        _warn(
            "Replacing an unusable inflight journal for worker '%s' "
            "(instance_id=%s): %s"
            % (fence.worker_name, fence.instance_id, existing_reason)
        )

    payload = record.to_payload(fence)
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(worker_dir),
            prefix=".%s." % INFLIGHT_FILENAME,
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, str(target))
        tmp_path = None  # the replace consumed it
        return None
    except OSError as exc:
        return "cannot write inflight journal: %s" % exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def read_inflight(
    worker_dir: Path,
    fence: CommandFence,
) -> Tuple[Optional[InflightRecord], Optional[str]]:
    """Read the journal record for *fence*, as ``(record, reason)``.

    ``(None, None)``      -- nothing journalled for this worker. Normal.
    ``(record, None)``    -- a usable record.
    ``(None, <reason>)``  -- unusable, and the reason says why: a corrupt file,
                            a malformed record, or a fence disagreement named
                            in ``fence_rejection_reason``'s vocabulary.

    A fenced-away record is QUARANTINED, not deleted: it is reported and left
    on disk exactly like a stale command, which is why this path needs no
    high-water map. Never raises -- a journal must not be able to fail a turn.
    """
    target = journal_path(worker_dir)
    data, read_reason = _read_payload(target)
    if data is None:
        if read_reason is not None:
            _warn(
                "Unusable inflight journal for worker '%s' "
                "(instance_id=%s, epoch=%s): %s"
                % (fence.worker_name, fence.instance_id, fence.epoch, read_reason)
            )
        return None, read_reason

    reason = fence_rejection_reason(fence, data)
    if reason is not None:
        # Same rule as a stale command: report, do not obey, do not delete.
        _warn(
            "Quarantined an inflight journal record for worker '%s' "
            "(instance_id=%s, epoch=%s): %s"
            % (fence.worker_name, fence.instance_id, fence.epoch, reason)
        )
        return None, reason

    record = InflightRecord.from_payload(data)
    if record is None:
        _warn(
            "Ignored a malformed inflight journal record for worker '%s' "
            "(instance_id=%s, epoch=%s): %s"
            % (fence.worker_name, fence.instance_id, fence.epoch, _MALFORMED_REASON)
        )
        return None, _MALFORMED_REASON
    return record, None


def read_inflight_unfenced(
    worker_dir: Path,
) -> Tuple[Optional[InflightRecord], Optional[Dict[str, Any]], Optional[str]]:
    """Read the journal record REGARDLESS of which incarnation wrote it.

    WARNING: ADOPTION READER ONLY. There is exactly ONE legitimate caller --
    the S7b-4 adoption path at resume/spawn, which takes over the record a DEAD
    incarnation left in a directory the successor now owns (instance 1 keeps the
    bare name, so a successor instance 1 at a new epoch reuses it). With the
    fenced reader that record is quarantined -- "epoch 10 != 11" -- which is
    correct for every same-incarnation caller and fatal for the adopter, which
    is why this second reader exists.

    Every SAME-INCARNATION path -- the S7b-2 write points, the park flip, the
    terminal clear -- MUST use :func:`read_inflight` / :func:`clear_inflight`.
    A same-incarnation caller of THIS function is a bug: it would let a live
    incarnation act on a record it does not own. Do not add a bypass flag to
    the fenced reader instead of calling this -- the separate name is the
    safety property, not an inconvenience.

    Returns ``(record, fence_axes, reason)``:

    ``(None, None, None)``       -- nothing journalled. Normal.
    ``(record, axes, None)``     -- a usable record, whoever wrote it.
    ``(None, axes, <reason>)``   -- unusable, and the reason says why: a
                                   corrupt file or a malformed record.

    *fence_axes* is the raw on-disk mapping of ``session_id`` /
    ``worker_name`` / ``instance_id`` / ``epoch`` as FOUND -- deliberately not
    validated and not compared against any caller's fence, because its purpose
    is to tell the adopter whose record this is. It is ``None`` when no journal
    exists or the payload is unreadable, and an incomplete mapping is returned
    as-is rather than completed: the missing axis is the diagnosis. On the
    malformed-record path the axes are returned too, so the adopter can name
    the dead incarnation it declined to adopt.

    Never raises, and never deletes: a corrupt or malformed record is reported
    and left on disk, exactly as the fenced reader leaves it. Reading is not
    taking over -- the adoption WRITE (S7b-4) restamps the record, and only
    that write changes ownership.
    """
    # Reviewers: an "unfenced" read in a same-incarnation path is the bug this
    # function is named to make visible. See the WARNING above.
    target = journal_path(worker_dir)
    data, read_reason = _read_payload(target)
    if data is None:
        if read_reason is not None:
            _warn(
                "Unusable inflight journal in '%s' (unfenced adoption read): %s"
                % (target, read_reason)
            )
        return None, None, read_reason

    axes = _raw_fence_axes(data)
    record = InflightRecord.from_payload(data)
    if record is None:
        _warn(
            "Ignored a malformed inflight journal record in '%s' "
            "(unfenced adoption read, fence axes found: %s): %s"
            % (target, axes, _MALFORMED_REASON)
        )
        return None, axes, _MALFORMED_REASON
    return record, axes, None


def clear_inflight(worker_dir: Path, fence: CommandFence) -> Optional[str]:
    """Delete the journal record iff it is fenced to *fence*.

    Returns ``None`` when the record was removed OR when there was nothing to
    remove (idempotent), otherwise a short reason. Never raises.

    A record that is not this incarnation's is LEFT INTACT -- a dead
    incarnation must not be able to clear a live record -- and a corrupt one is
    left intact too: with no fence to compare there is nothing to authorise the
    delete, and the file is the evidence. That is the same "report, do not
    delete" rule the read path follows, and it is why the fence removes the
    need for a high-water map here.
    """
    target = journal_path(worker_dir)
    data, read_reason = _read_payload(target)
    if data is None:
        if read_reason is None:
            return None  # nothing journalled: already clear
        _warn(
            "Refused to clear an unusable inflight journal for worker '%s' "
            "(instance_id=%s, epoch=%s): %s"
            % (fence.worker_name, fence.instance_id, fence.epoch, read_reason)
        )
        return read_reason

    reason = fence_rejection_reason(fence, data)
    if reason is not None:
        _warn(
            "Refused to clear an inflight journal record addressed to another "
            "incarnation (worker '%s', instance_id=%s, epoch=%s): %s"
            % (fence.worker_name, fence.instance_id, fence.epoch, reason)
        )
        return reason

    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        return "cannot clear inflight journal: %s" % exc
    return None
