"""Importing web_ui.backend.server must not hijack the process signal handlers.

The same import-time discipline is enforced for the CLI edge: importing
``agent.cli.rag_commands`` pulls in ``agent.knowledge.codebase_indexer``, which
must not install SIGINT/SIGTERM handlers at module scope either.

The server deliberately does NOT install SIGINT/SIGTERM handlers at import
time: uvicorn overwrites the process signal handlers when its event loop
starts, so import-time handlers would be clobbered anyway (and they would
shadow the interpreter defaults during tests/CLI runs). The atexit hook for
session saving is the intended import-time side effect and must survive.

The probe runs in a subprocess so it observes a clean interpreter that has
never imported the server before.
"""

import subprocess
import sys

# sys.path is prepared by tests/conftest.py, which derives the repo root from
# this checkout's own __file__.  Never insert a literal container path here:
# "/workspace" resolves to the PARENT checkout when the suite runs from a git
# worktree, so the module imported another tree's security/tools/agent and still
# went green.  Pinned by tests/test_import_provenance.py.
# Kept: the sandbox-only /tmp/stubs fake agent package is still put first.
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
sys.path.insert(0, _stubs_path)

_PROBE = """
import signal
import atexit

# Spy on atexit.register before importing the server so we can assert that
# _shutdown_save (and only the atexit mechanism) survived — this works on
# CPython builds that do not expose the private atexit._exithandlers list.
_registered = []
_real_register = atexit.register

def _spy_register(func, *args, **kwargs):
    _registered.append(func)
    return _real_register(func, *args, **kwargs)

atexit.register = _spy_register

import web_ui.backend.server  # noqa: F401  (must not hijack handlers)

assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, \\
    "SIGINT handler hijacked by server import"
assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, \\
    "SIGTERM handler hijacked by server import"
assert any(f is web_ui.backend.server._shutdown_save for f in _registered), \\
    "atexit _shutdown_save hook missing after server import"
print('OK')
"""


def test_server_import_does_not_hijack_signal_handlers():
    """Importing web_ui.backend.server leaves SIGINT/SIGTERM at defaults."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"probe failed (exit {result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


_PROBE_CLI = """
import signal

import agent.cli.rag_commands  # noqa: F401  (must not hijack handlers)

assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, \\
    "SIGINT handler hijacked by agent.cli.rag_commands import"
assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, \\
    "SIGTERM handler hijacked by agent.cli.rag_commands import"
print('OK')
"""


def test_cli_import_does_not_hijack_signal_handlers():
    """Importing agent.cli.rag_commands leaves SIGINT/SIGTERM at defaults."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_CLI],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"probe failed (exit {result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
