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
    { "command": "list_sessions" }
    { "command": "save_session",          "name": "..." (optional) }
    { "command": "load_session",          "session_id": "..." }
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
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager

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
    print("🧠 ThoughtMachine Web UI server starting ...")
    yield
    print("🧠 Server shutting down.")

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
    print(f"⚡ WebSocket connected: {ws.client}")

    # Import bridge here (after project root is on sys.path)
    from web_ui.backend.bridge import WebAgentBridge
    from agent.controller import AgentController

    bridge: Optional[WebAgentBridge] = None

    # Capture the asyncio event loop HERE (inside the async handler)
    # so we can schedule sends from the agent thread later.
    import asyncio
    _loop = asyncio.get_running_loop()

    # Asynchronous event sender — queues events to the WebSocket
    async def send_event(event: Dict[str, Any]) -> None:
        try:
            await ws.send_json(event)
        except Exception as exc:
            print(f"⚠ send_event failed: {exc}")

    # Callback wrapper — called from the bridge's agent thread
    def event_callback(event: Dict[str, Any]) -> None:
        """Called from agent thread.  Schedule send on the asyncio loop."""
        try:
            asyncio.run_coroutine_threadsafe(send_event(event), _loop)
        except Exception as exc:
            print(f"🔥 event_callback error: {exc}")
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
            print(f"  ▶ Command: {command}")

            # ── Handle commands ─────────────────────────────────────────────
            try:
                if command == "start_session":
                    query = msg.get("query", "")
                    config_dict = msg.get("config", {})
                    if not query.strip():
                        await ws.send_json({"type": "status_message", "text": "⚠ Query cannot be empty."})
                        continue

                    # Translate frontend config format → AgentConfig format
                    # The preset provides defaults; frontend fields become overrides.
                    config_dict = _translate_frontend_config(config_dict)

                    controller = AgentController()
                    bridge = WebAgentBridge(event_callback=event_callback)
                    bridge.set_controller(controller)
                    try:
                        bridge.start(query, config_dict, preset_name="Default")
                    except RuntimeError as exc:
                        # Controller may be stuck from a prior session; stop and retry
                        await ws.send_json({"type": "status_message", "text": f"⚠ Controller busy — resetting: {exc}"})
                        controller.stop()
                        bridge.start(query, config_dict, preset_name="Default")
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to start: {exc}",
                        })
                        continue
                    await ws.send_json({"type": "status_message", "text": "Session started."})

                elif command == "continue_session":
                    if bridge is None or not bridge.is_running:
                        await ws.send_json({"type": "status_message", "text": "No active session — start a new one."})
                        continue
                    query = msg.get("query", "")
                    if not query.strip():
                        await ws.send_json({"type": "status_message", "text": "⚠ Query cannot be empty."})
                        continue
                    try:
                        bridge.continue_session(query)
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to continue: {exc}",
                        })

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
                    if bridge is not None:
                        cfg = bridge.get_config()
                        if cfg is not None:
                            await ws.send_json({
                                "type": "config_changed",
                                "config": _config_to_dict(cfg),
                            })
                    else:
                        await ws.send_json({
                            "type": "config_changed",
                            "config": _default_frontend_config(),
                        })

                elif command == "get_conversation":
                    if bridge is not None:
                        conv = bridge.get_conversation()
                        if conv is not None:
                            messages = [
                                {"role": _map_role(m), "content": _map_content(m)}
                                for m in conv
                            ]
                            await ws.send_json({
                                "type": "conversation_changed",
                                "messages": messages,
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

                elif command == "list_sessions":
                    if bridge is None:
                        await ws.send_json({"type": "status_message", "text": "⚠ No bridge available."})
                        continue
                    try:
                        sessions = bridge.list_sessions()
                        await ws.send_json({
                            "type": "sessions_list",
                            "sessions": sessions,
                        })
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to list sessions: {exc}",
                        })

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
                            "is_running": bridge.is_running,
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
                    if bridge is None:
                        await ws.send_json({"type": "status_message", "text": "⚠ No bridge available."})
                        continue
                    try:
                        bridge.delete_session(session_id)
                        await ws.send_json({
                            "type": "session_deleted",
                            "session_id": session_id,
                        })
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to delete session: {exc}",
                        })

                elif command == "rename_session":
                    session_id = msg.get("session_id", "")
                    new_name = msg.get("new_name", "")
                    if not session_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ session_id is required."})
                        continue
                    if bridge is None:
                        await ws.send_json({"type": "status_message", "text": "⚠ No bridge available."})
                        continue
                    try:
                        bridge.rename_session(session_id, new_name)
                        await ws.send_json({
                            "type": "session_renamed",
                            "session_id": session_id,
                            "new_name": new_name,
                        })
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to rename session: {exc}",
                        })

                elif command == "get_open_sessions":
                    if bridge is None:
                        await ws.send_json({"type": "status_message", "text": "⚠ No bridge available."})
                        continue
                    try:
                        session_ids = bridge.get_open_sessions()
                        await ws.send_json({
                            "type": "open_sessions_list",
                            "session_ids": session_ids,
                        })
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to get open sessions: {exc}",
                        })

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
                    # Clear any loaded session and stop current bridge
                    if bridge is not None:
                        bridge.stop()
                        await ws.send_json({
                            "type": "state_changed",
                            "state": "IDLE",
                            "is_running": False,
                        })
                    bridge = None
                    await ws.send_json({
                        "type": "session_cleared",
                    })
                    await ws.send_json({"type": "status_message", "text": "Ready for a new session."})

                else:
                    await ws.send_json({
                        "type": "status_message",
                        "text": f"⚠ Unknown command: {command}",
                    })
            except Exception as exc:
                print(f"🔥 FATAL WebSocket error: {exc}")
                import traceback
                traceback.print_exc()
                try:
                    await ws.send_json({"type": "status_message", "text": f"⚠ Internal error: {exc}"})
                except Exception:
                    pass

    except WebSocketDisconnect:
        print(f"⚡ WebSocket disconnected: {ws.client}")
    except Exception as exc:
        print(f"⚠ WebSocket error: {exc}")
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

    # Translate tools list to enabled_tools
    tools = cfg.pop("tools", None)
    if isinstance(tools, list):
        cfg["enabled_tools"] = [
            t["name"] for t in tools
            if isinstance(t, dict) and t.get("enabled", False)
        ]

    # Remove any keys that start with _
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}

    return cfg


