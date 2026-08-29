# tools/host_bash_tool.py
"""Supervised host-shell execution tool.

``host_bash`` runs a shell command on the host machine (outside the Docker
sandbox) under explicit operator control.  It is disabled by default:

* the operator must set ``AgentConfig.allow_host_resources = True`` (via
  ``SessionConfig.allow_host_resources`` for the frontend path), **and**
* the effective permission grain for ``host_bash`` must be ``"ask"`` or
  ``"allow"`` (``effective_permissions['host_bash']`` /
  ``session_permissions['host_bash']``).

Every invocation is written to ``<log_root>/host_bash_audit.log`` as a CSV
line carrying the command, workspace/session context, permission grain and
outcome.  Secret-looking values from ``os.environ`` (length >= 6) are
redacted from the audit line.
"""

import json
import os
import queue
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import Field

from .base import ToolBase

# Execution timeout for the spawned shell (seconds).
HOST_BASH_TIMEOUT = 120
# How long to wait for the operator's approval decision (seconds).
HOST_BASH_APPROVAL_TIMEOUT = 120.0


class HostBashTool(ToolBase):
    """Run a supervised shell command on the host machine."""

    tool: Literal["host_bash"] = "host_bash"
    command: str = Field(description="Shell command to execute on the host machine.")

    # No outer-gate categories: ``get_effective_permissions`` only knows the
    # seven standard categories (filesystem/network/container/git/system/mcp/
    # execution), so a ``host_bash`` grain never reaches the gate.  All
    # permission checks therefore happen inside this tool.
    required_categories: ClassVar[List[str]] = []

    # -- helpers -----------------------------------------------------------

    def _effective_grain(self) -> Optional[str]:
        """Return the effective host_bash permission grain, if any."""
        perms = self.effective_permissions or {}
        if "host_bash" in perms:
            return perms.get("host_bash")
        session_perms = self.session_permissions or {}
        return session_perms.get("host_bash")

    def _workspace_id(self) -> str:
        """Best-effort workspace id for audit context ('' when unknown)."""
        ws_id = (self.agent_config or {}).get("workspace_id")
        return ws_id or ""

    def _redact_command(self, command: str) -> str:
        """Redact environment values (len >= 6) from the command for audit."""
        for value in os.environ.values():
            if isinstance(value, str) and len(value) >= 6 and value and value in command:
                command = command.replace(value, "<redacted>")
        return command.replace("\n", "\\n")

    def _audit_log(self, outcome: str, permission_level: Optional[str], command: str) -> None:
        """Append one CSV-style audit line (best-effort, never raises)."""
        try:
            log_dir = (self.agent_config or {}).get("log_dir")
            if log_dir:
                root = Path(log_dir).expanduser()
            else:
                from agent._log_root import get_log_root
                root = get_log_root()
            root.mkdir(parents=True, exist_ok=True)
            line = (
                f"{datetime.now().isoformat(timespec='seconds')}, "
                f"{self._redact_command(command)}, "
                f"{self._workspace_id()}, "
                f"{self.session_id or ''}, "
                f"{permission_level or 'none'}, "
                f"{outcome}"
            )
            with open(root / "host_bash_audit.log", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:
            try:
                self._log_tool_warning(f"host_bash audit write failed: {exc}")
            except Exception:
                pass

    def _request_approval(self, command: str) -> str:
        """Ask the operator for approval; returns ``approved``/``denied``/``timeout``.

        Registers a one-shot response queue in the shared security registry
        (``thoughtmachine.security._pending_security_requests``), publishes a
        ``SecurityPromptEvent`` on the global event bus, and waits for
        ``resolve_security_prompt(request_id, approved, ...)`` to answer.
        The registry entry is always removed afterwards (one-time per command).
        """
        from agent.events import SecurityPromptEvent, global_event_bus
        from thoughtmachine.security import _pending_requests_lock, _pending_security_requests

        request_id = str(uuid.uuid4())
        response_queue = queue.Queue()
        with _pending_requests_lock:
            _pending_security_requests[request_id] = response_queue
        agent_id = str((self.agent_config or {}).get("agent_id") or "0")
        session_id = self.session_id or ""
        ws_id = self._workspace_id()
        description = (
            f"Run host shell command: {command} "
            f"(workspace: {ws_id or 'unknown'}, session: {session_id or 'unknown'})"
        )
        event = SecurityPromptEvent(
            data={
                "request_id": request_id,
                "agent_id": agent_id,
                "tool_name": "host_bash",
                "capabilities": ["host_bash:execute"],
                "arguments": {"command": command},
                "session_id": session_id,
                "description": description,
            }
        )
        try:
            global_event_bus.publish(event)
        except Exception as exc:
            with _pending_requests_lock:
                _pending_security_requests.pop(request_id, None)
            try:
                self._log_tool_warning(f"host_bash approval publish failed: {exc}")
            except Exception:
                pass
            return "denied"
        try:
            response = response_queue.get(
                timeout=float(os.environ.get("HOST_BASH_APPROVAL_TIMEOUT", str(HOST_BASH_APPROVAL_TIMEOUT)))
            )
            return "approved" if response.get("approved") else "denied"
        except queue.Empty:
            return "timeout"
        finally:
            with _pending_requests_lock:
                _pending_security_requests.pop(request_id, None)

    # -- execute -----------------------------------------------------------

    def execute(self) -> str:
        cmd = (self.command or "").strip()

        def denied_json(message: str, grain: Optional[str], outcome: str = "denied") -> str:
            return json.dumps(
                {
                    "success": False,
                    "error": message,
                    "command": cmd,
                    "permission_level": grain,
                    "outcome": outcome,
                },
                default=str,
            )

        if not cmd:
            self._audit_log("error", None, cmd)
            return json.dumps(
                {
                    "success": False,
                    "error": "host_bash: empty command",
                    "permission_level": None,
                    "outcome": "error",
                },
                default=str,
            )

        # Operator switch, read at call time from the injected config dict.
        allow = bool((self.agent_config or {}).get("allow_host_resources", False))
        grain = self._effective_grain()
        if not allow:
            self._audit_log("denied", grain, cmd)
            return denied_json(
                "host_bash disabled: allow_host_resources is false - operator must enable it via AgentConfig/SessionConfig",
                grain,
            )
        if grain not in ("ask", "allow"):
            self._audit_log("denied", grain, cmd)
            return denied_json(
                f"host_bash permission level '{grain}' not allowed (requires ask or allow)",
                grain,
            )
        if grain == "ask":
            decision = self._request_approval(cmd)
            if decision == "denied":
                self._audit_log("denied", grain, cmd)
                return denied_json("host_bash: command rejected by user", grain)
            if decision == "timeout":
                self._audit_log("approval_timeout", grain, cmd)
                return denied_json("host_bash: security approval timed out", grain, outcome="approval_timeout")

        timeout = int(os.environ.get("HOST_BASH_TIMEOUT", str(HOST_BASH_TIMEOUT)))
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            self._audit_log("executed", grain, cmd)
            return json.dumps(
                {
                    "success": True,
                    "command": cmd,
                    "stdout": result.stdout or "",
                    "stderr": result.stderr or "",
                    "exit_code": result.returncode,
                    "permission_level": grain,
                    "outcome": "executed",
                },
                default=str,
            )
        except subprocess.TimeoutExpired as exc:
            self._audit_log("error", grain, cmd)
            return json.dumps(
                {
                    "success": False,
                    "error": f"host_bash: command timed out after {timeout}s",
                    "command": cmd,
                    "stdout": getattr(exc, "stdout", "") or "",
                    "stderr": getattr(exc, "stderr", "") or "",
                    "exit_code": None,
                    "permission_level": grain,
                    "outcome": "error",
                },
                default=str,
            )
        except Exception as exc:
            self._audit_log("error", grain, cmd)
            return json.dumps(
                {
                    "success": False,
                    "error": f"host_bash: execution error: {exc}",
                    "command": cmd,
                    "permission_level": grain,
                    "outcome": "error",
                },
                default=str,
            )
