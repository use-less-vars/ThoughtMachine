"""Repository-root test configuration.

This file lives at the rootdir on purpose.  ``pyproject.toml`` sets
``testpaths = ["tests", "web_ui/backend/tests"]`` and pytest's ``confcutdir``
defaults to the rootdir, so a conftest here is loaded for *every* test in the
project -- there was no such file before, and the alternative (the same fixture
copied into ``tests/conftest.py`` and ``web_ui/backend/tests/conftest.py``)
would leave the next test directory to be added uncovered.

It does two things.

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

import importlib.machinery
import sys

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
    """Enable ``auth_boundary.TRUST_TEST_HARNESS`` for the whole session.

    Session-scoped and autouse: the allowance is process-wide, so the fixture
    that grants it has to outlive every module, and no test should have to
    remember to ask for it.  The value is recorded in ``_suite_trust`` (which
    the import hook re-applies to any re-imported ``auth_boundary``), and the
    previous value is restored on the way out, which keeps the flip a property
    of "a test session was running" rather than of "this process".
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    set_suite_trust_value(True)
    auth_boundary.enable_test_harness_trust(True)
    try:
        yield
    finally:
        auth_boundary.enable_test_harness_trust(previous)
        set_suite_trust_value(previous)
