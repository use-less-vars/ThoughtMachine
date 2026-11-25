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
#: v2 adds the ``restart_policy`` intent field (absent in v1 records).
#: v3 adds the ``name`` identity field (absent in v1/v2 records).
#: v4 adds the ``retention_days`` lifecycle field (absent in v1-v3 records).
#: v5 adds the ``permissions`` resolved-grant field (absent in v1-v4 records;
#: ``None`` = the record predates the field, i.e. no recorded grant to diff).
#: v6 adds the *retention charter*: ``retention_mode`` / ``retention_until`` /
#: ``retention_source`` (absent in v1-v5 records; the 5->6 step maps every
#: such record to :data:`RETENTION_PINNED`).
SCHEMA_VERSION_CURRENT = 6

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

#: Post-run state written when a freshly created container is attached: the
#: record advances ``creating`` -> ``running`` in the same ``attach`` call that
#: binds the docker container (the five fresh-create sites).
STATE_RUNNING = "running"

# ── Retention charter (v6) ──────────────────────────────────────────────────
#
# ``retention_mode`` is the ONLY machine-readable retention authority on a
# record: every automatic deletion decision (workspace GC, sweeper, operator
# purge) reads it.  Free-text prose is display-only and is never parsed by
# policy code -- a human sentence such as "do not remove this" carries no
# authority whatsoever.

#: Never deleted by any automatic policy; only an explicit operator delete
#: (or a workspace purge) removes it.
RETENTION_PINNED = "pinned"

#: Deleted automatically once the wall-clock deadline in ``retention_until``
#: (epoch seconds, UTC) has passed.
RETENTION_RETAINED_UNTIL = "retained_until"

#: Deleted when the container exits -- exit is the ONLY trigger; the record
#: carries no deadline and its execution authority expires with the container.
RETENTION_EPHEMERAL_UNTIL_EXIT = "ephemeral_until_exit"

#: The closed set of retention modes.  Anything else is rejected fail-closed.
RETENTION_MODES: frozenset[str] = frozenset(
    {
        RETENTION_PINNED,
        RETENTION_RETAINED_UNTIL,
        RETENTION_EPHEMERAL_UNTIL_EXIT,
    }
)

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
    # ``restart_policy`` is the *intent* restart policy applied at container
    # creation (``None`` = the record predates the field, i.e. unset).
    "restart_policy",
    # ``name`` is the workspace-scoped container identity ("" = the record
    # predates the field, i.e. unset). Appended last to keep the on-disk key
    # order of v1/v2 records stable.
    "name",
    # ``retention_days`` is the record's lifecycle retention window in *days*
    # (``None`` = the record predates the field, i.e. unset -> the sweeper
    # falls back to its own default). Appended last to keep the on-disk key
    # order of v1-v3 records stable.
    "retention_days",
    # ``user`` is the container user (``uid:gid``) recorded at attach time
    # ("" = the record predates the field). Appended last to keep the on-disk
    # key order of v1-v4 records stable.
    "user",
    # ``permissions`` is the resolved six-category grant dict captured at
    # create time (``None`` = the record predates the field, i.e. no recorded
    # grant is available to diff against). Appended last to keep the on-disk
    # key order of v1-v5 records stable.
    "permissions",
    # ``retention_mode`` is the record's retention charter -- the ONLY
    # machine-readable retention authority (see ``RETENTION_MODES``).  Appended
    # last to keep the on-disk key order of v1-v6 records stable.
    "retention_mode",
    # ``retention_until`` is the epoch-seconds deadline implied by
    # ``retention_mode == RETENTION_RETAINED_UNTIL`` (``None`` otherwise).
    "retention_until",
    # ``retention_source`` records *why* the mode holds: ``"explicit"``
    # (caller-supplied), ``"class_default"`` (derived from the lifecycle
    # class) or ``"migration_default"`` (stamped by the 5->6 step).
    "retention_source",
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
    # ``restart_policy`` records the restart policy observed in the live
    # container's ``HostConfig.RestartPolicy.Name`` (``""`` when unset).
    "restart_policy",
    # ``user`` records the container user (``uid:gid``) observed in the live
    # container's ``Config.User`` (``""`` when unset).
    "user",
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


def validate_retention_mode(value: str) -> str:
    """Return *value* if it is a valid retention mode, else raise.

    Fail-closed: an unknown mode (including an empty string) is rejected rather
    than defaulted, so no caller can silently acquire a retention guarantee.
    """
    if value not in RETENTION_MODES:
        raise ValueError(
            f"invalid retention_mode {value!r}; expected one of "
            f"{', '.join(sorted(RETENTION_MODES))}"
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
    # FREEZE(record.state): the SOLE reader of this field is
    # ``thoughtmachine.container_record.state.resolve_container_state``; the
    # workspace container-view display path routes through it via
    # ``web_ui.backend.server._map_container_view_state``. Do not read
    # ``record.state`` anywhere else.
    state: str = ""
    created_at: str = ""
    # MUTATION-ONLY: updated_at advances on real mutation; never on inspection
    # or serialization.
    updated_at: str = ""
    restart_policy: str | None = None
    #: Workspace-scoped container identity ("" = unset).
    name: str = ""
    #: Lifecycle retention window in days (``None`` = unset).
    retention_days: int | None = None
    #: The container user (``uid:gid``) recorded at attach time ("" = unset).
    user: str = ""
    #: The resolved six-category grant captured at create time
    #: (``None`` = the record predates the field, i.e. no recorded grant).
    permissions: dict | None = None
    #: The retention charter -- the ONLY machine-readable retention authority
    #: (one of :data:`RETENTION_MODES`).  A record whose charter is
    #: ``RETENTION_PINNED`` is never removed by an automatic policy, whatever
    #: its free-text prose field says.
    retention_mode: str = RETENTION_PINNED
    #: Epoch-seconds UTC deadline; required by (and only meaningful for)
    #: ``RETENTION_RETAINED_UNTIL``.
    retention_until: float | None = None
    #: Provenance of ``retention_mode``: ``"explicit"`` / ``"class_default"``
    #: / ``"migration_default"`` ("" = unset).
    retention_source: str = ""

    def __post_init__(self) -> None:
        """Validate the retention charter fail-closed (v6)."""
        mode = validate_retention_mode(self.retention_mode)
        if mode == RETENTION_RETAINED_UNTIL and self.retention_until is None:
            raise ValueError(
                "retention_mode 'retained_until' requires retention_until"
            )

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
            "restart_policy": self.restart_policy,
            "name": self.name,
            "retention_days": self.retention_days,
            "user": self.user,
            "permissions": self.permissions,
            "retention_mode": self.retention_mode,
            "retention_until": self.retention_until,
            "retention_source": self.retention_source,
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

        # Absent / blank -> ``None`` (unset); a non-empty string is kept as-is.
        restart_policy = data.get("restart_policy")
        if not isinstance(restart_policy, str) or not restart_policy:
            restart_policy = None

        # ``retention_days`` is lenient: absent / blank / non-numeric /
        # non-integer values fall back to ``None`` and never raise.
        retention_days = data.get("retention_days")
        if isinstance(retention_days, bool) or retention_days is None:
            retention_days = None
        elif isinstance(retention_days, int):
            pass
        elif isinstance(retention_days, float):
            retention_days = int(retention_days) if retention_days.is_integer() else None
        elif isinstance(retention_days, str):
            text = retention_days.strip()
            try:
                retention_days = int(text) if text else None
            except ValueError:
                retention_days = None
        else:
            retention_days = None

        # ``permissions`` is lenient: absent / non-dict / empty fall back to
        # ``None`` (no recorded grant); a non-empty dict is copied as-is.
        permissions = data.get("permissions")
        if not isinstance(permissions, dict) or not permissions:
            permissions = None
        else:
            permissions = dict(permissions)

        # ``retention_mode`` is lenient on READ (absent / unknown -> ``pinned``,
        # the most conservative mode) but fail-closed on explicit construction
        # (``__post_init__``).  Reading can never widen a retention guarantee.
        retention_mode = data.get("retention_mode")
        if retention_mode not in RETENTION_MODES:
            retention_mode = RETENTION_PINNED

        retention_until = data.get("retention_until")
        if isinstance(retention_until, bool) or not isinstance(
            retention_until, (int, float)
        ):
            retention_until = None
        else:
            retention_until = float(retention_until)
        if retention_mode != RETENTION_RETAINED_UNTIL:
            retention_until = None
        if retention_mode == RETENTION_RETAINED_UNTIL and retention_until is None:
            # A dangling deadline (absent / non-numeric) would be rejected by
            # ``__post_init__`` and so poison every read of the workspace;
            # downgrade to the conservative mode instead of raising -- a read
            # must never widen retention and must never raise.
            retention_mode = RETENTION_PINNED

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
            restart_policy=restart_policy,
            name=str(data.get("name", "") or ""),
            retention_days=retention_days,
            user=str(data.get("user", "") or ""),
            permissions=permissions,
            retention_mode=retention_mode,
            retention_until=retention_until,
            retention_source=str(data.get("retention_source", "") or ""),
        )
