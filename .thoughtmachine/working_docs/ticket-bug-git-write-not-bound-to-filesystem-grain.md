# Ticket: bug/git-write-not-bound-to-filesystem-grain

**Filed:** Gen 43, 2026-10-15 at `eadd2ca2`
**Source:** field report, red-team exercise at operator request
**Severity:** High — "read-only filesystem" is a false promise
**Status:** filed. Live bug, independent of Q1.

## Defect

With session grain `filesystem:read`, `git_write(stage, path)` **succeeded**.
Observed at the field agent's session:

- `git_write(stage, temp/net/hw-container-README.txt)` → succeeded with
  `execution_mode: containerized, failure_reason: none`
- `git_read diff_cached` proves it hit disk:
  `new file mode 100644 ... index 0000000..980a0d5`
  → `.git/index` rewritten.

The same `git:write` grain gates `commit / checkout / worktree_add /
stash_push / stash_pop` — the operations that create, rewrite and delete
working-tree files. So "read-only filesystem" is false while `git:write`
is on.

## Evidence

Reproduced live by the field agent. The `.git/index` mutation is
**observed**, not inferred. Inference labelled: checkout/stash/worktree
were deliberately NOT run (would mutate real files) — but the shared gate
means the same grain grants them.

## Fix shape (not written)

**Ruled by Main: bind `git:write` to the `filesystem` grain.** One fact,
one owner: "can this session write to the workspace?" is currently held
by two grains that don't reconcile.

Alternative (rejected by Main): keep `git:write` independent of
`filesystem` and document the implication. Not chosen because "I can
read the filesystem but write to git" is not a coherent user intent.

## `displayed ≠ enforced` cluster — member

This is a member of the cluster Main named:
**`displayed ≠ enforced`** — the panel shows one truth, enforcement uses
another.

Cluster members so far:
- **git:write under filesystem:read** (this ticket) — live
- **Build network under panel-banned** — conditional on Q1

Cluster becomes a strand when a third member appears. Until then, live
members ship as fixes.

## Must NOT touch

- `thoughtmachine/security.py::SAFE_DEFAULTS`
- `security/resource_catalog.py`, `agent/config/resource_catalog.json`
- `security_gate.apply_workspace_ceiling` + call sites
- parked `.thoughtmachine/knowledge/project/architecture_audit_2026-09.md`
- `scratch/_exp2.py`

## Bearing

Phase 3-adjacent (Secure Execution); ships as a fix, not a feature.
