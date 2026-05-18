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
        loaded and its ``provider_type``, ``base_url``, ``api_key``, and
        ``model`` (respecting ``model_override``) are filled into the returned
        dict.  Values already present in *config_dict* take precedence.
        """
        profile_id = config_dict.get('provider_id')

        if not profile_id:
            return config_dict

        profile = self._profiles.get(profile_id)
        if not profile:
            return config_dict  # profile missing -> keep as-is

        result = dict(config_dict)

        # Only fill fields that aren't already explicitly set
        result.setdefault('provider_type', profile.provider_type)
        result.setdefault('base_url', profile.base_url)
        result.setdefault('api_key', profile.api_key)

        # model: use model_override if set, else profile.default_model
        if 'model_override' in config_dict and config_dict['model_override']:
            result['model'] = config_dict['model_override']
        elif not result.get('model') or result['model'] in profile.models:
            # Only override model if current value is not a custom model
            result.setdefault('model', profile.default_model)

        return result
