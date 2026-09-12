"""Container Record — schema model, constants and validation.

Implements §1 of ``docs/container_record_design.md``: the 13-field record
schema, the nested ``intent_snapshot`` shape, the ``event_log`` entry shape,
and the record-owned identity rules (§2.2).

The ``Record`` dataclass is the "thin typed wrapper" the design doc describes
around the parsed JSON object.  ``to_dict``/``from_dict`` are the only
serialisation surface; ``from_dict`` is deliberately lenient (missing keys fall
back to schema defaults) while writers are expected to go through the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Exceptions (fail-closed, §3.1) ──────────────────────────────────────────


class ContainerRecordError(Exception):
    """Base class for every container-record error."""


class RecordNotFound(ContainerRecordError):
    """Raised by a mutator when the target record does not exist (§3.1)."""


class RecordCorrupt(ContainerRecordError):
    """Raised when a record file exists but is not a parseable record (§3.1)."""


class RecordLocked(ContainerRecordError):
    """Raised when the per-record lock cannot be acquired in time (§2.3)."""


# ── Schema constants (§1) ───────────────────────────────────────────────────

#: ``schema_version`` for records synthesised from live legacy Docker state.
SCHEMA_VERSION_LEGACY = 0

#: ``schema_version`` for records authored natively (never synthesised).
SCHEMA_VERSION_CURRENT = 1

LIFECYCLE_EPHEMERAL = "ephemeral"
LIFECYCLE_PERSISTENT = "persistent"
LIFECYCLE_RESOURCE = "resource"
LIFECYCLE_SERVICE = "service"
LIFECYCLE_CLASSES: tuple[str, ...] = (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
)

OWNER_WORKSPACE = "workspace-owned"
OWNER_SYSTEM = "system-owned"
OWNER_VALUES: tuple[str, ...] = (OWNER_WORKSPACE, OWNER_SYSTEM)

#: Pre-run intent state persisted by ``api.begin_record`` (``docker_id=""``).
#: §1 names no pre-run state; ``creating`` is a proposal pending operator
#: ratification (see the subsystem report).
STATE_CREATING = "creating"

#: Field order of §1 (also the on-disk JSON key order).
SCHEMA_FIELD_NAMES: tuple[str, ...] = (
    "id",
    "docker_id",
    "lifecycle_class",
    "owner",
    "purpose",
    "intent_snapshot",
    "notes",
    "event_log",
    "schema_version",
    "inferred",
    "state",
    "created_at",
    "updated_at",
)

#: Nested ``intent_snapshot`` field order (§1.1).
INTENT_SNAPSHOT_KEYS: tuple[str, ...] = (
    "network_mode",
    "workspace_mode",
    "mem_limit",
    "cpu_quota",
    "oom_score_adj",
    # ``image_ref`` preserves the human-facing image reference recorded at
    # container start (e.g. ``python:3.12``); it is re-taggable and can drift
    # from the immutable digest captured in ``image_hash`` below.
    "image_ref",
    "image_hash",
    "hardening",
)

#: ``event_log`` entry field order (§1.2).
EVENT_ENTRY_KEYS: tuple[str, ...] = (
    "timestamp",
    "event_type",
    "actor",
    "payload",
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def new_record_id() -> str:
    """Return a fresh record-owned opaque identifier (UUID4 string, §2.2)."""
    return str(uuid.uuid4())


def validate_lifecycle_class(value: str) -> str:
    """Return *value* if it is a valid lifecycle class, else raise."""
    if value not in LIFECYCLE_CLASSES:
        raise ValueError(
            f"invalid lifecycle_class {value!r}; expected one of "
            f"{', '.join(LIFECYCLE_CLASSES)}"
        )
    return value


def validate_owner(value: str) -> str:
    """Return *value* if it is a valid owner, else raise."""
    if value not in OWNER_VALUES:
        raise ValueError(
            f"invalid owner {value!r}; expected one of {', '.join(OWNER_VALUES)}"
        )
    return value


# ── Record ──────────────────────────────────────────────────────────────────


@dataclass
class Record:
    """A single container record (§1).

    ``id`` / ``lifecycle_class`` / ``owner`` are required; every other field
    falls back to its §1 default.
    """

    id: str
    lifecycle_class: str
    owner: str
    docker_id: str = ""
    purpose: str = ""
    intent_snapshot: dict = field(default_factory=dict)
    notes: str = ""
    event_log: list = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION_LEGACY
    inferred: bool = False
    state: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a plain dict in §1 field order."""
        return {
            "id": self.id,
            "docker_id": self.docker_id,
            "lifecycle_class": self.lifecycle_class,
            "owner": self.owner,
            "purpose": self.purpose,
            "intent_snapshot": dict(self.intent_snapshot),
            "notes": self.notes,
            "event_log": list(self.event_log),
            "schema_version": int(self.schema_version),
            "inferred": bool(self.inferred),
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        """Build a record from a parsed JSON dict (lenient, defaults §1)."""
        if not isinstance(data, dict):
            raise ValueError("record data must be a JSON object")

        intent = data.get("intent_snapshot")
        if not isinstance(intent, dict):
            intent = {}

        event_log = data.get("event_log")
        if not isinstance(event_log, list):
            event_log = []

        return cls(
            id=str(data.get("id", "")),
            lifecycle_class=str(data.get("lifecycle_class", "")),
            owner=str(data.get("owner", "")),
            docker_id=str(data.get("docker_id", "") or ""),
            purpose=str(data.get("purpose", "") or ""),
            intent_snapshot=intent,
            notes=str(data.get("notes", "") or ""),
            event_log=event_log,
            schema_version=int(data.get("schema_version", SCHEMA_VERSION_LEGACY) or 0),
            inferred=bool(data.get("inferred", False)),
            state=str(data.get("state", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )
