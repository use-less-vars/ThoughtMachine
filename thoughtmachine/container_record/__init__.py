"""Container Record subsystem.

Persistent, record-owned container metadata (design: ``docs/container_record_design.md``).
A *record* is the durable identity for a container; the Docker container is an
ephemeral attachment to it.

Layout:
    * :mod:`thoughtmachine.container_record.models`    — schema + exceptions (§1)
    * :mod:`thoughtmachine.container_record.storage`   — on-disk primitives (§2)
    * :mod:`thoughtmachine.container_record.api`       — lookup/mutation API (§3)
    * :mod:`thoughtmachine.container_record.migration` — legacy synthesis (§4)
"""

from __future__ import annotations

from .api import (
    RECORD_LABEL_KEY,
    append_event,
    attach_container,
    begin_record,
    create_record,
    delete_record,
    find_by_docker_label,
    list_records,
    load_record,
    read_event_log,
    record_label,
    update_record,
)
from .migration import migrate_records
from .models import (
    EVENT_ENTRY_KEYS,
    INTENT_SNAPSHOT_KEYS,
    LIFECYCLE_CLASSES,
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
    OWNER_SYSTEM,
    OWNER_VALUES,
    OWNER_WORKSPACE,
    SCHEMA_FIELD_NAMES,
    SCHEMA_VERSION_CURRENT,
    SCHEMA_VERSION_LEGACY,
    STATE_CREATING,
    ContainerRecordError,
    Record,
    RecordCorrupt,
    RecordLocked,
    RecordNotFound,
)
from .snapshot import (
    HARDENING_KEYS,
    snapshot_from_attrs,
)

__all__ = [
    # exceptions (§3.1)
    "ContainerRecordError",
    "RecordNotFound",
    "RecordCorrupt",
    "RecordLocked",
    # model (§1)
    "Record",
    "SCHEMA_VERSION_LEGACY",
    "SCHEMA_VERSION_CURRENT",
    "SCHEMA_FIELD_NAMES",
    "INTENT_SNAPSHOT_KEYS",
    "EVENT_ENTRY_KEYS",
    "LIFECYCLE_CLASSES",
    "LIFECYCLE_EPHEMERAL",
    "LIFECYCLE_PERSISTENT",
    "LIFECYCLE_RESOURCE",
    "LIFECYCLE_SERVICE",
    "OWNER_VALUES",
    "OWNER_WORKSPACE",
    "OWNER_SYSTEM",
    "STATE_CREATING",
    # snapshot extraction (§1.1 / §4)
    "snapshot_from_attrs",
    "HARDENING_KEYS",
    # api (§3 / §7)
    "RECORD_LABEL_KEY",
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
    # migration (§4)
    "migrate_records",
]
