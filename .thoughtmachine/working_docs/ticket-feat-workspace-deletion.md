# Ticket: feat/workspace-deletion

**Filed:** Gen 44, 2026-10-15, operator request (friend asked: "can workspaces be
deleted?"; answer: currently no).
**Severity:** Medium — a missing capability; workspace lifetime has no end.
**Status:** filed, not dispatched. Awaits the container lifecycle contract.
**Bearing:** sibling of the container lifecycle contract. Same shape.

## What's missing

A workspace has a beginning (create) but no end. Once created, its
`.thoughtmachine/workspaces/<id>/` directory (config, sessions, container
records, workers, capabilities) persists indefinitely. There is no
`delete_workspace` verb, no operator UI action, no agent tool.

## Why now

The container lifecycle contract (in flight) names a destroy rule for
containers. A workspace is the same shape of problem one level up: a bounded
thing with a lifecycle, whose end needs a decision procedure. If we don't
write the container contract first, we'll write the workspace contract wrong
too.

## Scope to be ruled (shape only — Main rules)

1. Who may delete: operator only? session with explicit permission? never
   by an agent (like persistent container removal)?
2. What gets deleted: the workspace config? the session history? the
   container records? the working tree on disk?
3. What gets *preserved*: a tombstone? an audit record? a grace period?
4. What happens to live containers bound to the workspace (free-use,
   resource)?
5. What happens to shared resource containers (`tm-res-*-git`)?
6. `.thoughtmachine/` at the repo root vs `<vault>/workspaces/<id>/` — one
   directory or both?
7. Two-phase: soft-delete (mark) then hard-delete (reap), or immediate?

## MUST NOT TOUCH (once dispatched)
Same list as the container tickets.

## Dispatch pattern
Read-first spec, then fix spec. The read answers (2), (4), (5), (6); Main
rules (1), (3), (7).
