# Root conftest — shared fixtures for the test suite

import atexit
import builtins
import functools
import importlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import subprocess
import warnings

import pytest


# ---------------------------------------------------------------------------
# Real-vault detection. Computed at conftest import time — before any HOME
# redirect performed by fixtures below.
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
    # io.open, shutil.copyfile through builtins.open — patch both.
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
# Hermetic HOME redirect — applied at CONFTEST IMPORT TIME.
# ---------------------------------------------------------------------------
# A session fixture used to redirect HOME after pytest had already imported
# every test module (collection-time imports). Test modules and app modules
# imported during collection (security/security_gate.py, agent.core.tool_
# executor, agent.config.models, tools.docker_code_runner, tools.git_info_tool)
# bind `from thoughtmachine.security import X` at import time, so a later
# importlib.reload split object identity: direct references held by test
# modules (exception classes, VAULT_ROOT, _pending_security_requests dict,
# _pending_requests_lock, _prompt_cancelled event, SessionPermissions class)
# stayed OLD while reloaded module globals pointed at NEW objects. Redirecting
# HOME HERE, before any test module is imported, keeps every collection-time
# import-time binding consistent; the six-module reload loop now runs inside
# the hermetic_vault_env fixture (see below).

_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

HERMETIC_BASE_DIR = tempfile.mkdtemp(prefix="hermetic-vault-base-")
atexit.register(shutil.rmtree, HERMETIC_BASE_DIR, ignore_errors=True)

_SAVED_HOME = os.environ.get("HOME")
_SAVED_VAULT_ROOT = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
_SAVED_PATH_HOME = pathlib.Path.home
_SAVED_USERPROFILE = os.environ.get("USERPROFILE")
_SAVED_HOMEDRIVE = os.environ.get("HOMEDRIVE")
_SAVED_HOMEPATH = os.environ.get("HOMEPATH")

# Tracked at session start by hermetic_vault_env; used by the exit-time
# cleanup helper (kept named for symmetry; see _cleanup_hermetic_base).
_HERMETIC_BASE_DIR = None

os.environ["HOME"] = HERMETIC_BASE_DIR
# On Windows, ``os.path.expanduser`` is ``ntpath.expanduser``: it resolves a
# leading ``~`` from ``USERPROFILE`` (or ``HOMEDRIVE`` + ``HOMEPATH``) and
# IGNORES ``HOME`` -- the HOME redirect above therefore does not reach it. Set
# the Windows variables too (HOMEDRIVE/HOMEPATH reconstructed from the base dir
# for completeness) so any ``os.path.expanduser`` caller is redirected.
os.environ["USERPROFILE"] = HERMETIC_BASE_DIR
_HOMEDRIVE, _HOMEPATH = os.path.splitdrive(HERMETIC_BASE_DIR)
os.environ["HOMEDRIVE"] = _HOMEDRIVE or ""
os.environ["HOMEPATH"] = _HOMEPATH or HERMETIC_BASE_DIR
os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)
pathlib.Path.home = staticmethod(
    lambda: pathlib.Path(os.environ.get("HOME", HERMETIC_BASE_DIR))
)

# ---------------------------------------------------------------------------
# Ambient git identity env vars -- scrubbed at CONFTEST IMPORT TIME.
# ---------------------------------------------------------------------------
# ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` are OPERATOR SHELL STATE, not test
# input. tools/git_write_tool.py pins them onto every commit whose workspace
# config names no ``git_identity`` (see ``_ambient_git_identity``), which is
# correct production behaviour -- but it means a developer (or CI runner, or
# agent spawner) who exports them silently rewrites the argv of EVERY commit
# the suite exercises. The exact-argv assertions in
# tests/security/test_git_info_tool_operations.py then fail on that one machine
# while passing everywhere else, which is indistinguishable from a real
# regression and costs an hour of forensics.
#
# Scrubbing here -- before any test module is imported, next to the HOME
# redirect -- makes the suite hermetic: tests that WANT an ambient identity set
# it explicitly with ``monkeypatch.setenv``
# (tests/test_git_workspace_identity.py), and tests that want none get none.
_AMBIENT_IDENTITY_ENV_VARS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
for _identity_env_name in _AMBIENT_IDENTITY_ENV_VARS:
    os.environ.pop(_identity_env_name, None)
