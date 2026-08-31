"""
global_routes.py — Global dashboard summary + global credentials CRUD.

REST endpoints:
  GET    /api/global/summary        one-shot global snapshot
  GET    /api/credentials           bare array of sorted global credential names
  POST   /api/credentials           create/update a global credential
  DELETE /api/credentials/{name}    delete a global credential

The summary is assembled from runtime singletons only
(``SessionRegistry.get_default()``, ``WorkspaceRegistry.get_default()``,
``WorkerManager.get_manager()``) plus the same per-workspace Docker daemon
query the existing ``/api/workspace/{id}/containers`` endpoint uses — no
scanning of session/worker files.  A component that cannot be read degrades
to an empty list plus a top-level ``warning`` string (the ``warning`` key is
omitted when every component is available).  Each workspace entry carries
``status`` ``"working"`` while at least one session is open in it, otherwise
``"idle"``.

Global credentials are stored as plain files at
``<vault_root>/credentials/global/<name>`` (vault root resolved via
``thoughtmachine.vault.vault_root()``, honouring ``THOUGHTMACHINE_VAULT_ROOT``),
written with mode ``0o600`` and never returned by the API (GET
``/api/credentials`` returns a bare array of names only).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from thoughtmachine.vault import vault_root
from session.session_registry import SessionRegistry
from thoughtmachine.workspace_registry import WorkspaceRegistry

# Module-level reference for monkeypatchability in tests; the import is
# guarded so a failing worker module can never break router import.
try:
    from tools.workspace.worker_manager import get_manager as _get_worker_manager
except Exception:  # pragma: no cover - defensive
    _get_worker_manager = None

router = APIRouter(prefix="/api")

# ── Shared error helper (mirrors server.py's _json_error) ────────────────────


def _json_error(message: str, status_code: int = 500):
    """JSON error response mirroring the existing API error style."""
    return JSONResponse({"error": message}, status_code=status_code)


# ══════════════════════════════════════════════════════════════════════════════
#  GET /api/global/summary
# ══════════════════════════════════════════════════════════════════════════════


def _resolve_worker_manager():
    """Return the process-wide WorkerManager singleton, or None when unavailable."""
    if _get_worker_manager is None:
        return None
    try:
        return _get_worker_manager()
    except Exception:
        return None


def _session_worker_count(worker_manager, session_id: str) -> int:
    """Number of registered workers for a session (0 when unavailable)."""
    if worker_manager is None:
        return 0
    try:
        return len(worker_manager.list_workers(session_id) or [])
    except Exception:
        return 0


def _workspace_allow_host_resources(workspace_id: str) -> bool:
    """Read allow_host_resources from the workspace config.json; never raises.

    This is the server's canonical source for the flag (workspace_routes
    persists it there); the registry entry carries no such field.
    """
    try:
        cfg_path = vault_root() / "workspaces" / workspace_id / "config.json"
        if cfg_path.is_file():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return bool(data.get("allow_host_resources", False))
    except (OSError, ValueError):
        pass
    return False


def _workspace_root_mountable(entry) -> bool:
    """True when the workspace root may be passed to ContainerManager.

    Mirrors the server's host-path confinement: the vault root itself (or
    anything inside it) must never be mounted into a container.
    """
    try:
        root = os.path.abspath(os.path.expanduser(str(entry.root_path or "")))
        vault = str(vault_root())
        if root == vault or root.startswith(vault + os.sep):
            return False
        return bool(root)
    except Exception:
        return False


def _containers_for_workspace(entry) -> Optional[List[Dict[str, Any]]]:
    """Summary handles for one workspace's containers, or None on failure."""
    if not _workspace_root_mountable(entry):
        return None
    try:
        from infra.container_manager import ContainerManager

        manager = ContainerManager(
            workspace_path=entry.root_path,
            workspace_id=entry.id,
            session_id=None,
            session_permissions=None,
        )
    except Exception:
        return None
    try:
        raw = manager.list_containers() or []
    except Exception:
        return None
    handles = []
    for container in raw:
        name = container.get("name") or ""
        handles.append({
            "id": container.get("container_id") or "",
            "name": name,
            "type": "resource" if name.startswith("tm-res-") else "free_use",
            "workspace_id": container.get("workspace_id") or entry.id,
            "status": container.get("status") or "unknown",
        })
    handles.sort(key=lambda h: h["name"])
    return handles


