"""Pin test: a ``sys.modules`` purge must be paired with a full restore.

This module is the regression pin for the last Class A change of the
sys.modules-leak fix.

Background / the hole being pinned
----------------------------------
``tests/integration/test_concurrent_sessions.py::mock_server`` purges a set of
app modules from ``sys.modules`` so that a fresh (MockProvider-backed) server
import happens inside the fixture.  The old code did this with a bare
``del sys.modules[name]`` loop and never restored anything, so after the
fixture tore down:

* the purged modules were *gone* from ``sys.modules`` (a later
  ``importlib.reload`` on a still-referenced object then fails with
  ``ImportError: module <name> not in sys.modules``); and
* the ``package.submodule`` attributes on the parent packages still pointed at
  whatever objects happened to be bound, while ``sys.modules`` disagreed --
  cross-test pollution.

The fix replaces the bare delete with the shared
``tests.integration.harness.SysModulesSnapshot`` helper: ``snapshot()`` before
the purge, ``purge()`` to drop modules AND their parent-package attributes,
and ``restore()`` on teardown so the process is left exactly as it was found.

The two pins below
------------------
1. ``test_sysmodules_snapshot_restores_identity_and_absent_attrs`` -- a
   deterministic unit pin on a synthetic package/child built at runtime: it
   checks that ``purge()`` removes the child from ``sys.modules`` and from its
   parent package, that ``restore()`` reinstates both with identity
   ``getattr(parent, leaf) is sys.modules[name]``, and that an attribute that
   was absent before the snapshot stays absent afterwards.

2. ``test_mock_server_teardown_restores_original_modules`` -- an end-to-end pin
   on the real ``mock_server`` fixture *site*.  The fixture's generator
   (``mock_server.__wrapped__``) is driven explicitly through setup AND
   teardown, and the invariants are asserted afterwards.  Driving the
   generator is what makes the pin deterministic from a *separate* module:
   pytest's own teardown of the module-scoped fixture cannot be observed from
   another module, so we run the exact same code path here instead.  Under the
   old behaviour (bare ``del`` + no restore) the post-teardown ``sys.modules``
   entries were the fixture's fresh objects (or missing entirely), so the
   identity / consistency assertions below fail.

Style note: the source deliberately avoids non-ASCII characters.
"""

from __future__ import annotations

import importlib
import sys
import types

from tests.integration.harness import SysModulesSnapshot
from tests.integration.test_concurrent_sessions import mock_server

# The same prefix tuple the ``mock_server`` fixture purges.
_MOD_PREFIXES = (
    "web_ui.backend",
    "agent.config.provider_profile",
    "thoughtmachine.bootstrap",
)


def _matches_prefixes(name):
    """Mirror of ``SysModulesSnapshot._matched`` for a single module name."""
    return any(name == p or name.startswith(p + ".") for p in _MOD_PREFIXES)


def _fixture_func(fixture_obj):
    """Return the plain generator function wrapped by a pytest fixture."""
    func = getattr(fixture_obj, "__wrapped__", None)
    if func is None:
        func = fixture_obj._get_wrapped_function()
    return func


def _drive_fixture_once(fixture_obj):
    """Run a generator fixture's setup AND teardown, returning its value.

    ``fixture_obj`` is the pytest fixture.  By the time this returns every
    teardown side effect has been applied.
    """
    gen = _fixture_func(fixture_obj)()
    value = next(gen)
    sentinel = object()
    try:
        nxt = next(gen)
    except StopIteration:
        nxt = sentinel
    assert nxt is sentinel, "fixture yielded more than once"
    return value


def test_sysmodules_snapshot_restores_identity_and_absent_attrs():
    pkg_name = "_tm_pin_synth_pkg"
    child_name = "_tm_pin_synth_pkg.child"
    absent_name = "_tm_pin_synth_pkg.absent_child"

    pkg = types.ModuleType(pkg_name)
    child = types.ModuleType(child_name)
    absent_child = types.ModuleType(absent_name)

    sys.modules[pkg_name] = pkg
    sys.modules[child_name] = child
    sys.modules[absent_name] = absent_child
    pkg.child = child
    # Deliberately do NOT bind ``pkg.absent_child``: it is absent-before.

    try:
        snap = SysModulesSnapshot((child_name, absent_name))
        snap.snapshot()
        snap.purge()

        # purge drops the matched children from sys.modules ...
        assert child_name not in sys.modules
        assert absent_name not in sys.modules
        # ... and removes the parent-package attribute that held the child.
        assert not hasattr(pkg, "child")
        assert not hasattr(pkg, "absent_child")
        # The parent package itself is untouched (it was not matched).
        assert sys.modules[pkg_name] is pkg

        # Simulate a fresh import rebinding a NEW object over the stale name.
        new_child = types.ModuleType(child_name)
        sys.modules[child_name] = new_child
        pkg.child = new_child
        assert sys.modules[child_name] is new_child

        snap.restore()

        # restore reinstates the ORIGINAL objects (identity preserved) ...
        assert sys.modules[child_name] is child
        assert sys.modules[absent_name] is absent_child
        # ... and keeps the parent-package attribute in lockstep with
        # sys.modules -- the exact invariant a bare ``del`` violates.
        assert getattr(pkg, "child") is sys.modules[child_name]
        # An attribute that was absent before the snapshot stays absent.
        assert not hasattr(pkg, "absent_child")
    finally:
        for name in (pkg_name, child_name, absent_name):
            sys.modules.pop(name, None)
        pkg.__dict__.pop("child", None)


def test_mock_server_teardown_restores_original_modules():
    # Establish a known baseline: import the module the fixture centres on, then
    # remember every prefix-matching ``sys.modules`` entry and its object.
    importlib.import_module("web_ui.backend.server")
    before_modules = {
        n: sys.modules[n]
        for n in list(sys.modules)
        if _matches_prefixes(n)
    }
    assert "web_ui.backend.server" in before_modules

    value = _drive_fixture_once(mock_server)
    assert isinstance(value, tuple) and len(value) == 2

    # Every baseline module must be back, by IDENTITY.
    missing = [n for n in before_modules if n not in sys.modules]
    assert missing == [], f"fixture teardown lost sys.modules entries: {missing}"
    for name, mod in before_modules.items():
        assert sys.modules[name] is mod, (
            f"{name}: teardown left a different object in sys.modules"
        )

    # ... and no module the fixture imported fresh may survive teardown.
    after_names = {n for n in sys.modules if _matches_prefixes(n)}
    extra = after_names - set(before_modules)
    assert extra == set(), f"fixture teardown left NEW modules behind: {sorted(extra)}"

    # The parent-package attributes must agree with sys.modules (identity in
    # lockstep) -- exactly what a bare ``del`` + no restore broke.
    for name in before_modules:
        parent_name, _, leaf = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, leaf):
            assert getattr(parent, leaf) is sys.modules[name], (
                f"{name}: parent attribute disagrees with sys.modules"
            )

    # A reload of the restored server module must succeed; the old code failed
    # this with ``ImportError: module ... not in sys.modules``.
    server_mod = before_modules["web_ui.backend.server"]
    assert sys.modules.get(server_mod.__name__) is server_mod
    importlib.reload(server_mod)