del _identity_env_name

# ``os.path.expanduser`` is the second "~"-resolving seam: POSIX-path code
# reaches the vault via ``os.path.expanduser("~/.thoughtmachine")`` without
# going through ``pathlib.Path.home`` (patched above). Wrap it so the two
# unambiguous "~" forms always resolve to the hermetic base on every platform,
# recognising BOTH separators (so the rewrite is correct under posixpath and
# ntpath alike). Everything else (``~otheruser``, absolute paths, ...) falls
# through to the captured original unchanged, preserving its edge behaviour.
# For realistic POSIX inputs (``~``, ``~/...``) the original already returned
# the hermetic base via the redirected HOME, so this is a behavioural NO-OP on
# Linux/macOS. Re-import safe: the wrapper carries a marker and is never
# stacked (a double conftest import reuses the first captured original).
if getattr(os.path.expanduser, "_hermetic_expanduser", False):
    _SAVED_EXPANDUSER = os.path.expanduser._hermetic_original
else:
    _SAVED_EXPANDUSER = os.path.expanduser

    @functools.wraps(_SAVED_EXPANDUSER)
    def _hermetic_expanduser(path):
        """Resolve a leading ``~`` from ``$HOME`` (both separators).

        conftest redirects ``HOME`` to the hermetic base dir at import time,
        but on Windows ``ntpath.expanduser`` IGNORES ``HOME`` and reads
        ``USERPROFILE``/``HOMEDRIVE``+``HOMEPATH`` instead -- so wrap the seam
        and resolve from ``HOME`` explicitly on every platform.  Because the
        rewrite still goes through ``HOME``, tests that monkeypatch ``HOME``
        (e.g. to a tmp dir) keep seeing their value, exactly as the unwrapped
        ``posixpath.expanduser`` would.
        """
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

    _hermetic_expanduser._hermetic_expanduser = True
    _hermetic_expanduser._hermetic_original = _SAVED_EXPANDUSER
    os.path.expanduser = _hermetic_expanduser


def _restore_env_var(name, saved):
    """Restore env var *name* to *saved* (delete it when it was originally unset)."""
    if saved is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = saved


def _restore_hermetic_env():
    """Restore pre-redirect HOME / THOUGHTMACHINE_VAULT_ROOT / ``Path.home``.

    Registered via atexit at conftest import time. atexit runs handlers in
    LIFO order, and this handler is registered BEFORE the framework's
    ``_shutdown_save`` (web_ui/backend/server.py, registered when test
    collection imports the module), so it runs AFTER it. The HOME redirect
    and ``Path.home`` patch therefore stay alive for the whole process
    lifetime: the shutdown-time session save writes into the hermetic base
    dir instead of the real user vault. Idempotent; safe to call twice.
    """
    pathlib.Path.home = _SAVED_PATH_HOME
    os.path.expanduser = _SAVED_EXPANDUSER
    if _SAVED_HOME is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _SAVED_HOME
    if _SAVED_VAULT_ROOT is None:
        os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)
    else:
        os.environ["THOUGHTMACHINE_VAULT_ROOT"] = _SAVED_VAULT_ROOT
    _restore_env_var("USERPROFILE", _SAVED_USERPROFILE)
    _restore_env_var("HOMEDRIVE", _SAVED_HOMEDRIVE)
    _restore_env_var("HOMEPATH", _SAVED_HOMEPATH)


