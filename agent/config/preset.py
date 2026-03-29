# agent/config/preset.py
"""
Preset configuration management for the ThoughtMachine agent.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    yaml = None
    YAML_AVAILABLE = False
    logger.warning("PyYAML not installed; presets will not be available.")


@dataclass
class Preset:
    """Agent preset configuration."""
    name: str
    system_prompt: str
    model: str
    temperature: float = 0.2
    tools: List[str] = None
    safety_level: str = "standard"

    def __post_init__(self):
        if self.tools is None:
            self.tools = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "temperature": self.temperature,
            "tools": self.tools,
            "safety_level": self.safety_level,
        }
    
    def to_agent_config(self) -> Dict[str, Any]:
        """Convert to agent configuration dictionary.
        
        Maps preset fields to agent configuration fields.
        """
        from .loader import load_default_config
        
        config = load_default_config()
        # Update with preset values
        config.update({
            "system_prompt": self.system_prompt,
            "model": self.model,
            "temperature": self.temperature,
            # Note: tools field needs special handling as agent config uses enabled_tools
            "enabled_tools": self.tools if self.tools else config.get("enabled_tools", [])
        })
        return config


class PresetLoader:
    """Discovers and loads presets from the presets directory."""

    def __init__(self, presets_dir: str = "presets"):
        self.presets_dir = Path(presets_dir)
        self._presets: Dict[str, Preset] = {}
        self._load_all()

    def _load_all(self):
        """Load all YAML files in the presets directory."""
        if not self.presets_dir.exists():
            logger.info(f"Presets directory {self.presets_dir} not found; no presets loaded.")
            return

        if not YAML_AVAILABLE:
            return

        for yaml_file in self.presets_dir.glob("*.yaml"):
            try:
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if not data:
                    logger.warning(f"Preset file {yaml_file} is empty; skipping.")
                    continue
                
                # Validate required fields
                required = ["name", "system_prompt", "model"]
                missing = [k for k in required if k not in data]
                if missing:
                    logger.warning(f"Preset {yaml_file} missing required fields: {missing}; skipping.")
                    continue
                
                preset = Preset(
                    name=data["name"],
                    system_prompt=data["system_prompt"],
                    model=data["model"],
                    temperature=data.get("temperature", 0.2),
                    tools=data.get("tools", []),
                    safety_level=data.get("safety_level", "standard")
                )
                self._presets[preset.name] = preset
                logger.debug(f"Loaded preset: {preset.name}")
                
            except Exception as e:
                logger.error(f"Failed to load preset {yaml_file}: {e}")

    def list_presets(self) -> List[str]:
        """Return list of preset names."""
        return list(self._presets.keys())

    def get_preset(self, name: str) -> Optional[Preset]:
        """Get a preset by name."""
        return self._presets.get(name)

    def has_preset(self, name: str) -> bool:
        """Check if a preset exists."""
        return name in self._presets
    
    def apply_preset(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Apply preset configuration to existing config.
        
        Args:
            name: Preset name
            config: Current configuration dictionary
            
        Returns:
            Updated configuration dictionary with preset applied
        """
        preset = self.get_preset(name)
        if not preset:
            logger.warning(f"Preset '{name}' not found")
            return config
        
        # Create a copy to avoid modifying original
        updated = config.copy()
        # Apply preset values
        updated.update({
            "system_prompt": preset.system_prompt,
            "model": preset.model,
            "temperature": preset.temperature,
        })
        
        # Handle tools if preset specifies them
        if preset.tools:
            updated["enabled_tools"] = preset.tools
        
        logger.debug(f"Applied preset '{name}' to configuration")
        return updated


# Global preset loader instance
_preset_loader: Optional[PresetLoader] = None


def get_preset_loader() -> PresetLoader:
    """Get the global preset loader instance, initializing on first call."""
    global _preset_loader
    if _preset_loader is None:
        _preset_loader = PresetLoader()
    return _preset_loader