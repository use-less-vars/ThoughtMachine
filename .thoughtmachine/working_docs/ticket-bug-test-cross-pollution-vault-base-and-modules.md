# bug/test-cross-pollution-vault-base-and-modules

**Filed:** 2026-10-21
**Severity:** LOW
**Status:** filed

## Finding

Two pre-existing cross-test polluters found while fixing test order-independence
in PR 2 (`fix/boundary-layer-ingress`). Both predate the branch and neither is
fixed there.

1. **Vault-base leak at `tests/conftest.py:133`.** The `hermetic_vault` fixture
   does not restore the base vault path on teardown; tests that follow see the
   previous test's base.

2. **`sys.modules` purger for `web_ui.backend.server`** — purger not yet
   identified. A peer test purges the module from `sys.modules`, so any test
   that follows a specific ordering sees a re-imported server with fresh
   module-level state.

## Why it matters

Both are order-dependent full-suite failures waiting to happen. They won't
show on a single test run; they show when the suite grows or runs in a
different order. The class is "fixture leaks across the isolation boundary."

## Disposition

Not this wave. Fix needs its own PR; the conftest.py:133 fix is a one-line
teardown, the sys.modules purger needs identification first.
