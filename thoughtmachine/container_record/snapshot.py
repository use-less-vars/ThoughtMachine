"""Intent-snapshot extraction (design §1.1 / §4 step 4).

Pure helpers that build the nested ``intent_snapshot`` mapping for a
container record from a Docker inspect payload.  The intent snapshot is a
*recovery* artefact (``schema_version`` legacy, ``inferred`` true), so a
missing or malformed inspect field must never abort migration: absent keys
are left empty and nothing is fabricated.

The functions here are total and side-effect free (no IO, no environment
access) so they can be exercised under the same purity guard as the rest of
the pure configuration layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import INTENT_SNAPSHOT_KEYS

#: ``intent_snapshot["hardening"]`` sub-keys (design §1.1), in fixed order.
HARDENING_KEYS: tuple[str, ...] = (
    "cap_drop",
    "cap_add",
    "security_opt",
    "readonly_rootfs",
)

#: Docker ``HostConfig`` key -> ``intent_snapshot["hardening"]`` sub-key.
#: Only truthy values are kept (a falsy cap/opt is indistinguishable from an
#: absent one in inspect output).
_HARDENING_SOURCE_KEYS: tuple[tuple[str, str], ...] = (
    ("CapDrop", "cap_drop"),
    ("CapAdd", "cap_add"),
    ("SecurityOpt", "security_opt"),
    ("ReadonlyRootfs", "readonly_rootfs"),
)

#: Mount ``Destination`` that denotes the managed workspace volume.
_WORKSPACE_MOUNT_DESTINATION = "/workspace"


def _mapping(value: Any) -> Mapping:
    """Return ``value`` when it is a mapping, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def hardening_from_host_config(host: Any) -> dict:
    """Extract the *partial* hardening sub-mapping from a ``HostConfig``.

    Only truthy entries are kept; unknown keys are ignored and a malformed
    ``host`` yields an empty mapping.  Never raises.
    """
    host = _mapping(host)
    hardening: dict[str, Any] = {}
    for src_key, dst_key in _HARDENING_SOURCE_KEYS:
        value = host.get(src_key)
        if value:
            hardening[dst_key] = value
    return hardening


def _workspace_mode(mounts: Any) -> str:
    """Derive ``"rw"``/``"ro"`` from the ``/workspace`` mount, else ``""``.

    Returns ``""`` when there is no ``/workspace`` mount, the mount's ``RW``
    flag is missing or not a bool, or ``mounts`` is malformed.  Never raises.
    """
    if not isinstance(mounts, (list, tuple)):
        return ""
    for mount in mounts:
        if not isinstance(mount, Mapping):
            continue
        if mount.get("Destination") != _WORKSPACE_MOUNT_DESTINATION:
            continue
        rw = mount.get("RW")
        if rw is True:
            return "rw"
        if rw is False:
            return "ro"
        return ""
    return ""


def snapshot_from_attrs(attrs: Any) -> dict:
    """Build the intent snapshot recoverable from a Docker inspect payload.

    ``attrs`` is the whole inspect mapping (the same shape the migration's
    ``_extract`` reads: top-level ``Image``/``Mounts`` plus ``HostConfig`` and
    ``Config`` sub-mappings).  Every key in
    :data:`~thoughtmachine.container_record.models.INTENT_SNAPSHOT_KEYS` is
    always present; missing information is left as ``""`` (or ``{}`` for
    ``hardening``) and is never fabricated.  This function is total: any input
    yields a well-formed dict and it never raises.
    """
    snapshot: dict[str, Any] = {key: "" for key in INTENT_SNAPSHOT_KEYS}
    snapshot["hardening"] = {}

    if not isinstance(attrs, Mapping):
        return snapshot

    host = _mapping(attrs.get("HostConfig"))
    config = _mapping(attrs.get("Config"))

    network_mode = host.get("NetworkMode")
    if network_mode:
        snapshot["network_mode"] = str(network_mode)

    memory = host.get("Memory")
    if memory:
        snapshot["mem_limit"] = str(memory)

    cpu_quota = host.get("CpuQuota")
    if cpu_quota:
        snapshot["cpu_quota"] = cpu_quota

    oom = host.get("OomScoreAdj")
    if oom is not None:
        snapshot["oom_score_adj"] = oom

    image_ref = config.get("Image")
    if image_ref:
        snapshot["image_ref"] = str(image_ref)

    image_hash = attrs.get("Image")
    if image_hash:
        snapshot["image_hash"] = str(image_hash)

    snapshot["workspace_mode"] = _workspace_mode(attrs.get("Mounts"))
    snapshot["hardening"] = hardening_from_host_config(host)
    return snapshot
