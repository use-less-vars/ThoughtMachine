"""
Onboarding REST endpoints for the first-run wizard.

Provides:
    GET  /api/onboarding/status         -> whether onboarding is complete
    POST /api/onboarding/complete       -> mark onboarding complete (idempotent)
    POST /api/onboarding/test-connection -> test an LLM provider connection

Onboarding is considered complete when any of these hold:
  * the marker file ~/.thoughtmachine/onboarding_complete.json exists and
    parses to {"completed": true}
  * at least one provider profile exists in ~/.thoughtmachine/providers.json
  * at least one workspace is registered in the workspace registry

Never imports from server.py (circular import risk) and never echoes API keys.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.config.provider_profile import ProviderManager
from thoughtmachine.workspace_registry import WorkspaceRegistry
from llm_providers.factory import ProviderFactory
from llm_providers.exceptions import ProviderError

router = APIRouter(prefix="/api/onboarding")

_VAULT_DIR = Path.home() / ".thoughtmachine"
_MARKER_FILE = _VAULT_DIR / "onboarding_complete.json"


class TestConnectionRequest(BaseModel):
    provider: str
    api_key: str
    base_url: Optional[str] = None
    model: Optional[str] = None


def _onboarding_marker_complete() -> bool:
    """True if the marker file exists and contains {"completed": true}."""
    try:
        if not _MARKER_FILE.exists():
            return False
        with open(_MARKER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return isinstance(data, dict) and data.get("completed") is True
    except (OSError, ValueError):
        return False


def _onboarding_complete() -> bool:
    """Onboarding is complete if marker, provider profile, or workspace exists."""
    if _onboarding_marker_complete():
        return True
    try:
        if len(ProviderManager().list_profiles()) > 0:
            return True
    except Exception:
        pass
    try:
        if len(WorkspaceRegistry.get_default().list_workspaces()) > 0:
            return True
    except Exception:
        pass
    return False


def _short_error(exc: Exception, api_key: str) -> str:
    """Return a short, safe error message that never contains the API key."""
    msg = str(exc).strip() or type(exc).__name__
    lines = msg.splitlines()
    if lines:
        msg = lines[0]
    if len(msg) > 200:
        msg = msg[:200]
    if api_key and api_key in msg:
        msg = msg.replace(api_key, "[REDACTED]")
    return msg


@router.get("/status")
def get_onboarding_status() -> dict:
    """Return whether the first-run wizard has been completed."""
    return {"onboarding_complete": _onboarding_complete()}


@router.post("/complete")
def complete_onboarding() -> dict:
    """Mark onboarding as complete. Idempotent; atomic marker-file write."""
    try:
        _VAULT_DIR.mkdir(parents=True, exist_ok=True)
        if _onboarding_marker_complete():
            return {"onboarding_complete": True}
        fd, tmp_path = tempfile.mkstemp(
            prefix="onboarding_complete_", suffix=".tmp", dir=str(_VAULT_DIR)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"completed": True}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, _MARKER_FILE)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return {"onboarding_complete": True}
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not persist onboarding state: {_short_error(e, '')}",
        )


@router.post("/test-connection")
def test_connection(body: TestConnectionRequest) -> dict:
    """Test an LLM provider connection. Never returns the API key."""
    try:
        provider = ProviderFactory.create_provider(
            body.provider,
            api_key=body.api_key,
            base_url=body.base_url,
            model=body.model or "",
            timeout=15,
            max_retries=0,
        )
        provider.chat_completion([{"role": "user", "content": "ping"}])
    except ProviderError as e:
        return {"ok": False, "error": _short_error(e, body.api_key)}
    except Exception as e:  # noqa: BLE001 - surface any connection failure safely
        return {"ok": False, "error": _short_error(e, body.api_key)}
    return {"ok": True}