def _cleanup_hermetic_base():
    """Remove the hermetic base dir tracked by hermetic_vault_env.

    Deliberately NOT registered: base-dir removal at exit is already covered
    by the direct ``atexit.register(shutil.rmtree, HERMETIC_BASE_DIR, ...)``
    above (registered first, so it runs last at exit). Registering this too
    would duplicate cleanup; kept as a named helper for symmetry.
    """
    if _HERMETIC_BASE_DIR:
        shutil.rmtree(_HERMETIC_BASE_DIR, ignore_errors=True)


# Exit-time ordering (LIFO): _shutdown_save (registered later, during test
# collection) -> _restore_hermetic_env -> rmtree(HERMETIC_BASE_DIR).
atexit.register(_restore_hermetic_env)

# NOTE: the six-module reload loop (thoughtmachine.bootstrap,
# thoughtmachine.security, agent.knowledge.global_kb, agent.config.loader,
# agent.config.provider_profile, tools.mcp_server_connect) now lives inside
# the hermetic_vault_env fixture below, so no repo module is imported at
# conftest import time.


@pytest.fixture(scope="session")
def real_vault_paths():
    """(REAL_HOME, REAL_VAULT) captured before any HOME redirect."""
    return REAL_HOME, REAL_VAULT


# ---------------------------------------------------------------------------
# Session-wide hermetic environment.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def hermetic_vault_env():
    """Expose the import-time redirected HOME base dir for the session.

    The HOME redirect itself happens at conftest import time (see the
    “Hermetic HOME redirect — applied at CONFTEST IMPORT TIME” block above),
    so every test module binds the redirected modules consistently. This
    fixture only yields the base dir; the original HOME / vault root /
    ``Path.home`` are restored at PROCESS EXIT by the atexit-registered
    ``_restore_hermetic_env`` (registered at conftest import, before the
    framework's own atexit handlers, so it runs after them).
    """
    global _HERMETIC_BASE_DIR
    base_dir = HERMETIC_BASE_DIR
    _HERMETIC_BASE_DIR = str(base_dir)
    yield base_dir


@pytest.fixture(scope="session", autouse=True)
def hermetic_fs_guard(hermetic_vault_env):
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
# Per-test hermetic vault fixture (unchanged).
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_vault(tmp_path, monkeypatch):
    """
    Hermetic vault fixture for integration tests.

    Creates a complete, isolated vault at ``tmp_path /.thoughtmachine`` that
    complies with the design spec.  Includes all 8 required subdirectories,
    factory defaults, and user defaults, but **no** workspace-specific defaults.

    Patches:
    - ``pathlib.Path.home()`` → ``tmp_path`` (catches all code using ``~/.thoughtmachine``)
    - ``thoughtmachine.vault.vault_root()`` → vault path (catches direct callers)
    """
    # Patch Path.home() BEFORE any vault modules are imported at test time
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    # Reload modules whose module-level PATH constants are set at import time
    import thoughtmachine.bootstrap
    import agent.knowledge.global_kb
    importlib.reload(thoughtmachine.bootstrap)
    importlib.reload(agent.knowledge.global_kb)

    vault_path = tmp_path / ".thoughtmachine"
    vault_path.mkdir(parents=True, exist_ok=True)

    # 1. Create all 8 required spec directories
    for subdir in ("credentials", "global", "logs", "sessions", "state", "system", "user", "workspaces"):
        (vault_path / subdir).mkdir(parents=True, exist_ok=True)

    # 2. Write factory defaults with the exact schema expected by load_factory_defaults()
    factory_defaults = {
        "version": "1",
        "description": "System factory defaults — immutable base configuration for ThoughtMachine vault.",
        "config": {
            "max_turns": 50,
            "temperature": 0.7,
            "provider_id": "",
            "model": "",
            "system_prompt": "",
        },
    }
    (vault_path / "system" / "factory_defaults.json").write_text(
        json.dumps(factory_defaults, indent=2)
    )

    # 3. Write user defaults with minimal config
    (vault_path / "user" / "defaults.json").write_text(
        json.dumps({}, indent=2)
    )

    # 4. Monkeypatch vault_root() for code that uses it directly
    import thoughtmachine.vault
    monkeypatch.setattr(thoughtmachine.vault, "vault_root", lambda: vault_path)

    yield vault_path


