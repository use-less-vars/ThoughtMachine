# Archive — personal/lessons_learned.md

> Entries moved out of `personal/lessons_learned.md` by `mode=archive` (older than 21 days as of 2026-11-06). Oldest first.

## 2026-11-03 — ## SUBSTRATE-AUDIT STANDARD: verify symbols against ASSIGNME...

## 2026-11-03 — ## S3b dispatch: what worked, and two tooling defects hit li...

## 2026-11-03 — ## S4 audit: the frame surface has no transport seam, and si...

## 2026-11-03 — ### THE SPINE OF THE STANDARD: the description is not the su...

### THE SPINE OF THE STANDARD: the description is not the substrate

One pattern, three worked examples. Not three entries — one lineage. Every audit failure in this project has been the same error wearing different clothes: **a description was mistaken for the thing described.** Docs, briefs, test doubles and design sections are all *descriptions*. Only the code that runs is the substrate. Verify against the substrate.

**Example 1 — the brief describes the code from the design (S4).** Six false claims in one document: `worker_bus` does not exist (real surface is `WorkerRegistry._worker_event_bus_registry` + `register_event_bus`); `paused_manually` is not an attribute (derived from `_manual_only_pause`); `_heartbeat_tick` does not write `status.json` (global bus only, 30 s gate); `status` vs `runtime_status` mismatch; `Heartbeat` has no `current_task`; `_heartbeat_age` is *three* functions, two of which need a `WorkerThread` the supervisor does not have. Each claim was plausible, none was checked. The expensive failure was not the six errors — it was that the audit found something the brief never mentioned and could not have mentioned: there is **no single event egress** in `WorkerThread`. A wrong brief is cheap to correct; a *silent* brief costs a re-plan.

**Example 2 — my own hypothesis, falsified by the probe (S4a).** I predicted `WorkerBusAdapter` funnelled worker events, so the sink could go in one place and S4a would shrink. The probe killed it: the adapter sees only agent tool-loop events, and there are six egress families whose only real choke point is `EventBus.publish`. My hypothesis was a description too — inferred from an attribute name and five line numbers — and I nearly dispatched work built on it. **Pre-dispatch probes are not overhead; they are the only thing that distinguishes a plan from a guess.** Note the shape: I was not checking the engineer here, I was checking myself, and I was wrong.

**Example 3 — the double is more capable than production (S4a).** `WorkerThread` takes `workspace_dir` and discards it, keeping only `_worker_dir`, so `spec_from_worker` raises `RuntimeSpecError` on any real thread and every process-mode control verb is dead. The suite was green. The process-runtime tests pass **fakes that carry `workspace_dir`** — an attribute the real object does not have. A double *more capable* than production hides exactly the class of bug it was built to catch, and it hides it with a green checkmark, which is worse than no test. Compare: my own chunk-B tests manufactured `SimpleNamespace(_worker_dir=...)` and would have passed while D2 silently returned `unknown` in production. The fix shape is `test_status_json_path_resolves_the_real_worker_thread_mirror`: construct the **real** object, assert the real path, and **mutation-check the test** (perturb the resolution, watch it fail, restore). A test that has never been seen to fail is a description of a test.

**Consequences, stated as rules:**
1. Every symbol named in a brief is a claim until resolved in the tree. Resolve by grep, not by recall.
2. Probe before dispatch when a plan rests on an inference. If the probe can falsify the plan, run it first.
3. Ask for the FALSE list explicitly and early, and split audits so no query carries more claims than survive one summarisation pass.
4. Test against the thing that runs. If a test double is more capable than production, the test is a description.
5. Mutation-check any test whose whole job is to pin a path or a resolution.
6. When a brief omits rather than errs, treat silence as the expensive category — audit for what is *absent*, not only for what is *wrong*.

**Corollary about who is wrong.** The audit loop caught the engineer's brief four turns running (S3: 5 corrections; S3b: 3-of-5 dead on arrival; S4a: D1 underspecified; S4b: caught pre-dispatch). The engineer's own read, on the record: the convergence is because the agent is doing the audit, **not** because briefs got better. Both halves matter — the auditing and the not-claiming-credit.

## 2026-11-03 — ### Worked example — the S4 brief, six false claims in one d...

### Worked example — the S4 brief, six false claims in one document (not six one-offs)

The `stopped` ghost was read as a one-off typo. It was not. The S4 brief, written by the same hand and citing design docs rather than grepping the tree, contained **six** independent factual errors in a single document. The pattern is: *the brief is a plausible reconstruction of the substrate from memory of the design, and it is wrong wherever memory substitutes for a grep.* Audit standard is therefore "every symbol named in a brief is a claim until resolved in the tree" — and the failure mode is not one bad name, it is a whole document whose names are drawn from the design rather than the code.

Each of the six, with the resolution that proves it:

