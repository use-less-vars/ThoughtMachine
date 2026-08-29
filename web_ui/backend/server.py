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
    session_loaded      { "type": "session_loaded",      "session_id": "...", "session_name": "...", "message_count": int, "workspace_id": "...", "workspace_path": "...", "config": {...} }
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
    worker_spawned      { "type": "worker:worker_spawned",      "worker_name": "...", "timestamp": "...", "data": {...} }
    worker_status       { "type": "worker:worker_status",       "worker_name": "...", "timestamp": "...", "data": {...} }
    worker_completed    { "type": "worker:worker_completed",    "worker_name": "...", "timestamp": "...", "data": {...} }
    worker_error        { "type": "worker:worker_error",        "worker_name": "...", "timestamp": "...", "data": {...} }
"""

from __future__ import annotations

# ---- Stdio guard -------------------------------------------------------
# If this process is launched with file descriptors 1/2 closed (e.g. by a
# headless launcher or service wrapper), CPython sets sys.stdout / sys.stderr
# to None.  Any print(), warnings emission, or stdlib-logging lastResort
# fallback then crashes with "'NoneType' object has no attribute 'write'"
# (this is the DockerCodeRunner crash).  Redirect them to devnull so the
# whole process is stdio-safe in any launch environment.  This MUST run
# before any import that could emit warnings (e.g. fastapi, websockets,
# docker SDK) or the crash would strike during module import.
import sys as _sys
import os as _os
if _sys.stdout is None:
    _sys.stdout = open(_os.devnull, 'w')
if _sys.stderr is None:
    _sys.stderr = open(_os.devnull, 'w')
del _sys, _os

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
from datetime import datetime, timezone

from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import Body, FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from agent.logging import log
from contextlib import asynccontextmanager
from session.store import FileSystemSessionStore
from session.session_registry import SessionRegistry
from agent.config.presets import get_tools_for_mode
from agent.config.session_config import SessionConfig
from agent.config.config_manager import save_config_defaults
from thoughtmachine.workspace_registry import WorkspaceRegistry

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
from web_ui.backend.onboarding_routes import router as onboarding_router
from web_ui.backend.config_routes import router as config_router
from web_ui.backend.logging_routes import router as logging_router
from web_ui.backend.health_routes import router as health_router
from web_ui.backend.session_routes import router as session_router
from web_ui.backend.prompt_routes import router as prompt_router

# ── ConfigManager (facade for all config operations) ────────────────────────
from web_ui.backend.config_manager import (
    ConfigManager,
    GLOBAL_DEFAULT_KEYS,
    load_global_defaults,
)

# Singleton instance used throughout the WebSocket handlers.
config_manager = ConfigManager()


def save_global_defaults(cfg_dict: Dict[str, Any]) -> Path:
    """Persist a config payload as the global default (Path A of config ownership).

    Extracts only the six session-level keys allowed in the global-default
    layer (``GLOBAL_DEFAULT_KEYS``: provider_id, model, base_url, temperature,
    max_turns, system_prompt), merges them into the existing
    ``~/.thoughtmachine/user/defaults.json`` (preserving unrelated keys), and
    writes the merged dict back through the canonical writer
    ``agent.config.config_manager.save_config_defaults(..., global_scope=True)``.

    Returns the path of the file written.  This replaces the legacy write to
    ``~/.thoughtmachine/agent_config.json``, which is now read-compat only and
    never written by the server.  See ``docs/architecture/config_ownership.md``.
    """
    subset = {k: v for k, v in cfg_dict.items() if k in GLOBAL_DEFAULT_KEYS}
    existing = load_global_defaults()
    merged = dict(existing)
    merged.update(subset)
    workspace_id = str(cfg_dict.get("workspace_id") or "")
    return save_config_defaults(merged, workspace_id, global_scope=True)


# Ensure project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── Server revision (git HEAD at import time) ─────────────────────────────────────────────────────────
# Used by the /health endpoint and the startup log for deployment
# verification.  Falls back to "unknown" when git is unavailable (e.g. a
# packaged build) or the checkout has no commits yet.

def _get_server_revision() -> str:
    """Return the git revision this server was built from, or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_project_root,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if result.returncode == 0:
            rev = result.stdout.strip()
            if rev:
                return rev
    except Exception:
        pass
    return "unknown"


_SERVER_REVISION = _get_server_revision()


