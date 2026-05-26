"""
FastAPI WebSocket server for the ThoughtMachine Web UI.

Architecture
────────────
                          ┌──────────────────┐
                          │   React Frontend  │  (Vite dev server :5173)
                          └────────┬─────────┘
                                   │ ws://host:8000/ws
                          ┌────────▼─────────┐
                          │  FastAPI Server   │  (uvicorn :8000)
                          │  /ws  endpoint    │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │  WebAgentBridge   │  (bridge.py)
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │  Agent  +  LLM    │
                          └──────────────────┘

Startup
───────
    uvicorn web_ui.backend.server:app --host 0.0.0.0 --port 8000 --reload

Or via provided run script:
    python -m web_ui.backend.server

WebSocket Protocol
──────────────────
Client → Server (JSON):
    { "command": "start_session",     "query": "...", "config": {...} }
    { "command": "continue_session",  "query": "..." }
    { "command": "pause_session" }
    { "command": "resume_session" }
    { "command": "stop_session" }
    { "command": "get_config" }
    { "command": "get_conversation" }
    { "command": "update_config",     "field": "...", "value": ... }
    { "command": "apply_config",      "config": {...} }
    { "command": "list_sessions" }
    { "command": "save_session",          "name": "..." (optional) }
    { "command": "load_session",          "session_id": "..." }
    { "command": "get_providers" }
    { "command": "save_provider",      "provider": {...} }
    { "command": "delete_provider",    "provider_id": "..." }
    { "command": "get_available_tools" }
    { "command": "delete_session",        "session_id": "..." }
    { "command": "rename_session",        "session_id": "...", "new_name": "..." }
    { "command": "get_open_sessions" }
    { "command": "close_session",          "session_id": "..." (optional) }
    { "command": "new_session" }

Server → Client (JSON):
    state_changed       { "type": "state_changed",       "state": "IDLE|RUNNING|PAUSED|WAITING_FOR_USER", "is_running": bool }
    tokens_updated      { "type": "tokens_updated",      "input": int, "output": int }
    context_updated     { "type": "context_updated",     "context_length": int }
    conversation_changed { "type": "conversation_changed", "messages": [...] }
    config_changed      { "type": "config_changed",      "config": {...} }
    status_message      { "type": "status_message",      "text": "..." }
    sessions_list       { "type": "sessions_list",       "sessions": [...] }
    session_saved       { "type": "session_saved",       "session": {...} }
    session_loaded      { "type": "session_loaded",      "session_id": "...", "session_name": "...", "message_count": int }
    session_deleted     { "type": "session_deleted",     "session_id": "..." }
    session_renamed     { "type": "session_renamed",     "session_id": "...", "new_name": "..." }
    open_sessions_list  { "type": "open_sessions_list",  "session_ids": ["..."] }
    session_closed      { "type": "session_closed",      "session_id": "..." }
    session_cleared     { "type": "session_cleared" }
    providers_list      { "type": "providers_list",      "providers": [...] }
    provider_saved      { "type": "provider_saved",      "provider": {...} }
    provider_deleted    { "type": "provider_deleted",    "provider_id": "..." }
    tools_list          { "type": "tools_list",          "tools": [...] }
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from agent.logging import log
from contextlib import asynccontextmanager
from session.store import FileSystemSessionStore

# Ensure project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── App + lifespan ──────────────────────────────────────────────────────────

# We import bridge lazily inside the lifespan / endpoint to avoid
# circular / early import issues.

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler — no global state needed (state is per‑connection)."""
    log('INFO', 'server', 'ThoughtMachine Web UI server starting ...')
    yield
    log('INFO', 'server', 'Server shutting down.')

