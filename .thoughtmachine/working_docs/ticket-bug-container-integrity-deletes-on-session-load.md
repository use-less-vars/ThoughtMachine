# Ticket: bug/container-integrity-deletes-on-session-load

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** Wave 9 read-first (spare agent). Root cause named; verdict one-pass.
**Severity:** High — silent, irreversible destruction of a workspace-owned
persistent container and its in-flight job.
**Status:** filed, not dispatched. Blocked on verification read.

## Defect

`verify_container_integrity()` in `agent/presenter/session_lifecycle.py`
is called on **both** `load_session` and `load_session_by_id` — i.e. at
every new query. When the container's network mode or `/workspace` mount
mode no longer matches the current permission envelope, it **stops and
removes** the container.

Both call sites are wrapped in bare `except Exception: pass`. Silent.

## Why this is the turn-start reap

The timing is exact: session load fires on the incoming query, which is
what the field report measured (0.567s / 0.778s before the next query).
The sweep that Wave 9's H1 pointed at hard-skips non-exited containers
and is cleared.

## Correct behavior

The container trust contract says a session may *use* a container iff
the envelope fits. On mismatch, the correct behavior is to **refuse use**
— not to destroy the container. A workspace-owned persistent container
is not the session's property to reap.

## Fix shape (not written; for Main's brief)

1. On envelope mismatch: refuse use, log an event, surface in the panel.
2. Remove the `remove(force=True)` / `stop()` in this path.
3. Remove the bare `except: pass` — the refusal must be visible.
4. Enumerate sibling call sites that stop/remove a container on a
   session boundary. Fix-reveals-siblings rule applies.

## Not established

- Whether every caller of `verify_container_integrity` behaves the same.
- Whether the envelope-mismatch condition is common or rare in practice.
- Whether any other session-load path reaps.

## Blocked on

Verification read — confirm this is the turn-start reap and enumerate
sibling sites. No fix on one-pass evidence (Main's rule).

