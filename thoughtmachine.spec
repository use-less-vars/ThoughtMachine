# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ThoughtMachine.

Build a single-folder (--onedir) or single-file (--onefile) executable
that bundles the entire ThoughtMachine application including:

  - The Python application code (agent, tools, session, llm_providers, web_ui, …)
  - All third-party dependencies (fastapi, uvicorn, pydantic, …)
  - The pre-built React frontend (web_ui/frontend/dist/)
  - Default resource files (system_prompt.txt, config, security policy, …)

Usage
─────
  1. Build the frontend first:
       cd web_ui/frontend  &&  npm install  &&  npm run build

  2. Run PyInstaller:
       pyinstaller thoughtmachine.spec

  3. The output appears in dist/ThoughtMachine/

Notes
─────
  • The entry point auto-enables --serve-frontend so the bundled frontend is
    served at http://localhost:8000/ without any extra flags.
  • Use --no-frontend to skip frontend serving (headless API mode).
  • On first run, bootstrap.py creates ~/.thoughtmachine/ with defaults.
"""

import os
import sys
from pathlib import Path

# ── Project paths ────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
FRONTEND_DIST = PROJECT_ROOT / "web_ui" / "frontend" / "dist"
RESOURCES_DIR = PROJECT_ROOT / "resources"


# ══════════════════════════════════════════════════════════════════════════
#  Collect all third-party packages (the easy way)
# ══════════════════════════════════════════════════════════════════════════

# PyInstaller hooks / hidden imports for dynamic / lazy imports
# -----------------------------------------------------------------------
# tools/__init__.py uses try/except to import each tool -- PyInstaller's
# scanner won't see those imports because they are hidden behind
# ImportError handlers.  Explicitly list every tool module.
# -----------------------------------------------------------------------
HIDDEN_IMPORTS = [
    # ── Tool modules (dynamically imported in tools/__init__.py) ──────────
    "tools.base",
    "tools.file_editor",
    "tools.file_preview_tool",
    "tools.directory_tree_tool",
    "tools.glob_tool",
    "tools.file_search_tool",
    "tools.apply_edits",
    "tools.code_modifier",
    "tools.code_modifier_utils",
    "tools.refactor_tool",
    "tools.search_codebase",
    "tools.datetime_tool",
    "tools.directory_creator",
    "tools.docker_code_runner",
    "tools.field_viewer",
    "tools.file_mover",
    "tools.file_summary_tool",
    "tools.git_info_tool",
    "tools.knowledge_base",
    "tools.mcp_validator",
    "tools.paginate_tool",
    "tools.progress_report",
    "tools.respond",
    "tools.summarize_tool",
    "tools.thought",
    "tools.utils",

    # ── Agent sub-packages (lazy-loaded in server.py) ────────────────────
    "agent.controller",
    "agent.config",
    "agent.config.loader",
    "agent.config.models",
    "agent.config.preset",
    "agent.config.provider_profile",
    "agent.config.service",
    "agent.core.agent",
    "agent.core.conversation_manager",
    "agent.core.debug_context",
    "agent.core.llm_client",
    "agent.core.message",
    "agent.core.message_utils",
    "agent.core.state",
    "agent.core.token_counter",
    "agent.core.tool_executor",
    "agent.core.turn_transaction",
    "agent.cli",
    "agent.cli.main",
    "agent.cli.rag_commands",
    "agent.knowledge.base",
    "agent.knowledge.codebase_indexer",
    "agent.knowledge.codebase_kb",
    "agent.knowledge.dependencies",
    "agent.knowledge.global_kb",
    "agent.logging",
    "agent.logging.unified",
    "agent.logging.debug_log_adapter",
    "agent.presenter",
    "agent.presenter.agent_presenter",
    "agent.presenter.event_processor",
    "agent.presenter.gui_integration",
    "agent.presenter.session_lifecycle",
    "agent.presenter.state_bridge",

    # ── Session package ──────────────────────────────────────────────────
    "session.models",
    "session.store",
    "session.context_builder",
    "session.event_schema",
    "session.history_provider",
    "session.history_pruner",
    "session.utils",

    # ── LLM providers (factory loads lazily by name) ─────────────────────
    "llm_providers.base",
    "llm_providers.factory",
    "llm_providers.openai_compatible",
    "llm_providers.anthropic_provider",
    "llm_providers.exceptions",
    "llm_providers.tool_converter",

    # ── Web UI backend ───────────────────────────────────────────────────
    "web_ui.backend.bridge",
    "web_ui.backend.server",

    # ── thoughtmachine package ───────────────────────────────────────────
    "thoughtmachine.bootstrap",
    "thoughtmachine.security",
    "thoughtmachine.security_config",

    # ── MCP ──────────────────────────────────────────────────────────────
    "tools.mcp_client",
    "tools.mcp_client_new",
    "tools.mcp_manager",

    # ── uvicorn internals (needed for sub-process / module-string runner)─
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.middleware.debug",
    "uvicorn.middleware.proxy_headers",
    "uvicorn.middleware.wsgi",

    # ── Starlette / FastAPI internals ────────────────────────────────────
    "starlette.routing",
    "starlette.middleware",

    # ── pydantic ─────────────────────────────────────────────────────────
    "pydantic",
    "pydantic.dataclasses",

    # ── tiktoken ─────────────────────────────────────────────────────────
    "tiktoken_ext.openai_public",
    "tiktoken_ext",
]

# Data files that should be placed alongside the Python packages
# inside the bundle (e.g. dist/ThoughtMachine/_internal/resources/).
DATA_FILES = []

# ── Bundled resources ────────────────────────────────────────────────────
# bootstrap.py uses importlib.resources.files("thoughtmachine") to locate
# the resources/ directory.  PyInstaller places packages under _internal/,
# so we must add resources/ as a top-level data folder next to the
# thoughtmachine/ package directory.
if RESOURCES_DIR.is_dir():
    DATA_FILES.append((str(RESOURCES_DIR), "resources"))

# ── Pre-built frontend ──────────────────────────────────────────────────
# The server mounts StaticFiles at "/" pointing to frontend/dist/.
# We place it at a known path relative to the entry point.
if FRONTEND_DIST.is_dir():
    DATA_FILES.append((str(FRONTEND_DIST), "frontend_dist"))
else:
    print(
        "WARNING: frontend/dist/ not found. "
        "Run: cd web_ui/frontend && npm install && npm run build",
        file=sys.stderr,
    )

# ── Docker executor Dockerfile ───────────────────────────────────────────
dockerfile = PROJECT_ROOT / "docker" / "executor.Dockerfile"
if dockerfile.exists():
    DATA_FILES.append((str(dockerfile), "docker"))

docker_reqs = PROJECT_ROOT / "docker" / "requirements-docker.txt"
if docker_reqs.exists():
    DATA_FILES.append((str(docker_reqs), "docker"))

# ── Other data files the agent expects at runtime ────────────────────────
system_prompt = PROJECT_ROOT / "system_prompt.txt"
if system_prompt.exists():
    DATA_FILES.append((str(system_prompt), "."))

agent_config = PROJECT_ROOT / "agent_config.json"
if agent_config.exists():
    DATA_FILES.append((str(agent_config), "."))


# ══════════════════════════════════════════════════════════════════════════
#  PyInstaller Analysis
# ══════════════════════════════════════════════════════════════════════════

a = Analysis(
    # Entry point -- a small wrapper that auto-enables --serve-frontend
    ["thoughtmachine_entry.py"],

    # PATHS = extra paths to search for imports
    pathex=[str(PROJECT_ROOT)],

    binaries=[],
    datas=DATA_FILES,

    hiddenimports=HIDDEN_IMPORTS,

    # Exclude modules that are never used
    excludes=[
        # Qt GUI -- not needed for web UI packaging
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtWidgets",
        "PyQt6.QtGui",
        "qt_gui",
        # tkinter
        "tkinter",
        "tkinter.filedialog",
        "tkinter.ttk",
        # idlelib (bundled with CPython, never used)
        "idlelib",
        "idlelib.*",
        # test packages
        "test",
        "unittest",
        "distutils",
        "ensurepip",
        "venv",
        "lib2to3",
        # numpy & scipy (not used)
        "numpy",
        "scipy",
        # matplotlib (not used)
        "matplotlib",
        "pandas",
        # Jupyter (not used)
        "jupyter",
        "notebook",
        "IPython",
        # Cryptography backends (not needed)
        "cryptography",
        "OpenSSL",
    ],

    hookspath=[],
    hooksconfig={},
)

# ══════════════════════════════════════════════════════════════════════════
#  PyInstaller PYZ (bytecode archive)
# ══════════════════════════════════════════════════════════════════════════

pyz = PYZ(a.pure, a.zipped_data)

# ══════════════════════════════════════════════════════════════════════════
#  Executable
# ══════════════════════════════════════════════════════════════════════════

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ThoughtMachine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                # compress the binary (requires UPX on PATH)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,            # show a console window (useful for first-time debug)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows icon (if you have one)
    # icon="resources/icon.ico",
)

# ══════════════════════════════════════════════════════════════════════════
#  One-folder COLLECT
# ══════════════════════════════════════════════════════════════════════════

# COLLECT gathers everything into dist/ThoughtMachine/ (one-folder mode).
# This is preferred for the first build iteration; switch to
# EXE(…, console=False) with COLLECT omitted for a single .exe.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ThoughtMachine",
)
