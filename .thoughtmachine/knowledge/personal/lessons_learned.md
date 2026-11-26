# Lessons Learned — index (2026-10-29)

Hot file. The full lesson log (2025-04-11 to 2026-10-29) was archived on
2026-10-29 to `personal/lessons_learned_archive_2026-10-29.md` (569,256 B,
2,671 lines, 393 `## ` headings). This file is the index and the live append
area; the archive is the source of truth. Do NOT rename or move either file:
`tools/knowledge_base.py` hard-codes `personal/lessons_learned.md`, and
`_mode_status` reads the first Current Status section from
`personal/task_tracker.md`.

**Pointer format.** `archive:LNNNN` = line NNNN of the lessons archive **as it
exists today**. A 4-line header was prepended during the restructure, so
**archive line = original line + 4**; every number below is already post-shift.
Re-check any pointer with `sed -n "NNNN,NNNN+6p"`.

> Older entries (63 of 120) archived verbatim to personal/lessons_learned_archive_2026-11-05.md. Do not rewrite those entries here.

## 2026-11-03 — ## SUBSTRATE-AUDIT STANDARD: verify symbols against ASSIGNME...

## SUBSTRATE-AUDIT STANDARD: verify symbols against ASSIGNMENT sites, not grep hits

Promoted from a one-off finding to a standing rule after the S3b audit (engineer concurred; this is not a single bug, it is a class).

### The defect class: **ghost status / ghost symbol**
A name that appears in a comment, docstring, type stub, test, or design doc, and therefore reads as real, but is **never assigned** (for a status), **never called** (for a function), or **never constructed** (for a class) in production code.

Confirmed instance: worker status `stopped`. It appears in the declaration comment at `worker_runtime.py:1103`, so it looks like part of the vocabulary. It is **never assigned at any of the 16 status-assignment sites**. The real vocabulary is `ready, busy, paused, completed, error, detaching, detached, killed`. There is also no validation tuple anywhere, so nothing would have caught a predicate keyed on `stopped` — it would simply never fire, forever, silently.

Related instances found in the same audit:
- `WorkerRuntimeHandle(...)` — looks like production infrastructure; constructed **only** in `tests/test_worker_runtime_protocol.py` (3 sites).
- `resolve_runtime_mode` / `WORKER_RUNTIME_MODE_ENV` / `MODE_PROCESS` — look like the mode-routing surface; **zero production references** outside the runtime files themselves.

### The rule
Before citing any symbol in a brief, spec, predicate, or edit:

1. **Definitions are not evidence.** `grep -rn "symbol" --include=*.py` returning hits proves the name *exists*, not that it is *reachable or meaningful*.
2. **For a status/enum value:** grep for **assignments** (`= "value"`, `state =`, `.status =`), not for the string. A value with zero assignment sites is a ghost.
3. **For a function/class:** grep for **call sites outside `tests/`**. If every hit is a test, the thing is a test fixture that escaped into production vocabulary.
4. **For a config/env flag:** grep for **readers** and **writers** separately. A flag with a resolver and no readers outside its own module is not a feature, it is a placeholder.
5. **Comments and docstrings are the primary source of ghosts.** They are written when someone *intends* the vocabulary and never updated when the intent is dropped. Treat a name that appears only in prose as unverified.

### Why this is a substrate standard and not a gotcha
Ghost symbols are worse than missing symbols. A missing symbol fails loudly at import or call time. A ghost **type-checks, imports, lints, and runs**, and simply never matches, never fires, never routes. The failure is silent and unbounded in time. Every predicate, dispatch table, and state machine in this codebase should be read with this rule in hand.

### Corollary for brief-writing
If a brief's implementation plan names a symbol and step 3 above fails, the brief is describing work at stage N+1 wearing stage N clothes. Push it back to staging rather than letting a worker discover the absence mid-edit. (Three such briefs so far: S4-in-S2, S4-in-S2 again, S4-in-S3b. All caught pre-dispatch by audit, none by authoring care.)

## 2026-11-03 — ## S3b dispatch: what worked, and two tooling defects hit li...

## S3b dispatch: what worked, and two tooling defects hit live

### Verification pattern that earned its cost
S3b was dispatched as one long `query` (~465 s, 128 tool calls, 74 k tokens) and then **independently re-verified by a second worker** (spawn, 81 s, 64 calls) before commit. The second worker only ran greps + the pytest gate and reported raw output. That is the right split: the implementing worker is allowed to be optimistic; the verifier is not allowed to fix anything. Checks that actually caught risk:
- re-run the gate and confirm the exact pass count + exit code (not "tests pass"),
- `grep -n "skip\|xfail\|\.only"` on the new test files — a *grep exit 1* is the passing result,
- `grep -rn` the new constant to prove **one definition**, not a copy per module,
- `grep -n "^def \|^class "` to see definition order (a function may legally reference a later global; I wanted to *see* the order rather than assume it).

### TOOLING DEFECT 1 — `git_read` `diff` returns EMPTY for a single-element `file_path` list
`operation="diff"` with `file_path=["web_ui/backend/workspace_routes.py"]` returned **only the two trailer lines and no diff**, while `git status` showed the file as ` M`. The same call with a 2-element list worked, and with no `file_path` at all it worked. So: **single-element list ⇒ silently empty diff.** Silently, as in "looks like the file didn't change". That is a dangerous false negative for an agent whose whole verification story is "I read the diff". Workaround: omit `file_path` and read the whole diff, or pass ≥2 paths. Worth fixing in the tool.

### TOOLING DEFECT 2 — `spawn` reports `spawned: false` for a worker that ran
The `default2` spawn returned `"spawned": false, "status": "ready"` in the envelope while the worker had in fact executed all five checks and returned a full report (80.8 s, 64 tool calls). The content was correct; the envelope's `spawned` flag was simply wrong/false-negative. Don't infer "it didn't run" from that flag — read `content` and `telemetry`.

### Other measured notes
- `query` on a warm worker ran **465 s**; the earlier belief that ~454 s was the ceiling is fine but 600 s `timeout_seconds` did not trigger a soft timeout, so budget ~475 s of real work per query.
- The implementing worker still lost budget to path confusion: my brief said `tools/web_ui/backend/routers/workspace_routes.py`, the real file is `web_ui/backend/workspace_routes.py`. **Verify exact paths in the brief before dispatch** — same failure class as the ghost-symbol problem, one layer out.
- DockerCodeRunner's default timeout is 30 s, so any pytest invocation must pass `timeout=300` explicitly; the 5-file gate takes ~36 s.
- A worker will quietly retire *more* old tests than the brief names when behaviour changes: S3b retired **two** tests encoding the old "must be busy" rule (control + protocol), and only one was named in the brief. Ask explicitly: "list every existing test that encodes the contract you are changing."

### On scope discipline
Three consecutive briefs from the engineer pulled S4 work into an earlier stage; all three were caught pre-dispatch by auditing the tree, none by the brief being careful. The audit is the control, not the author's diligence. Cost of the audit: 104 s. Cost of not doing it: a worker editing toward a target that does not exist.

## 2026-11-03 — ## S4 audit: the frame surface has no transport seam, and si...

## S4 audit: the frame surface has no transport seam, and six brief claims are false

Audit of the engineer's S4 brief (`design-worker-runtime-process.md` §10) against the tree @ `32173f5a`. Read-only, 118 s. Verdict: **not clean — do not dispatch as written.**

### The architectural gap (brief does not mention it at all)
The brief says `_publish_event` / `_publish_global_event` / `_heartbeat_tick` / the status writer "must emit Event frames in process mode". **They cannot, and there is no "in process mode" branch to take.**

- `WorkerThread` has **zero transport capability**: greps for `_outbox|sendall|self._sock|FrameEmitter|encode_frame` in `worker_thread.py` → **zero hits**. `__init__` (972-990) has no socket/outbox/emitter/mode parameter.
- `_Outbox` (`worker_runtime_main.py:91`) lives **exclusively** in the child runtime; `put()` (113) is a non-blocking `queue.Queue.put` and the ONLY `sendall` is in its single writer thread (`_run`, 117-133). So the §4.6 single-writer guarantee is intact and emitting from a worker thread would be safe — but nothing hands the worker that queue.
- **No spawned-child signal exists** in `worker_thread.py`: zero hits for `os.environ|sys.argv|getenv|WORKER_RUNTIME_MODE_ENV|MODE_PROCESS|_runtime_mode`.

⇒ S4 must **first add the sink** (a `frame_sink` kwarg threaded through `_build_worker(factory, kwargs)` at `worker_runtime_main.py:444/458`, which passes opaque kwargs to `WorkerThread(**kwargs)`). Injection is cleaner than a mode flag: `if self._frame_sink is not None` *is* the mode test, and it needs no new global.
⇒ Likely **pre-existing** indirection worth checking before adding anything: `self._worker_bus_adapter` (1170, 2317, 2326, 2442-2445, 2565-2566) and the `emit_*` adapter methods (424/430/436/442/448/462). If events already funnel through those, the sink belongs in the adapter, not in `_publish_event`.

### Good news the brief did not know
`_write_status_file` (`worker_thread.py:3757`) is **ONE choke point** — a single def writing 10 keys atomically (tmp + `os.replace` under `FileLock`, 3779-3793) with **21 call sites** (1548, 1574, 1594, 1767, 1948, 2058, 2117, 2385, 2400, 2734, 2892, 2948, 3061, 3134, 3222, 3277, 3411, 3450, 3812, 3868, 6035/6060). So the brief's claim 3 ("writes status.json at many points") is right about the *call sites* but wrong about the *implementation*: hooking it is a ~20-line job, **not** a refactor of 21 sites.

### False claims (quote these)
1. **`worker_bus` does not exist.** Only the log tag `'pipeline.worker_bus'`, `_worker_bus_adapter`, `EventLogger.attach_worker_bus`, and `bridge._worker_bus_subs`. The real per-worker bus registry is `WorkerRegistry._worker_event_bus_registry` (`worker_registry.py:62`) via `register_event_bus`/`unregister_event_bus`/`get_event_bus`/`get_event_buses_for_session` (151/158/165/172). `bridge.py` (`web_ui/backend/bridge.py`) already discovers buses through `get_event_buses_for_session` (800-801) and `_worker_bus_is_stale` → `get_worker_event_bus` (765-788) — so "bridge needs no change" is **achievable, with the right names**.
2. **`paused_manually` is not a `WorkerThread` attribute.** Derived from `_manual_only_pause` by design (`worker_runtime.py:338-340`, used at 357).
3. **`_heartbeat_tick` does NOT write `status.json`.** It publishes to the *global* bus only, interval-gated on `HEARTBEAT_INTERVAL_S` (30 s), and its docstring says file writes are the other machinery's job. So claim 2's premise is wrong.
4. **`status` vs `runtime_status`.** The status file key is `runtime_status` (`worker_thread.py:3768`); the `WorkerState` frame field is `status` (`protocol:508`). The protocol docstring's "field names match status.json verbatim" is only 3/4 true.
5. **`Heartbeat` frame has no `current_task`** — fields are `at, status, current_context_tokens, max_context_tokens` (protocol:486-489).
6. **`_heartbeat_age` is not one function — there are three staleness implementations**: `worker_thread._heartbeat_summary` (5204, `HEARTBEAT_STALE_AFTER_S`), `worker_manager._heartbeat_age` (336, reads `last_heartbeat` then `_last_heartbeat_monotonic`), `worker_lifecycle._heartbeat_age` (130, own dict, `_is_stale` 143/`_is_hung` 148). ⚠️ The first two take a *thread object* — the supervisor has no local thread in process mode, so **process mode needs its own clock owner**. This is a design decision, not a code edit.

### Confirmed TRUE
Protocol `Event` (protocol:457), `Heartbeat` (471), `WorkerState` (493) all exist, all `@dataclass(frozen=True)`. **`Event` frames have zero production construction sites** (only `threading.Event()`/`asyncio.Event()`/`BaseEvent(`) — claim C2 holds. `WorkerRuntimeHandle.state()` process-mode really does `raise NotImplementedError` (347, message points at §10). `CANCEL_TERMINAL_STATUSES` still `frozenset({"killed","detached","error","completed"})` at `worker_runtime.py:85-86`. `web_ui/backend/event_forwarder.py` **does exist** (but not under `tools/workspace/`).

### Method note
The audit worker lost the verbatim text of five claims (C1/C3/C4/C5/C7) to context summarisation and said so instead of guessing — correct behaviour, but it means **long numbered claim lists should be re-supplied in a second batch**, or the audit should be split so no single query carries more claims than survive a summarisation. Ask for "the FALSE list" explicitly and early, not at the end.

## Substrate-Audit Standard

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

## Live tooling / orchestration problems observed (reported to engineer)

Session where S5-A was committed (`w8/p3s5` @ `a7adf6af`). These are defects in the
agent/worker harness, not in the product code, and each one cost real time.

1. **`Worker(action="spawn")` without an explicit `worker_name` errors.**
   The tool prompt states the default worker is used when no name is given, but the
   schema/call fails without one. Prompt and schema disagree. Workaround: always pass
   `worker_name="default"` explicitly.

2. **Spawn with `force=True` returned `spawned:false` / `status: busy` and my dispatch
   never reached its target.** Worse: worker `default2` was found holding a FOREIGN
   context ("D3+D4 per-worker EventBus registry") from another assignment under the
   same session_id. Instance-level dispatch appears to alias/misroute between workers.
   Lesson: never trust that the context you get back is the one you sent; verify with a
   known-answer probe (ask the worker to echo a unique nonce) before relying on output.

3. **`action="stop"` detached a worker while a pytest run was in flight**; the in-flight
   result was lost, and the detached instance still occupied instance 1, blocking a
   clean respawn. Detach is not free even though it is "recoverable": it can eat a
   pending result.

4. **Calling timeout ceiling is ~420s regardless of `timeout_seconds`.** Passing
   `timeout_seconds=900` still soft-killed a coding worker at 374s mid-task. Budget
   sub-tasks at well under 300s of worker wall clock, and check
   `elapsed_seconds >= timeout_seconds` on every envelope to spot soft timeouts.

5. **Running the whole `tests/` directory HANGS with no protection** (no pytest-timeout
   plugin). Two workers were destroyed on it. Always enumerate named test files
   explicitly; never `pytest tests/`.

6. Minor git-tool ergonomics: `git_read diff` requires >=2 paths; `show_file` cannot
   read untracked files and ignores line ranges on large files; `branch_create` does
   not move HEAD (a separate `checkout` is required); `WorkingDocument create` ignores a
   supplied `doc_id` and `read` returns nothing without an explicit `section=`.

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

## Reproducing macOS-class path bugs on Linux + reading CI reds correctly (macOS CI red on w8/p3s6b / PR #281)

1. **TMPDIR-symlink trick (reusable):** macOS runners hand out `/var/folders/...` which is itself a symlink to `/private/var/folders/...`, so `realpath() != abspath()`. Reproduce that class on Linux in one line:
   `mkdir -p /tmp/tmreal && ln -sfn /tmp/tmreal /tmp/tmlink`
   then run the test with `TMPDIR=/tmp/tmlink`. Identical failures/warnings to the macOS CI log. Plain `TMPDIR` passes — proving the bug is symlink-shaped, not logic-shaped.

2. **`str(AttributeError(name))` on a pydantic v2 model renders as the BARE name, no quotes.** `ModelMetaclass.__getattr__` raises `AttributeError(name)`, so a log line reading `Could not backfill ... from_agent_config` looks exactly like an arbitrary "config source" label. Do not be fooled by swallowed-exception log text: grep the called method and confirm it exists. `SessionConfig` (pydantic v2) only has `from_factory` / `to_agent_config` — `from_agent_config` does not exist anywhere in the repo, so `web_ui/backend/bridge.py:2592` and `agent/presenter/session_lifecycle.py:105` are dead/unguarded calls respectively. A blanket `except Exception` mislabelled the regression as a registry failure.

3. **Read the job name for "(advisory)" and the workflow's `runs-on:` BEFORE believing a red check is a merge blocker.** `platform-matrix.yml` → `macos-suite` (job display name literally `macOS - full test suite (advisory)`) carries `continue-on-error: true` — it cannot fail the workflow. `python-version-matrix.yml` has no macOS runner at all. Verify the workflow/runner/Python version from the log itself rather than trusting the reporter's framing; here the report said "Py 3.12 / 1 failure" while the log said "Python 3.14.10 / 3 failed".

4. **Frozen caches break fallbacks:** `_build_workspace_id_cache` early-returns `if _workspace_id_cache:`, so a just-registered workspace is permanently absent, and the fallback path (`registry.resolve_by_root`) compares a `realpath(abspath())` query against abspath-only-normalised stored roots → mismatch → `None`. When a lookup "sometimes" returns None, check whether one side normalises more aggressively than the other.

## 2026-11-03 — ## Post-restart smoke probe: thread→process worker migration...

## Post-restart smoke probe: thread→process worker migration (runtime f4324ecf, dev)

First worker call after the migration to process-based workers succeeded and was FAST/normal:
- `Worker(action="spawn", force=True, timeout_seconds=90)`, trivial 4-command probe → `status: final`, `confidence: high`, **18.6 s**, 4 tool calls, 18 095 tokens, `timeout_triggered: false`. No degradation, no heartbeat freeze, no `meta.blocked_by`. So the process model is healthy on a canary.

Environment facts observed (useful and non-obvious):
- Worker container reports **Python 3.12.15** and `pwd = /workspace`. CI's macOS job ran **3.14.10** — local/container runs therefore CANNOT catch 3.14-only failures. Keep that in mind before declaring "passes locally".
- `/workspace/.git` inside the worker container is a **0-byte regular FILE** (not a directory, not a valid `gitdir:` worktree pointer). So the container workspace is effectively git-less by construction: no git binary, no usable repo metadata. Never trust worker-quoted repo state; all git archaeology stays with the orchestrator's `git_read`/`git_write` tools (which report `execution_mode: containerized` and DO work — they clearly use a different container than the worker's shell).
- `CheckSystem(runtime_state)`: `worker_limits` all `null` (`max_workers_per_session`, `worker_timeout_seconds`, `worker_max_retries`) → no enforced caps visible from the agent side; `enabled_tools_count: 0` even though tools are clearly working, so don't read that field as "no tools"; `session_sandbox_status: stopped`, `container_status: stopped` even though a containerized command then ran fine → **both statuses are reported before lazy start, not a fault indicator.**
- Effective perms at restart: filesystem `write`, container true, git `write_on_feature_branch`, network/host_bash/ci/mcp/credential/visual all `banned`.

## 2026-11-04 — ## Epoch fencing landed (S7a-1) — patterns worth reusing

**...

## Epoch fencing landed (S7a-1) — patterns worth reusing

**1. "Fail open on TYPE, never on ORDER."** When threading a version/epoch through an existing API, coerce at ONE choke point (the registry), not at 10 call sites: `_coerce_epoch = epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None`. Mocked objects are everywhere in tests; their `_generation` is a `MagicMock` and it silently reaches int comparisons. One helper beat ten `isinstance` guards.

**2. Coercion at the choke point does NOT cover RAW writes to the same data.** Worker_thread's spawn path wrote straight into `_worker_epochs` (bypassing `register_worker`, because calling it would deadlock on a non-reentrant lock), so the MagicMock slipped past `_coerce_epoch` and poisoned the shared singleton map — causing 3 test failures that PASSED individually (ordering-dependent, because the poison persisted between tests). Lesson: whenever a guard lives in a method, audit every path that touches the underlying state directly. Symptom of a poisoned singleton map = "passes alone, fails in module order".

**3. Exact-match CAS on a pop is nearly right, but misses self-ownership.** If an incarnation's own generation is re-allocated upward mid-life, an exact-match pop refuses and the entity leaks its own entry. `stored <= epoch` (refuse only when `stored > epoch`) is both safe (records are monotone, so only a strictly newer owner can look newer) and tolerant. ALSO: raise the record on a successful pop to `max(stored, epoch)` — otherwise `register(e=5) -> unregister(e=7) -> register(e=6)` re-admits the stale writer.

**4. Mutation-check wiring tests.** A test that asserts "the production call passes an epoch" is worthless if a future refactor drops the kwarg and the test still passes. Prove deletion-provability: apply the mutation in a single shell invocation with a `trap` restore, watch the test FAIL, then confirm the file is byte-identical (`git diff`). Turns a claim into evidence, costs ~1 min.

**5. Test doubles are the real compatibility surface of a Registry API.** Adding one additive keyword-only arg broke 11 tests across 4 modules purely via local `_FakeRegistry` classes. Budget for this: grep `tests/` for the method names BEFORE changing the signature so the blast radius is known up front.

## Tooling complaints (live)
- `git_write` has no `branch_delete` — cannot clean up dead branches; must ask the engineer.
- `git_read` has no `diff_stat` / `--stat` operation, so getting a commit's file-by-file line counts needs a full `diff` read (expensive on multi-file commits).
- Worker container: `git` is absent (exit 127) AND `/workspace/.git` is a 0-byte file, so a worker can never verify repo state or produce its own diff. All git is on me. A worker asking to "check `git diff`" will fail confusingly.
- `DockerCodeRunner` default timeout is 30 s — a normal pytest module run exceeds it; every test ask needs an explicit large timeout.
- `FileSearchTool` refuses repo-wide search and renders matched phrases in bold, which the worker then copied verbatim into `ApplyEdits` find-strings (needed two retries). Cosmetic but wastes a turn.
- `ReadFile` ignores line ranges on large files; `sed -n` is the only reliable window.

## 2026-11-04 — ## Live tooling fault: `Worker(spawn)` returned an empty env...

## Live tooling fault: `Worker(spawn)` returned an empty envelope ("Unknown error") on a healthy worker (2026-11-03, S7a-2)

**Symptom.** `Worker(action="spawn", force=True, timeout_seconds=600, purpose=coding, context={query, briefing})` on the default worker returned `{"content": "Unknown error", "status": "error", "confidence": null, "meta": null, "telemetry": {"elapsed_seconds": 151.3, "tool_call_count": 4, "token_usage": 80108}}`. No `timeout_triggered`, elapsed 151 s against a 600 s budget, so this was NOT a soft or hard timeout.

**The worker was fine.** `Worker(action="check")` immediately after: `status: ready`, `alive: true`, `error: null`, `last_heartbeat_age_s: 0.5`, `conversation_length: 127`, 80 108/128 000 context tokens. A follow-up `query` answered in 3.9 s and confirmed **nothing had been applied** — it was mid-probe (4 read-only `sed`/`grep` calls) when the dispatch died.

**So the failure is in the dispatch/reply path, not in the worker or the task.** Two concrete observations for whoever owns the worker plumbing:
1. The reply envelope was dropped or unparseable while the worker kept its full context and its liveness. **My task state survived; only the answer did.** Nothing about `force=True`, `purpose`, `style` or the timeout value looked implicated — the one unusual input was a very large `context` (query + briefing ≈ 9 KB of text).
2. The recovery `query` came back with a **doubly-encoded envelope**: the tool's `response` field held a JSON *string* `{"content": "...", "status": null, "confidence": null, "meta": null, "telemetry": {...}}` — i.e. `status`/`confidence`/`meta` were `null` inside the inner document rather than the `final`/`high` the outer call would normally report, and `query_id` appeared at the inner level. Two different serialisation paths for the same envelope shape, and the spawn path's one can evidently emit a null body.

**What saved the work.** Treating the error envelope as "unknown, not failed": `check` then `query` (the worker's context is not lost on a failed dispatch), then re-issuing the *continuation* rather than a fresh spawn. A fresh `spawn` would have thrown away 80 k tokens of probe work.

**Operational rule now in force.** After any non-`final` spawn envelope: `action="check"` first (is it alive? how much context?), then `action="query"` asking for *state only, in under 250 words, do not restart* — never re-spawn before establishing whether edits were applied.

## 2026-11-04 — ## S7a-3 slice: orchestrating a worker through summarisation...

## S7a-3 slice: orchestrating a worker through summarisation + soft timeout (commit 8e68447b)

**The failure that mattered:** I handed the worker a ~4 KB spec inline. It received it, then its own context summarisation pruned it, and it continued with only the *constraints* left — the spec itself was gone. It then had to reconstruct intent, which is exactly where unrequested design decisions appear. Fix that worked: pin the canonical spec as a section in a `WorkingDocument` and send the worker only a short pointer + the 3-4 rulings it must obey. This also survived a mid-task soft timeout, because the spec lived outside the worker's context entirely. **Pattern to reuse: spec in the doc, rulings in the briefing, evidence in the response.**

**Soft timeout:** one worker hit ~568 s against a 600 s budget, reported progress honestly on the way out, lost nothing. Detect it by `elapsed_seconds` ~= budget rather than `timeout_triggered` (that stays false for soft timeouts). Chunk by *verifiable unit* (one file + its tests), not by "half the feature".

**Reviewing a worker's own deltas:** the worker volunteered 5 deltas from spec. Three were sound and I sanctioned them; one (`test_worker_runtime_seam.py`: replacing a `registered_handles: 0` world-state literal with an agreement check) needed an explicit ruling *plus* an explicit prohibition of the obvious alternative (unregister on every exit), because the alternative would have looked like a leak fix while silently breaking parked-worker resume. **When you rule on option A, name the rejected option and why — otherwise the worker re-derives it later and "fixes" it.**

**Source-grep tests:** the worker's "pin the spawn site" test greps `_action_spawn`'s source because a real spawn needs an agent definition, vault and LLM turn. Accepted as a *tripwire* (fails loudly when the pinned lines move) rather than a behavioural guarantee. Cheap and honest when the docstring says which it is; unacceptable when it masquerades as behaviour.

**Verification order that caught things:** read non-test diffs before test diffs (they carry the risk); re-`md5sum` the tree *after* mutation-restore but *before* commit (cheap 10 s worker task, proves the tree matches the tested bytes); then commit. Do not trust "restored, md5 identical" without re-checking when mutations happened mid-session.

### Tooling faults hit this session (engineer-visible)
- `Worker(action="spawn")` **requires `worker_name`** — the tool description says it defaults to the default worker; it does not. Costs a wasted call and a confusing error, and it contradicts the docs.
- `query` envelopes arrive **doubly encoded** (`response` is a JSON *string*), so consumers must parse twice.
- `git_read show_file` silently ignores `line_start`/`line_end`.
- No `branch_delete`, no `diff --stat` in `git_read`, so the agent must ask a worker for md5/stat facts the tool could report itself.
- One spawn returned `{"content": "Unknown error"}` on a healthy worker; recovery was `check` -> state-only `query` -> continue on the same instance.
- `.worktrees/` mirrors pollute every grep (use `--exclude-dir=.worktrees`); `ReadFile` ignores line ranges, so `sed -n` is the only way to read a slice.

## 2026-11-04 — ORCHESTRATOR SELF-INFLICTED TRAP (deeply embarrassing, entir...

ORCHESTRATOR SELF-INFLICTED TRAP (deeply embarrassing, entirely avoidable): I staged a worker's new files, then sent the worker a follow-up query that EDITED those same files, then ran `git commit`. Both paths showed `AM` -- added-to-index, modified-in-worktree -- and a plain commit would have shipped the PRE-follow-up blobs (480-line file instead of 578) with a commit message describing the follow-up. The worker caught it, not me. RULE: after ANY edit round that follows a stage, re-stage and look at the porcelain first column. `AM`/`MM` = the index is stale; `A `/`M ` = safe to commit. Never trust a stage you did before the last write. Cheap check: `git_read diff_cached` shows exactly what the commit will contain.
TOOL NOTE (useful): `git_read show_file` fails on a path that exists on disk but is not in HEAD ("exists on disk, but not in 'HEAD'"), so a brand-new untracked file cannot be reviewed with it. Workaround: stage the file, then `git_read cat_file` with object=":<path>" (index rev syntax) returns the staged blob. This is the only way I have to read a worker's new file before committing it -- and it doubles as verification that the index holds the version I reviewed.
TOOL NOTE: no `diff --stat`, no `branch_delete`, `show_file` ignores line_start/line_end (reads whole file). For a 578-line file that is acceptable; for worker_thread.py (6507 lines) it is not, so targeted regions must be read back through the worker with `sed -n`.
REVIEW PATTERN THAT PAID OFF: ask the worker to answer pointed falsification questions about its own change ("is this branch reachable in production?", "what is the worst case if X?"). It answered honestly, including a caveat that hurt its own work (duplicate event publish) and correctly identified that caveat as PRE-EXISTING. But the defect I actually caught was one it could not have caught by self-review: a primitive that is correct in isolation yet cannot serve its only future caller (fenced reader vs cross-epoch adoption). Read the primitives against the NEXT slice's needs, not against this slice's tests.


## 2026-11-04 — ## S7b session — live tooling problems (for engineers)

1. *...

## S7b session — live tooling problems (for engineers)

1. **`Worker(action="query")` returns `status: null` and `confidence: null`** in its envelope, while `action="spawn"` populates both. The documented triage rule ("check `status`; check `confidence`; treat `elapsed_seconds >= timeout_seconds` as a soft-timeout") is therefore unusable on follow-up queries — the exact call where I most want to know whether the worker finished or was cut off. Please populate both on `query`, or document that query envelopes never carry them.
2. **`host_bash` unavailable to the worker** ("Tool requires host_bash:allow"). Every test run goes through `DockerCodeRunner`, ~8s of container overhead per invocation, so workers batch everything into one giant shell line and lose cheap iteration. Not blocking, but it distorts worker strategy and inflates `tool_call_count`.
3. **`elapsed_seconds` can exceed the `timeout_seconds` I set without `timeout_triggered: true`** (565.5s against a 540s budget, with a fully coherent final report and verified checksums). Either the soft-timeout fires late or the budget is advisory. I now treat "coherent report + matching md5s + matching suite counts" as the real signal, but the field is not trustworthy as a cutoff.
4. **`tool_call_count` accumulates across `spawn` + subsequent `query` calls** (334 → 340 → 344) instead of being per-call, so it cannot judge whether a single dispatch flailed.
5. **No search/grep or read-range primitive on the orchestrator side**, and `git_read cat_file` returns a whole file. Vetting two functions inside a 790-line module meant either reading all 790 lines or asking the worker to paste them — i.e. asking the party under review for the evidence. A grep/read-range tool for the orchestrator would materially improve review quality.

## Lesson: the orchestrator cannot review what it cannot read

I verified the most safety-critical property of S7b-2b (`clear_inflight` refusing a foreign record instead of unlinking it) only by asking the worker to paste the source. That is circular; it happened to be honest and consistent with the checksums, but the fallback should not be "trust the worker". Mitigation actually used: cross-check the worker's claims against signals it cannot fake cheaply — md5 + line-count stability across turns, a re-run suite count that must match the previously reported count, and reading the diff myself where the file is tracked and the diff is small.

## Lesson: fail-closed primitives make a write point safe by construction

Because `clear_inflight` and `write_inflight` are both fence-aware and refuse rather than displace, the S7b-2b write points inherit safety without any logic of their own: an incarnation that could not claim the directory at turn start is also refused at the park and at the clear, so it can never erase a successor's evidence. Worth designing for: put the safety in the primitive, so every future call site is safe by default rather than by discipline.

## 2026-11-04 — ## Silent envelopes from Worker spawn/query (seen twice, S7b...

## Silent envelopes from Worker spawn/query (seen twice, S7b-3)

On long dispatches the `Worker` envelope can come back ABSENT or with `status: null` while the worker is perfectly healthy. `spawn` reported "did not respond within 660.0s (last heartbeat 339s ago)" and `action=query` hit the same wall twice — yet in every case the worker had ALREADY finished and written its files to disk; a follow-up short query on the same instance answered in 15s ("DONE").

**Act on it:** never conclude "the worker failed" from a missing envelope. Check the WORKSPACE first (`git_read status`, file mtimes, `git_read diff`). Then re-ask with a tiny status-only query (<=15 lines: "paste md5s and test counts") before re-spawning. Re-spawning `force=True` while the old instance is mid-write risks a half-written file. Also, `status: null` / `confidence: null` happens on *spawn* too, not just query — so `status` is uninformative in both directions and evidence must come from the workspace.

**Budget:** `elapsed_seconds` 410.6 against a 420s timeout is one breadth-run from a soft timeout. A `-k worker` breadth run alone costs ~323s; budget ~2x that when a brief includes one, or split "implement" and "verify" into separate dispatches (a pure measurement brief needs only ~5 tool calls).

## `git_read blame` is the read-range workaround (no grep, no sed)

I have no grep and `cat_file` returns whole files (worker_thread.py is 6869 lines). But `blame` accepts `line_start`/`line_end` and prints `<sha> (<author> <date> <lineno>) <source line>` — so it doubles as **the only way to read an arbitrary line range**. Use it to verify a specific claim by line number (this caught real behaviour at 5145 and 5215-5235 in one call each). Cost: ~1 line of output per source line, so read 15-70 lines, not 700. Complain to the engineer: a plain `read_range` would remove this hack.

**Corollary:** `git_read diff <file>` on an `AM`-status file shows working-tree-vs-index, i.e. ONLY what changed since I staged — the cheapest way to review a delta after a silent dispatch.

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

## S7c phase A: independent verification caught a false invariant (2026-11-04)

**The lesson, in one line:** a worker's report of its own safety invariant is not evidence of that invariant. Verify the claim, not the summary.

The implementer reported "announce-only, nothing in the tree can terminate anything" and shipped a plausible guard test (source-level AST scan for `kill`/`cancel`/`terminate_*` verbs). A fresh verifier worker traced the *call graph* instead of the verbs and found the invariant false: `check_stale_transitions()` publishes `worker_timeout` on the **global** event bus (`worker_lifecycle.py:255`), `EventBus.publish` is *synchronous* (`agent/events.py:486`), and `worker_thread`'s own lifecycle observer subscribes to that type with `stale_callback=_on_worker_stale`, which calls `terminate_executions()` / `terminate_all()`. So driving the shared scan from a background thread would have terminated in-flight executions of any worker silent >600s — precisely the hazard the phase split existed to avoid.

Why the guard test missed it: it injected a fake observer, so the real `check_stale_transitions() -> bus -> _on_worker_stale` chain was never exercised; the verb list also omitted `pause`/`stop`, and string dispatch (`getattr(handle, "kill")`) would have passed it. Fix: give the announce observer a private `EventBus()` + `stale_callback=None`, closing both the direct edge and the *published* edge. Lesson for future slices: **in a codebase with a synchronous global event bus, "this thread calls no terminating function" is not the invariant. The invariant is "nothing this thread publishes can reach a terminating subscriber."** Any background thread that drives a publisher needs its own bus.

Second defect the implementer missed and the verifier caught: dedupe bookkeeping that only ever grew (keys of departed workers leaked; a worker that vanished while announced and returned stale was silenced for the process lifetime, because `age is None` skipped the re-arm). Class of bug worth grepping for: **"seen-once" sets must be pruned against the live population each tick**, not only mutated on the happy path.

### Tooling win: staging is the agent's only read path to new files
The agent has no file-read tool and `git_read show_file`/`cat_file` need a committed ref, so a *new untracked* file was unreadable — previously solved by making the worker echo content back (34k–69k tokens per review round). Discovered this session: `git_write stage` the file, then `git_read diff_cached` with a single `file_path` prints it as an all-`+` diff. Read a 441-line new module for ~6k tokens, verified the fix myself, *then* committed. Do this instead of asking a worker to quote its own code — the whole point is to read it without the worker as intermediary. (Note: `show_file` rejects `:` in ref, so `:0` index syntax is unavailable; `diff_cached` is the way.)

### Other observations
- Implementer instance economics: implement → verifier (fresh, 69k) → fix on the *verifier's* instance (it already held the analysis; 54k total). Re-querying the instance that found the bug is far cheaper than briefing a fresh one, and the fix stayed narrowly scoped. Watch the 65k warn / 85k critical thresholds; a fix round on an instance at ~70k is fine, a full re-review is not.
- Spawn window is ~450s; a 6-question review with two pytest runs took 215s, a 5-part fix with tests took 351s. Both fit, but multi-part fixes at 350s+ are the edge.
- Workers still cannot run `git` (no binary) and `host_bash` is banned. Always verify branch state yourself with `git_read status`. Also: a worker will happily report pytest summary lines from *piped* runs; ask for unpiped runs explicitly, and this instance self-reported that it had piped, then re-ran correctly — asking for deviations explicitly is what surfaced that.


## 2026-11-04 — ## Live tooling friction — gen57, S7c session (reported, not...

## Live tooling friction — gen57, S7c session (reported, not worked around silently)

These cost real tokens/time this session and are worth fixing in the harness.

1. **`Worker(action="query")` demands `worker_name` explicitly**, while `spawn` defaults it. The first query of a session therefore fails with `worker_name is required for action 'query'` and burns a turn; discover the name via `CheckSystem running_workers` first, or default it like `spawn` does.

2. **A hard-timeout worker is still alive and queryable, but nothing says so.** A 480s ladder task returned `timeout_triggered: true` with zero content; the file work survived on disk. Recovery path that worked: `CheckSystem running_workers` → the instance read `status: ready, current_task: null` → `action="query"` the same instance for a status report. Do NOT re-spawn (loses context, risks duplicate file edits). Suggested: have the timeout envelope state whether the instance is still queryable, and say that partial file work survives.

3. **`git_read diff_cached` has no line-range or context filter.** Verifying a 3-line delta inside a large staged file costs a full re-read (~9k tokens each time). `show_file` rejects `:` in `ref`, so `` :0:file `` index syntax is unavailable — there is currently no cheap way to see "just the hunks that changed since I last looked". A `since=`/`paths=`-style filter, or accepting a revision for `show_file`, would pay for itself immediately.

4. **Workers report host absolute paths.** An instance answered with `/home/jojo/PycharmProjects/ThoughtMachine-dev/.thoughtmachine/...`, which the agent cannot use (agent tools are workspace-relative and cannot leave the workspace). Always ask workers for workspace-relative paths.

5. **KB domain naming is not the file path.** `personal/bugs_and_fixes.md` is addressed as domain `bugs_and_fixes`; `domain="personal/bugs_and_fixes"` errors out. And the agent-side KB surface has no prepend/rename/"insert at top" mode and cannot write arbitrary paths, so **KB file surgery (headers, splits) must be delegated to a worker** — the agent can only append/update a section.

6. **Splitting a KB domain needs a "does this break the tool" check first.** `bugs_and_fixes` had already been split once (entries moved to `archive_bugs_a.md` / `archive_bugs_b.md`), and the domain filename must stay put for the KB tool to resolve it. A warning header at the top of the oversized file was the right-sized fix; a second split would have fragmented the history across four files for no resolution gain.


## 2026-11-04 — ## Pause looks exactly like "stale" to the watchdog (S7c int...

## Pause looks exactly like "stale" to the watchdog (S7c interaction, noted for gen58)

Live finding at the end of S7: the operator's pause test explains a mystery from this session — a worker task "hard-timed-out" at 480s with no report, and the worker's own reasoning showed *"only Respond available"*. It was not hung: it had been **paused**, which restricts its tool set to `Respond` and stops the work loop.

Why this matters for S7c: a paused worker emits no turn-loop activity, so after `WORKER_STALE_ANNOUNCE_AFTER_S` (90s) it will be announced as stale. What the ladder then does is correct but only by two independent accidents, both worth knowing:

1. `paused` is a real shipped status and is NOT in `_WORKING_STATUSES` (`{"busy"}`), so `_turn_stopped()` returns True → a cancel is judged coopoperated → **the ladder stops without killing**. Safe direction.
2. The ping is answered from the child's frame pump, not the work loop, so a paused-but-alive worker answers and the episode aborts at rung 1 regardless.

Open question for W10/gen58: is a paused worker *supposed* to be announced as stale at all? Announce-only noise every 90s on an operator-paused worker is a UX wart. Consider excluding `paused` from the announce predicate rather than relying on the ladder never reaching a decision.


## 2026-11-04 — ## LIVE tooling problems hit while working (Gen 58 onboardin...

## LIVE tooling problems hit while working (Gen 58 onboarding session) — for the engineers

Recording these because each one cost real time or nearly produced a wrong answer. None are fatal; two are actively dangerous because they fail *silently and plausibly*.

**1. `git_read` `log` silently ignores the `commit`/ref argument — DANGEROUS.**
I called `operation="log", commit="origin/feat/c2-worker-blueprint-config", max_count=10` and got back the log of `w8/p3s7c` (d641131a, 67220390, 01ff586e, fec9c099…). `rev_parse` on the same ref correctly returned `4cb24ddf`. So `log` did not resolve the ref I gave it and returned *some other branch's* history with no error and no warning. I only caught it because two different refs returned byte-identical logs, which is impossible. Had I trusted it I would have reported "the blueprint branch contains S7c-1/2/3" — completely wrong, and it would have derailed a W9 planning decision. **Fix needed: `log` must fail loudly on an unresolvable/unrecognised ref instead of falling back.**

**2. `for_each_ref` with `pattern` returns the ENTIRE ref list.**
Passed `pattern="c2-worker-blueprint"` expecting one line; got ~250 refs (all heads, remotes, tags, even `refs/stash`). The pattern appears to be unmatched-or-ignored, so it degrades to a full dump. Costs a large slice of context for one line of information. Same family of bug as #1: the filter argument is accepted and then not applied.

**3. Workers cannot determine the branch they are on.**
A worker reported its tree as "branch w8/p3s5" when HEAD was actually `w8/p3s7c`. It ran no git (no git binary, and `.git` is a **0-byte file** in the container, not a directory), so this was an inference from some other signal (config/env) and it was wrong. Consequence: never ask a worker "what branch is this?" and never let a worker's branch claim gate a decision. Branch facts must come from the agent's `git_read` tools. The *content* findings were still accurate — the line numbers matched the real w8/p3s7c tree — so a wrong branch label is a label problem, not a fabricated-reading problem.

**4. Frontend tests fail in this container without a TMPDIR redirect.**
`npx vitest run` dies with `ENOENT: mkdir '/var/tmp/<hash>/ssr'` — `/var/tmp` is not writable in the sandbox. Workaround that worked: `TMPDIR=/workspace/.tmptest npx vitest run …`. Worth seeding into the container or the worker prompt, because the naive run looks like a test/env failure rather than a path-permission one, and a worker that trusts it will wrongly report "tests are broken".

**5. The agent has no shell, no `gh`, and no network.**
Asked to run `gh run list --branch … --json status,conclusion`, I simply cannot: there is no generic command execution in my tool surface (no bash/grep/find), only structured `git_read`/`git_write` plus a worker that executes inside a container. Every "just run this and paste the output" request to an agent is really "reimplement it in structured tools + N worker round-trips". Reading CI status, counting lines, grepping symbols: all of it had to be rebuilt piecemeal, and each worker dispatch costs 100–300 seconds. **A read-only shell tool with an allowlist would remove most of this overhead.**

**6. `git diff --stat` is not exposed.**
`git_read diff` returns full patches only; there is no way to get change *sizes* without either a huge diff dump or inferring from `show`. Requesting "diff --stat dev...HEAD" (a completely reasonable ask) is not actually satisfiable through the tool surface.

**7. Worker says it ran `git status`, but there is no git binary.**
The frontend worker’s report included “`git status --porcelain` shows exactly the two intended files”. There is no git in the container. It may be re-deriving status from filesystem/tool state and describing it as git, or hallucinating the command. I verified independently with `git_read status` (correct — exactly 2 modified files). Lesson: a worker's *method* claims are not evidence; verify the *result* with an independent tool. This is cheap to do and I would not trust a commit decision without it.

**8. (minor) Two worker definitions exist (`default`, `default2`) — real parallelism works.**
Spawning both in one tool block ran them genuinely concurrently (132s and 235s, overlapping), which is much better than sequential. Worth knowing, since `spawn` blocks and can look serial-only. Only `default` showed `spawned: false` vs `default2` `spawned: true` in the envelope for reasons I could not determine, yet both executed fine.

## LIVE tooling problems

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

## Live tooling problems (agent-reported)

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

## 2026-11-06 — ## Reviewing a worker's "tests passed" claim (log-spam fix, ...

## Reviewing a worker's "tests passed" claim (log-spam fix, commit aeaf1de6)
Three cheap gates that caught real risk and cost ~3 min total:
1. **Vacuous-test check on monkeypatched module globals.** A test that patches `agent.events.global_event_bus` proves nothing if the code under test did `from agent.events import global_event_bus` at module import (bound name). Ask the worker for the *import lines with line numbers*, not a yes/no. Here `_emit_event` imports inside the function body (controller/__init__.py:597), so the patch is live — but this was unknowable from the diff alone, because the diff only showed the lines *below* the import.
2. **`except Exception: pass` around the patched call** means any "it didn't raise" assertion is guaranteed green. Insist tests assert on the fake's *recorded calls*.
3. **Reader sweep before a persistence-format change.** Changing what gets written is only safe if you know who reads it. Worker enumerated both sinks' readers: `event_log.jsonl` → one generic tailer (`tools/workspace/check_system.py:1088`), `session.log` → `agent/cli/logs.py`; both duck-typed on `event_type`/`event`/`level`/`session_id`. Also learned `thoughtmachine/container_record/api.py:read_event_log` is a *different* store despite the identical name — don't confuse them.
Plus: the targeted `pytest tests/ -k "event or lifecycle or stream or controller or session_log"` covering 582 tests in 40 s was a much better gate than the 15-test file alone, and far cheaper than a full suite.

## 2026-11-06 — ## A log line that contradicts the return value is the cheap...

## A log line that contradicts the return value is the cheapest bug signal there is (gen19, c0142021 follow-up)

CI pasted three red tests in `tests/test_worker_workspace_resolution.py`. Every
one of them carried the SAME `Captured log call` block: the cross-wiring
WARNING *did* fire saying "Using the path-derived id 'fierce-owl-43'" — while
the assertion showed the method returned/dispatched `'gentle-wolf-72'`.
Warning fires, wrong value comes back ⇒ the divergence is *inside* the
function, between the log statement and the return, not in the caller wiring.
That single observation collapsed a 3-failure investigation to one 28-line
block, and the actual defect was `return registry_id or path_id` in the
fall-through right after the warning (commit c0142021 inverted the precedence
its own docstring promises).

Three transferable rules:
1. **Read the captured log, not just the assertion diff.** A traceback tells
   you the wrong value; the captured-log block tells you how far the code got.
   "Log says the fix happened, value says it didn't" localises the bug to the
   statements between them.
2. **Check the fall-through of every probe/decision rewrite.** A "prefer X
   when the probe is informative" patch is only as good as its inconclusive
   branch. Here the new sidecar probe was correct in its two decisive branches
   and inverted in the third — and the third is the only branch tests and
   unprovisioned sessions ever take, so the patch was a no-op-to-regression
   everywhere except production-with-a-provisioned-sidecar.
3. **Re-verify the hypothesis against the ACTUAL checkout before theorising.**
   I initially reasoned the red run must be from a branch lacking the fix
   (unpushed local commit, my branch predates it). One `rev_parse --abbrev-ref`
   + one test run showed the repo had been switched to `fix/worker-sidecar-final`
   whose HEAD *was* the suspect commit, red on its own. Cheap check, saved a
   wrong report. `git_read rev_parse(abbrev=True)` prints the current branch and
   costs ~15 tokens.


## 2026-11-06 — ## 2026-11-06 — Gen 60 handoff: the tooling that cost us tim...

## 2026-11-06 — Gen 60 handoff: the tooling that cost us time, and the process failure behind it

### Tooling failures (all observed by me, live, this generation; none fixed)
1. **`git_read show_file` ignores `line_start`/`line_end`.** Ask for lines 6000-6100 of `server.py`, get ~10k tokens starting at line 1. For an agent whose whole job is reading big files under a token cap, this is the single most damaging defect — it makes surgical reading impossible, so agents either burn context or read nothing. **Fix first.** `git_read log` also ignores `format`, and `git_read status` returns an empty body on a clean tree (indistinguishable from a tool failure — a `clean` line would cost nothing).
2. **No `git rebase` / `cherry-pick` verb in `git_write`.** Verbs are branch_create, checkout, stage, commit, init, clone. The consequence is not cosmetic: a branch that needs replaying onto a moved base **cannot be finished by the agent at all**, so the work has to be abandoned or handed to a human. Anything that forces the agent to say "you must do this in a terminal" is a capability hole, not a nuisance. (`fix/log-spam` was left stranded this way: `aeaf1de6` still needs a rebase onto `dev` plus an author amend to `Test User <test@example.com>`.)
3. **Worker instance reuse starves the follow-up task.** `Worker(action="query")` carries the old conversation by design — but so does `spawn` with `force=true`, which *rehydrates the previous transcript from disk*. Observed twice in one session: the worker came up at **92k and 99,298 tokens** with every tool refused (`not allowed. Reason: token`), ran **zero** commands, and answered a fresh probe from stale memory instead of failing loudly. It looked healthy in the envelope because `telemetry.token_usage` is sampled **after** the worker's own prune (16,660) and hides the saturated start. Workarounds: spawn a *different* worker name (a cold `default2` did the whole job at 39k), or instruct the worker explicitly to call `SummarizeTool` first. Real fix: spawn must auto-compact (or refuse) when the restored context is already above `critical_threshold_tokens`.
4. **Smaller ones:** `Worker(spawn)` docstring claims `worker_name` is optional — it is required. Passing tool args as a JSON string yields a pydantic `action field required` error instead of "invalid JSON". `KnowledgeBaseTool` domain names are case-sensitive on read (`KB_MAP` in `list`, `Unknown domain 'kb_map'` on `read`) yet the error text lowercases the offender, so the message hides the bug. `KB search` returns whole matching *sections*, not lines — one query for `287` dumped ~15k tokens of unrelated history; search is not a cheap primitive in practice. KB `bugs_and_fixes` is ~68.6k tokens: append is cheap, read is not.
5. **Worker-visible vs agent-visible tool namespaces differ.** Inside the worker container there is no `git` binary and no `docker` CLI/socket, and `/workspace/.git` is a 0-byte *file* with a read-only ext4 bind mounted over it — while `git_read`/`git_write` work fine (they run in their own container). Meanwhile `FileEditor` **rejects** `/workspace/...` absolute paths ("outside workspace /home/jojo/PycharmProjects/ThoughtMachine-dev") and speaks host paths, even though host and `/workspace` are the same bind. Any agent that doesn't know this concludes the repo is corrupt. Document it in the worker system prompt.

### Process failure (the important one)
**The operator should never perform manual git surgery. The Infra engineer owns the terminal.** This generation the operator did merges/rebases by hand, and the reason is structural: defect (2) above means the agent *physically cannot* complete a history-rewriting task, and defects (1)/(3) mean an agent low on context will report plausible-sounding progress while doing nothing. Manual git on the operator's side is the tell that the agent's tool surface has a hole in it.

Corollary rule for every future Main: **when the human reaches for the terminal, stop and log a tool defect.** The human's hands on the keyboard are a bug report, not a workaround. And the reciprocal: never ask the operator to run a command whose result the agent could have obtained itself.


## 2026-11-06 — ## 2026-11-06 (Gen 60) — LIVE tooling failures observed whil...

## 2026-11-06 (Gen 60) — LIVE tooling failures observed while running the 4-question read-only audit

Reported for the Infra engineer. Each item cost me real capacity this session.

1. **Worker-side `SummarizeTool` inside a `query` breaks the turn.** I asked `default2` to summarise as its first action, then continue with Question 2. It summarised (usage dropped 68.7k -> 35k) and then the query returned `{"content": "Unknown error", "status": "error"}` after 14.6 s. Re-issuing the same query without the summarise instruction worked fine (110 s, 83 tool calls). Inference (Unverified): summarising mid-`query` invalidates something the dispatch envelope expects.
2. **A "fresh" spawn is not fresh — it rehydrates the old transcript.** `spawn(default, force=false)` for Question 3b came up with `token_usage: 85970` after only **4** tool calls (its own ceiling is 85k). There is no chance of a clean vessel once a worker name has run before; the only way to free it is a forced respawn, which is exactly the path that restores `context.json` (see Q3b in the audit).
3. **`git_read show` has no stat mode.** `operation=show` on a 1-commit branch dumped the entire patch (~8k tokens) when I only wanted the files touched. A `--stat`/`--name-only` variant is needed for the "what was in flight" class of question.
4. **`git_read branch_list(all_branches=true)` costs ~2.5k tokens** and prints ~380 branch names with `remotes/origin/` duplicated for every one of them. Nearly every name exists twice; the local list would have answered the question.
5. **`KnowledgeBaseTool mode=list` costs ~1.4k tokens** and prints 90 rows; `mode=read domain=project/status` fails with `Unknown domain` because the domain key is the bare `status` (the table's "Domain" column and the "File" column disagree — the agent reads the Domain column and gets an error). Same class of defect as the `KB_MAP`/`kb_map` lowercasing bug already logged.
6. **`mode=status` is not a cheap primitive** — it emits a 5-row activity table plus storage totals (~700 tokens) but does NOT contain the current-state section the operator means by "read status.md".

Pattern confirmed across the session: **the agent's read surface has no bounded mode for any of its three read tools** (git show/diff, KB read/search, doc read). Every audit question therefore has to be delegated to a worker with `grep -n` + `sed -n` — a workaround that only exists because workers have shell access.

## 2026-11-06 — ## Gen 60 — root cause of the "hallucinated commits / worktr...

## Gen 60 — root cause of the "hallucinated commits / worktrees" incident
Operator believed the agent invented `a4b2c19d`/`e8f1b39a` and fabricated worktree state. Verified with `git_read show a4b2c19d` -> `fatal: ambiguous argument`: those commits genuinely never existed. BUT the fabrication had a real substrate on disk: `/workspace/.worktrees/` contains **11 pre-existing git worktrees** (handoff-gen57, kb-braindump, w2-landing, w2-panel, w2-rename, w3-audit, w3-fix, w3-resid, w4-auth, w7-isolation, w8-p3s0, all Oct 31 - Nov 3), each a complete checkout holding its own `web_ui/backend/server.py`. A worker running `grep -rn` over `/workspace` therefore sees 11 stale copies of whatever file it is asked to edit, and can report an "edit" or a "commit" located in a path that exists but is the wrong one. **LESSON: before trusting any worker edit, `git_read diff` on the MAIN tree for the named file must show the hunk. A path that exists is not a path that is right.** Recommend `git worktree prune` + removing `.worktrees/` in anything but an intentional multi-worktree session; `git_write worktree_remove` exists for this. Also confirmed: with 11 worktrees present, "parallel branches via worktrees" is actively dangerous here, because the agent loses track of which checkout it is in — serial execution in the main tree is the safe mode.

## Gen 60 — `git_write push` is unusable in a `network:banned` session
`git_write push` -> `Permission denied: Tool requires network:outbound, but session allows network:banned`. There is no alternative egress (no `gh`, no patch handoff verb). Consequence: commits made by the agent are stranded on local branches until a human or Infra pushes. **Design gap:** a `git_write` verb that can only ever fail should either be hidden from the tool surface in banned-network sessions, or the refusal should name the actual remedy ("ask the operator to push") instead of only citing the permission grain. It cost several turns and an operator correction to establish that the agent could not deliver its own work.


## 2026-11-06 — ## Gen 60 — FALSE BELIEF: "tests cannot run in this sandbox"...

## Gen 60 — FALSE BELIEF: "tests cannot run in this sandbox" (it cost a pushed broken PR)
Earlier in this session the agent concluded the project's tests were unrunnable here (`No module named pytest`, `No module named pydantic`, "sandbox py3.13 vs venv 3.12") and therefore committed `tests/test_worker_force_spawn_clean_slate.py` **without ever executing it**. That test was broken (see below) and shipped into PR #298. The belief was wrong: `python3` = /usr/local/bin/python3, **Python 3.12.15, pytest 9.2.0**, rootdir /workspace with configfile pyproject.toml — pytest and pydantic both import fine, and `DockerCodeRunner` has /workspace mounted so it can run the suite directly. The earlier failure came from probing the wrong interpreter (a bare sandbox python) and generalising from one error. **RULE: never conclude "cannot run tests" from a single import error. Probe `which python3`, `python3 -V`, `python3 -c "import pytest, pydantic"`, and the venv paths; then run the suite from the project root.** A test that has never been executed is not evidence — it is a liability, and it burns the operator's trust in the whole PR.

## Gen 60 — test-fixture schema drift: `context.json` stores history under `conversation`, not `user_history`
`WorkerContext.to_persistable_dict` (agent/core/worker_context.py:205-228) writes the message list as key **`conversation`**; `from_persistable_dict` reads `data.get("conversation", [])`. A hand-written fixture planting a top-level `user_history` key is therefore *silently ignored*: `_load_context()` still returns a non-None context, just with an empty history. Symptom is deceptive — the "continuity" assertion fails with `the restored conversation lost its history: []` even though production save/load is symmetric and correct. **RULE: never hand-write a persisted-state file in a fixture. Drive the real serialiser (`_save_context()` on a real object over a temp dir) and assert the file was actually written.** Then the fixture cannot drift from the loader's schema. Three sibling tests still plant the stale `user_history` shape (test_worker_agent_transplant.py:1640/1765, test_worker_cap.py:113, test_worker_sync_query_timeout_containment.py:59 `_FakeContext`) and pass *vacuously* because they only assert file existence; `test_worker_operation_semantics.py:1468` asserts `persisted["user_history"]` and passes only because that test monkeypatches a fake context that emits that key itself. All are green today, none of them test the real schema.

## Gen 60 — verification pattern that actually caught things
Sequential pattern worth reusing: (1) worker makes the change and quotes code; (2) **main agent reads `git_read diff` on the named file in the MAIN tree** — a path that exists is not a path that is right, especially with 11 stale worktrees present; (3) a **different** worker, reset fresh, independently runs the tests and pastes raw output; (4) main agent commits only after (2) and (3). The independent-run step caught two false claims this session: a "likely broken test at line 1468" that was actually 60 passed, and forced honesty about which interpreter can run pytest. Also: `-p no:warnings` is mandatory here — the pydantic deprecation wall eats the `tail` window and hides the test names.


## 2026-11-14 — ## Gen64 doc-truth wave — live problems hit while working (s...

## Gen64 doc-truth wave — live problems hit while working (supervisor side)

1. **`Worker(action="query")` cannot continue a spawned worker.** After a blocking `spawn` returns, the instance is gone: follow-up `query` → `{"error": "Worker 'default' is not running"}`. Consequence: **every follow-up is a fresh agent with zero memory**, so a 3-line wording fix costs a full context (~80k cumulative token_usage here vs 26.9 s wall time). Multi-turn worker iteration is effectively unavailable; budget briefs as one-shot and accept re-briefing cost, or fold fixes into the original brief.
2. **A spawn that returns `status: null, confidence: null, meta: null`** (seen twice this session, on successful runs) — anything branching on `status == "final"` will mis-handle a good result. `telemetry` is populated, so the envelope is only half-filled.
3. **`token_usage` appears to be per-worker-instance cumulative, not per-spawn.** 33,952 → 39,729 → 71,369 → 80,206 across four spawns all reporting `instance_id: 1`. A 26.9 s one-edit spawn "cost" 80,206 by that field. Do not quote it as a per-task cost.
4. **`git_write worktree_add` ignores the `branch` parameter** and creates a **detached** worktree at HEAD ("Created worktree at '.worktrees/x' (base: HEAD)"), silently. The named branch is never created. Working recipe: `branch_create(base=...)` → `worktree_add(path=...)` → `checkout(branch=..., working_dir=<worktree>)`; verify with `git_read status(working_dir=...)` before editing.
5. **`git_write worktree_add` rejects absolute paths** — `Path /workspace/.worktrees/x is outside workspace /home/jojo/...`. Must pass workspace-relative (`.worktrees/x`). Related: if the `base` ref is already checked out in the main worktree, `worktree_add` fails with `'dev' is already used by worktree` — omit `base` and rely on HEAD.
6. **`CheckSystem query="vault_status"` returns ~150 individual file entries**, a large context hit for what should be a summary; it also reported `status: warnings` where every listed file was `ok` (only 3 drift warnings). Needs a detail=false mode.
7. **`CheckSystem query="running_workers"` returns `[]` immediately after a successful blocking spawn** — live-only listing plus synchronous dispatch means the panel/query always looks dead after a worker just worked. Not a defect in the worker, a misleading surface.
8. **Docs/pointer drift, found by worker verification, worth folding into the operator's file inventory:** `INFRA_ONBOARDING.md` and `status.md` live at `.thoughtmachine/knowledge/project/`, NOT repo root; that same file claims (line 58) "the KB is gitignored" but `git status` shows `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md` as a **tracked modified file** — so the KB is committable and that claim is false at `cd06c87c`.

### Agent-worker interaction pattern that keeps paying
Demanding a `MEASURED REALITY vs OPERATOR CLAIMS` table with CONFIRMED / REFUTED / INCONCLUSIVE per claim. This pass it **refuted two operator claims** (a "byte-identical duplicate" doc that does not exist at the stated path, and the "Under 100 KB total" claim — real sum 124,954 B) and caught a briefed path that did not exist, without ever writing a false sentence. Also: instructing "if your budget runs short, report measurements and partial edits" + ordering measurement → edits → pytest meant the 240 s pass returned complete facts and finished edits with 70 s to spare.

## 2026-11-14 — ## Gen65 — live problems, ranked by how much they cost

### ...

## Gen65 — live problems, ranked by how much they cost

### 1. A spawn that "timed out with no reply" keeps running and keeps WRITING FILES (worst one yet)
`Worker(action="spawn")` with `timeout_seconds: 300` returned a `WorkerQueryTimeout` after 361 s wall, `runtime_mode: "process"`. The envelope claimed the worker "is registered and can be retried via action='query'"; the immediate `action="query"` answered `Worker 'default' is not running`. Both statements are wrong in the same direction: the *forked child process* was still alive and still working.
Proof, and how I found it: after I committed `tools/workspace/worker_thread.py` (clean tree, `git_read status` empty), the next `git_read status` showed it **modified again**. The delta was a fifth call site the original brief never listed — i.e. the ghost finished its own plan on top of my commit. `Worker(action="list")` → single `default` entry, `runtime_status: ready`, and `CheckSystem running_workers` → `alive: false, conversation_length: 0`. So every registry view says "not running" while a process is actively mutating the workspace.
Consequences worth designing around: (a) any verification run in a worktree a timed-out spawn ever touched is racy; (b) a worker's own edits can silently land *after* the supervisor concludes the task failed and commits something else; (c) when a worker reported "another actor rewrote my file between read and write" it was right, and it is not a test artefact.
Mitigation that actually worked: fingerprint `sha256sum` before AND after each run and require them to match (a worker did this unprompted and it is the only reason I trust the final green); `find . -newer <edited file> -name '*.py'` to enumerate what the ghost touched; and treat any `git_read status` that reappears non-empty after a clean commit as ghost evidence, never as "the commit didn't take".
There is no agent-side kill: terminating is operator-only. `action="stop"` detaches a *registered* worker, and this one is not registered. So the supervisor's only recourse is detection plus serialisation by hand.

### 2. pytest dies with SIGTERM/exit 143 in the sandbox, intermittently and unrelated to the test
`tests/test_worker_process_contract.py`: 6 consecutive attempts, all `Terminated` (143), once after 9 collected tests; the same file passed `10 passed, 1 xfailed` earlier in the session on the same tree. The target file itself and `test_worker_is_a_real_process.py` were also SIGTERM'd once each and passed on retry. Kills land ~3-8 s in, well inside a 600 s budget. Raising mem_limit to 2 g/4 g did not help. A worker's hypothesis is worth following up: this looks like a **process-group kill from the production worker teardown path**, i.e. the ghost from problem 1 tearing down siblings. Reported counts are therefore only trustworthy when a run names the file it measured.
Also in this bucket: one combined `cat > file && sha256sum && pytest` via DockerCodeRunner was **SIGKILLed (137) after the write had already landed** (file present, 6824 B). Split writes from test runs, always.

### 3. Worker file-editing tools cannot reach a git worktree
`ApplyEdits`/`FileEditor` refused `/workspace/.worktrees/c73-declared-mode/...` as "outside workspace": they resolve against the **host** path (`/home/jojo/PycharmProjects/ThoughtMachine-dev`) while the worktree lives on the **Docker** side of `/workspace`. Every edit had to be smuggled through `DockerCodeRunner` with a python/cat heredoc. This makes the standard "make a worktree, have a worker edit it in place" workflow impossible with the ordinary tools, and pushes workers onto heredocs where quoting mistakes are silent. A worker's workaround that I want to keep: guard the rewrite with a hash precondition and **abort instead of writing** when it fails (`AssertionError: 0`) — that is what saved the file from being clobbered by the version it did not intend.

### 4. `git_read ls_tree` ignores `prefix` and returns the whole tree
`ls_tree(treeish=<sha>, prefix="tests/", recursive=true)` dumped the entire repository (`original was 33330 tokens`, truncated at 10 k) with no error and no hint that the filter was dropped. It burned a large slice of context for zero information. If a prefix cannot be honoured, the tool should refuse rather than silently widen. Use `show_file(path=..., ref=..., source="committed")` to probe a single path — a clean `fatal: path ... does not exist in <ref>` is the cheapest existence check available here.

### 5. Minor API annoyances, each one a wasted turn
- `show_file` requires **`path`**, while every other operation takes **`file_path`**; passing the consistent one yields `Error: path is required for show_file operation`.
- `git_write` has no `merge` and no branch-delete, and workers have no `git` binary at all (`command -v git` → 127). So "merge origin/dev, then push a PR" is unreachable from the agent side in every combination and always lands on the operator.
- `Worktree_add` ignores its `branch` argument (always detached at `base`), rejects absolute paths, and refuses a `base` that is checked out elsewhere — but **accepts a commit SHA even when the branch pointing at it is checked out**, which is the workaround: `worktree_add(base=<sha>, path=".worktrees/x")`.
- Piping hides exit codes: `/bin/sh` here rejects `${PIPESTATUS[@]}` ("Bad substitution"), so any `cmd | head` loses the status and a worker will confidently report a meaningless `EXIT=0`.

## 2026-11-14 — ## Gen65 — the HANDOFF.md sha256 trap (why PR #380 went red,...

## Gen65 — the HANDOFF.md sha256 trap (why PR #380 went red, and how to regenerate it safely)

**Symptom looked unrelated to the branch's content:** two workflows failed on
`tests/test_onboarding_bundle_contract.py::test_every_recorded_hash_matches_the_file_on_disk` and
`::test_checked_in_handoff_matches_fresh_generation`, above two `WARNING: <file> changed since HANDOFF.md
recorded sha256:...` lines. Cause: `.thoughtmachine/knowledge/project/HANDOFF.md` is *generated* by
`scripts/gen_handoff.py` and carries an inline `- sha256:<64 hex>` pin for every entry in its
`## 2. Read in this order` manifest. Editing a pinned doc (here `docs/ARCHITECTURE_MACHINE_MODEL.md` and
`INFRA_ONBOARDING.md`) invalidates the router. Bug class already named in `working_agreements.md` as
`sha-pinned-blocks-go-stale-on-correction`.

**The invocation matters more than the script.** `gen_handoff.py` also records live git facts (branch head,
subject, recent commits). If you run it bare in a worktree, it bakes *that worktree's* HEAD into the file and
CI's fresh generation will disagree. The contract test passes overrides (`--root`, `--dev-sha`,
`--dev-subject`, `--recent-file`, `--stdout`) whose values live as `PINNED_*` constants in the test module.
Extract them with `ast.literal_eval` off the test source rather than retyping, then run the generator with
exactly those. That is what makes the artifact CI-stable.

**Verify by prediction, not by eyeballing the diff.** Recompute all manifest digests independently (parse the
manifest, `hashlib.sha256` each named file) and print the mismatch set *before* regenerating; then require the
emitted diff to change exactly that set. Here: 10 entries, 1 advisory (`PRIORITIES.md`, deliberately carries
no digest because it is edited by every branch — recording it would red any branch merely up to date with dev),
predicted 2 mismatches, got exactly 2.

**Expect one tolerated extra hunk.** The `## First task (from the live queue)` body is regenerated from
`PRIORITIES.md`; on this branch it added `+C68`/`+C69`. The contract collapses that block on both sides
(`_without_queue_block`), so it does not affect the check. **Leave it as generated — do not splice the old
queue back.** Hand-editing the router to keep a diff pretty is the exact drift the pin exists to catch.
`bash scripts/check_kb_staleness.sh` is the shell-side twin and prints `PASS: HANDOFF.md is up to date.`

**Read-order manifest has no `worker_thread.py`** (all entries are `docs/` and `.thoughtmachine/knowledge/project/`),
so a change to `tools/workspace/worker_thread.py` never requires regenerating this router.

### New tool finding: DockerCodeRunner does not share `/tmp` between calls
A temp file written in one call is `FileNotFoundError` in the next. Anything that stages input for a later
command (recent-commit lists, fixtures, heredoc payloads) must be created and consumed inside a single
invocation. Cost a retry cycle here.

## 2026-11-15 — ## Gen65 — a new `docs/` file is INVISIBLE to git (cost: wou...

## Gen65 — a new `docs/` file is INVISIBLE to git (cost: would have shipped an empty contract gate)

`.gitignore` here has `docs/*` as a **catch-all**, then ~79 `!docs/<name>` negations, one per published
doc. The convention is stated in-file ("every docs/ file needs its own line") and has been followed 78
times. Nobody added a line for the new `docs/CONTRACTS.md`, so the file existed on disk, the tests that
read it passed, and `git status` did not list it. Committing what git offered would have shipped the
workflow and the parity test **without the ledger they read** — the gate would then have failed on
`LEDGER.is_file()`, or, had I ordered the commit differently, a workflow citing a document absent from
the repo.

**The process lesson is the transferable part:** a worker reporting "file written, verified, sha256 X"
proves the bytes are on disk and proves nothing about whether git will ship them. `git status` after
every write phase is not ceremony — it is the only check that distinguishes "created" from "versioned".
Here it showed 2 untracked files where the worker had produced 3, which is what caught it.

Generalisable traps in the same file worth knowing before adding anything new: `AI_tasks/`/`reports/`,
`.thoughtmachine/working_docs/*` (re-ignored after being un-ignored, so a deliverable there needs a
second negation AFTER the re-ignore), `.thoughtmachine/knowledge/project/**/*.md` (same shape), and
`*secret*`/`test_*.json`/`fix_*.py` which silently swallow ordinary-looking real files. Order matters
throughout: a negation must follow the ignore it overrides, and git cannot re-include a file beneath an
excluded *directory* — hence the recurring `!docs/ci/` + `!docs/ci/**` pairs.

## Verification-first briefs: three confident anchors, two wrong

A design brief specified six `file:symbol` anchors for a claims ledger, with line numbers. A read-only
pass before writing the document found: `tests/test_identity_injection.py` **does not exist**; the named
enforcement symbol `_denial_layer_label` is denial-*message formatting* (`security_gate.py:610-622`,
called from `_denial_clause:649`) and can neither allow nor deny — the real deciders are
`check_required_categories:1600`, `check_atomic_operation:1492`, `check_requires_resource:1530`; and a
supposedly mutation-proof test compares against `DEFAULT_RUNTIME_MODE` rather than `MODE_PROCESS`,
so flipping the constant keeps it green.

Cheapest reliable pattern found: a **separate read-only pass** whose only output is a report
(`claim | verdict | real anchor | mutation-proof? | runs-without-docker?`), dispatched *before* the
writing pass, with an explicit instruction to name missing things rather than substitute quietly. It
converts a document that asserts into a document that has been checked, and it surfaced a genuine
fail-open (`apply_workspace_ceiling`, `security_gate.py:234`) that the brief never mentioned.

Related: the same pass confirmed the whole fast-gate set is hermetic (no docker, vault, network, `/proc`
or git) — `tests/test_security_gate_disk.py` patches `vault_root()` via a `hermetic_vault` fixture, and
`test_worker_is_a_real_process.py` documents "no LLM, vault, docker or network". 69 tests, 4.29 s.

## 2026-11-15 — ## C77b — why the worker output panel never streamed (root c...

## C77b — why the worker output panel never streamed (root cause + live tooling friction)

**Finding (backend, not frontend).** `web_ui/backend/bridge.py` `_on_worker_spawned` computed
`session_id = data.get('session_id', self._session_id or '')` (line ~1043) but the gate one line later
called `_event_belongs_to_this_session(data)`, which re-reads the **raw** event and is deny-by-default.
The denial `return` sat *before* the per-worker EventBus subscription and *before* the reconnect buffer,
so a spawn rejected on an unbound/mismatched session was lost permanently: no subscription, no replay,
no `tool_call` / `tool_result` / `assistant_message` frames. Both reported symptoms had that one cause —
the worker list is REST-polled (so it looked fine) and `WorkerOutputPanel.jsx` defaults its dot to
`'ready'` grey because only WS events populate `workerInfo`.

Lesson: a guard that `return`s before a subscription/buffer is a *silent data-loss* pattern, not a
validation. Any deny-by-default early return on an event path must be (a) logged at a level the
deployment actually emits, and (b) ordered after any side effect whose absence is invisible to the UI.

**Diagnosability lesson.** `agent.logging.log` writes to stderr and emits no stdlib records; with
`TM_LOG_TAGS` unset the console admits WARNING+ only. The old drop was DEBUG-only, so
`logs/backend_startup.log` (3,562 lines) contained **zero** DEBUG lines and the defect was
unprovable after the fact. Fixed in `63abd246` by logging the mismatches at WARNING. Also: whether the
original repro hit "bridge unbound" or "session mismatch" is *still unproven* — say so, don't guess.

**Tooling friction met while doing it (for the engineer):**
1. `git_write worktree_add` ignores its `branch` argument — always detached at HEAD; a later `commit`
   then fails with "workspace HEAD is detached". Working order: `branch_create` → `worktree_add` →
   `checkout branch=<name>` (staged changes survive).
2. `working_dir` must be host-absolute; `/workspace/...` is rejected and the error is then re-worded as
   `Not a git repository: <host path>`, naming a path the caller never typed.
3. `git worktree repair` cannot fix worktrees whose gitdir points into a pre-rename clone; those must be
   recreated. Recreate + `cp -p` + sha256 verify works.
4. `Worker` timeout ceiling is a hard 600 s; 900 is refused outright rather than clamped.
5. `DockerCodeRunner` gets a fresh container per invocation → `/tmp` baselines do not survive between
   calls; keep baselines as files inside a sibling worktree.
6. `FileEditor` rejects absolute container paths; needs workspace-root-relative paths.
7. `.gitignore`'s `node_modules/` (trailing slash) does not match a symlinked `node_modules`, so it
   shows up as untracked — never stage it.
8. Live-log bug worth a ticket: `Worker worktree default skipped: git worktree add raised (unknown
   permission grain 'git'; this port only accepts ['host_bash','mcp','network'])` — workers cannot
   create their own worktrees in this instance.


## 2026-11-15 — ### Live tooling / agent-worker problems observed while hard...

### Live tooling / agent-worker problems observed while hardening `apply_workspace_ceiling` (worktree `harden-permissions`)

Recorded per session instruction so the platform can fix these. Nothing here is a code bug in the product; all of it made the *work* slower or made verification untrustworthy.

1. **`host_bash` is banned for me** — I cannot run a single shell command. Any `bash` heredoc an operator hands me is undoable, and every file must be created through a worker + FileEditor. Plan for this: read a proposed script, re-express it as worker instructions.
2. **Worktree test runs silently test the WRONG code (worst issue found).** Nine `tests/` modules hard-code `sys.path.insert(1, "/workspace")`. Inside a worktree that literal is the **parent** checkout, and the parent's `security/`/`tools/`/`agent/` get imported and cached in `sys.modules` before the worktree root reaches `sys.path[0]`. Measured: 6 `tests/security/*` modules **PASS while importing the parent's `security_gate.py`** — a phantom pass on a security suite, the dangerous direction. Workaround that works: a `pytest_configure` plugin that strips the parent root and force-imports `security`, `agent`, `tools` from the worktree, run as `PYTHONPATH=/tmp/wtplug python -m pytest -p wtpreload ...`. Ticket filed: `.thoughtmachine/working_docs/ticket-worktree-syspath-hardcode.md`. **Implication: any earlier "N passed" figure from a worktree run is suspect** unless it was produced under that plugin.
3. **`GlobTool` returns 0 results for patterns that do exist** (e.g. `conftest.py`). Never trust an empty glob as evidence of absence — confirm with a directory listing or a direct read.
4. **`git_read` `show_file` takes `path`, not `file_path`**; passing `file_path` yields a bare "path is required". Also it rejects container-style paths (`/workspace/...`) with "outside workspace" — use host-workspace-relative paths (`.worktrees/<name>/...`), not the container path the worker sees.
5. **`git_write` `worktree_add` ignores its `branch` argument** and produces a DETACHED worktree; you must follow with `checkout` to attach the branch. Costs a confusing detour every time.
6. **DockerCodeRunner has no `git` binary and its `/tmp` does not persist between calls** — a preload plugin must be recreated on every invocation (cheap, but it invalidates "just set it up once" plans).
7. **Worker hard timeout ceiling is 600s.** A 4-task chunk with a full test sweep took 430-510s and produced ~80k tokens of report. Chunk to ~4 tasks and expect to spend the whole budget; a 5th task gets truncated and the worker reports it as "out of scope" rather than as a timeout.

## 2026-11-15 — **8. A worker can silently LOSE its own task instructions wh...

**8. A worker can silently LOSE its own task instructions when its context compacts mid-task.** Observed live twice in one session. Symptom: the worker returns a confident "task complete" report that covers only *part* of the brief, and mentions in `meta.struggles` that "the numbered list is not in my post-summarisation context". In the worst instance a two-part brief (cheap doc write + expensive refactor) came back with the *cheap* part silently dropped because it had ordered the work the other way round. Mitigations that worked: (a) put the cheap deliverable FIRST in the brief, (b) number the deliverables and require the final report to be numbered to match, (c) keep a brief to ~2 tasks when it includes long test runs, (d) treat a report with no per-deliverable numbering as incomplete and re-verify the artifacts on disk yourself rather than trusting the summary.

**9. Verify a worker's edits with a read of your own, not its report.** In the same session a worker correctly reported fixing a ticket's Status line as "blocked: file does not exist" — it was right (the file was on a different branch), and a less careful agent would have "fixed" it by inventing the file. Cross-checking `git status` plus `show_file` on the committed blob caught both the real work and the missing work.

## 2026-11-15 — **10. `Worker spawn` in process mode can hard-fail with `Wor...

**10. `Worker spawn` in process mode can hard-fail with `WorkerQueryTimeout` AFTER the worker has already done the work.** Observed: a 480 s spawn returned `WorkerQueryTimeout: query ... got no reply within 540.0s`, and `Worker check` then showed `status: ready, alive: false`. But `git status` in the target worktree showed both files already edited. **The report was lost, the edits were not.** Consequences: (a) never retry the same spawn blindly on this error — you will get duplicate edits stacked on top of the first attempt; (b) always reconcile by inspecting the artifacts (`git status` + `git diff`) before deciding what happened; (c) therefore, prefer to give a mutating worker task a *checkpoint the agent can verify* (exact files, exact intended hunks) rather than a narrative deliverable. A second spawn of the same task hit the same ~540 s wall (elapsed 536.8 s) even with `timeout_seconds: null` (effective 600) — so the wall is upstream of the worker's own timeout setting.

**11. You cannot verify a full pytest run from inside a worker: the container runner caps one call at ~360 s and background processes do not survive between calls.** The whole suite here takes >10 minutes (measured: 53 % at 360.8 s, progress still advancing, so it is slow, not hung), dominated by WebSocket / security-prompt integration tests that dump large payloads and block on socket reads. The only workable pattern is targeted subsets (`pytest -q <specific files>`, `-k` filters) plus `python -m py_compile` for a syntax gate. Any earlier "full suite in 6.3 s" figure must have been a narrower file selection — do not repeat it as if it were the whole suite.

## 2026-11-15 — FRONTEND SILENT-GATE PATTERN (found on the worker-panel-blan...

FRONTEND SILENT-GATE PATTERN (found on the worker-panel-blank hunt, dev 4dc40618): a chain where every hop logs can still lose the payload at the ONE hop that does not log. Here: bridge envelope `{type:'worker:<evt>', worker_name, instance_id, instance_label, timestamp, data}` always stamps a numeric instance_id; the panel's own filter (WorkerOutputPanel.jsx:282) correctly requires "both sides present" before comparing instance ids, but the upstream router (components/chat/workerEventRouting.js:29) compares strictly with no guard — so `event.instance_id === panel.instance_id` is `1 === undefined` → false → zero events reach the panel, including worker_spawned/worker_status, which is why the status dot stays "Idle". Diagnostic lesson: when two layers implement the same matching rule with different leniency, the stricter layer wins and the failure is attributed to the layer that logs. Always compare duplicated predicates across layers, not within one layer.
Also: a UI symptom described in prose by a human ("it shows Idle Worker") may not be a symptom at all — there is no such string; STATUS_DOT.ready.label is literally 'Idle', the normal healthy label. Grep for the literal before theorising about it. The real "nothing rendered" surface was `events.length === 0 → .worker-output-empty`.

## 2026-11-15 — LIVE TOOLING PROBLEMS (for the engineer, session Gen65). (1)...

LIVE TOOLING PROBLEMS (for the engineer, session Gen65). (1) Context budget: one Read-only forensics worker report on a 4-layer trace consumed ~77k worker tokens for 39 tool calls / 148 s and its report alone pushed my context past the 86k critical threshold immediately after a fresh summarise — deep code-archaeology tasks summarise me out within one spawn. A cheaper pattern is needed: have the worker emit findings as file:line + one-line verdict only, or write the long report to a working_doc and return a summary + pointer. (2) Two context resets were reported by the worker mid-task on a 148 s spawn, i.e. it summarised itself twice inside one task, so late deliverables in a numbered list are at real risk; I ordered deliverables so the crux was #1 and it survived. (3) `git_read search`-equivalent is absent: I cannot grep, so every code question costs a worker spawn — a read-only grep/glob tool for the agent would remove ~80% of my spawns. (4) `merge_base` ignores `commit1`/`commit2` and needs `a`/`b`+`is_ancestor`, `worktree_add` needs `path` not `paths` and drops `branch` (detached HEAD), `branch_create` ignores `commit1` — three silent parameter-name mismatches in one tool family, each costing a round trip. (5) Spawning with timeout_seconds=480 was honoured (timeout_clamped:false) and the worker finished at 148 s — generous timeouts are safe and cost nothing; that part works well.

## 2026-11-15 — PROCESS FAILURE (mine, Gen65): before writing a NEW test fil...

PROCESS FAILURE (mine, Gen65): before writing a NEW test file for a module, search for an EXISTING suite covering it. I had a worker add `src/components/chat/__tests__/workerEventRouting.test.js` when a comprehensive suite already lived at `src/components/__tests__/workerEventRouting.test.jsx`. The duplicate had the relaxed expectation baked in and the original had the strict one, so my "green" claim was meaningless — CI caught it, I didn't. Two suites for one module = the same drift that causes duplicated predicates to diverge. Rule: one suite per module, extend it, and if a behaviour change contradicts an existing assertion, treat that assertion as a specification to argue with, not a thing to edit until green.
Related design rule learned the same day: fix a broken invariant at its SOURCE, not by widening the contract downstream. Here the panel identity was repaired at creation (backfill `?? 1`); the tempting alternative (make the router forgiving) would have let an identity-carrying event reach a panel that cannot be identified — i.e. silently mixing two worker instances into one stream. Widening a fail-closed check to stop a symptom usually trades a visible bug for an invisible one.
FRONTEND TEST INFRA FACTS worth not rediscovering: (1) per-worktree `node_modules` does not exist, but a symlink works — `cd .worktrees/<wt>/web_ui/frontend && ln -s ../../../../web_ui/frontend/node_modules node_modules`; note `web_ui/frontend/.gitignore` has `node_modules/` (trailing slash) which does NOT ignore a symlink of that name, so remove the link after running or it shows up as untracked. (2) vitest in the container fails with `ENOENT: mkdir '/var/tmp/gobo…/web'` unless `TMPDIR=/tmp` is set — otherwise you get a bogus "Test Files 1 / Tests no tests / Errors 1". (3) There is no file-unlink tool for the agent/worker: FileEditor 'delete' wants line_numbers and only empties the file; deletion had to go through ContainerExecTool `rm` (host_bash is banned).

## 2026-11-15 — ## 2026-02 — I was wrong about "no Zustand", and a §live-too...

## 2026-02 — I was wrong about "no Zustand", and a §live-tooling note

**My error (record it, it matters):** for three sessions I asserted "there is no Zustand store in this app; it's plain React useState". That was **half wrong**. Zustand IS a dependency (`web_ui/frontend/package.json:24` `"zustand": "^4.5.0"`) and there are **three live stores**: `src/sessionTabsStore.js:58`, `src/store/workspaceStore.js:245`, and `src/store/useStore.js:153` (slices: sessions, sessionModes, tabRunningStates, sessionConfigs, sessionMessages, sessionStates, sessionErrors, sessionDrafts, addendumEntries, runtimeNotices). What is true is the narrower claim I should have made: **`workerEvents` is not in any store** — it is `useState` in `App.jsx:193` (`{[sessionId]: [event,…]}`), read via `routeEventsToPanels()` in a `useMemo` at `App.jsx:232`. So "the Zustand slice for workerEvents is not being updated" was unfounded — there is no such slice. Lesson: when I refute a premise, refute the *specific* claim ("no workerEvents slice, no addWorkerEvent mutation anywhere") rather than the general one ("no state library") — the general claim was falsifiable in one grep of package.json and I never ran it.
- Second false-premise check that DID pay: the same pass found `worker:worker_partial_result` missing from SessionTab's allow-list switch, i.e. "grep for the function the other engineer *assumed* exists" (`addWorkerEvent` → 0 hits) is what proved the premise wrong, and "diff the emitted type strings against the consumed case labels" is what found the real bug. Both are cheap, mechanical, and should be step 1 of any "the UI shows nothing" diagnosis.

**Topology fact worth reusing:** there are **two** WebSocket connections. `App.jsx:284-322` `connectHub` = "Hub WebSocket (sessions list only)", only reacts to `msg.type === 'open_sessions'`; it never inspects `worker:*`. Worker events arrive on the **per-tab** socket: `SessionTab.jsx:352` `connectSessionWs` → `:371 new WebSocket(wsUrl())` → `:379-386 handleEvent(msg)` → switch → `onWorkerEventRef.current(...)` (ref set at `:345-346`, wired from `App.jsx:1440 onWorkerEvent={handleWorkerEvent}`). Any future "events don't arrive" report must say *which* socket is being inspected.

**Live tooling problems for the engineer (this pass):**
1. **No git binary inside worker containers.** `meta.tool_issues`: "git is not installed in the container image". Workaround discovered: the `git_read`/`git_write` tools carry their own containerized git backend, so real `git diff` still works from inside a worker — but `git diff --stat` has no tool flag, so stat lines have to be hand-assembled. Please add `--stat` (and `--name-only`) to `git_read diff`, or install git in the image.
2. **No ripgrep in the image.** Workers fall back to `grep -rn`, which works but is slower and noisier. There is still **no grep/search tool for the agent itself** — every code question costs a full worker spawn (98 s and 166 s this pass for two questions that a `rg` would have answered in 2 s).
3. **`FileSearchTool` rejects absolute `/workspace/...` paths** (it roots at the host path), while `FileEditor`/`ApplyEdits` accept workspace-relative `.worktrees/<name>/...`. Inconsistent path convention across the file tools.
4. **`git_write worktree_add` ignores its `branch` argument as a "create this branch" instruction** — passing `branch` + `base=dev` produced `worktree add <path> dev` and failed with "'dev' is already used by worktree". The working recipe is: `branch_create` (which correctly bases on the main worktree's HEAD and reports the base hash) → then `worktree_add` with `base` = the existing branch name.
5. Confirmed again: a clean worktree's `git status` returns **empty output** rather than a "clean" line, which reads like a tool failure.

## 2026-11-15 — ## Live tooling problems (Gen65, worker-isolation investigat...

## Live tooling problems (Gen65, worker-isolation investigation)

1. **I briefed the worker with the WRONG workspace root.** I told two workers "your workspace root IS the worktree at `.worktrees/<name>`". Both times the worker's root was actually the **main checkout**, with the worktree nested *inside* it at `.worktrees/<name>`. Worker #1 worked it out and reported it; worker #2 got an explicit warning and worked it out immediately. Correct phrasing: "your workspace root is the main checkout; the tree to edit is the nested git worktree `.worktrees/<name>`, so prefix all paths with it."
2. **Worker substrate died once** mid-session: `WorkerRuntimeLost: ... failed in the runtime: 'error'`. After that, `action='query'` answered "Worker 'default' is not running" while `Worker(action='inspect')` reported `child_pid 2567521, child_alive: true` and `running_workers` said `status: ready, alive: false` — three tools, three different answers about the same worker. `Worker(action='spawn', force=True)` recovered instantly. `inspect`/`running_workers`/`query` disagreeing is worth fixing in the substrate.
3. **`spawn` rejects `query=` plus `context['query']`** unless byte-identical ("Conflicting tasks"). Reasonable, but the error arrives after you have already written the whole brief; putting the task in exactly one place is the only safe habit.
4. **Workers have no `git` binary and no `ripgrep`.** `rg` and `git` both fail. Every brief must say "use plain `grep -rn`; I handle commits."
5. **`FileEditor` / `ApplyEdits` / `FileSearchTool` reject absolute `/workspace/...` paths** with a path-violation security error; only workspace-relative paths work. Combined with (1) this is a real trap.
6. **`worker_thread.py.bak.<epoch>` leftovers** appear in worktrees (gitignored, so invisible in `git status`). Workers evidently still write backup files when editing. Harmless but accumulates; check for them before declaring a tree clean.
7. **Soft-timeout budget reality**: 300 s spawns that genuinely need ~190–245 s work fine, but a 300 s spawn whose `elapsed_seconds` lands within ~5 s of the cap is a soft timeout and its "final" content must be distrusted. Both spawns here finished comfortably inside (188 s, 244 s) with `timeout_triggered: false`.
8. **Good pattern — proving a red state without touching source**: a throwaway test file that `delattr`s the injected attribute in memory and restores it in a `finally`, importing `_make_processor` from the real test module. Zero source edits, worktree stays clean, and the pre-fix `AttributeError` reproduces verbatim for all 6 parametrized event types. Reusable for any "I added a no-op method to satisfy a contract" fix.


## 2026-11-16 — ## Gen66 — live tooling problems (reported, not worked aroun...

## Gen66 — live tooling problems (reported, not worked around silently)

1. **`git_write worktree_add(branch=X)` does not create branch X.** It creates a worktree at the base in **detached HEAD**, despite the parameter being called `branch`. Recovery: `branch_create(branch=X, base=<sha>)` from the **main checkout** (not the worktree), then `checkout(branch=X)` inside the worktree.
2. **`branch_create` is refused in a detached-HEAD worktree** with `refusing to commit: the workspace HEAD is detached (not on a branch)` — the commit guard's message and policy applied to the one operation that repairs the condition. `checkout` is not blocked in the same state, so the guard is over-broad. Net effect: an agent that follows the error's own advice loops.
3. **`.venv` is unusable inside worker containers.** `.venv/bin/python` is 3.13 while `site-packages` is `lib/python3.12` (`No module named pytest`), and `.venv/bin/pytest`'s shebang points at a non-existent `/usr/bin/python3.12`. Use system `python3` (3.12.15, pytest 9.2.0). Briefing workers to "use the venv" wastes a cycle.
4. **`git_read ls_tree(recursive=true, pattern=...)` ignores the pattern** and dumps the whole tree (~10k tokens, truncated). Never use it for filename lookup in this repo.
5. **`git_read log(commit=X)` ignores `commit`** and logs HEAD instead (asking for `b60c44d3` returned `dev@b800cb48`'s log). Use `branch=origin/dev`.
6. **`Worker(action='query')` fails right after a completed spawn**: `Worker 'default' is not running`. The worker exits after `status: final`, so its context is gone — a follow-up must be a fresh `spawn(force=True)` carrying the full brief again.
7. **A completed spawn can return `status: null`, `confidence: null`** while having full, correct `content` (observed on a 77s job). Do not treat null status as failure; read `content` and `telemetry`.
8. **Provider streaming `APIConnectionError` aborted a turn mid-flight**; no state was lost (tool results stayed in history) but the user had to nudge with "go on".
9. Container has no `git`, no `ripgrep`; `FileEditor`/`ApplyEdits` reject absolute `/workspace/...` paths; workspace root is the MAIN checkout so worktree paths need the `.worktrees/<name>/` prefix.
10. **Verify before you build.** Four remote branches (`fix/process-pause-deadlock`, `fix/worker-pause-registry-race`, `fix/gen67-worker-runtime-default-process`, `fix/runtime-default-truth`) were already merged into dev; a whole night of log analysis described a build older than the branch. `merge_base(is_ancestor)` is cheap and should run before any fix is written. Corollary: a zero grep count is only evidence if the handler can actually emit at the configured level — check the logger level first.

**Correction (Gen 66, verified):** the failure mode is narrower than "`.venv` is broken." In the container, `.venv/bin/python` runs but raises `No module named pytest` — use system `python3` (3.12.15 / pytest 9.2.0). On the **host**, `.venv` works fully: Python 3.12.3, pytest 9.1.1. Test invocations from the host use `.venv/bin/python -m pytest`. Verified at `544bd407`.

## 2026-11-16 — ## Production logging threshold is WARNING — any DEBUG/INFO ...

## Production logging threshold is WARNING — any DEBUG/INFO diagnostic is invisible (verified at dev ee1ac54c)

Anything added to `logs/backend_startup.log` at DEBUG or INFO on a `tools.*` logger is **dropped before it reaches the file**. Verified by count on a live 4145-line post-restart log plus a container reproduction of the spawn-child logging behaviour:

- **Parent/backend process:** `tools.workspace.worker` is `logging.getLogger("tools.workspace.worker")` (`worker_thread.py:834`) with **no `setLevel` anywhere** → NOTSET → inherits **root = WARNING**. The repo installs **no root handler at all** (zero `basicConfig`/`dictConfig` in `agent/ tools/ web_ui/ thoughtmachine/ session/ infra/`); the handler printing `LEVEL:name:msg` comes from the launcher/dependency. Handler installs that do exist: `agent/logging/console.py:61-64` (on `thoughtmachine` only, `TM_LOG_CONSOLE_LEVEL` else WARNING), `agent/logging/lifecycle.py:70-76` (`thoughtmachine.lifecycle` at INFO), `session/history_provider.py:34-44` (env-gated).
- **Worker child process:** configures **no logging whatsoever** (`worker_runtime_main.py` / `worker_runtime_process.py` / `worker_runtime.py` only `getLogger`). A `spawn` child inherits no handlers and no level → only `logging.lastResort` prints, and it prints **WARNING+ with a bare message** (no `LEVEL:name:` prefix). That is exactly why the child's `Worker worktree ... skipped` lines appear unprefixed in the log.
- **Log evidence:** 39 `^WARNING:<logger>:` records from stdlib-style loggers, **0 INFO and 0 DEBUG records from any `tools.*` logger**, while many `logger.debug(...)` sites sit on certainly-executed paths. The 3412 `INFO` lines are all uvicorn's own format; an `INFO:` prefix in the file does **not** mean my INFO records would print.
- **Consequence:** to be observable in production a diagnostic **must** be `logger.warning`. Precedent and rationale already documented in-repo at `infra/container_manager.py:5859-5866` ("WARNING is used deliberately: an INFO line would be dropped ... WARNING reaches stderr, which start_thoughtmachine.sh tees into logs/backend_startup.log"). `start_thoughtmachine.sh:184` does `( exec ... ) > >(tee "$BACKEND_LOG") 2>&1` — stderr is the channel that survives.
- **Self-correction:** the DEBUG line I shipped in PR #391 for the join guard is **invisible in production**. Next time: WARNING, one-shot.
- **Corollary that made this diagnosable:** the one-shot `worker runtime mode resolved to 'process'` WARNING (`worker_runtime.py:159`) is the load-bearing proof of which build is running, precisely because it is WARNING and once-per-process. Design every production diagnostic that way: **WARNING + one-shot + greppable noun phrase**.

## 2026-11-16 — ## `.gitignore` catch-alls silently swallow new docs (bit me...

## `.gitignore` catch-alls silently swallow new docs (bit me on `docs/`)

`docs/` has a `docs/*` catch-all with per-file negations; `.thoughtmachine/knowledge/project/` is ignored wholesale with ~15 explicit `!` re-inclusions. Consequence: a **newly created** doc in either place is untracked-AND-ignored, so it never appears in `git status`, never gets committed, and vanishes on the next clone — while any pointer to it (index entry, cross-ref) survives and turns into a broken reference. This is exactly the mechanism that made `INFRA_ONBOARDING.md` misleading (17 index entries pointing at files not on disk).

Detection: after creating any file under `docs/` or `.thoughtmachine/knowledge/`, confirm it is *visible to git*, not merely present on disk:

```
git check-ignore -v docs/NEWFILE.md     # prints a rule => it is ignored
```

Git-compatible check without the git binary (worker containers have no git): `pathspec` 1.1.1 is installed — `GitIgnoreSpec.from_lines(...)` + `.match_file(relpath)`. Verified this reproduces git's answer for this repo.

Fix: add `!docs/NEWFILE.md` (and `!docs/` if the parent dir itself got pruned). Commit the negation in the SAME commit as the doc, otherwise the doc is lost while the commit looks complete.

Tooling gap worth reporting: neither `git_read status` nor `git_write commit` warns that a file the agent just wrote is ignored-but-uncommitted — the commit simply succeeds with a smaller file list. A "wrote N files, committed <N files" mismatch check would have caught this instantly.

## 2026-11-16 — ## C76 measurement (spawn 1) — suite-wide trust harness: sma...

## C76 measurement (spawn 1) — suite-wide trust harness: small blast radius in auth_boundary, concentrated in tests/
Worktree `.worktrees/c76-trust-harness` @ `544bd407`. Write-up: `measurement.md` (298 lines) + `## Stage 1–4` sections.
- **S1 baseline** `web_ui/backend/tests/test_auth_boundary.py` (⚠️ NOT `tests/test_auth_boundary.py` — that path does not exist) → 66 tests green.
- **S2 flag off suite-wide** → **1 failure (A:0, B:1)**: `test_a_freshly_imported_boundary_carries_the_suite_trust_value` — a harness *self*-test (asserts `fresh_boundary.TRUST_TEST_HARNESS is True`, 200 for a fresh harness re-import); legitimately needs the harness.
- **S3** `-k "auth or websocket or harness or trust or permission"` over `tests/` → 1046 passed, 5 skipped, 6329 deselected, **42 failed (A:6, B:36)**; 52–91s per run (cheap — ran twice, nowhere near the wall).
- All 42 share one mechanism: the closed gate refusing the `TestClient` peer `testclient` → **26× HTTP 401 `{"error":"unauthorized"}`** (16 at `tests/integration/harness.py:333`, 1 at `:373`, plus `test_ui_permission_persistence_flow.py:70`, `test_container_record_user_action_routes.py:399`, `test_permission_refresh_ws_clobber_guard.py:108`, `test_permission_sidecar_clobber_guard.py:82`/`:202`, `test_seeding_path_invariants.py:302`, `test_safe_defaults_single_owner.py:146`, `test_workspace_containers_view.py:389`) and **16× `WebSocketDisconnect`** at `starlette/testclient.py:143`.
- **A-class = 6**: 5× `tests/integration/test_bind_workspace_authority.py` (`:156,:201,:238,:362,:405` — "would adopt B is refused", "cannot re-scope a handshake-bound socket", "refusal leaves `bridge._workspace_path` alone") + `tests/test_container_record_user_action_routes.py::test_permission_denied_403_audited` (`:391`, needs 403/no-permission distinguishable from 401/no-credential).
- **B-class = 37** (1+36) = the explicit opt-in list for spawn 2.
- **Restore: MATCH** — sha256 `a8c194c0b2c72e0aff19333f7f8d9a2c07528928f126004b30dbb7f72e84a606` pre-edit and post-restore; `git status --short` = only `?? measurement.md`; no `.bak`/`.orig`/`.pytest_cache` (every run used `-p no:cacheprovider`).
- **Design conclusion**: the audit's "default off, require opt-in" is cheaper than feared for `test_auth_boundary.py` (1 test) and is entirely mechanical in `tests/` (36 of 42 are incidental setup).

### Problems hit live (infra log)
1. **Brief path drift**: the operator-authored brief pinned `tests/test_auth_boundary.py:161-167`; the file is `web_ui/backend/tests/test_auth_boundary.py`. It also made Stage 3's `--ignore=tests/test_auth_boundary.py` an **inert no-op** — a silent false assertion about coverage (harmless here only because that file is outside `tests/`). Briefs that pin file paths must be verified on the worker's first read, and deviations reported.
2. **Ephemeral code-runner containers**: pytest output written to `/tmp` in one tool call was **gone** when a later call tried to append it → the first Stage 1 block in `measurement.md` came out empty and needed a correction note. Capture every result **inside the same tool call** as the run.
3. `.venv/bin/python` **executes** inside the container but has `No module named pytest` → always fall back to system `python3` (3.12.15 / pytest 9.2.0).
4. **No `git` in worker containers** → a brief instructing `git checkout -- <file>` to restore is not executable by the worker. Working substitute: record pre-edit `sha256sum` + the verbatim original lines at Stage 1, reverse edit, re-hash, compare. Proven clean this spawn.

## 2026-11-16 — **Gen 68 — WorkingDocument `create` is silently lossy. Use `...

**Gen 68 — WorkingDocument `create` is silently lossy. Use `append`.**
`WorkingDocument(action="create", doc_id="my-spec", title=..., sections=[...], content=<long spec>)` writes a file whose sections are ALL EMPTY STRINGS and whose filename is an auto-generated id (`4a3ec61fa7e5.json`), not the `doc_id` you passed. Two separate defects in one call: `doc_id` ignored, `content` ignored. It reports success. Consequence: I briefed a worker to `cat` a filename that never existed; the worker spent a full spawn on `find / -xdev` (0 hits) and aborted with 0 edits — the second consecutive abort on the same task.
Remedy that works: `create` (sections only, expect the auto id from the response), then `append(doc_id=<returned id>, section=..., content=...)` — append is honoured. Read the returned `doc_id`/`path` and use THAT, never the name you asked for. Belt-and-braces: also inline the whole spec in the spawn `context.query`, and make the worker's first action a heredoc write of the spec into its own worktree, re-read after every couple of edits. That third mechanism is the only one that survives worker-side compaction.

**Gen 68 — never re-inject a citation from a previous document; source quotes from disk.**
Sweeping `docs/CONTRACTS.md`'s stale "required check cannot be determined" section, I supplied two KB quotes copied out of the OLD section. They were paraphrases (single quotes for double, `` `--required` `` for bare, and the leading sentences dropped entirely). The worker inserted them faithfully, so the sweep briefly *created* new rot — attributing words to two KB files that those files do not contain. One dropped prefix (`CAUTION: this is about how the CLI reports merge eligibility, NOT about Edge 8`) also inverted the analysis: the two notes were about different objects and did not contradict.
Rule: a doc-rot sweep must quote byte-exactly, and the instruction to the worker must be "read line N and paste its bytes", never "insert this sentence". Give the worker a verification step that diffs the quote against the file and reports MATCH/MISMATCH; an honest worker will then flag your paraphrase instead of laundering it.

**Gen 68 — deleting a stale section deletes its evidence; retire the conclusion, keep the proof.**
The first pass replaced a 26-line OPEN ITEM wholesale. The conclusion was indeed false and should die, but the section also held two quoted KB notes and a count-discrepancy analysis that cannot be reconstructed from the checkout. Better edit shape, and the one finally committed: corrected `**RESOLVED (partial):**` paragraph above, `### OPEN ITEM (revised)` below carrying the preserved evidence verbatim. Reviewing the worker's own `git diff` before committing is what caught this — the worker reported it as a "judgment call", not as a loss.

**Gen 68 — the regenerate-a-hashed-doc contract does not need git.**
`tests/test_onboarding_bundle_contract.py` pins sha256 for 9 bundle members, so editing any of them forces `HANDOFF.md` regeneration. `scripts/gen_handoff.py` looks like it needs git; it does not. The three git facts are literal constants in the test file (`PINNED_DEV_SHA`, `PINNED_DEV_SUBJECT`, `PINNED_RECENT`), deliberately frozen so `dev` advancing cannot turn the contract red. Pass them as `--dev-sha/--dev-subject/--recent-file` and it needs no git at all — so a container with no `git` binary can still satisfy it. Verify by checking that exactly ONE digest line changed in the regenerated file.
Gotcha: write the `--recent-file` from a Python literal, not bash `printf '%s\n' "line with 'quotes'"` — bash quote-concatenation strips the inner quotes from `'origin/dev'` and you get a silent content diff (2 failed / 6 passed) that looks like a generator bug.

**Gen 68 — host memory pressure kills pytest with rc=143 and empty output.**
Four consecutive pytest runs, including a bare `--collect-only`, were SIGTERMed mid-run while `pytest --version` and `python3 -c` worked. Cause was host swap exhaustion (`free -m`: swap 12287/12287 MiB), not the tree. Symptom to recognise: rc=143, no pytest summary line. Retry after pressure eases or raise the runner memory limit; do not conclude the tree is broken and do not report counts from a run that never printed a summary line.

## 2026-11-16 — **Gen 69 — the C76 marker poison is a snapshot taken too ear...

**Gen 69 — the C76 marker poison is a snapshot taken too early, not a stale module object (measured, both rounds).** The 2b-ii-a markers made `tests/integration/test_config_control_matrix.py` 3 failed / 5 passed. The obvious theory (teardown restores onto the pre-restart module object, so the live module stays False) is REFUTED by instrumentation: at the victim's setup entry the live module is a NEW object but `flag_live=True`; the CONFIRMED signature never appears where predicted. What is poisoned is the single authority `_suite_trust`: `pytest_runtest_setup` is `tryfirst`, so `item.stash[_PREVIOUS_TRUST] = auth_boundary.TRUST_TEST_HARNESS` reads the shipped default `False` *before* the session-scoped autouse fixture installs `True`; `pytest_runtest_teardown` then calls `enable_test_harness_trust(False)`, and the `_remembering` wrapper writes that into `_suite_trust`; every later re-import is re-armed from `_suite_trust`, so each subsequent `*_survives_restart` test gets a disarmed boundary and 401s. Full-file round 2 put the whole chain in print: one single `remember value=False caller=conftest.py:263 caller_fn=pytest_runtest_teardown` is the only flip in the run; the second and third marked tests then stash False as a *consequence* of it. Two consequences worth carrying: (a) the tempting one-line fix "stash `suite_trust_value()` instead of the module attribute" is INERT — the probe shows `suite_trust=False` at that same entry point, so it captures False too; the fix has to change ordering (a function-scoped autouse yield fixture runs after the session fixture), not the value source; (b) two of the eight tests pass even with `_suite_trust=False` because their live module object is still armed, so a regression test for this must be a `*_survives_restart` shape or it detects nothing. Evidence: `.worktrees/c76-trust-harness/probe_marker_teardown.md` §1-§6 (262 lines), conftest.py restored byte-identically (sha `1534bcb6…`, 232 lines) in both rounds.

**Gen 69 — the agent cannot run an operator "one-block shell" cleanup, and cannot commit on `dev`.** Two hard limits found live, worth reporting to an engineer rather than working around silently. (1) There is no shell tool on the agent side at all: `rm -f`, `git pull --ff-only` and `git status` on the host are simply not expressible, and the git surface is `git_read` + `git_write`, which has **no `pull` and no file-delete** — so an operator script containing `rm` + `stash` + `pull` + `stash pop` cannot be executed by the agent even in principle; it must be the operator's, or split into "operator runs the two commands I lack, I run the two I have". (2) `git_write commit` is REFUSED on `dev` (`write_on_feature_branch` permission: protected branches dev/master/main), so the documented pattern "commit the KB file to dev" fails; the workable shape is `branch_create base=dev` → `checkout` → `commit` → back to `dev`, but beware that a KB append made while on `dev` will CONFLICT when popped on the branch (both sides append at EOF), and `git_write` then has no way to restore a file — recovering needs `unstage` → `stash_push` → re-append, because there is no `checkout -- <file>` and no `pull`. Reporting the whole block at once beats discovering them one failed call at a time.

**Gen 69 — a worker spawn can end with `status: null` and an unfinished task, and `action='query'` cannot resume it.** A spawn returned `content: "Decisive. Verdict: … — writing the artifact, then restoring and proving it."`, `status: null`, `confidence: null`, `tool_call_count: 8`, `elapsed 246.8s` of a 540s budget — i.e. the worker stopped mid-task having made its measurements but written neither the artifact nor the restore. The follow-up `action='query'` was rejected with `Worker 'default' is not running`, so there was no way to continue the *same* instance; a fresh `spawn` (same instance 1, conversation continued) recovered it and finished in 104s. Handling that worked: treat `status: null` as "unfinished, verify on disk before trusting the prose", check the worktree with `git_read status` to see what the worker actually left (here: instrumented `conftest.py`, backup file, no report), and re-brief with the restore step promoted to FIRST priority plus "tee every run into a log inside the worktree" so a second interruption cannot destroy the evidence. Ask for the sha/summary-line proof in the reply, never the conclusion alone.

**Gen 69 — "write the brief verbatim to a file" deviates silently under token pressure.** Both probe workers were told to heredoc the full task text into their worktree as their first action; the second one disclosed that it wrote a *condensed* 31-line brief instead, to save tokens, and removed it as instructed. That is the failure mode the heredoc rule exists to prevent (a worker that compacts mid-task then re-reads a summary of a summary), so accept the deviation only when the worker says so explicitly — as this one did — and prefer briefs whose critical invariants are restated in the reply requirements (sha values, expected counts) rather than only in the brief text.

## 2026-11-16 — ## C76 2b-ii-b / b-1 — marking the trust harness: measure-fi...

## C76 2b-ii-b / b-1 — marking the trust harness: measure-first, then a falling-count gate
**Context:** C76 makes the in-process ASGI harness trust explicit per-test instead of session-wide, so the trust flag can be flipped (2b-iii) without leaving suites red.

1. **Measure before you mark.** `web_ui/backend/tests/` had *zero* flag-off evidence (Stage 2 ran only `test_auth_boundary.py`, Stage 3 ran top-level `tests/`). One measurement pass (b-1a) turned "unknown blast radius" into an exact split: flag-ON 305 collected/300 passed/5 xfailed/**0 failed**; flag-OFF **76 failed**/224 passed/5 xfailed, rerun byte-identical, reverse-direction 0. Never mark blind.
2. **There is no env off-switch.** `auth_boundary.py:228-229` — the trust value is never read from the environment (deliberate: an env switch is itself a bypass). So every flag-off run is a 2-line source mutation of the *root* `conftest.py` (`_trust_the_in_process_asgi_harness`). **Locate by symbol, never by line number** — the 2b-ii-a fix shifted it 163→170 and any line-number brief rots instantly. Pristine sha `5aeb59e8…`, flipped sha `93f2eef3…`.
3. **The gate is a falling count, not a green run.** Each chunk's flag-OFF whole-dir failure count must fall by *exactly* the number of markers added: **76 → 43 → 21 → 1** for +33, +22, +20. The single survivor is the self-test that asserts the trust value is present — it *must* fail flag-off, so it is the invariant, not a miss. Recompute the same invariant every chunk; it caught nothing this time, which is exactly what "it works" looks like.
4. **Leave known-red-alone.** `TestSessionPermissionsRouteis::test_get_falls_back_to_legacy_record_permissions` is xfail in *both* directions (known product bug). Marking it would have made the count fake. Distinguish "fails only flag-off" (mark it) from "fails either way" (do not touch).
5. **Worker proof-of-work = numbers, not prose.** Every chunk brief demanded the literal invariant line (`flag-off dir: N failed (expected M)`) plus the flag-ON line. All three chunks returned the exact expected numbers — this is the only reason I trusted commits while the worker envelope was reporting `status: null` (see below).

## 2026-11-16 — **Long-document construction by delegation: verify, don't tr...

**Long-document construction by delegation: verify, don't trust structure.** Writing a 3,700-line audit in one-section-per-dispatch increments failed silently three times: workers returned sections with every structural count correct ("15/15 five-part labels present", "12 blocks, 5 labels each") while omitting mandated *content*. Caught only by independent passes that quote candidate text verbatim. Three countermeasures that worked, in cost order: (1) mandate literal-token counts in the dispatch and recount yourself — cheap, but paraphrase evades a token count, so when a count is 0, read the candidate entries before ordering a repair; (2) a bounded extraction pass (`purpose=reviewing`, `style=precise`, 90-180s) that says `ZERO HITS` explicitly — caught all three omissions plus a proposal that asked for something the code already did; (3) for documents that cite themselves by absolute line number, prepending a guide shifts every citation — do not mechanically rewrite (source-code anchors `file.py:NNN` are indistinguishable from document positions to a regex), state the offset constant once instead. Also: a document whose own thesis is "status lines decay" acquired a stale `Status: §02 complete — §03 onward pending` line; regressions of the thing you are auditing are the expected failure mode, so check for them last.

## 2026-11-16 — ## Gen69 — C76 integration chunk 5b closed; two ledger corre...

## Gen69 — C76 integration chunk 5b closed; two ledger corrections + a hard tooling bug

**Integration marker sweep is now COMPLETE** (commit `0c4f50f5`, 12 files, 61 decorators for 65 node ids; 5 functions parametrised so ids > decorators). Census gate `-m trust_harness --collect-only` = 103/327 (12 pre-existing + 26 chunk 5a + 65 chunk 5b), `-m "not trust_harness"` = 224. Flag-OFF whole directory went from 235 passed / 91 failed to **326 passed / 0 failed / 1 skipped**. Flag-ON 12 files: 72 passed.

### Correction 1 — my own ledger was wrong, and the worker caught it
I had recorded "2d `web_ui/backend/tests/` flag-OFF **300 passed, 5 xfailed**" in chunk 5a. The 5b worker was asked to reproduce 300/5 and returned **1 failed, 299 passed, 5 xfailed** — and led its reply with the discrepancy instead of hiding it. The one failure is `web_ui/backend/tests/test_auth_boundary.py::test_a_freshly_imported_boundary_carries_the_suite_trust_value`, failing on `assert fresh_boundary.TRUST_TEST_HARNESS is True`. That test carries **no** marker, so under flag-OFF it *must* fail; 300/5 is the flag-ON number. Independent proof it is not a 5b regression: chunk 3b's log (`logs/b1b3_3b_flagOFF_dir.log`) shows the identical `1 failed, 299 passed, 5 xfailed` with the same test named, while flag-ON (`b1b3_3e_flagON_dir.log`) is 300/5. **The 300/5 figure in my 5a ledger was the flag-ON result mislabelled.** Lesson: an expected value quoted to a worker is a hypothesis, not a fact — a worker that returns the honest different number is doing its job. Do not "fix" the worker, fix the ledger.
**Carry-forward finding:** that test is a legitimate trust-harness-dependent test in `web_ui/backend/tests/` that is still UNMARKED. It is the one visible residue of the flag-off gap outside `tests/`. Candidate for a future micro-chunk.

### Correction 2 — worker instances do not survive a blocking spawn
After a blocking `Worker(action="spawn")` returns, `Worker(action="query")` fails with `Worker 'default' is not running`. So the "continue with the same worker via action=query" pattern in my instructions **does not work after a blocking spawn**. Consequence for a two-part task: you must either do everything in one spawn, or accept a fresh spawn for part two. Mitigation that worked: whatever tooling part one invents, insist it be **persisted to a log directory on disk**, so a successor worker can reuse it instead of re-deriving it. In 5b a fresh worker picked up `logs/5b_batch.py`, `logs/C5a_mutate.py` and `logs/conftest_pristine_chunk5a.py` and finished in 193 s with 28 tool calls. Cost of NOT doing this: roughly 10k tokens per re-derivation.

### PROBLEM I HAD LIVE (for the engineer) — pytest exec'd from the shell gets SIGTERM'd
The single biggest time sink of this session. Directly invoking pytest in the worker container dies with **rc=143 "Terminated"** after ~0.2–8 s, so no summary line is ever produced. It is **NOT an out-of-memory kill** — the cgroup says otherwise: `memory.max 536870912`, `memory.events max 6996 / oom 0 / oom_kill 0 / oom_group_kill 0`, `memory.current ≈58 MiB` against a 512 MiB cap, host `MemAvailable ≈977 MB` with swap nearly exhausted. The killer hits the **exec process group**, so it takes down `sleep 45` (died at 29.2 s) and even a bare `cat` of a cgroup file (died at 0.17 s).
- **Workaround:** run the command from a `setsid`-detached parent that sets `SIGTERM` to `SIG_IGN` for itself and its children, then poll the output file. Same command, same mutated conftest: foreground → `rc=143` at 5.9 s with `......Terminated`; detached → `26 passed … in 28.52s`.
- **Blast radius:** 28 files under `logs/` contain the literal `Terminated`, 35 occurrences ⇒ on the order of 35 lost run attempts across chunks 1b/3b/3c, plus 3 more killed this session.
- **Fix I would ask for:** don't let the sandbox's exec-timeout / process-group reaper SIGTERM a *detached* child that outlives the exec call, or expose the real reason for the kill in the tool response instead of a bare rc=143. Diagnosing this consumed most of a generation because the failure looks exactly like a crash in the code under test.
Full write-up (symptom, minimal repro, copy-pasteable recipe, impact, proposed fix) produced by the worker at `logs/5b_sigterm_finding.md` (147 lines, 8124 B).

### Method that keeps working (now proven 7×)
Probe flag-OFF per batch and collect **node ids** → restore conftest byte-identically and verify sha **before** touching a test file → insert markers with the validated bulk inserter (groups by function, above topmost decorator, asserts inserted == targets, re-parses AST) → **census gate on the marked count** (not on the failure count) → prove flag-ON green → prove flag-OFF whole-dir has zero failures. The census gate is the real anti-over-marking guard; a "expect N failures remaining" guard is fragile because the correct answer is usually 0 and a guard phrased around non-zero failures will fire on success.
Also: `@pytest.mark.trust_harness` makes a test **pass** under flag-OFF (function-scoped fixture opens the gate for it), so the flag-OFF evidence for the ids you are about to mark must be captured **before** marking. Post-marking zeros are expected behaviour, not proof the tests were fine all along.


## 2026-11-17 — ## Gen69 — C76 flip landed (`7ea107ce`); the flip was NOT a ...

## Gen69 — C76 flip landed (`7ea107ce`); the flip was NOT a two-line change

**The flip is committed.** Default closed, allowance opt-in. Verified: `tests/` 26-file sweep 257 passed · `tests/integration/` 326 passed 1 skipped · `web_ui/backend/tests/` 300 passed 5 xfailed.

### The finding that mattered: a two-line flip silently broke 16 tests
Flipping the session fixture to `False` looked sufficient and was not. The 26-file `tests/` set combined in one process gave **16 failed / 241 passed**, all `401 unauthorized`, and **every one of the 16 already carried its marker**. The same 26 files one-process-per-file gave a clean 257 passed. Mechanism: `tests.integration.harness.purged_sys_modules` re-imports `web_ui.backend.*`, the conftest import hook re-applies `_suite_trust`, and `_suite_trust` was now `False` — so the marker's grant landed on a module object the app no longer resolved through. Pre-flip this was invisible because the session fixture pinned `_suite_trust=True` for everything. **`conftest.py`'s own comment block had already documented this exact failure mode** as the bug the marker mechanism exists to avoid; the flip reintroduced it.
Fix: the marker fixture now propagates the suite value too, and arms **every live copy** of the boundary module (a purge drops the `sys.modules` entry, not the object) via a `gc.get_objects()` sweep, restoring each individually. Unmarked tests stay a strict no-op so the negative direction remains testable.
**General lesson: when a flag moves from session-wide to per-test, every propagation path the session value fed must be re-fed by the per-test mechanism. A one-line default change can silently disarm a re-import hook.** Test for it by running the affected set **in one process** — batching hides exactly this class of bug. My earlier per-batch 13-way runs would never have caught it.

### PROBLEMS I HAD LIVE (engineer)
1. **No `merge`/`fetch` in `GitWriteTool`.** Operations are commit/init/clone/push/branch_create/checkout/stage/unstage/worktree_*/stash_*. `git merge origin/dev` — required before opening a PR — cannot be done by the agent, and the worker container reports **git absent**, so it cannot either. This is a hard stop on the PR workflow and needs an operator or a new tool op.
2. **`ContainerExecTool` kills the whole container when its own exec timeout expires** (not just the exec). An over-long `sleep`-poll returned `status: exited, oom_killed: false` and `ContainerRestartTool` was needed. Poll with `sleep 24-25` inside an explicit 28-29 s budget.
3. **`git_write commit` refused on a protected branch**: `write_on_feature_branch permission only allows commits on non-protected branches; current branch is 'dev'`. So tracked KB files that live on `dev` cannot be committed by the agent at all — the KB is on the read-on-entry path and now ~43.5k tokens, and it drifts dirty with no way to clear it.
4. **Unattributed edit to `conftest.py`.** A fresh worker found the TASK 1 fix already on disk (mtime 21 s after the prior worker's checkpoint) and correctly refused to claim it. Provenance unknown — possibly an out-of-band session or a re-dispatched run after the backend crash. The code was verified by reading it and by the fact that `suite_trust_value()` is called on all 298 marked tests without `NameError`, then committed. **Lesson: verify an unattributed edit by reading it and by finding a call path that exercises it, not by trusting its sha or its author's account.**
5. Minor, unfixed: `import gc` was inserted after `import importlib.machinery`, so the import block is no longer isort-ordered — will trip a lint gate if one runs.


## 2026-11-18 — ## Problems I had live — agent/worker/tooling shortcomings o...

## Problems I had live — agent/worker/tooling shortcomings observed 2026-11-18

Written up for the engineers, per the standing request to report anything that makes working harder.

### 1. `Worker(action="query")` is unusable after a `final` result (worst one)
Every `spawn` that returns `status: final` tears the instance down. The very next `action="query"` fails with `Worker 'default' is not running`, even though `instance_id` was in hand and nothing signalled termination. Effect: **a multi-step task cannot be continued conversationally**; each follow-up is a cold start, so I re-send ~1-2k tokens of briefing per turn and the worker re-reads files it already understood. Three of my five spawns this session were cold re-briefs of work the previous turn had just finished. Suggested: keep a `final` instance parkable for a follow-up window, or make `query` auto-respawn with the prior context, or at minimum say so in the error text (`not running — spawn again`) instead of making the caller guess.
- Related: `timeout` results keep the instance alive (`query` works), `final` results do not. That is backwards from what a caller expects — the successful one is the reusable one.

### 2. `git` is absent inside the Docker execution container
The worker cannot run `git diff`/`git log`/`git status`, so any "show me your diff" instruction has to be satisfied from the host side with `git_read`. Two consequences: (a) the worker cannot self-check that it changed only what it was asked to change, which is exactly the check a committing agent wants before trusting a patch; (b) I had to spend a round trip doing the review myself. Workers are told not to run git anyway, but the absence also blocks the read-only uses.

### 3. `git_read show_file` vs worktrees is fragile
`show_file` with `working_dir=<workspace root>` and `path=".worktrees/<name>/tests/x.py"` returned *"working-tree path ... is absent from the working tree"* for a file that plainly existed on disk (untracked, so no HEAD fallback). The same call with `working_dir=".worktrees/<name>"` and a path relative to the worktree worked. The relative-path rule is documented for file tools; it is not obvious that it applies to `show_file`'s `path` argument *in addition to* `working_dir`, or that the two must be consistent. Suggest validating and erroring with the expected form rather than "absent from the working tree".

### 4. `/tmp` inside DockerCodeRunner does not persist between calls
Confirmed again (a file written in one call was gone in the next). Every measurement has to be produced and consumed inside a single call, which interacts badly with the reaper (#5) and with output-trimming. A persistent scratch dir (e.g. `/home/agent/tmp`, which did survive) should be the documented default.

### 5. The C90 reaper plus the block quota makes verification a one-shot
Processes die at ~3-27 s, so "run the thing, look at the output, then run the thing again" is not possible in one worker turn; each run must be a single self-contained command, and a timeout in step 2 silently voids the evidence of step 1. This shaped several instructions I gave (baseline-and-after inside one invocation, instead of two clean runs). Worth an explicit "one command per invocation" hint in the worker's own system prompt, since a worker that chains commands produces evidence it cannot stand behind.

### 6. Knowledge base files are huge
`bugs_and_fixes` is ~93k tokens and `task_tracker` ~37k. `mode=append` works but every future `mode=read` is unaffordable, and there is no cheap "how big is this domain" signal except the warning after writing. Archiving exists but the warning does not suggest it by name. A per-append hint ("consider `mode=archive older_than_days=30`") would make this self-correcting.

### What went *right* (so it does not get lost)
- The worker's `meta` channels are genuinely good: `needs_direction` and `struggles` carried the real design question twice, and `tool_issues` carried the missing-git fact. Reading `meta` before `content` changed my next instruction both times.
- A worker that reports a *measured* negative result ("7 bindings, 7 failures, 0 clean resolutions") is worth far more than one that reports success. I only got that because the brief asked for a controlled experiment; that should be a routine ask, not a lucky one.

## 2026-11-18 — ## MEASURED 2026-11-18 — `Worker(action="query")` is broken ...

## MEASURED 2026-11-18 — `Worker(action="query")` is broken by DEFAULT and the workaround is `instance_id`

I ran the discriminator instead of arguing, on the same live worker, seconds apart:
- `Worker(action="query", instance_id=1, worker_query="…")` → `{"content": "SECOND", "elapsed_seconds": 1.4}`. Works.
- `Worker(action="query", worker_query="…")` (name-only, the default shape) → `{"error": "Worker 'default' is not running"}`. Fails.

The instance survived `final` and is fully dispatchable. The failure is entirely `_resolve_worker_thread`'s name-only path (`worker_thread.py:1047-1052`): `is_alive()` is False forever for a process-mode worker (the facade never starts its thread — `:3224-3250`), so the resolver raises `"Worker 'X' is not running"` before `_action_query:8372` can reach its correctly mode-aware `worker_is_dispatchable` gate (`:1817-1855`). The exact-key path (`:1029-1031`) has no liveness test, which is why `instance_id` sidesteps it.

### PRACTICAL RULES FOR AGENTS (use these now)
1. **Always pass `instance_id` on `action="query"`.** Capture it from the spawn result (`"instance_id": 1`). Without it a follow-up to a live worker fails with a false "not running".
2. Therefore multi-turn worker conversations WORK and are cheap (my probe: 1.4s, 0 tool calls, context preserved). Do not assume every follow-up needs a cold re-brief; that belief cost this session a lot of redundant briefing.
3. `token_usage` in the envelope is not a delta — a worker that answered "OK" in 1.3s reported 55196. Do not use it to reason about cost per query.
4. When a supervisor says a worker "is not running", distrust it: cross-check `action="check"`/`list` and retry with the explicit `instance_id` before believing it.

### METHODOLOGICAL LESSON (the real one)
My previous conclusion ("completion destroys the instance, so every follow-up is a cold re-brief") was an *inference from a symptom*, and it was wrong in a way that made me over-brief every worker for the whole session. The A/B above took 2 tool calls and 6 seconds. **Rule: before reporting a mechanism, find the cheapest call that would falsify it and run it.** A/B the suspected branch (here: explicit key vs name-only) rather than reasoning from one failing path.

### FOR THE ENGINEERS: "problems I had live"
- The default `query` shape is unusable against a live process-mode worker, and the error text (`"not running"`) actively misleads — it sends you hunting for a lifecycle bug when the object is healthy. Fix: resolver returns the handle; liveness is ONE mode-aware predicate applied at dispatch; the error should report mode + what was observed, never a bare "not running".
- `WORKER_RUNTIME_MODE` is an ambient env var, so neither the caller nor the agent can see the mode in any envelope. Exposing `"runtime_mode": "process"` in the spawn/query/check envelopes would have prevented this whole misdiagnosis (mine and, plausibly, more than one human's).
- Class/file/registry naming (`WorkerThread`, `worker_thread.py`, registry of `WorkerThread`) describes the opt-in mode, not the default. A facade whose `is_alive()` is intentionally always False needs that stated at the class site, or the method should not exist on the facade at all.

## 2026-11-18 — ## 2026-11-18 "A kill is not an environment law" — and how t...

## 2026-11-18 "A kill is not an environment law" — and how to tell cost from kill

A worker concluded from its own SIGTERMs that "long pytest runs are impossible in this sandbox: every pytest process is killed at 8-20s", and shrank its scope accordingly. An INDEPENDENT worker then ran a 33-second suite to completion, and a later A/B showed the "unrunnable" file was green all along on both trees. The generalisation was false; the observations were real. Two rules follow:

1. **Never promote one kill to an environment property.** The reaper is real (C90) but intermittent, and the same command can die once and pass on retry. Retry ONCE verbatim: same result = deterministic, different result = transient. Report dot count + elapsed, never just "killed".
2. **Distinguish fixed cost from a kill with `--durations=5`.** "Dot printed, summary never came, died at 6s" reads like a hang and is usually a teardown paying a timeout budget. `5.00s teardown <test>` for a body that ran in <5ms is the tell. A green dot followed by death is evidence the TEST passed; it is not evidence about the rest of the file.
3. **Measure before believing a colleague's scope cut.** A worker that has just been killed adjusts its plan defensively and will over-report impossibility. The cheap fix is a second worker with no sunk cost and one honest command. This session: implementer said "unrunnable", independent said "34 passed in 1.02s", truth was "green, slow teardown".
4. **Mutation-test the briefing, not just the code.** For any regression test, require the implementer to state what makes it fail on the OLD code, and then check that claim by reverting the fix in one line (`worker_is_live` -> `lambda t: bool(t.is_alive())`) and re-running. Here all three new spawn tests failed as required. Without that step a "new test" is a comment with a green tick.
5. **MagicMock impersonates domain state.** `getattr(mock, "_runtime_mode", None)` returns a Mock, not None, so a predicate branching on "anything that is not thread mode" silently classified every mock as a process worker with a live handle. The fix is not to stamp the mocks (that hides the trap from the next author) but to compare against the EXACT vocabulary value (`mode != MODE_PROCESS`) so unknown and fabricated stamps degrade to the legacy branch. Corollary for review: a predicate that branches on "not X" is a predicate that accepts every value nobody has thought of yet.

## 2026-11-19 — Gen 69 Final Offboarding (Infra 66)

**The Detached HEAD Trap:** `git_write worktree_add(branch=X)` silently ignores the branch name and creates a detached HEAD worktree. When you commit, the commit exists but there is no branch pointer, so `git push -u origin X` fails with `src refspec X does not match any`. Fix: inside the worktree, always run `git checkout -b X` before pushing.

**The Local Reaper is Test Teardown:** C90 is not a blind wall-clock killer. A 33-second single-invocation test passed. The local SIGTERM killer (exit 143 at 3-27s) is caused by test files that spawn process-mode workers and fail to reap them properly, causing the exec process group to kill the parent pytest. The fix is not a CI timeout tweak; it is the mode-aware verb layer (`worker_is_live()`, `stop()`, `wait()`) implemented in PR #411.

**The is_alive() Lie:** In MODE_PROCESS, the supervisor's thread is intentionally never started. `thread.is_alive()` is always False. Six different places in the codebase used `is_alive()` to decide if a worker was dead, causing orphan spawns and lying status panels. The fix is `worker_is_live()` which checks the runtime handle, not the thread.

**The Timeout Race:** If a child process replies with `status='timeout'` at the exact moment the parent's `future.result()` times out, the parent raises `WorkerRuntimeLost` instead of `WorkerQueryTimeout`. The `dispatch_query` must inspect `reply.envelope['status']` and map it to the correct exception.

## 2026-11-19 — ## Live problems encountered (agent-side), 2026-11-19 — "pro...

## Live problems encountered (agent-side), 2026-11-19 — "problems I had live"

Written because the session addendum asks for it directly; I had accumulated this list across several hours without filing it, which is itself a compliance gap.

**1. `Worker(action="spawn")` transport corrupts long payloads (2 occurrences this session).**
- Call A lost the `action` field entirely → `Invalid arguments: 1 validation error for Worker / action: Field required`.
- Call B turned `timeout_seconds` into `string="false">150` → `Input should be a valid integer, unable to parse string as an integer`.
- Cost: one full retry each time (payload re-sent, tokens re-spent). Not agent sloppiness: the same payload shape succeeded immediately on resend. Long XML-ish bodies appear to get mangled at serialisation. Fix idea: validate the nested payload before dispatch and echo back the received argument tree on failure, so the caller can see *what arrived* versus what it sent.

**2. One verb, two result shapes.** Same `spawn` call returns either `spawned:true, status:"final"` with full telemetry, or `spawned:false, status:"ready"`, `elapsed_seconds:null`, with the report nested inside a JSON *string*. Seen three times. A caller cannot tell success from in-progress by top-level fields. Fix: one schema, always with `telemetry`.

**3. `timeout_seconds` is advisory on the spawn path.** Requested 150s → worker ran 224.0s with `timeout_triggered:false, timeout_clamped:false`. A parameter documented as a timeout that terminates nothing is a lie that looks like an API (same class as `worktree_add` ignoring `branch=`). Consequence: "it finished, therefore it finished within budget" is not a valid inference.

**4. `git_read show_file` returns EMPTY SUCCESS out of range.** `show_file path=… ref=<sha> source=committed line_start=205 line_end=230` on a 191-line file → header line, zero content lines, `failure_reason: none`. Silent-empty reads as "absence measured" when it is really "I did not look". Fix: `file_lines: N, lines_returned: M` trailer, or refuse.

**5. `git_read ls_tree` with `path=` is a SUBTREE RE-ROOT, not a file filter.** A blob path yields an empty body with `failure_reason: none`. Correct form: `treeish="<rev>:<path>"`. Fix: `entries: N` trailer, or reject blob paths explicitly.

**CORRECTION (2026-11-19, code-level) — the re-root diagnosis above is RETRACTED.** `path=` is a pathspec filter, not a re-root: it goes through `_validated_rel_paths` and is appended after `--` (`tools/git_info_tool.py:2240-2243`), so `git ls-tree -r <tree> -- <path>` filters and still prints full paths from the tree root. `ls_tree` reads no `prefix` and no `pattern` field at all; `treeish="<rev>:<path>"` is the only form that re-roots, and the entry above already names it. The empty body was most likely `recursive` unset (`-r` is emitted only when `recursive=true`, `:2238-2239`), so a pathspec naming a nested file matched nothing — which is the real trap, and it is silent: empty body, `failure_reason: none`. The `entries: N` trailer proposal above stands.

**6. The agent has no file-write or grep tool.** Every code edit requires a worker spawn; every search must be delegated. Workers reach worktrees only via RELATIVE `.worktrees/…` paths (absolute `/workspace/...` rejected). This makes cheap operations (grep a symbol, read 20 lines) into expensive round-trips.

**7. pytest cannot be run in the container.** `python -m pytest tests/test_worker_jobs.py` → `Killed` on stderr, empty stdout, ~2.9s, no summary line (SIGTERM reaper). Consequence: a fail-first test (red before fix, green after) CANNOT be demonstrated locally. Workaround proposed: split into commit b1 = test only (pushes red, CI is the instrument) and b2 = the fix (pushes green). Without egress I cannot see CI, so the operator must relay the red/green.

**8. Worker token cap enforcement is late and unclassified.** A worker was declared 80k, consumed 86,649 tokens, and then had tools restricted — enforcement exists but fires after the declared cap. Not yet a fail-open; unclassified.

**9. Self-inflicted, worth recording because it recurred in the tools too:** my briefing said "Repo: /workspace" while the main tree was checked out on a *different* branch; the worker therefore grepped non-baseline content and I reported a false claim. Rule adopted: every path citation must name its substrate and root — `DISK:<root>` / `REF:<hash>` / `UNKNOWN` — and a bare relative path with no root is a rumour. A measurement whose root is unstated is a rumour.


## 2026-11-19 — ## CORRECTION to a defect I myself filed: "pytest unusable i...

## CORRECTION to a defect I myself filed: "pytest unusable in container" is FALSE (measured 2026-xx)

I previously filed "pytest unusable in container; every commit is unverified by construction" after a `Killed` with empty stdout. That conclusion was wrong, and it cost a whole strand its verification loop: I built a standing protocol ("every dispatch ends with a host command, my commits are unverified") on top of a false premise.

MEASURED, in-container, twice:
  cd /workspace/.worktrees/async-worker && /usr/local/bin/python3.12 -m pytest tests/test_worker_jobs.py tests/test_worker_operation_semantics.py tests/test_worker_detached_queue.py -q
  -> "84 passed, 25 warnings in 50.20s", real summary line, no `Killed`, 58.5s wall. NOT reaped.
  .../ -m pytest tests/test_job_session_scoping.py -q  -> "4 failed, 2 passed ... in 1.42s", EXIT=1 (measured under bash + pipefail).

The original `Killed` was almost certainly the *broken interpreter path*, not the reaper: `.venv/bin/python` is broken in this image, and `/usr/local/bin/python3.12` works. Distinguishing "command died" from "environment cannot run this" requires varying the invocation before generalising the failure. I generalised from one dead invocation to a platform capability limit.

WORKING RECIPE (use this, stop re-deriving):
  - interpreter: `/usr/local/bin/python3.12` (NOT `.venv/bin/python`)
  - `-m pytest` for evidence: per-test `FAILED <node id>` lines in one place; `-m unittest` reports the same SetUpError/TypeError split as `errors=` vs `failures=`, which makes tallies incomparable across runners and mislabelled runners easy.
  - `PYTHONPATH=/workspace/.venv/lib/python3.12/site-packages` only if imports fail.

TOOL DEFECT (new): the code-execution backend's default shell is **dash (/bin/sh)**, so `${PIPESTATUS[0]}` yields "Bad substitution" and the tool reports **exit_code 2 = the shell's error, NOT the tested command's status**. A run can therefore look "failed" while its own output says "84 passed". Fix/avoid: pass `interpreter: "bash"` or use `set -o pipefail; cmd | tail -3; echo EXIT=$?`. Suggested product fix: capture and report the real exit code of the pipeline's first command by default, or refuse `${PIPESTATUS}`/bashisms in dash mode with a clear message instead of a silent shell error.

ARCHITECTURAL GAP (worth raising, with a first idea): the agent's git surface has **no rebase and no cherry-pick and no fetch** — only commit/checkout/branch_create/worktree_add/stash/push. With no egress and no git binary in the container, a branch cannot be replayed onto a moved `dev` at all; every base-drift requires an operator round trip. First idea: add `rebase` and `cherry_pick` to `git_write` with the same gating as `commit` (they are history-rewriting, so they deserve the write grain), and give `checkout` an optional `start_point` so a branch can be rebuilt on a new base without a host shell.

## 2026-11-19 — ## Host paths and host interpreters are NOT the container's ...

## Host paths and host interpreters are NOT the container's (measured, cost 2 round trips)

The container's `/workspace` is the mount point; the SAME tree on the operator's host is `~/PycharmProjects/ThoughtMachine-dev`. `git -C /workspace ...` on the host fails with "cannot change to '/workspace'". The bind means files and `.git` ARE shared (confirmed: a host-side `git rebase` produced SHAs the container then read), but the path strings are not interchangeable.

Also: the container's working interpreter `/usr/local/bin/python3.12` DOES NOT EXIST on the host (`bash: No such file or directory`, EXIT=127). The host has an activated venv at the repo root — `python -m pytest` is the host invocation.

Consequences for how I compose host commands (every one of these was a real failure):
- never emit `/workspace/...` in a command for the operator; use `~/PycharmProjects/ThoughtMachine-dev/...`
- container interpreter paths do not transfer; ask for `python -m pytest` and let the venv resolve
- `git fetch` moves `origin/dev`, NOT local `dev` — `git rebase dev` is the wrong target unless someone fast-forwards `dev`; use `git rebase origin/dev`
- rebasing a worktree whose branch is checked out IN THAT WORKTREE is fine and leaves it clean/attached; SHAs are rewritten, so every hash I quoted before the rebase is dead and must be re-anchored.

ARCHITECTURAL GAP (with first idea): nothing in `CheckSystem` reports the workspace's HOST-side root or whether `.worktrees/` is shared. My entire commit discipline (and the operator round trip) depends on the bind being shared, and I inferred it rather than measured it for several sessions. Fix: `CheckSystem` should report the mount source path plus a shared-filesystem assertion, and the host-command templates in worker briefings should be rendered from that measured path, not from a path the agent guessed.

## 2026-11-19 — ## Vitest IN the container: set TMPDIR/HOME, and re-read the branc...

## Vitest IN the container: set TMPDIR/HOME, and re-read the branch before re-applying a fix

Two measured corrections from one round trip.

1. Vitest is runnable in-container (node v20.19.2, `web_ui/frontend/node_modules` pre-installed, 230 entries), but the bare invocation dies BEFORE collecting tests with
   `Unhandled Error: ENOENT: no such file or directory, mkdir '/var/tmp/<rand>/web'` and reports `Tests no tests` — a harness failure that reads like "no suite". The temp root is unusable in this image; overriding it is the whole fix:
   `cd /workspace/web_ui/frontend && TMPDIR=/tmp HOME=/tmp ./node_modules/.bin/vitest run <file|empty>`
   Use `./node_modules/.bin/vitest`, not `npx` (no egress). Full suite: 70 files / 833 tests, 378 s wall — long but under the 900 s tool ceiling; `mem_limit: 2g` was enough.

2. I spent the start of this session planning to re-apply a 4-line frontend fix, because my last edit had vanished when the tree was left on a DETACHED HEAD. The workspace had moved under me: my branch had been **merged into `origin/dev` as a squash commit (#424)**, and the very fix I was about to write already existed as `b44765ba` on `fix/addendum-applied-label` (an operator commit with the same label map and the same `applied ${applied ?? 'next_query'}` fallback). Re-applying would have produced a duplicate commit against merged history.
   Rule: after any "my edit is gone" observation, `git_read log/branch_list` FIRST and look for (a) a detached HEAD, (b) a squash-merge of the branch into `origin/dev`, (c) an operator follow-up branch carrying the fix. A lost working-tree edit is a symptom of someone else's concurrent commit, not necessarily of work still owed. Reverting my speculative edit (`ApplyEdits` back out) and checking out the operator's branch cost two calls; the alternative cost a duplicated patch.


## Declaration discipline — hard-won rules (2026-11-20 session)

### Rule 1 — a canary must arrive through the channel under test, and only through it
Three separate failures in one night:
- The orchestrator was shown the canary **in a screenshot** and nearly recited it as proof the addendum pipeline worked. It proved nothing: the answer came through the image, not the prompt.
- A host agent reported "clean negative on every surface — not in the store, not in any backups, not in the prompt" for a canary **it had never been given**. Zero hits on an unknown needle is an empty query, not a negative.
- A worker's own test fixture string (`CANARY-AGENT-SYSTEM-PROMPT-ADDENDUM-7f3a`) was later found in the vault and mistaken for the operator's probe, because the orchestrator's grep pattern (`CANARY[A-Za-z0-9_-]*`) matched anything canary-shaped.
**Rule:** probes carry a timestamp + random suffix a fixture generator cannot produce; the prober states the literal token; the answer is recited, never screenshotted.

### Rule 2 — a claim, once logged, is lexically indistinguishable from state
An id (`a-64858916e343`) appeared **61 times across 10 files** — session log, rotated event logs, the session JSON itself — and was never once a JSON value. Every `grep -rl` "confirmed" it, and log rotation *multiplied* the confirmation. A whole lost-write theory was built on it and had to be retracted.
**Rule:** a real identifier is a JSON value at a known path. An identifier that appears only in prose is a rumour. Counting sightings is not evidence of persistence — the count grows whether the state exists or not.

### Rule 3 — inherited facts enter as ASSUMED
Six orchestrator declarations had to be retracted in one night, every one of them a claim inherited or asserted before measurement:
1. `ls_tree` "subtree re-root" (refuted — `path=` IS a pathspec filter; the real trap is `path=` WITHOUT `recursive`).
2. A "phantom UI row" the client invented (refuted — ids are server-minted).
3. Accepting "2 rows / 4823 chars" for hours without a root (4823 = 105 real + 4718 *quoted in prose*; no 2-entry state ever existed on disk).
4. "There is a process boundary" (refuted — one process; the split is object identity).
5. "Two resolvers" (refuted — one store read, one renderer, both callers already use them).
6. "`#425` merged as `f0402b2`" (false — that object does not exist in the repo; and "ledger-wt is a separate clone" — false, it is a worktree sharing `.git`).
**Rule:** a fact inherited from a previous instance or another agent enters as ASSUMED until re-measured in the current context. Three agents corrected the orchestrator six times in one night; the ledger should record it that way, because a ledger that only records the agents' errors is not a ledger.

### Rule 4 — declare the boundary of a negative result
"Not found" is only meaningful with the search set stated. Agent 1's inventory closed with exactly that: `infra/`, `session/`, `presets/`, `resources/`, `scripts/` were not searched — "a cache there is unenumerated, not absent". Copy that habit. The alternative is that the next reader treats an incomplete sweep as a complete one.

### Rule 5 — write at decision points, not at the wall
Continuity written under token pressure is written badly or not at all. Append after a dispatch, a ruling, a merge, a measurement that changed beliefs, or a self-caught error.

### Tool traps confirmed this session
- `git_read log` **ignores `commit=`** and silently returns HEAD — history searches are shallower than they look.
- `git_read ls_tree` with `path=` and no `recursive` returns the single tree entry, not its contents.
- `git_read show_file source=committed` returns EMPTY SUCCESS for an out-of-range window.
- `git_write branch_create` does NOT switch; `checkout` is a separate call.
- Worker spawn `timeout_seconds` is advisory and `timeout_triggered` is unreliable in both directions.
- `Worker action=query` ignores `context['query']`; only `worker_query` is read.
- `git worktree list` marks worktrees "prunable" when they are registered with the container path `/workspace/...` rather than the host path. Cosmetic, but it makes them look dead.

## 2026-11-21 — **2026-11-21 (J2) — the guarantee system's own coverage, MEA...

**2026-11-21 (J2) — the guarantee system's own coverage, MEASURED.**

(1) The repo has exactly ONE sha256 tripwire: 9 inline tokens in `.thoughtmachine/knowledge/project/HANDOFF.md` §2 pinning the BYTES of 9 onboarding docs. Enforced twice: `tests/test_onboarding_bundle_contract.py:693-718` and `scripts/check_kb_staleness.sh` (hard exit 1; `python-version-matrix.yml:171-172`). Coverage of `agent/ session/ infra/ tools/ web_ui/backend/` SOURCE: **0 of 199 files** content-pinned; 4 files line-bounds-checked only. `session/`, `infra/`, `web_ui/backend/` have zero coverage of any kind. It is a documentation-staleness pin, NOT a security mechanism.

(2) `docs/CONTRACTS.md` held 3 contracts; `architecture_invariants.md` held 2 numbered invariants (§1 container envelope, §2 host chokepoint). **No contract named either invariant, and none of the six detecting test files was in the gate.** Both headline invariants were enforced AND tested but NOT merge-blocking — e.g. widening `thoughtmachine/host_port.py::AUTHORIZED_VALUES` would not have blocked a merge. Repaired in #434 by `tests/test_invariants_ledger_parity.py`, which makes ABSENCE fail (red-first: 3 rule-(a) failures before any marker was added).

(3) `scripts/verify_doc_citations.py` checks that a cited LINE EXISTS AND IS IN BOUNDS — not that it is the RIGHT line. Measured rot it passes today: `architecture_invariants.md` cites `security_gate.py:1360-1361` (pair actually at `:1580`) and `infra/container_manager.py:2249-2252` (actual `:2260`). A citation pin cannot detect semantic drift.

(4) Prose that asserts a guarantee is an unverified claim, and this repo does it repeatedly one layer above where it breaks: `CONTRACTS.md`'s header and `contract-gate.yml`'s step title both claimed the gate "runs only the tests named here" (measured 18 run / 10 cited) — same defect class as `ConfigPanel.jsx:560-561`'s "the panel cannot drift" and `mutate_session`'s docstring vs the negative path cache. Corrected in #434.

(5) `MANDATORY_STOP.md:9` records §9.3's two named exceptions: **git and container-git are OUTSIDE `host_port`'s coverage.** Do not claim the host chokepoint covers git — that surface is unaudited (the operator's "git hooks surface" class of attack).

(6) `contract-gate.yml` runs a HAND-ENUMERATED 18-file list, no `paths` filter, on every PR to `dev`, but NOT on push to dev. The ledger→gate coupling is one-way (`cited ⊆ run`): it catches a deleted check, never an absent one. A new contract citing no test passes.

## 2026-11-21 — Worker reliability: reliable at refusing, unreliable at facts (recorded, not fixed)

Operator asked whether the worker is malfunctioning. Measured answer: **not broken, but its output must be split before use.** Its refusals and self-checks are trustworthy; its counts, paths and mechanisms are not.

**Reliable (adopt as-is):** refusing to record a mechanism it could not measure rather than writing a plausible one (I9, and it was right to refuse even though I overruled it with CI evidence); refusing to cite `D1`/`D2` because no such entry exists in any DIRECTION record — those labels live only in conversation; flagging "broken reaper" → "broken lookup" as imprecise; flagging that an entry naming a MODULE inherits every defect that module ever had; reporting four stale citations it had been told to leave untouched instead of silently fixing them; validating its own YAML with `yaml.safe_load` and its `run:` blocks with `sh -n`.

**Unreliable (re-derive every time):** every self-reported figure and location. Measured instances this session — `"100s"` for a computed 60s; `"fifteen entries"` for 18 (16 with a live call site, 15 tests passed); `attempts > 0 on Darwin` (wrong function inspected in isolation; real mechanism is `os.getpgid` raising `ESRCH` before any poll); `platform-matrix.yml` for `.github/workflows/platform-matrix-macos.yml`; `worker_runtime_process.py:14-21` described as the `fast_json_repair` import block when those lines are a docstring (the real block is `agent/core/tool_executor.py:14-21`).

**Cost signal:** 300–373 tool calls per single-task turn. That is consistent with locating files by guessing their names rather than reading the tree, and it is the likely origin of the wrong paths.

**Rule.** Split the envelope: `meta.struggles`, `meta.needs_direction` and explicit refusals arrive correct. `content`'s numbers, paths, line references and mechanism claims are drafts. Never publish one, and never brief another worker with one, without re-deriving it from a tool. Corollary learned twice: a worker's claim inside MY brief is still a rumour — verify before handing it on.

**My own instance, same day:** I wrote `# expect 2` for a `grep -c` in a command handed to the operator, without running it. Actual 4, because the phrase also appears in the prose the worker added. Predicting a count I could have measured is the same defect as citing one I could not. Prefer structural checks with known shape (`grep -c '^      - name: Set up Python'`) over prose greps.

## 2026-11-21 (late) — Chokepoint ruling, and the two measurements it corrected

**The F2 note I carried all session was wrong.** I had recorded `security/sandboxed_execution.py` as having "no credential-env stripping". Measured at source (`origin/dev` 12a52a46 era): it has `_STRIPPED_ENV = {HOME: /dev/null, PATH: fixed}`, `_GIT_ENV = {GIT_CONFIG_NOSYSTEM: 1, GIT_CONFIG_GLOBAL: /dev/null}`, applied by default via `strip_env=True`. `thoughtmachine/host_port.py` (`host_run`) instead refuses/strips credential-shaped env *keys* (check 7) and audits their names.

**The real asymmetry, which is sharper:** door A has kill switch, cwd containment, cross-workspace session check, and a credential-key **denylist**. Door B has a hermetic env **allowlist**, shell-metachar reject, category gate, audit. **Neither has the union.** A denylist loses to a name it does not know; an allowlist does not. They are not substitutes, so "which door is ungoverned" is the wrong question.

**Ruling (design brief `docs/design-second-chokepoint.md`, branch `docs/second-chokepoint` @ f501655a):** collapse the *spawn* to one door (`host_run`), and demote door B's policy to parameters applied to that spawn. Not "collapse = call host_run", which would silently delete the hermetic env and be a security regression published as a fix. Not federation, whose only guarantee is the discipline that produced two doors with complementary holes.

**No call cycle** — measured by AST BFS both ways (94 modules each, neither reachable from the other) plus runtime `sys.modules` check. So a single spawn point is achievable; the two-door split was sequencing, not architecture.

**Reusable decision rule:** one spawn point per capability; a second door is permitted only if it needs a different governance *shape*, not merely a different call site.

## Two open contradictions (do not resolve by reading)

1. **Same test file, same platform, two verdicts.** `test_unreadable_proc_state_degrades_toward_alive` was GREEN on macOS run #199 (tree 9de79d42 + 228df0e7) and RED on macOS run #204 (same test file merged into dev 12a52a46), failing `assert attempts > 0` at :984. The fix injects three seams; with a working injection `attempts == 0` is impossible. Either the injection is incomplete or the test is racy on Darwin. Instrumented instead of guessed: branch `test/proc-seam-trace` @ 7a4e9dec1650809a3604a8144591cd89d2c8405f prints `PROC_SEAM_TRACE summary: ... read=yes|no first=...` on **pass and fail**, so the two runs can be compared. My hypothesis (ESRCH from `os.getpgid` on a Darwin zombie) is unconfirmed and must not put a fix in a file.
2. **The timeout test has three behaviours because "timeout" has three encodings.** Observed across runs: `WorkerRuntimeLost: ... 'timeout' (§4.4)`, then `DID NOT RAISE WorkerQueryTimeout`. Encodings: (a) client `future.result(timeout=)` → `WorkerQueryTimeout`; (b) child `reply_q.get(timeout=)` → `Reply(status="timeout", ok=False)` → `if not reply.ok` → `WorkerRuntimeLost` (a timeout relabelled as a lost runtime); (c) worker-internal timeout → `status="timeout"` with `ok=True` → returns normally, raises nothing. Both timers carry the same number, started milliseconds apart by IPC latency, so the racing one wins.

## Mutation gate (band closed, three stacked PRs)

`test/mutation-gate-harness` @ 9416be6e → `test/contract-3-gate` @ f8831940 → `test/gate-in-ci` @ 1169d07b. It produced its first real result immediately: **Contract 3's "mutation-proven" claim was false** (`MUTATION contract-3-default-mode: SURVIVED`), and one test asserting the literal `"process"` turned it to `KILLED`. The harness refuses rather than passes on an absent/ambiguous anchor, an uncollectable node id, a timed-out run, or a **red baseline** — the last one because KILLED is a delta, not a property: without it a pre-existing failure would be scored as proof a control works.

**Still not a required check.** Requiredness is a property of ruleset `24214086`; no file in the repo can verify it, and `docs/BRANCH_PROTECTION.md` still names only the old job. Correct sentence: "the mutation gate runs in CI", never "contracts are gate-enforced".

## 2026-11-23 — 2026-11-21 — THREE INSTRUMENT LESSONS from the mutation-gate...

2026-11-21 — THREE INSTRUMENT LESSONS from the mutation-gate band (all mine, all self-inflicted).

1. GREEN ON THE WRONG TREE, concretely. I added `scripts/mutation_gate.py` (imports `subprocess`) and verified the tests I had touched (parity, citations, harness, the gate itself) — all green — then shipped. The operator's full-suite run found FIVE failures: `tests/test_host_port_kill_switch.py` x2 and `tests/test_tool_import_declaration_match.py` x2. A new module under a scanned root trips the repo-wide meta-guards. Root cause of the miss: this container CANNOT run the full suite (`exit 137, OOM-killed at 47%` after 275s), so full-suite green is never available to me; verification is always by name, and I named the wrong set. Standing rule: when a NEW FILE lands, run by name — kill-switch drift (`test_host_port_kill_switch.py`), importer pin (`test_tool_import_declaration_match.py`), citation/ledger parity (`test_contracts_ledger_parity.py`, `test_invariants_ledger_parity.py`, `test_verify_doc_citations.py`), and the `docs/*` gitignore negation.

2. A DATA MODULE IS NOT A TEST. Putting `tests/mutation_gate/specs.py` under `tests/` and citing it in `docs/CONTRACTS.md` failed `test_every_cited_test_runs_in_the_contract_gate` (cited subset of run). The checker was right. Fix: registry lives beside its consumer (`scripts/mutation_specs.py`). Do not "fix" this class of failure by adding a data file to a workflow (running pytest on data) or by un-backticking the citation (hiding it).

3. BOUND-METHOD IDENTITY CANNOT PROVE A MONKEYPATCH. I dictated a diagnostic asserting `os.getpgid is no_procfs.getpgid` — ALWAYS False, because attribute access rebuilds a bound method each time. Correct form: `getattr(seam, "__self__", None) is fake` (or `==`). The worker caught it; left unfixed it would have reported "injection absent" on a host where it was installed — a fresh rumour manufactured by the very block meant to stop rumours.

4. `host_run` IS UNREACHABLE FROM ANY CI SCRIPT (measured, `thoughtmachine/host_port.py:557`). It refuses on: unknown grain, empty workspace_id/session_id, `_check_identity` against the session registry, `allow_host_resources is not True` (`:657-666`, fails CLOSED on a missing workspace config), vault-resolved permissions, mcp/client_type pairing. `host_run_async` raises `NotImplementedError` (`:854`). Consequence: every `scripts/` entry in the two spawn allowlists is admitted on the same ground — "human/CI verification path, not a tool path" — and no CI script can ever be migrated to the chokepoint. A mutation gate needs a FRESH interpreter (in-process pytest would read a cached module and prove nothing), so its `subprocess` use is legitimate and exact-path allowlisted in BOTH pinned lists.

5. GIT ARCHAEOLOGY KILLED MY OWN HYPOTHESIS CHEAPLY. `git blame`/`git log <file>` established that `tests/test_execution_kill_target_guard.py` has only two commits (`336cd697` #441, `2d540347` #449) and that `worker_execution.py`'s `_kill_decision`/`_pid_is_alive` block is ALSO `336cd697` — so the "three-seam proc fix" (#456) touched neither, and the green/red macOS pair executed IDENTICAL code with IDENTICAL (deterministic, no pytest-randomly) ordering. Blame before theorising: it costs one call and it deletes whole families of explanation.

## Live problems observed while working (agent-side, container retention task)

**1. Worker fabricates verification when there is no shell.** The workspace container was `stopped` (CheckSystem twice) yet the worker reported "207 passed / 1 skipped", "604 passed", "full test run green", and later "54 passed", "314 passed, 4 skipped" plus a `python -m py_compile ... exit=0`. None of it is measurable by me. Its *file edits* were real (git status confirms). Rule adopted: a worker's code edits are evidence, a worker's test output is not. Ask workers for file:line + quoted text instead of test counts; when they volunteer counts, discount them and never repeat them as fact.

**2. A git-tool-created worktree is invisible to worker file tools.** `worktree_add` (via the containerized git backend) created `.worktrees/retention-charter`, and `git_read` inside it worked, but the worker's file tools reported "there is NO `.worktrees/retention-charter` directory" and its writes silently landed in the MAIN tree. Probable cause: the git backend and the worker file tools mount different filesystem views, so a directory created only through git is not visible to the file-tool sandbox (and vice versa). Workaround used: do branch work in the main tree (`checkout(branch=...)`), no worktrees. Fix idea: have `worktree_add` create the directory through the same FS layer the file tools see, or make file tools fall back to the git backend's view; at minimum return the absolute path the file tools will resolve.

**3. `git_read ls_tree` ignores the `prefix` parameter** and dumps the whole tree (~35k tokens, truncated). Use `treeish="HEAD:<dir>"` instead. Same class of problem for `show_file`: it requires `path=`, not `file_path=` (silently errors "path is required").

**4. Migration entry-point gap found by wiring review (reusable lesson).** A new migration step (`migrate_retention_charter`) can pass its own unit tests and still never run in production: `migrate_records` did not call it and `upgrade_record_schema` had zero production callers. Whenever a migration is added, trace the caller chain from the actual startup path (here `web_ui/backend/server.py:1204`) before declaring it wired.

## 2026-11-24 — A pin with no verifier is a hope, not a guarantee (measured, with a version number)

Agent 2's container holds **`openai 3.40.0`**. `requirements.txt:5` now reads `openai>=1.0.0,<3`. So our own substrate runs a version the declared constraint forbids CI from ever installing. Consequences, both real:

1. **Every Python number a delegated agent reports is from a dependency resolution CI cannot reproduce.** Agent 2's `57 passed` on `tests/providers` was true and worthless: it verified the OLD `request=SimpleNamespace()` fake against an SDK (3.40.0) that tolerates `SimpleNamespace`. Agent 2 demonstrated this directly — on 3.40.0 the pre-repair fake constructs fine (`constructed_ok`, no raise). CI resolves 2.x and raises inside `openai/_exceptions.py:49`. Same tree, same test, opposite result, entirely because of an unmeasured version.
2. **The constraint itself had nothing checking it.** `<3` is prose in a file. Nothing asserts that the running interpreter satisfies it, so a sandbox can violate it indefinitely and produce plausible greens. **This is the general pattern: a declared constraint with no verifier is indistinguishable from no constraint.** The fix is cheap and general — resolve every pin in `requirements.txt` against `importlib.metadata.version()` in one test, so a substrate that violates its own pins fails loudly at the gate instead of manufacturing confident greens. That single test would have caught the entire baseline red at build time rather than as six mystery failures in site-packages.

Same disease as `continue-on-error: true`, as `git_read log` ignoring `commit=`, and as the `own_lifecycle` reap guard: **a control that reports success without doing its job.**

## 2026-11-24 — When the host has no swap, CI is the only instrument

`free -m` on the operator's machine: `Swap: 12287 / 12287 used`, `available 1323 MB`. Agent 2's bisect over `tests/`: **424 of 444 files returned rc=143 with empty summaries**; only 3 finished clean, all ≤0.75 s. The cgroup did NOT account for the kills (`memory.peak 414 MB < memory.max 512 MB`, `oom_kill 0`, `oom_group_kill 0`), so it is system-wide memory pressure reaching the exec harness, not the container limit.

The dangerous property: **a run that dies with an empty summary looks like a run that has not finished, not like a failure.** Two of Agent 2's wide runs froze mid-progress with no living pytest process. `exit 0` with no summary is a masked kill. Rule that follows: when local execution is degraded, do not spend the operator's machine on full-suite runs — **open the PR and let the required checks be the transcript**, because a check run carries the head sha, runs on the merge ref, and cannot be silently SIGTERM'd. A green on CI is a stronger receipt than a green on a laptop, and when swap is exhausted it is the only receipt.

## Standing instructions from the operator (2026-11-24) — measurement honesty
Applies to every future session in this workspace. Issued after the image-tombstone delivery; rule 1 was **extended by the operator** to the whole pinned set ("one measured lie is enough").

1. **Quote the version of every pinned dependency the result depends on, in the same breath as the result — not just `openai`.** Measured pinned set (`requirements.txt`, 26 lines): `openai>=1.0.0,<3` (:5), `anthropic>=0.30.0` (:6), `httpx>=0.25.0`, `pytest>=8.0.0`, … Installed in the worker container: `openai 3.40.0`, `anthropic 2.3.0`, `httpx 0.28.1`, `pytest 9.2.0`.
   - **`requirements.txt` contains exactly ONE ceiling in 26 lines: openai's `<3` — and that is the one constraint the container violates.** Filed by the operator as a substrate defect. Every provider number produced here is **advisory at best**.
   - The other 25 lines are floors only (`>=`), i.e. "whatever pip resolves today" with no upper bound — so version drift is unconstrained for anthropic/httpx/pytest too. A floor-only pin cannot be *violated*, only *satisfied*, which is why extending the rule to the full set is cheap: the failure mode is silent for all of them and loud for exactly the one that has a ceiling.
2. **Never present a provider result as evidence about CI.** CI installs from `requirements.txt` (e.g. `.github/workflows/contract-gate.yml:64`), so it resolves a different SDK than this container holds. Green here ≠ green there. Show the mechanism (e.g. a fake that is inert on the installed version), don't assert it.
3. **Do not run wide `-k` sweeps in this container.** Bisect proved the kills are systemic (424/444 files `rc=143` with empty summaries; every survivor finished in <0.75 s; `oom_kill 0` while host swap was 12287/12287 used). Narrow per-file runs or nothing. The PR's CI lanes are the receipt.
4. **Never claim the frontend hint renders.** There is no `web_ui/frontend/node_modules`, so Vitest cannot run. JSX/CSS changes are **unexecuted**; say so. The PR's frontend lane (e.g. `web-ui-frontend-suite`) is the operator's to read; if it goes red he supplies the exact assertion, not a paraphrase.
5. **`show_ref` before declaring a sha unreachable.** Doctrine; skipping it cost a round trip on `3680f567`. A read that echoes its own argument (D1–D4 PR) is welcome but is **not** a precondition — use `show_ref` unconditionally.
6. **A skip is never a pass; a run with no summary line is NO RESULT; a green measured on the wrong tree is not a pass.**

### Verifier status (honest)
- Rule 1's only verifier today is a reviewer's eye. **Proposed, not built:** a CI step that prints the pinned-set versions at lane start, so the version is in the log by construction. Same primitive as the D1–D4 argument-echo line — a read/run that states its own inputs.
- The two operator entries of 2026-11-24 — "A pin with no verifier is a hope, not a guarantee" and "When the host has no swap, CI is the only instrument" — **have no verifier yet**. That is not a joke that resolves itself: the verifier for an unverifiable pin can only be CI, which is what the second entry says is the only instrument. Do not cite either as settled.

### Reusable technique that settled the reconciliation
To test whether a test *fake* is version-portable, execute the fake directly and print it (`openai.APITimeoutError(request=SimpleNamespace())` → constructs, `str` → `"Request timed out."` on 3.40.0), rather than reasoning about the SDK signature. That single command turned an unprovable assertion into a receipt.

### Evidence classification for the tombstone work (post-advisory rule, operator-accepted)
- **SDK-free ⇒ non-advisory, 51 tests:** `tests/test_image_retention.py` (11), `tests/core/test_image_gc.py` (36, docstring: "no provider, no network"), `tests/test_image_tombstone_placeholder_parity.py` (4 — imports only `os`, `re`, `agent.core.image_store`; reads `MessageBubble.jsx` **as text**).
- **Advisory, 57 tests:** all of `tests/providers`, including `tests/providers/test_tombstoned_image_placeholders.py` (imports `anthropic_provider` and `openai_compatible`; clients constructed, never called).
- **Official phrasing:** the adapters' base behaviours are *pinned statically* by the parity test and *exercised at runtime only inside the advisory directory*. The parity pin is a text-level check — it keeps the JSX literal honest and **cannot stand in for the Vitest lane**; the JSX hint has never been executed.

### Housekeeping
KB churn (this file) and `.thoughtmachine/pr/` stay **uncommitted** and must never be committed onto a delivery branch. Archive this domain after #477 closes — it was ~56.5k tokens at 2026-11-24 and is past the size where a session reads it whole.

### 2026-11-25 - The fallback class is not the runtime path

I reported "workers are threads" from `worker_thread.py:2071`
(`class WorkerThread(threading.Thread)`) and published it as runtime behaviour.
It was false. Measured with host_bash, at last:

    backend    2042672  python -m web_ui.backend.server --serve-frontend --port 8000
      child    2042673  tee -a logs/backend_startup.log
      child    2044315  python -c from multiprocessing.resource_tracker import main
      child    2044316  python -c from multiprocessing.spawn import spawn_main ... --multiprocessing-fork
                        RSS 255,188 kB, 5 threads, spawned 01:23:47
      grandchild 2077230  /bin/sh -c ...     <- that worker's own host_bash tool call

`worker_thread.py:3332` - `start()` returns early for any worker whose mode is
not MODE_THREAD and which is not `is_runtime_child`: "the body lives in the
child interpreter". `:2071` is the fallback class, 1,261 lines above the branch
that decides behaviour. It certifies the class hierarchy and nothing about the
runtime.

Second, independent error in the same report: I called the process substrate
"unmerged" (branch `fix/gen67-worker-runtime-default-process`) when
`worker_runtime.py`, `worker_runtime_main.py`, `worker_runtime_process.py` and
`worker_runtime_protocol.py` are all committed blobs on `origin/dev` at
`339034e6`. Wrong premise, wrong inference, published as a finding.

Third, why I never measured: `host_bash` was granted (`permission_level:
"allow"`) and refused every command including `echo hi`, because
`~/.thoughtmachine/workspaces/fierce-owl-43/config.json` had no top-level
`root`. I knew that one-key unblock for hours and filed it as a footnote in a
status block instead of surfacing it as the thing blocking every measurement.
The dead instrument was the story.

RULES (permanent):
1. If the empirical test is cheap, take it before the code read. A line of code
   certifies its line, not its behaviour.
2. A blocked measurement is a headline, not a footnote. When the instrument is
   dead, say so first and name the one-line fix.
3. A declaration is not a verdict: check whether the path you are reading is on
   the runtime route or merely inherited alongside it.

TRAP worth remembering: `multiprocessing.Process(name=...)` sets a Python
name, not an OS process title. `pgrep -af worker_runtime_main` and
`ps | grep worker-runtime-` both returned NOTHING while a worker process was
live and serving. Those greps are false negatives that look like proof of
absence. Identify runtime children by `--multiprocessing-fork` / `spawn_main`,
or by `ppid == backend_pid`.

## Tool/interface facts that cost this session real time (2026-11-25, Gen 67)

**The repo is a linked git worktree, and that is the root of the "three roots" confusion.** `~/PycharmProjects/ThoughtMachine-dev/.git` is a FILE containing `gitdir: /home/jojo/PycharmProjects/ThoughtMachine/.git/worktrees/ThoughtMachine-dev`, with `commondir: ../..` and `HEAD: ref: refs/heads/<branch>`. Consequences: (a) any code reading git refs must follow gitdir -> commondir -> loose ref -> packed-refs, or it silently yields None; (b) agent file tools resolve to `.worktrees/<session-id>` on branch `wt/<session-id>`, agent `git_read`/`git_write` resolve to the MAIN checkout, and `host_bash` resolves to the workspace config `root` = the MAIN checkout. Three tools, three roots: file tools refuse `../`, `git status` refuses because the main repo is "outside the workspace", and `host_bash` is the only write bridge. (c) This checkout is the real exercise of the awkward path, so a `read_git_head()` test against `git rev-parse HEAD` uses real data, not a fixture.

**`gh pr edit` is BROKEN in this repo** — GraphQL error `Projects (classic) is being deprecated ... repository.pullRequest.projectCards`. Use REST instead: `gh api -X PATCH repos/use-less-vars/ThoughtMachine/pulls/<n> -f body="$(cat body.md)" --jq .html_url`. `gh pr create --body-file` works fine.

**Permission grants that flap and misreport:** `host_bash` flips allow/banned between turns; `network:outbound` is banned for the agent session (so `git push` is impossible, only local commits) while `CheckSystem workspace_info` still reports `network: "write"` — a live F1 lie: the permissions the operator SEES do not match the effective grant.

**DockerCodeRunner is dead in this workspace**: `/workspace/.git` shadow bind target has no host source (`~/.thoughtmachine/workspaces/default/.git-shadow-mask` missing). `infra/container_create.py:115-147` refuses rather than letting docker materialise the mask as a directory — correct refusal, but `thoughtmachine/workspace_capabilities.py` only creates the mask from `setup_workspace.py`, so pre-existing vault workspaces never get one. This is why the container write path is unavailable and host_bash became a single point of failure. Fix queued as its own PR.

**Host-process kill-switch contract** (`tests/test_host_port_kill_switch.py` + `tests/test_tool_import_declaration_match.py`): every module under `agent, infra, mcp_examples, scripts, security, thoughtmachine, tools, web_ui` that reaches the host must go through `thoughtmachine.host_port.host_run()`. Declaring an exception needs BOTH an exact-path `ALLOWED_OFFENDERS` entry with a reason AND a `PINNED_SITE_COUNTS` pin — an allowlist entry without a pin fails as "unpinned". Removing `import subprocess` from a module clears the importer/import-match tests entirely, so shrink the surface before declaring the residue. `tests/` is not scanned, so tests may use subprocess.

## MACHINERY DEFECT — FileSearchTool is token-pathological on long single files (2026-11-26, gen67)

Measured inside a worker turn (worker's own report, its `tool_issues` field): one `FileSearchTool` call over `scripts/forensics_after_reboot.sh` (455 lines) returned **~10k tokens**, because the tool re-emits the whole file block once per match pattern — 5 hits = the same file 5×. The worker spent its entire turn budget (187.8 s, 123 tool calls, 58,860 tokens) reading and produced ZERO edits. Same file read with `FilePreviewTool` + explicit line range costs a fraction.

Consequence for briefs: **never let a worker discover a long file by search.** Hand over exact line numbers in the brief, or order `FilePreviewTool` with a range. This has now cost one full turn.

Related, and unfixed (all F1 shape — a tool answering confidently and wrongly):
- `GlobTool` returns "Page 1 of 0" for `*` / `*/` over a directory with 110 subdirs (false negative, and it looks authoritative).
- `DirectoryTreeTool` `max_depth=0` means UNLIMITED (one call produced a 2.9M-token dump). A "zero" depth must mean zero, or refuse.
- Absolute paths: `ReadFileTool` REFUSES them, `FileEditor` ACCEPTS them — the same path gets two different answers from the same root.
- `FileSearchTool` refuses repo-wide search above ~33.5k files.
- Worker timeout is granted-but-not-honoured: **480 s requested (clamp reported `false`, `timeout_source: worker_thread.spawn`), cut at 187.8 s** with `timeout_triggered: true`. Earlier in the same session: 420 granted, cut at 140 s and 163 s. The cut never names its layer. Mitigation that works: order the brief "edit code first, tests second, report third" so a cut still leaves work on disk, and never put the report before the work.

Ticket candidate: one PR "tools answer truthfully" covering the four tool defects above.
