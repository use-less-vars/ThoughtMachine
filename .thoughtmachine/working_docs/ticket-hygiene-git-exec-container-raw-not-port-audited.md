# Ticket: hygiene/git-exec-container-raw-not-port-audited

**Filed:** 2026-10-24, from the W4 premise-check finding
**Category:** hygiene
**Severity:** MEDIUM — container git is a separate boundary
**Status:** filed, not fixed

## Summary

`_exec_container_raw` (tools/git_info_tool.py:1072) does not route through
`host_run`. It calls `_ensure_resource_container()` → `manager.exec(...)`,
with its own permission gate on `git:read` / `git:write` and its own
`check_atomic_operation` hook path.

Two sub-findings, distinct:

**Sub-finding A — audit bypass.** Container git operations do not appear
in `host_run`'s audit log. A container-git `commit` is not attributed in
the same file as a host-git `commit`. Cross-tenant audit queries that key
on `host_run`'s event stream miss the container path entirely.

**Sub-finding B — env contract distinct.** `_exec_container_raw` passes
env to the container manager, not to `subprocess`. The F-A strip (which
prevents inherited credential-shaped env from reaching a host child) is a
host-env concern. Container env is a different concern with a different
shape — the container's `env=` is set by the manager, not inherited from
`os.environ` unless the manager forwards it.

## Not "should route through host_run"

`_exec_container_raw` should NOT route through `host_run`. It is on the
container side of the container/host boundary — Main's Q1 ruling confirmed:
containers stay on their own path. The audit gap is real; the fix is not
"route through the port," the fix is "audit the container path with the
same discipline."

## What closes this ticket

- **Sub-finding A** — when `_exec_container_raw` writes the same audit
  line `host_run` writes, on the container path. Or the container manager
  exposes an audit hook the tool calls.
- **Sub-finding B** — when the container env contract is named in
  `docs/host-port.md` alongside the host env contract. Two contracts,
  both documented, neither implied by the other.

Neither is W4's scope. Both are named now so the next wave that touches
the container path sees the gap.

## Sibling

`design/host-port-git-authorization` — host-side design pass; the container
path is out of that scope by boundary, not by omission.

## Filed

2026-10-24, Gen 49, ruled by Main Gen 39.