def _frontend_config_from_bridge(bridge) -> Dict[str, Any]:
    """Convert bridge's AgentConfig back to frontend config format."""
    from web_ui.backend.bridge import WebAgentBridge
    cfg = bridge.get_config()
    if cfg is None:
        return _default_frontend_config()
    raw = _config_to_dict(cfg)
    # Reverse provider mapping
    provider_reverse = {
        "openai": "openai",
        "anthropic": "anthropic",
        "openai_compatible": "local",
    }
    raw["provider"] = provider_reverse.get(raw.get("provider_type", ""), "local")
    # Convert enabled_tools back to tools list
    enabled = raw.pop("enabled_tools", [])
    raw["tools"] = [{"name": t, "enabled": True} for t in enabled]
    return raw


def _default_frontend_config() -> Dict[str, Any]:
    """Return config in frontend format."""
    return {
        "temperature": 0.7,
        "max_turns": 20,
        "provider": "openai",
        "tools": [
            {"name": "bash", "enabled": True},
            {"name": "file_read", "enabled": False},
        ],
    }


def _config_to_dict(cfg) -> Dict[str, Any]:
    """Convert an AgentConfig to a plain dict for JSON serialization."""
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump()
    if hasattr(cfg, "dict"):
        return cfg.dict()
    return {k: str(v) for k, v in vars(cfg).items() if not k.startswith("_")}

def _map_role(msg: Dict[str, Any]) -> str:
    role = msg.get("role", "system")
    mapping = {
        "user": "user",
        "assistant": "assistant",
        "system": "system",
        "tool": "tool_result",
    }
    return mapping.get(role, role)

def _map_content(msg: Dict[str, Any]) -> str:
    content = msg.get("content", "")
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "tool_use":
                    texts.append(f"[Tool: {part.get('name', '?')}]")
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    if not isinstance(content, str):
        return str(content)
    return content


# ══════════════════════════════════════════════════════════════════════════════
#  Direct execution
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Run the server via `python -m web_ui.backend.server`."""
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD", "false").lower() == "true"

    print(f"🧠 Starting ThoughtMachine Web UI on {host}:{port}")
    uvicorn.run(
        "web_ui.backend.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )

if __name__ == "__main__":
    main()
