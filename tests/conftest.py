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

