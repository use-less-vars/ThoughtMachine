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
    { "command": "load_session",          "session_id": "...", "limit": 50 (optional), "offset": 0 (optional) }
    { "command": "load_more_messages",  "offset": 50, "limit": 50 }
    { "command": "get_providers" }
    { "command": "save_provider",      "provider": {...} }
    { "command": "delete_provider",    "provider_id": "..." }
    { "command": "get_available_tools" }
    { "command": "delete_session",        "session_id": "..." }
    { "command": "rename_session",        "session_id": "...", "new_name": "..." }
    { "command": "get_open_sessions" }
    { "command": "close_session",          "session_id": "..." (optional) }
    { "command": "new_session" }
    { "command": "set_project",           "project": "/path/to/project" }
    { "command": "security_response",  "request_id": "...", "approved": true/false, "remember": false }
    { "command": "get_workspace_capabilities", "workspace_id": "..." }
    { "command": "bootstrap_workspace",       "workspace_id": "..." }

Server → Client (JSON):
    state_changed       { "type": "state_changed",       "state": "IDLE|RUNNING|PAUSED|WAITING_FOR_USER", "is_running": bool }
    tokens_updated      { "type": "tokens_updated",      "input": int, "output": int }
    context_updated     { "type": "context_updated",     "context_length": int }
    conversation_changed { "type": "conversation_changed", "messages": [...], "total_count": int, "has_more": bool }
    more_messages       { "type": "more_messages",       "messages": [...], "offset": int, "total_count": int, "has_more": bool }
    config_changed      { "type": "config_changed",      "config": {...} }
    status_message      { "type": "status_message",      "text": "..." }
    sessions_list       { "type": "sessions_list",       "sessions": [...] }
    session_saved       { "type": "session_saved",       "session": {...} }
    session_loaded      { "type": "session_loaded",      "session_id": "...", "session_name": "...", "message_count": int, "workspace_id": "..." }
    session_deleted     { "type": "session_deleted",     "session_id": "..." }
    session_renamed     { "type": "session_renamed",     "session_id": "...", "new_name": "..." }
    open_sessions_list  { "type": "open_sessions_list",  "session_ids": ["..."] }
    session_closed      { "type": "session_closed",      "session_id": "..." }
    session_cleared     { "type": "session_cleared" }
    providers_list      { "type": "providers_list",      "providers": [...] }
    provider_saved      { "type": "provider_saved",      "provider": {...} }
    provider_deleted    { "type": "provider_deleted",    "provider_id": "..." }
    tools_list          { "type": "tools_list",          "tools": [...] }
    security_prompt     { "type": "security_prompt",     "request_id": "...", "tool_name": "...", "capabilities": [...], "description": "..." }
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import shutil
import tempfile
import time
import traceback
import threading

from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from agent.logging import log
from contextlib import asynccontextmanager
from session.store import FileSystemSessionStore

# ── Shared session store singleton ────────────────────────────────────────────
# All WebSocket connections and bridges share ONE FileSystemSessionStore so that
# the in-memory list_sessions() cache is coherent across concurrent connections.
_session_store: Optional[FileSystemSessionStore] = None
_session_store_lock = threading.Lock()

def _get_session_store() -> FileSystemSessionStore:
    global _session_store
    if _session_store is None:
        with _session_store_lock:
            if _session_store is None:
                _session_store = FileSystemSessionStore()
    return _session_store


from web_ui.backend.workspace_routes import router as workspace_router
from web_ui.backend.config_routes import router as config_router

# Ensure project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── Monkey-patch websockets library race condition ──────────────────
# websockets.legacy.protocol has a race condition: when send_json() is
# called concurrently from multiple coroutines, two _drain_helper calls
# can race on _drain_waiter, raising:
#   AssertionError: assert waiter is None or waiter.cancelled()
# This patch serialises concurrent drains by awaiting the in-flight
# waiter instead of asserting.  (Upstream fix in websockets >= 14.0.)
try:
    import websockets.legacy.protocol
    _orig = websockets.legacy.protocol.WebSocketCommonProtocol._drain_helper

    async def _patched_drain_helper(self):
        if self.connection_lost_waiter.done():
            raise ConnectionResetError("Connection lost")
        if not self._paused:
            return
        waiter = self._drain_waiter
        if waiter is not None and not waiter.cancelled():
            # Another drain is in progress — share it instead of asserting
            await waiter
            return
        waiter = self.loop.create_future()
        self._drain_waiter = waiter
        await waiter

    websockets.legacy.protocol.WebSocketCommonProtocol._drain_helper = \
        _patched_drain_helper
    log('INFO', 'server',
        'Patched websockets _drain_helper race condition (concurrent drain)')
except ImportError:
    pass  # websockets not installed — nothing to patch

# ── App + lifespan ──────────────────────────────────────────────────────────