1. **`worker_bus` does not exist.** Brief assumed a module/object by that name. Real surface: `WorkerRegistry._worker_event_bus_registry` (`worker_registry.py:62`) plus `register_event_bus`/`unregister_event_bus`/`get_event_bus`/`get_event_buses_for_session` (`:151/:158/:165/:172`). Bonus: the brief also asserted "bridge.py unchanged" as if it were work; `bridge.py` already discovers buses via `get_event_buses_for_session` and `_worker_bus_is_stale`→`get_worker_event_bus`, so the claim was achievable but only under the *real* names.
2. **`paused_manually` is not an attribute.** It is derived: `_manual_only_pause` (`worker_runtime.py:338-340`, read at `:357`). A brief that says "read `paused_manually`" describes a field that would have to be invented.
3. **`_heartbeat_tick` does NOT write `status.json`.** It publishes to the *global* bus only, and is gated on `HEARTBEAT_INTERVAL_S` (30 s). The brief's causal story ("the heartbeat tick is what refreshes the file the supervisor reads") is false; the docstring assigns file writes elsewhere.
4. **`status` vs `runtime_status`.** File key is `runtime_status` (`worker_thread.py:3768`); frame field is `status` (`protocol:508`). The brief's "verbatim match, copy the dict" is 3-of-4 true — exactly the kind of near-miss that survives review and breaks at runtime.
5. **`Heartbeat` has no `current_task`.** Fields are `at, status, current_context_tokens, max_context_tokens` (`protocol:486-489`). The brief's D1 amendment told me to cache `current_task` from the frame. Impossible as written.
6. **`_heartbeat_age` is three functions, not one.** `worker_thread._heartbeat_summary` (`:5204`, `HEARTBEAT_STALE_AFTER_S`), `worker_manager._heartbeat_age` (`:336`, reads `last_heartbeat` then `_last_heartbeat_monotonic`), `worker_lifecycle._heartbeat_age` (`:130`, own dict, `_is_stale` `:143` / `_is_hung` `:148`). The first two take a *thread object*; in process mode the supervisor has no thread object. So "reuse the existing age function" was unimplementable and D1 had to become "one NEW supervisor-side function".

What the audit found that the brief did not mention at all — and this is the point, because a brief that is *wrong* is cheap to fix and a brief that is *silent* is expensive:

- **`WorkerThread` has zero transport capability.** Greps for `_outbox|sendall|_sock|FrameEmitter|encode_frame` in `worker_thread.py` → 0 hits. `__init__` has no sink/socket/mode param. `_Outbox` exists only in `worker_runtime_main.py:91`. There is also no spawned-child signal (`os.environ|sys.argv|getenv|WORKER_RUNTIME_MODE_ENV|MODE_PROCESS|_runtime_mode` → 0 hits). So the entire "emit frames from the child" step required a `frame_sink` kwarg threaded through `_build_worker`→`WorkerThread(**kwargs)`, which the brief never budgeted.
- **`_write_status_file` is a single choke point.** One def, 10 keys, atomic tmp+`os.replace` under `FileLock`, **21 call sites**. The brief implied a 21-site refactor; the work is ~20 lines in one function. Finding the choke point changed the estimate by an order of magnitude. (Its sibling `_save_context` is a *separate* def — do not assume all file writes funnel here.)

Meta-lesson: run the audit asking for the **FALSE list explicitly and early**, and split audits so no single query carries more claims than survive one summarisation pass. Audit workers lose verbatim claim lists to summary compression; the six-claim list above survived only because it was demanded in the first third of the query and written out before the long greps.


## 2026-11-03 — TOOLING FRICTIONS OBSERVED LIVE (S4b2 session, orchestrator ...

TOOLING FRICTIONS OBSERVED LIVE (S4b2 session, orchestrator side). Reporting these because they cost real time:
1. `Worker(action="spawn")` FAILS with {"error": "worker_name is required for action 'spawn'"} even though the system prompt says worker_name defaults to the default worker. Must always pass worker_name="default" explicitly. The prompt and the schema disagree — one of them should be fixed.
2. `git_read(operation="show_file")` CANNOT read an UNTRACKED file: returns `fatal: path exists on disk, but not in HEAD`. So a brand-new file created by a worker (an untracked test file) is UNREVIEWABLE through git; the only routes are (a) have the worker paste it, or (b) have a verification worker `cat -n` it. Worth handling explicitly since reviewing a new file before committing it is the normal case.
3. `git_read(operation="show_file")` IGNORES line_start/line_end on large files — it returns the head of the file truncated at ~10k tokens, so you cannot page to the interesting region. Same workaround: worker `grep -n` / `sed -n`.
4. `git_read(operation="diff")` requires file_path with >= 2 paths (a single path errors).
5. `WorkingDocument(action="create")` silently ignores a supplied `doc_id`.
6. A worker's `context` being summarised mid-task is invisible to the orchestrator except through the worker's own self-report. Twice now a worker flagged "my context was summarised, this is a reconstruction of your brief" — and both times the missing/weak part was exactly what the summary had eaten. TREAT THAT FLAG AS A DEFECT SIGNAL, not chatter: re-read the worker's deliverable in full rather than trusting the green test count.
7. `git_write(action="stage")` then `commit` with the same explicit file_path list works and DOES pick up previously-untracked files; commit is not implicit-staging, so stage first.

## 2026-11-03 — ## Live tooling / orchestration problems observed (reported ...

## 2026-11-03 — ## 2026-11-04 — LIVE DEFECT (agent-side, W8 S5 session): a l...

## 2026-11-04 — LIVE DEFECT (agent-side, W8 S5 session): a long worker call ends in a *detach*, not a partial result — and every recovery path I have costs the worker's context

Measured, not guessed. Three distinct problems compounded in one call, and the shape is worth memorising because the individual pieces each look benign.

### 1. The 420 s ceiling detaches instead of returning
A worker call that exceeds the ~420 s caller ceiling does **not** return "here is what I had so far". It **detaches** the worker. I get no partial deliverable, and the call I made is not reported as failed — it is reported as a state.

