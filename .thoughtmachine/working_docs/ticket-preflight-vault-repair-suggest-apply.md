# Ticket: preflight suggests `vault_repair --apply` for keys `--apply` can never touch

Status: open

## Problem

Preflight suggests `vault_repair --apply` even when the workspace config contains engine-read keys
(`root`, `git_identity`, `git_allow_worktree_commits`). That suggestion cannot work: `--apply` can
never touch those keys — the repair tool reports them as "manual review only (engine-read …;
never auto-removed)". The operator is therefore pointed at a command that will run, change nothing
for the reported finding, and print a refusal. The correct suggestion in that situation is inspect
mode, which shows the finding without implying it is auto-fixable.

## Evidence

- `_engine_read_refusal_message(path_in_file)` in `thoughtmachine/vault_repair.py` returns
  `"manual review only (engine-read <flag|key>; never auto-removed)"`, and the guard is wired into
  the `_fix_issue` unknown-top-key path, the `legacy_permission_key` branch and
  `_apply_selected_fixes`.
- `_ENGINE_READ_KEYS = frozenset({_GAW_KEY, "root", "git_identity"})` — those keys are never
  removed or rewritten by the tool.
- Preflight nevertheless recommends `--apply` for findings that are, by construction, engine-read
  only.

## Suggested fix

In the preflight suggestion logic, split the recommendation by finding class: when every reported
finding is an engine-read key, suggest inspect mode and say the finding needs manual review; only
suggest `--apply` when at least one finding is auto-remediable. Add a test that a config containing
only engine-read findings never produces an `--apply` suggestion.
