# Ticket: hygiene/catalog-count-drift

**Filed:** Gen 48, after W2 credential grain landed (935f0ad0).
**Verified at:** web_ui/backend/tests/test_permission_routes.py:516 fixed (CATALOG_DEFAULTS); other prose-only sites remain.
**Severity:** LOW — stale comments and test names, no behaviour.
**Status:** filed, not dispatched. **Do NOT fix in a behaviour PR** — test node ID renames need their own wave.

## Sites

1. `thoughtmachine/security.py:205` — "exactly the six session resources"; `:114` says "exactly eight" in the same file. Self-contradiction.
2. `web_ui/backend/tests/test_security_coercion.py:30` — "exactly six keys" comment, green.
3. `test_resource_catalog_nine_resources_with_exact_keys` — test name says nine; asserts 10.
4. Other "seven-key"/"six-key"/"9 entries" docstrings flagged by the W2 sweep (see commit f45f6127 message for list).

## Why not in a behaviour PR

Test node IDs are user-visible — renaming breaks CI dashboards and `-k` filters. Do all renames in one commit in a naming wave, not per-fix.

## Bearing

LOW. Prose-only. But a docstring that lies about the contract is the `declared-but-not-enforced` class at the documentation layer — same family as the 50 NONE rows and the gitignored working_docs. Names matter.