### 2. The follow-up `query` is silently QUEUED, not refused
After the detach, my natural next move — `Worker(action="query", ...)` asking for what it had — does **not** error. It **enqueues**. The wording is "runs only once an operator resumes", and in v1 resume is operator-initiated with **no agent resume API**. So the result is not lost-and-reported; it is *stored where I cannot reach it*, and the tool told me nothing that stops me from waiting.

### 3. The detached instance holds instance 1, so there is no clean respawn
Re-`spawn` cannot take the slot. The only recovery I actually have is `action="reset"` — which **drops the worker's context**. So the recovery for "I lost the result" is "also lose the context", and the real work becomes reconstructing the state from disk.

**Net rule for budgeting: a long task must be chunked so that no single call can approach 420 s, because the failure mode is not "slow", it is "destroy the worker".** And never issue a follow-up `query` to a worker you believe is detached — check status first, and treat a detach as terminal for that instance until an operator acts.

### 4. `/tmp` does not survive between container runs
A worker that backed up a file to `/tmp` before a mutation experiment, **reported the restore as complete, and the backup was simply gone** on the next call. Restore has to be proven from something durable. What worked: capture the file's **byte size** and `git status --porcelain` before and after — a mutated `worker_thread.py` shows as `M`, and a correct restore shows only the file under test. (Byte-identical-to-HEAD is the claim; `git status` is the cheap evidence, `sha256sum` is the good one.)

### 5. A recon worker mis-attributed a class to a facade file
A read-only recon worker reported `class Worker` / `_action_spawn` as living in `tools/workspace/worker.py`. That file is a **112-line re-export facade**. The real definitions are in `tools/workspace/worker_thread.py`. I then repeated the bad attribution in my own briefing to the next worker, so the error propagated one generation further than the report that caused it.

Two cheap defences, both now standing practice:
- **Ask the recon worker for the file's line count** alongside the symbol. A "5113-line module" and a "112-line module" cannot both be the home of `_action_spawn`.
- **Blame one line of the symbol** instead of trusting a path. `git_read blame` is the cheapest ground truth available, and it fails informatively: blaming a line in `worker.py` returned *"has only 112 lines"* — the refusal itself was the evidence that the symbol was not there.

Corollary worth keeping: **my own briefing is an input to the next worker, so a wrong fact in it becomes a wrong fact in theirs.** When a fact is load-bearing, cite the file *and* the line, and verify at least one of them cheaply before passing it on.

## 2026-11-03 — ### How to sweep a 6k-test suite through a ~210s worker wind...

### How to sweep a 6k-test suite through a ~210s worker window

The worker dispatch window is a hard ~210s regardless of the `timeout_seconds` you request (asking 300 gets you cut at 210 with `status: busy` and the instance still alive). So budget script runtime to ~95-120s and leave ~60s for the worker to write its report.

Shape that worked (461 files, 6,135 tests, ~9 calls):
- **Never** `pytest tests` bare: 21 `docker`-marked + `slow` files need a real daemon and will hang. Use `-m "not docker and not e2e"` — that is exactly what CI runs (`.github/workflows/platform-matrix.yml:81`), so it is fidelity, not a shortcut.
- **12 files per pytest invocation**, not one file per invocation. Per-file invocation spends ~1.5-2s of interpreter startup each; batching 12 cut wall clock roughly in half (108 files in 103s vs 35 files in 210s).
- Bound it twice: inner `timeout 85` per invocation, and a wall box checked *before* each chunk (`[ $((NOW-START)) -gt 95 ] && break`). Inner+box must be < 210s with margin, otherwise the call is cut forever before reporting.
- `/tmp` does **not** survive between container runs — regenerate `find tests -name 'test_*.py' | sort` every call and carry the cursor as an integer index in the agent's own notes.
- **Do not let RED stop the sweep.** Print `RED<start>: <file list>` with the full chunk membership and carry on; attribute afterwards. Stopping on the first red while also wanting a full-suite signal wastes the two things in short supply (calls and wall clock).
- Always print the chunk's **file list** on red, because a killed chunk loses its results silently: `tail -2` of a `timeout`-killed pytest is progress dots, and `grep failed` finds nothing. A timed-out chunk must be re-broken into per-file runs.

**Attribute every red on the base branch, not just the feature branch.** Both reds in this sweep looked like regressions in the exact area under test (worker pause/resume, vault bootstrap) and both reproduced identically on `dev`. Checking out the base branch is ~2 calls and converts "unknown regression, block the merge" into "documented baseline, merge is safe". Filter the comparison to files that exist on both sides — wave-added test files make a naive `file-not-found` look like a pass or a failure.

**A hang claim needs a per-file run plus `--durations`.** `--timeout` does not fire when the time is in teardown or in interpreter exit, so "pytest-timeout did not fire" is evidence about *where* the time goes, not evidence that nothing is slow.


## 2026-11-03 — ## 2026-10-25 — `_RecordingForwarder` / WORKER_SPAWNED `_cal...

## 2026-10-25 — `_RecordingForwarder` / WORKER_SPAWNED `_callbacks` red: NOT reproducible locally; root cause is a production leak

