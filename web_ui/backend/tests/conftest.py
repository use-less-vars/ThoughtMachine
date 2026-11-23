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
import sys
import tempfile
import threading

import pytest
from pathlib import Path
from types import SimpleNamespace
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


# ---------------------------------------------------------------------------
# Thread / bridge census, local mirror of the one in the root ``tests/``
# conftest.
#
# This directory is its own pytest root (``testpaths`` lists it separately), so
# the root conftest's fixtures -- autouse ones included -- never apply here, and
# nine of these modules build a ``WebAgentBridge`` / start a ``WorkerThread`` /
# subscribe to the process-global ``EventBus``.  Without a reaper those tests
# leak into each other exactly like the ones under ``tests/`` did.
#
# Deliberately self-contained: a lazy import of the same three repo symbols, no
# import of anything under ``tests/`` and no dependency on a root-conftest
# fixture (this root must stay runnable on its own).  The duplication is ~60
# lines and is the price of two independent pytest roots; a shared helper module
# would make this root import the other root's test package.
#
# Reaping is autouse and unconditional; the assertion is opt-in per test with
# ``@pytest.mark.thread_census`` only (there is no measured allow-list for this
# root yet, so nothing is enforced here by default).
# ---------------------------------------------------------------------------

_WEBUI_REAP_JOIN_TIMEOUT = 5.0


def _webui_worker_registry():
    from tools.workspace.worker_registry import WorkerRegistry

    return WorkerRegistry.get_instance()


def _webui_is_worker_thread(obj):
    return (
        isinstance(obj, threading.Thread)
        and callable(getattr(obj, "kill", None))
        and hasattr(obj, "worker_name")
    )


def _webui_reap_bridges(bridges):
    for bridge in bridges:
        try:
            bridge.stop()
        except Exception as exc:
            print("thread census: bridge.stop() failed for %r: %s" % (bridge, exc),
                  file=sys.stderr)
            for name in ("_unsubscribe_security_events", "_unsubscribe_worker_events"):
                try:
                    getattr(bridge, name, lambda: None)()
                except Exception:
                    pass


def _webui_reap_worker_threads(threads_before, keys_before):
    try:
        registered = _webui_worker_registry().get_all_workers()
    except Exception:
        registered = {}
    candidates = [thread for key, thread in registered.items()
                  if key not in keys_before and _webui_is_worker_thread(thread)]
    for thread in threading.enumerate():
        if id(thread) in threads_before or any(thread is c for c in candidates):
            continue
        if _webui_is_worker_thread(thread):
            candidates.append(thread)
    survivors = []
    for thread in candidates:
        try:
            thread.kill()
        except Exception as exc:
            print("thread census: kill() failed for %r: %s" % (thread, exc),
                  file=sys.stderr)
        try:
            thread.join(_WEBUI_REAP_JOIN_TIMEOUT)
        except Exception as exc:
            print("thread census: join() failed for %r: %s" % (thread, exc),
                  file=sys.stderr)
        try:
            if thread.is_alive():
                survivors.append(thread)
        except Exception:
            pass
    return survivors


def _webui_bus_counts():
    from agent.events import global_event_bus

    counts = {}
    for event_type, callbacks in list(global_event_bus._subscribers.items()):
        counts[getattr(event_type, "value", str(event_type))] = len(callbacks)
    return counts


@pytest.fixture
def _webui_reaper():
    """The reaper as a value, so that a test never has to find this module.

    ``sys.modules`` cannot be used for that lookup: this file is imported under
    the bare name ``conftest``, and so are ``<rootdir>/conftest.py`` and the
    conftests of the other roots that are not packages
    (``tests/security/conftest.py``, ``tests/e2e/conftest.py``).  Only the last
    one imported keeps the ``"conftest"`` key, so scanning ``sys.modules`` for
    this file's path misses whenever another root's conftest was loaded after
    this one -- exactly what happens in a whole-repo run such as
    ``pytest tests/ web_ui/backend/tests``.
    """
    return SimpleNamespace(
        worker_registry=_webui_worker_registry,
        reap_worker_threads=_webui_reap_worker_threads,
    )


@pytest.fixture(autouse=True)
def _webui_thread_bridge_census(request):
    """Reap the threads and bridges a test leaves; ``thread_census`` asserts."""
    from web_ui.backend.bridge import WebAgentBridge

    enforced = request.node.get_closest_marker("thread_census") is not None
    threads_before = {id(t) for t in threading.enumerate()}
    try:
        keys_before = set(_webui_worker_registry().get_all_workers())
    except Exception:
        keys_before = set()
    counts_before = _webui_bus_counts() if enforced else {}

    bridges_created = []
    original_init = WebAgentBridge.__init__

    def _webui_tracking_init(self, *args, **kwargs):
        bridges_created.append(self)
        original_init(self, *args, **kwargs)

    WebAgentBridge.__init__ = _webui_tracking_init

    try:
        yield
    finally:
        # Patched and restored by hand, deliberately NOT via ``monkeypatch``:
        # this fixture is autouse, so requesting ``monkeypatch`` would create it
        # before every module/tests-level autouse fixture and push its undo
        # *after* their teardown -- a teardown re-reading a global the test
        # patched would then see the patched garbage (see
        # tests/test_fixture_teardown_ordering.py for the same defect in
        # tests/conftest.py).  Restore FIRST here, before the reaping and before
        # any assertion, so no fixture tearing down alongside this one ever
        # observes the tracking ``__init__``.
        try:
            if WebAgentBridge.__init__ is _webui_tracking_init:
                WebAgentBridge.__init__ = original_init
        except Exception as exc:
            print("thread census: __init__ restore failed: %s" % (exc), file=sys.stderr)
        try:
            _webui_reap_bridges(bridges_created)
        except Exception as exc:
            print("thread census: bridge reaping failed: %s" % (exc), file=sys.stderr)
        survivors = []
        try:
            survivors = _webui_reap_worker_threads(threads_before, keys_before)
        except Exception as exc:
            print("thread census: thread reaping failed: %s" % (exc), file=sys.stderr)
        if not enforced:
            return
        problems = []
        try:
            keys_now = set(_webui_worker_registry().get_all_workers())
        except Exception:
            keys_now = set(keys_before)
        for key in sorted(keys_now - keys_before, key=repr):
            problems.append("worker registry still holds %r" % (key,))
        for thread in threading.enumerate():
            if id(thread) not in threads_before:
                problems.append("thread still alive: %r" % (thread.name,))
        for thread in survivors:
            problems.append("worker thread survived kill()+join: %r" % (thread,))
        counts_now = _webui_bus_counts()
        for event_type in sorted(counts_now):
            count = counts_now[event_type]
            if count > counts_before.get(event_type, 0):
                problems.append(
                    "bus global gained subscribers for %r: %d -> %d"
                    % (event_type, counts_before.get(event_type, 0), count)
                )
        if problems:
            pytest.fail(
                "thread/bridge census: this test left process-global state behind "
                "(the autouse reaper already ran, so these survived ``kill()`` + "
                "%.1fs join and ``WebAgentBridge.stop()``):\n"
                % _WEBUI_REAP_JOIN_TIMEOUT
                + "\n".join("  - " + line for line in problems[:20]),
                pytrace=False,
            )
