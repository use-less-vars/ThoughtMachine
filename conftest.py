"""Repository-root test configuration.

This file lives at the rootdir on purpose.  ``pyproject.toml`` sets
``testpaths = ["tests", "web_ui/backend/tests"]`` and pytest's ``confcutdir``
defaults to the rootdir, so a conftest here is loaded for *every* test in the
project -- there was no such file before, and the alternative (the same fixture
copied into ``tests/conftest.py`` and ``web_ui/backend/tests/conftest.py``)
would leave the next test directory to be added uncovered.

It does three things.

1. It hands the auth boundary the in-process harness allowance the pre-existing
   suite needs.  ``web_ui/backend/auth_boundary.py`` refuses every request it
   cannot authenticate, and a very large part of this suite drives the *real*
   app (``web_ui.backend.server.app``) through Starlette's ``TestClient``
   without a credential; the harness peer address is what lets those tests keep
   testing what they were written to test.  The flag is ``False`` on import --
   the production value, the value a fresh interpreter sees -- and is put back
   afterwards so nothing done here can leak into a test that asserts the
   default.

2. It keeps that allowance alive across the suite's own ``sys.modules`` purge,
   which is the only reason the ``sys.meta_path`` hook below exists.

3. It lets a single test *ask* for that allowance by name, with
   ``@pytest.mark.trust_harness``, so the session-wide grant can later shrink to
   the tests that actually need it without those tests being rewritten first.
   The marker changes nothing on its own: the ``autouse`` fixture at the foot
   of this file turns the flag on for a marked test and puts back what it
   found.

Why the hook is needed
----------------------
``tests/integration/harness.py`` (``SysModulesSnapshot`` / ``purged_sys_modules``
/ ``_purge_modules``) deletes every ``web_ui.backend*`` entry from ``sys.modules``
and re-imports the app so each test gets a cold start.  The trust flag lives
*inside* ``web_ui.backend.auth_boundary``, so every such purge throws the
allowance away: the re-imported module runs its module body, which assigns the
shipped default ``False``.  Nothing re-applies the suite's value, so the fresh
app refuses the harness peer and the test sees a 401 it did not ask for.

The flag itself is deliberately kept (it is defence in depth behind the
proxy-header refusal, and it is what makes the *negative* direction testable at
all), so the fix belongs at the seam where the module comes back: one
``sys.meta_path`` finder, matching exactly ``web_ui.backend.auth_boundary``,
whose loader runs the real module body and then re-applies the value the suite
is currently using to the *new* module object.  A guard that silently stops
being applied after a re-import is worse than no guard at all, so this is
pinned by
``web_ui/backend/tests/test_auth_boundary.py::test_a_freshly_imported_boundary_carries_the_suite_trust_value``.
"""

import gc
import importlib.machinery
import sys
import types

import pytest

from web_ui.backend import auth_boundary

#: The one module whose flag is re-applied.  Nothing else is touched: the hook
#: must not become a general-purpose import interceptor.
_TARGET_MODULE = "web_ui.backend.auth_boundary"

#: The single authoritative value: what the suite is currently using.  It lives
#: here, outside every purge prefix, so a purge cannot throw it away.
_suite_trust = {"value": False}


def suite_trust_value() -> bool:
    """The trust value the suite is currently using (the hook re-applies this)."""
    return _suite_trust["value"]


def set_suite_trust_value(enabled: bool) -> None:
    """Record the suite's trust value; the next re-import will be given it."""
    _suite_trust["value"] = bool(enabled)


def _remembering(enabled_by):
    """Wrap ``enable_test_harness_trust`` so a flip also updates the authority.

    Every caller (the session fixture, ``_harness_trust`` in the tests) goes
    through the module-level function, so recording the value there is what
    keeps the hook in step with a test that deliberately turns trust *off*: a
    module re-imported inside that block must come back OFF, not True.
    """

    def enable_test_harness_trust(enabled: bool = True) -> None:
        enabled_by(enabled)
        set_suite_trust_value(enabled)

    enable_test_harness_trust.__doc__ = enabled_by.__doc__
    return enable_test_harness_trust


def _apply_suite_trust(module) -> None:
    """Give *module* the suite's current value, and a setter that records."""
    module.enable_test_harness_trust(suite_trust_value())
    module.enable_test_harness_trust = _remembering(module.enable_test_harness_trust)


class _TrustRestoringLoader:
    """A real loader, plus: re-apply the suite value once the body has run.

    Wrapping (rather than re-implementing) the loader keeps source loading,
    ``__file__``/``__spec__``/``__loader__`` metadata, ``create_module``
    semantics and ``importlib.reload`` exactly as they were: the wrapped loader
    does all the work, and only the value of one global is written afterwards.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        return None if create is None else create(spec)

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _apply_suite_trust(module)

    def __getattr__(self, name):  # get_filename, get_source, path_mtime, ...
        return getattr(self._wrapped, name)


class _TrustRestoringFinder:
    """``sys.meta_path`` finder for exactly one module name.

    Delegates the real work to ``importlib.machinery.PathFinder`` so the
    normal filesystem search (and its error behaviour) is untouched; every
    other module name returns ``None`` and is handled by the rest of
    ``sys.meta_path`` as before.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _TARGET_MODULE:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _TrustRestoringLoader(spec.loader)
        return spec


def _install_finder() -> None:
    """Put the finder on ``sys.meta_path`` once, however often this file loads."""
    if any(isinstance(finder, _TrustRestoringFinder) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _TrustRestoringFinder())