**Symptom (CI matrix, Python 3.12, same-process run):**
`[core.event_bus] Error in event subscriber for EventType.WORKER_SPAWNED: '_RecordingForwarder' object has no attribute '_callbacks'`
presenting as a red on `test_resume_returns_200_and_revives_a_detached_worker` (`tests/test_worker_lifecycle_routes.py`) that passes 19/19 in isolation.

**Both reproduction strategies came back clean on `dev @ dddcd6d4`:**
1. Direct polluter×victim pairs — the only two files defining `_RecordingForwarder` (`tests/test_bridge_stop_semantics.py`, `web_ui/backend/tests/test_permission_routes.py`) run immediately before the victim: `25 passed` / `54 passed 1 xfailed`, `grep -c _callbacks` = 0 both times.
2. The exact CI slice (alphabetical, 12 files ending at the victim): **`115 passed in 20.74s`**, 0 `_callbacks`, 0 `Error in event subscriber`.
No branch-only variant exists either: all 11 worktrees carry byte-identical copies of the same two files at the same line numbers (`:61`, `:743`).

**Root cause, from code (mechanism identified, not observed):**
- `agent/events.py` `EventBus.subscribe()` ends its registration branch with `self._subscribers[event_type].append(callback)` and **no `return`** → returns `None`.
- `web_ui/backend/bridge.py:715-716` stores that `None` in `self._worker_spawned_sub` (and 7 sibling attrs).
- `_unsubscribe_worker_events` (`bridge.py:898`) guards with `if sub is not None:` → **the detach branch can never run**. Every `WebAgentBridge` ever constructed leaves 8 callbacks on the process-global `global_event_bus` forever.
- `_on_worker_spawned` reads `self._forwarder._callbacks` at **`bridge.py:930`, before the session-mismatch guard** — so any leaked bridge raises for *every* later WORKER_SPAWNED, regardless of session.
- `publish()` wraps each subscriber in `try/except` logging `Error in event subscriber for {event.type}: {e}` → the observed string.
- The two test doubles define only `broadcasts` / `calls`; the **real** `EventForwarder` does have `_callbacks` (`web_ui/backend/event_forwarder.py:46`). So the message requires *test double swapped in* (`tests/test_bridge_stop_semantics.py:154,184` do `bridge._forwarder = _RecordingForwarder()`, and its `bridge` fixture is a bare `return` with **no teardown**) **+** *a bridge leaked by the production defect*.

**Existing prior art — do not re-diagnose:** `.thoughtmachine/working_docs/ticket-hygiene-eventbus-subscribe-returns-none-bridge-leaks-listeners.md`, status **"recorded, NOT fixed"**, severity LOW/MEDIUM, recommends Option 2 (have the call site store `(event_type, callback)` pairs) and forbids changing `publish`/`unsubscribe` semantics. `tests/test_per_worker_eventbus_bridge.py:187` already detaches correctly by using the two-arg `global_event_bus.unsubscribe(EventType.WORKER_SPAWNED, bridge._on_worker_spawned)` form — that is the pattern any test-side mitigation must copy.

**Standing rules earned:**
1. A red in the matrix that passes in isolation, with a **mock/double error in stderr**, is test pollution or cross-test leakage — not a regression. Reproduce in the same slice *and* in the same file order before blaming the branch.
2. **Reproduction failure is information, not a dead end:** the absence of a repro plus a `try/except`-swallowed publisher means the defect can be fully identified by reading the subscriber-registration path. Read `subscribe`→`store`→`unsubscribe`→`guard` as a four-hop chain; a `None` handle anywhere makes the teardown dead code.
3. Never assume "the fix is test-only" from the *symptom* — the symptom named a test double, but the enabling defect was production (`subscribe()` not returning a handle). Check where the handle is produced before accepting a scope constraint.

## 2026-11-03 — ## 2026-10-25 — STANDING RULE: a per-process sweep cannot de...

## 2026-10-25 — STANDING RULE: a per-process sweep cannot detect cross-file test pollution

**Rule (adopted on operator instruction, applies to all future CI/sweep design):**
> A 12-files-per-process sweep **cannot detect cross-file test pollution**. Same-process, same-order execution is the only valid reproduction for that class. A green sweep is therefore **not** evidence that a matrix red is a flake.

Consequences to apply:
1. When a CI red passes in isolation, the reproduction must be *same process, contiguous files, CI's ordering* — never a fresh process per file, and never a hand-picked pair (I tried pairs first and got a false negative; the polluter can be two-or-more files upstream).
2. The cost of this blind spot is now measured: **three separate reds in one wave** (`test_command_file_detach_is_seen_while_parked`, then `test_resume_returns_200_and_revives_a_detached_worker`) all traced to one order-dependent leak, and my 461-file sweep reported all of them as green-then-unexplained.
3. Preferred instrument for this class: one process, whole CI slice, `2>&1 | grep -c '<signature>'` for the tell-tale message plus the `FAILED` lines. Grading a slice by "passed/failed" hides pollution that only *degrades* a subscriber (see below).

### Companion insight — silent-degradation guards
The leak survived because **two** independently-correct-looking guards both degraded silently:
- `EventBus.subscribe()` has no `return` → callers stored `None` → `if sub is not None:` made the detach a no-op **that looks defensive on read-through**.
- `EventBus.unsubscribe(callback)` with one positional arg puts the callback into `event_type`, matches nothing, and returns **without raising** (wrapped in `except Exception: pass` anyway).
- `EventBus.publish()` swallows per-subscriber exceptions and only logs.

