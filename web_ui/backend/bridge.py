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

# Import event system for security prompt forwarding
try:
    from agent.events import global_event_bus, EventType, SecurityPromptEvent
    EVENT_SYSTEM_AVAILABLE = True
except ImportError:
    global_event_bus = None
    EventType = None
    SecurityPromptEvent = None
    EVENT_SYSTEM_AVAILABLE = False

from session.models import Session
from session.store import FileSystemSessionStore


# ══════════════════════════════════════════════════════════════════════════════
#  Bridge class
# ══════════════════════════════════════════════════════════════════════════════

# Global registry of active tab bridges — used to broadcast session renames
# across all open tabs holding the same session.
_active_tab_bridges: set = set()

def _broadcast_rename(session_id: str, new_name: str) -> None:
    """Update in-memory session name on every bridge that has this session loaded."""
    for b in list(_active_tab_bridges):
        try:
            if b._loaded_session and b._loaded_session.session_id == session_id:
                b._loaded_session.metadata['name'] = new_name
            if b._session and b._session.session_id == session_id:
                b._session.metadata['name'] = new_name
        except Exception:
            pass


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
        self._workspace_id: Optional[str] = None

        # Subscribe to global event bus for security prompt events
        self._security_subscription = None
        self._subscribe_to_security_events()

        # Track whether close_session() was called cleanly — used by server.py
        # to avoid re-saving on abrupt disconnect (data loss guard).
        self._cleanly_closed = False


    # ── Security event subscription ───────────────────────────────────────────

    def _subscribe_to_security_events(self) -> None:
        """
        Subscribe to SECURITY_PROMPT events from the global event bus.

        When the tool executor publishes a SecurityPromptEvent (because a
        tool requires a permission set to ``ask``), this handler forwards
        it to the frontend via the WebSocket event callback so the user
        can see a dialog and approve or deny.
        """
        if not EVENT_SYSTEM_AVAILABLE or global_event_bus is None:
            log('WARNING', 'server.bridge', 'Event system unavailable — security prompts not forwarded')
            return

        def _security_prompt_handler(event: SecurityPromptEvent) -> None:
            """Forward a SecurityPromptEvent to the frontend."""
            if self._event_callback is None:
                log('DEBUG', 'server.bridge', 'No event callback — dropping security prompt')
                return
            data = event.data or {}
            # Filter: only handle events for this bridge's session
            if data.get('session_id') != self._session_id:
                log('DEBUG', 'server.bridge',
                    f'Ignoring security prompt for different session '
                    f'(event session_id={data.get("session_id")}, '
                    f'bridge session_id={self._session_id})')
                return
            try:
                self._event_callback({
                    'type': 'security_prompt',
                    'request_id': data.get('request_id', ''),
                    'agent_id': data.get('agent_id', ''),
                    'tool_name': data.get('tool_name', ''),
                    'capabilities': data.get('capabilities', []),
                    'arguments': data.get('arguments', {}),
                    'session_id': data.get('session_id', self._session_id),
                    'description': (
                        f"Tool '{data.get('tool_name', '?')}' requires: "
                        f"{', '.join(data.get('capabilities', []))}"
                    ),
                })
            except Exception as exc:
                log('ERROR', 'server.bridge',
                    f'Failed to forward security prompt: {exc}')

        self._security_subscription = global_event_bus.subscribe(
            EventType.SECURITY_PROMPT, _security_prompt_handler
        )
        log('INFO', 'server.bridge', 'Subscribed to SECURITY_PROMPT events')

    def _unsubscribe_security_events(self) -> None:
        """Unsubscribe from security events."""
        if self._security_subscription is not None:
            try:
                # The subscription might be a registration handle;
                # try to remove it if the bus provides that API.
                if hasattr(global_event_bus, 'unsubscribe'):
                    global_event_bus.unsubscribe(self._security_subscription)
            except Exception:
                pass
            self._security_subscription = None

    # ── Global tab registry ──────────────────────────────────────────────────

    def register(self) -> None:
        """Register this bridge in the global active-tab set."""
        _active_tab_bridges.add(self)

    def unregister(self) -> None:
        """Remove this bridge from the global active-tab set."""
        _active_tab_bridges.discard(self)

    @staticmethod
    def broadcast_rename(session_id: str, new_name: str) -> None:
        """Update in-memory state on all bridges holding this session."""
        _broadcast_rename(session_id, new_name)


    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def session(self) -> Optional[Session]:
        """Get the active session, falling back to the loaded session."""
        return self._session or self._loaded_session

    @property
    def workspace_id(self) -> Optional[str]:
        """Get the workspace ID for this bridge instance."""
        return self._workspace_id

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
        Build an AgentConfig from the global config file
        (~/.thoughtmachine/agent_config.json) via ConfigService,
        mirroring what the PyQt GUI does in
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

        Configuration is built from these layers (each overriding the previous):
          1. ``self._config`` (if already set by a prior ``apply_config`` call)
             *or* global config from ``~/.thoughtmachine/agent_config.json``
          2. Frontend ``config_dict`` (the caller's runtime overrides)

        If a session was previously loaded via load_session(), the loaded
        session's conversation is passed to the controller so the agent
        can continue from that context.  After starting, the loaded-session
        reference is cleared (the new run owns its own session).

        Args:
            query: Initial user query string.
            config_dict: Frontend configuration overrides (see AgentConfig fields).
                         Applied on top of the existing (or global) config.
        """
        # ── Layer 1: existing config from apply_config, or global config ──
        if self._config is not None:
            # A prior apply_config() call already set a validated config.
            # Use it as the base so that continue_session preserves it.
            # NOTE: api_key has ``exclude=True`` on AgentConfig, so model_dump()
            # already strips it. We re-add it explicitly below.
            merged_config = self._config.model_dump(
                exclude={'api_key'}, exclude_none=True)
            merged_config['api_key'] = self._config.api_key
        else:
            global_config = self._build_global_agent_config()
            merged_config = global_config.model_dump(
                exclude={'api_key'}, exclude_none=True)
            merged_config['api_key'] = global_config.api_key

        # ── Layer 2: frontend config_dict (deep merge for nested dicts) ──
        if config_dict:
            from agent.utils import deep_merge
            # Filter out session metadata keys before merging
            filtered = {k: v for k, v in config_dict.items()
                        if k not in ('session_id', 'created_at', 'updated_at')}
            merged_config = deep_merge(merged_config, filtered)

        provider_id = merged_config.get('provider_id')
        if provider_id:
            # Let any ProviderManager / resolve_config errors propagate — the
            # caller (server.py) catches them and reports to the frontend.
            manager = ProviderManager()
            merged_config = manager.resolve_config(merged_config)

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
        # Apply workspace_id from bridge if session doesn't already have one
        if self._workspace_id and not session.workspace_id:
            session.workspace_id = self._workspace_id
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
        self.unregister()
        self._unsubscribe_security_events()
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

    # ── Controller restart / health check ───────────────────────────────────

    def _restart_controller(self, config: Optional[AgentConfig] = None) -> None:
        """
        Restart the controller thread with an optional new config.

        Stops the existing controller (if any), creates a fresh one,
        re-attaches it to the bridge, and preserves the current session
        state.  Called automatically by apply_config() when the controller
        is unresponsive, or explicitly to force a full controller restart.

        Args:
            config: Optional new AgentConfig.  If None, the current
                    self._config is kept.
        """
        old_controller = self._controller
        new_config = config or self._config

        if old_controller is not None:
            log('INFO', 'server.bridge', '_restart_controller: stopping old controller')
            old_controller.stop()

        # Create a fresh controller
        from agent.controller import AgentController
        new_controller = AgentController()
        self.set_controller(new_controller)
        self._controller = new_controller
        self._config = new_config

        # Preserve session ID
        if self._session is not None:
            self._session_id = self._session.session_id

        log('INFO', 'server.bridge', '_restart_controller: controller restarted')

    # ── Container integrity re-sync ───────────────────────────────────────────
    # Called by apply_config() when session_permissions change.

    def _maybe_re_sync_container(self, config: 'AgentConfig') -> None:
        """Verify container integrity after a config change that may affect permissions.

        If the config includes ``session_permissions`` and a ``workspace_path``,
        this calls ``verify_container_integrity`` so any existing container whose
        network/volume mode no longer matches the new permissions is stopped and
        removed.  The container will be recreated with the correct settings the
        next time the agent needs it.

        This is a no-op when Docker is unavailable or no workspace path is known.
        """
        sp = getattr(config, 'session_permissions', None)
        ws = getattr(config, 'workspace_path', None)
        if sp is None or not ws:
            return

        sp_dict = sp.model_dump() if hasattr(sp, 'model_dump') else sp
        try:
            from docker_executor import verify_container_integrity
            result = verify_container_integrity(ws, sp_dict)
            if result.get('action_taken') == 'removed':
                log('INFO', 'server.bridge',
                    f'Container re-synced after config change: '
                    f'{result.get("container_name")} removed '
                    f'(reason: {result.get("mismatch_reason")})')
            elif result.get('action_taken') == 'error':
                log('WARNING', 'server.bridge',
                    f'Container re-sync error: {result.get("mismatch_reason")}')
        except Exception as exc:
            log('WARNING', 'server.bridge',
                f'Container re-sync skipped: {exc}')

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

        # Step 1: Validate non-negative integer fields before merging
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

        # Step 3: Merge incoming on top (deep merge — preserves nested dicts)
        from agent.utils import deep_merge
        merged = deep_merge(base, config_dict)

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
            # ── Health check: verify controller thread is alive ──────────────
            controller_alive = (
                hasattr(self._controller, 'thread')
                and self._controller.thread is not None
                and self._controller.thread.is_alive()
            )
            if not controller_alive:
                log('WARNING', 'server.bridge',
                    'apply_config: controller thread is dead — restarting controller')
                self._restart_controller(validated)
                # Push the config update into the new controller
                self._controller._config = validated
                self._controller.request_config_update(validated)
            else:
                self._controller._config = validated
                self._controller.request_config_update(validated)

        # Step 7: Re-sync container integrity if session_permissions changed
        self._maybe_re_sync_container(self._config)

        # Step 8: Persist to session
        self.save_session()

        # Step 9: Notify frontend — the server handler sends config_changed
        # in frontend format after this method returns, so no explicit emit needed here.

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
                    workspace_id=self._workspace_id,
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
        """
        FINAL_TOOL_NAMES = {"Respond"}
        # Legacy tool names that map to Respond for old session replay
        LEGACY_TO_RESPOND = {
            "Final": {"response_type": "answer"},
            "FinalReport": {"response_type": "answer"},
            "RequestUserInteraction": {"response_type": "question"},
        }
        # Combined set: current Respond + all legacy tools mapped to Respond
        ALL_RESPOND_NAMES = FINAL_TOOL_NAMES | set(LEGACY_TO_RESPOND.keys())
        SUMMARY_TOOL_NAMES = {"SummarizeTool", "summarize", "Summarize"}
        normalized = []
        last_tool_call_name = None       # track for final detection
        pending_final_assistant = False  # mark next assistant as final

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # ── structured tool_calls array (current format) ──
            if role == "assistant" and msg.get("tool_calls"):
                assistant_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                if assistant_msg.get("content"):
                    normalized.append(assistant_msg)
                for tc in msg["tool_calls"]:
                    # 🟢 FIX 1: extract from nested function object
                    func = tc.get("function", {})
                    tool_name = func.get("name", "?")
                    args_str = func.get("arguments", "{}")
                    # arguments is a JSON string; parse if possible, else keep raw
                    try:
                        args_obj = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args_obj = args_str  # fallback: display raw string
                    normalized.append({
                        "role": "tool_call",
                        "content": json.dumps({
                            "name": tool_name,
                            "arguments": args_obj,
                        }),
                        "is_final": False,
                        "is_system_notification": False,
                    })
                    last_tool_call_name = tool_name   # remember for final detection
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
                # 🟣 FIX 2: if preceding tool call was Final/FinalReport, mark final
                if last_tool_call_name in ALL_RESPOND_NAMES:
                    new_msg["is_final"] = True
                    pending_final_assistant = True   # next assistant also final
                # 🟡 SummarizeTool results: dark golden, full markdown, no truncation
                if last_tool_call_name in SUMMARY_TOOL_NAMES:
                    new_msg["is_summary"] = True
                normalized.append(new_msg)
                continue

            # ── assistant message (no tool_calls) ──
            if role == "assistant":
                # drop empty placeholder messages
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
        from agent.core.message import SYSTEM_NOTIFICATION_PREFIX
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
            self._workspace_id = session.workspace_id

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
                    from agent.utils import deep_merge
                    merged = deep_merge(base, agent_config_raw)
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
                "workspace_id": self.workspace_id,
            })
            # Emit initial context_length so the frontend status bar shows
            # the correct value immediately (no need to wait for a live token_update).
            self._emit({
                "type": "context_updated",
                "context_length": self._session.context_length,
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
            # Also update active in-memory session to prevent save_session() from reverting
            if self._session and self._session.session_id == session_id:
                self._session.metadata['name'] = new_name
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
        # STOP the bridge FIRST — let the agent thread finish and flush
        # final messages into user_history before we snapshot.
        self.stop()
        # Wait for agent thread to fully exit (controller.stop() already
        # joins the controller thread with a 5s timeout in shutdown()).
        if self._controller is None and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=60)
        # NOW save — all final messages are captured
        if sid:
            self.save_session()
            self.remove_open_session(sid)
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
        self._cleanly_closed = True
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
                "pausing": "PAUSING",
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
            if ctx is not None:
                if self._session is not None:
                    self._session.context_length = ctx
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

        elif event_type in ("user_query", "turn", "tool_call", "tool_result"):
            # Session has been updated by the agent; sync to frontend.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._emit({
                    "type": "conversation_changed",
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })

        elif event_type == "agent_responded":
            # Always sync conversation first — ensures frontend sees the agent's last message.
            if self._session is not None:
                self._history_version = self._session.conversation_version
                self._emit({
                    "type": "conversation_changed",
                    "messages": self._normalize_for_frontend(self._session.user_history),
                })
            # Then decide UI state based on response_type
            if raw_event.get('response_type') == 'question':
                self._emit({
                    "type": "state_changed",
                    "state": "WAITING_FOR_USER",
                    "is_running": _is_busy,
                })
            else:
                self._emit({
                    "type": "state_changed",
                    "state": "IDLE",
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
