"""Importing web_ui.backend.server must not hijack the process signal handlers.

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

# ── Fix sys.path for Docker sandbox ──────────────────────────────────────
# Pytest injects the tests dir into sys.path; keep the repo root importable
# and put /tmp/stubs (sandbox fake agent package) first, when present.
_bad_prefix = "/workspace/tests"
sys.path = [p for p in sys.path if not p.startswith(_bad_prefix)]
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _stubs_path)
sys.path.insert(1, "/workspace")

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