Three layers, none of which can fail loudly. Standing heuristic: **when a defect is invisible, look for the logging/swallowing/`is not None` triple before looking for the bug** — a leak with no symptom is almost always a swallow at the publisher plus a no-op at the teardown. And: *a test that can only be shown green by CI is not verified*; construct the mechanism directly instead (two bridges, close the first, publish, assert no subscriber error). That test was RED→GREEN locally and mutation-checked, while the CI-ordering symptom never reproduced on this machine at all.

## 2026-11-03 — ## Reproducing macOS-class path bugs on Linux + reading CI r...

## 2026-11-03 — ## Post-restart smoke probe: thread→process worker migration...

## 2026-11-04 — ## Epoch fencing landed (S7a-1) — patterns worth reusing

**...

## 2026-11-04 — ## Live tooling fault: `Worker(spawn)` returned an empty env...

## 2026-11-04 — ## S7a-3 slice: orchestrating a worker through summarisation...

## 2026-11-04 — ORCHESTRATOR SELF-INFLICTED TRAP (deeply embarrassing, entir...

ORCHESTRATOR SELF-INFLICTED TRAP (deeply embarrassing, entirely avoidable): I staged a worker's new files, then sent the worker a follow-up query that EDITED those same files, then ran `git commit`. Both paths showed `AM` -- added-to-index, modified-in-worktree -- and a plain commit would have shipped the PRE-follow-up blobs (480-line file instead of 578) with a commit message describing the follow-up. The worker caught it, not me. RULE: after ANY edit round that follows a stage, re-stage and look at the porcelain first column. `AM`/`MM` = the index is stale; `A `/`M ` = safe to commit. Never trust a stage you did before the last write. Cheap check: `git_read diff_cached` shows exactly what the commit will contain.
TOOL NOTE (useful): `git_read show_file` fails on a path that exists on disk but is not in HEAD ("exists on disk, but not in 'HEAD'"), so a brand-new untracked file cannot be reviewed with it. Workaround: stage the file, then `git_read cat_file` with object=":<path>" (index rev syntax) returns the staged blob. This is the only way I have to read a worker's new file before committing it -- and it doubles as verification that the index holds the version I reviewed.
TOOL NOTE: no `diff --stat`, no `branch_delete`, `show_file` ignores line_start/line_end (reads whole file). For a 578-line file that is acceptable; for worker_thread.py (6507 lines) it is not, so targeted regions must be read back through the worker with `sed -n`.
REVIEW PATTERN THAT PAID OFF: ask the worker to answer pointed falsification questions about its own change ("is this branch reachable in production?", "what is the worst case if X?"). It answered honestly, including a caveat that hurt its own work (duplicate event publish) and correctly identified that caveat as PRE-EXISTING. But the defect I actually caught was one it could not have caught by self-review: a primitive that is correct in isolation yet cannot serve its only future caller (fenced reader vs cross-epoch adoption). Read the primitives against the NEXT slice's needs, not against this slice's tests.


## 2026-11-04 — ## S7b session — live tooling problems (for engineers)

1. *...

## 2026-11-04 — ## Silent envelopes from Worker spawn/query (seen twice, S7b...

## 2026-11-04 — ## 2026-11-04 — S7b-4/S7b-5: pipes, breadth selection, and a...

## 2026-11-04 — S7b-4/S7b-5: pipes, breadth selection, and a docstring that lies

**A `| tail` pipe can destroy a whole test run (worst tooling trap of the session).** Two breadth runs were lost (651s and 808s, zero usable output) because the command was `timeout 600 python -m pytest ... | tail`. Pytest's spawned grandchild processes inherit the stdout pipe and hold it open after pytest exits, so the piped command blocks for the full bound and gets killed. THREE workers independently made the same mistake. Fix, now standard instruction in every verification brief: **redirect to a file, never pipe** — `timeout 300 python -m pytest ... > /tmp/run.log 2>&1; echo EXIT=$?; tail -3 /tmp/run.log`. Also: bound each invocation, and never trust `timeout N` to be the real bound (the runner caps lower, ~480s wall for a spawn and ~650-800s for a command).

**My breadth baseline was wrong, and the worker proved it.** I carried "1076 passed / 5 skipped, ~323s" as *the* baseline across slices. Splitting the suite into four glob groups produced **6506 passed, 64 skipped, 3 deselected, 5 xfailed, 0 failed** (~521s of pytest time). So the 1076 figure came from a much narrower/partial selection. Lesson: a baseline number is only meaningful WITH its exact selection (`-m`, `--ignore`, paths). Ask for the selection, not just the count. Corollary: the full suite now takes ~900s, which exceeds the command cap — the canonical breadth gate must be the four-group split: `tests/test_[a-f]*.py` (49s), `tests/test_[g-s]*.py` (98s), `tests/test_[t-z]*.py tests/tools tests/workspace` (228s), and the subdirectory globs (140s).

**Spawn/query envelopes keep vanishing.** A 660s spawn died as "did not respond within 480.0s (last heartbeat 475s ago)" while the worker was simply blocked inside a long pytest run (no heartbeats mid-command). The instance stays alive: `action='query'` with a small timeout returns in 4-5s. Rule: heartbeat-ago ≈ time spent inside one long command, not a hang. Check `git_read status` (tree unchanged = no progress OR clean success), then query.