# ---------------------------------------------------------------------------
# Isolation of the module-scope container-identity memos.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_container_manager_memos():
    """Isolate the module-scope identity / notes memos in ``infra.container_manager``.

    ``_NAME_INDEX`` / ``_NAME_INDEX_COLLISIONS`` / ``_NAME_INDEX_BUILT`` /
    ``_NAME_MIGRATED`` / ``_NOTES_WARNED`` / ``_NOTES_MIGRATED`` are module
    globals keyed by ``(workspace_id, name)``.  Test modules across the suite
    reuse workspace ids (``w1``, ``ws-rp``, ``ws-cm``, ...) and container names
    (``my-box``, ``n``, ...), so a stale ``_NAME_INDEX`` entry left by one test
    makes a later test with the same (workspace, name) resolve a record id that
    never existed on its own fresh per-test vault, yielding a spurious
    ``container_record_unbound`` refusal (the fake docker client is then never
    asked to run).  Clear the memos before and after every test so each test's
    identity ladder starts from a clean, disk-backed state.

    ``infra.container_manager`` is imported lazily to preserve this conftest's
    "no repo module imported at conftest import time" invariant.
    """
    import infra.container_manager as _cm

    memos = (
        _cm._NAME_INDEX,
        _cm._NAME_INDEX_COLLISIONS,
        _cm._NAME_INDEX_BUILT,
        _cm._NAME_MIGRATED,
        _cm._NOTES_WARNED,
        _cm._NOTES_MIGRATED,
    )
    for memo in memos:
        memo.clear()
    yield
    for memo in memos:
        memo.clear()

# ---------------------------------------------------------------------------
# Child-interpreter probe (Gen 50, hook-robustness wave).
#
# tests/security/test_gate_import_order.py spawns a child interpreter to
# verify import-order independence. A parent-side find_spec cannot catch the
# case where the parent imports a module only because PYTHONPATH carries it --
# the child spawned with PYTHONPATH stripped loses it, and the test fails as
# if the import order were wrong. This probe runs the child check itself.
# ---------------------------------------------------------------------------

_REQUIRED_CHILD_MODULES = ("pydantic",)


def _probe_child_import(module, env):
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            env=env, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pytest_configure(config):
    if os.environ.get("THOUGHTMACHINE_SKIP_CHILD_PROBE") == "1":
        return
    env_with = os.environ.copy()
    env_without = {k: v for k, v in env_with.items() if k != "PYTHONPATH"}
    for module in _REQUIRED_CHILD_MODULES:
        if _probe_child_import(module, env_without):
            continue
        if _probe_child_import(module, env_with):
            warnings.warn(
                f"{module!r} is importable in a child interpreter only via "
                f"PYTHONPATH. Tests that strip PYTHONPATH will fail. "
                f"Run the suite under a venv interpreter ({sys.prefix}).",
                stacklevel=1,
            )
        else:
            raise pytest.UsageError(
                f"{module!r} is not importable in the child interpreter "
                f"({sys.executable}). The test suite requires it. "
                f"Activate the venv or install dev deps."
            )





# ---------------------------------------------------------------------------
# C60: provider-registry isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_provider_registry():
    """C60: no test may leave the provider registry mutated for the next one."""
    from llm_providers.factory import ProviderFactory

    saved = None if ProviderFactory._providers is None else dict(ProviderFactory._providers)
    try:
        yield
    finally:
        ProviderFactory._providers = saved


