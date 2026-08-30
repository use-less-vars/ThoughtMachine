# tools/host_bash_tool.py
"""Supervised host-shell execution tool.

``host_bash`` runs a shell command on the host machine (outside the Docker
sandbox) under explicit operator control.  It is disabled by default:

* the **workspace** must opt in via ``allow_host_resources: true`` in
  ``<vault>/workspaces/<id>/config.json`` (feature flag, default false);
* the **session** must opt in via ``allow_host_resources: true`` in
  ``<vault>/sessions/<id>/config.json`` **or** via the operator-injected
  ``AgentConfig.allow_host_resources`` flag (legacy path — operator-set,
  never defaulted open); and
* the effective permission grain for ``host_bash`` must be ``"ask"`` or
  ``"allow"`` (``effective_permissions['host_bash']`` /
  ``session_permissions['host_bash']``).

The workspace leg is vault-only and fails closed: with no workspace id (or no
vault config) the tool denies.  The session leg additionally honours the
injected agent config flag so existing operator-configured deployments keep
working; it is never defaulted open.

Every invocation is written to ``<log_root>/host_bash_audit.jsonl`` as a
JSONL record with exactly six fields: ``timestamp``, ``workspace_id``,
``session_id``, ``command`` (redacted), ``outcome`` and ``reason``.
Secret-looking values from ``os.environ`` (length >= 6) are redacted from the
audit record.

Audit mapping (``outcome``, ``reason``):

* empty command               -> (``"deny"``, ``"empty command"``)
* workspace/session flag missing or false
                              -> (``"deny"``, names the flag(s) that are off)
* permission grain not ask/allow
                              -> (``"deny"``, ``"<grain> not allowed ..."``)
* approval rejected           -> (``"deny"``, ``"host_bash: command rejected by user"``)
* approval timed out          -> (``"deny"``, ``"host_bash: security approval timed out"``)
* command executed            -> (``"allow"``, ``""``)
* subprocess timeout          -> (``"timeout"``, ``"subprocess timeout after <n>s"``)
* unexpected execution error  -> (``"allow"``, ``"execution error: <exc>"``)
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

# Localized defaults.  The shared ``agent.config.defaults`` module is not
# present in every deployment, so the values live here (exported under the
# historical names so existing importers keep seeing them on this module).
HOST_BASH_TIMEOUT = 120
HOST_BASH_APPROVAL_TIMEOUT = 120.0


class HostBashTool(ToolBase):
    """Run a supervised shell command on the host machine."""

    tool: Literal["host_bash"] = "host_bash"
    command: str = Field(description="Shell command to execute on the host machine.")
    audit_log_path: Optional[str] = Field(
        default=None,
        description="Explicit JSONL audit file path (overrides default vault log root; tests inject tmp_path).",
    )

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
        """Best-effort workspace id for audit/flag context ('' when unknown).

        Resolution order: injected ``agent_config['workspace_id']``, then the
        session registry for ``session_id``, then ``''``.
        """
        ws_id = (self.agent_config or {}).get("workspace_id")
        if ws_id:
            return str(ws_id)
        if self.session_id:
            try:
                from session.session_registry import SessionRegistry

                session_info = SessionRegistry.get_default().get(self.session_id)
                if session_info and session_info.get("workspace_id"):
                    return str(session_info["workspace_id"])
            except Exception:
                pass
        return ""

    def _workspace_allow_host_resources(self) -> bool:
        """Read the workspace feature flag from the vault, fail-closed.

        ``<vault>/workspaces/<id>/config.json`` -> ``allow_host_resources``.
        Missing config, invalid JSON, or a non-dict root all read as False.
        """
        ws_id = self._workspace_id()
        if not ws_id:
            return False
        try:
            from thoughtmachine.vault import vault_root

            config_path = vault_root() / "workspaces" / ws_id / "config.json"
            if not config_path.is_file():
                return False
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            return bool(data.get("allow_host_resources", False))
        except Exception:
            return False

    def _session_allow_host_resources(self) -> bool:
        """Session feature flag: vault config OR the injected agent config flag.

        The session leg is open when ``<vault>/sessions/<id>/config.json``
        sets ``allow_host_resources: true`` OR the operator-injected
        ``agent_config['allow_host_resources']`` is true (legacy path).  The
        workspace leg is vault-only and stays fail-closed.
        """
        agent_allow = bool((self.agent_config or {}).get("allow_host_resources", False))
        session_id = self.session_id or ""
        if not session_id:
            return agent_allow
        try:
            from thoughtmachine.vault import vault_root

            config_path = vault_root() / "sessions" / session_id / "config.json"
            if config_path.is_file():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("allow_host_resources"):
                    return True
        except Exception:
            pass
        return agent_allow

    def _redact_command(self, command: str) -> str:
        """Redact environment values (len >= 6) from the command for audit."""
        for value in os.environ.values():
            if isinstance(value, str) and len(value) >= 6 and value and value in command:
                command = command.replace(value, "<redacted>")
        return command.replace("\n", "\\n")

    def _audit_log(self, outcome: str, reason: str, command: str) -> None:
        """Append one JSONL audit record (best-effort, never raises).

        Record fields (exactly): timestamp, workspace_id, session_id,
        command (redacted), outcome, reason.
        """
        try:
            if self.audit_log_path:
                path = Path(self.audit_log_path).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                log_dir = (self.agent_config or {}).get("log_dir")
                if log_dir:
                    root = Path(log_dir).expanduser()
                else:
                    from agent._log_root import get_log_root

                    root = get_log_root()
                root.mkdir(parents=True, exist_ok=True)
                path = root / "host_bash_audit.jsonl"
            record = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "workspace_id": self._workspace_id(),
                "session_id": self.session_id or "",
                "command": self._redact_command(command),
                "outcome": outcome,
                "reason": reason,
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
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
            self._audit_log("deny", "empty command", cmd)
            return json.dumps(
                {
                    "success": False,
                    "error": "host_bash: empty command",
                    "permission_level": None,
                    "outcome": "error",
                },
                default=str,
            )

        # Feature flags, read at call time from the vault (fail-closed) plus
        # the operator-injected session flag.
        ws_allow = self._workspace_allow_host_resources()
        session_allow = self._session_allow_host_resources()
        grain = self._effective_grain()
        if not ws_allow or not session_allow:
            missing = []
            ws_id = self._workspace_id()
            session_id = self.session_id or ""
            if not ws_allow:
                missing.append(f"workspace (workspaces/{ws_id or '<unknown>'}/config.json)")
            if not session_allow:
                missing.append(f"session (sessions/{session_id or '<unknown>'}/config.json)")
            reason = "host_bash denied: allow_host_resources is false or missing for: " + "; ".join(missing)
            self._audit_log("deny", reason, cmd)
            return denied_json(reason, grain)
        if grain not in ("ask", "allow"):
            self._audit_log(
                "deny",
                f"host_bash permission level '{grain}' not allowed (requires ask or allow)",
                cmd,
            )
            return denied_json(
                f"host_bash permission level '{grain}' not allowed (requires ask or allow)",
                grain,
            )
        if grain == "ask":
            decision = self._request_approval(cmd)
            if decision == "denied":
                self._audit_log("deny", "host_bash: command rejected by user", cmd)
                return denied_json("host_bash: command rejected by user", grain)
            if decision == "timeout":
                self._audit_log("deny", "host_bash: security approval timed out", cmd)
                return denied_json("host_bash: security approval timed out", grain, outcome="approval_timeout")

        timeout = int(os.environ.get("HOST_BASH_TIMEOUT", str(HOST_BASH_TIMEOUT)))
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            self._audit_log("allow", "", cmd)
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
            self._audit_log("timeout", f"subprocess timeout after {timeout}s", cmd)
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
            self._audit_log("allow", f"execution error: {exc}", cmd)
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

