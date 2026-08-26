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
