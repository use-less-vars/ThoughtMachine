"""
SessionManager — Dedicated session lifecycle component extracted from WebAgentBridge.

Handles all session persistence and loading logic previously embedded in
``bridge.py``, using ``FileSystemSessionStore`` for storage and ``ConfigManager``
for config translation/enforcement.

The bridge retains in-memory references (``_session``, ``_session_config``,
``_session_id``) and delegates storage operations here.  This component
does NOT know about event forwarding, agent controllers, or WebSockets.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agent.config.session_config import SessionConfig
from agent.config.provider_profile import ProviderManager
from agent.core.message import SYSTEM_NOTIFICATION_PREFIX
from agent.logging import log
from session.models import Session
from session.store import FileSystemSessionStore
from session.session_registry import SessionRegistry

from web_ui.backend.config_manager import ConfigManager


# ── Final / Respond tool names used in message normalisation ────────────────
FINAL_TOOL_NAMES = {"Respond"}
LEGACY_TO_RESPOND = {
    "Final": {"response_type": "answer"},
    "FinalReport": {"response_type": "answer"},
    "RequestUserInteraction": {"response_type": "question"},
}
ALL_RESPOND_NAMES = FINAL_TOOL_NAMES | set(LEGACY_TO_RESPOND.keys())
SUMMARY_TOOL_NAMES = {"SummarizeTool", "summarize", "Summarize"}


# ══════════════════════════════════════════════════════════════════════════════
# SessionManager
# ══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    """
    Manages session persistence, loading, and configuration merge.

    Owns the ``FileSystemSessionStore`` and ``ConfigManager`` references so
    that ``WebAgentBridge`` does not need to manage them directly.
    """

    def __init__(
        self,
        session_store: FileSystemSessionStore,
        config_manager: ConfigManager,
    ) -> None:
        self._session_store = session_store
        self._config_manager = config_manager

    # ── Create ────────────────────────────────────────────────────────────────

    def create_session(
        self,
        mode: str = "custom",
        workspace_path: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Create a new empty ``Session``, build a matching ``SessionConfig``,
        persist to the store, and return ``(session_id, frontend_config_dict)``.

        The caller (bridge) is responsible for setting ``_session``,
        ``_session_config``, etc. from the returned values.
        """
        from session.models import Session

        new_session = Session()
        new_session.mode = mode
        new_session.metadata["source"] = "web_ui"
        new_session.ensure_name()

        # Build a minimal SessionConfig for the requested mode
        from agent.config.presets import get_tools_for_mode
        tools = get_tools_for_mode(mode)
        session_config = SessionConfig(
            mode=mode,
            max_turns=100,
            session_permissions={},
            enabled_tools=tools,
            provider_id="",
            model="",
            base_url="",
        )

        # Merge global defaults so saved provider/model appear in new sessions
        from web_ui.backend.config_manager import (
            GLOBAL_DEFAULT_KEYS,
            load_global_defaults,
            translate_frontend_config,
        )
        defaults_be = translate_frontend_config(load_global_defaults())
        # Only copy the global-default allowlist (see docs/architecture/config_ownership.md)
        session_config_keys = GLOBAL_DEFAULT_KEYS
        for key in session_config_keys:
            if key in defaults_be and defaults_be[key]:
                setattr(session_config, key, defaults_be[key])

        # Persist session + config metadata
        new_session.metadata["session_config"] = session_config.model_dump(
            exclude={"api_key"}, exclude_none=True
        )
        self._session_store.save_session(
            new_session, workspace_id=new_session.workspace_id
        )
        self._session_store.add_open_session(new_session.session_id)

        frontend_config = self._config_manager.session_config_to_frontend(
            session_config, workspace_path=workspace_path
        )

        log(
            "INFO",
            "session_manager",
            f"Created session {new_session.session_id} (mode={mode})",
        )
        return new_session.session_id, frontend_config

    # ── Load ──────────────────────────────────────────────────────────────────

    def load_session(
        self,
        session_id: str,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        """
        Load a ``Session`` from the store by ID.

        Performs role repair (``tool_result`` → ``tool``) and config extraction
        so the caller can update its in-memory state.

        Returns ``None`` if the session is not found.
        """
        session = self._session_store.load_session(
            session_id, workspace_id=workspace_id
        )
        if session is None:
            return None

        # Repair roles, migrate config, register in registry
        self.repair_session(session)

        return session

    def extract_session_config(
        self,
        session: Session,
    ) -> Optional[SessionConfig]:
        """
        Extract and construct a ``SessionConfig`` from session metadata.

        Handles API-key resolution via ``ProviderManager``.
        Returns ``None`` if no config metadata exists.
        """
        session_config_raw = session.metadata.get("session_config") or \
            session.metadata.get("agent_config")
        if not session_config_raw or not isinstance(session_config_raw, dict):
            return None

        from agent.config.session_config import SessionConfig
        from agent.config.provider_profile import ProviderManager

        try:
            if "mode" not in session_config_raw:
                session_config_raw["mode"] = "agent"

            sc = SessionConfig(**session_config_raw)

            if not sc.mode:
                sc.mode = "agent"

            # Re-inject API key from provider resolution
            try:
                manager = ProviderManager()
                resolved = manager.resolve_config(
                    {"provider_id": sc.provider_id}
                )
                if resolved.get("api_key"):
                    sc.api_key = resolved["api_key"]
                else:
                    for profile in manager.list_profiles():
                        if profile.api_key:
                            sc.api_key = profile.api_key
                            sc.provider_id = profile.id
                            break
            except Exception as exc:
                log(
                    "WARNING",
                    "session_manager",
                    f"Could not resolve API key: {exc}",
                )

            return sc
        except Exception as exc:
            log(
                "WARNING",
                "session_manager",
                f"extract_session_config failed: {exc}",
            )
            return None

    def save_config_to_session(
        self,
        session: Session,
        session_config: SessionConfig,
    ) -> None:
        """Persist session_config into session metadata and save."""
        session.metadata["session_config"] = session_config.model_dump(
            exclude={"api_key"}, exclude_none=True
        )
        if "agent_config" in session.metadata:
            del session.metadata["agent_config"]
        self._session_store.save_session(
            session, workspace_id=session.workspace_id
        )

    # ── Save ──────────────────────────────────────────────────────────────────

    def save_session(
        self,
        session: Session,
        session_config: Optional[SessionConfig] = None,
        name: Optional[str] = None,
    ) -> Session:
        """
        Persist a session and its config to the store.

        If ``session_config`` is given, its serialised form is written to
        ``session.metadata["session_config"]``.
        """
        if session_config is not None:
            session.metadata["session_config"] = session_config.model_dump(
                exclude={"api_key"}, exclude_none=True
            )
        session.metadata.setdefault("source", "web_ui")
        if name:
            session.metadata["name"] = name
        session.ensure_name()
        self._session_store.save_session(
            session, workspace_id=session.workspace_id
        )
        return session

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_session(
        self,
        session_id: str,
        workspace_id: Optional[str] = None,
    ) -> bool:
        """
        Delete a session from the store, the global registry, and open-sessions
        state.
        """
        try:
            result = self._session_store.delete_session(
                session_id, workspace_id=workspace_id
            )
            if result:
                registry = SessionRegistry.get_default()
                registry.remove(session_id)
                self._session_store.remove_open_session(session_id)
                log(
                    "INFO",
                    "session_manager",
                    f"Session deleted: {session_id}",
                )
            else:
                log(
                    "WARNING",
                    "session_manager",
                    f"Session not found for deletion: {session_id}",
                )
            return result
        except Exception as e:
            log("ERROR", "session_manager", f"delete_session error: {e}")
            return False

    # ── List ──────────────────────────────────────────────────────────────────

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all saved sessions from the session store."""
        try:
            return self._session_store.list_sessions()
        except Exception as e:
            log("ERROR", "session_manager", f"list_sessions error: {e}")
            return []

    # ── Rename ────────────────────────────────────────────────────────────────

    def rename_session(
        self,
        session_id: str,
        new_name: str,
        workspace_id: Optional[str] = None,
    ) -> bool:
        """Rename a session in the store."""
        try:
            session = self._session_store.load_session(
                session_id, workspace_id=workspace_id
            )
            if session is None:
                log(
                    "WARNING",
                    "session_manager",
                    f"Session not found for rename: {session_id}",
                )
                return False
            session.metadata["name"] = new_name
            self._session_store.save_session(
                session, workspace_id=session.workspace_id
            )
            log(
                "INFO",
                "session_manager",
                f"Session renamed: {session_id} → {new_name}",
            )
            return True
        except Exception as e:
            log("ERROR", "session_manager", f"rename_session error: {e}")
            return False

    # ── Open sessions ─────────────────────────────────────────────────────────

    def get_open_sessions(self) -> List[str]:
        """Return the list of open session IDs from ``open_sessions.json``."""
        try:
            return self._session_store.get_open_sessions()
        except Exception as e:
            log("ERROR", "session_manager", f"get_open_sessions error: {e}")
            return []

    def save_open_session(self, session_id: str) -> None:
        """Add a session ID to the open sessions list."""
        try:
            self._session_store.add_open_session(session_id)
            log(
                "INFO",
                "session_manager",
                f"Session {session_id} added to open sessions",
            )
        except Exception as e:
            log(
                "ERROR",
                "session_manager",
                f"save_open_session error: {e}",
            )

    def remove_open_session(self, session_id: str) -> None:
        """Remove a session ID from the open sessions list."""
        try:
            self._session_store.remove_open_session(session_id)
            log(
                "INFO",
                "session_manager",
                f"Session {session_id} removed from open sessions",
            )
        except Exception as e:
            log(
                "ERROR",
                "session_manager",
                f"remove_open_session error: {e}",
            )

    # ── Close (persistence only — bridge handles agent shutdown) ──────────────

    def close_session(
        self,
        session_id: str,
    ) -> None:
        """Clean up persistence state for a closed session."""
        try:
            self._session_store.remove_open_session(session_id)
            registry = SessionRegistry.get_default()
            registry.set_open(session_id, is_open=False)
            log(
                "INFO",
                "session_manager",
                f"Session closed (persistence cleanup): {session_id}",
            )
        except Exception as e:
            log(
                "ERROR",
                "session_manager",
                f"close_session error: {e}",
            )

    # ── Conversation helpers ──────────────────────────────────────────────────

    def get_conversation(
        self,
        session: Optional[Session],
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Return the normalized conversation for a session, or ``None`` if no
        session is provided.  Normalizes API roles (``"tool"``) to frontend
        roles (``"tool_result"``) without modifying the session data.
        """
        if session is None:
            return None
        return self._normalize_for_frontend(session.user_history or [])

    def load_more_messages(
        self,
        session: Optional[Session],
        offset: int,
        limit: int = 50,
    ) -> Optional[Dict[str, Any]]:
        """
        Return a page of older messages from a session.

        Args:
            session: The session to page through.
            offset:  Number of messages to skip from the end.
            limit:   How many messages to return.

        Returns:
            A dict with ``messages``, ``total_count``, ``has_more``, or
            ``None`` if session is ``None``.
        """
        if session is None:
            return None

        all_messages = session.user_history or []
        total_count = len(all_messages)

        end_idx = total_count - offset
        start_idx = max(0, end_idx - limit)

        page = self._normalize_for_frontend(all_messages[start_idx:end_idx])
        has_more = start_idx > 0

        return {
            "type": "more_messages",
            "messages": page,
            "offset": offset,
            "total_count": total_count,
            "has_more": has_more,
        }

    # ── Session registry helpers ──────────────────────────────────────────────

    def register_session(self, session_id: str) -> None:
        """Register a session in the global ``SessionRegistry``."""
        try:
            registry = SessionRegistry.get_default()
            registry.add(session_id)
        except Exception as e:
            log(
                "WARNING",
                "session_manager",
                f"register_session failed: {e}",
            )

    def unregister_session(self, session_id: str) -> None:
        """Remove a session from the global ``SessionRegistry``."""
        try:
            registry = SessionRegistry.get_default()
            registry.remove(session_id)
        except Exception as e:
            log(
                "WARNING",
                "session_manager",
                f"unregister_session failed: {e}",
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _normalize_for_frontend(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Normalize messages for frontend display without modifying the originals.

        Copied from ``bridge._normalize_for_frontend`` — kept here so
        ``SessionManager`` can produce frontend-friendly message lists
        without importing the bridge module.
        """
        normalized: List[Dict[str, Any]] = []
        last_tool_call_name: Optional[str] = None
        pending_final_assistant = False

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # ── structured tool_calls array (current format) ──
            if role == "assistant" and msg.get("tool_calls"):
                assistant_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                if assistant_msg.get("content"):
                    normalized.append(assistant_msg)
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "?")
                    args_str = func.get("arguments", "{}")
                    try:
                        args_obj = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args_obj = args_str
                    normalized.append({
                        "role": "tool_call",
                        "content": json.dumps({
                            "name": tool_name,
                            "arguments": args_obj,
                        }),
                        "is_final": False,
                        "is_system_notification": False,
                    })
                    last_tool_call_name = tool_name
                continue

            # ── old-format tool call fallback ──
            if role == "assistant" and isinstance(content, str) and content.startswith("[Tool call:"):
                match = re.match(r'^\[Tool call:\s*(\w+)\(([^)]*)\)\]$', content)
                if match:
                    tool_name = match.group(1)
                    args_str = match.group(2)
                    try:
                        args_obj = json.loads(args_str.replace("'", '"'))
                    except Exception:
                        args_obj = {}
                    normalized.append({
                        "role": "tool_call",
                        "content": json.dumps({"name": tool_name, "arguments": args_obj}),
                        "is_final": False,
                        "is_system_notification": False,
                    })
                    last_tool_call_name = tool_name
                    continue

            # ── tool result → tool_result + final detection ──
            if role == "tool":
                new_msg = dict(msg)
                new_msg["role"] = "tool_result"
                if last_tool_call_name in ALL_RESPOND_NAMES:
                    new_msg["is_final"] = True
                    pending_final_assistant = True
                if last_tool_call_name in SUMMARY_TOOL_NAMES:
                    new_msg["is_summary"] = True
                normalized.append(new_msg)
                continue

            # ── assistant message (no tool_calls) ──
            if role == "assistant":
                if isinstance(content, str) and content.strip() == "":
                    continue
                new_msg = dict(msg)
                if pending_final_assistant:
                    new_msg["is_final"] = True
                    pending_final_assistant = False
                normalized.append(new_msg)
                continue

            # ── everything else (user, system notifications) ──
            normalized.append(dict(msg))

        # ── post-processing: inject is_system_notification for frontend ──
        for m in normalized:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                if m["content"].startswith(SYSTEM_NOTIFICATION_PREFIX):
                    m["is_system_notification"] = True
                else:
                    m.setdefault("is_system_notification", False)
            else:
                m.setdefault("is_system_notification", False)
            m.setdefault("is_final", False)
            m.setdefault("response_type", None)

        return normalized
    def repair_session(self, session: Session) -> Session:
        """
    Apply role repair and config migration to an already-loaded session.

    This is a stateless transformation — the session must have already been
    loaded from a store by the caller.  The method:

    * corrects ``tool_result`` roles back to ``tool``
    * migrates legacy config metadata
    * registers the session in the global ``SessionRegistry``

    Returns the same ``Session`` object (mutated in place).
    """
        # ── Repair: restore corrupted roles ───────────────────────────────
        repaired = False
        for msg in session.user_history:
            if msg.get("role") == "tool_result":
                msg["role"] = "tool"
                repaired = True
        if repaired:
            log(
                "WARNING",
                "session_manager",
                f"Repaired {session.session_id}: corrected 'tool_result' roles back to 'tool'",
            )

        # ── Migrate config metadata ───────────────────────────────────────
        session_config_raw = session.metadata.get("session_config") or \
        session.metadata.get("agent_config")
        if session_config_raw and isinstance(session_config_raw, dict):
            if "mode" not in session_config_raw:
                session_config_raw["mode"] = "agent"
                log(
                    "INFO",
                    "session_manager",
                    f"Migrated legacy session {session.session_id}: defaulted mode to 'agent'",
                )

        # Re-register in global registry
        registry = SessionRegistry.get_default()
        name = session.metadata.get("name", "") or ""
        registry.register(
            session_id=session.session_id,
            workspace_id=session.workspace_id or "",
            name=name,
            mode=session.mode,
        )

        return session
