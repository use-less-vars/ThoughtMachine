# ORCHESTRATOR_GUIDE.md — how to run workers without believing the brief

**What this file is.** The operating manual for the seat that never touches the repository directly: the orchestrator has no file editor, no shell and no codebase search, and reaches the tree only through workers. It covers the Worker tool's real surface, the two resources that run out (wall clock and context), how to read a report, how to recover a stalled worker, and how work reaches `dev`.

**Who it is for.** The next orchestrator, and any engineer who briefs one. Assume the reader will be handed a confident, plausible brief that is partly wrong: this generation's own dispatch carried four claims the code refutes plus one mis-attribution (§2).

**How to update it.** Edit in place; do not regenerate. Never restate a claim taken from a brief, an operator message or an earlier manual — open the file and cite the line. If the code moves, move the citation instead of adding a second sentence that says the same thing: this repo has paid for it once: the onboarding-contract comment at `.gitignore:270` used to state a count ("three read-on-entry KB files") that the list beneath it had outgrown, and it now states the rule instead of the number. Cite the command, not the count. History belongs in `machine_history_and_vision.md`.

**Sourcing rule.** Every factual claim is a `file:line` citation, a commit, or a KB entry — or it is marked **UNVERIFIED**. Where a brief and the code disagree, the code wins, and this file says so where it happens.

**Siblings.** `MANDATORY_STOP.md` (read first), `architecture_invariants.md`, `docs/ARCHITECTURE_MACHINE_MODEL.md`, `machine_history_and_vision.md`, `CI_PROCESS.md`.

---

## 1. What the orchestrator is

The orchestrator is a host-side agent with a deliberately poor footprint: it cannot edit a file, run a command in the workspace, or search the tree. What it has is the Worker tool, a response channel and — subject to its session grant — the git tools and the working-document store. Delegation is therefore not a convenience it may skip when a task looks small; it is the only mechanism by which the repository changes.

Do not take that on faith, including when it describes your own seat. The authoritative answer is computed, not documented: `agent/core/state.py:485-525` (`get_allowed_tools()`) narrows the set and `is_tool_allowed()` (`:527+`) enforces it fail-closed. The prose you are given is derived from that gate (`describe_allowed_tools()`, rationale at `agent/core/state.py:14-27`) precisely so prose cannot contradict mechanism — the standard to hold every other claim in a brief to.

**The orchestrator commits; workers do not.** The container has no `git` binary, so `git_read` and `git_write` are backend tools rather than shell commands, and `git_write` is gated on the session's effective git grain. A worker report saying "committed" is evidence of intent, never of a commit; verify with `git_read` (`log`, `merge_base --is-ancestor`). The asymmetry is useful: commit decisions sit with the one seat that sees every report.

**The orchestrator's scarce resources are its own.** It has no long local work to hide behind, so a wasted turn costs a whole context cycle and a blocked call costs real wall clock (§4). Spend turns on decisions and evidence-checking, not on transcription.

---

## 2. Myths this doc corrects

Four claims circulate in briefs about this tool. All four are wrong, and each will cost a turn or a file if believed.

### Myth 1 — "spawn requires `worker_name`"

Optional, defaults to `"default"`. The myth has a real origin: the schema once declared the field optional while `execute()` rejected `None`, and the comment records the cost.

```python
# tools/workspace/worker_thread.py:5330-5335
#: The worker an action lands on when the caller omits ``worker_name``.
#: "omitted" always names a worker that exists rather than demanding a name the
#: agent was never told to supply (the schema said ``worker_name`` was optional
#: while ``execute()`` rejected None -- that contradiction cost a turn).
DEFAULT_WORKER_NAME = "default"

# tools/workspace/worker_thread.py:5386 / 5561-5566
worker_name: Optional[str] = Field(default=None, ...)   # "Optional: when omitted ..."
if self.action in self.ACTIONS_REQUIRING_WORKER and self.worker_name is None:
    self.worker_name = DEFAULT_WORKER_NAME
```