app = FastAPI(
    title="ThoughtMachine Web UI",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS (allow frontend dev server on any port) ────────────────────────────
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
#  WebSocket endpoint
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log('INFO', 'server.ws', f'WebSocket connected: {ws.client}')

    # Import bridge here (after project root is on sys.path)
    from web_ui.backend.bridge import WebAgentBridge
    from agent.controller import AgentController

    bridge: Optional[WebAgentBridge] = None
    session_store = FileSystemSessionStore()

    # Capture the asyncio event loop HERE (inside the async handler)
    # so we can schedule sends from the agent thread later.
    import asyncio
    _loop = asyncio.get_running_loop()

    # Asynchronous event sender — queues events to the WebSocket
    async def send_event(event: Dict[str, Any]) -> None:
        try:
            await ws.send_json(event)
        except Exception as exc:
            log('ERROR', 'server.ws', f'send_event failed: {exc}')

    # Callback wrapper — called from the bridge's agent thread
    def event_callback(event: Dict[str, Any]) -> None:
        """Called from agent thread.  Schedule send on the asyncio loop."""
        try:
            asyncio.run_coroutine_threadsafe(send_event(event), _loop)
        except Exception as exc:
            log('ERROR', 'server.ws', f'event_callback error: {exc}')
            import traceback
            traceback.print_exc()

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "status_message", "text": "⚠ Invalid JSON received."})
                continue

            command = msg.get("command", "")
            log('DEBUG', 'server.ws', f'Command: {command}')

            # ── Handle commands ─────────────────────────────────────────────
            try:
                if command == "start_session":
                    query = msg.get("query", "")
                    config_dict = msg.get("config", {})
                    if not query.strip():
                        await ws.send_json({"type": "status_message", "text": "⚠ Query cannot be empty."})
                        continue

                    # Translate frontend config format → AgentConfig format.
                    # Global config from agent_config.json provides defaults;
                    # frontend fields become overrides.
                    config_dict = _translate_frontend_config(config_dict)

                    # Always create a fresh bridge + controller for start_session.
                    # Stop any existing bridge first to prevent resource leaks.
                    if bridge is not None:
                        bridge.stop()
                    controller = AgentController()
                    bridge = WebAgentBridge(event_callback=event_callback)
                    bridge.set_controller(controller)

                    try:
                        bridge.start(query, config_dict)
                    except RuntimeError as exc:
                        # Controller may be stuck from a prior session; stop and retry
                        await ws.send_json({"type": "status_message", "text": f"⚠ Controller busy — resetting: {exc}"})
                        bridge.stop()
                        bridge.start(query, config_dict)
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to start: {exc}",
                        })
                        continue
                    await ws.send_json({"type": "status_message", "text": "Session started."})

                elif command == "continue_session":
                    query = msg.get("query", "")
                    if not query.strip():
                        await ws.send_json({"type": "status_message", "text": "⚠ Query cannot be empty."})
                        continue

                    # Case 1: Bridge has a loaded session (first query for a new session)
                    # Only start if the agent is not already running.
                    if bridge is not None and bridge._loaded_session is not None and not bridge.agent_is_running:
                        log('INFO', 'server.ws', f'continue_session: loaded session exists — starting bridge with session {bridge._loaded_session.session_id}')
                        try:
                            # Pass the frontend config if available (for first query)
                            config_dict = msg.get("config", {})
                            if config_dict:
                                config_dict = _translate_frontend_config(config_dict)
                                bridge.start(query, config_dict)
                            else:
                                bridge.start(query)
                        except Exception as exc:
                            await ws.send_json({
                                "type": "status_message",
                                "text": f"⚠ Failed to start loaded session: {exc}",
                            })
                        continue

                    # Case 2: Agent is already running — continue normally
                    if bridge is not None and bridge.agent_is_running:
                        try:
                            config_dict = msg.get("config", {})
                            if config_dict:
                                config_dict = _translate_frontend_config(config_dict)
                            bridge.continue_session(query, config_dict)
                        except Exception as exc:
                            await ws.send_json({
                                "type": "status_message",
                                "text": f"⚠ Failed to continue: {exc}",
                            })
                        continue

                    # Case 3: Nothing to continue
                    await ws.send_json({"type": "status_message", "text": "No active session — start a new one."})

                elif command == "pause_session":
                    if bridge is not None:
                        bridge.pause()
                        await ws.send_json({"type": "status_message", "text": "⏸ Paused."})

                elif command == "resume_session":
                    if bridge is not None:
                        bridge.resume()
                        await ws.send_json({"type": "status_message", "text": "▶ Resumed."})

                elif command == "stop_session":
                    if bridge is not None:
                        bridge.stop()
                        await ws.send_json({"type": "status_message", "text": "⏹ Stopped."})

                elif command == "get_config":
                    # _frontend_config_from_bridge handles bridge=None and cfg=None gracefully
                    await ws.send_json({
                        "type": "config_changed",
                        "config": _frontend_config_from_bridge(bridge),
                    })

                elif command == "get_conversation":
                    if bridge is not None:
                        conv = bridge.get_conversation()
                        if conv is not None:
                            # Session user_history already has normalized roles;
                            # use directly without remapping.
                            await ws.send_json({
                                "type": "conversation_changed",
                                "messages": conv,
                            })

                elif command == "update_config":
                    # Config update — stored for next session start
                    field = msg.get("field", "")
                    value = msg.get("value")
                    # If bridge exists, apply update to live config
                    if bridge is not None and bridge._config is not None:
                        cfg = bridge._config
                        try:
                            if field == "temperature":
                                cfg.temperature = float(value)
                            elif field == "max_turns":
                                cfg.max_turns = int(value)
                            elif field.startswith("tools."):
                                pass  # tool toggle handled at session start
                            elif field == "provider":
                                cfg.provider_type = value
                            else:
                                pass
                        except (ValueError, TypeError):
                            pass
                    await ws.send_json({
                        "type": "config_changed",
                        "config": _frontend_config_from_bridge(bridge) if bridge else _default_frontend_config(),
                    })

                elif command == "apply_config":
                    # Receive full config from frontend, validate, merge, persist
                    config = msg.get("config", {})
                    if not config:
                        await ws.send_json({
                            "type": "status_message",
                            "text": "⚠ apply_config: empty config received",
                        })
                        continue

                    if bridge is None:
                        await ws.send_json({
                            "type": "status_message",
                            "text": "⚠ No active session to configure",
                        })
                        continue

                    # Translate frontend format → backend AgentConfig format
                    backend_config = _translate_frontend_config(config)

                    # Apply via bridge (validates, merges, resolves provider, persists)
                    result = bridge.apply_config(backend_config)

                    if result.get("success"):
                        await ws.send_json({
                            "type": "config_changed",
                            "config": _frontend_config_from_bridge(bridge),
                        })
                        log('INFO', 'server.config', "Config applied and persisted via apply_config")
                    else:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to apply config: {result.get('error', 'unknown error')}",
                        })

                elif command == "get_providers":
                    # Return list of provider profiles (safe fields only)
                    from agent.config.provider_profile import ProviderManager
                    try:
                        manager = ProviderManager()
                        profiles = manager.list_profiles()
                        safe_profiles = []
                        for p in profiles:
                            safe_profiles.append({
                                "id": p.id,
                                "label": p.label,
                                "provider_type": p.provider_type,
                                "base_url": p.base_url,
                                "default_model": p.default_model,
                                "models": list(p.models) if p.models else [],
                                "timeout": p.timeout,
                            })
                        await ws.send_json({
                            "type": "providers_list",
                            "providers": safe_profiles,
                        })
                        log('INFO', 'server.config', f"Returned {len(safe_profiles)} provider profiles")
                    except Exception as exc:
                        log('ERROR', 'server.config', f"get_providers failed: {exc}")
                        await ws.send_json({
                            "type": "providers_list",
                            "providers": [],
                        })

                elif command == "save_provider":
                    # Save (add or update) a provider profile
                    from agent.config.provider_profile import ProviderManager, ProviderProfile
                    try:
                        provider_data = msg.get('provider', {})
                        if not provider_data.get('id'):
                            await ws.send_json({
                                "type": "status_message",
                                "text": "⚠ Provider must have an id",
                            })
                        else:
                            manager = ProviderManager()
                            profile = ProviderProfile(**provider_data)
                            manager.add_profile(profile)
                            if manager.save():
                                # Re-read to confirm and broadcast updated list
                                manager2 = ProviderManager()
                                profiles = manager2.list_profiles()
                                safe_profiles = []
                                for p in profiles:
                                    safe_profiles.append({
                                        "id": p.id,
                                        "label": p.label,
                                        "provider_type": p.provider_type,
                                        "base_url": p.base_url,
                                        "default_model": p.default_model,
                                        "models": list(p.models) if p.models else [],
                                        "timeout": p.timeout,
                                    })
                                await ws.send_json({
                                    "type": "provider_saved",
                                    "provider": provider_data,
                                })
                                await ws.send_json({
                                    "type": "providers_list",
                                    "providers": safe_profiles,
                                })
                                log('INFO', 'server.config', f"Saved provider: {provider_data.get('id')}")
                            else:
                                await ws.send_json({
                                    "type": "status_message",
                                    "text": "⚠ Failed to save provider",
                                })
                    except Exception as exc:
                        log('ERROR', 'server.config', f"save_provider failed: {exc}")
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to save provider: {exc}",
                        })

                elif command == "delete_provider":
                    # Delete a provider profile by id
                    from agent.config.provider_profile import ProviderManager
                    try:
                        provider_id = msg.get('provider_id', '')
                        if not provider_id:
                            await ws.send_json({
                                "type": "status_message",
                                "text": "⚠ Provider id is required",
                            })
                        else:
                            manager = ProviderManager()
                            if manager.delete_profile(provider_id):
                                manager.save()
                                # Broadcast updated list
                                manager2 = ProviderManager()
                                profiles = manager2.list_profiles()
                                safe_profiles = []
                                for p in profiles:
                                    safe_profiles.append({
                                        "id": p.id,
                                        "label": p.label,
                                        "provider_type": p.provider_type,
                                        "base_url": p.base_url,
                                        "default_model": p.default_model,
                                        "models": list(p.models) if p.models else [],
                                        "timeout": p.timeout,
                                    })
                                await ws.send_json({
                                    "type": "provider_deleted",
                                    "provider_id": provider_id,
                                })
                                await ws.send_json({
                                    "type": "providers_list",
                                    "providers": safe_profiles,
                                })
                                log('INFO', 'server.config', f"Deleted provider: {provider_id}")
                            else:
                                await ws.send_json({
                                    "type": "status_message",
                                    "text": f"⚠ Provider '{provider_id}' not found",
                                })
                    except Exception as exc:
                        log('ERROR', 'server.config', f"delete_provider failed: {exc}")
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to delete provider: {exc}",
                        })

                elif command == "get_available_tools":
                    # Return list of available tool definitions
                    try:
                        from tools import SIMPLIFIED_TOOL_CLASSES
                        tool_defs = []
                        for cls in SIMPLIFIED_TOOL_CLASSES:
                            tool_defs.append({
                                "name": cls.__name__,
                                "description": (cls.__doc__ or "").strip(),
                            })
                        await ws.send_json({
                            "type": "tools_list",
                            "tools": tool_defs,
                        })
                        log('INFO', 'server.config', f"Returned {len(tool_defs)} available tools")
                    except Exception as exc:
                        log('ERROR', 'server.config', f"get_available_tools failed: {exc}")
                        await ws.send_json({
                            "type": "tools_list",
                            "tools": [],
                        })

                elif command == "list_sessions":
                    try:
                        sessions = session_store.list_sessions()
                        await ws.send_json({
                            "type": "sessions_list",
                            "sessions": sessions,
                        })
                        log("INFO", "server.config", f"Listed {len(sessions)} sessions")
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to list sessions: {exc}",
                        })
                        log("ERROR", "server.config", f"list_sessions failed: {exc}")

                elif command == "save_session":
                    if bridge is None or not bridge.is_running:
                        await ws.send_json({"type": "status_message", "text": "⚠ No active session to save."})
                        continue
                    name = msg.get("name", "")
                    try:
                        session = bridge.save_session(name=name if name else None)
                        await ws.send_json({
                            "type": "session_saved",
                            "session": {
                                "id": session.session_id,
                                "name": session.metadata.get('name', 'Untitled'),
                                "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at),
                                "updated_at": session.updated_at.isoformat() if hasattr(session.updated_at, 'isoformat') else str(session.updated_at),
                                "message_count": len(session.user_history) if session.user_history else 0,
                            },
                        })
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to save session: {exc}",
                        })

                elif command == "load_session":
                    session_id = msg.get("session_id", "")
                    if not session_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ session_id is required."})
                        continue
                    # If a bridge/session is running, stop it first
                    if bridge is not None:
                        bridge.stop()
                    try:
                        # Create fresh controller and bridge
                        from agent.controller import AgentController
                        controller = AgentController()
                        bridge = WebAgentBridge(event_callback=event_callback)
                        bridge.set_controller(controller)
                        bridge.load_session(session_id)
                        await ws.send_json({
                            "type": "session_loaded",
                            "session_id": session_id,
                        })
                        await ws.send_json({
                            "type": "state_changed",
                            "state": "IDLE",
                            "is_running": False,
                        })
                        # Send tokens_updated so the frontend shows saved token counts
                        loaded = bridge._session or bridge._loaded_session
                        if loaded:
                            await ws.send_json({
                                "type": "tokens_updated",
                                "input": loaded.total_input_tokens,
                                "output": loaded.total_output_tokens,
                            })
                            await ws.send_json({
                                "type": "context_updated",
                                "context_length": loaded.context_length,
                            })
                        # Send config_changed so the frontend shows the session's actual config
                        fe_config = _frontend_config_from_bridge(bridge)
                        await ws.send_json({
                            "type": "config_changed",
                            "config": fe_config,
                        })
                        await ws.send_json({"type": "status_message", "text": f"Session {session_id} loaded. Click Run to continue."})
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to load session: {exc}",
                        })

                elif command == "delete_session":
                    session_id = msg.get("session_id", "")
                    if not session_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ session_id is required."})
                        continue
                    try:
                        session_store.delete_session(session_id)
                        await ws.send_json({
                            "type": "session_deleted",
                            "session_id": session_id,
                        })
                        log("INFO", "server.config", f"Deleted session {session_id}")
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to delete session: {exc}",
                        })
                        log("ERROR", "server.config", f"delete_session failed: {exc}")

                elif command == "rename_session":
                    session_id = msg.get("session_id", "")
                    new_name = msg.get("new_name", "")
                    if not session_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ session_id is required."})
                        continue
                    try:
                        session = session_store.load_session(session_id)
                        if session is None:
                            await ws.send_json({"type": "status_message", "text": f"⚠ Session not found: {session_id}"})
                            continue
                        session.metadata['name'] = new_name
                        session_store.save_session(session)
                        await ws.send_json({
                            "type": "session_renamed",
                            "session_id": session_id,
                            "new_name": new_name,
                        })
                        log("INFO", "server.config", f"Renamed session {session_id} → {new_name}")
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to rename session: {exc}",
                        })
                        log("ERROR", "server.config", f"rename_session failed: {exc}")

                elif command == "get_open_sessions":
                    log("INFO", "server.config", "get_open_sessions handler reached")
                    try:
                        open_ids = session_store.get_open_sessions()
                        log("INFO", "server.config", f"get_open_sessions returned {len(open_ids)} ids: {open_ids}")
                        open_sessions = []
                        for sid in open_ids:
                            session = session_store.load_session(sid)
                            if session:
                                open_sessions.append({
                                    "session_id": session.session_id,
                                    "name": session.metadata.get('name', 'Untitled'),
                                    "updated_at": session.updated_at.isoformat(),
                                    "message_count": len(session.user_history) if session.user_history else 0
                                })
                        response = {"type": "open_sessions", "sessions": open_sessions}
                        await ws.send_json(response)
                        log("INFO", "server.config", f"Sent open_sessions with {len(open_sessions)} items")
                    except Exception as e:
                        log("ERROR", "server.config", f"get_open_sessions failed: {e}")
                        await ws.send_json({"type": "error", "message": str(e)})

                elif command == "close_session":
                    session_id = msg.get("session_id", "")
                    try:
                        if bridge is not None:
                            bridge.close_session(session_id if session_id else None)
                        else:
                            # No bridge active — just acknowledge
                            await ws.send_json({"type": "status_message", "text": "Session closed."})
                        await ws.send_json({
                            "type": "session_closed",
                            "session_id": session_id,
                        })
                        await ws.send_json({
                            "type": "state_changed",
                            "state": "IDLE",
                            "is_running": False,
                        })
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to close session: {exc}",
                        })

                elif command == "new_session":
                    # Stop any existing bridge
                    if bridge is not None:
                        bridge.stop()

                    # Create fresh bridge + controller with a new Session.
                    # The bridge sits IDLE with _loaded_session set so that
                    # the first continue_session triggers Case 1 (loaded session)
                    # and calls bridge.start(query) to kick off the agent.
                    from agent.controller import AgentController
                    controller = AgentController()
                    bridge = WebAgentBridge(event_callback=event_callback)
                    bridge.set_controller(controller)

                    # Create a new empty session
                    from session.models import Session
                    new_session = Session()
                    new_session.metadata['source'] = 'web_ui'
                    new_session.ensure_name()

                    # Store as the loaded session so continue_session picks it up
                    bridge._loaded_session = new_session

                    await ws.send_json({
                        "type": "session_loaded",
                        "session_id": new_session.session_id,
                    })
                    await ws.send_json({
                        "type": "state_changed",
                        "state": "IDLE",
                        "is_running": False,
                    })
                    # Reset token display for a fresh session
                    await ws.send_json({
                        "type": "tokens_updated",
                        "input": 0,
                        "output": 0,
                    })
                    await ws.send_json({
                        "type": "context_updated",
                        "context_length": 0,
                    })
                    # Send default config so the frontend shows proper defaults
                    await ws.send_json({
                        "type": "config_changed",
                        "config": _default_frontend_config(),
                    })
                    await ws.send_json({"type": "status_message", "text": "Ready. Type a query to start."})

                else:
                    await ws.send_json({
                        "type": "status_message",
                        "text": f"⚠ Unknown command: {command}",
                    })
            except Exception as exc:
                log('ERROR', 'server.ws', f'FATAL WebSocket error: {exc}')
                import traceback
                traceback.print_exc()
                try:
                    await ws.send_json({"type": "status_message", "text": f"⚠ Internal error: {exc}"})
                except Exception:
                    pass

    except WebSocketDisconnect:
        log('INFO', 'server.ws', f'WebSocket disconnected: {ws.client}')
    except Exception as exc:
        log('ERROR', 'server.ws', f'WebSocket error: {exc}')
        traceback.print_exc()
    finally:
        # Cleanup: auto-save open session + stop bridge
        if bridge is not None:
            try:
                bridge.save_open_session()
            except Exception:
                pass
            bridge.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  REST endpoints (health, information)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/browse")
