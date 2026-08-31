"""
provider_routes.py — REST CRUD for LLM provider profiles.

Mirrors the WebSocket command surface (``get_providers`` / ``save_provider`` /
``delete_provider`` in ``server.py``) as REST endpoints backed by the same
``ProviderManager`` (``agent/config/provider_profile.py``), which persists
profiles to ``~/.thoughtmachine/providers.json``.

Endpoints:
  GET    /api/providers                bare array of provider profiles
  POST   /api/providers                create/upsert a provider (``{"provider": {...}}``)
  DELETE /api/providers/{provider_id}  delete a provider (404 when unknown)

Secret values (``api_key``) are echoed back to the caller on GET/POST — the
same fields the WebSocket surface already returns — so the provider
management UI can round-trip them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from agent.config.provider_profile import (
    PROVIDERS_FILE,
    ProviderManager,
    ProviderProfile,
)

router = APIRouter(prefix="/api")

#: Canonical provider store; overridable in tests via monkeypatch.
PROVIDERS_STORE: Path = PROVIDERS_FILE


def _json_error(message: str, status_code: int = 500):
    """JSON error response mirroring the existing API error style."""
    return JSONResponse({"error": message}, status_code=status_code)


def _provider_manager() -> ProviderManager:
    """Construct a ProviderManager bound to the canonical store file."""
    return ProviderManager(file_path=PROVIDERS_STORE)


def _safe_profile(profile: ProviderProfile) -> Dict[str, Any]:
    """Serialize one provider profile (same fields as the WS providers_list)."""
    return {
        "id": profile.id,
        "label": profile.label,
        "provider_type": profile.provider_type,
        "base_url": profile.base_url,
        "api_key": profile.api_key,
        "default_model": profile.default_model,
        "models": list(profile.models) if profile.models else [],
        "timeout": profile.timeout,
    }


class ProviderPayload(BaseModel):
    """POST /api/providers request body (mirrors the WS ``save_provider`` shape)."""

    provider: Dict[str, Any]


@router.get("/providers")
def list_providers():
    """Return all provider profiles as a bare array (safe fields only)."""
    try:
        profiles = _provider_manager().list_profiles()
    except Exception as exc:  # pragma: no cover - defensive
        return _json_error(str(exc), status_code=500)
    return [_safe_profile(p) for p in profiles]


@router.post("/providers")
def upsert_provider(payload: ProviderPayload):
    """Create or update a provider profile; 400 on invalid payloads.

    Mirrors the WS ``save_provider`` semantics: an ``id`` is required, an
    empty incoming ``api_key`` preserves the existing one (prevents
    accidental overwrite when the frontend field is blank), and the profile
    is validated against ``ProviderProfile`` (unknown fields rejected).
    """
    provider_data = payload.provider if isinstance(payload.provider, dict) else {}
    if not provider_data.get("id"):
        return _json_error("Provider must have an id", status_code=400)
    manager = _provider_manager()
    existing = manager.get_profile(provider_data["id"])
    # Preserve existing api_key if incoming value is empty.
    if not provider_data.get("api_key") and existing and existing.api_key:
        provider_data = dict(provider_data)
        provider_data["api_key"] = existing.api_key
    try:
        profile = ProviderProfile(**provider_data)
    except ValidationError as exc:
        return _json_error(f"Invalid provider: {exc}", status_code=400)
    created = existing is None
    manager.add_profile(profile)
    if not manager.save():
        return _json_error("Failed to save provider", status_code=500)
    if created:
        return {"created": True, "provider": _safe_profile(profile)}
    return {"updated": True, "provider": _safe_profile(profile)}


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str):
    """Delete a provider profile; 404 when the id is unknown."""
    provider_id = (provider_id or "").strip()
    if not provider_id:
        return _json_error("Provider id is required", status_code=400)
    manager = _provider_manager()
    if not manager.delete_profile(provider_id):
        return _json_error(f"Provider '{provider_id}' not found", status_code=404)
    if not manager.save():
        return _json_error("Failed to save provider", status_code=500)
    return {"deleted": True}