#: The module conftest itself imported above is the one every test module gets,
#: so it needs the recording setter too, or the first flip of the session would
#: not be recorded.
_apply_suite_trust(auth_boundary)
_install_finder()


@pytest.fixture(scope="session", autouse=True)
def _trust_the_in_process_asgi_harness():
    """Establish the CLOSED default for the whole session.

    Session-scoped and autouse: the fixture that establishes the default has
    to outlive every module, and no test should have to remember to opt out.
    The value is recorded in ``_suite_trust`` (which the import hook
    re-applies to any re-imported ``auth_boundary``), and the previous value
    is restored on the way out, which keeps the closed default a property of
    "a test session was running" rather than of "this process".  A test that
    needs the allowance asks for it with ``@pytest.mark.trust_harness``.
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    # OPT-IN per test now: a test asks for the allowance with
    # @pytest.mark.trust_harness (the function-scoped marker fixture at the
    # foot of this file).  The session no longer grants it to everything;
    # the capture/restore of `previous` below is unchanged.
    set_suite_trust_value(False)
    auth_boundary.enable_test_harness_trust(False)
    try:
        yield
    finally:
        auth_boundary.enable_test_harness_trust(previous)
        set_suite_trust_value(previous)


# ── the opt-in marker: a test that asks for the allowance by name ─────────────
#
# The session fixture above establishes the closed default and grants nothing.  A
# test that would rather state the dependency itself -- so the allowance can
# later stop being session-wide without every such test being rewritten -- says
# so with ``@pytest.mark.trust_harness``.  The autouse fixture below is the
# whole mechanism: it turns the flag on for a marked test and puts back the
# value that was in force beforehand.
#
# A fixture rather than the setup-hook/teardown-hook pair this used to be, and
# the difference is the whole point.  A hook is not a fixture: the old
# ``pytest_runtest_setup(tryfirst=True)`` ran *before* the session-scoped
# fixture above was instantiated, so on the first marked test of a session it
# read the shipped default ``False`` and remembered that, and teardown then
# restored ``False`` through the recording setter -- which wrote
# ``_suite_trust=False``.  The import hook applies ``_suite_trust`` to every
# re-imported boundary, so from that moment on every later
# ``*_survives_restart`` test got a disarmed boundary and a 401 it did not ask
# for.  ``autouse`` needs no test to name it, so the old "a hook still runs for
# a test that names no fixture" advantage is void; and a function-scoped
# fixture is ordered *after* the session-scoped one, so by the time it runs the
# allowance is in force and what it remembers is ``True``.  A ``finally``
# around the ``yield`` also runs when the test body fails (and when a later
# fixture's setup fails), so the restore happens on the failure path too -- the
# old hook teardown was simply never reached when setup failed, which is why
# the value is remembered and restored rather than assumed to be the shipped
# default.


def pytest_configure(config):
    """Declare the marker, so using it is not a warning."""
    config.addinivalue_line(
        "markers",
        "trust_harness: request the in-process ASGI harness allowance "
        "(``auth_boundary.TRUST_TEST_HARNESS``) for this test",
    )


def _live_boundary():
    """The ``auth_boundary`` module a *test* would get right now.

    Deliberately not the module-level ``auth_boundary`` name above: that binding
    was frozen when this file was imported, and the suite's ``sys.modules`` purge
    replaces the module object, so arming it alone arms an object no test resolves.
    """
    live = sys.modules.get(_TARGET_MODULE)
    if live is None:
        live = importlib.import_module(_TARGET_MODULE)
    return live


def _boundary_modules():
    """Every ``auth_boundary`` module object this process still holds.

    Not just what ``sys.modules`` answers with: a purge drops the *entry*,
    not the object, and whatever was imported through the old object keeps
    it alive -- the app under test may be driving exactly that object's
    functions.  Arming only the module the next import would return leaves
    that app disarmed, so a marked test that runs after any purge gets the
    401 it never asked for.  Module objects are tracked by the cycle
    collector, so the heap is the authoritative list of every copy that can
    still answer a request.
    """
    found = []
    for candidate in [_live_boundary(), auth_boundary, *gc.get_objects()]:
        if not isinstance(candidate, types.ModuleType):
            continue
        if getattr(candidate, "__name__", None) != _TARGET_MODULE:
            continue
        if not hasattr(candidate, "enable_test_harness_trust"):
            continue
        if any(candidate is seen for seen in found):
            continue
        found.append(candidate)
    return found


@pytest.fixture(autouse=True)
def _harness_trust_for_marked_tests(request):
    """Grant the allowance to a test carrying ``@pytest.mark.trust_harness``.

    Strict no-op for every other test: an unmarked test must not have its flag
    touched, or the negative direction of the boundary stops being testable.

    ``autouse`` so a marked test need not name it, and function-scoped so it is
    set up *after* the session-scoped fixture above: what it remembers is the
    value in force, not the shipped default.  Both ends arm every copy, not
    just the module the next import would return, because the app a test drives
    may be driving a copy a purge left behind.
    """
    if request.node.get_closest_marker("trust_harness") is None:
        yield
        return

    modules = _boundary_modules()
    previous = [(module, module.TRUST_TEST_HARNESS) for module in modules]
    previous_suite = suite_trust_value()
    try:
        set_suite_trust_value(True)
        for module in modules:
            module.enable_test_harness_trust(True)
        yield
    finally:
        for module, armed in previous:
            module.enable_test_harness_trust(armed)
        set_suite_trust_value(previous_suite)
