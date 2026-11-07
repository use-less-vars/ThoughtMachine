# Ticket: hygiene/FakeManager-signature

**Filed:** Gen 45, 2026-10-17, from the P0 fixes wave.
**Severity:** low — the reason fix 4 shipped untested.
**Status:** filed.

## Finding

`tests/test_container_record_user_action_routes.py::FakeManager` defines
`remove(*args, **kwargs)`. This accepted the missing `keep_record=`
kwarg silently, which is *why* the bare `manager.remove(container_id)` in
`server.py:4654` passed tests — the bug that caused P7's 404.

## Fix

Tighten `FakeManager.remove` to an explicit signature:
`def remove(self, container_id: str, *, keep_record: bool = False) -> dict`.
Signature drift then fails visibly.

## Generalisation

Any test double that accepts `**kwargs` on a method with keyword-only
params is a silent-mismatch surface. Worth a sweep of `tests/` for the
pattern.
