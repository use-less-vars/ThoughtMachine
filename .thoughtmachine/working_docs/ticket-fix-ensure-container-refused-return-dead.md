# Ticket: fix/ensure-container-refused-return-dead

**Filed:** Gen 44, 2026-10-15, from T1b worker report §8.
**Severity:** Low — observability gap.
**Status:** filed, not dispatched.

## Finding

T1b's new `{"status":"refused", ...}` return from `_ensure_container` is
**dead**. Both in-module callers (`docker_executor.py:987`, `:1065`) discard
the return value; `_ensure_container` previously returned `None` on every
path.

Consequence: the refusal is only observable via the WARNING log and the
`CONTAINER_DRIFT_REFUSED` audit event. If callers should act on it, wire
them. If not, document that the return is diagnostic-only.

## MUST NOT TOUCH
Standard list.
