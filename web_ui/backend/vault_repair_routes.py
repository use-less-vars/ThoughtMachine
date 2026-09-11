"""vault_repair_routes.py -- REST endpoints for vault repair (chunk B).

Read-only status endpoint plus an explicit, origin-guarded apply endpoint.
All vault imports happen lazily inside the handlers so that tests can
monkeypatch ``thoughtmachine.vault.vault_root`` / the engine attribute paths
before any request runs; importing this module has no side effects on the
vault or the engine.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api")

_ORIGIN_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$",
                        re.I)


def _json_error(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _origin_allowed(request: Request) -> bool:
    """True when the request comes from a local web origin.

    Accepts a matching ``Origin`` header; falls back to the ``Referer``
    header (scheme+host part) when Origin is absent.  No header -> False.
    """
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw:
        return False
    if request.headers.get("referer") and not request.headers.get("origin"):
        match = re.match(r"^https?://[^/]+", raw, re.I)
        if not match:
            return False
        raw = match.group(0)
    return bool(_ORIGIN_RE.match(raw))


class _RepairApplyBody(BaseModel):
    repair_ids: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    confirmed: bool = False


@router.get("/vault/repair/status")
def vault_repair_status(request: Request) -> JSONResponse:
    from thoughtmachine import vault_repair
    from thoughtmachine.vault import vault_root

    if not _origin_allowed(request):
        return _json_error("origin not allowed", 403)
    try:
        root = Path(vault_root()).expanduser().resolve()
        if not root.is_dir():
            return _json_error("vault root %s does not exist" % root, 400)
        report = vault_repair.run_inspection(root)
        issues = report.get("issues") or []
        findings = [
            {
                "id": issue.get("id"),
                "category": issue.get("risk_category"),
                "issue_category": issue.get("category"),
                "severity": issue.get("severity"),
                "file": issue.get("file"),
                "message": issue.get("message"),
                "suggested_fix": issue.get("fix"),
                "classification": issue.get("classification"),
                "path_in_file": issue.get("path_in_file"),
            }
            for issue in issues
        ]
        return {
            "run": report.get("run"),
            "summary": report.get("summary"),
            "issues": issues,
            "extra_files": report.get("extra_files"),
            "seeded_files": report.get("seeded_files"),
            "findings": findings,
            "repairs_available": any(
                (issue.get("classification") or "") == "machine_apply"
                for issue in issues),
        }
    except Exception as exc:
        return _json_error(str(exc), 500)


@router.post("/vault/repair/apply")
def vault_repair_apply(request: Request, body: _RepairApplyBody) -> JSONResponse:
    from thoughtmachine import vault_repair
    from thoughtmachine.vault import vault_root

    if not _origin_allowed(request):
        return _json_error("origin not allowed", 403)
    ids = [v for v in (body.repair_ids or []) if v and str(v).strip()]
    cats = [v for v in (body.categories or []) if v and str(v).strip()]
    if bool(ids) == bool(cats):
        return _json_error(
            "exactly one of repair_ids or categories is required", 400)
    if not body.confirmed:
        return _json_error("confirmation required (confirmed must be true)",
                           400)
    try:
        root = Path(vault_root()).expanduser().resolve()
        if not root.is_dir():
            return _json_error("vault root %s does not exist" % root, 400)
        if not ids:
            # categories-only selection is never an explicit selection, so it
            # must not be able to sweep security_critical findings in: if the
            # engine's own category expansion would include one, reject the
            # whole request up front so nothing partial is applied.
            pre = vault_repair.run_inspection(root)
            sel = vault_repair._resolve_selection(
                pre, vault_repair._parse_selection(None, cats))
            if not sel.get("errors") and any(
                    i.get("classification") == "machine_apply"
                    and i.get("risk_category")
                    == vault_repair._RISK_SECURITY_CRITICAL
                    and (i.get("category") or "") in sel["cat_expanded"]
                    for i in (pre.get("issues") or [])):
                return _json_error(
                    "security_critical findings require explicit "
                    "repair_ids selection", 400)
        report = vault_repair.run_repair(
            root, apply=True, restore_seeds=False, yes=False,
            repair_ids=ids or None, categories=cats or None)
        repair = report.get("repair") or {}
        if repair.get("error"):
            return _json_error(repair["error"], 500)
        performed = repair.get("performed") or []
        applied = [p for p in performed if p.get("status") == "applied"]
        return {
            "report": report,
            "ok": bool(repair.get("ok", True)),
            "error_count": int(repair.get("error_count", 0)),
            "backups_created": len(repair.get("backups") or []),
            "files_changed": sorted(
                {p["file"] for p in applied if p.get("file")}),
            "quarantine_moves": [
                p for p in applied
                if "quarantin" in str(p.get("action") or "").lower()
            ],
            "errors": [p for p in performed if p.get("status") == "error"],
        }
    except Exception as exc:
        return _json_error(str(exc), 500)
