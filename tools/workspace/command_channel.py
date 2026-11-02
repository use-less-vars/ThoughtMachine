# tools/workspace/command_channel.py
"""Fenced ``command.json`` channel for cross-process worker control.

The worker command channel is one file in the worker's directory
(``<workspace>/workers/<instance_label>/command.json``). Today it carries
``{"action": ...}`` and nothing else, which produces two defects that no
amount of care at the call sites can remove:

Identity fence
--------------
A command carries no identity, so any process that resolves the same
directory can act on a worker it never meant to address -- two workers
sharing a directory, a stale command left over from a previous
incarnation, or a spawn-time cleanup racing a foreign stop. Every command
written through :func:`write_command` therefore carries a
:class:`CommandFence` (``session_id`` / ``worker_name`` / ``instance_id``
/ ``epoch``), and the consumer MUST compare it against its own fence
before acting (:func:`fence_rejection_reason` names the disagreeing axis
for the log line).

``epoch`` is the worker's incarnation counter and is deliberately NOT a
second counter invented here: it is ``WorkerThread._generation``, which is
re-allocated on every spawn and persisted next to ``status.json``. A fence
epoch that could drift from the worker's real generation would reject
valid commands, so there is exactly one source for it.

Read/unlink race
----------------
The obvious consumer reads the live file and then unlinks it. A command
written between those two steps is destroyed without ever being acted on,
and it is unrecoverable -- the writer got no acknowledgement. Every known
action in the old read/unlink loop had that window. :func:`claim_command`
removes it by ``os.replace``-ing the live file to a private claimed name
and reading THAT: the consumer never reads and never unlinks the live
path, so a command written immediately after a claim survives untouched
for the next poll. Consumption is a rename, which is atomic, so two
consumers cannot both claim the same command.

Stdlib only. Nothing here raises for the caller: write / claim /
quarantine report failure through their return value, because the existing
call sites swallow ``OSError`` today and their control flow must not
change.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: Name of the live command file inside a worker directory.
COMMAND_FILENAME = "command.json"

#: Infix for quarantined (rejected) commands: ``command.json.rejected-<ts>-<slug>``.
REJECTED_MARKER = ".rejected-"

#: ``(field name, required type)`` in payload order. ``int`` fields are
#: checked with ``type(v) is int`` -- see ``CommandFence.from_payload``.
_FENCE_FIELDS: Tuple[Tuple[str, type], ...] = (
    ("session_id", str),
    ("worker_name", str),
    ("instance_id", int),
    ("epoch", int),
)

#: Upper bound for the sanitised ``fence_repr`` slug. Long enough to keep a
#: session id + worker name readable, short enough that
#: ``command.json.rejected-<ts>-<slug>[-<n>]`` stays far below NAME_MAX.
_MAX_SLUG_LEN = 64


@dataclass(frozen=True)
class CommandFence:
    """Identity of the worker incarnation a command is addressed to.

    ``epoch`` is the worker's incarnation counter -- ``WorkerThread._generation``,
    which is re-allocated on every spawn and persisted alongside
    ``status.json``. It is NOT a second counter: a fence epoch that could
    drift from the worker's real generation would reject valid commands.

    Frozen so a fence can be shared with the thread that owns it without
    the payload it was written from silently changing underneath.
    """

    session_id: str
    worker_name: str
    instance_id: int
    epoch: int

    def matches(self, other: "CommandFence") -> bool:
        """True only when all four fence axes are equal.

        Compared axis by axis rather than by ``==``-on-a-tuple: each of the
        four is a separate identity claim, and a future field must not
        silently join the comparison.
        """
        return all(
            getattr(self, name) == getattr(other, name) for name, _ in _FENCE_FIELDS
        )

    def to_payload(self) -> Dict[str, Any]:
        """The four fence keys, ready to be merged into a command payload."""
        return {name: getattr(self, name) for name, _ in _FENCE_FIELDS}

    @classmethod
    def from_payload(cls, data: object) -> Optional["CommandFence"]:
        """Parse a fence out of *data*, or return ``None`` if it has none.

        STRICT by design, and never raises: a fence that is "close enough"
        is worse than no fence at all, because it would let a foreign
        command through while looking safe. Rejected: a non-dict, a missing
        key, ``None``, a numeric string, and a bool where an ``int`` is
        required -- ``type(v) is int`` rather than ``isinstance`` precisely
        because ``True`` IS an ``int`` and would otherwise match
        ``instance_id=1``.
        """
        if not isinstance(data, dict):
            return None
        values: list = []
        for name, expected_type in _FENCE_FIELDS:
            if name not in data:
                return None
            value = data[name]
            if type(value) is not expected_type:  # noqa: E721 - see docstring
                return None
            values.append(value)
        return cls(*values)


def _slugify(value: str, max_len: int = _MAX_SLUG_LEN) -> str:
    """Reduce *value* to a filesystem-safe slug.

    Session ids and worker names are user-influenced and may contain path
    separators, ``..``, whitespace or NUL, so every character outside
    ``[A-Za-z0-9_-]`` becomes ``_``. ASCII-only on purpose: ``str.isalnum``
    is true for non-ASCII letters, which are safe on Linux but pointless in
    a diagnostic filename.
    """
    slug = "".join(
        ch if (ch.isascii() and ch.isalnum()) or ch in "-_" else "_"
        for ch in str(value)
    )
    slug = slug[:_MAX_SLUG_LEN].strip("_")
    return slug or "unknown"


def write_command(
    worker_dir: Path,
    action: str,
    fence: CommandFence,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Atomically write a fenced command into *worker_dir*.

    Returns True on success and False on ``OSError`` -- it never raises,
    because both existing call sites swallow ``OSError`` today and their
    control flow must not change.

    The write is atomic (temp file in the SAME directory so ``os.replace``
    cannot cross a filesystem boundary, ``flush`` + ``fsync`` before the
    replace) because the reader may poll at any instant: truncate-then-write
    leaves a zero-length or half-written file visible, and to the consumer a
    partial file is indistinguishable from corruption -- which used to be
    deleted, silently losing a real stop/pause. The temp file is removed on
    every failure path so a failed write leaves no debris.

    ``reason`` is optional diagnostics, emitted only when given, so a plain
    command's payload stays byte-identical to the old one apart from the
    fence.
    """
    payload: Dict[str, Any] = {"action": action}
    payload.update(fence.to_payload())
    if reason is not None:
        payload["reason"] = reason

    target = worker_dir / COMMAND_FILENAME
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(worker_dir),
            prefix=".%s." % COMMAND_FILENAME,
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, str(target))
        tmp_path = None  # the replace consumed it
        return True
    except OSError:
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def claim_command(worker_dir: Path) -> Optional[Tuple[Path, str]]:
    """Atomically claim the live command file and return ``(path, text)``.

    Returns ``None`` when there is no command to claim (or when the claimed
    file could not be read).

    This is the race killer. The live ``command.json`` is ``os.replace``-d
    to a uniquely named claimed path and the CLAIMED file is what gets read,
    so the consumer never reads and never unlinks the live path: a command
    written immediately after a claim survives, untouched, for the next
    poll. The rename is also what makes consumption exclusive -- two
    consumers cannot both claim one command, and a command is never
    half-consumed.

    If the read fails after a successful move the claimed file is LEFT ON
    DISK: it is the only evidence of a command that arrived and was not
    acted on, and the caller can quarantine it (:func:`quarantine`).
    """
    live = worker_dir / COMMAND_FILENAME
    claimed = worker_dir / ("%s.claimed-%s" % (COMMAND_FILENAME, uuid.uuid4().hex))
    try:
        os.replace(str(live), str(claimed))
    except OSError:
        # Includes FileNotFoundError: no live command, which is the common
        # case and must stay as cheap as the old is_file() check.
        return None
    try:
        return claimed, claimed.read_text(encoding="utf-8")
    except OSError:
        return None