**Soft timeouts are survivable; hard timeouts are not free.** Two tasks were cut at 426-435s mid-flight. In both cases the *work* had landed (file written) — the loss was only the verification. Structure briefs as "write first, report before running anything long", and prefer one long-lived instance for iterate-to-green (its context holds the draft) over a respawn.

**Testing multiprocessing honesty:** 7 real `spawn` processes cost 55s, dominated by the child importing the full `worker_thread` stack (~7s/spawn) — not by the handshake. Fewer spawns is the only real lever on suite runtime. And `CommandFence(epoch=0)` is accepted by `write_inflight`, which lets a test plant a "previous incarnation" record in a fresh directory (generation 1) without burning a spawn on a warm-up process.

**Reading production code for review:** `git_read diff <file>` (working tree vs HEAD) reviewed a 7k-line module's +400-line slice in one call; `git_read blame` with `line_start`/`line_end` verified four specific claims in 4 calls. Uncommitted lines show `000000000 (Not Committed Yet)`, which also tells you the block is new. Cheapest review of the session by far.

## 2026-11-04 — TOOLING GAPS HIT LIVE (agent side, during S7c design pass — ...

TOOLING GAPS HIT LIVE (agent side, during S7c design pass — reported to the engineer):

1. **`git_read ls_tree` ignores its `pattern` parameter.** Called with `treeish=HEAD, recursive=true, pattern=*defaults*.py` expecting a filtered listing; got the FULL recursive tree (10,026 tokens, truncated at 10,000). No error, no warning — just a very expensive success. Do not use `ls_tree` hoping `pattern` narrows it. If you need "where is file X", ask a worker to `grep -rn` / `find` instead, or use `git_read show_file` on a guessed path. This cost ~10k tokens for one wrong guess and is the single most expensive tool mistake of the session.

2. **The agent cannot read its own deliverables under `.thoughtmachine/working_docs/`.** That directory is gitignored except for an explicit whitelist (`.gitignore:118-120` re-ignores everything; `:121-124`/`:126`/`:128`/`:249`/`:292-297` whitelist `ticket-*.md`, `protocol-*.md`, `reproduce_p0.*`, some backlogs). Consequences: `git_read show_file path=.thoughtmachine/working_docs/<any>.md` fails with `fatal: path ... exists on disk, but not in 'HEAD'`, and `git_read status` shows nothing (so a commissioned doc looks like "no changes" — I nearly read that as "the worker wrote nothing"). **There is no agent-side read path to working docs.** The only way to review a design doc you commissioned is to have the worker echo it back through `Worker(action="query")`, which burns agent context AND worker tokens (my reviewer round trips cost ~34k and ~69k worker tokens). Recommendation: add a plain file-read capability for workspace paths, or honour `pattern` in `ls_tree`, or whitelist `!.thoughtmachine/working_docs/*.md`. Related existing ticket: `ticket-hygiene-working-docs-cluster-reports-gitignored`.

3. **Worker-side git is unavailable.** In this session DockerCodeRunner has no `git` binary (`/bin/sh: git: not found`) and `host_bash` is banned, so a worker CANNOT verify git state at all — `git status` inside the worker silently can't run. Any "confirm the tree is clean" instruction in a worker brief is unfulfillable; verify it yourself with `git_read status` and don't ask the worker.

4. **Worker token ceiling bites on review loops, not on the first task.** The same instance did the design pass at 17k tokens, then 34k, then 69k across two review round trips (warning threshold 65k, critical 85k). Pattern: one long-lived instance for iterate-to-green is right, but budget ~3 review rounds max, then respawn. Keep review queries narrow (ask for one section verbatim, not the whole doc).

## 2026-11-04 — ## S7c phase A: independent verification caught a false inva...

## 2026-11-04 — ## Live tooling friction — gen57, S7c session (reported, not...

## 2026-11-04 — ## Pause looks exactly like "stale" to the watchdog (S7c int...

## 2026-11-04 — ## LIVE tooling problems hit while working (Gen 58 onboardin...

## 2026-11-04 — ### `git_read diff` returns an EMPTY body when `file_path` i...

### `git_read diff` returns an EMPTY body when `file_path` is a plain string — pass a LIST
Cost me a false alarm ("worker's edits are missing") and a whole extra worker round trip. `git_read(operation="diff", file_path="tests/x.py")` → empty output + `execution_mode: containerized` (looks like "no changes"). Same call with `file_path=["tests/x.py"]` → the full patch. Every earlier successful diff I ran happened to pass a list, which is why this surfaced only now. Rule: **always pass `file_path` as a list** to `git_read diff` (and probably `diff_cached`/`log`/`blame` too). Companion to the existing notes that `git_read status` returns empty and `show_file` ignores `line_start`/`line_end`.

### Commits made in the session ARE visible on the host checkout
The container mounts the same repository the operator works in (`/workspace` in-container == `~/PycharmProjects/ThoughtMachine-dev` on host; the worker reports host-style paths while writing container paths). So "the worker must push / must export format-patch" is never needed: the operator only has to `git push` from the shared worktree. Verify with `git log --oneline` rather than assuming a separate clone.

## 2026-11-04 — ### Workers' narrative can describe a design the code did no...

