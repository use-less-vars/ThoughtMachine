# Ticket: absence in an agent's view is not absence in the system

Status: open

## Problem

Agent workers are sandboxed to their own workspace and cannot see other workspaces. A worker
therefore cannot verify, and must not deny, the existence of a workspace it is not scoped to. On
the `crisp-eagle-24` incident the worker correctly reported the vault config as unverifiable from
its sandbox; the conclusion drawn from that report was then applied to the machine, where the
workspace was plainly present in the operator's live vault. The defect is in the interpretation of
a sandbox-scoped negative, not in the sandbox itself — but nothing in the report's wording stopped
the wider reading.

## Evidence

- `crisp-eagle-24` was reported absent by a worker whose sandbox does not include that workspace.
- The operator's live vault (UI) contained `crisp-eagle-24` without qualification.
- The worker's own wording scoped the finding to what it could verify from its sandbox; the
  downstream conclusion dropped that scope.

## Suggested fix

Keep the sandbox as it is; fix the reporting contract. Any report about vault/workspace state
produced from inside a worker must state its scope explicitly — "not visible from this worker's
sandbox (workspace `<id>`)" rather than "absent" — and any conclusion drawn from such a report must
be scoped to the sandbox until it is confirmed from a vantage point that can see the whole vault.
Add a lessons-learned entry (done: `.thoughtmachine/knowledge/personal/lessons_learned.md`) so the
distinction is not relearned.