def discard(path: Path) -> None:
    """Unlink a CLAIMED command path, swallowing ``OSError``.

    Only ever called with a path returned by :func:`claim_command` -- never
    with the live ``command.json``, which this module's consumer must not
    delete (that is exactly the read/unlink race). Idempotent.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def quarantine(path: Path, fence_repr: str) -> Optional[Path]:
    """Rename a CLAIMED command path to a ``.rejected-`` sibling.

    Returns the new path, or ``None`` when the rename failed.

    MUST NEVER DELETE: a rejected command is the only evidence that
    something addressed this worker and was not obeyed, so it stays on disk
    for post-mortem (the same rule ``vault_repair`` and
    ``container_record.storage`` follow for their own rejects). A second
    rejection with the same ``fence_repr`` therefore gets a numeric suffix
    rather than overwriting the first.

    ``fence_repr`` is sanitised (session ids and worker names are
    user-influenced) so the destination can never escape the worker
    directory, and the result is always a sibling of the claimed file --
    never the live ``command.json`` name.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = "%s%s%s-%s" % (COMMAND_FILENAME, REJECTED_MARKER, stamp, _slugify(fence_repr))
    parent = path.parent
    dest = parent / stem
    counter = 1
    while dest.exists():
        dest = parent / ("%s-%d" % (stem, counter))
        counter += 1
    try:
        os.replace(str(path), str(dest))
    except OSError:
        return None
    return dest


def fence_rejection_reason(expected: CommandFence, payload: object) -> Optional[str]:
    """Why *payload* must not be obeyed by the worker owning *expected*.

    Returns ``None`` when the payload carries a fence that matches
    *expected*. Otherwise a short reason for the log line, naming exactly
    which axes disagree (``"instance_id 2 != 1"``), or ``"no fence in
    payload"`` when the payload has no fence at all, or ``"malformed fence
    in payload"`` when it has all four keys with unusable types. Never
    raises.
    """
    if not isinstance(payload, dict):
        return "no fence in payload"
    if any(name not in payload for name, _ in _FENCE_FIELDS):
        return "no fence in payload"
    actual = CommandFence.from_payload(payload)
    if actual is None:
        return "malformed fence in payload"
    if actual.matches(expected):
        return None
    disagreements = []
    for name, _ in _FENCE_FIELDS:
        want = getattr(expected, name)
        got = getattr(actual, name)
        if got != want:
            disagreements.append("%s %r != %r" % (name, got, want))
    return ", ".join(disagreements)