### Workers' narrative can describe a design the code did not implement (gen 59, W9) — verify the wire schema
W9a's report said it shipped `ConfigAck{phase: accepted|applied, fields: {field: applied_live|requires_respawn}}`. I relayed that to the human as fact and wrote it into commit message `5f07db5d`. The next worker, reading the actual code, found `ConfigAck{request_id, ok: bool, tier, status}` — a **single** tier for the whole update, no per-field map, no phase split. The first worker's own fine print contained the contradiction ("`ok=True` + `tier=requires_respawn` is still an accepted frame") and I did not reconcile it against the headline. Lesson: for any protocol/schema change, ask the worker for the **literal field list of the dataclass** and treat the prose summary as a claim, not a record. A wrong commit message on a pushed branch cannot be amended; correcting it costs a follow-up commit.
Related: the *fix* here was cheap and good — per-field granularity was delivered statically instead (`config_field_tiers()` derives the tier map from `config_requires_respawn` by one-field-differing probes, so the field list is still written down once). A static tier map + "highest tier wins" per update is honest enough for the UI, which is what let the mismatch stand.

## 2026-11-04 — ### Worker-side tool friction: FileEditor read ranges (gen 5...

### Worker-side tool friction: FileEditor read ranges (gen 59)
Inside the worker, `FileEditor` refused its line-range read parameters (`line_numbers=45` and `line_numbers=[40,90]` both rejected); `FilePreviewTool` with a `"40-90"` range string worked instead. Combined with the orchestrator-side finding that `git_read show_file` ignores `line_start`/`line_end` and dumps whole files, **neither side of the system can currently read a bounded slice of a large file**. On a repo with multi-thousand-line files (`web_ui/backend/server.py`, `tools/workspace/worker_runtime_main.py`) that is a real token sink and it pushes agents toward guessing line numbers. Also note: hand-captured line numbers from earlier sessions were **stale by 20-60 lines** in this very task (parent `_route` was at :757, not :815 as briefed) — always instruct workers to resolve symbols by name, never by number.

## 2026-11-04 — ### `git_read show_file` ignores line_start/line_end (gen 59...

### `git_read show_file` ignores line_start/line_end (gen 59)
Called `git_read operation="show_file" path="web_ui/backend/server.py" line_start=1615 line_end=1690` to inspect a 76-line region. The tool returned the **entire file** from line 1, truncated at ~10000 tokens of output. That is ~10k tokens burned to read 76 lines, and the requested region was not even reached. Same family as the already-logged `git_read log` ignoring its ref/commit argument and `for_each_ref --pattern` ignoring the pattern: **the read tools silently drop optional selector parameters and return everything.** Workaround: rely on `git_read diff` hunks (they carry ~3 lines of context around the change and are correctly scoped), and ask the worker for the surrounding code instead of reading it myself.