# We import bridge lazily inside the lifespan / endpoint to avoid
# circular / early import issues.

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler — registers signal handlers for graceful shutdown."""
    log('INFO', 'server', 'ThoughtMachine Web UI server starting ...')
    # Ensure user ~/.thoughtmachine/ defaults exist before any connection
    try:
        from thoughtmachine.bootstrap import ensure_user_defaults, get_version
        touched = ensure_user_defaults()
        if touched:
            log('INFO', 'server', f'Created initial user defaults: {len(touched)} file(s)')
        log('INFO', 'server', f'ThoughtMachine version {get_version()}')
    except Exception as exc:
        log('WARNING', 'server', f'Could not initialise user defaults: {exc}')

    # ── Startup container integrity check ─────────────────────────────────
    # Scan for any existing agent-exec-* containers and verify they match
    # the current restrictive defaults.  Stale containers from a previous
    # run with permissive settings are stopped and removed.
    try:
        import docker
        from docker_executor import verify_container_integrity
        client = docker.from_env()
        # List all containers with names starting with agent-exec-
        all_containers = client.containers.list(all=True, filters={"name": "agent-exec-"})
        for c in all_containers:
            mounts = c.attrs.get("Mounts", [])
            ws_path = None
            for m in mounts:
                if m.get("Destination") == "/workspace":
                    ws_path = m.get("Source")
                    break
            if ws_path:
                result = verify_container_integrity(ws_path, session_permissions=None)
                if result.get("action_taken") == "removed":
                    log('INFO', 'server',
                        f'Startup integrity: removed stale container '
                        f'{result["container_name"]} for workspace {ws_path}',
                        {"mismatch": result.get("mismatch_reason")})
                elif result.get("action_taken") == "error":
                    log('WARNING', 'server',
                        f'Startup integrity: error checking container '
                        f'{result["container_name"]}: {result.get("mismatch_reason")}')
    except Exception as exc:
        log('DEBUG', 'server', f'Startup container scan skipped: {exc}')

    yield
    log('INFO', 'server', 'Server shutting down.')


# ── Graceful shutdown: save open sessions on Ctrl+C / SIGTERM ───────────────
# When the server terminates (Ctrl+C / SIGINT / SIGTERM), we need to ensure
# open sessions are saved before the process exits.
#
# Approach:
# 1. _session_bridges — dict mapping session_id → WebAgentBridge (warm cache).
# 2. _shutdown_save() — iterates _session_bridges values and saves each one.
# 3. atexit — ensures _shutdown_save runs when the Python process exits
#    normally (including after uvicorn's Ctrl+C handling).
# 4. asyncio.Event — the WebSocket handler checks this event between
#    messages and exits promptly, letting its ``finally`` block run.
#
# Note: atexit handlers do *not* run on SIGKILL or hard crashes, but they
# do run on normal Ctrl+C (SIGINT -> KeyboardInterrupt -> sys.exit).

import signal
import atexit

_session_bridges: Dict[str, Any] = {}  # session_id → WebAgentBridge (warm cache)
_shutdown_event: Any = None  # asyncio.Event, set lazily
_explicitly_closed_sessions: Set[str] = set()  # session_ids closed via close_session command


def _get_shutdown_event() -> Any:
    """Return the global shutdown asyncio.Event, creating it if needed."""
    global _shutdown_event
    if _shutdown_event is None:
        import asyncio
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def _shutdown_save() -> None:
    """Save all cached bridges' open sessions and stop them.

    Called via atexit — runs after uvicorn's event loop exits.
    """
    log('INFO', 'server', f'Shutdown save: {len(_session_bridges)} cached bridge(s)')
    for bridge in list(_session_bridges.values()):
        try:
            if not bridge._cleanly_closed:
                bridge.save_open_session()
                bridge._cleanly_closed = True
        except Exception:
            pass
        try:
            bridge.stop()
        except Exception:
            pass
    _session_bridges.clear()


def _trigger_shutdown() -> None:
    """Called by the signal handler — sets the shutdown event.

    If an asyncio loop is running (uvicorn), sets the event for graceful
    shutdown.  Otherwise, raises ``KeyboardInterrupt`` so the process
    actually stops even outside an event loop (e.g. during tests).
    """
    event = _get_shutdown_event()
    no_loop = False
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(event.set)
                log('INFO', 'server', 'Shutdown signal received — finishing in-flight work...')
                return
        except RuntimeError:
            pass
        event.set()
        no_loop = True
    except Exception:
        pass

    log('INFO', 'server', 'Shutdown signal received — finishing in-flight work...')
    if no_loop:
        # No asyncio loop — likely running outside uvicorn (e.g. tests).
        # The event set above won't be checked by anyone, so we need to
        # actually stop execution.
        raise KeyboardInterrupt()


# Register atexit handler to save sessions on normal exit (Ctrl+C etc.)
atexit.register(_shutdown_save)
# Signal handlers (also set the asyncio event so the WS loop can detect shutdown)
_orig_sigint = signal.signal(signal.SIGINT, lambda sig, frame: _trigger_shutdown())
_orig_sigterm = signal.signal(signal.SIGTERM, lambda sig, frame: _trigger_shutdown())


app = FastAPI(
    title="ThoughtMachine Web UI",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Frontend serving state (set by --serve-frontend) ─────────────────────
# Path to the built frontend dist directory
# ── Frontend dist path: development vs PyInstaller bundle ─────────────
# In development:              web_ui/frontend/dist/
# In PyInstaller one-folder:   <bundle>/frontend_dist/
# In PyInstaller one-file:     sys._MEIPASS/frontend_dist/
_MEIPASS = getattr(sys, "_MEIPASS", None)
if _MEIPASS is not None:
    # PyInstaller one-file: temp extraction dir
    _BUNDLE_DIR = Path(_MEIPASS)
elif getattr(sys, "frozen", False):
    # PyInstaller one-folder: next to the exe
    _BUNDLE_DIR = Path(sys.executable).parent
else:
    # Normal Python: project root (parent of web_ui/)
    _BUNDLE_DIR = Path(__file__).resolve().parent.parent.parent

_FRONTEND_DIST_DEV = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_FRONTEND_DIST_BUNDLE = _BUNDLE_DIR / "frontend_dist"

# Pick the first one that exists
if _FRONTEND_DIST_BUNDLE.exists():
    _FRONTEND_DIST = _FRONTEND_DIST_BUNDLE
elif _FRONTEND_DIST_DEV.exists():
    _FRONTEND_DIST = _FRONTEND_DIST_DEV
else:
    _FRONTEND_DIST = _FRONTEND_DIST_DEV  # fallback (will show build-error page)
# Whether --serve-frontend was requested (set in main())
_SERVE_FRONTEND = False

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
#
#  Query parameters:
#    ?project=<path>  — Absolute path to the project directory. Used to
#                       resolve the correct workspace for this connection.
#                       When omitted, falls back to _project_root (this file's
#                       grandparent directory at import-time).
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, project: Optional[str] = None):
    await ws.accept()
    # If the frontend provides a project query parameter, use it to resolve
    # the workspace instead of the default _project_root.  This allows the
    # web UI to serve sessions for multiple projects from a single server.
    _project_path = project or _project_root
    log('INFO', 'server.ws', f'WebSocket connected: {ws.client} project={_project_path}')

    # Import bridge here (after project root is on sys.path)
    from web_ui.backend.bridge import WebAgentBridge
    from agent.controller import AgentController

    bridge: Optional[WebAgentBridge] = None
    session_store = _get_session_store()

    # Capture the asyncio event loop HERE (inside the async handler)
    # so we can schedule sends from the agent thread later.
    import asyncio
    _loop = asyncio.get_running_loop()

    # Asynchronous event sender — queues events to the WebSocket
    async def send_event(event: Dict[str, Any]) -> None:
        if getattr(ws, '_closed', False):
            return
        try:
            await ws.send_json(event)
        except (RuntimeError, ConnectionError, AssertionError) as exc:
            # Expected during shutdown or websockets race — mark closed
            log('DEBUG', 'server.ws', f'send_event skipped (ws closed): {exc}')
            ws._closed = True
        except Exception as exc:
            log('ERROR', 'server.ws', f'send_event failed: {exc}\n{traceback.format_exc()}')

    # Shutdown guard — set when the event loop is closing
    _shutting_down = False

    # Callback wrapper — called from the bridge's agent thread
    def event_callback(event: Dict[str, Any]) -> None:
        """Called from agent thread.  Schedule send on the asyncio loop."""
        nonlocal _shutting_down
        if _shutting_down:
            return
        try:
            if _loop.is_closed():
                _shutting_down = True
                log('DEBUG', 'server.ws', 'Shutting down: event loop closed, discarding callback event.')
                return
            asyncio.run_coroutine_threadsafe(send_event(event), _loop)
        except RuntimeError as exc:
            if 'Event loop is closed' in str(exc):
                _shutting_down = True
                log('DEBUG', 'server.ws', 'Shutting down: event loop closed, discarding callback event.')
                return
            log('ERROR', 'server.ws', f'event_callback error: {exc}')
            traceback.print_exc()
        except Exception as exc:
            log('ERROR', 'server.ws', f'event_callback error: {exc}')
            traceback.print_exc()

    try:
        while True:
            # Check for shutdown signal between messages
            if _get_shutdown_event().is_set():
                log('INFO', 'server.ws', 'Shutdown event detected — closing WebSocket')
                break
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
                    bridge = WebAgentBridge(session_store=session_store)
                    bridge.set_event_callback(event_callback, key=id(ws))
                    bridge.register()
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

                elif command == "set_default_config":
                    """Save config as the global default.

                    Accepts an optional ``config`` payload (frontend format) so the
                    frontend can send the user's draft directly.  Falls back to
                    ``bridge.get_config()`` when no payload is provided.
                    """
                    try:
                        config_dict = msg.get("config")
                        if config_dict:
                            # Frontend sent the draft — translate to backend format
                            cfg_dict = _translate_frontend_config(config_dict)
                        elif bridge is not None:
                            # Fallback: use bridge's currently applied config
                            cfg_dict = bridge.get_config().model_dump(exclude={'api_key', 'stop_check'}, exclude_none=True)
                        else:
                            await ws.send_json({
                                "type": "default_config_saved",
                                "status": "error",
                                "message": "No config provided and no active session",
                            })
                            continue

                        config_dir = Path.home() / '.thoughtmachine'
                        config_dir.mkdir(parents=True, exist_ok=True)
                        config_path = config_dir / 'agent_config.json'

                        # Windows-safe atomic write with retry
                        _atomic_replace(
                            data=cfg_dict,
                            dst=str(config_path),
                            work_dir=str(config_dir),
                        )

                        log('INFO', 'server.config', f"Default config saved to {config_path}")
                        await ws.send_json({
                            "type": "default_config_saved",
                            "status": "ok",
                            "message": "Default config saved successfully",
                        })
                    except Exception as exc:
                        log('ERROR', 'server.config', f"set_default_config failed: {exc}")
                        await ws.send_json({
                            "type": "default_config_saved",
                            "status": "error",
                            "message": f"Failed to save default config: {exc}",
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
                                "api_key": p.api_key,
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
                            # Preserve existing api_key if incoming value is empty
                            # (prevents accidental overwrite when frontend field is blank)
                            if not provider_data.get('api_key'):
                                existing = manager.get_profile(provider_data['id'])
                                if existing and existing.api_key:
                                    provider_data = dict(provider_data)
                                    provider_data['api_key'] = existing.api_key
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
                                        "api_key": p.api_key,
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
                                        "api_key": p.api_key,
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
                        log('ERROR', 'server.config', f"get_available_tools failed: {exc}\n{traceback.format_exc()}")
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
                    if bridge is None or bridge.session is None:
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

                    # Save current session before switching
                    if bridge is not None and bridge.session is not None:
                        bridge.save_session()

                    # Check if we already have a cached bridge for this session
                    existing = _session_bridges.get(session_id)
                    if existing is not None and existing._controller is not None:
                        # Reuse cached bridge — register the new WS callback but DON'T
                        # call bridge.load_session() (which would reload from disk,
                        # replace self._session, and broadcast to ALL tabs).
                        # Instead, send the live state directly to the NEW WS only.
                        log('INFO', 'server.ws', f'Reusing cached bridge for session {session_id}')
                        bridge = existing
                        bridge.set_event_callback(event_callback, key=id(ws))
                        # Send current state from live bridge data (not from disk)
                        try:
                            page_limit = msg.get("limit", 50)
                            session = bridge._session
                            if session is not None and session.user_history:
                                all_messages = session.user_history
                                total_count = len(all_messages)
                                if page_limit is not None and len(all_messages) > page_limit:
                                    page = bridge._normalize_for_frontend(all_messages[-page_limit:])
                                    has_more = total_count > page_limit
                                else:
                                    page = bridge._normalize_for_frontend(all_messages)
                                    has_more = False
                            else:
                                page = []
                                total_count = 0
                                has_more = False
                            await ws.send_json({
                                "type": "conversation_changed",
                                "messages": page,
                                "total_count": total_count,
                                "has_more": has_more,
                            })
                        except Exception as exc:
                            log('WARNING', 'server.ws', f'Cached bridge state sending failed: {exc} — creating fresh bridge')
                            existing = None

                    if existing is None or existing._controller is None:
                        # Create fresh controller and bridge
                        from agent.controller import AgentController
                        controller = AgentController()
                        bridge = WebAgentBridge(session_store=session_store)
                        bridge.set_event_callback(event_callback, key=id(ws))
                        bridge.register()
                        bridge.set_controller(controller)
                        _session_bridges[session_id] = bridge
                        # Pagination: limit how many messages are sent initially
                        page_limit = msg.get("limit", 50)
                        page_offset = msg.get("offset", 0)
                        bridge.load_session(session_id, limit=page_limit, offset=page_offset)

                    # ── Fallback: resolve workspace_id from project path ──
                    # Old sessions may have been saved without a workspace_id.
                    if not bridge.workspace_id:
                        try:
                            from web_ui.backend.bridge import _resolve_workspace_id
                            resolved = _resolve_workspace_id(_project_path)
                            if resolved:
                                bridge._workspace_id = resolved
                                log('INFO', 'server',
                                    f"Resolved workspace_id from project path for loaded session: {resolved}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"Could not resolve workspace_id from project path: {exc}")

                    # ── Auto-switch project path if session belongs to a different workspace ──
                    # When loading a session from a different project (e.g., Wordle),
                    # _project_path still points to the old project. Update it so that
                    # workers, templates, and subsequent operations use the correct workspace.
                    if bridge.workspace_id:
                        try:
                            from pathlib import Path as _Path
                            import json as _json
                            ws_config_path = _Path("~").expanduser() / ".thoughtmachine" / "workspaces" / bridge.workspace_id / "config.json"
                            if ws_config_path.is_file():
                                ws_data = _json.loads(ws_config_path.read_text(encoding="utf-8"))
                                ws_project_path = ws_data.get("root", "")
                                if ws_project_path and os.path.abspath(ws_project_path) != os.path.abspath(_project_path):
                                    old_path = _project_path
                                    _project_path = ws_project_path
                                    log('INFO', 'server',
                                        f"load_session: auto-switched project path from {old_path} to {_project_path}")
                                    # Sync workspace_path into bridge config so that
                                    # _frontend_config_from_bridge sends the correct path
                                    # to the frontend (instead of falling back to _project_root).
                                    if hasattr(bridge, '_config') and bridge._config is not None:
                                        bridge._config.workspace_path = _project_path
                                    # Re-resolve workspace_id for the new project path
                                    from web_ui.backend.bridge import _resolve_workspace_id
                                    resolved = _resolve_workspace_id(_project_path)
                                    if resolved and resolved != bridge.workspace_id:
                                        bridge._workspace_id = resolved
                                        log('INFO', 'server',
                                            f"load_session: re-resolved workspace_id to {resolved} for {_project_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"load_session: auto-switch project path error: {exc}")

                    await ws.send_json({
                        "type": "session_loaded",
                        "session_id": session_id,
                        "workspace_id": bridge.workspace_id,
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

                elif command == "load_more_messages":
                    offset = msg.get("offset", 50)
                    limit = msg.get("limit", 50)
                    if bridge is None:
                        await ws.send_json({"type": "status_message", "text": "⚠ No active session."})
                        continue
                    try:
                        result = bridge.load_more_messages(offset=offset, limit=limit)
                        if result is None:
                            await ws.send_json({"type": "status_message", "text": "⚠ No session loaded."})
                        else:
                            await ws.send_json(result)
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to load more messages: {exc}",
                        })
                        log("ERROR", "server.config", f"load_more_messages failed: {exc}")

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
                        # Use bridge.rename_session() when available so in-memory
                        # state (_session, _loaded_session) stays in sync with
                        # the renamed session on disk. Fall back to direct store
                        # access when no bridge is active.
                        # Broadcast the new name to ALL open bridges BEFORE responding,
                        # so no auto-save on another tab can race us and persist the old name.
                        from web_ui.backend.bridge import _broadcast_rename
                        _broadcast_rename(session_id, new_name)

                        if bridge is not None and bridge.rename_session(session_id, new_name):
                            await ws.send_json({
                                "type": "session_renamed",
                                "session_id": session_id,
                                "new_name": new_name,
                            })
                            log("INFO", "server.config", f"Renamed session {session_id} → {new_name}")
                        else:
                            # Fallback: no bridge active, or bridge.rename_session returned False
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
                        # Batch-load metadata for all open sessions in a single
                        # directory scan — avoids reading each session file N times.
                        all_meta = session_store.load_sessions_metadata_batch(open_ids)
                        open_sessions = []
                        for sid in open_ids:
                            meta = all_meta.get(sid)
                            if meta:
                                open_sessions.append({
                                    "session_id": meta["session_id"],
                                    "name": meta["name"],
                                    "updated_at": meta["updated_at"],
                                    "message_count": meta["message_count"],
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
                            # Track explicitly closed sessions so the disconnect
                            # handler doesn't re-add them to open_sessions.json
                            resolved_id = session_id or bridge._session_id or (
                                bridge._loaded_session.session_id if bridge._loaded_session else None
                            )
                            if resolved_id:
                                _explicitly_closed_sessions.add(resolved_id)
                            # If session_id was empty, the bridge resolved it internally.
                            # Remove the bridge from _session_bridges by value (since the key
                            # might differ from the empty session_id).
                            for cached_sid, cached_bridge in list(_session_bridges.items()):
                                if cached_bridge is bridge:
                                    cached_bridge.stop()
                                    del _session_bridges[cached_sid]
                                    break
                            else:
                                # Fallback: try pop with the original key
                                cached = _session_bridges.pop(session_id, None)
                                if cached is not None:
                                    cached.stop()
                        else:
                            # No bridge active — clean up open sessions list
                            if session_id:
                                session_store.remove_open_session(session_id)
                            await ws.send_json({"type": "status_message", "text": "Session closed."})
                            # Remove from cache (by key) even without a bridge
                            cached = _session_bridges.pop(session_id, None)
                            if cached is not None:
                                cached.stop()
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
                    # Save current session before switching
                    if bridge is not None and bridge.session is not None:
                        bridge.save_session()
                    # Stop any existing bridge
                    if bridge is not None:
                        bridge.stop()

                    # Create fresh bridge + controller with a new Session.
                    # The bridge sits IDLE with _loaded_session set so that
                    # the first continue_session triggers Case 1 (loaded session)
                    # and calls bridge.start(query) to kick off the agent.
                    from agent.controller import AgentController
                    controller = AgentController()
                    bridge = WebAgentBridge(session_store=session_store)
                    bridge.set_event_callback(event_callback, key=id(ws))
                    bridge.register()
                    bridge.set_controller(controller)

                    # Extract optional workspace_id from the request
                    workspace_id = msg.get("workspace_id") or None
                    if workspace_id:
                        bridge._workspace_id = workspace_id

                    # ── Fallback: resolve workspace_id from the project path ──
                    # The frontend can pass a project query param to select
                    # which workspace to use.  Fall back to _project_root.
                    if not workspace_id:
                        try:
                            from web_ui.backend.bridge import _resolve_workspace_id
                            resolved = _resolve_workspace_id(_project_path)
                            if resolved:
                                workspace_id = resolved
                                bridge._workspace_id = resolved
                                log('INFO', 'server',
                                    f"Resolved workspace_id from project path: {resolved}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"Could not resolve workspace_id from project path: {exc}")

                    # ── Auto-register new workspace if no registration exists ──
                    # When the frontend opens a folder that has never been
                    # registered, there is no workspace_id on disk.  We create
                    # one on the fly so the workspace panel and workers work.
                    if not workspace_id:
                        try:
                            import uuid
                            import json as _json
                            from thoughtmachine.workspace_capabilities import (
                                ensure_workspace_dirs, _workspace_dir,
                            )
                            new_ws_id = uuid.uuid4().hex
                            ws_dir = _workspace_dir(new_ws_id)
                            ws_dir.mkdir(parents=True, exist_ok=True)
                            config_path = ws_dir / "config.json"
                            config_path.write_text(
                                _json.dumps({"root": str(_project_path)}, indent=2),
                                encoding="utf-8",
                            )
                            ensure_workspace_dirs(new_ws_id)
                            workspace_id = new_ws_id
                            bridge._workspace_id = new_ws_id
                            log('INFO', 'server',
                                f"Auto-registered workspace {new_ws_id} for {_project_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"Could not auto-register workspace: {exc}")

                    # Create a new empty session
                    from session.models import Session
                    new_session = Session()
                    new_session.metadata['source'] = 'web_ui'
                    if workspace_id:
                        new_session.workspace_id = workspace_id
                    new_session.ensure_name()

                    # Store as the loaded session so continue_session picks it up
                    bridge._loaded_session = new_session

                    # Cache bridge by the new session ID so reconnects reuse it
                    _session_bridges[new_session.session_id] = bridge
                    # Persist to session store so the SessionTab can load it
                    # via load_session on its own WS connection.
                    session_store.save_session(new_session)

                    await ws.send_json({
                        "type": "session_loaded",
                        "session_id": new_session.session_id,
                        "session_name": new_session.metadata.get('name', ''),
                        "workspace_id": bridge._workspace_id,
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

                elif command == "set_project":
                    """Switch the connection to a different project directory.

                    The Config Panel sends this when the user changes the workspace
                    folder and presses Apply.  The server tears down the current
                    session, registers (or resolves) the workspace for the new path,
                    creates a fresh session, and sends session_loaded back.
                    """
                    new_project = msg.get("project", "")
                    if not new_project:
                        await ws.send_json({
                            "type": "status_message",
                            "text": "⚠ set_project: project path is required",
                        })
                        continue

                    # Validate path exists and is a directory
                    if not os.path.isdir(new_project):
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Project path does not exist or is not a directory: {new_project}",
                        })
                        continue

                    log('INFO', 'server', f"Switching project to {new_project}")

                    # 1. Save current session and stop the bridge
                    if bridge is not None and bridge.session is not None:
                        bridge.save_session()
                    if bridge is not None:
                        bridge.stop()

                    # 2. Update the per-connection project path
                    _project_path = new_project

                    # 3. Create fresh bridge + controller
                    from agent.controller import AgentController
                    controller = AgentController()
                    bridge = WebAgentBridge(session_store=session_store)
                    bridge.set_event_callback(event_callback, key=id(ws))
                    bridge.register()
                    bridge.set_controller(controller)

                    # 4. Resolve or auto-register workspace for the new path
                    workspace_id = None
                    try:
                        from web_ui.backend.bridge import _resolve_workspace_id
                        resolved = _resolve_workspace_id(_project_path)
                        if resolved:
                            workspace_id = resolved
                            bridge._workspace_id = resolved
                            log('INFO', 'server',
                                f"set_project: resolved workspace {resolved} for {_project_path}")
                    except Exception as exc:
                        log('WARNING', 'server',
                            f"set_project: resolve error: {exc}")

                    if not workspace_id:
                        try:
                            import uuid
                            import json as _json
                            from thoughtmachine.workspace_capabilities import (
                                ensure_workspace_dirs, _workspace_dir,
                            )
                            new_ws_id = uuid.uuid4().hex
                            ws_dir = _workspace_dir(new_ws_id)
                            ws_dir.mkdir(parents=True, exist_ok=True)
                            config_path = ws_dir / "config.json"
                            config_path.write_text(
                                _json.dumps({"root": str(_project_path)}, indent=2),
                                encoding="utf-8",
                            )
                            ensure_workspace_dirs(new_ws_id)
                            workspace_id = new_ws_id
                            bridge._workspace_id = new_ws_id
                            log('INFO', 'server',
                                f"set_project: auto-registered workspace {new_ws_id} for {_project_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"set_project: auto-register error: {exc}")

                    # 5. Create a new empty session for the new workspace
                    from session.models import Session
                    new_session = Session()
                    new_session.metadata['source'] = 'web_ui'
                    if workspace_id:
                        new_session.workspace_id = workspace_id
                    new_session.ensure_name()
                    bridge._loaded_session = new_session
                    _session_bridges[new_session.session_id] = bridge
                    session_store.save_session(new_session)

                    # 6. Send session_loaded and state messages
                    await ws.send_json({
                        "type": "session_loaded",
                        "session_id": new_session.session_id,
                        "session_name": new_session.metadata.get('name', ''),
                        "workspace_id": bridge._workspace_id,
                    })
                    await ws.send_json({
                        "type": "state_changed",
                        "state": "IDLE",
                        "is_running": False,
                    })
                    await ws.send_json({
                        "type": "tokens_updated",
                        "input": 0,
                        "output": 0,
                    })
                    await ws.send_json({
                        "type": "context_updated",
                        "context_length": 0,
                    })
                    await ws.send_json({
                        "type": "config_changed",
                        "config": _default_frontend_config(),
                    })
                    await ws.send_json({
                        "type": "status_message",
                        "text": f"✅ Switched to project: {_project_path}",
                    })

                elif command == "security_response":
                    """Handle user response to a security prompt."""
                    request_id = msg.get("request_id", "")
                    approved = msg.get("approved", False)
                    remember = msg.get("remember", False)

                    if not request_id:
                        await ws.send_json({
                            "type": "status_message",
                            "text": "⚠ security_response: request_id is required",
                        })
                        continue

                    try:
                        from thoughtmachine.security import resolve_security_prompt
                        resolve_security_prompt(request_id, approved, remember)
                        log('INFO', 'server.ws',
                            f'Security prompt resolved: request_id={request_id} '
                            f'approved={approved} remember={remember}')
                    except ImportError:
                        log('ERROR', 'server.ws',
                            'security module not available — cannot resolve prompt')
                        await ws.send_json({
                            "type": "status_message",
                            "text": "⚠ Security module not loaded",
                        })
                    except Exception as exc:
                        log('ERROR', 'server.ws',
                            f'Failed to resolve security prompt: {exc}')
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to resolve: {exc}",
                        })

                elif command == "get_workspace_capabilities":
                    workspace_id = msg.get("workspace_id", "")
                    if not workspace_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ workspace_id is required."})
                        continue
                    try:
                        from thoughtmachine.workspace_capabilities import (
                            load_workspace_capabilities,
                        )
                        caps = load_workspace_capabilities(workspace_id)
                        if caps is None:
                            # Return unrestricted defaults when no capabilities file exists
                            from thoughtmachine.workspace_capabilities import (
                                WorkspaceCapabilities,
                            )
                            caps = WorkspaceCapabilities.default()
                        await ws.send_json({
                            "type": "workspace_capabilities",
                            "workspace_id": workspace_id,
                            "capabilities": caps.to_dict(),
                        })
                    except Exception as exc:
                        log("ERROR", "server.ws", f"get_workspace_capabilities failed: {exc}")
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to load capabilities: {exc}",
                        })

                elif command == "bootstrap_workspace":
                    workspace_id = msg.get("workspace_id", "")
                    if not workspace_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ workspace_id is required."})
                        continue
                    try:
                        from thoughtmachine.workspace_capabilities import (
                            ensure_workspace_dirs,
                        )
                        created = ensure_workspace_dirs(workspace_id)
                        await ws.send_json({
                            "type": "workspace_bootstrapped",
                            "workspace_id": workspace_id,
                            "paths_created": created,
                        })
                    except Exception as exc:
                        log("ERROR", "server.ws", f"bootstrap_workspace failed: {exc}")
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to bootstrap workspace: {exc}",
                        })

                elif command == "rebuild_container":
                    workspace = msg.get("workspace", "")
                    if not workspace:
                        await ws.send_json({"type": "rebuild_result", "status": "error", "build_log": "No workspace path provided."})
                    else:
                        from docker_executor import rebuild_container
                        try:
                            result = rebuild_container(workspace)
                            await ws.send_json({"type": "rebuild_result", "status": result.get("status", "unknown"), "build_log": result.get("build_log", "")})
                        except Exception as exc:
                            await ws.send_json({"type": "rebuild_result", "status": "error", "build_log": str(exc)})

                else:
                    await ws.send_json({
                        "type": "status_message",
                        "text": f"⚠ Unknown command: {command}",
                    })
            except Exception as exc:
                log('ERROR', 'server.ws', f'FATAL WebSocket error: {exc}')
                traceback.print_exc()
                try:
                    await ws.send_json({"type": "status_message", "text": f"⚠ Internal error: {exc}"})
                except Exception:
                    pass

    except WebSocketDisconnect:
        log('INFO', 'server.ws', f'WebSocket disconnected: {ws.client}')
        ws._closed = True
    except Exception as exc:
        log('ERROR', 'server.ws', f'WebSocket error: {exc}')
        traceback.print_exc()
        ws._closed = True
    finally:
        # Mark closed so pending send_event calls are silently dropped
        ws._closed = True
        # Unregister this WebSocket's callback so stale connections
        # don't linger (fixes multi-tab / tab-reconnect bug).
        if bridge is not None:
            try:
                bridge.remove_event_callback(id(ws))
            except Exception:
                pass
        # Save open session but DON'T stop the bridge — keep it cached
        # for fast reuse when the frontend reconnects (tab switch,
        # network blip, etc.). The bridge will be stopped either on
        # close_session or during server shutdown.
        if bridge is not None:
            # Check if this session was explicitly closed — if so, skip re-saving
            sid = bridge._session_id or (
                bridge._loaded_session.session_id if bridge._loaded_session else None
            )
            if sid and sid in _explicitly_closed_sessions:
                _explicitly_closed_sessions.discard(sid)
            elif not bridge._cleanly_closed:
                try:
                    bridge.save_open_session()
                except Exception:
                    pass
            bridge.unregister()


# ══════════════════════════════════════════════════════════════════════════════
# ── Register workspace REST router ────────────────────────────────────────
app.include_router(workspace_router)
app.include_router(config_router)


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


@app.post("/api/browse/create")
async def create_directory(body: dict):
    """Create a new directory for the workspace path browser."""
    try:
        parent_path = body.get("parent_path", "")
        dir_name = body.get("name", "")
        if not dir_name:
            return {"success": False, "error": "Directory name is required"}
        new_path = os.path.join(parent_path, dir_name)
        if os.path.exists(new_path):
            return {"success": False, "error": f"Already exists: {dir_name}"}
        os.makedirs(new_path, exist_ok=True)
        return {"success": True, "path": os.path.abspath(new_path)}
    except (OSError, PermissionError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "thoughtmachine-web-ui"}

@app.get("/api/container/integrity")
def container_integrity(workspace: str = "", permissions: str = ""):
    """Return container integrity status for the given workspace.

    Calls ``get_integrity_status()`` which wraps ``verify_container_integrity()``
    to check the existing container against the expected security config.
    If permissions have been tightened since the container was created,
    it is removed so it will be recreated with the new settings.

    Query params:
        workspace: Absolute path to the workspace.
        permissions: Optional JSON-encoded session_permissions dict.
            When omitted, the most restrictive defaults are used.

    Returns:
        dict with keys:
        - status (str): "ok", "mismatch", "removed", or "error"
        - container_name (str | None)
        - desired (dict): {"network": ..., "mode": ...}
        - actual (dict | None): {"network": ..., "mode": ...}
        - mismatch_reason (str | None)
    """
    if not workspace:
        return {"status": "error", "container_name": None,
                "desired": {}, "actual": None,
                "mismatch_reason": "No workspace path provided."}

    from docker_executor import get_integrity_status

    sp = None
    if permissions:
        try:
            import json
            sp = json.loads(permissions)
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        result = get_integrity_status(workspace, sp)
        return result
    except Exception as exc:
        log("ERROR", "server.container_integrity",
            f"Integrity check failed: {exc}")
        return {"status": "error", "container_name": None,
                "desired": {}, "actual": None,
                "mismatch_reason": str(exc)}


@app.get("/api/container/status")
def container_status(workspace: str = ""):
    """Return status of the Docker container for the given workspace path."""
    if not workspace:
        return {"status": "unavailable", "capabilities": {}, "build_log": ""}

    # Import lazily to avoid circular imports at module level
    from docker_executor import get_container_status

    try:
        result = get_container_status(workspace)
        return result
    except Exception as exc:
        log("ERROR", "server.container_status", f"Container status failed: {exc}")
        return {"status": "error", "capabilities": {}, "build_log": str(exc)}

@app.get("/")
async def root():
    if _SERVE_FRONTEND:
        index = _FRONTEND_DIST / "index.html"
        if index.exists():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<html><body><h1>Frontend not built</h1>"
            "<p>Run <code>cd web_ui/frontend && npm run build</code> first, "
            "or use <code>--serve-frontend</code> to auto-build.</p></body></html>",
            status_code=503,
        )
    # Dev mode — backend only serves API. Frontend is on the Vite dev server.
    return RedirectResponse(url="http://localhost:5173")





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

    # ── Whitelist validation for session_permissions ────────────────────
    # Delegated to coerce_session_permissions() in thoughtmachine/security.py
    # which rejects any unknown category or invalid level to prevent a
    # compromised (or malformed) WebSocket message from injecting dangerous
    # values such as filesystem="full", container=True, network="write".
    from thoughtmachine.security import coerce_session_permissions
    cfg["session_permissions"] = coerce_session_permissions(
        cfg.get("session_permissions", {}),
    )

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
    # Check if API key is configured before stripping it
    api_key = getattr(cfg, 'api_key', '') or ''
    if not api_key:
        import os
        api_key = os.getenv('OPENAI_API_KEY') or os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_COMPATIBLE_API_KEY') or ''
    api_key_configured = bool(api_key)
    raw = _config_to_dict(cfg)
    result = _backend_to_frontend_config(raw)
    result['api_key_configured'] = api_key_configured
    return result



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
    "api_key_configured": False,
    "token_monitor_warning_threshold": 60000,
    "token_monitor_critical_threshold": 75000,
    "turn_monitor_enabled": True,
    "enable_logging": True,
    "log_dir": "./logs",
    "log_level": "INFO",
    "enable_file_logging": True,
    "jsonl_format": True,
    "log_categories": ["SESSION", "LLM", "TOOLS"],
    "max_file_size_mb": 10,
    "max_backup_files": 5,
    "workspace_path": _project_root,
    # Auto-detected from server.py location at module load time (see line 102)
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
    "session_permissions": {
        "container": False,
        "network": "banned",
        "filesystem": "read",
        "system": "read",
        "git": "read",
        "execution": "banned",
    },
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
    # If the key is missing, leave tools alone (frontend defaults apply).
    # If it's an explicit empty list, all tools are disabled.
    if "enabled_tools" in cfg:
        enabled = cfg.pop("enabled_tools")
        cfg["tools"] = [{"name": t, "enabled": True} for t in enabled]
    # Ensure workspace_path is always present.
    # The bridge config may have workspace_path=None (default), and
    # _config_to_dict uses exclude_none=True which strips it. Without
    # this fallback the frontend never receives workspace_path, the
    # ContainerPanel sends an empty workspace param, and the backend
    # returns "Container status unavailable".
    cfg.setdefault("workspace_path", _project_root)
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
        return cfg.model_dump(exclude={'api_key', 'stop_check'}, exclude_none=True)
    if hasattr(cfg, "dict"):
        return cfg.dict()
    return {k: str(v) for k, v in vars(cfg).items() if not k.startswith("_")}


def _atomic_replace(data: dict, dst: str, work_dir: str, retries: int = 3) -> None:
    """Atomically write *data* as JSON to *dst*, with Windows-safe retries.

    Writes to a temporary file in *work_dir*, then replaces the destination.
    Retries up to *retries* times on OSError (covers Windows sharing
    violations from antivirus / file locks).  Falls back to ``shutil.move``
    if ``os.replace`` fails after all retries.
    """
    for attempt in range(1, retries + 2):
        with tempfile.NamedTemporaryFile(
            mode='w', delete=False,
            dir=work_dir,
            suffix='.tmp',
            prefix='agent_config_',
        ) as tmp:
            json.dump(data, tmp, indent=2, default=str)
            tmp.flush()
            tmp_path = tmp.name

        try:
            os.replace(tmp_path, dst)
            return  # success
        except OSError:
            # Clean up orphaned temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            if attempt > retries:
                # Final fallback: try shutil.move (more resilient on Windows)
                try:
                    shutil.move(tmp_path, dst)
                    log('WARNING', 'server.config',
                        f'_atomic_replace: os.replace failed after {retries} retries, '
                        f'used shutil.move as fallback')
                    return
                except OSError as exc:
                    raise exc

            # Back off before retrying
            time.sleep(0.2 * attempt)


# ══════════════════════════════════════════════════════════════════════════════
#  Direct execution
# ══════════════════════════════════════════════════════════════════════════════

def _build_frontend() -> bool:
    """Run `npm run build` in the frontend directory.

    Returns True if the build succeeded, False otherwise.
    """
    frontend_dir = _FRONTEND_DIST.parent
    if not (frontend_dir / "package.json").exists():
        log('ERROR', 'server', f'Frontend package.json not found at {frontend_dir}')
        return False

    log('INFO', 'server', f'Building frontend in {frontend_dir}...')
    npm_cmd = os.environ.get('TM_NPM_CMD') or 'npm'
    try:
        result = subprocess.run(
            [npm_cmd, "run", "build"],
            cwd=str(frontend_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            log('ERROR', 'server', f'Frontend build failed:\n{result.stderr}')
            return False
        log('INFO', 'server', 'Frontend build succeeded.')
        return True
    except FileNotFoundError:
        log('ERROR', 'server', 'npm not found — is Node.js installed?')
        return False
    except subprocess.TimeoutExpired:
        log('ERROR', 'server', 'Frontend build timed out (120s).')
        return False
    except Exception as exc:
        log('ERROR', 'server', f'Frontend build error: {exc}')
        return False


def _setup_frontend_serving() -> bool:
    """Build frontend (if needed) and mount static file serving.

    Must be called AFTER all API routes are registered.
    Returns True if frontend is ready to serve.
    """
    global _SERVE_FRONTEND
    _SERVE_FRONTEND = True

    # Always rebuild to prevent stale dist/ from serving old code
    # Vite/Rollup use incremental caching, so subsequent builds are fast.
    dist = _FRONTEND_DIST
    if not _build_frontend():
        log('WARNING', 'server',
            'Frontend build failed — will show build-error page on /')
        return False

    # Mount static files — this catches all non-API paths for assets
    # FastAPI routes are checked before mounts, so API routes still work.
    if dist.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(dist), html=True),
            name="frontend",
        )
        log('INFO', 'server', f'Serving frontend from {dist}')
        return True

    return False


def main():
    """Run the server via `python -m web_ui.backend.server`.

    Usage:
        python -m web_ui.backend.server
        python -m web_ui.backend.server --serve-frontend
        python -m web_ui.backend.server --serve-frontend --host 127.0.0.1 --port 8080

    The frontend can pass ?project=<path> as a URL query param to select
    which workspace/project to use (each tab's WebSocket sends it).
    """
    import uvicorn

    parser = argparse.ArgumentParser(description="ThoughtMachine Web UI Server")
    parser.add_argument(
        "--serve-frontend",
        action="store_true",
        help="Build and serve the React frontend alongside the API",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port to bind to (default: 8000)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=os.environ.get("RELOAD", "false").lower() == "true",
        help="Enable auto-reload (default: from RELOAD env var)",
    )

    args = parser.parse_args()

    if args.serve_frontend:
        _setup_frontend_serving()

    log('INFO', 'server',
        f'Starting ThoughtMachine Web UI on {args.host}:{args.port}')
    # Pass the app OBJECT directly — using the string form would
    # double-import this module, creating a second copy where _SERVE_FRONTEND
    # is still False and the StaticFiles mount is missing.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )

if __name__ == "__main__":
    main()