def _collect_active_containers(
    workspace_entries,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Per-workspace Docker listing; degrades to [] + warning when unavailable."""
    if not workspace_entries:
        return [], None
    containers: List[Dict[str, Any]] = []
    failures = 0
    for entry in workspace_entries:
        handles = _containers_for_workspace(entry)
        if handles is None:
            failures += 1
        else:
            containers.extend(handles)
    if failures == 0:
        return containers, None
    if containers:
        return containers, (
            f"containers unavailable for {failures} of {len(workspace_entries)} "
            "workspace(s)"
        )
    return [], (
        "containers unavailable (docker SDK missing or daemon unreachable)"
    )


def _build_summary() -> Dict[str, Any]:
    """Assemble the global summary from the runtime singletons; never raises."""
    warnings: List[str] = []

    # ── Workspaces (WorkspaceRegistry singleton) ──────────────────────────
    try:
        workspace_entries = WorkspaceRegistry.get_default().list_workspaces()
    except Exception as exc:
        workspace_entries = []
        warnings.append(f"workspaces unavailable: {exc}")

    # ── Sessions (SessionRegistry singleton) ──────────────────────────────
    try:
        all_sessions = SessionRegistry.get_default().get_all()
    except Exception as exc:
        all_sessions = {}
        warnings.append(f"sessions unavailable: {exc}")
    open_sessions = [
        s for s in all_sessions.values()
        if isinstance(s, dict) and s.get("is_open")
    ]
    open_sessions.sort(key=lambda s: str(s.get("session_id") or ""))

    # ── Workers (WorkerManager singleton) ─────────────────────────────────
    worker_manager = _resolve_worker_manager()
    if worker_manager is None:
        warnings.append("workers unavailable")

    # ── Assemble ──────────────────────────────────────────────────────────
    workspaces_out: List[Dict[str, Any]] = []
    for entry in workspace_entries:
        eid = entry.id or ""
        open_in_workspace = [
            s for s in open_sessions if s.get("workspace_id") == eid
        ]
        workspaces_out.append({
            "id": eid,
            "label": entry.label or "",
            "active_sessions_count": len(open_in_workspace),
            "total_workers": sum(
                _session_worker_count(worker_manager, str(s.get("session_id") or ""))
                for s in open_in_workspace
            ),
            "last_active": entry.last_opened or entry.updated_at or "",
            "status": "working" if open_in_workspace else "idle",
            "allow_host_resources": _workspace_allow_host_resources(eid),
        })

    active_sessions_out: List[Dict[str, Any]] = [
        {
            "session_id": s.get("session_id", ""),
            "workspace_id": s.get("workspace_id", ""),
            "name": s.get("name", ""),
            "mode": s.get("mode", ""),
            "started_at": s.get("created_at", ""),
            "worker_count": _session_worker_count(
                worker_manager, str(s.get("session_id") or "")
            ),
        }
        for s in open_sessions
    ]

    containers, container_warning = _collect_active_containers(workspace_entries)
    if container_warning:
        warnings.append(container_warning)

    result: Dict[str, Any] = {
        "workspaces": workspaces_out,
        "active_sessions": active_sessions_out,
        "active_containers": containers,
    }
    if warnings:
        result["warning"] = ", ".join(warnings)
    return result


@router.get("/global/summary")
def global_summary():
    """One-shot snapshot of registered workspaces, open sessions and containers."""
    try:
        return _build_summary()
    except Exception as exc:  # pragma: no cover - defensive
        return _json_error(str(exc), status_code=500)


# ══════════════════════════════════════════════════════════════════════════════
#  Global credentials CRUD  (vault-root/credentials/global/<name>, 0600)
# ══════════════════════════════════════════════════════════════════════════════

_MAX_CREDENTIAL_NAME_LEN = 128


def _validate_credential_name(name: str) -> Optional[str]:
    """Return an error message for an invalid credential name, else None."""
    if not name:
        return "credential name must not be empty"
    if name == "." or ".." in name or "/" in name or "\\" in name:
        return "credential name must be a single path segment"
    if len(name) > _MAX_CREDENTIAL_NAME_LEN:
        return (
            f"credential name must be at most {_MAX_CREDENTIAL_NAME_LEN} characters"
        )
    return None


def _credentials_dir() -> Path:
    """Vault credentials directory (<vault_root>/credentials/global)."""
    return vault_root() / "credentials" / "global"


def _write_credential(name: str, value: str) -> bool:
    """Persist a credential file with mode 0600; returns True when created.

    Directories are created 0700 (owner-only) and the file is written
    atomically via a temp file + os.replace so the 0600 mode survives.
    """
    cred_dir = _credentials_dir()
    cred_dir.mkdir(parents=True, exist_ok=True)
    try:
        cred_dir.chmod(0o700)
        cred_dir.parent.chmod(0o700)
    except OSError:
        pass

    target = cred_dir / name
    created = not target.exists()
    tmp = cred_dir / f".{name}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(target))
    return created


class CredentialPayload(BaseModel):
    """POST /api/credentials request body."""

    name: str
    value: str


@router.get("/credentials")
def list_credentials():
    """Return the sorted names of all global credentials as a bare array.

    Secret values are never returned, only the file names.
    """
    cred_dir = _credentials_dir()
    try:
        if not cred_dir.is_dir():
            return []
        return sorted(
            p.name for p in cred_dir.iterdir() if p.is_file()
        )
    except OSError as exc:
        return _json_error(str(exc), status_code=500)


@router.post("/credentials")
def upsert_credential(payload: CredentialPayload):
    """Create or update a global credential; 400 on invalid names."""
    name = (payload.name or "").strip()
    error = _validate_credential_name(name)
    if error:
        return _json_error(error, status_code=400)
    value = payload.value if isinstance(payload.value, str) else str(payload.value or "")
    try:
        created = _write_credential(name, value)
    except OSError as exc:
        return _json_error(str(exc), status_code=500)
    if created:
        return {"created": True}
    return {"updated": True}


@router.delete("/credentials/{name}")
def delete_credential(name: str):
    """Delete a global credential; idempotent (missing credentials still 200)."""
    name = (name or "").strip()
    error = _validate_credential_name(name)
    if error:
        return _json_error(error, status_code=400)
    target = _credentials_dir() / name
    try:
        if target.exists() and not target.is_dir():
            target.unlink()
    except OSError as exc:
        return _json_error(str(exc), status_code=500)
    return {"deleted": True}