# ---------------------------------------------------------------------------
# Thread / bridge census: reap what a test spawns, then count what is left.
#
# Nothing in the suite used to end what it started.  A test that spawns a
# ``WorkerThread`` leaves a live thread and its registry entry behind; a test
# that builds a ``WebAgentBridge`` leaves that bridge subscribed to the
# process-global ``EventBus`` and to every per-worker bus it discovered.  The
# leak is invisible to the test that causes it and expensive for every test
# after it in the same process: one ``WORKER_SPAWNED`` publish is dispatched
# once per bridge the process has ever built, so a module's cost grows with the
# number of tests that ran before it.
#
# This fixture makes that VISIBLE, per test:
#
#   * before -- snapshot the worker-registry key set, the live-thread set and
#     (when the census is enforced) the subscriber counts of the global bus
#     plus every registered per-worker bus;
#   * after  -- reap what the test created.  ``kill()``, never ``stop()``:
#     ``WorkerThread.stop`` is an alias for ``detach``
#     (tools/workspace/worker_thread.py:2796) and leaves the thread ALIVE and
#     parked, so a reaper built on ``stop()`` would reap nothing.  ``kill()``
#     (same file, :2856) sets the latch the run loop is waiting on; the
#     ``join`` is bounded.  Bridges the test constructed are detached through
#     the bridge's real teardown path, ``WebAgentBridge.stop()``
#     (web_ui/backend/bridge.py:2288), which is what ``close_session`` and the
#     server shutdown path call;
#   * then   -- assert the census (opt-in, see ``_CENSUS_MODULES``).
#
# The reaping half is autouse and unconditional and never fails a test (it
# cannot: it never raises, and the census is skipped when the test already
# failed).  The assertion half is opt-in so the harness can land without
# turning a pre-existing leak into somebody else's red build:
# ``_CENSUS_MODULES`` names the modules that are green with it enforced today,
# and a test can also opt in with ``@pytest.mark.thread_census``.
# ---------------------------------------------------------------------------

_REAP_JOIN_TIMEOUT = 5.0

# Modules (``request.module.__name__``) that opt in to the hard census
# assertion.  Measured, not guessed: see the commit body for the blast radius
# of enforcing it everywhere.
_CENSUS_MODULES = frozenset({
    "tests.test_event_bus_subscription_leak",
})

_CENSUS_MARKER = "thread_census"

# Memoized: the symbol check imports three repo modules, so it runs once.
_CENSUS_SYMBOLS_OK = False

# Set by the ``pytest_runtest_makereport`` hook below: the call-phase report of
# the test being torn down, so the census can stay quiet when the test itself
# already failed (a second, confusing failure would bury the first).
_CENSUS_CALL_REPORT = pytest.StashKey()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if call.when == "call":
        item.stash[_CENSUS_CALL_REPORT] = report
    return report


def _census_registry():
    """The process-global ``WorkerRegistry`` singleton."""
    from tools.workspace.worker_registry import WorkerRegistry

    return WorkerRegistry.get_instance()


def _census_buses():
    """Every bus whose subscriber lists the census reads.

    The process-global bus plus every per-worker bus the registry still holds.
    Strong references are kept so the "after" counts are read off the same
    objects: a bus that was merely dropped leaks no subscriber, and it must not
    look like a leak just because it left the registry.
    """
    from agent.events import global_event_bus

    buses = {"global": global_event_bus}
    try:
        registry = _census_registry()
        with registry._bus_registry_lock:
            per_worker = dict(registry._worker_event_bus_registry)
    except Exception:
        per_worker = {}
    for key, bus in per_worker.items():
        buses["worker%r" % (tuple(key),)] = bus
    return buses


def _census_bus_counts(buses):
    """{bus label: ({event type value: subscriber count}, wildcard count)}."""
    counts = {}
    for label, bus in buses.items():
        subscribers = getattr(bus, "_subscribers", None)
        row = {}
        if isinstance(subscribers, dict):
            for event_type, callbacks in list(subscribers.items()):
                row[getattr(event_type, "value", str(event_type))] = len(callbacks)
        counts[label] = (row,
                         len(getattr(bus, "_wildcard_subscribers", None) or ()))
    return counts


