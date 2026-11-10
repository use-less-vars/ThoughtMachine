# HANDOFF — T0 bootstrap

**T0 state:** `dev` at `4927ab33b0e967195f561c12d2b7332f2cc8fd3a` (merge of PR #324).
The **Control Plane phase is CLOSED**. **Phase 10 (EM-Solver / Resources) is OPEN.**

**New seat:** read `.thoughtmachine/knowledge/project/BOOTSTRAP.md` first — it is the paste-in onboarding brief for a web LLM with no tools.

## Read in this order

1. `.thoughtmachine/knowledge/project/BOOTSTRAP.md` — entry point for a fresh web-LLM operator.
2. `.thoughtmachine/knowledge/project/PRIORITIES.md` — the live work queue.
3. `docs/CONTROL_PLANE.md` — the pause/stop/timeout contract. Doctrine, not history. Note it lives in `docs/`, not in the knowledge base.
4. `.thoughtmachine/knowledge/project/vision_ledger.md` — the five standing commitments.
5. `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md` — the environment you are working in.

## First task

Start **C25 (Worker State Reattachment)**, open in `PRIORITIES.md`. It carries a name and a paragraph of intent but no scope — writing the scope is its own first step, and the symbol names in that paragraph (`WorkerJobRegistry`, the IPC socket) are the Operator's description, not verified identifiers: confirm each exists before designing around it. **C4 (Agent Sysprompt Addendum)** is also open; neither blocks the other.

## What Gen 61 landed (the Control Plane)

- **C26 — symmetric resume notice.** `3f839766`. Pause had a notice; resume did not, so a resumed agent went on obeying a stale pause instruction. `Agent.leave_reading_mode()` is the only `clear_restriction()` caller outside `state.py`, so every resume path funnels through it: the flag is armed there and the notice is materialised on the turn thread and written **straight to history** rather than to `_pending_warnings` — every flush site for that buffer runs *after* the LLM call, so a buffered notice would miss the first post-resume call, which was the entire bug.
- **C27 — resume left the banner grey.** `e5260677`. `Controller.resume()` released the pause flags but never left `ExecutionState.PAUSED`/`PAUSING`, so no `execution_state_change` fired, no `state_changed` reached the UI, and the banner stayed grey until F5. Resume now settles to `RUNNING` (turn in flight) or `READY`.
- **C28 — one tool call, two bubbles.** `dc7b0899` (backend) + `d8d65dd6` (frontend). One stored call yields two rows — the assistant `tool_calls` array and the `{"role": "tool", "tool_call_id": …}` result — and the chat had no pairing step. Both emit paths now carry `tool_call_id` and the UI folds on it.
- **C29 — worker pause never reached container exec.** `9082e2cd`. `bind_pause_event` was bound only for the main agent; it is now bound at the worker turn boundary and polled in both container managers.

**Merge state at T0:** all four are ancestors of `dev` — C26 `3f839766`, C27 `e5260677`, C28 `dc7b0899` + `d8d65dd6`, C29 `9082e2cd` — merged via PR #323 and PR #324. Nothing from the sprint is stranded on a side branch. Verify with `git log --oneline -12 dev` rather than concluding a change was never written.

## Open questions carried forward

1. **`git_read diff` returns silently empty** when `file_path` is a list. Pass a single path, or run it once per file. Unfixed.
2. **`git_read show_file` ignores its `commit` argument** and returns the working-tree copy. Reading a file *at a specific commit* silently yields the checked-out version instead — this is how the placeholder HANDOFF.md above survived a review pass. Use `git_read diff <base> <commit> -- <path>` instead. Unfixed (found at T0).
3. **A KB file with no `!` negation is invisible, not just untracked.** `.thoughtmachine/knowledge/project/**/*.md` is ignored wholesale, so a new read-on-entry file does not appear in `git status` at all and does not survive a clone, while the tracked file that points at it does. Every new onboarding file needs its own negation line in `.gitignore`.
4. **Harness `TMPDIR`.** Test runs need `TMPDIR=/tmp`; `/var/tmp` is read-only in this environment and the failure presents as a misleading `ENOENT`. Unfixed.

## Do not re-litigate

- **Pause = soft wind-down:** the turn finishes and the agent writes exactly one `Respond`. **Stop = hard abort:** the turn is discarded, the work parks, context is saved, it is resumable. These are different mechanisms, not two settings of one knob.
- **Timeout is not cancellation.** On timeout the CALLER stops waiting; nothing is cancelled, the worker keeps running and answers later. Only **Stop** parks a worker as `detached`. Do not build on "timeout kills the worker" — it does not.
- **The 4 Lego Pieces are law:** Persistent State, Interrupt Signal, Tool Return Contract, Loop Gate. Measure every control-plane change against them instead of arguing around them.
