"""Container Record — legacy-container synthesis (migration, §4).

Synthesises records for containers that predate the record subsystem.  The
pass is **additive, idempotent, crash-safe and read-only**: it never stops,
restarts, mutates or relabels a container.

Idempotency rests on a per-workspace write-ahead log (``migrations.log``,
§4.2): a ``{docker_id, record_id, ts}`` entry is appended and ``fsync``-ed
*before* the record file is written.  A re-run skips an already-logged
``docker_id`` whose record file exists, and re-materialises — with the **same**
``record_id`` — one whose file is missing (crash between log and write).

Docker access is injectable (a client object with ``.containers.list(...)``, a
zero-arg callable returning inspect payloads, or an iterable of payloads) so
the whole state machine is unit-testable without a daemon (§5.1).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import storage
from .api import RECORD_LABEL_KEY
from .models import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    OWNER_WORKSPACE,
    SCHEMA_VERSION_LEGACY,
    Record,
    iso_now,
    new_record_id,
)
from .snapshot import snapshot_from_attrs

logger = logging.getLogger(__name__)

# ── Production Docker label set (§4.1) ──────────────────────────────────────

WORKSPACE_ID_LABEL = "thoughtmachine.workspace_id"
WORKER_LABEL = "thoughtmachine.worker"
CONTAINER_TYPE_LABEL = "thoughtmachine.container_type"
RESOURCE_LABEL = "thoughtmachine.resource"

CONTAINER_TYPE_FREE_USE = "free_use"
CONTAINER_TYPE_RESOURCE = "resource"


# ── Docker payload adapters (§5.1 injectable) ───────────────────────────────


def _coerce_payload(item: Any) -> dict:
    """Normalise a docker container object / dict into an inspect-style dict."""
    if isinstance(item, dict):
        return item
    attrs = getattr(item, "attrs", None)
    if isinstance(attrs, dict):
        return attrs
    return {"Id": str(getattr(item, "id", "") or "")}


def _load_payloads(source: Any) -> list[dict]:
    """Return inspect-style payloads from *source*.

    ``source`` may be a Docker client, a callable, an iterable, or ``None``
    (a real ``docker.from_env()`` client is then used).
    """
    if source is None:
        import docker  # noqa: F401  (only needed on a live host)

        client = docker.from_env()
        items = client.containers.list(
            all=True, filters={"label": WORKSPACE_ID_LABEL}
        )
    elif callable(source):
        items = source()
    elif hasattr(source, "containers"):
        items = source.containers.list(
            all=True, filters={"label": WORKSPACE_ID_LABEL}
        )
    else:
        items = list(source)
    return [_coerce_payload(item) for item in items]


def _extract(payload: dict) -> dict:
    """Pull docker_id / labels / state status from a payload."""
    config = payload.get("Config") or {}
    labels = {}
    if isinstance(config, dict):
        labels = config.get("Labels") or {}
    if not labels and isinstance(payload.get("labels"), dict):
        labels = payload["labels"]
    state = payload.get("State") or {}
    status = state.get("Status") if isinstance(state, dict) else ""

    return {
        "docker_id": str(payload.get("Id") or payload.get("id") or ""),
        "labels": labels if isinstance(labels, dict) else {},
        "state": str(status or ""),
    }


def _derive_class_owner(labels: dict) -> tuple[str, str, bool]:
    """Derive ``(lifecycle_class, owner, unknown_type)`` from container labels."""
    container_type = str(labels.get(CONTAINER_TYPE_LABEL) or "").strip().lower()
    is_resource = bool(labels.get(RESOURCE_LABEL)) or (
        container_type == CONTAINER_TYPE_RESOURCE
    )
    if is_resource:
        return LIFECYCLE_RESOURCE, OWNER_WORKSPACE, False
    if container_type == CONTAINER_TYPE_FREE_USE:
        return LIFECYCLE_EPHEMERAL, OWNER_WORKSPACE, False
    # Unknown / absent type: safest class, never abort (§4.3).
    return LIFECYCLE_PERSISTENT, OWNER_WORKSPACE, True


# ── Write-ahead log (§4.2) ──────────────────────────────────────────────────


def _read_migration_log(path: str | os.PathLike) -> dict[str, str]:
    """Return ``{docker_id: record_id}`` from the write-ahead log."""
    path = Path(path)
    mapping: dict[str, str] = {}
    if not path.is_file():
        return mapping
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        return mapping

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        docker_id = entry.get("docker_id")
        record_id = entry.get("record_id")
        if docker_id and record_id:
            mapping[str(docker_id)] = str(record_id)
    return mapping


def _record_failure(workspace_id: str, vault_root: Any, message: str) -> None:
    """Best-effort write of a failure marker to the migrations log (§4.3)."""
    try:
        storage.append_migration_line(
            storage.migrations_log_path(workspace_id, vault_root),
            {"event": "failure", "error": message, "ts": iso_now()},
        )
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("could not record migration failure: %s", exc)


def _materialise(
    workspace_id: str,
    record_id: str,
    docker_id: str,
    lifecycle_class: str,
    owner: str,
    intent_snapshot: dict,
    state: str,
    note: str | None,
    vault_root: Any,
) -> None:
    """Write a synthesised record (§4 step 6) atomically."""
    now = iso_now()
    payload: dict[str, Any] = {"docker_id": docker_id}
    if note:
        payload["inference"] = note
    event = {
        "timestamp": now,
        "event_type": "synthesised",
        "actor": "migration",
        "payload": payload,
    }
    record = Record(
        id=record_id,
        lifecycle_class=lifecycle_class,
        owner=owner,
        docker_id=docker_id,
        purpose="",
        intent_snapshot=intent_snapshot,
        notes="",
        event_log=[event],
        schema_version=SCHEMA_VERSION_LEGACY,
        inferred=True,
        state=state,
        created_at=now,
        updated_at=now,
    )
    path = storage.record_path(workspace_id, record_id, vault_root)
    with storage.record_lock(storage.lock_path(workspace_id, record_id, vault_root)):
        storage.write_record_file(path, record.to_dict())


# ── Entry point ─────────────────────────────────────────────────────────────


def migrate_records(
    workspace_id: str | None = None,
    *,
    docker_source: Any = None,
    vault_root: Any = None,
) -> dict:
    """Synthesise records for legacy containers (§4).

    Args:
        workspace_id: restrict the pass to one workspace; ``None`` processes
            every container carrying a ``thoughtmachine.workspace_id`` label
            into its own workspace (§4 step 7).
        docker_source: injectable Docker access (client / callable / iterable);
            ``None`` uses ``docker.from_env()``.
        vault_root: explicit vault root override (defaults to ``vault_root()``).

    Returns:
        A summary dict with ``scanned``/``created``/``rematerialised``/
        ``skipped``/``failed`` counts, ``aborted`` and ``errors``.
    """
    summary: dict[str, Any] = {
        "workspace_id": workspace_id,
        "scanned": 0,
        "created": 0,
        "rematerialised": 0,
        "skipped": 0,
        "failed": 0,
        "aborted": False,
        "errors": [],
    }

    try:
        payloads = _load_payloads(docker_source)
    except Exception as exc:  # daemon down → abort cleanly, no record writes
        summary["aborted"] = True
        summary["errors"].append(f"docker access failed: {exc}")
        if workspace_id is not None:
            _record_failure(workspace_id, vault_root, str(exc))
        return summary

    logs: dict[str, tuple[dict[str, str], Any]] = {}

    def _log_for(ws: str) -> tuple[dict[str, str], Any]:
        if ws not in logs:
            log_path = storage.migrations_log_path(ws, vault_root)
            logs[ws] = (_read_migration_log(log_path), log_path)
        return logs[ws]

    for payload in payloads:
        summary["scanned"] += 1
        try:
            info = _extract(payload)
            labels = info["labels"]
            ws = labels.get(WORKSPACE_ID_LABEL)
            if not ws:
                summary["skipped"] += 1
                continue
            if workspace_id is not None and ws != workspace_id:
                continue

            docker_id = info["docker_id"]
            mapping, log_path = _log_for(ws)

            # The label injected by write-on-create is ground truth: it names the
            # record the container already belongs to.  It takes precedence over
            # the migrations.log mapping (whose ids are migration-authored).
            labelled_id = labels.get(RECORD_LABEL_KEY)

            rematerialise = False
            if labelled_id:
                # Write-on-create record: no migration-authored WAL line.
                existing = storage.record_path(ws, labelled_id, vault_root)
                if existing.is_file():
                    summary["skipped"] += 1
                    continue
                record_id = labelled_id
                rematerialise = True  # record file lost after create; rebuild it
            elif docker_id and docker_id in mapping:
                record_id = mapping[docker_id]
                existing = storage.record_path(ws, record_id, vault_root)
                if existing.is_file():
                    summary["skipped"] += 1
                    continue
                rematerialise = True  # crash between step 5 and step 6
            else:
                record_id = new_record_id()

            lifecycle_class, owner, unknown = _derive_class_owner(labels)
            intent = snapshot_from_attrs(payload)
            note = (
                "unknown container type; defaulted to persistent/workspace-owned"
                if unknown
                else None
            )

            if not rematerialise:
                # Write-ahead (§4 step 5) BEFORE the record file.
                storage.append_migration_line(
                    log_path,
                    {
                        "docker_id": docker_id,
                        "record_id": record_id,
                        "ts": iso_now(),
                    },
                )
                mapping[docker_id] = record_id

            _materialise(
                ws,
                record_id,
                docker_id,
                lifecycle_class,
                owner,
                intent,
                info["state"],
                note,
                vault_root,
            )
            if rematerialise:
                summary["rematerialised"] += 1
            else:
                summary["created"] += 1
        except Exception as exc:  # per-container best-effort (§4.3)
            summary["failed"] += 1
            summary["errors"].append(str(exc))
            logger.warning(
                "container record migration skipped one container: %s", exc
            )

    return summary
