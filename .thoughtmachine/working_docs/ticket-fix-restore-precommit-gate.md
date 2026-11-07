# fix/restore-precommit-gate

**Filed:** 2026-10-21
**Severity:** MEDIUM
**Status:** filed — supersedes the earlier `feat/pre-commit-gate-hook` framing

## The finding — the hook existed, was deleted on a false premise

The containerized git path (`tools/git_info_tool.py::_exec_container_raw`)
prepends `-c core.hooksPath=<container path>/.githooks` for commits and does
**not** pass `--no-verify`. Container commits ran hooks by design — verified
in-tree comment attributing the wiring to `caff93cb` (2026-08-16).

On **2026-09-22**, commit `85035605` ("Remove dead .githooks/pre-commit tracked
hook") deleted the 292-line fast gate. Its justification: *"the tracked file is
unreachable: core.hooksPath is unset."* **The premise is false — the container
sets core.hooksPath explicitly.** The file was dead for the operator's bare
shell only.

The KB recorded the contradiction and nobody reconciled it:
- `project/open_items_register.md` §17 **corrected** the claim — container maps
  `core.hooksPath` to the 12 KB hook; "deliberate and load-bearing."
- `personal/task_tracker.md` (2026-09-21 "Hook TRUTH") still calls the hook
  "DORMANT… core.hooksPath is UNSET" — reasoning from a bare shell.
- 09-22 deletion followed the stale entry, not the correction.

## Current state

- `.githooks/` does not exist.
- `core.hooksPath` points at a nonexistent directory. Git silently runs
  nothing — no warning.
- Agent commits run **zero** hooks. Operator commits still get the worktree
  gitdir 617-test gate. QA gate applies to the human and not the agent.
- §E3 host-mode fail-closed guard refuses host commits *when
  `.githooks/pre-commit` is configured*. File is gone → guard is inert → host
  mode silently permitted hook-free. The safety net that would have caught
  this was disabled by the loss.

## Fix — four steps

1. **Restore the hook.** `git show 85035605^:.githooks/pre-commit` — the
   292-line gate, exact bytes recoverable from git history. Runs: import gate →
   live `/health` revision check → hermetic TestClient WS roundtrip →
   conditional contract test.
2. **Make missing-hooks-dir fail closed.** Today "hooks directory missing" and
   "no hooks configured" are indistinguishable. Container git tool asserts
   `os.path.exists(hooksPath) and os.access(hooksPath + '/pre-commit', X_OK)`
   before committing; failure names the missing path and refuses the commit.
   **This is the actual defect, independent of which hook we want.**
3. **Re-key §E3 host-parity guard** off something other than the deleted file.
4. **Correct the KB.** Fix `personal/task_tracker.md` 2026-09-21 "Hook TRUTH"
   (false premise). Add `personal/bugs_and_fixes` entry naming `85035605` and
   the date.

## Pattern

`declared-but-not-enforced` at the commit surface — the doc/comment says the
gate is installed; the file was deleted on a false premise; the silent no-op
is the failure mode. Same family as the panel-lying-about-permissions class.
