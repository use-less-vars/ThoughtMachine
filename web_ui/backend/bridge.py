"""
WebAgentBridge — Pure‑Python bridge wrapping the Agent class for WebSocket
frontends.  No Qt / PyQt dependencies.

Architecture
────────────
A single bridge instance manages one agent session.  The lifecycle is:

    bridge.start(query, config_dict)
        → creates Agent in a daemon thread
        → runs process_query() → yields events
        → calls event_callback(event_dict) for each event

    bridge.pause()   / bridge.resume()   / bridge.stop()
        → thread‑safe control signals (events + threading.Event)

    bridge.get_conversation()
        → returns current message list

The event_callback is the glue to the outside world (FastAPI WebSocket,
CLI, or any consumer).  It receives raw dicts from the Agent's process_query
generator, plus synthetic events injected by the bridge itself.

Mapping: Agent events → frontend events
────────────────────────────────────────
The bridge translates raw agent events into the simplified event protocol
that the React frontend expects (see frontend/src/App.jsx):

    Agent → process_query yields:    Frontend receives:
    ──────────────────────────────────────────────────
    execution_state_change          → state_changed
    token_update                   → tokens_updated + context_updated
    user_query / turn / tool_call
      / tool_result / final        → conversation_changed
    (synthetic bridge events)      → status_message
"""

from __future__ import annotations

import json
import os
import re
import threading
import queue
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

from agent import Agent
from agent.config import AgentConfig
from agent.config.provider_profile import ProviderManager
from agent.config.service import create_agent_config_service
from agent.controller import AgentController
from agent.logging import log
from agent.core.message import SYSTEM_NOTIFICATION_PREFIX
from session.models import Session
from session.store import FileSystemSessionStore


# ══════════════════════════════════════════════════════════════════════════════
#  Bridge class
# ══════════════════════════════════════════════════════════════════════════════