`ACTIONS_REQUIRING_WORKER` (`:5489-5495`) is spawn, check, query, submit_query, join, wait_for_job, reset. `pause` and `stop` are excluded on purpose: there `None` means session/main scope, and defaulting them would silently redirect a main-agent pause onto one worker. The exported schema cannot mark the field required either — a defaulted field never lands in `required` (`tools/utils.py:95-126`); `tests/test_worker_operation_semantics.py` (`test_join_defaults_worker_name_to_default`) pins it.

What to do: omit it. Naming a worker is legitimate when the task is worker-specific, but "required" is false, and a brief that asserts it is a brief nobody checked.

### Myth 2 — "TimeBudget yields at the limit by calling Respond and WorkingDocument"

Nothing auto-calls anything. `thoughtmachine/timebudget.py` is 224 lines of resolution contract — `DECLARED_CEILING = 600`, `TimeBudgetExceeded`, a `resolve()` that raises rather than clamps, `as_envelope_fields()` — with zero references to Respond or WorkingDocument. The real mechanism is an allow-list: on overrun the framework sets `restrictions_active` and `restriction_reason='timeout'` (`agent/core/state.py:75-76`, `:90`, notice `:240-249`) and `get_allowed_tools()` (`:485-525`) returns

```python
if reason == 'token':   return ['Respond', 'SummarizeTool']
if reason == 'timeout': return ['Respond', 'SummarizeTool'] + sorted(TIMEOUT_WRITE_ESCAPE_TOOLS)
return ['Respond']        # 'turn', 'pause', and every unrecognised reason
```

`TIMEOUT_WRITE_ESCAPE_TOOLS = frozenset({'WorkingDocument'})` (`thoughtmachine/timeout_constants.py:63`); the rationale at `:33-62` is explicit that the hatch is write-only by design — a read tool would let the agent keep investigating, which defeats a resource-management restriction; the point is to dump verbatim working memory to disk before it is summarised away. It stays subject to `filesystem:write` (dynamic `get_required_categories()`), so the allow-list narrows what may be called and never grants a permission. A restricted worker that does not think to use the hatch loses whatever existed only in its context. Write "dump state to a WorkingDocument as you go" into the brief: the framework will not say it for you.

### Myth 3 — "workers are bound to their own worktree, so they must not write to the workspace"

There is no automatic per-worker worktree. `worktree_root` is opt-in, per tool instance, never inherited (verbatim at `worker_thread.py:6077-6085`, quoted in §7). The default is the registered workspace root, so a worker's file writes land in the tree you are looking at — the sandbox mounts it. "A worker cannot write to /workspace" is backwards: writing there is the normal case, a worktree is the exception somebody must ask for, and it cannot be entered by accident.

### Myth 4 — "you must reset a worker between tasks"

The default worker is a live thread that persists across spawns and queries, and its context survives — usually what you want, since it already knows the tree. `force=True` stops and replaces the *instance*; it does not reset the thread's context. `reset` is a separate action (`VALID_ACTIONS`, `tools/workspace/worker_thread.py:5487`) and it is the one that wipes context. Resetting "to start clean" throws away a map the worker paid for; reach for it when the context is full or poisoned (§6), never routinely.

---

## 3. Reading the envelope

Every Worker call returns a JSON envelope. Four parts carry meaning, one carries evidence.