def _census_threads():
    return {id(thread): thread for thread in threading.enumerate()}


def _census_is_worker_thread(obj):
    """Duck-typed ``WorkerThread`` check.

    ``tests/tools/test_worker_registry.py`` registers plain strings and
    ``MagicMock``s in the registry, so a reaper that trusted the registry alone
    would end up calling ``.kill()`` on a ``str``.
    """
    return (
        isinstance(obj, threading.Thread)
        and callable(getattr(obj, "kill", None))
        and hasattr(obj, "worker_name")
    )


def _census_symbols():
    """Fail loudly if a symbol the harness reaps through was renamed.

    A reaper that silently reaps nothing is worse than no reaper: it would keep
    the census green while the leak kept growing.
    """
    from tools.workspace.worker_registry import WorkerRegistry
    from tools.workspace.worker_thread import WorkerThread
    from web_ui.backend.bridge import WebAgentBridge

    required = (
        (WorkerThread, "kill"),
        (WorkerRegistry, "get_instance"),
        (WorkerRegistry, "get_all_workers"),
        (WebAgentBridge, "stop"),
        (WebAgentBridge, "_unsubscribe_security_events"),
        (WebAgentBridge, "_unsubscribe_worker_events"),
    )
    missing = ["%s.%s" % (owner.__name__, name)
               for owner, name in required
               if not callable(getattr(owner, name, None))]
    if missing:
        pytest.fail(
            "thread/bridge census harness: required symbol(s) missing: "
            + ", ".join(missing)
            + " -- the reaper would silently reap nothing; fix the harness.",
            pytrace=False,
        )
    return WebAgentBridge


def _reap_bridges(bridges):
    """Detach every bridge the test built, through its real teardown path."""
    for bridge in bridges:
        try:
            bridge.stop()
        except Exception as exc:
            # A reaper may never mask the test's own failure, so it reports and
            # keeps going rather than propagating.
            print("thread census: bridge.stop() failed for %r: %s" % (bridge, exc),
                  file=sys.stderr)
            for name in ("_unsubscribe_security_events",
                         "_unsubscribe_worker_events"):
                try:
                    getattr(bridge, name, lambda: None)()
                except Exception:
                    pass


def _reap_worker_threads(threads_before, keys_before):
    """``kill()`` and bounded-``join`` every worker thread the test left behind.

    Candidates are the worker threads registered under a key the test added and
    every worker thread that is alive now but was not alive before the test.
    Returns the threads still alive after the bounded join.
    """
    candidates = []
    try:
        registered = _census_registry().get_all_workers()
    except Exception:
        registered = {}
    for key, thread in registered.items():
        if key not in keys_before and _census_is_worker_thread(thread):
            candidates.append(thread)
    for ident, thread in _census_threads().items():
        if ident in threads_before or any(thread is c for c in candidates):
            continue
        if _census_is_worker_thread(thread):
            candidates.append(thread)

    survivors = []
    for thread in candidates:
        try:
            thread.kill()
        except Exception as exc:
            print("thread census: kill() failed for %r: %s" % (thread, exc),
                  file=sys.stderr)
        try:
            thread.join(_REAP_JOIN_TIMEOUT)
        except Exception as exc:
            print("thread census: join() failed for %r: %s" % (thread, exc),
                  file=sys.stderr)
        try:
            if thread.is_alive():
                survivors.append(thread)
        except Exception:
            pass
    return survivors


def _census_enforced(request):
    name = getattr(getattr(request, "module", None), "__name__", "")
    if name in _CENSUS_MODULES:
        return True
    return request.node.get_closest_marker(_CENSUS_MARKER) is not None