class WebAgentBridge:
    """
    Thread‑safe bridge that runs one Agent session and emits events through
    a callback.  Designed to be driven by a FastAPI WebSocket endpoint.
    """

    def __init__(self, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self._agent: Optional[Agent] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._processing = False

        # Event primitives
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()          # start in "running" (not paused)

        # Query queue — the agent thread pulls from this
        self._query_queue: queue.Queue = queue.Queue()

        # Mailbox for config updates
        self._pending_config: Optional[AgentConfig] = None

        # Callback — called from the agent thread for every event
        self._event_callback = event_callback

        # Track last known controller busy state for state_changed is_running
        self._last_state_busy: Optional[bool] = None

        # Current config (for get_config / get_conversation)
        self._config: Optional[AgentConfig] = None
        self._session_id: Optional[str] = None

        # Track session conversation version for efficient history sync
        self._history_version: int = 0

        # Controller integration (optional — used when Web UI wants to
        # reuse the existing AgentController instead of creating an Agent directly)
        self._controller: Optional[AgentController] = None

        # Session persistence
        self._session_store = FileSystemSessionStore()
        self._session: Optional[Session] = None
        self._loaded_session: Optional[Session] = None

        # Agent running flag — delegated to controller.is_busy.

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def agent_is_running(self) -> bool:
        """True if the agent controller exists and its thread is alive (can accept queries)."""
        if self._controller is not None:
            return self._controller.is_running
        return False

    @property
    def is_running(self) -> bool:
        """
        Whether the agent thread (standalone) or controller thread (controller mode)
        is still alive and can accept follow-up queries.
        """
        if self._controller is not None:
            return self._controller.is_busy
        return self._running and (self._thread is not None and self._thread.is_alive())

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    @property
    def is_processing(self) -> bool:
        return self._processing

    # ── Event callback ──────────────────────────────────────────────────────

    def set_event_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Set or replace the event callback.  The callback will be called from
        the agent thread for every event generated by process_query().
        """
        self._event_callback = callback

    def set_controller(self, controller: AgentController) -> None:
        """
        Attach an existing AgentController instance.
        When set, bridge delegates start/pause/resume/stop to the controller
        and receives events via controller.set_event_callback().
        """
        self._controller = controller
        controller.set_event_callback(self._on_controller_event)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def _build_global_agent_config(self) -> AgentConfig:
        """
        Build an AgentConfig from the global config file (agent_config.json)
        via ConfigService, mirroring what the PyQt GUI does in
        SessionTab.create_new_session() -> state_bridge.create_agent_config().

        This removes the dependency on presets for config defaults.

        Note: provider resolution and API-key fallback happen *after* all
        three config layers are merged in start(), so they are not done here.
        """
        try:
            service = create_agent_config_service()
            raw_config = service.get_all()
            return AgentConfig(**raw_config)
        except Exception as e:
            log('ERROR', 'server.bridge', f"Could not build global agent config: {e}")
            # Fall back to minimal AgentConfig with env-var key
            return AgentConfig(api_key='')

    def start(self, query: str,
              config_dict: Optional[Dict[str, Any]] = None) -> None:
        """
        Start a new agent session.

        Configuration is built from three layers (each overriding the previous):
          1. Global config (from agent_config.json via ConfigService)
          2. Session config overrides (from a loaded session's metadata)
          3. Frontend config_dict (the caller's runtime overrides)

        If a session was previously loaded via load_session(), the loaded
        session's conversation is passed to the controller so the agent
        can continue from that context.  After starting, the loaded-session
        reference is cleared (the new run owns its own session).

        Args:
            query: Initial user query string.
            config_dict: Frontend configuration overrides (see AgentConfig fields).
                         Applied on top of global config + loaded session config.
        """
        # ── Layer 1: global config from agent_config.json ──────────────────
        global_config = self._build_global_agent_config()
        merged_config = global_config.model_dump(exclude={'api_key'}, exclude_none=True)

        # ── Layer 2: frontend config_dict ─────────────────────────────────
        if config_dict:
            for k, v in config_dict.items():
                if k not in ('session_id', 'created_at', 'updated_at'):
                    merged_config[k] = v

        provider_id = merged_config.get('provider_id')
        if provider_id:
            try:
                manager = ProviderManager()
                merged_config = manager.resolve_config(merged_config)
            except Exception:
                pass

        if not merged_config.get('api_key'):
            log('DEBUG', 'server.bridge', 'No API key resolved from provider profile; Agent will check env vars')

        if self._controller is not None:
            # ── Delegate to controller ──────────────────────────────────
            session_arg = self._loaded_session
            config = AgentConfig(**merged_config)
            self._config = config
            self._running = True
            self._session = session_arg
            self._controller.start(query, config, session=session_arg)
            self._loaded_session = None  # consumed
            return

        if self.is_running:
            raise RuntimeError("Bridge is already running. Stop it first.")

        # Reset primitives
        self._stop_event.clear()
        self._pause_event.set()
        self._running = True
        self._processing = False

        # Build AgentConfig from the merged dict
        config = AgentConfig(**merged_config)
        self._config = config

        # Create session for this run (no Qt dependency, pure Python)
        if self._loaded_session is not None:
            session = self._loaded_session
            self._loaded_session = None  # consumed
        else:
            session = Session()
            session.metadata['source'] = 'web_ui'
        self._session = session
        self._agent = Agent(config, session=session)
        self._session_id = session.session_id

        # Queue the initial query
        self._query_queue.put(query)

        # Start background thread
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="web-bridge-agent",
        )
        self._thread.start()

    def continue_session(self, query: str, config_dict: Optional[Dict[str, Any]] = None) -> None:
        """
        Submit a follow‑up query to a running (or paused) agent.

        If config_dict is provided, it is merged into the current config
        via apply_config() (validated, provider-resolved, persisted) and
        pushed to the controller via request_config_update() so the agent
        picks it up on the next process_query() boundary.
        """
        # ── Apply config update if provided ──────────────────────────────
        if config_dict:
            result = self.apply_config(config_dict)
            if result.get("success"):
                log('INFO', 'server.bridge',
                    f"Config updated during continue_session: "
                    f"provider={self._config.provider_type}, "
                    f"model={self._config.model}")
                # Push to controller so running agent picks it up
                if self._controller is not None:
                    self._controller.request_config_update(self._config)
            else:
                log('WARNING', 'server.bridge',
                    f"Config update skipped during continue_session: "
                    f"{result.get('error', 'unknown error')}")

        # ── Submit the query ──────────────────────────────────────────────
        if self._controller is not None:
            self._controller.continue_session(query)
            return
        if not self.is_running:
            raise RuntimeError("Bridge is not running. Start it first.")
        self.resume()
        self._query_queue.put(query)

    def pause(self) -> None:
        """Request the agent to pause after the current turn."""
        if self._controller is not None:
            self._controller.pause()
            return
        if not self.is_running:
            return
        self._pause_event.clear()
        if self._agent is not None:
            self._agent.request_pause()

    def resume(self) -> None:
        """Resume a paused agent."""
        if self._controller is not None:
            self._controller.resume()
            return
        self._pause_event.set()
        if self._agent is not None:
            self._agent._pause_requested = False

    def stop(self) -> None:
        """Request the agent to stop (finishes current operation then exits)."""
        if self._controller is not None:
            self._controller.stop()
            return
        self._stop_event.set()
        self._pause_event.set()  # unblock if paused

    # ── Query API ───────────────────────────────────────────────────────────

    def get_conversation(self) -> Optional[List[Dict[str, Any]]]:
        """
        Return the current conversation for frontend display.

        Normalizes roles (``tool" → ``tool_result") for the frontend
        without modifying the underlying session data.
        """
        if self._session is not None:
            return self._normalize_for_frontend(self._session.user_history)
        return None

    def get_config(self) -> Optional[AgentConfig]:
        if self._controller is not None:
            return self._controller.get_config() or self._config
        return self._config

    def apply_config(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply a full config dict to the session, merging with existing config.

        Steps:
        1. Start with existing self._config as base (if available)
        2. Merge incoming config_dict on top
        3. Validate the merged dict via validate_config()
        4. Resolve provider credentials if provider_id is present
        5. Store validated config in self._config
        6. Persist to session via save_session()

        Returns:
            {"success": True} or {"success": False, "error": "..."}
        """
        from agent.config.loader import validate_config

        # Step 1: Validate positive integer fields before merging
        for field_name in ('max_tokens',):
            val = config_dict.get(field_name)
            if val is not None:
                if not isinstance(val, int) or val < 1:
                    return {"success": False, "error": f"{field_name} must be a positive integer or null"}
        for field_name in ('token_monitor_warning_threshold', 'token_monitor_critical_threshold'):
            val = config_dict.get(field_name)
            if val is not None:
                if not isinstance(val, int) or val < 0:
                    return {"success": False, "error": f"{field_name} must be a non-negative integer"}

        # Step 2: Start with current config as base
        if self._config is not None:
            base = self._config.model_dump(exclude={'api_key'}, exclude_none=True)
        else:
            base = {}

        # Step 3: Merge incoming on top (shallow merge)
        merged = dict(base)
        merged.update(config_dict)

        # Step 4: Validate the merged dict
        validated = validate_config(merged)
        if validated is None:
            return {"success": False, "error": "Configuration validation failed"}

        # Step 5: Resolve provider if provider_id is present
        validated_dict = validated.model_dump(exclude={'api_key'}, exclude_none=True)
        provider_id = validated_dict.get('provider_id')
        if provider_id:
            try:
                manager = ProviderManager()
                validated_dict = manager.resolve_config(validated_dict)
                validated = AgentConfig(**validated_dict)
            except Exception as e:
                log('WARNING', 'server.bridge', f"Provider resolution failed during apply_config: {e}")

        # Step 6: Store the validated config
        self._config = validated
        if self._controller is not None:
            self._controller._config = validated

        # Step 7: Persist to session
        self.save_session()

        log('INFO', 'server.bridge', 'Config applied and persisted via apply_config')
        return {"success": True}

    # ── Session persistence ──────────────────────────────────────────────────

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all saved sessions from the session store."""
        try:
            return self._session_store.list_sessions()
        except Exception as e:
            log('ERROR', 'server.bridge', f"list_sessions error: {e}")
            return []

    def save_session(self, name: Optional[str] = None) -> Optional[Session]:
        """
        Save current conversation as a session to the store.

        Args:
            name: Optional display name for the session.

        Returns:
            The saved Session object, or None on failure.
        """
        try:
            # Use the active session if available (standalone or controller path)
            session = getattr(self, '_session', None)
            if session is None:
                # Fallback when no session is active — build from existing session data
                session_id = self._loaded_session.session_id if self._loaded_session else str(uuid.uuid4())
                session = Session(
                    session_id=session_id,
                    user_history=list(self._session.user_history) if self._session else [],
                    metadata={
                        'agent_config': self._config.model_dump(exclude={'api_key'}, exclude_none=True) if self._config else {},
                        'source': 'web_ui',
                    }
                )
            else:
                # Update existing session metadata
                session.metadata.setdefault('agent_config', {})
                if self._config:
                    session.metadata['agent_config'] = self._config.model_dump(exclude={'api_key'}, exclude_none=True)
                session.metadata.setdefault('source', 'web_ui')

            # Apply name: explicit arg > existing loaded session name > generated
            if name:
                session.metadata['name'] = name
            elif self._loaded_session and self._loaded_session.metadata.get('name'):
                session.metadata['name'] = self._loaded_session.metadata['name']
            session.ensure_name()
            self._session_store.save_session(session)
            self._loaded_session = session
            log('INFO', 'server.bridge', f"Session saved: {session.session_id} ({session.metadata.get('name')})")
            return session
        except Exception as e:
            import traceback
            traceback.print_exc()
            log('ERROR', 'server.bridge', f"save_session error: {e}")
            return None

    @staticmethod
    def _normalize_for_frontend(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalize messages for frontend display without modifying the originals.

        The session stores roles in canonical API format (e.g. ``role: "tool"``),
        but the frontend ChatPanel expects roles like ``tool_result`` and ``tool_call``.
        This method returns a *new* list with normalized roles, leaving the
        original messages untouched so they remain valid for API calls.

        Additionally injects ``is_system_notification`` into every output message
        because the ``Message`` class derives this flag as a read-only property
        that is **not** included when ``json.dumps`` serializes the dict subclass.
        The frontend needs the flag to style system notifications correctly.
        """
        normalized = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Handle structured tool_calls array (current format)
            if role == "assistant" and msg.get("tool_calls"):
                # Emit the assistant message without the tool_calls array
                assistant_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                if assistant_msg.get("content"):
                    normalized.append(assistant_msg)
                # Emit each tool call as a separate display message
                for tc in msg["tool_calls"]:
                    normalized.append({
                        "role": "tool_call",
                        "content": json.dumps({
                            "name": tc.get("name", "?"),
                            "arguments": tc.get("arguments", {}),
                        }),
                        "is_final": msg.get("is_final", False),
                    })
                continue

            # Convert old tool_call: stored as assistant with "[Tool call: name(args)]"
            if role == "assistant" and isinstance(content, str) and content.startswith("[Tool call:"):
                match = re.match(r'^\[Tool call:\s*(\w+)\(([^)]*)\)\]$', content)
                if match:
                    tool_name = match.group(1)
                    args_str = match.group(2)
                    try:
                        args_json = args_str.replace("'", '"')
                        args = json.loads(args_json)
                    except Exception:
                        args = {}
                    new_content = json.dumps({"name": tool_name, "arguments": args})
                    normalized.append({"role": "tool_call", "content": new_content})
                    continue

            # Convert role "tool" -> "tool_result" for frontend display
            if role == "tool":
                new_msg = dict(msg)
                new_msg["role"] = "tool_result"
                # Preserve is_final flag if present
                if msg.get("is_final"):
                    new_msg["is_final"] = True
                normalized.append(new_msg)
                continue

            # Remove empty assistant messages that were placeholders for tool calls
            if role == "assistant" and isinstance(content, str) and content.strip() == "":
                continue

            # Ensure the message is a plain dict and inject is_system_notification
            # (Message subclass derives it as a property, but json.dumps won't include it)
            normalized.append(dict(msg))

        # Inject is_system_notification into every output message.
        # Message derives it from role+content but never stores it as a real key,
        # so json.dumps misses it entirely.  The frontend needs the flag.
        for m in normalized:
            m.setdefault("is_system_notification", False)
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user" and isinstance(content, str) and content.startswith(SYSTEM_NOTIFICATION_PREFIX):
                m["is_system_notification"] = True

        return normalized

    def load_session(self, session_id: str) -> bool:
        """Load a session from the store, replacing current conversation.

        Also extracts ``session.metadata['agent_config']`` and merges it into
        ``self._config`` so that ``get_config()`` returns the saved configuration
        immediately and ``start()`` uses it as the base config.

        After calling this, the server handler should send ``config_changed``
        (in frontend format) so the frontend controls update.

        Important: The session's ``user_history`` is kept in canonical API format
        (``role: "tool"``). Frontend display normalization happens at emit time
        via ``_normalize_for_frontend()`` so the data stays valid for API calls.
        """
        try:
            session = self._session_store.load_session(session_id)
            if session is None:
                log('WARNING', 'server.bridge', f"Session not found: {session_id}")
                return False
            self._session = session
            self._history_version = session.conversation_version

            # ── Repair: restore corrupted roles ─────────────────────────────
            # Sessions previously saved via a buggy bridge version may have
            # "tool_result" as a role (introduced by in-place frontend normalization).
            # The canonical API format is "tool" – fix it here so the session
            # stays valid for API calls.
            repaired = False
            for msg in session.user_history:
                if msg.get("role") == "tool_result":
                    msg["role"] = "tool"
                    repaired = True
            if repaired:
                log('WARNING', 'server.bridge',
                    f"Repaired {session_id}: corrected 'tool_result' roles back to 'tool'")
                self._session_store.save_session(session)

            self._loaded_session = session

            # ── Extract agent_config from session metadata into self._config ──
            # This makes self._config the single source of truth so bridge.get_config()
            # returns the saved config immediately (config_changed broadcasts show correct values).
            agent_config_raw = session.metadata.get('agent_config', {})
            if agent_config_raw and isinstance(agent_config_raw, dict):
                try:
                    from agent.config.loader import validate_config
                    if self._config is not None:
                        base = self._config.model_dump(exclude={'api_key'}, exclude_none=True)
                    else:
                        base = {}
                    merged = dict(base)
                    merged.update(agent_config_raw)
                    # Ensure enabled_tools is always present — merge from global config if missing
                    if 'enabled_tools' not in merged:
                        try:
                            global_config = self._build_global_agent_config()
                            merged['enabled_tools'] = global_config.enabled_tools
                            log('INFO', 'server.bridge', f"Merged enabled_tools from global config: {len(global_config.enabled_tools)} tools")
                        except Exception as exc:
                            log('WARNING', 'server.bridge', f"Could not merge enabled_tools: {exc}")
                    validated = validate_config(merged)
                    if validated is not None:
                        self._config = validated
                        log('INFO', 'server.bridge', f"Loaded agent_config from session metadata: {len(agent_config_raw)} keys")
                    else:
                        log('WARNING', 'server.bridge', 'load_session: validate_config returned None, keeping existing _config')
                except Exception as exc:
                    log('WARNING', 'server.bridge', f'load_session: config merge failed: {exc}')
            else:
                log('INFO', 'server.bridge', "No agent_config in session metadata")

            # Emit conversation_changed so the frontend updates
            # Use _normalize_for_frontend to convert API roles
            # (e.g. "tool") to frontend roles (e.g. "tool_result")
            # without modifying the session data.
            self._emit({
                "type": "conversation_changed",
                "messages": self._normalize_for_frontend(session.user_history),
            })
            # Emit session_loaded for metadata
            self._emit({
                "type": "session_loaded",
                "session_id": session_id,
                "session_name": session.metadata.get('name', 'Untitled Session'),
                "message_count": len(session.user_history),
            })
            log('INFO', 'server.bridge', f"Session loaded: {session_id} ({session.metadata.get('name')}) — {len(session.user_history)} messages")

            # Register this session as an open session (persists to open_sessions.json)
            # so it survives server restarts and the hub WS can return it.
            # Use add_open_session directly to avoid an unnecessary full session write.
            try:
                self._session_store.add_open_session(session_id)
            except Exception as e:
                log('WARNING', 'server.bridge', f"Could not register open session: {e}")

            return True
        except Exception as e:
            import traceback
            traceback.print_exc()
            log('ERROR', 'server.bridge', f"load_session error: {e}")
            return False

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from the store."""
        try:
            result = self._session_store.delete_session(session_id)
            if result:
                log('INFO', 'server.bridge', f"Session deleted: {session_id}")
            else:
                log('WARNING', 'server.bridge', f"Session not found for deletion: {session_id}")
            return result
        except Exception as e:
            log('ERROR', 'server.bridge', f"delete_session error: {e}")
            return False

    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session in the store."""
        try:
            session = self._session_store.load_session(session_id)
            if session is None:
                log('WARNING', 'server.bridge', f"Session not found for rename: {session_id}")
                return False
            session.metadata['name'] = new_name
            self._session_store.save_session(session)
            # Update loaded session name if it's the one being renamed
            if self._loaded_session and self._loaded_session.session_id == session_id:
                self._loaded_session.metadata['name'] = new_name
            log('INFO', 'server.bridge', f"Session renamed: {session_id} → {new_name}")
            return True
        except Exception as e:
            log('ERROR', 'server.bridge', f"rename_session error: {e}")
            return False

    # ── Open sessions management ────────────────────────────────────────────

    def get_open_sessions(self) -> List[str]:
        """
        Return the list of open session IDs from open_sessions.json.
        Delegates to FileSystemSessionStore.
        """
        try:
            return self._session_store.get_open_sessions()
        except Exception as e:
            log('ERROR', 'server.bridge', f"get_open_sessions error: {e}")
            return []

    def save_open_session(self, session_id: Optional[str] = None) -> None:
        """
        Save the current session and add it to the open sessions list.
        If session_id is provided, that ID is added; otherwise uses the
        bridge's current session ID.
        """
        sid = session_id or self._session_id or (
            self._loaded_session.session_id if self._loaded_session else None
        )
        if sid is None:
            log('WARNING', 'server.bridge', "save_open_session: no session ID available")
            return
        # Save the session first (so it exists on disk)
        self.save_session()
        # Then add to open list
        try:
            self._session_store.add_open_session(sid)
            log('INFO', 'server.bridge', f"Session {sid} added to open sessions")
        except Exception as e:
            log('ERROR', 'server.bridge', f"save_open_session error: {e}")

    def remove_open_session(self, session_id: Optional[str] = None) -> None:
        """
        Remove a session from the open sessions list.
        If session_id is provided, that ID is removed; otherwise uses the
        bridge's current session ID.
        """
        sid = session_id or self._session_id or (
            self._loaded_session.session_id if self._loaded_session else None
        )
        if sid is None:
            log('WARNING', 'server.bridge', "remove_open_session: no session ID available")
            return
        try:
            self._session_store.remove_open_session(sid)
            log('INFO', 'server.bridge', f"Session {sid} removed from open sessions")
        except Exception as e:
            log('ERROR', 'server.bridge', f"remove_open_session error: {e}")

    def close_session(self, session_id: Optional[str] = None) -> None:
        """
        Save session, remove from open sessions list, and stop the bridge.
        This is the complete "close tab" sequence.

        Args:
            session_id: Session to close. If None, uses the bridge's current
                        session or loaded session.
        """
        sid = session_id or self._session_id or (
            self._loaded_session.session_id if self._loaded_session else None
        )
        log('INFO', 'server.bridge', f"Closing session: {sid or '(no id)'}")
        # Save current state
        if sid:
            self.save_session()
            self.remove_open_session(sid)
        # Stop the bridge
        self.stop()
        # Reset state
        self._session = None
        self._loaded_session = None
        self._session_id = None
        self._emit({
            "type": "session_cleared",
        })
        self._emit({
            "type": "state_changed",
            "state": "IDLE",
            "is_running": False,
        })
        log('INFO', 'server.bridge', f"Session closed: {sid or '(no id)'}")

    def clear_loaded_session(self) -> None:
        """Clear the loaded session reference for a fresh start."""
        self._session = None
        self._loaded_session = None
        self._emit({
            "type": "conversation_changed",
            "messages": []
        })
        log('INFO', 'server.bridge', "Session cleared for fresh start")

    # ── Internal: event emission ────────────────────────────────────────────

    def _emit(self, event: Dict[str, Any]) -> None:
        """Thread‑safe event emission via callback."""
        log('DEBUG', 'server.bridge', f"Sending to frontend: {event}")

        if self._event_callback is not None:
            try:
                self._event_callback(event)
            except Exception:
                traceback.print_exc()

    def _on_controller_event(self, event: Dict[str, Any]) -> None:
        """
        Callback registered with the controller via set_event_callback().
        Receives raw events from the controller's agent thread and maps
        them to the frontend protocol, exactly like _map_and_emit does
        for the standalone agent loop.

        Also captures the session ID from the first event that carries one
        and triggers save_open_session() — deferred from start() because
        the session ID isn't known until the controller has created its agent.
        """
        log('DEBUG', 'server.bridge', f"Bridge received event: {event.get('type')}")
        # Capture session ID from first event that has one
        if self._session_id is None:
            sid = event.get('session_id')
            if sid is not None:
                self._session_id = sid
                self.save_open_session()
                log('INFO', 'server.bridge', f"Session ID captured from controller event: {sid}")
        self._map_and_emit(event)

    def _map_and_emit(self, raw_event: Dict[str, Any]) -> None:
        """
        Dual-stream event mapping: tick state and conversation changes
        from raw agent events to frontend protocol events.

        State stream: state_changed, tokens_updated, context_updated, status_message
        Conversation stream: conversation_changed (always sourced from session)
        """
        event_type = raw_event.get("type", "")

        # ── 1. Sync conversation changes from session ──────────────────────
        # This picks up silent mutations (e.g. _pending_warnings flushed
        # after tool commit, errors added as system notifications) so they
        # arrive chronologically before the new event's content.
        if self._session is not None:
            current_version = self._session.conversation_version
            if current_version != self._history_version:
                self._history_version = current_version
                self._emit({
                    "type": "conversation_changed",
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        # ── 2. Handle event-type-specific logic ───────────────────────────
        _is_busy = self._controller.is_busy if self._controller else False

        if event_type == "execution_state_change":
            new_state = raw_event.get("new_state", "")
            frontend_state = {
                "running": "RUNNING",
                "pausing": "PAUSED",
                "paused": "PAUSED",
                "idle": "IDLE",
                "error": "IDLE",
                "stopped": "IDLE",
                "completed": "IDLE",
                "ready": "IDLE",
                "waiting": "WAITING_FOR_USER",
            }.get(new_state.lower(), new_state.upper())
            self._emit({
                "type": "state_changed",
                "state": frontend_state,
                "is_running": _is_busy,
            })

        elif event_type == "token_update":
            tokens_in = raw_event.get("total_input", 0)
            tokens_out = raw_event.get("total_output", 0)
            self._emit({
                "type": "tokens_updated",
                "input": tokens_in,
                "output": tokens_out,
            })
            ctx = raw_event.get("context_length", 0)
            if ctx:
                self._emit({
                    "type": "context_updated",
                    "context_length": ctx,
                })

        elif event_type in ("token_warning", "turn_warning"):
            # Conversation should already reflect the warning in session;
            # re-read to ensure frontend gets the full snapshot.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._emit({
                    "type": "conversation_changed",
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        elif event_type in ("user_query", "turn", "tool_call", "tool_result", "final"):
            # Session has been updated by the agent; sync to frontend.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._emit({
                    "type": "conversation_changed",
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })
            if event_type == "final":
                self._emit({
                    "type": "state_changed",
                    "state": "IDLE",
                    "is_running": _is_busy,
                })

        elif event_type == "user_interaction_requested":
            self._emit({
                "type": "state_changed",
                "state": "WAITING_FOR_USER",
                "is_running": _is_busy,
            })

        elif event_type == "error":
            msg_text = raw_event.get('message', 'unknown')
            error_type = raw_event.get('error_type', 'PROVIDER_ERROR')
            self._emit({
                "type": "status_message",
                "text": f"⚠ Error: {msg_text}",
            })
            # Error may have added a system notification to session; sync it
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._emit({
                    "type": "conversation_changed",
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        # ── 3. Handle stop signals ────────────────────────────────────────
        if event_type == "session_stop":
            if raw_event.get("stop_reason"):
                self._emit({
                    "type": "state_changed",
                    "state": "IDLE",
                    "is_running": _is_busy,
                })
            return

        if raw_event.get("stop_reason"):
            self._emit({
                "type": "state_changed",
                "state": "IDLE",
                "is_running": _is_busy,
            })

    # ── Internal: agent thread ──────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Background thread: owns the Agent and runs process_query()."""
        try:
            while self._running and not self._stop_event.is_set():
                # Check pause
                if not self._pause_event.is_set():
                    self._pause_event.wait(timeout=0.5)
                    continue

                # Wait for a query
                try:
                    query = self._query_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # Handle special commands
                if query == "[STOP]":
                    break
                if query == "[RESET]":
                    if self._agent is not None:
                        self._agent.reset()
                    continue

                # Process the query
                self._processing = True
                try:
                    for raw_event in self._agent.process_query(query):
                        if self._stop_event.is_set():
                            break
                        self._map_and_emit(raw_event)

                        # Check pause after each event
                        if not self._pause_event.is_set():
                            break

                        # If the agent yielded a stop_reason, break
                        if raw_event.get("stop_reason"):
                            break
                except Exception as exc:
                    traceback.print_exc()
                    self._emit({
                        "type": "error",
                        "error_type": "PROCESS_QUERY_ERROR",
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    })
                finally:
                    self._processing = False
                    self._map_and_emit({
                        "type": "session_stop",
                        "stop_reason": "completed",
                    })
        except Exception as exc:
            traceback.print_exc()
            self._emit({
                "type": "error",
                "error_type": "BRIDGE_LOOP_ERROR",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            })
        finally:
            self._running = False
            self._emit({
                "type": "state_changed",
                "state": "IDLE",
                "is_running": False,
            })
            self._emit({"type": "thread_finished"})


# ══════════════════════════════════════════════════════════════════════════════
#  Convenience factory
# ══════════════════════════════════════════════════════════════════════════════

def create_bridge(event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> WebAgentBridge:
    """Factory function – create a WebAgentBridge with optional callback."""
    return WebAgentBridge(event_callback=event_callback)
