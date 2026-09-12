"""container_record_routes.py -- read-only REST surface for the container-record store.

Exposes the durable container records described in
``docs/container_record_design.md`` (``<vault_root>/workspaces/<ws>/containers/``)
over a small, strictly read-only HTTP API:

- ``GET /api/container-records``                     -- list records (one or all workspaces)
- ``GET /api/container-records/{record_id}``         -- fetch a single record
- ``GET /api/container-records/{record_id}/events``  -- fetch a record's merged event log

Every handler is a **read**: nothing here creates, updates or deletes a record,
and no request path may mutate the on-disk store (the corrupt-file quarantine
that :func:`thoughtmachine.container_record.api.list_records` performs is only
triggered by an already-corrupt store, never by a healthy one).

All vault / container-record imports happen lazily **inside** the handlers so
that (a) importing this module has no side effects on the vault and (b) tests
can monkeypatch ``thoughtmachine.vault.vault_root`` before any request runs.
The vault root is resolved at request time only, via ``vault_root()``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/container-records", tags=["container-records"])


def _json_error(message: str, status_code: int = 500) -> JSONResponse:
    """Return a JSON error payload with the given HTTP status code."""
    return JSONResponse({"error": message}, status_code=status_code)


def _serialise(record: Any, workspace_id: str) -> dict[str, Any]:
    """Return a record as a plain dict, tagged with its owning workspace.

    ``Record.to_dict()`` has no ``workspace_id`` and no container handle, so
    the route adds both: ``workspace_id`` (the workspace the record lives in)
    and ``container_id`` (the record ``id`` -- the value Docker carries under
    the record-owned ``thoughtmachine.container_id`` label).
    """
    data = record.to_dict()
    data["workspace_id"] = workspace_id
    data["container_id"] = record.id
    return data


@router.get("")
def list_container_records(
    workspace_id: Optional[str] = Query(None),
) -> JSONResponse:
    """List container records.

    Query parameters:
        - ``workspace_id`` (optional): restrict to a single workspace. When
          omitted (or blank) records from *all* workspaces are aggregated.
          An unknown/never-seen workspace is not an error: it simply yields an
          empty list (200).

    Returns ``{"records": [...], "count": <int>}`` where each record is the
    serialised record plus ``workspace_id`` and ``container_id``.
    """
    from thoughtmachine.container_record import api as cr_api
    from thoughtmachine.container_record import storage as cr_storage
    from thoughtmachine.vault import vault_root

    try:
        root = vault_root()
        if workspace_id and workspace_id.strip():
            workspace_ids = [workspace_id]
        else:
            workspace_ids = cr_storage.iter_workspace_ids(root)

        records: list[dict[str, Any]] = []
        for ws in workspace_ids:
            for record in cr_api.list_records(ws, root):
                records.append(_serialise(record, ws))
        return JSONResponse({"records": records, "count": len(records)})
    except Exception as exc:  # corrupt/unreadable store -> JSON, never a traceback
        return _json_error(str(exc), 500)


@router.get("/{record_id}")
def get_container_record(
    record_id: str,
    workspace_id: Optional[str] = Query(None),
) -> JSONResponse:
    """Fetch a single container record.

    ``workspace_id`` is a **required** query parameter (records are keyed by
    workspace on disk and the id alone is not unique across workspaces). A
    missing or blank ``workspace_id`` yields 400; an unknown ``record_id``
    yields 404.
    """
    from thoughtmachine.container_record import api as cr_api
    from thoughtmachine.vault import vault_root

    if not workspace_id or not workspace_id.strip():
        return _json_error("workspace_id query parameter is required", 400)
    try:
        root = vault_root()
        record = cr_api.load_record(workspace_id, record_id, root)
        if record is None:
            return _json_error("record not found", 404)
        return JSONResponse(_serialise(record, workspace_id))
    except Exception as exc:
        return _json_error(str(exc), 500)


@router.get("/{record_id}/events")
def get_container_record_events(
    record_id: str,
    workspace_id: Optional[str] = Query(None),
) -> JSONResponse:
    """Return the full merged event log for a record (design 1.2 / 2.4).

    ``workspace_id`` is required (same contract as the single-record GET). A
    missing/blank ``workspace_id`` yields 400. A benign miss (no such record)
    yields an empty list (200), mirroring
    :func:`thoughtmachine.container_record.api.read_event_log`.
    """
    from thoughtmachine.container_record import api as cr_api
    from thoughtmachine.vault import vault_root

    if not workspace_id or not workspace_id.strip():
        return _json_error("workspace_id query parameter is required", 400)
    try:
        root = vault_root()
        events = cr_api.read_event_log(workspace_id, record_id, vault_root=root)
        return JSONResponse(
            {"record_id": record_id, "events": events, "count": len(events)}
        )
    except Exception as exc:
        return _json_error(str(exc), 500)