def _census_fail(problems):
    listed = "\n".join("  - " + line for line in problems[:20])
    more = "" if len(problems) <= 20 else "\n  ... and %d more" % (len(problems) - 20)
    pytest.fail(
        "thread/bridge census: this test left process-global state behind "
        "(the autouse reaper already ran, so these survived ``kill()`` + "
        "%.1fs join and ``WebAgentBridge.stop()``):\n" % _REAP_JOIN_TIMEOUT
        + listed + more,
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def _thread_bridge_census(request):
    """Reap the threads and bridges a test leaves; opt-in, assert a census."""
    global _CENSUS_SYMBOLS_OK
    if _CENSUS_SYMBOLS_OK:
        from web_ui.backend.bridge import WebAgentBridge as bridge_class
    else:
        bridge_class = _census_symbols()
        _CENSUS_SYMBOLS_OK = True

    enforced = _census_enforced(request)
    threads_before = _census_threads()
    try:
        keys_before = set(_census_registry().get_all_workers())
    except Exception:
        keys_before = set()
    buses = _census_buses() if enforced else {}
    bus_counts_before = _census_bus_counts(buses) if enforced else {}

    bridges_created = []
    original_init = bridge_class.__init__

    def _census_tracking_init(self, *args, **kwargs):
        # Recorded BEFORE the call: a bridge whose __init__ raises has already
        # subscribed to the global bus in _subscribe_to_security_events.
        bridges_created.append(self)
        original_init(self, *args, **kwargs)

    bridge_class.__init__ = _census_tracking_init

    try:
        yield
    finally:
        # Patched and restored by hand, deliberately NOT via ``monkeypatch``:
        # this fixture is autouse, so requesting ``monkeypatch`` would create it
        # before every module/tests-level autouse fixture and push its undo
        # *after* their teardown -- a teardown re-reading a global the test
        # patched would then see the patched garbage.  Restore FIRST here, before
        # the reaping and before any assertion, so no fixture tearing down
        # alongside this one ever observes the tracking ``__init__``.
        try:
            if bridge_class.__init__ is _census_tracking_init:
                bridge_class.__init__ = original_init
        except Exception as exc:
            print("thread census: __init__ restore failed: %s" % (exc), file=sys.stderr)
        try:
            _reap_bridges(bridges_created)
        except Exception as exc:
            print("thread census: bridge reaping failed: %s" % (exc), file=sys.stderr)
        survivors = []
        try:
            survivors = _reap_worker_threads(threads_before, keys_before)
        except Exception as exc:
            print("thread census: thread reaping failed: %s" % (exc), file=sys.stderr)

        if not enforced:
            return
        report = request.node.stash.get(_CENSUS_CALL_REPORT, None)
        if report is not None and report.failed:
            # The test already failed; a second failure here would bury it.
            return

        problems = []
        try:
            keys_now = set(_census_registry().get_all_workers())
        except Exception:
            keys_now = set(keys_before)
        for key in sorted(keys_now - keys_before, key=repr):
            problems.append("worker registry still holds %r" % (key,))
        for key in sorted(keys_before - keys_now, key=repr):
            problems.append("worker registry lost the pre-test entry %r" % (key,))
        for ident, thread in _census_threads().items():
            if ident not in threads_before:
                problems.append("thread still alive: %r (%r)" % (thread.name, thread))
        for thread in survivors:
            problems.append("worker thread survived kill()+join: %r" % (thread,))
        bus_counts_now = _census_bus_counts(buses)
        for label in sorted(bus_counts_before):
            row_before, wildcard_before = bus_counts_before[label]
            row_now, wildcard_now = bus_counts_now.get(label, ({}, 0))
            for event_type in sorted(row_now):
                count = row_now[event_type]
                if count > row_before.get(event_type, 0):
                    problems.append(
                        "bus %s gained subscribers for %r: %d -> %d"
                        % (label, event_type, row_before.get(event_type, 0), count)
                    )
            if wildcard_now > wildcard_before:
                problems.append(
                    "bus %s gained wildcard subscribers: %d -> %d"
                    % (label, wildcard_before, wildcard_now)
                )
        if problems:
            _census_fail(problems)