## 2026-11-04 — ### Orchestrator tool friction (gen 59, live)
- `Worker(acti...

### Orchestrator tool friction (gen 59, live)
- `Worker(action="spawn")` **rejects** a call that omits `worker_name` with `"worker_name is required for action 'spawn'"`, even though the system prompt says a default worker is always available and no name is needed. Must pass `worker_name="default"` explicitly.
- `git_read operation="status"` returned **empty output** (only the `execution_mode` / `failure_reason` trailers) — no way to read the working-tree state. `git_read operation="branch"` works and correctly marks HEAD with `*`, which is the only reliable way to confirm a checkout landed.
- `git_write operation="checkout"` **does** move HEAD (the earlier "working tree HEAD not moved" was a `branch_create` limitation), but the guard refuses it when the context token budget is critical (`Tool 'git_write' not allowed. Reason: token`). Summarize first, then checkout.
- Worker (DockerCodeRunner) containers **do not share `/tmp` between separate calls** — a fixture/repro prepared in one call is gone in the next. Any multi-step shell setup (symlinks, temp dirs) must be re-created inside the SAME script as the command that consumes it. This silently produced two false-negative test runs for the worker.
- Worker recon on a root-cause investigation consumes the whole budget: 413 s soft-timeout, 142 tool calls, 52k tokens, **zero edits**. For "investigate AND fix" tasks, split: recon spawn → then `action="query"` on the same worker with an explicit "write the edit in the first third of your budget" instruction, so its context is reused cheaply.
- A briefing path supplied by the human was wrong (`web_ui/backend/tests/integration/test_session_loaded_contract.py`); the real file is `tests/integration/test_session_loaded_contract.py`. Instruct workers to *find* the file, not to trust the path.

## 2026-11-05 — ### W10 briefing audit — three hazards worth remembering

1....

### W10 briefing audit — three hazards worth remembering

1. **Revert-proofs in the shared tree are unsafe across tool calls.** A worker left `tools/workspace/worker_thread.py` with the fix REVERTED (to prove the new test goes red) and stored its backup in `/tmp` **inside its executor container**. Containers are ephemeral per call, so `/tmp` vanished, and a 300s dispatch hard-timeout then fired with the tree in the broken state. If I had committed without an independent `git diff`, I would have committed the revert. **Rule: a mutation/revert proof must be applied and reverted inside ONE tool call.** Independent-tree verification before every commit is not optional.

2. **The "silent string-context drop" was not reachable.** The audit found `_action_spawn` assigning `{}` for any non-dict `context` and declared it a real user-facing bug ("`Worker(spawn, context="text")` silently discards the brief"). It is in fact already covered by a pydantic `@field_validator(..., mode='before') coerce_context` that wraps a string into `{"query": v}`. **Reading one assignment site is not the same as knowing the reachable behaviour — trace validation/coercion layers before calling something a bug.** The honest framing (defence-in-depth for plain assignment, which pydantic v2 does not validate) is what shipped.

3. **Mutation-proof the property, not just the assertion.** For the "briefing survives compaction" test the worker initially could not show red. The decisive mutations were two *different* ones: (a) keep only one leading system message → drops the brief → test red; (b) make compaction a no-op → test red. (b) is the one people forget: without it a test can pass **vacuously** because the code path never pruned anything. Always ask "what makes this test fail?" *and* "what if the feature is inert?"

**Also confirmed (harness fact):** `Worker(spawn)` hard-timed out at 300s while the instance stayed `busy` and alive; `action="query"` recovered it cheaply and it still had all its work in context. Two such recoveries this session. Do not respawn on timeout.


## 2026-11-05 — ## 2026-11-05 — Recon dispatch pattern that worked (217s, 59...

## 2026-11-05 — Recon dispatch pattern that worked (217s, 59 tool calls, high confidence)

The best recon result this session came from a brief that (a) listed the exact files that DO exist (verified by me beforehand via `ls_tree`) so the worker did not spend its budget rediscovering them, (b) named the suspect symbols the requester had claimed and instructed "verify by name, never by line number", (c) told the worker explicitly that "an honest 'this does not exist' is worth more than a plausible guess", and (d) demanded `<80 lines`, one-line quotes, and an `UNVERIFIED` marker on every unproven claim. Result: it confirmed two of three guesses, falsified the third with evidence, and produced a 7-step plan with named symbols.

Second pattern: for a task that must never load a huge file (118k-token KB), forbid file-read tools outright and require a *script* whose printed output is capped at N lines. The worker verified a byte-preserving move with sha256 on both slices plus a reversibility check, touching nothing else. Always ask "what did the move destroy?" — it caught that the file's protective "do not read this whole" banner had been moved into the archive and restored it to the live file.

Lesson: archiving is not the fix for an oversized KB. 118k → 59k is still unreadable by the agent that owns it. The real fix is an index/pointer discipline, not a bigger split.


## 2026-11-05 — ## 2026-11-05 — A recon can be overtaken by a merge; re-read...

## 2026-11-05 — A recon can be overtaken by a merge; re-read the branch that owns the subject before you implement from it

**What happened.** I spent a W11 item reconnoitring `docs/design-worker-lifecycle.md` §Q2C (an orphaned-container reaper) and concluded "the doc overrides my plan, implement it as written". Between that recon and the next dispatch, PR #284 merged **S7c-1/2/3** into `dev` — a worker-lifecycle refactor that added a ping/pong protocol, a production-wired stale watchdog, and a gated ping→cancel→kill escalation ladder. The doc had not changed, so it still read as authoritative, but its *liveness* half had in the meantime been implemented twice over. Implementing §Q2C verbatim would now have meant a second liveness ladder beside a working one.

**Rule.** A design doc's shelf life is bounded by the merges that touch its subject. Before implementing from a doc, re-check (a) `git log` on `dev` for commits whose message names the same subsystem, and (b) whether the symbols the doc says are missing now exist. Cite the doc for *intent*, the code for *state* — and when they disagree, the code wins and the doc gets a correction.

**Corollary — "announce-only" is a claim with a date on it.** S7c-1's own commit message says it left the scan with no production caller and left kill unreachable. Two commits later both were false. When a recon quotes a guarantee ("never kills", "no caller"), record the commit it was read at, or the guarantee will be quoted back as current fact long after it stopped being true. This bit the same way a stale `path:LINE` does: **a line number drifts, a guarantee expires.**

**Corollary — when a landed refactor overlaps your plan, shrink the plan to its net-new core.** The useful question stopped being "does the doc's five-part plan exist?" and became "which of these five parts does the new code already do?". Answer: four of five were already delivered in spirit by S7c; the genuinely missing piece was the one thing an in-memory registry structurally cannot provide — ownership evidence that survives the owning process (container labels + a persisted `owner_boot_id`). Duplication, not absence, is the expensive failure: a redundant second reaper is worse than no reaper, because it acts.

**Also re-verified this session:** symbol names in commit messages drift too — S7c-1's message names `FRAME_TYPES`, the table is actually `MESSAGE_TYPES`. Verify by grep, never by quoting a commit message.

## 2026-11-05 — Live-testing a permission fix differs from unit-testing it: ...

Live-testing a permission fix differs from unit-testing it: unit tests that construct the subject with explicit kwargs (e.g. `Worker(..., workspace_path=PARENT_WORKSPACE_PATH)`) cannot catch "the production config never populates that kwarg". Always check the runtime value of the injected input (`CheckSystem my_config`) before trusting a green suite.

Worker spawn facts learned this session:
- `Worker(action="spawn")` REQUIRES `worker_name` (the tool docs claim it defaults; it does not) — omit it and you get `{"error": "worker_name is required for action 'spawn'"}`.
- `CheckSystem` is NOT in the default worker's tool surface, despite the worker system prompt advertising it; don't route diagnostics that need CheckSystem through a worker.
- A permission-denied worker is a total dead end: filesystem, git AND DockerCodeRunner all fail closed through the same sidecar lookup, so there is no side-channel probe (container `pwd`) available from inside.
- Determinism check (spawn a second worker definition with a one-call probe) is cheap (~6s) and worth it before declaring a bug.
- `elapsed_seconds` well under `timeout_seconds` + `confidence: high` + verbatim error text = trustworthy report; both workers here returned exactly that.