def _session_config_dict(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Filter a (translated) frontend config dict to SessionConfig fields.

    SessionConfig is strict (``extra='forbid'``), but the frontend payload may
    carry auxiliary keys (``api_key_configured``, ``top_p``, ``max_tokens``,
    ``stop_check``, ...) that are consumed elsewhere.  Drop them here so
    ``SessionConfig(**cfg)`` never raises on unknown keys.
    """
    return {k: v for k, v in cfg.items() if k in SessionConfig.model_fields}

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

# ── Migration helper ───────────────────────────────────────────────────────────

def _migrate_old_workspaces() -> int:
    """Migrate old-style workspace directories (with ``config.json`` but no
    ``workspace_identity.json``) into the :class:`WorkspaceRegistry`.

    Scans ``~/.thoughtmachine/workspaces/`` for subdirectories.  For each
    directory that contains a ``config.json`` with a ``root`` key but does
    **not** contain a ``workspace_identity.json``, it calls
    ``WorkspaceRegistry.get_default().register_by_root(root)`` to add it to
    the registry.

    Returns the number of workspaces migrated.
    """
    count = 0
    base_dir = Path.home() / ".thoughtmachine" / "workspaces"
    if not base_dir.is_dir():
        return 0

    registry = WorkspaceRegistry.get_default()
    for ws_dir in sorted(base_dir.iterdir()):
        if not ws_dir.is_dir():
            continue
        config_path = ws_dir / "config.json"
        identity_path = ws_dir / "workspace_identity.json"
        if config_path.exists() and not identity_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                root = cfg.get("root")
                if not root:
                    continue
                entry = registry.register_by_root(str(root))
                log('INFO', 'server',
                    f"_migrate_old_workspaces: registered {entry.id} for {root} "
                    f"(was legacy dir {ws_dir.name})")
                count += 1
            except Exception as exc:
                log('WARNING', 'server',
                    f"_migrate_old_workspaces: error processing {ws_dir.name}: {exc}")
    return count


# ── App + lifespan ──────────────────────────────────────────────────────────

# We import bridge lazily inside the lifespan / endpoint to avoid
# circular / early import issues.


def _sweep_orphan_resource_containers():
    """Startup sweep: remove resource containers of unregistered workspaces.

    Best-effort and unit-testable; NEVER raises — a broken docker daemon or a
    failing sweep must not prevent the server from starting.  Removes hidden
    git resource containers (``thoughtmachine.resource`` label) whose
    ``thoughtmachine.workspace_id`` is no longer registered, then prunes the
    shared resource image once no resource container remains anywhere.
    """
    try:
        from infra.resource_container_manager import (
            prune_unreferenced_resource_images,
            sweep_stale_resource_containers,
        )

        try:
            registry = WorkspaceRegistry.get_default()
            ids = [e.id for e in registry.list_workspaces()]
        except Exception as exc:
            log('WARNING', 'server',
                f'Startup sweep: could not list registered workspaces: {exc}')
            ids = []

        result = sweep_stale_resource_containers(ids)
        detail = result.get("detail") or ""
        log('INFO', 'server',
            f'Startup sweep: removed {result.get("removed", 0)} orphan '
            f'resource container(s), kept {result.get("skipped_in_use", 0)} '
            f'in use' + (f' — {detail}' if detail else ''))

        prune_result = prune_unreferenced_resource_images()
        prune_detail = prune_result.get("detail") or ""
        log('INFO', 'server',
            f'Startup sweep: pruned images '
            f'{prune_result.get("removed_images", [])}, '
            f'{prune_result.get("remaining_containers", 0)} resource '
            f'container(s) remaining' + (f' — {prune_detail}' if prune_detail else ''))
    except Exception as exc:
        log('WARNING', 'server', f'Startup resource sweep skipped: {exc}')


# Idle TTL (seconds) after which an EXITED generic workspace container is
# removed at startup. Tune via env override if needed.
_EXITED_CONTAINER_SWEEP_MAX_AGE_S = int(
    os.environ.get('THOUGHTMACHINE_EXITED_CONTAINER_MAX_AGE_S', '3600')
)


def _sweep_exited_workspace_containers():
    """Startup sweep: remove EXITED generic workspace containers.

    Two concerns, both best-effort and NEVER raising:
    - idle/TTL: exited containers of registered workspaces that have been idle
      past ``_EXITED_CONTAINER_SWEEP_MAX_AGE_S`` are removed;
    - orphan GC: exited containers of UNREGISTERED workspaces are removed too.
    Resource containers (``thoughtmachine.resource`` label) are excluded.
    If the workspace registry cannot be read the sweep degrades to TTL-only
    (``ids=None``) so a failing/empty registry can never trigger a wipe.
    """
    try:
        from infra.container_manager import sweep_exited_workspace_containers

        try:
            registry = WorkspaceRegistry.get_default()
            ids = [e.id for e in registry.list_workspaces()]
        except Exception as exc:
            log('WARNING', 'server',
                f'Startup sweep: could not list registered workspaces: {exc}')
            ids = None  # TTL-only; never orphan-wipe on registry failure

        result = sweep_exited_workspace_containers(
            registered_workspace_ids=ids,
            max_age_s=_EXITED_CONTAINER_SWEEP_MAX_AGE_S,
        )
        detail = result.get("detail") or ""
        log('INFO', 'server',
            f'Startup sweep: removed {result.get("removed", 0)} exited '
            f'workspace container(s), skipped {result.get("skipped", 0)}'
            + (f' — {detail}' if detail else ''))

        # Registry companion: drop swept names so the in-memory container
        # registry does not retain entries for removed containers. The
        # disabled registry no-ops here, so this is safe on every start.
        removed = result.get("removed_containers") or []
        if removed:
            try:
                from infra.registry_wiring import get_active_registry
                registry = get_active_registry(None)
                for name in removed:
                    try:
                        registry.unregister(name)
                    except Exception:
                        pass
            except Exception as exc:
                log('WARNING', 'server',
                    f'Startup sweep: registry companion unregister failed: {exc}')
    except Exception as exc:
        log('WARNING', 'server', f'Startup workspace sweep skipped: {exc}')


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler — registers signal handlers for graceful shutdown."""
    log('INFO', 'server', 'ThoughtMachine Web UI server starting ...')
    log('INFO', 'server', f'Starting ThoughtMachine server — revision {_SERVER_REVISION}')

    # Stdlib logging console layer (human-readable lifecycle lines)
    try:
        from agent.logging.console import configure_console_logging
        configure_console_logging()
    except Exception as exc:
        log('WARNING', 'server', f'Console logging setup skipped: {exc}')
    # Start EventLogger to persist all events to disk
    try:
        from agent.logging.event_logger import EventLogger
        event_logger = EventLogger()
        event_logger.start()
        app.state.event_logger = event_logger
        log('INFO', 'server', f'EventLogger started: {event_logger.file_path}')
    except Exception as exc:
        log('WARNING', 'server', f'EventLogger startup skipped: {exc}')
        app.state.event_logger = None

    # Ensure user ~/.thoughtmachine/ defaults exist before any connection
    try:
        from thoughtmachine.bootstrap import ensure_user_defaults, get_version
        touched = ensure_user_defaults()
        if touched:
            log('INFO', 'server', f'Created initial user defaults: {len(touched)} file(s)')
        log('INFO', 'server', f'ThoughtMachine version {get_version()}')
    except Exception as exc:
        log('WARNING', 'server', f'Could not initialise user defaults: {exc}')

    # ── Migrate old-style workspaces ──
    try:
        migrated = _migrate_old_workspaces()
        if migrated:
            log('INFO', 'server',
                f'Migrated {migrated} old workspace(s) to the registry.')
    except Exception as exc:
        log('WARNING', 'server', f'Workspace migration error: {exc}')

    # ── Auto-register project root as a default workspace ──────────────
    try:
        from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

        registry = WorkspaceRegistry.get_default()
        entry = registry.register_by_root(str(_project_root))
        ensure_workspace_dirs(entry.id)
        log('INFO', 'server',
            f'Auto-registered default workspace: {entry.id} at {_project_root}')
    except Exception as exc:
        log('WARNING', 'server', f'Could not auto-register default workspace: {exc}')

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

    # ── Startup orphan resource-container sweep ──────────────────────────
    # Remove hidden git resource containers (thoughtmachine.resource label)
    # whose workspace is no longer registered, then prune the shared resource
    # image once no resource container remains anywhere. Best-effort: a
    # failing sweep must never break startup.
    _sweep_orphan_resource_containers()

    # ── Startup exited-workspace-container sweep ────────────────────────
    # Remove EXITED generic workspace containers (thoughtmachine.workspace_id
    # label) idle past the TTL; containers of unregistered workspaces are
    # treated as orphans and removed too. Resource containers are excluded.
    # Best-effort: a failing sweep must never break startup.
    _sweep_exited_workspace_containers()

    yield
    log('INFO', 'server', 'Server shutting down.')
    # Stop EventLogger
    try:
        el = getattr(app.state, 'event_logger', None)
        if el is not None:
            el.stop()
            log('INFO', 'server', 'EventLogger stopped.')
    except Exception as exc:
        log('WARNING', 'server', f'EventLogger shutdown error: {exc}')


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
            log('DEBUG', 'server.ws', f'send_event: type={event.get("type","?")!r} state={event.get("state",event.get("text",""))[:60]}')
            await ws.send_json(event)
        except (RuntimeError, ConnectionError, AssertionError, WebSocketDisconnect) as exc:
            # Expected during shutdown, websockets race, or client disconnect — mark closed
            log('WARNING', 'server.ws', f'send_event skipped (ws closed): {exc} | event_type={event.get("type","?")}')
            ws._closed = True
        except Exception as exc:
            log('ERROR', 'server.ws', f'send_event failed: {exc}\n{traceback.format_exc()}')

    # Shutdown guard — set when the event loop is closing
    _shutting_down = False

    # Callback wrapper — called from the bridge's agent thread
    def event_callback(event: Dict[str, Any]) -> None:
        """Called from agent thread.  Schedule send on the asyncio loop."""
        nonlocal _shutting_down
        log('INFO', 'server.ws', f'event_callback ENTRY: type={event.get("type","?")!r}')
        if _shutting_down:
            log('DEBUG', 'server.ws', f'event_callback: discarding (shutting down) type={event.get("type","?")}')
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
                    config_dict = config_manager.translate_frontend_config(config_dict)
                    config_dict = _session_config_dict(config_dict)

                    # Always create a fresh bridge + controller for start_session.
                    # Stop any existing bridge first to prevent resource leaks.
                    if bridge is not None:
                        bridge.stop()
                    controller = AgentController()
                    bridge = WebAgentBridge(session_store=session_store)
                    bridge.set_event_callback(event_callback, key=id(ws))
                    bridge.register()
                    bridge.set_controller(controller)

                    log('INFO', 'server.ws',
                        f'start_session: bridge+controller created, calling start() | '
                        f'query={query[:80]!r}...')

                    try:
                        bridge.start(query, SessionConfig(**config_dict))
                    except RuntimeError as exc:
                        # Controller may be stuck from a prior session; stop and retry
                        log('WARNING', 'server.ws', f'start_session: controller busy, retrying: {exc}')
                        await ws.send_json({"type": "status_message", "text": f"⚠ Controller busy — resetting: {exc}"})
                        bridge.stop()
                        bridge.start(query, SessionConfig(**config_dict))
                    except Exception as exc:
                        log('ERROR', 'server.ws', f'start_session: failed: {exc}\n{traceback.format_exc()}')
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

                    # ── Log full bridge state at entry ──
                    log('INFO', 'server.ws',
                        f'continue_session ENTRY: query={query[:80]!r}... | '
                        f'bridge={bridge is not None} | '
                        f'_loaded_session={bridge._loaded_session.session_id if bridge and bridge._loaded_session else None} | '
                        f'agent_is_running={bridge.agent_is_running if bridge else False} | '
                        f'_controller={bridge._controller is not None if bridge else False} | '
                        f'_controller.is_running={bridge._controller.is_running if bridge and bridge._controller else False}')

                    # Case 1: Bridge has a loaded session (first query for a new session)
                    # Only start if the agent is not already running.
                    if bridge is not None and bridge._loaded_session is not None and not bridge.agent_is_running:
                        log('INFO', 'server.ws', f'continue_session: loaded session exists — starting bridge with session {bridge._loaded_session.session_id}')
                        try:
                            # Pass the frontend config if available (for first query)
                            config_dict = msg.get("config", {})
                            if config_dict:
                                config_dict = config_manager.translate_frontend_config(config_dict)
                                config_dict = _session_config_dict(config_dict)
                                # 🛡️ Preserve mode from loaded session if frontend config doesn't specify it
                                # Otherwise the frontend config (which may omit mode) would override
                                # the correct mode loaded from session metadata with None → 'agent' default.
                                if not config_dict.get('mode') and bridge._session_config and bridge._session_config.mode:
                                    config_dict['mode'] = bridge._session_config.mode
                                    log('INFO', 'server.ws',
                                        f'continue_session: injected mode={bridge._session_config.mode} from loaded session config')
                                sc = SessionConfig(**config_dict)
                                bridge.start(query, sc)
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
                        log('INFO', 'server.ws',
                            f'continue_session: CASE 2 - bridge is running, calling bridge.continue_session() | '
                            f'query={query[:80]!r}...')
                        try:
                            config_dict = msg.get("config", {})
                            if config_dict:
                                config_dict = config_manager.translate_frontend_config(config_dict)
                            bridge.continue_session(query, config_dict)
                            log('INFO', 'server.ws',
                                f'continue_session: CASE 2 - bridge.continue_session() returned OK')
                        except Exception as exc:
                            log('ERROR', 'server.ws',
                                f'continue_session: CASE 2 - Exception: {exc}\n{traceback.format_exc()}')
                            await ws.send_json({
                                "type": "status_message",
                                "text": f"⚠ Failed to continue: {exc}",
                            })
                        continue

                    # Case 3: Nothing to continue
                    log('WARNING', 'server.ws',
                        f'continue_session: CASE 3 - no active session! | '
                        f'bridge={bridge is not None} | '
                        f'_loaded_session={bridge._loaded_session.session_id if bridge and bridge._loaded_session else None} | '
                        f'agent_is_running={bridge.agent_is_running if bridge else False}')
                    await ws.send_json({"type": "status_message", "text": "No active session — start a new one."})

                elif command == "pause_session":
                    if bridge is not None:
                        bridge.pause()
                        await ws.send_json({"type": "status_message", "text": "⏸ Pausing…"})

                elif command == "resume_session":
                    if bridge is not None:
                        bridge.resume()
                        await ws.send_json({"type": "status_message", "text": "▶ Resumed."})

                elif command == "stop_session":
                    if bridge is not None:
                        bridge.stop()
                        await ws.send_json({"type": "status_message", "text": "⏹ Stopped."})

                elif command == "get_config":
                    # frontend_config_from_bridge handles bridge=None and cfg=None gracefully
                    fe_config = config_manager.get_frontend_config(bridge)
                    settings = config_manager.extract_settings(fe_config) if isinstance(fe_config, dict) else {}
                    permissions = config_manager.resolve_effective_permissions(bridge._session_config) if bridge._session_config else {}
                    await ws.send_json({
                        "type": "config_changed",
                        "config": fe_config,
                        "settings": settings,
                        "permissions": permissions,
                        "merged_config": fe_config,
                        "effective_config": config_manager.get_effective_config(
                            bridge,
                            workspace_id=bridge._workspace_id if bridge else None,
                            workspace_path=bridge._workspace_path if bridge else None,
                        ),
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

                    # Detect workspace_path change — the frontend no longer sends
                    # a separate set_project command; apply_config handles the full
                    # project switch internally to avoid race conditions.
                    #
                    # FIX6: baseline the comparison on the SESSION's own workspace,
                    # not _project_path. The frontend echoes the session's
                    # workspace_path in every apply_config payload (ConfigPanel
                    # getSafeDraft), while _project_path defaults to the server
                    # root because the tab frontend never sends ?project= — so
                    # comparing against _project_path would treat EVERY apply on a
                    # non-default-workspace session as a workspace switch and
                    # spawn a new session id. Only a genuine difference from the
                    # session's own workspace should trigger the switch.
                    raw_workspace_path = config.get("workspace_path", "") or ""
                    new_workspace_path = os.path.normpath(raw_workspace_path) if raw_workspace_path else ""
                    session_ws = None
                    if bridge is not None:
                        _session_obj = bridge.session  # property: _session or _loaded_session (bridge.py L864-867)
                        if _session_obj is not None:
                            session_ws = (_session_obj.metadata or {}).get('agent_config', {}).get('workspace_path') or None
                        if session_ws is None:
                            session_ws = bridge._workspace_path or None
                    current_path = os.path.normpath(session_ws or _project_path or "")
                    workspace_changed = bool(new_workspace_path) and new_workspace_path != current_path

                    if workspace_changed:
                        log('INFO', 'server.config',
                            "apply_config: workspace_changed=True — switching project workspace")
                        # ── Full project switch ────────────────────────────────────────────
                        # The user changed the workspace folder. Save and stop the current
                        # bridge, resolve the new workspace, then update the EXISTING
                        # session's workspace_id so the conversation is preserved (no new
                        # session created, no session_loaded sent to frontend).

                        # 0. Grab reference to the existing session BEFORE stopping the bridge
                        existing_session = None
                        if bridge is not None:
                            existing_session = bridge.session  # may be None if never started
                            if existing_session is None:
                                existing_session = bridge._loaded_session

                        # 1. Save current session and stop the bridge.
                        #    A failing save/stop must not abort the whole workspace
                        #    switch — log and proceed with a fresh bridge.
                        try:
                            if bridge is not None and bridge.session is not None:
                                bridge.save_session()
                            if bridge is not None:
                                bridge.stop()
                        except Exception as exc:
                            log('ERROR', 'server.ws',
                                f"apply_config: save/stop of previous bridge failed: {exc}")
                        finally:
                            # Drop the old bridge from the cache so the new bridge
                            # created below is the only owner of this session —
                            # otherwise a later load_session could resurrect a
                            # stopped bridge.
                            if bridge is not None:
                                for _sid, _br in list(_session_bridges.items()):
                                    if _br is bridge:
                                        del _session_bridges[_sid]
                                        break

                        # 2. Update the per-connection project path
                        _project_path = new_workspace_path

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
                                bridge._workspace_path = _project_path
                                log('INFO', 'server',
                                    f"apply_config: resolved workspace {resolved} for {_project_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"apply_config: resolve error: {exc}")

                        if not workspace_id:
                            try:
                                from thoughtmachine.workspace_capabilities import ensure_workspace_dirs
                                entry = WorkspaceRegistry.get_default().register_by_root(str(_project_path))
                                workspace_id = entry.id
                                bridge._workspace_id = entry.id
                                bridge._workspace_path = _project_path
                                ensure_workspace_dirs(entry.id)
                                log('INFO', 'server',
                                    f"apply_config: registered workspace {entry.id} for {_project_path}")
                            except Exception as exc:
                                log('WARNING', 'server',
                                    f"apply_config: auto-register error: {exc}")

                        # 5. Decide session strategy based on whether the existing session
                        #    has an actual conversation or not.
                        #
                        #    - **Existing session with conversation**: create a NEW session,
                        #      assign the new workspace, and send session_loaded to the frontend
                        #      so it opens a fresh tab. The old session+tab stays untouched.
                        #
                        #    - **Existing session with NO conversation** (empty/fresh tab):
                        #      reuse the same tab; the session's workspace_id is immutable once
                        #      set, so the workspace switch is applied via the workspace-scoped
                        #      save below — no new tab created.
                        #
                        #    - **No existing session**: create a fresh one.
                        #
                        #    All session creation goes through the bridge's SessionManager
                        #    (mirrors the REST /api/session/create path and set_project) so the
                        #    new session gets the full persisted config.
                        try:
                            if existing_session is not None and _session_has_conversation(existing_session):
                                # Session has real conversation — create new session for new workspace
                                # (opens a new tab on the frontend via session_loaded)
                                session_id, _ = bridge._session_manager.create_session(
                                    mode="custom", workspace_path=_project_path
                                )
                                # Reload the persisted session so we can layer workspace
                                # metadata on top (create_session does not handle workspace_id).
                                new_session = session_store.load_session(session_id, workspace_id=None)
                                if workspace_id and new_session:
                                    new_session.workspace_id = workspace_id
                                # Wire the reloaded session into the bridge (mirrors
                                # bridge.create_session) and re-save so the workspace_id
                                # lands on disk in the workspace-scoped location.
                                bridge._session = new_session
                                bridge._loaded_session = new_session
                                bridge._history_version = new_session.conversation_version
                                sc = bridge._session_manager.extract_session_config(new_session)
                                if sc is not None:
                                    bridge._session_config = sc
                                session_store.save_session(new_session, workspace_id=workspace_id)
                                session_store.add_open_session(new_session.session_id)
                                _session_bridges[new_session.session_id] = bridge
                                log('INFO', 'server',
                                    f"apply_config: created new session {new_session.session_id} "
                                    f"for workspace {workspace_id} (existing session {existing_session.session_id} "
                                    f"had conversation, preserved intact)")

                                # Send session_loaded to frontend so it opens a new tab
                                await ws.send_json({
                                    "type": "session_loaded",
                                    "session_id": new_session.session_id,
                                    "session_name": new_session.metadata.get('name', ''),
                                    "message_count": 0,
                                    "workspace_id": workspace_id or '',
                                    "workspace_path": _project_path,
                                    # Intentional replacement (workspace switch): the frontend
                                    # adopts this session_loaded silently (no stale-session banner)
                                    # and rebinds the tab to the new session id.
                                    "replacement": True,
                                    # Fix 4a: embed the config the user just submitted so the
                                    # chat UI renders from the first event; the config_changed
                                    # sent below (step 7) carries the canonical merged config.
                                    "config": msg.get("config"),
                                })
                            elif existing_session is not None:
                                # No conversation (empty/fresh tab) — reuse the same tab. The
                                # session's workspace_id is immutable once persisted, so the
                                # workspace switch is applied via the workspace-scoped save below.
                                bridge._loaded_session = existing_session
                                _session_bridges[existing_session.session_id] = bridge
                                session_store.save_session(existing_session, workspace_id=workspace_id)
                                log('INFO', 'server',
                                    f"apply_config: updated existing session {existing_session.session_id} "
                                    f"to workspace {workspace_id} (no conversation — kept same tab)")
                            else:
                                # No prior session — create one (rare: first config on a new conn)
                                session_id, _ = bridge._session_manager.create_session(
                                    mode="custom", workspace_path=_project_path
                                )
                                new_session = session_store.load_session(session_id, workspace_id=None)
                                if workspace_id and new_session:
                                    new_session.workspace_id = workspace_id
                                bridge._session = new_session
                                bridge._loaded_session = new_session
                                bridge._history_version = new_session.conversation_version
                                sc = bridge._session_manager.extract_session_config(new_session)
                                if sc is not None:
                                    bridge._session_config = sc
                                session_store.save_session(new_session, workspace_id=workspace_id)
                                session_store.add_open_session(new_session.session_id)
                                _session_bridges[new_session.session_id] = bridge
                                log('INFO', 'server',
                                    f"apply_config: created new session {new_session.session_id} "
                                    f"for workspace {workspace_id} (no prior session)")

                                # Mirror site 1: send session_loaded so the frontend opens the
                                # freshly created session (same payload as the conversation branch).
                                await ws.send_json({
                                    "type": "session_loaded",
                                    "session_id": new_session.session_id,
                                    "session_name": new_session.metadata.get('name', ''),
                                    "message_count": 0,
                                    "workspace_id": workspace_id or '',
                                    "workspace_path": _project_path,
                                    # Intentional replacement (workspace switch): the frontend
                                    # adopts this session_loaded silently and rebinds the tab.
                                    "replacement": True,
                                    "config": msg.get("config"),
                                })
                        except Exception as exc:
                            # The old bridge is already stopped at this point; surface the
                            # error and continue the command loop (the Round C try/except
                            # below is deliberately NOT entered — the session strategy
                            # failure left the bridge in an unknown state, but the
                            # connection stays alive for subsequent commands).
                            log('ERROR', 'server.ws',
                                f"apply_config: session strategy failed: {exc}")
                            try:
                                await ws.send_json({
                                    "type": "status_message",
                                    "text": f"⚠ apply_config: session strategy failed: {exc}",
                                })
                            except Exception:
                                pass
                            continue

                        try:
                            # 6. Now apply the config to the NEW bridge
                            config = config_manager.translate_frontend_config(config)
                            result = bridge.apply_config(config)

                            # 7. Send config_changed + status message.
                            #    session_loaded was already sent above (step 5) if a new session was
                            #    created for a conversation-bearing session. For empty sessions that
                            #    were updated in-place, no session_loaded is needed since the tab
                            #    is reused.
                            if isinstance(result, dict) and "config" in result:
                                # Success — result includes config, settings, permissions, merged_config
                                await ws.send_json({
                                    "type": "config_changed",
                                    **result,
                                })
                                log('INFO', 'server.config',
                                    f"Config applied after project switch to {_project_path}")
                            else:
                                # Config apply failed — still send a config
                                # from the bridge so the frontend doesn't hang
                                # with stale state or wrong workspace_path.
                                fe_config = config_manager.get_frontend_config(bridge)
                                settings = config_manager.extract_settings(fe_config) if isinstance(fe_config, dict) else {}
                                permissions = config_manager.resolve_effective_permissions(bridge._session_config) if bridge._session_config else {}
                                await ws.send_json({
                                    "type": "config_changed",
                                    "config": fe_config,
                                    "settings": settings,
                                    "permissions": permissions,
                                    "merged_config": fe_config,
                                    "effective_config": config_manager.get_effective_config(
                                        bridge,
                                        workspace_id=bridge._workspace_id if bridge else None,
                                        workspace_path=bridge._workspace_path if bridge else None,
                                    ),
                                })
                                err_msg = result.get('error', 'unknown error') if isinstance(result, dict) else 'unknown error'
                                await ws.send_json({
                                    "type": "status_message",
                                    "text": f"⚠ Config apply had issues: {err_msg}",
                                })

                            await ws.send_json({
                                "type": "status_message",
                                "text": f"✅ Switched to project: {_project_path}",
                            })
                        except Exception as exc:
                            # Do NOT let a failed apply kill the WS handler — the old
                            # bridge is already stopped at this point, so surface the
                            # error to the frontend and let the loop continue.
                            log('ERROR', 'server.ws',
                                f"apply_config failed in load_session: {exc}")
                            session_id = bridge._session_id or (
                                bridge._loaded_session.session_id if bridge._loaded_session else None
                            )
                            try:
                                await ws.send_json({
                                    "type": "error",
                                    "session_id": session_id,
                                    "message": "Failed to apply config",
                                })
                            except Exception:
                                pass


                    else:
                        # ── Normal config apply (no workspace change) ──────────────
                        if bridge is None:
                            await ws.send_json({
                                "type": "status_message",
                                "text": "⚠ No active session to configure",
                            })
                            continue

                        # Ensure session config is initialised before applying
                        if bridge._session_config is None:
                            from agent.config.presets import get_tools_for_mode
                            _mode = config.get("mode", "agent") if isinstance(config, dict) else "agent"
                            bridge._session_config = SessionConfig(
                                mode=_mode,
                                max_turns=100,
                                session_permissions={},
                                enabled_tools=list(get_tools_for_mode(_mode)),
                            )

                        # Translate frontend tools list → backend enabled_tools, then apply.
                        # If the controller is busy (agent mid-turn) the config is QUEUED
                        # on the bridge and applied automatically once the controller
                        # becomes idle — the frontend is ACKed with config_queued and
                        # receives the config_changed later (deferred broadcast).
                        config = config_manager.translate_frontend_config(config)
                        outcome = bridge.apply_config_queued(config)

                        if isinstance(outcome, dict) and outcome.get("status") == "queued":
                            await ws.send_json({
                                "type": "config_queued",
                                "status": "queued",
                            })
                            log('INFO', 'server.config',
                                "Controller busy — config queued (config_queued sent)")
                            continue

                        result = outcome
                        if isinstance(result, dict) and "config" in result:
                            # Success — result includes config, settings, permissions, merged_config
                            await ws.send_json({
                                "type": "config_changed",
                                **result,
                            })
                            log('INFO', 'server.config', "Config applied and persisted via apply_config")
                        else:
                            err_msg = result.get('error', 'unknown error') if isinstance(result, dict) else 'unknown error'
                            await ws.send_json({
                                "type": "status_message",
                                "text": f"⚠ Failed to apply config: {err_msg}",
                            })
                            await ws.send_json({
                                "type": "config_apply_failed",
                                "text": f"⚠ Failed to apply config: {err_msg}",
                            })

                elif command == "set_default_config":
                    """Save config as the global default.

                    Accepts an optional ``config`` payload (frontend format) so the
                    frontend can send the user's draft directly.  Falls back to
                    ``bridge.get_config()`` when no payload is provided.

                    Persists ONLY the ``GLOBAL_DEFAULT_KEYS`` subset into
                    ``~/.thoughtmachine/user/defaults.json`` (see
                    ``docs/architecture/config_ownership.md``) — the legacy
                    ``~/.thoughtmachine/agent_config.json`` write was removed;
                    that file is now read-compat only.
                    """
                    try:
                        config_dict = msg.get("config")
                        if config_dict:
                            # Frontend sent the draft — translate to backend format
                            cfg_dict = config_manager.translate_frontend_config(config_dict)
                        elif bridge is not None:
                            # Fallback: bridge's applied config, else full resolved chain
                            cfg_dict = bridge.get_config() or config_manager.resolve_full_config(
                                workspace_id=getattr(bridge, "workspace_id", None),
                                session_id=getattr(bridge, "_session_id", None)
                                or (
                                    bridge._loaded_session.session_id
                                    if getattr(bridge, "_loaded_session", None) else None
                                ),
                                provider_id=(
                                    bridge._session_config.provider_id
                                    if getattr(bridge, "_session_config", None) else None
                                ),
                            )
                        else:
                            await ws.send_json({
                                "type": "default_config_saved",
                                "status": "error",
                                "message": "No config provided and no active session",
                            })
                            continue

                        saved_path = save_global_defaults(cfg_dict)

                        log('INFO', 'server.config',
                            f"Default config saved to {saved_path} (global defaults)")
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
                    # F2: attach the current session id so the reply routes to the
                    # right tab/store key (provider dropdown). Non-null source:
                    # bridge._session_id, falling back to the loaded session
                    # (load_session / create_session set _loaded_session but not
                    # _session_id). Mirrors the canonical pattern used at Round C.
                    provider_session_id = None
                    if bridge is not None:
                        provider_session_id = bridge._session_id or (
                            bridge._loaded_session.session_id if bridge._loaded_session else None
                        )
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
                            "session_id": provider_session_id,
                            "providers": safe_profiles,
                        })
                        log('INFO', 'server.config', f"Returned {len(safe_profiles)} provider profiles")
                    except Exception as exc:
                        log('ERROR', 'server.config', f"get_providers failed: {exc}")
                        await ws.send_json({
                            "type": "providers_list",
                            "session_id": provider_session_id,
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
                    # Return list of available tool definitions, filtered by mode if provided
                    try:
                        mode = msg.get("mode", "custom")
                        from tools import SIMPLIFIED_TOOL_CLASSES
                        from agent.config.presets import get_tools_for_mode
                        if mode != "custom":
                            mode_tool_names = set(get_tools_for_mode(mode))
                        else:
                            mode_tool_names = None
                        tool_defs = []
                        for cls in SIMPLIFIED_TOOL_CLASSES:
                            if mode_tool_names is None or cls.__name__ in mode_tool_names:
                                tool_defs.append({
                                    "name": cls.__name__,
                                    "description": (cls.__doc__ or "").strip(),
                                })
                        await ws.send_json({
                            "type": "tools_list",
                            "tools": tool_defs,
                        })
                        log('INFO', 'server.config', f"Returned {len(tool_defs)} available tools for mode={mode}")
                    except Exception as exc:
                        log('ERROR', 'server.config', f"get_available_tools failed: {exc}\n{traceback.format_exc()}")
                        await ws.send_json({
                            "type": "tools_list",
                            "tools": [],
                        })

                elif command == "list_sessions":
                    try:
                        registry = SessionRegistry.get_default()
                        all_sessions = registry.get_all()
                        sessions = list(all_sessions.values())
                        # Fall back to disk scan if registry is empty
                        if not sessions:
                            registry.rebuild_from_disk()
                            all_sessions = registry.get_all()
                            sessions = list(all_sessions.values())
                        await ws.send_json({
                            "type": "sessions_list",
                            "sessions": sessions,
                        })
                        log("INFO", "server.config", f"Listed {len(sessions)} sessions via registry")
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
                        # Register in global session registry
                        registry = SessionRegistry.get_default()
                        registry.register(
                            session_id=session.session_id,
                            workspace_id=session.workspace_id or "",
                            name=session.metadata.get('name', 'Untitled'),
                            mode=session.mode,
                        )
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
                        # NOTE: do NOT set _bridge_loaded_session here — the cached
                        # bridge's session_loaded broadcast predates this tab's
                        # callback (registered only just now), so the fallback below
                        # must send the session_loaded payload to THIS websocket
                        # directly (and report load_error if the cached bridge holds
                        # no session).
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

                    # _bridge_loaded_session means "this tab's callback already received
                    # the session_loaded broadcast for this load".
                    #   - Fresh path: bridge.load_session() broadcasts session_loaded to
                    #     ALL registered callbacks (including this tab's) and returns
                    #     True on success — the fallback below is then skipped.
                    #   - Cached path: the reuse branch above did NOT call
                    #     bridge.load_session(); the live-state broadcast from a previous
                    #     connection predates this tab's callback, so it stays False and
                    #     the fallback below sends session_loaded to THIS websocket.
                    _bridge_loaded_session = False

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
                        # load_session returns False (no broadcast) when the id is dead —
                        # fall through to the fallback, which reports load_error instead
                        # of pretending the session loaded.
                        _bridge_loaded_session = bool(
                            bridge.load_session(session_id, limit=page_limit, offset=page_offset)
                        )

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
                                    # frontend_config_from_bridge sends the correct path
                                    # to the frontend (instead of falling back to _project_root).
                                    bridge._workspace_path = _project_path
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

                    # ── Ensure workspace_path is set on bridge config ──
                    # The auto-switch block above looks for workspace config.json on disk,
                    # but that file may not exist (only capabilities.json is guaranteed).
                    # Fall back to the workspace registry which always has root_path.
                    if bridge.workspace_id and not bridge._workspace_path:
                        try:
                            entry = WorkspaceRegistry.get_default().get_workspace(bridge.workspace_id)
                            if entry and entry.root_path:
                                root_path = entry.root_path
                                bridge._workspace_path = root_path
                                log('INFO', 'server',
                                    f"load_session: set workspace_path from registry: {root_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"load_session: could not resolve workspace root from registry: {exc}")

                    # Fix 4a: compute the frontend config once so session_loaded carries it
                    # (chat UI renders from the first event) and config_changed below
                    # reuses the exact same value.
                    try:
                        fe_config = config_manager.get_frontend_config(bridge)
                    except Exception:
                        fe_config = None

                    if not _bridge_loaded_session:
                        _loaded_meta = bridge._session or bridge._loaded_session
                        if _loaded_meta is None:
                            # Load failure (dead session id, e.g. backend restart):
                            # report it explicitly so the frontend can show a recovery
                            # banner instead of rendering a phantom loaded session.
                            await ws.send_json({
                                "type": "session_loaded",
                                "session_id": session_id,
                                "session_name": '',
                                "load_error": True,
                                "workspace_id": bridge.workspace_id,
                                "workspace_path": bridge._workspace_path or '',
                                "is_running": False,
                                "config": fe_config,
                            })
                        else:
                            await ws.send_json({
                                "type": "session_loaded",
                                "session_id": session_id,
                                "session_name": _loaded_meta.metadata.get('name', ''),
                                "workspace_id": bridge.workspace_id,
                                "workspace_path": bridge._workspace_path or '',
                                "is_running": bridge._controller.is_busy if bridge._controller else False,
                                "config": fe_config,
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
                        # (fe_config computed above — Fix 4a)
                        settings = config_manager.extract_settings(fe_config) if isinstance(fe_config, dict) else {}
                        permissions = config_manager.resolve_effective_permissions(bridge._session_config) if bridge._session_config else {}
                        await ws.send_json({
                            "type": "config_changed",
                            "config": fe_config,
                            "settings": settings,
                            "permissions": permissions,
                            "merged_config": fe_config,
                            "effective_config": config_manager.get_effective_config(
                                bridge,
                                workspace_id=bridge._workspace_id if bridge else None,
                                workspace_path=bridge._workspace_path if bridge else None,
                            ),
                        })
                        await ws.send_json({"type": "status_message", "text": f"Session {session_id} loaded. Click Run to continue."})
                        # Register/update in global session registry
                        registry = SessionRegistry.get_default()
                        registry.register(
                            session_id=loaded.session_id,
                            workspace_id=loaded.workspace_id or "",
                            name=loaded.metadata.get('name', 'Untitled'),
                            mode=loaded.mode,
                        )
                        registry.set_open(loaded.session_id, is_open=True)
                    else:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Session {session_id} could not be loaded.",
                        })

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
                    workspace_id = msg.get("workspace_id")
                    if not session_id:
                        await ws.send_json({"type": "status_message", "text": "⚠ session_id is required."})
                        continue
                    try:
                        session_store.delete_session(session_id, workspace_id=workspace_id)
                        session_store.remove_open_session(session_id)
                        # Remove from global session registry
                        registry = SessionRegistry.get_default()
                        registry.remove(session_id)
                        # Remove from bridge cache so _shutdown_save doesn't re-save it
                        cached_bridge = _session_bridges.pop(session_id, None)
                        if cached_bridge is not None:
                            cached_bridge._cleanly_closed = True
                            cached_bridge.stop()
                        # Track as explicitly closed so WS disconnect handler doesn't re-save it
                        _explicitly_closed_sessions.add(session_id)
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
                        from web_ui.backend.event_forwarder import EventForwarder
                        EventForwarder.broadcast_rename(session_id, new_name)

                        if bridge is not None and bridge.rename_session(session_id, new_name):
                            # Broadcast session_renamed to ALL tabs of this session via the
                            # shared bridge's forwarder (each tab's WS is registered as a
                            # callback keyed by ws id — see the load_session reuse path).
                            # The requesting socket is one of them, so this doubles as its
                            # success ack — no separate reply is needed.
                            bridge._forwarder.broadcast(session_id, "session_renamed", {
                                "session_id": session_id,
                                "new_name": new_name,
                            })
                            log("INFO", "server.config", f"Renamed session {session_id} → {new_name}")
                        else:
                            # Fallback: no bridge active, or bridge.rename_session returned False
                            ws_id_for_load = bridge.workspace_id if bridge else None
                            session = session_store.load_session(session_id, workspace_id=ws_id_for_load)
                            if session is None:
                                await ws.send_json({"type": "status_message", "text": f"⚠ Session not found: {session_id}"})
                                continue
                            session.metadata['name'] = new_name
                            session_store.save_session(session, workspace_id=session.workspace_id)
                            # Broadcast to all tabs via the shared session bridge when one
                            # exists; otherwise fall back to acking only the requesting socket.
                            rename_bridge = bridge if bridge is not None else _session_bridges.get(session_id)
                            if rename_bridge is not None:
                                rename_bridge._forwarder.broadcast(session_id, "session_renamed", {
                                    "session_id": session_id,
                                    "new_name": new_name,
                                })
                            else:
                                await ws.send_json({
                                    "type": "session_renamed",
                                    "session_id": session_id,
                                    "new_name": new_name,
                                })
                            log("INFO", "server.config", f"Renamed session {session_id} → {new_name}")
                        # Update name in global session registry
                        registry = SessionRegistry.get_default()
                        entry = registry.get(session_id)
                        if entry:
                            registry.register(
                                session_id=session_id,
                                workspace_id=entry.get('workspace_id', ''),
                                name=new_name,
                                mode=entry.get('mode', 'agent'),
                            )
                    except Exception as exc:
                        await ws.send_json({
                            "type": "status_message",
                            "text": f"⚠ Failed to rename session: {exc}",
                        })
                        log("ERROR", "server.config", f"rename_session failed: {exc}")

                elif command == "get_open_sessions":
                    try:
                        open_ids = session_store.get_open_sessions()
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
                        # Mark session as closed in global registry
                        if session_id:
                            registry = SessionRegistry.get_default()
                            registry.set_open(session_id, is_open=False)
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
                        try:
                            from web_ui.backend.bridge import _validate_workspace_id
                            workspace_id = _validate_workspace_id(workspace_id)
                        except ValueError as exc:
                            log('WARNING', 'server',
                                f"new_session: invalid workspace_id rejected: {exc}")
                            await ws.send_json({
                                "type": "status_message",
                                "text": f"⚠ Invalid workspace_id: {exc}",
                            })
                            continue
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
                            from thoughtmachine.workspace_capabilities import ensure_workspace_dirs
                            entry = WorkspaceRegistry.get_default().register_by_root(str(_project_path))
                            workspace_id = entry.id
                            bridge._workspace_id = entry.id
                            ensure_workspace_dirs(entry.id)
                            log('INFO', 'server',
                                f"Auto-registered workspace {entry.id} for {_project_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"Could not auto-register workspace: {exc}")

                    # Ensure workspace directories are bootstrapped for this workspace
                    if workspace_id:
                        try:
                            from thoughtmachine.workspace_capabilities import ensure_workspace_dirs
                            ensure_workspace_dirs(workspace_id)
                        except Exception:
                            pass

                    # Create a new empty session via SessionManager
                    # Use mode from the frontend payload if provided, fall back to 'custom'
                    mode = msg.get('mode', 'custom')
                    session_id, frontend_config = bridge.create_session(mode=mode, workspace_id=workspace_id)
                    new_session = bridge._session
                    if workspace_id and new_session:
                        new_session.workspace_id = workspace_id
                        bridge._workspace_id = workspace_id

                    # ── Set workspace_path on bridge config from registry ──
                    if workspace_id and not bridge._workspace_path:
                        try:
                            entry = WorkspaceRegistry.get_default().get_workspace(workspace_id)
                            if entry and entry.root_path:
                                root_path = entry.root_path
                                bridge._workspace_path = root_path
                                log('INFO', 'server',
                                    f"new_session: set workspace_path from registry: {root_path}")

                                # ── Persist workspace_path in session metadata for reload ──
                                if new_session and 'agent_config' not in new_session.metadata:
                                    new_session.metadata['agent_config'] = {}
                                if new_session and isinstance(new_session.metadata['agent_config'], dict):
                                    new_session.metadata['agent_config']['workspace_path'] = root_path
                                    log('INFO', 'server',
                                        f"new_session: persisted workspace_path in session metadata: {root_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"new_session: could not resolve workspace root from registry: {exc}")

                    log('INFO', 'server.ws',
                        f'new_session: bridge created, session_id={session_id}, '
                        f'bridge._session set')

                    # Cache bridge by the new session ID so reconnects reuse it
                    _session_bridges[session_id] = bridge

                    await ws.send_json({
                        "type": "session_loaded",
                        "session_id": new_session.session_id,
                        "session_name": new_session.metadata.get('name', ''),
                        "workspace_id": bridge._workspace_id,
                        "workspace_path": bridge._workspace_path or '',
                        "is_running": bridge._controller.is_busy if bridge._controller else False,
                        "config": frontend_config if isinstance(frontend_config, dict) else None,
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
                    # Send config from bridge so frontend gets workspace path etc.
                    fe_config = config_manager.get_frontend_config(bridge)
                    settings = config_manager.extract_settings(fe_config) if isinstance(fe_config, dict) else {}
                    permissions = config_manager.resolve_effective_permissions(bridge._session_config) if bridge._session_config else {}
                    await ws.send_json({
                        "type": "config_changed",
                        "config": fe_config,
                        "settings": settings,
                        "permissions": permissions,
                        "merged_config": fe_config,
                        "effective_config": config_manager.get_effective_config(
                            bridge,
                            workspace_id=bridge._workspace_id if bridge else None,
                            workspace_path=bridge._workspace_path if bridge else None,
                        ),
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
                            from thoughtmachine.workspace_capabilities import ensure_workspace_dirs
                            entry = WorkspaceRegistry.get_default().register_by_root(str(_project_path))
                            workspace_id = entry.id
                            bridge._workspace_id = entry.id
                            ensure_workspace_dirs(entry.id)
                            log('INFO', 'server',
                                f"set_project: registered workspace {entry.id} for {_project_path}")
                        except Exception as exc:
                            log('WARNING', 'server',
                                f"set_project: auto-register error: {exc}")

                    # 5. Create a new empty session for the new workspace via SessionManager
                    # The bridge already owns a SessionManager built from the same
                    # session_store (mirrors the REST /api/session/create pattern).
                    session_id, _ = bridge._session_manager.create_session(
                        mode="custom", workspace_path=_project_path
                    )

                    # Reload the persisted session so we can layer workspace metadata
                    # on top (create_session does not handle workspace_id itself).
                    new_session = session_store.load_session(session_id, workspace_id=None)
                    if workspace_id and new_session:
                        new_session.workspace_id = workspace_id

                    # Wire the reloaded session into the bridge (mirrors bridge.create_session)
                    # and re-save so the workspace_id lands on disk in the
                    # workspace-scoped location.
                    bridge._session = new_session
                    bridge._loaded_session = new_session
                    bridge._history_version = new_session.conversation_version
                    sc = bridge._session_manager.extract_session_config(new_session)
                    if sc is not None:
                        bridge._session_config = sc
                    session_store.save_session(new_session, workspace_id=workspace_id)
                    session_store.add_open_session(new_session.session_id)
                    _session_bridges[new_session.session_id] = bridge

                    # 6. Send session_loaded and state messages
                    # Fix 4a: embed config (bridge._session_config is already set above, so
                    # get_frontend_config is canonical) so the chat UI renders immediately.
                    try:
                        fe_config = config_manager.get_frontend_config(bridge)
                    except Exception:
                        fe_config = None
                    await ws.send_json({
                        "type": "session_loaded",
                        "session_id": new_session.session_id,
                        "session_name": new_session.metadata.get('name', ''),
                        "workspace_id": bridge._workspace_id,
                        "workspace_path": _project_path,
                        "is_running": bridge._controller.is_busy if bridge._controller else False,
                        # Intentional replacement (legacy set_project): the frontend adopts
                        # this session_loaded silently and rebinds the tab (parity with the
                        # apply_config workspace-switch branch).
                        "replacement": True,
                        "config": fe_config,
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
                    fe_config = config_manager.get_frontend_config(bridge)
                    settings = config_manager.extract_settings(fe_config) if isinstance(fe_config, dict) else {}
                    permissions = config_manager.resolve_effective_permissions(bridge._session_config) if bridge._session_config else {}
                    await ws.send_json({
                        "type": "config_changed",
                        "config": fe_config,
                        "settings": settings,
                        "permissions": permissions,
                        "merged_config": fe_config,
                        "effective_config": config_manager.get_effective_config(
                            bridge,
                            workspace_id=bridge._workspace_id if bridge else None,
                            workspace_path=bridge._workspace_path if bridge else None,
                        ),
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
            except WebSocketDisconnect:
                # Client disconnected mid-handling — let outer handler clean up
                ws._closed = True
                raise
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
                    # Verify after save
                    after_ids = session_store.get_open_sessions()
                except Exception as e:
                    pass
            bridge.unregister()


# ══════════════════════════════════════════════════════════════════════════════
# ── Register REST routers ────────────────────────────────────────────────
app.include_router(workspace_router)
app.include_router(onboarding_router)
app.include_router(config_router)
app.include_router(health_router)

app.include_router(logging_router)

app.include_router(session_router)
app.include_router(prompt_router)


# ══════════════════════════════════════════════════════════════════════════════
#  REST endpoints (health, information)
# ══════════════════════════════════════════════════════════════════════════════

# ── Host path confinement (path browser + container lifecycle) ──────────────
# The browser and the container lifecycle endpoints accept arbitrary host
# paths. Confine them to $HOME minus the vault root (~/.thoughtmachine) so a
# malicious or buggy client cannot read/create directories or mount host
# paths outside the user's home directory.

def _vault_root_path() -> str:
    """Absolute, normalized vault root (~/.thoughtmachine)."""
    root = os.path.join(os.path.expanduser("~"), ".thoughtmachine")
    try:
        return os.path.realpath(root)
    except Exception:
        return root


def _path_is_within(path: str, prefix: str) -> bool:
    """True when *path* equals *prefix* or lies beneath it (component-aware)."""
    path = os.path.normpath(path)
    prefix = os.path.normpath(prefix)
    return path == prefix or path.startswith(prefix + os.sep)


def _confine_to_home(path: str) -> str:
    """Resolve *path* (empty → $HOME) and require it to be under $HOME and
    outside the vault root (~/.thoughtmachine).

    Returns the absolute, normalized, symlink-resolved path. Raises ValueError
    when the path escapes those bounds.
    """
    raw = os.path.expanduser(path or "~")
    resolved = os.path.realpath(os.path.abspath(raw))
    home = os.path.realpath(os.path.expanduser("~"))
    if not _path_is_within(resolved, home):
        raise ValueError(
            f"Path '{path}' resolves to '{resolved}', which is outside the "
            f"allowed home directory '{home}'"
        )
    vault = _vault_root_path()
    if _path_is_within(resolved, vault):
        raise ValueError(
            f"Path '{path}' resolves into the protected vault directory '{vault}'"
        )
    return resolved


@app.get("/api/browse")
async def browse_directory(path: str = ""):
    """List directory contents for the workspace path browser.

    Confined to $HOME minus the vault root (~/.thoughtmachine): paths that
    resolve outside the home directory or into the vault are rejected.
    """
    try:
        base_path = _confine_to_home(path)
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


@app.get("/api/tools")
async def api_get_tools():
    """Return the complete list of all available tool names."""
    try:
        from session.tool_presets import _ALL_TOOLS
        return {"tools": _ALL_TOOLS}
    except ImportError:
        # Fallback: try alternate import path
        try:
            from agent.config.presets import _ALL_TOOLS
            return {"tools": _ALL_TOOLS}
        except ImportError:
            return {"tools": [], "error": "Could not load tool list"}


@app.post("/api/browse/create")
async def create_directory(body: dict):
    """Create a new directory for the workspace path browser.

    The parent path is confined to $HOME minus the vault root
    (~/.thoughtmachine), and the resulting path is checked to stay within the
    parent (blocking ``../`` traversal and absolute-name escapes).
    """
    try:
        parent_path = body.get("parent_path", "")
        dir_name = body.get("name", "")
        if not dir_name:
            return {"success": False, "error": "Directory name is required"}
        parent = _confine_to_home(parent_path)
        if not os.path.isdir(parent):
            return {"success": False, "error": f"Not a directory: {parent}"}
        new_path = os.path.normpath(os.path.join(parent, dir_name))
        if not _path_is_within(new_path, parent):
            return {"success": False, "error": f"Invalid directory name: {dir_name}"}
        if os.path.exists(new_path):
            return {"success": False, "error": f"Already exists: {dir_name}"}
        os.makedirs(new_path, exist_ok=True)
        return {"success": True, "path": os.path.abspath(new_path)}
    except (OSError, PermissionError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.get("/api/user-home")
async def user_home():
    """Return the user's home directory path."""
    return {"home": str(Path.home())}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "thoughtmachine-web-ui",
        "revision": _SERVER_REVISION,
    }


@app.get("/api/health")
async def api_health():
    """Structured health alias of GET /health (back-compat mirror)."""
    return await health()


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


# --- Phase 7: per-workspace container lifecycle API ---
# Lifecycle endpoints (list/status/start/stop/delete) address containers by
# NAME (stable, human-meaningful) and resolve them to container IDs via
# ContainerManager.list_containers(). Workspace resolution: the explicit
# `workspace_path` query param wins; otherwise the workspace registry is used.
# All endpoints mirror the JSON-error style of the existing /api/container/*.

class WorkspacePathError(ValueError):
    """Raised when an explicit ``workspace_path`` fails host-side validation."""


class WorkspacePathForbiddenError(WorkspacePathError):
    """Raised when an explicit ``workspace_path`` is valid but lies outside the
    registered workspace root for the requested workspace id (HTTP 403)."""


def _validate_workspace_path(workspace_path: str, workspace_id: str) -> str:
    """Validate an explicit container ``workspace_path`` query parameter.

    The path must resolve (after ~ expansion and symlink resolution) to an
    existing directory under $HOME and outside the vault root
    (~/.thoughtmachine), AND it must lie within the registered workspace root
    for *workspace_id*. This keeps the container lifecycle endpoints from
    mounting arbitrary host paths (e.g. /, /etc, another user's home, the
    vault, or a non-registered home directory) into a container.

    Returns the resolved absolute path. Raises WorkspacePathError (HTTP 400)
    for invalid input or an unregistered workspace, and
    WorkspacePathForbiddenError (HTTP 403) when the path escapes the
    registered workspace root.
    """
    if not workspace_path:
        raise WorkspacePathError("workspace_path is required")
    try:
        resolved = _confine_to_home(workspace_path)
    except ValueError as exc:
        raise WorkspacePathError(str(exc)) from exc
    if not os.path.isdir(resolved):
        raise WorkspacePathError(
            f"workspace_path '{workspace_path}' resolves to '{resolved}', "
            f"which is not an existing directory"
        )
    # The explicit path must stay within the registered workspace root so a
    # container cannot bind-mount arbitrary (even in-home) directories.
    try:
        entry = WorkspaceRegistry.get_default().get_workspace(workspace_id)
    except Exception as exc:
        raise WorkspacePathError(
            f"workspace '{workspace_id}' is not registered"
        ) from exc
    if entry is None:
        raise WorkspacePathError(f"workspace '{workspace_id}' is not registered")
    root = os.path.realpath(entry.root_path)
    if not _path_is_within(resolved, root):
        raise WorkspacePathForbiddenError(
            f"workspace_path '{workspace_path}' resolves to '{resolved}', "
            f"which is outside the registered workspace root '{root}' "
            f"for workspace '{workspace_id}'"
        )
    return resolved


def _resolve_workspace_path(workspace_id: str, workspace_path: str = ""):
    """Resolve the workspace root path for a container manager.

    The explicit ``workspace_path`` query param wins and is validated against
    $HOME minus the vault root (raises WorkspacePathError); otherwise fall
    back to the workspace registry. Returns None when the workspace is
    unknown or the path cannot be resolved.
    """
    if workspace_path:
        return _validate_workspace_path(workspace_path, workspace_id)
    try:
        entry = WorkspaceRegistry.get_default().get_workspace(workspace_id)
        return entry.root_path if entry is not None else None
    except Exception:
        return None


def _make_container_manager(workspace_id: str, workspace_path: str = ""):
    """Build a ContainerManager for the workspace.

    Returns None when the workspace path cannot be resolved; raises when the
    manager itself cannot be constructed. Lazy import avoids circular imports.
    """
    path = _resolve_workspace_path(workspace_id, workspace_path)
    if not path:
        return None
    from infra.container_manager import ContainerManager
    return ContainerManager(
        workspace_path=path,
        workspace_id=workspace_id,
        session_id=None,
        session_permissions=None,
    )


def _find_container_id(manager, name: str):
    """Resolve a container name to its container_id via list_containers().

    Raises when list_containers() fails; callers translate that into an error
    response.
    """
    for entry in manager.list_containers() or []:
        if entry.get("name") == name:
            return entry.get("container_id")
    return None


def _json_error(message: str, status_code: int = 500):
    """JSON error response mirroring the existing API error style."""
    return JSONResponse({"error": message}, status_code=status_code)


@app.get("/api/workspace/{workspace_id}/containers")
def workspace_containers(workspace_id: str, workspace_path: str = ""):
    """List containers for the workspace."""
    try:
        manager = _make_container_manager(workspace_id, workspace_path)
    except WorkspacePathForbiddenError as exc:
        return _json_error(str(exc), status_code=403)
    except WorkspacePathError as exc:
        return _json_error(str(exc), status_code=400)
    except Exception as exc:
        log("ERROR", "server.workspace_containers",
            f"ContainerManager construction failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if manager is None:
        return _json_error(
            f"workspace '{workspace_id}' not found or path unresolvable",
            status_code=404)
    try:
        containers = manager.list_containers() or []
        containers_in_use = len(containers)
        # Session container cap: ContainerManager.max_containers (workspace
        # config.json, default 4; clamped >= 1, mirroring
        # ContainerManager._get_max_containers()).
        raw_cap = getattr(manager, "max_containers", 4)
        try:
            cap = max(1, int(raw_cap))
        except (TypeError, ValueError):
            cap = 4
        return {
            "containers": containers,
            "containers_in_use": containers_in_use,
            "containers_available": max(0, cap - containers_in_use),
        }
    except Exception as exc:
        log("ERROR", "server.workspace_containers", f"List failed: {exc}")
        return _json_error(str(exc), status_code=503)


@app.get("/api/workspace/{workspace_id}/containers/{container_name}/status")
def workspace_container_status(workspace_id: str, container_name: str,
                               workspace_path: str = ""):
    """Report status for a single named container in the workspace."""
    try:
        manager = _make_container_manager(workspace_id, workspace_path)
    except WorkspacePathForbiddenError as exc:
        return _json_error(str(exc), status_code=403)
    except WorkspacePathError as exc:
        return _json_error(str(exc), status_code=400)
    except Exception as exc:
        log("ERROR", "server.workspace_container_status",
            f"ContainerManager construction failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if manager is None:
        return _json_error(
            f"workspace '{workspace_id}' not found or path unresolvable",
            status_code=404)
    try:
        container_id = _find_container_id(manager, container_name)
    except Exception as exc:
        log("ERROR", "server.workspace_container_status",
            f"Container lookup failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if container_id is None:
        return _json_error(f"container '{container_name}' not found",
                           status_code=404)
    try:
        return manager.status(container_id)
    except Exception as exc:
        log("ERROR", "server.workspace_container_status",
            f"Status failed: {exc}")
        return _json_error(str(exc), status_code=503)


@app.post("/api/workspace/{workspace_id}/containers/{container_name}/start")
def workspace_container_start(workspace_id: str, container_name: str,
                              workspace_path: str = "",
                              body: Optional[dict] = Body(default=None)):
    """Start (or reuse) the named container in the workspace."""
    try:
        manager = _make_container_manager(workspace_id, workspace_path)
    except WorkspacePathForbiddenError as exc:
        return _json_error(str(exc), status_code=403)
    except WorkspacePathError as exc:
        return _json_error(str(exc), status_code=400)
    except Exception as exc:
        log("ERROR", "server.workspace_container_start",
            f"ContainerManager construction failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if manager is None:
        return _json_error(
            f"workspace '{workspace_id}' not found or path unresolvable",
            status_code=404)
    note = (body or {}).get("note") if isinstance(body, dict) else None
    try:
        result = manager.start(name=container_name, note=note)
        if isinstance(result, dict) and result.get("error"):
            return _json_error(result["error"], status_code=409)
        return result
    except Exception as exc:
        log("ERROR", "server.workspace_container_start",
            f"Start failed: {exc}")
        return _json_error(str(exc), status_code=503)


@app.post("/api/workspace/{workspace_id}/containers/{container_name}/stop")
def workspace_container_stop(workspace_id: str, container_name: str,
                             workspace_path: str = ""):
    """Stop the named container in the workspace."""
    try:
        manager = _make_container_manager(workspace_id, workspace_path)
    except WorkspacePathForbiddenError as exc:
        return _json_error(str(exc), status_code=403)
    except WorkspacePathError as exc:
        return _json_error(str(exc), status_code=400)
    except Exception as exc:
        log("ERROR", "server.workspace_container_stop",
            f"ContainerManager construction failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if manager is None:
        return _json_error(
            f"workspace '{workspace_id}' not found or path unresolvable",
            status_code=404)
    try:
        container_id = _find_container_id(manager, container_name)
    except Exception as exc:
        log("ERROR", "server.workspace_container_stop",
            f"Container lookup failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if container_id is None:
        return _json_error(f"container '{container_name}' not found",
                           status_code=404)
    try:
        result = manager.stop(container_id)
        if result.get("status") == "missing":
            return _json_error(result.get("error", "container not found"),
                               status_code=404)
        if result.get("status") == "error":
            return _json_error(result.get("error", "stop failed"),
                               status_code=503)
        return result
    except Exception as exc:
        log("ERROR", "server.workspace_container_stop",
            f"Stop failed: {exc}")
        return _json_error(str(exc), status_code=503)


@app.delete("/api/workspace/{workspace_id}/containers/{container_name}")
def workspace_container_delete(workspace_id: str, container_name: str,
                               workspace_path: str = ""):
    """Remove the named container in the workspace."""
    try:
        manager = _make_container_manager(workspace_id, workspace_path)
    except WorkspacePathForbiddenError as exc:
        return _json_error(str(exc), status_code=403)
    except WorkspacePathError as exc:
        return _json_error(str(exc), status_code=400)
    except Exception as exc:
        log("ERROR", "server.workspace_container_delete",
            f"ContainerManager construction failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if manager is None:
        return _json_error(
            f"workspace '{workspace_id}' not found or path unresolvable",
            status_code=404)
    try:
        container_id = _find_container_id(manager, container_name)
    except Exception as exc:
        log("ERROR", "server.workspace_container_delete",
            f"Container lookup failed: {exc}")
        return _json_error(str(exc), status_code=503)
    if container_id is None:
        return _json_error(f"container '{container_name}' not found",
                           status_code=404)
    try:
        result = manager.remove(container_id)
        if result.get("status") == "missing":
            return _json_error(result.get("error", "container not found"),
                               status_code=404)
        if result.get("status") == "error":
            return _json_error(result.get("error", "remove failed"),
                               status_code=503)
        return result
    except Exception as exc:
        log("ERROR", "server.workspace_container_delete",
            f"Remove failed: {exc}")
        return _json_error(str(exc), status_code=503)


@app.get("/api/health/containers")
def health_containers():
    """Report whether the Docker daemon is reachable from the server.

    Returns a structured payload so callers can surface degraded Docker
    access with an actionable hint instead of a bare status string.
    The "status" key stays backward compatible ("ok"/"degraded"); the
    "docker" key carries the dispatch spec:
      {"available": bool, "reason": str-or-null, "hint": str-or-null,
       "version": str-or-null, "error": str-or-null}
    where reason is exactly one of daemon_down | permission_denied |
    lib_missing | import_failed | docker_host_unreachable, or null when
    Docker is available.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    client = None
    try:
        import docker as _docker
        client = _docker.from_env()
        # Log exactly what endpoint the SDK resolved so CI/doctor runs can
        # prove DOCKER_HOST was honored (no silent socket fallback).
        log("INFO", "server.health_containers",
            f"docker client resolved base_url={client.api.base_url} "
            f"(DOCKER_HOST={os.environ.get('DOCKER_HOST')!r})")
        client.ping()
        version = None
        try:
            version = client.version().get("Version")
        except Exception:
            version = None
        log("INFO", "server.health_containers",
            "Docker daemon reachable"
            + (f" (version {version})" if version else ""))
        return {
            "status": "ok",
            "docker": {
                "available": True,
                "reason": None,
                "hint": None,
                "version": version,
                "error": None,
            },
            "checked_at": checked_at,
        }
    except ImportError as exc:
        # Docker SDK missing, or a docker-backed tool failed to import.
        # Distinguish via the import-failure registry tools/__init__ keeps.
        reason = "lib_missing"
        hint = "Install the Docker SDK: .venv/bin/pip install docker"
        try:
            import tools
            failed = [
                f for f in getattr(tools, "IMPORT_FAILURES", [])
                if f.get("tool") in ("DockerCodeRunner", "container_control")
            ]
            if failed:
                reason = "import_failed"
                hint = (
                    "The Docker tool failed to load: "
                    f"{failed[0].get('error')} — check the server logs"
                )
        except Exception:
            pass
        log("ERROR", "server.health_containers",
            f"Docker SDK unavailable: {exc}")
        return {
            "status": "degraded",
            "docker": {
                "available": False,
                "reason": reason,
                "hint": hint,
                "version": None,
                "error": str(exc),
            },
            "checked_at": checked_at,
        }
    except Exception as exc:
        msg = str(exc).lower()
        if isinstance(exc, PermissionError) or "permission denied" in msg:
            reason = "permission_denied"
            hint = (
                "Add your user to the docker group (sudo usermod -aG docker $USER) "
                "and re-login, or run the server as a docker-group user."
            )
        elif any(token in msg for token in (
                "connection refused", "cannot connect", "socket",
                "connect", "timeout", "daemon")):
            reason = "daemon_down"
            hint = "Start the Docker daemon: sudo systemctl enable --now docker"
        else:
            reason = "docker_host_unreachable"
            hint = (
                f"Docker host unreachable: {str(exc)[:200]} — "
                "check DOCKER_HOST and docker context."
            )
        log("ERROR", "server.health_containers",
            f"Docker daemon unreachable: {exc}")
        return {
            "status": "degraded",
            "docker": {
                "available": False,
                "reason": reason,
                "hint": hint,
                "version": None,
                "error": str(exc),
            },
            "checked_at": checked_at,
        }
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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

def _session_has_conversation(session) -> bool:
    """Check if a session has an actual conversation (user or assistant messages).

    Returns True if any message in user_history has a 'role' of 'user' or 'assistant'.
    System notifications (role='system') alone do NOT count as a conversation.
    """
    if session is None:
        return False
    try:
        history = session.user_history
        if not history:
            return False
        for msg in history:
            role = msg.get('role', '') if isinstance(msg, dict) else ''
            if role in ('user', 'assistant'):
                return True
        return False
    except Exception:
        return False


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


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser (extracted for testability).

    The default bind host is 127.0.0.1 (loopback only); override with the
    HOST environment variable or ``--host``.
    """
    parser = argparse.ArgumentParser(description="ThoughtMachine Web UI Server")
    parser.add_argument(
        "--serve-frontend",
        action="store_true",
        help="Build and serve the React frontend alongside the API",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Host to bind to (default: 127.0.0.1; override with HOST env var)",
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
    return parser


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

    args = build_parser().parse_args()

    # Rebuild global session registry from disk
    registry = SessionRegistry.get_default()
    count = registry.rebuild_from_disk()
    log('INFO', 'server', f'Rebuilt session registry from disk: {count} sessions found')

    if args.serve_frontend:
        _setup_frontend_serving()

    log('INFO', 'server',
        f'Starting ThoughtMachine Web UI on {args.host}:{args.port} — revision {_SERVER_REVISION}')
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
