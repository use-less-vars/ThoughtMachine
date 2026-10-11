"""
Test configuration for web_ui backend tests.

Includes a hermetic-environment guard mirroring the root ``tests/conftest.py``:
running this directory in isolation must never read or write the real user
vault (``~/.thoughtmachine``).  The existing ``temp_session_dir`` /
``session_store`` fixtures are preserved unchanged.
"""
import atexit
import builtins
import functools
import io
import os
import pathlib
import shutil
import tempfile

import pytest
from pathlib import Path
from session.store import FileSystemSessionStore


# ---------------------------------------------------------------------------
# Real-vault detection. Computed at conftest import time -- before any HOME
# redirect performed below.
# ---------------------------------------------------------------------------

REAL_HOME = os.path.expanduser("~")
REAL_VAULT = os.path.realpath(os.path.join(REAL_HOME, ".thoughtmachine"))

_GUARD_DISABLED = os.environ.get("THOUGHTMACHINE_HERMETIC_GUARD_DISABLED") == "1"


def _is_real_vault_path(p):
    """Return True when *p* (str/bytes/PathLike) is inside the real vault."""
    try:
        raw = os.fsdecode(os.fspath(p))
    except TypeError:
        return False
    real = os.path.realpath(raw)
    return real == REAL_VAULT or real.startswith(REAL_VAULT + os.sep)


def _raise_if_real_vault(op, *paths):
    for p in paths:
        if p is not None and _is_real_vault_path(p):
            raise RuntimeError(
                "HERMETIC VAULT GUARD: blocked %s on real vault path %r "
                "(real vault: %s)" % (op, os.fspath(p), REAL_VAULT)
            )


def _install_guard():
    """Wrap filesystem primitives; writes into the real vault now raise."""
    originals = {}

    def make_wrapper(mod, name, arg_indices):
        original = getattr(mod, name)
        originals[(mod, name)] = original

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            for i in arg_indices:
                if i < len(args):
                    _raise_if_real_vault(name, args[i])
            return original(*args, **kwargs)

        setattr(mod, name, wrapper)

    for mod, name, idx in (
        (os, "makedirs", (0,)),
        (os, "mkdir", (0,)),
        (os, "remove", (0,)),
        (os, "unlink", (0,)),
        (os, "rmdir", (0,)),
        (os, "rename", (0, 1)),
        (os, "replace", (0, 1)),
        (os, "open", (0,)),
        (os, "utime", (0,)),
        (os, "chmod", (0,)),
        (shutil, "rmtree", (0,)),
        (shutil, "move", (0, 1)),
    ):
        make_wrapper(mod, name, idx)

    # builtins.open and io.open are the same function object in CPython but
    # live in separate module namespaces; pathlib.Path.open() routes through
    # io.open -- patch both.
    def make_open_wrapper(mod):
        original = mod.open
        originals[(mod, "open")] = original

        @functools.wraps(original)
        def open_wrapper(*args, **kwargs):
            if "mode" in kwargs:
                mode = kwargs["mode"]
            elif len(args) > 1:
                mode = args[1]
            else:
                mode = "r"
            if any(c in str(mode) for c in "wax+"):
                _raise_if_real_vault("open", args[0])
            return original(*args, **kwargs)

        setattr(mod, "open", open_wrapper)

    make_open_wrapper(builtins)
    make_open_wrapper(io)

    return originals


def _restore_guard(originals):
    for (mod, name), original in originals.items():
        setattr(mod, name, original)


# ---------------------------------------------------------------------------
# Hermetic HOME redirect -- applied at CONFTEST IMPORT TIME.
# ---------------------------------------------------------------------------

_HERMETIC_BASE = tempfile.mkdtemp(prefix="webui-hermetic-")
atexit.register(shutil.rmtree, _HERMETIC_BASE, ignore_errors=True)

_SAVED_HOME = os.environ.get("HOME")
_SAVED_VAULT_ROOT = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
_SAVED_PATH_HOME = pathlib.Path.home
_SAVED_USERPROFILE = os.environ.get("USERPROFILE")
_SAVED_HOMEDRIVE = os.environ.get("HOMEDRIVE")
_SAVED_HOMEPATH = os.environ.get("HOMEPATH")

os.environ["HOME"] = _HERMETIC_BASE
# On Windows, ``os.path.expanduser`` is ``ntpath.expanduser``: it resolves a
# leading ``~`` from ``USERPROFILE`` (or ``HOMEDRIVE`` + ``HOMEPATH``) and
# IGNORES ``HOME``.  Set the Windows variables too so any expanduser caller is
# redirected.
os.environ["USERPROFILE"] = _HERMETIC_BASE
_HOMEDRIVE, _HOMEPATH = os.path.splitdrive(_HERMETIC_BASE)
os.environ["HOMEDRIVE"] = _HOMEDRIVE or ""
os.environ["HOMEPATH"] = _HOMEPATH or _HERMETIC_BASE
os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)
pathlib.Path.home = staticmethod(
    lambda: pathlib.Path(os.environ.get("HOME", _HERMETIC_BASE))
)

# ``os.path.expanduser`` is a second "~"-resolving seam.  Re-import safe: the
# wrapper carries a marker and is never stacked (a double conftest import
# reuses the first captured original).
if getattr(os.path.expanduser, "_webui_hermetic", False):
    _SAVED_EXPANDUSER = os.path.expanduser._webui_original
else:
    _SAVED_EXPANDUSER = os.path.expanduser

    @functools.wraps(_SAVED_EXPANDUSER)
    def _hermetic_expanduser(path):
        if path == "~":
            rest = None
        elif path.startswith("~/") or path.startswith("~\\"):
            rest = path[2:]
        else:
            return _SAVED_EXPANDUSER(path)
        home = os.environ.get("HOME")
        if not home:
            return _SAVED_EXPANDUSER(path)
        return home if rest is None else os.path.join(home, rest)

    _hermetic_expanduser._webui_hermetic = True
    _hermetic_expanduser._webui_original = _SAVED_EXPANDUSER
    os.path.expanduser = _hermetic_expanduser


def _restore_env_var(name, saved):
    """Restore env var *name* to *saved* (delete it when it was originally unset)."""
    if saved is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = saved


def _restore_hermetic_env():
    """Restore pre-redirect HOME / vault root / ``Path.home`` / ``expanduser``."""
    pathlib.Path.home = _SAVED_PATH_HOME
    os.path.expanduser = _SAVED_EXPANDUSER
    _restore_env_var("HOME", _SAVED_HOME)
    _restore_env_var("THOUGHTMACHINE_VAULT_ROOT", _SAVED_VAULT_ROOT)
    _restore_env_var("USERPROFILE", _SAVED_USERPROFILE)
    _restore_env_var("HOMEDRIVE", _SAVED_HOMEDRIVE)
    _restore_env_var("HOMEPATH", _SAVED_HOMEPATH)


atexit.register(_restore_hermetic_env)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_fs_guard():
    """Block every filesystem write targeting the REAL user vault."""
    if _GUARD_DISABLED:
        yield
        return
    originals = _install_guard()
    try:
        yield
    finally:
        _restore_guard(originals)


# ---------------------------------------------------------------------------
# Session-store fixtures (unchanged).
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_session_dir():
    """Create a temporary directory for session storage and clean up after test."""
    tmpdir = tempfile.mkdtemp(prefix="test_sessions_")
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def session_store(temp_session_dir):
    """Return a FileSystemSessionStore pointing at the temp directory."""
    return FileSystemSessionStore(sessions_dir=str(temp_session_dir), enable_session_history_pruning=False)