- **`content`** — the report, in prose, from an agent that may have been cut off mid-task. Treat every claim about repository state as a claim requiring a read.
- **`status`** — `final`, `progress`, `timeout`. `progress` is not a result: the worker expects a follow-up `query`. Treating it as done is how half-finished work gets committed.
- **`confidence`** — `high`, `medium`, `low`. `low` is a request to read the raw evidence, not to re-spawn and hope.
- **`meta`** — `struggles`, `needs_direction`, `blocked_by`, `remaining_work`, `tool_issues`, `permission_issues`, `task_too_large`, `container_issues`, `insights` (the Respond tool's own parameter documentation). This is where an honest worker says what it could not do, and the field most conspicuous by its emptiness when a worker papers over a partial result.
- **telemetry** — `elapsed_seconds`, `timeout_seconds`, `timeout_triggered`, plus container exit/duration numbers. This is the part without an opinion.

**The soft-timeout trap.** If `elapsed_seconds` is at or past `timeout_seconds`, read the report as a forced finish — even when `timeout_triggered` is `false`, and even when `status` says `final`. The flag records whether the admission-time check fired, not whether the work finished.

This generation produced the exact case: a spawn requested 240 s, the effective budget resolved to 300 s, and the worker returned after 413.7 s as an ordinary answer. The only tells were the elapsed number having left the budget behind and the worker's own `meta` note that a mid-task summarisation had eaten part of its brief. `status` will not save you here; the arithmetic will.

Two reading rules that pay for themselves: never accept "done" without a path, a line number or a diff hunk; and if a worker reports writing a file that does not appear in `git status`, suspect the ignore rules before suspecting the worker (§8).

---

## 4. The time budget

A worker's budget is resolved at spawn (`worker_thread.py:6752-6772`, `store_budget_on_thread`; the module landed in `9d1c52f4`, `personal/task_tracker.md:890`). `TimeBudget` is a resolution contract, not a scheduler: `resolve()` raises `TimeBudgetExceeded` rather than clamping, so an impossible request is an error you see, not a number you inherit. What the budget controls in practice is the allow-list in §2 — the mechanism that changes what the worker can do.

**Enforcement is at admission only; in-flight calls are not cancelled.** This is C61 (`PRIORITIES.md`, "In-flight Tool Cancellation & Dispatch Window"), still open:

```
dispatch: sleep(200) under effective_timeout_seconds = 60
result:   completed in 205.63 s, timed_out: false, exit 0
```

The deadline was checked when the call was admitted and never looked at again. C61 records the same shape on the caller's side: the dispatch blocks for roughly twice the effective budget (a 60 s budget blocking the caller for 120.0 s). The budget is not propagated to container and tool paths at all — `docker_code_runner` keeps its own 30 s default and `agent/core/tool_executor.py` applies no wall clock to a tool call (`personal/task_tracker.md:896`).

So the budget is a yield signal, not a deadline, and it has two failure modes: the worker overruns quietly, and your own call blocks past the budget while you wait.

**Budget the call, not the session.** A worker given "implement the feature" spends its window orienting and returns at 80 %. A worker given "make this named change in these named files, confirm with this named test, stop" finishes, and the next window starts from a known position. If the task does not fit one window, split it into windows that each end in something verifiable.

---

## 5. Context is the scarce resource

Workers get their own context window; you cannot see inside it. Observed spawn windows in this machine's lessons records run roughly 300–480 s of real work — long enough to do one thing well, short enough that a second thing gets truncated (`.thoughtmachine/knowledge/personal/lessons_learned*.md`).

The failure that matters most is not a crash. It is **mid-task summarisation**: a worker whose context fills while the task is still running gets compacted, and compaction can eat the brief itself. This happened twice in the generation that wrote this file; each time the worker lost part of the specification and returned work that quietly did not cover it — the §3 example is one of the two. Nothing in the envelope flags it, and the worker may not notice.

The mitigation is to stop storing the plan in the prompt:

- Put critical state — spec, file list, acceptance criteria, decisions already made — in a **WorkingDocument** and tell the worker to read it instead of restating it in the spawn context. The document store survives compaction; a prompt does not.
- Ask the worker to write findings there as it goes. One habit then covers both the timeout escape hatch (§2) and compaction.
- Keep the spawn prompt short and point at the document. A long prompt is the text that gets summarised away first.

Do not confuse compaction with reset. Compaction is forced, lossy, and happens to a worker mid-flight; `reset` is deliberate, total, and happens because you asked. Only one is yours.

---

## 6. Recovery

A worker that has gone quiet, stalled, or reported `progress` is not dead. Its thread is alive and its context is intact; that is the value of the default worker.

| Situation | Action | Why |
|---|---|---|
| Returned `progress`, or went quiet mid-task | `action="query"` | Context survives; asking costs one call, re-spawning costs the orientation |
| Hit a soft timeout (§3) | `action="query"` after it yields | Restrictions clear on the next query; no new instance needed |
| Context is full, or poisoned with a wrong premise | `action="reset"` | The only thing that wipes context |
| Instance wedged while the thread is fine | `spawn` with `force=True` | Replaces the instance; does **not** reset the thread's context |
| A genuinely unrelated task | new `worker_name` | Separate worker, separate context |

`force=True` is the most misused row: it is an instance-replacement flag, not a memory wipe. If the worker is confused because its context is wrong, forcing it produces a confused worker faster.

---

## 7. Worktrees and the workspace root

There is no automatic per-worker worktree, and the code says why in one paragraph:

```python
# tools/workspace/worker_thread.py:6077-6085
# Inheritance is deliberately ABSENT: worktree_root is forwarded only
# when THIS tool instance was itself given one, never copied from the
# parent agent's config.  A sub-worker must therefore be opted in
# explicitly, so it can never be silently dragged into a worktree
# (writing to a checkout its caller is not looking at) merely because
# the worker that spawned it happens to be bound there.
```

The rest of the chain agrees. `worktree_root` is `Optional[str] = Field(default=None, ...)` with "None = use the registered workspace root … invalid values fail closed" (`agent/config/models.py:201`); the executor injects it into a tool only when set (`agent/core/tool_executor.py:480-495`); `tools/base.py:307-369` validates realpath containment and a vault barrier and never silently falls back to the wider root. Changing it is `RESTART_REQUIRED` (`agent/config/models.py:112`, read at `agent/core/agent.py:109-112`) — a config edit does not take effect in a running session.

The rule that actually exists is a human convention, not enforcement: "for true parallelism, use `git worktree add`" (`project/working_agreements.md:389`; worktree cleanup is destructive by default at `:783-784`). If you want a worker in a worktree, say so and supply `worktree_root`. Nothing will do it for you — which also means "the worker can't have broken that, it's in a worktree" is never a valid defence.

---

## 8. Committing

The orchestrator commits; workers do not (§1). The mechanics, in order:

1. `branch_create` — the `git_write` grain is `write_on_feature_branch`, and `dev` is protected.
2. `checkout` the new branch.
3. `stage` and `commit` with **explicit `file_path` lists**. There is no `-A` in the surface: a wildcard stage commits whatever else is in the working tree, including another agent's in-flight work, and a commit whose contents you cannot enumerate is not one you verified.

The surface is `commit`, `init`, `clone`, `push`, `branch_create`, `checkout`, `stage`, `unstage`, `worktree_add`, `worktree_remove`, `stash_push`, `stash_pop`. There is no merge and no PR operation — merging is a host/operator action, not something you reach by asking harder.

**The `.gitignore` trap.** `.thoughtmachine/knowledge/project/**/*.md` is ignored wholesale at `.gitignore:230`. Each tracked file under it needs its own `!` negation line; the required shape is at `.gitignore:282-285`, and the comment above it (`:270-281`) names the failure mode: a file created without a negation line is "never staged, never committed, and gone on the next clone while the tracked documents that point at it survive."

A brand-new KB file that does not appear as `??` in `git status` is **IGNORED**, not "already tracked" — that inference is how this generation nearly lost a file. Check before believing either story:

```bash
git status --short
git check-ignore -v .thoughtmachine/knowledge/project/<file>.md
```

If the second command prints a rule, the file is invisible to git and no amount of committing will include it. Add the negation line, then check `git status` again.

---

## 9. Checklist before you hand off

1. **Is the mechanism in the brief sourced?** A required field, an automatic tool call, a binding rule — grep before repeating it (§2).
2. **Short spawn prompt, state in a WorkingDocument?** (§5)
3. **Does the task fit one window, with a defined stopping point?** (§4)
4. **Did you read `meta` before `content`?** `blocked_by` and `remaining_work` decide how much of the report you may believe. (§3)
5. **Does `elapsed_seconds` exceed `timeout_seconds`?** Then it is a forced finish, whatever `status` says. (§3)
6. **Is every "done" backed by a path, a line or a diff hunk?**
7. **Does every new file appear as `??` in `git status`?** If not, run `git check-ignore -v` before assuming it is tracked. (§8)
8. **On a feature branch, staging explicit paths, committing by name?** (§8)
9. **If a worker stalled, did you choose `query` or `reset` deliberately**, rather than `force=True` out of habit? (§6)
10. **When you learned something that contradicts this file, did you fix this file?**
