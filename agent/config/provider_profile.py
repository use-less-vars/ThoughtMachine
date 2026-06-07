"""
Provider profiles for the ThoughtMachine agent.

Stored in ~/.thoughtmachine/providers.json — never read by the agent or committed to git.
The GUI owns this file exclusively.
"""
import json
import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


THOUGHTMACHINE_DIR = Path.home() / '.thoughtmachine'
PROVIDERS_FILE = THOUGHTMACHINE_DIR / 'providers.json'


class ProviderProfile(BaseModel):
    """A single provider profile with connection details."""
    id: str
    label: str
    provider_type: str = 'openai_compatible'
    base_url: str = ''
    api_key: str = ''
    default_model: str = ''
    models: List[str] = Field(default_factory=list)
    timeout: int = 120


class ProviderManager:
    """Manages provider profiles stored in ~/.thoughtmachine/providers.json."""

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or PROVIDERS_FILE
        self._profiles: Dict[str, ProviderProfile] = {}
        self._active_profile_id: Optional[str] = None
        self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        """Load profiles from disk. Creates empty store if file is missing."""
        if not self.file_path.exists():
            self._profiles = {}
            self._active_profile_id = None
            return
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            profiles_list = data.get('profiles', [])
            self._profiles = {p['id']: ProviderProfile(**p) for p in profiles_list}
            self._active_profile_id = data.get('active_profile_id')
            # Validate active profile still exists
            if self._active_profile_id and self._active_profile_id not in self._profiles:
                self._active_profile_id = None
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self._profiles = {}
            self._active_profile_id = None

    def save(self) -> bool:
        """Persist profiles to disk."""
        try:
            os.makedirs(THOUGHTMACHINE_DIR, exist_ok=True)
            data = {
                'profiles': [p.model_dump() for p in self._profiles.values()],
                'active_profile_id': self._active_profile_id,
            }
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            return True
        except OSError as e:
            return False

    # ── CRUD ─────────────────────────────────────────────────────

    def add_profile(self, profile: ProviderProfile) -> None:
        """Add or replace a profile (matched by id)."""
        self._profiles[profile.id] = profile

    def update_profile(self, profile_id: str, updates: Dict[str, Any]) -> Optional[ProviderProfile]:
        """Update fields on an existing profile. Returns updated profile or None."""
        if profile_id not in self._profiles:
            return None
        current = self._profiles[profile_id]
        updated = current.model_copy(update=updates)
        self._profiles[profile_id] = updated
        return updated

    def delete_profile(self, profile_id: str) -> bool:
        """Remove a profile. Returns True if it existed."""
        if profile_id not in self._profiles:
            return False
        del self._profiles[profile_id]
        if self._active_profile_id == profile_id:
            self._active_profile_id = None
        return True

    def get_profile(self, profile_id: str) -> Optional[ProviderProfile]:
        """Get a profile by id, or None if not found."""
        return self._profiles.get(profile_id)

    def list_profiles(self) -> List[ProviderProfile]:
        """Return all profiles."""
        return list(self._profiles.values())

    # ── Active profile ──────────────────────────────────────────

    @property
    def active_profile_id(self) -> Optional[str]:
        return self._active_profile_id

    @active_profile_id.setter
    def active_profile_id(self, value: Optional[str]) -> None:
        if value is not None and value not in self._profiles:
            raise ValueError(f'Unknown profile id: {value}')
        self._active_profile_id = value

    def get_active_profile(self) -> Optional[ProviderProfile]:
        """Return the active profile, or None."""
        if self._active_profile_id is None:
            return None
        return self._profiles.get(self._active_profile_id)

    def resolve_config(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve provider fields from the active profile into a config dict.

        If *config_dict* contains a ``provider_id``, the matching profile is
        the authoritative source for ``provider_type``, ``base_url``, and
        ``api_key`` — they overwrite *config_dict* when non-empty (empty
        profile values leave the config's values intact).
        For ``model``, ``model_override`` (if set) takes precedence over the
        profile's ``default_model``.  If ``model`` is explicitly provided in
        *config_dict* it is preserved; the profile's ``default_model`` is only
        used as a fallback when no model is specified.

        .. note::

            Before 2025-07 the code used ``setdefault``, which left stale
            values from a previous provider in place, breaking provider
            switching.  See the fix in
            :meth:`ProviderManager.resolve_config`.
        """
        profile_id = config_dict.get('provider_id')

        if not profile_id:
            return config_dict

        profile = self._profiles.get(profile_id)
        if not profile:
            return config_dict  # profile missing -> keep as-is

        result = dict(config_dict)

        # Provider fields: overwrite from profile when non-empty.
        # When switching providers, non-empty profile values clear stale
        # values (e.g. base_url, api_key from the previous provider).
        # Empty profile values leave the config's values intact — this
        # prevents blanking out valid config values when the profile
        # has empty optional fields.
        if profile.provider_type:
            result['provider_type'] = profile.provider_type
        if profile.base_url:
            result['base_url']       = profile.base_url
        if profile.api_key:
            result['api_key']        = profile.api_key

        # Model: model_override > explicit user model > profile.default_model
        model_override = config_dict.get('model_override')
        if model_override:
            result['model'] = model_override
        elif config_dict.get('model') and config_dict['model'] != 'deepseek-reasoner':
            # User explicitly set a non-default model — preserve it
            pass
        else:
            result['model'] = profile.default_model if profile.default_model else result.get('model', '')

        return result
