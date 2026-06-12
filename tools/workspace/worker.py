# tools/workspace/worker.py
"""
Worker — manage background / child worker processes.

Actions
-------
list:
    List known worker processes from the workspace workers.json file.
spawn:
    Register a spawned worker (stub).
check:
    Check on a specific worker by name (stub).
query:
    Query a worker (stub).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional
from pydantic import Field

from tools.base import ToolBase

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from thoughtmachine.workspace_capabilities import (
        resolve_workspace_id,
        _workspace_dir,
    )
    CAPABILITIES_AVAILABLE = True
except ImportError:
    CAPABILITIES_AVAILABLE = False
    resolve_workspace_id = None
    _workspace_dir = None


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class Worker(ToolBase):
    """Manage background or child worker processes (experimental / stub)."""

    tool: str = "Worker"
    required_categories: ClassVar[List[str]] = ["execution:read"]

    action: str = Field(description="Action: list, spawn, check, query")

    worker_name: Optional[str] = Field(
        default=None,
        description="Name of the worker",
    )

    worker_query: Optional[str] = Field(
        default=None,
        description="Query to send to worker",
    )

    context: Optional[Dict] = Field(
        default=None,
        description="Optional context",
    )

    skip_output_truncation: ClassVar[bool] = True

    VALID_ACTIONS: ClassVar[list[str]] = ["list", "spawn", "check", "query"]

    # ------------------------------------------------------------------
    def execute(self) -> str:
        try:
            # Validate action
            if self.action not in self.VALID_ACTIONS:
                return json.dumps({
                    "error": f"Unknown action: {self.action}",
                    "available_actions": self.VALID_ACTIONS,
                })

            # Validate worker_name required for spawn/check/query
            if self.action in ("spawn", "check", "query") and not self.worker_name:
                return json.dumps({
                    "error": f"worker_name is required for action '{self.action}'",
                })

            # Resolve workspace ID and load workers
            ws_id = None
            if resolve_workspace_id and self.workspace_path:
                ws_id = resolve_workspace_id(self.workspace_path)

            workers = self._load_workers(ws_id)

            handler = {
                "list": lambda: self._action_list(workers),
                "spawn": lambda: self._action_spawn(workers),
                "check": lambda: self._action_check(workers),
                "query": lambda: self._action_query(workers),
            }[self.action]

            result = handler()
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.exception("Worker failed")
            return json.dumps(
                {"error": str(exc), "action": self.action}, indent=2
            )

    # -- helpers -----------------------------------------------------

    def _load_workers(self, ws_id: Optional[str]) -> list:
        """Load workers list from workers.json in workspace dir."""
        if not CAPABILITIES_AVAILABLE or not _workspace_dir or not ws_id:
            return []

        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            logger.warning(f"workers.json not found at {workers_path}")
            return []

        try:
            return json.loads(workers_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load workers.json: {e}")
            return []

    def _find_worker(self, workers: list, name: str) -> Optional[dict]:
        """Find a worker by name in the workers list."""
        for w in workers:
            if isinstance(w, dict) and w.get("name") == name:
                return w
        return None

    # -- action implementations (stubs) ----------------------------

    def _action_list(self, workers: list) -> dict:
        """Return all known workers."""
        if not workers:
            return {"workers": [], "count": 0}
        return {
            "workers": workers,
            "count": len(workers),
        }

    def _action_spawn(self, workers: list) -> dict:
        """Spawn a new worker (stub)."""
        existing = self._find_worker(workers, self.worker_name)
        if existing:
            return {
                "spawned": True,
                "worker_name": self.worker_name,
                "message": "Worker spawned",
            }
        return {
            "error": f"Worker '{self.worker_name}' not found in workers.json",
        }

    def _action_check(self, workers: list) -> dict:
        """Check on a specific worker (stub)."""
        entry = self._find_worker(workers, self.worker_name)
        if entry is None:
            return {
                "error": f"Worker '{self.worker_name}' not found",
            }
        return {
            "worker_name": self.worker_name,
            "status": "idle",
            "current_task": None,
            "last_heartbeat": None,
        }

    def _action_query(self, workers: list) -> dict:
        """Query a worker (stub)."""
        entry = self._find_worker(workers, self.worker_name)
        if entry is None:
            return {
                "error": f"Worker '{self.worker_name}' not found",
            }
        return {
            "worker_name": self.worker_name,
            "response": "Worker query not yet implemented. This is a stub.",
        }
