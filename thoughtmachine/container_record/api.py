"""Container Record — lookup / mutation API (§3).

Implements the seven documented functions (§3) plus the two-phase create/attach
helpers used by call sites that must record intent *before* the container is
created and complete the record once Docker has assigned an id:

* :func:`create_record` — the documented create (§3): a natively authored
  record (``docker_id`` empty, ``state`` at its §1 default ``""``).
* :func:`begin_record` — pre-run intent stub: persists ``state="creating"``
  (a value §1 does not name; flagged as a proposal).
* :func:`attach_container` — completion step: binds ``docker_id``/``state``.
* :func:`record_label` — the single record-owned Docker label (§7).
* :func:`read_event_log` — merged reader over embedded + sidecar entries (§2.4).

Fail-closed semantics follow §3.1: lookups return ``None`` on a benign miss;
mutations raise :class:`RecordNotFound`; corrupt JSON is quarantined and raises
:class:`RecordCorrupt`; a lock timeout raises :class:`RecordLocked`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import storage
from .models import (
    EVENT_ENTRY_KEYS,
    INTENT_SNAPSHOT_KEYS,
    SCHEMA_FIELD_NAMES,
    SCHEMA_VERSION_CURRENT,
    STATE_CREATING,
    Record,
    RecordNotFound,
    iso_now,
    new_record_id,
    validate_lifecycle_class,
    validate_owner,
)

#: The one record-owned Docker label (§7).  Its value is the record's ``id``.
RECORD_LABEL_KEY = "thoughtmachine.container_id"

#: Marker ``type`` of the §2.4 event-log pointer entry.
EVENT_LOG_POINTER_TYPE = "event_log_pointer"

#: Fields a caller may never change through ``update_record``.  ``id`` is a
#: bound positional parameter of ``update_record`` (and cannot appear in
#: ``**changes``), so it needs no runtime guard here (R5).
_IMMUTABLE_FIELDS = frozenset({"created_at"})


# ── Event-log pointer (§2.4) ─────────────────────────────────────────────────


def _is_event_log_pointer(entry: Any) -> bool:
    """Return True if *entry* is the §2.4 event-log pointer entry."""
    return isinstance(entry, dict) and entry.get("type") == EVENT_LOG_POINTER_TYPE


def _event_log_pointer(record_id: str, count: int) -> dict:
    """Return the embedded event-log pointer entry (§2.4).

    Proposed shape, pending operator ratification: §2.4 fixes only the
    behaviour ("the embedded ``event_log`` becomes a pointer to it"), not the
    pointer's wire shape.  It lives *inside* ``event_log`` so the on-disk JSON
    still carries exactly the 13 §1 fields — never a 14th key.
    """
    return {
        "type": EVENT_LOG_POINTER_TYPE,
        "sidecar": f"{record_id}{storage.EVENTS_SUFFIX}",
        "count": int(count),
    }


def _count_sidecar_entries(path: Path) -> int:
    """Return the number of non-blank lines in the event sidecar."""
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def record_label(record_id: str) -> dict[str, str]:
    """Return the single record-owned Docker label (§7).

    ``{"thoughtmachine.container_id": <record_id>}`` — the only label the
    record system adds; infra labels stay during the transition.
    """
    return {RECORD_LABEL_KEY: str(record_id)}


# ── Create / begin / attach (two-phase) ──────────────────────────────────────


def _create(
    workspace_id: str,
    lifecycle_class: str,
    owner: str,
    purpose: str,
    intent_snapshot: dict | None,
    id: str | None,
    state: str,
    vault_root: str | os.PathLike | None,
) -> Record:
    """Shared create path for :func:`create_record` / :func:`begin_record`."""
    validate_lifecycle_class(lifecycle_class)
    validate_owner(owner)

    record_id = str(id) if id is not None else new_record_id()
    path = storage.record_path(workspace_id, record_id, vault_root)

    with storage.record_lock(storage.lock_path(workspace_id, record_id, vault_root)):
        if path.is_file():
            # Idempotent re-materialisation: an existing record is returned
            # unchanged (§3, caller-supplied ``id``).
            return Record.from_dict(storage.read_record_file(path) or {})
        now = iso_now()
        record = Record(
            id=record_id,
            lifecycle_class=lifecycle_class,
            owner=owner,
            docker_id="",
            purpose=purpose,
            intent_snapshot=dict(intent_snapshot or {}),
            notes="",
            event_log=[],
            schema_version=SCHEMA_VERSION_CURRENT,
            inferred=False,
            state=state,
            created_at=now,
            updated_at=now,
        )
        storage.write_record_file(path, record.to_dict())
    return record


def create_record(
    workspace_id: str,
    lifecycle_class: str,
    owner: str,
    purpose: str = "",
    intent_snapshot: dict | None = None,
    id: str | None = None,
    *,
    vault_root: str | os.PathLike | None = None,
) -> Record:
    """Create (or idempotently re-materialise) a record (§3, doc signature).

    Natively authored records carry ``schema_version=1`` / ``inferred=False``;
    ``docker_id`` is empty (§1 default) and ``state`` takes its §1 default
    (``""``, "last observed container state").  Supplying ``id`` for an existing
    record returns it unchanged (idempotent re-materialisation, §3).
    """
    return _create(
        workspace_id,
        lifecycle_class,
        owner,
        purpose,
        intent_snapshot,
        id,
        state="",
        vault_root=vault_root,
    )


def begin_record(
    workspace_id: str,
    lifecycle_class: str,
    owner: str,
    purpose: str = "",
    intent_snapshot: dict | None = None,
    id: str | None = None,
    *,
    vault_root: str | os.PathLike | None = None,
) -> Record:
    """Persist a pre-run intent stub: ``state="creating"``, ``docker_id=""``.

    §1 names no pre-run state; ``"creating"`` is a **proposal** pending operator
    ratification.  Idempotent on a caller-supplied ``id`` (returns the existing
    record unchanged).
    """
    return _create(
        workspace_id,
        lifecycle_class,
        owner,
        purpose,
        intent_snapshot,
        id,
        state=STATE_CREATING,
        vault_root=vault_root,
    )


def attach_container(
    workspace_id: str,
    id: str,
    docker_id: str,
    state: str | None = None,
    *,
    intent_snapshot: Mapping[str, Any] | None = None,
    vault_root: str | os.PathLike | None = None,
) -> Record:
    """Complete a pre-run record once Docker has assigned a container id.

    ``state`` (when supplied) overwrites the record's ``state``; when ``None``
    the existing state is left unchanged.

    ``intent_snapshot`` (when supplied) is written into the record's
    ``intent_snapshot`` field in the *same* ``update_record`` call; when
    ``None`` the existing snapshot is left untouched (no-op).
    """
    changes: dict[str, Any] = {"docker_id": docker_id}
    if state is not None:
        changes["state"] = state
    if intent_snapshot is not None:
        changes["intent_snapshot"] = intent_snapshot
    return update_record(workspace_id, id, vault_root=vault_root, **changes)


# ── Reads ────────────────────────────────────────────────────────────────────


def load_record(
    workspace_id: str, id: str, vault_root: str | os.PathLike | None = None
) -> Record | None:
    """Return the record for *id*, or ``None`` when it does not exist (§3.1)."""
    path = storage.record_path(workspace_id, id, vault_root)
    data = storage.read_record_file(path)
    if data is None:
        return None
    return Record.from_dict(data)


def list_records(
    workspace_id: str, vault_root: str | os.PathLike | None = None
) -> list[Record]:
    """Return every record in *workspace_id*.

    A corrupt file is quarantined (``<id>.json`` → ``<id>.json.corrupt-<ts>``)
    and :class:`RecordCorrupt` is raised (§3.1 line 225) — never a silent skip.
    Self-healing: because the corrupt file is renamed away, an immediate retry
    sees only the good files and succeeds.
    """
    containers = storage.containers_dir(workspace_id, vault_root)
    records: list[Record] = []
    for path in storage.iter_record_files(containers):
        data = storage.read_record_file(path)
        if data is not None:
            records.append(Record.from_dict(data))
    return records


def find_by_docker_label(
    label_value: str, vault_root: str | os.PathLike | None = None
) -> Record | None:
    """Resolve the record named by the record-owned label value (§7).

    ``thoughtmachine.container_id`` carries the record's ``id`` (§7: "the value
    must be a token the record, not Docker, owns"), so the doc-sanctioned
    lookup is a cross-workspace id lookup.  No ``docker_id``-field fallback is
    offered (§7 does not sanction one).
    """
    for workspace_id in storage.iter_workspace_ids(vault_root):
        path = storage.record_path(workspace_id, label_value, vault_root)
        if path.is_file():
            return Record.from_dict(storage.read_record_file(path) or {})
    return None


def read_event_log(
    workspace_id: str,
    record_id: str,
    *,
    vault_root: str | os.PathLike | None = None,
) -> list[dict]:
    """Return the full merged event log (§1.2 / §2.4).

    Appended sidecar entries first, then the embedded entries.  The §2.4
    pointer entry (if any) is filtered out.  A missing record yields ``[]``
    (benign miss); a corrupt record is quarantined and raises
    :class:`RecordCorrupt`.
    """
    path = storage.record_path(workspace_id, record_id, vault_root)
    data = storage.read_record_file(path)
    embedded: list[dict] = []
    if isinstance(data, dict):
        raw = data.get("event_log")
        if isinstance(raw, list):
            embedded = [entry for entry in raw if not _is_event_log_pointer(entry)]
    sidecar_entries = storage.read_events_sidecar(
        storage.events_path(workspace_id, record_id, vault_root)
    )
    return sidecar_entries + embedded


# ── Mutations ────────────────────────────────────────────────────────────────


def update_record(
    workspace_id: str,
    id: str,
    vault_root: str | os.PathLike | None = None,
    **changes: Any,
) -> Record:
    """Apply *changes* to a record, bumping ``updated_at``.

    Raises :class:`RecordNotFound` when the record does not exist, and
    ``ValueError`` for unknown or immutable fields (``id`` is a bound
    positional parameter, so it is rejected at the call boundary).
    """
    unknown = set(changes) - set(SCHEMA_FIELD_NAMES)
    if unknown:
        raise ValueError(f"unknown record field(s): {', '.join(sorted(unknown))}")
    immutable = set(changes) & _IMMUTABLE_FIELDS
    if immutable:
        raise ValueError(f"cannot modify field(s): {', '.join(sorted(immutable))}")

    path = storage.record_path(workspace_id, id, vault_root)
    with storage.record_lock(storage.lock_path(workspace_id, id, vault_root)):
        data = storage.read_record_file(path)
        if data is None:
            raise RecordNotFound(f"record not found: {workspace_id}/{id}")
        record = Record.from_dict(data)
        for key, value in changes.items():
            setattr(record, key, value)
        record.updated_at = iso_now()
        storage.write_record_file(path, record.to_dict())
    return record


def append_event(
    workspace_id: str,
    id: str,
    event_type: str,
    actor: str,
    vault_root: str | os.PathLike | None = None,
    **payload: Any,
) -> Record:
    """Append an event to the record's log (§1.2), bumping ``updated_at``.

    The log stays embedded below the 256 KiB ceiling; once an append would
    exceed it, the log moves to the ``<id>.events.jsonl`` sidecar and the
    embedded ``event_log`` becomes a pointer to it (§2.4).  Further appends go
    straight to the sidecar and refresh the pointer's ``count``.
    """
    entry = {
        "timestamp": iso_now(),
        "event_type": event_type,
        "actor": actor,
        "payload": dict(payload),
    }

    path = storage.record_path(workspace_id, id, vault_root)
    sidecar = storage.events_path(workspace_id, id, vault_root)
    with storage.record_lock(storage.lock_path(workspace_id, id, vault_root)):
        data = storage.read_record_file(path)
        if data is None:
            raise RecordNotFound(f"record not found: {workspace_id}/{id}")
        record = Record.from_dict(data)
        record.updated_at = iso_now()

        if sidecar.is_file():
            # Already moved: append to the sidecar, refresh the pointer count.
            storage.append_event_line(sidecar, entry)
            record.event_log = [
                _event_log_pointer(record.id, _count_sidecar_entries(sidecar))
            ]
            storage.write_record_file(path, record.to_dict())
            return record

        record.event_log.append(entry)
        size = len(json.dumps(record.to_dict()).encode("utf-8"))
        if size > storage.EVENT_LOG_CEILING_BYTES:
            # Move every real entry to the sidecar; embedded becomes a pointer.
            storage.write_events_sidecar(sidecar, record.event_log)
            record.event_log = [
                _event_log_pointer(record.id, _count_sidecar_entries(sidecar))
            ]
        storage.write_record_file(path, record.to_dict())
    return record


def delete_record(
    workspace_id: str, id: str, vault_root: str | os.PathLike | None = None
) -> None:
    """Delete a record (and its event sidecar).

    Raises :class:`RecordNotFound` when the record does not exist.
    """
    path = storage.record_path(workspace_id, id, vault_root)
    with storage.record_lock(storage.lock_path(workspace_id, id, vault_root)):
        if not path.is_file():
            raise RecordNotFound(f"record not found: {workspace_id}/{id}")
        os.unlink(path)
        sidecar = storage.events_path(workspace_id, id, vault_root)
        if sidecar.is_file():
            os.unlink(sidecar)


__all__ = [
    "RECORD_LABEL_KEY",
    "EVENT_LOG_POINTER_TYPE",
    "EVENT_ENTRY_KEYS",
    "INTENT_SNAPSHOT_KEYS",
    "record_label",
    "create_record",
    "begin_record",
    "attach_container",
    "load_record",
    "list_records",
    "find_by_docker_label",
    "read_event_log",
    "update_record",
    "append_event",
    "delete_record",
]
