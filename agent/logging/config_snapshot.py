"""Config Snapshot — captures and persists AgentConfig for later debugging."""
from __future__ import annotations
import json
import os
import time
from typing import Any, Dict, Optional

from agent.config.models import AgentConfig


class ConfigSnapshot:
    """Captures and persists a snapshot of AgentConfig for later debugging."""

    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.file_path = os.path.join(workspace_path, "config_snapshot.json")

    def capture(self, config: AgentConfig, label: str = "session_start") -> None:
        """Write config snapshot to JSON file."""
        # Use model_dump (Pydantic v2), matching existing _config_to_dict pattern
        raw_config = config.model_dump(exclude={'api_key', 'stop_check'}, exclude_none=True)

        snapshot = {
            "label": label,
            "timestamp": time.time(),
            "config": raw_config,
            # Include important derived values for quick inspection
            "model": config.model,
            "max_turns": config.max_turns,
            "token_warning_threshold": config.token_monitor_warning_threshold,
            "token_critical_threshold": config.token_monitor_critical_threshold,
            "timeout_seconds": config.timeout_seconds,
            "enabled_tools": sorted(config.enabled_tools),
        }
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        with open(self.file_path, 'w') as f:
            json.dump(snapshot, f, indent=2, default=str)

    def load(self) -> Optional[Dict[str, Any]]:
        """Load the last saved snapshot."""
        if not os.path.exists(self.file_path):
            return None
        with open(self.file_path) as f:
            return json.load(f)
