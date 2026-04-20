"""
Preset Loader: Discovers, validates, and loads agent presets from YAML files.

Provides a simple API to list available presets and retrieve them by name.
"""
import os
import yaml
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path
from agent.logging import log
try:
    import yaml
except ImportError:
    yaml = None
    log('WARNING', 'core.preset_loader', '[WARN] PyYAML not installed; presets will not be available.')

@dataclass
class Preset:
    """Agent preset configuration."""
    name: str
    system_prompt: str
    model: str
    temperature: float = 0.2
    tools: List[str] = None
    safety_level: str = 'standard'

    def __post_init__(self):
        if self.tools is None:
            self.tools = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {'name': self.name, 'system_prompt': self.system_prompt, 'model': self.model, 'temperature': self.temperature, 'tools': self.tools, 'safety_level': self.safety_level}

class PresetLoader:
    """Discovers and loads presets from the presets directory."""

    def __init__(self, presets_dir: str='presets'):
        self.presets_dir = Path(presets_dir)
        self._presets: Dict[str, Preset] = {}
        self._load_all()

    def _load_all(self):
        """Load all YAML files in the presets directory."""
        if not self.presets_dir.exists():
            log('INFO', 'core.preset_loader', f'[INFO] Presets directory {self.presets_dir} not found; no presets loaded.')
            return
        if yaml is None:
            return
        for yaml_file in self.presets_dir.glob('*.yaml'):
            try:
                with open(yaml_file, 'r') as f:
                    data = yaml.safe_load(f)
                if not data:
                    log('WARNING', 'core.preset_loader', f'[WARN] Preset file {yaml_file} is empty; skipping.')
                    continue
                required = ['name', 'system_prompt', 'model']
                missing = [k for k in required if k not in data]
                if missing:
                    log('WARNING', 'core.preset_loader', f'[WARN] Preset {yaml_file} missing required fields: {missing}; skipping.')
                    continue
                preset = Preset(name=data['name'], system_prompt=data['system_prompt'], model=data['model'], temperature=data.get('temperature', 0.2), tools=data.get('tools', []), safety_level=data.get('safety_level', 'standard'))
                self._presets[preset.name] = preset
            except Exception as e:
                log('ERROR', 'core.preset_loader', f'[ERROR] Failed to load preset {yaml_file}: {e}')

    def list_presets(self) -> List[str]:
        """Return list of preset names."""
        return list(self._presets.keys())

    def get_preset(self, name: str) -> Optional[Preset]:
        """Get a preset by name."""
        return self._presets.get(name)

    def has_preset(self, name: str) -> bool:
        """Check if a preset exists."""
        return name in self._presets
_preset_loader: Optional[PresetLoader] = None

def get_preset_loader() -> PresetLoader:
    """Get the global preset loader instance, initializing on first call."""
    global _preset_loader
    if _preset_loader is None:
        _preset_loader = PresetLoader()
    return _preset_loader