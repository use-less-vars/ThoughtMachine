"""Regression tests: import order robustness of the `tools` package.

Verifies that importing `thoughtmachine.security` or `agent.config` BEFORE
`import tools` does not break tool discovery (DockerCodeRunner must still be
registered and no import failure recorded).

Each test runs a subprocess so the import order is truly fresh, and uses the
system interpreter (repo root on sys.path via cwd).
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SECURITY_FIRST = (
    "import thoughtmachine.security\n"
    "import tools\n"
    "names = [c.__name__ for c in tools.TOOL_CLASSES]\n"
    "assert 'DockerCodeRunner' in names, names\n"
    "failures = [f for f in tools.IMPORT_FAILURES if f.get('tool') == 'DockerCodeRunner']\n"
    "assert not failures, failures\n"
    "print('IMPORT-ORDER-OK')\n"
)

_AGENT_CONFIG_FIRST = (
    "import agent.config\n"
    "import tools\n"
    "names = [c.__name__ for c in tools.TOOL_CLASSES]\n"
    "assert 'DockerCodeRunner' in names, names\n"
    "failures = [f for f in tools.IMPORT_FAILURES if f.get('tool') == 'DockerCodeRunner']\n"
    "assert not failures, failures\n"
    "print('IMPORT-ORDER-OK')\n"
)


def _run_import_order(code):
    """Run `code` in a fresh interpreter with repo root on sys.path."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_import_order_security_first_does_not_break_tools():
    proc = _run_import_order(_SECURITY_FIRST)
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "IMPORT-ORDER-OK" in proc.stdout


def test_import_order_agent_config_first_does_not_break_tools():
    proc = _run_import_order(_AGENT_CONFIG_FIRST)
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "IMPORT-ORDER-OK" in proc.stdout


# ---- Phase B: web_ui workspace-routes / backend entrypoint import order ----

_WORKSPACE_ROUTES_FIRST = (
    "import web_ui.backend.workspace_routes\n"
    "from tools import SIMPLIFIED_TOOL_CLASSES\n"
    "names = [c.__name__ for c in SIMPLIFIED_TOOL_CLASSES]\n"
    "assert 'DockerCodeRunner' in names, names\n"
    "print('IMPORT-ORDER-OK')\n"
)

_BACKEND_ENTRYPOINT = (
    "import web_ui.backend.server\n"
    "print('BACKEND-IMPORT-OK')\n"
)


def test_import_order_workspace_routes_first_does_not_break_tools():
    """web_ui.backend.workspace_routes is imported at backend startup before
    `tools`; that order must still register DockerCodeRunner.

    NOTE: `'DockerCodeRunner' in SIMPLIFIED_TOOL_CLASSES` would be False BY
    DESIGN (the list holds class objects; string-in-list compares identity),
    so the name-based any() check is used instead.
    """
    proc = _run_import_order(_WORKSPACE_ROUTES_FIRST)
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "IMPORT-ORDER-OK" in proc.stdout


def test_backend_entrypoint_imports_without_error():
    """The FastAPI backend entrypoint (web_ui.backend.server) must import
    cleanly. uvicorn only runs under `__main__`, so importing must not bind
    ports or hang.
    """
    proc = _run_import_order(_BACKEND_ENTRYPOINT)
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "BACKEND-IMPORT-OK" in proc.stdout
    assert "ImportError" not in proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr

