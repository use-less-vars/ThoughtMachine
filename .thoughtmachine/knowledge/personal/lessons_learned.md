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