async def browse_directory(path: str = ""):
    """List directory contents for the workspace path browser."""
    try:
        base_path = path or os.path.expanduser("~")
        if not os.path.isdir(base_path):
            return {"success": False, "error": f"Not a directory: {base_path}", "entries": []}
        entries = []
        try:
            for name in sorted(os.listdir(base_path)):
                full = os.path.join(base_path, name)
                try:
                    entries.append({
                        "name": name,
                        "is_dir": os.path.isdir(full),
                        "size": os.path.getsize(full) if os.path.isfile(full) else None,
                    })
                except (OSError, PermissionError):
                    entries.append({"name": name, "is_dir": False, "size": None})
        except PermissionError:
            pass
        parent = os.path.dirname(base_path.rstrip("/")) if base_path != "/" else None
        return {
            "success": True,
            "current_path": os.path.abspath(base_path),
            "parent_path": parent,
            "entries": entries,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "entries": []}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "thoughtmachine-web-ui"}

@app.get("/")
async def root():
    return {
        "name": "ThoughtMachine Web UI",
        "version": "0.1.0",
        "endpoints": {
            "ws": "/ws — WebSocket for agent interaction",
            "health": "/health — Health check",
            "browse": "/api/browse?path=… — Directory listing for workspace browser",
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _translate_frontend_config(fe_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translate frontend config format to AgentConfig format.

    Frontend sends:
        provider: 'openai' | 'anthropic' | 'local'
        tools: [{name, enabled}, ...]
        temperature, max_turns, etc.

    AgentConfig expects:
        provider_type: 'openai_compatible' | 'anthropic' | 'openai'
        enabled_tools: ['name1', 'name2', ...]
    """
    cfg = dict(fe_config)

    # Map provider names
    provider_map = {
        "openai": "openai",
        "anthropic": "anthropic",
        "local": "openai_compatible",
        "openai_compatible": "openai_compatible",
    }
    provider = cfg.pop("provider", None)
    if provider:
        cfg["provider_type"] = provider_map.get(provider, provider)

    # Translate frontend tools list → backend enabled_tools
    # Frontend: [{name: "bash", enabled: true}, {name: "file_read", enabled: false}, ...]
    # Backend:  ["bash", ...]  (only enabled tools)
    tools_list = cfg.pop("tools", None)
    if isinstance(tools_list, list):
        enabled = [t["name"] for t in tools_list if isinstance(t, dict) and t.get("enabled")]
        if enabled:
            cfg["enabled_tools"] = enabled

    # Remove any keys that start with _
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}

    # ── Translate diagnostic log ──────────────────────────────────────
    log('INFO', 'server.config',
        f"[TRANSLATE] frontend config: provider={fe_config.get('provider')}, "
        f"model={fe_config.get('model')}, keys={list(fe_config.keys())}")
    log('DEBUG', 'server.config',
        f"[TRANSLATE] full dump: tools_field={fe_config.get('tools')}, "
        f"enabled_tools_after={cfg.get('enabled_tools')}")

    return cfg


def _frontend_config_from_bridge(bridge) -> Dict[str, Any]:
    """Convert bridge's AgentConfig back to frontend config format."""
    if bridge is None:
        return _default_frontend_config()
    from web_ui.backend.bridge import WebAgentBridge
    cfg = bridge.get_config()
    if cfg is None:
        return _default_frontend_config()
    raw = _config_to_dict(cfg)
    return _backend_to_frontend_config(raw)



_FALLBACK_FRONTEND_CONFIG = {
    "base_url": "https://api.deepseek.com/v1/",
    "model": "deepseek-v4-flash",
    "provider_type": "openai_compatible",
    "provider_config": {},
    "provider_id": "v4_flash",
    "model_override": None,
    "temperature": 1.0,
    "max_turns": 200,
    "stop_check": None,
    "system_prompt": None,
    "token_monitor_warning_threshold": 60000,
    "token_monitor_critical_threshold": 75000,
    "turn_monitor_enabled": True,
    "enable_logging": True,
    "log_dir": "./logs",
    "log_level": "INFO",
    "enable_file_logging": True,
    "enable_console_logging": False,
    "jsonl_format": True,
    "log_categories": ["SESSION", "LLM", "TOOLS"],
    "max_file_size_mb": 10,
    "max_backup_files": 5,
    "workspace_path": "/home/jojo/PycharmProjects/ThoughtMachine-dev",
    "rag_enabled": False,
    "rag_embedding_model": "BAAI/bge-small-en-v1.5",
    "rag_vector_store_path": None,
    "rag_chunk_size": 1500,
    "rag_chunk_overlap": 200,
    "rag_batch_size": 16,
    "rag_truncate_dim": 256,
    "kb_enabled": True,
    "kb_path": None,
    "tool_output_token_limit": 10000,
    "detail": "normal",
    "enabled_tools": [
        "FileEditor",
        "FilePreviewTool",
        "DirectoryTreeTool",
        "GlobTool",
        "FileSearchTool",
        "ApplyEdits",
        "CodeModifier",
        "RefactorTool",
        "DateTimeTool",
        "DirectoryCreator",
        "DockerCodeRunner",
        "FieldViewer",
        "FileMover",
        "FileSummaryTool",
        "Final",
        "FinalReport",
        "GitInfoTool",
        "KnowledgeBaseTool",
        "MCPValidator",
        "PaginateTool",
        "ProgressReport",
        "RequestUserInteraction",
        "SummarizeTool",
        "Thought"
    ],
}


def _load_global_defaults() -> Dict[str, Any]:
    """Load global defaults from ~/.thoughtmachine/agent_config.json.
    Auto-creates the file with sensible defaults on first run."""
    import json
    from pathlib import Path
    config_dir = Path.home() / '.thoughtmachine'
    config_path = config_dir / 'agent_config.json'

    config_dir.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except Exception:
            log('ERROR', 'server.config', f'Could not parse {config_path}, using fallback')
            return dict(_FALLBACK_FRONTEND_CONFIG)
    else:
        log('INFO', 'server.config', f'Creating default config at {config_path}')
        with open(config_path, 'w') as f:
            json.dump(_FALLBACK_FRONTEND_CONFIG, f, indent=2)
        return dict(_FALLBACK_FRONTEND_CONFIG)


def _backend_to_frontend_config(backend: Dict[str, Any]) -> Dict[str, Any]:
    """Convert backend AgentConfig format to frontend format for WS messages."""
    cfg = dict(backend)
    # Map provider_type → provider
    provider_reverse = {
        "openai": "openai",
        "anthropic": "anthropic",
        "openai_compatible": "local",
    }
    provider_type = cfg.pop("provider_type", None)
    cfg["provider"] = provider_reverse.get(provider_type, "local")
    # Map enabled_tools → tools list
    enabled = cfg.pop("enabled_tools", [])
    cfg["tools"] = [{"name": t, "enabled": True} for t in enabled] if enabled else []
    return cfg


def _default_frontend_config() -> Dict[str, Any]:
    """Return config in frontend format, merged with global defaults."""
    defaults = dict(_FALLBACK_FRONTEND_CONFIG)
    global_defaults = _load_global_defaults()
    defaults.update(global_defaults)
    return _backend_to_frontend_config(defaults)


def _config_to_dict(cfg) -> Dict[str, Any]:
    """Convert an AgentConfig to a plain dict for JSON serialization."""
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(exclude={'api_key'}, exclude_none=True)
    if hasattr(cfg, "dict"):
        return cfg.dict()
    return {k: str(v) for k, v in vars(cfg).items() if not k.startswith("_")}


# ══════════════════════════════════════════════════════════════════════════════
#  Direct execution
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Run the server via `python -m web_ui.backend.server`."""
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD", "false").lower() == "true"

    log('INFO', 'server', f'Starting ThoughtMachine Web UI on {host}:{port}')
    uvicorn.run(
        "web_ui.backend.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )

if __name__ == "__main__":
    main()
