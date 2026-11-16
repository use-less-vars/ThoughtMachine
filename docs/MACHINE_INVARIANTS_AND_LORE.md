# Machine invariants and lore — the hard core

## 1. How to read this document

This is the slowly-changing layer: the invariants that must stay true, the failure modes they
prevent, and the traps that have already caught someone. Not a feature tour, not a changelog. Every
non-obvious sentence carries a `file:line`, and the citations are the point — documentation here has
repeatedly drifted from code, including documentation whose whole job was to be the map. The pass
that wrote this file caught the pattern in the act: `docs/ARCHITECTURE_MACHINE_MODEL.md`, the most
trustworthy doc in the tree, had three code facts right and all four of its coordinates wrong —
`DEFAULT_RUNTIME_MODE` cited at `tools/workspace/worker_runtime.py:83` instead of `:84`, the
production spawn site cited as `worker_runtime.py:1277-1281` instead of `:674` (that range is inside
`WorkerRuntimeHandle.cancel`), `class WorkerThread(threading.Thread)` cited at `:1630` instead of
`tools/workspace/worker_thread.py:1880`, and a bare `line 72` instead of 73. All four were fixed in
the same pass, and so were five design docs that had gone on asserting the thread world (§10). The
claims survived; the coordinates had rotted. Treat every line number here, including the ones in
this file, as a hint to grep, not as truth.

**The rule: when this document and the code disagree, the code wins and this document is a bug.**
Fix the document in the same change.

Two labels are used deliberately:

- **Verified** — cited to a line that was read in this checkout.
- **Reported history (not reconstructable from the code)** — a story consistent with the code and
  possibly true, but which no artefact in the tree substantiates. Do not build on it. Where a claim
  could be neither verified nor refuted it lives in section 11, not in the body.

## 2. The process boundary is sacred

The single largest structural fact about this machine: **a worker is an OS process, not a thread.**
An absent `WORKER_RUNTIME_MODE` resolves to `process`.

| Fact | Cite |
|---|---|
| Flag name `WORKER_RUNTIME_MODE` | `tools/workspace/worker_runtime.py:67` |
| `MODE_THREAD` / `MODE_PROCESS` | `worker_runtime.py:70` / `:73` |
| `RUNTIME_MODES` — "Nothing else is a mode" | `worker_runtime.py:76` |
| `DEFAULT_RUNTIME_MODE = MODE_PROCESS` | `worker_runtime.py:90` |
| Resolution, not memoised; a typo raises | `worker_runtime.py:357` |
| Production spawn resolves the mode here | `worker_runtime.py:674` |
| `class WorkerThread(threading.Thread)` — now only the in-process body | `worker_thread.py:1880` |

Thread mode is now opt-in (`WORKER_RUNTIME_MODE=thread`, or an explicit `mode=MODE_THREAD`) and is
documented as such at `worker_runtime.py:78-83`. Do not read the class declaration as evidence of
the deployment shape; the class survived the migration, the process did not go away. What actually
differs, and what an engineer must internalise:

1. **Shared memory is gone.** The in-process registry stays — one shared in-memory registry keyed
   `(session_key, name, iid)` — but the worker's own state, its event bus and its turn live in
   another address space.
2. **Every event crosses the boundary through the IPC outbox seam**
   (`tools/workspace/worker_runtime_main.py:333-341`). The child constructs
   `_Outbox(sock, FrameEmitter(...))` **before** `worker.start()`, for a stated reason: *"an outbox
   built after `start` returned would drop — or, worse, reorder against a later reply — the first
   events of this incarnation"* (`:329-333`). The sink is one assignment,
   `worker._frame_sink = outbox.put` (`:349-350`), chosen because `put` never blocks, so a heartbeat
   tick never waits on the socket.
3. **The ordering guarantee** is "one writer, `seq` owned in one place" — the outbox is this
   process's only writer and the fence is stamped by the emitter (`worker_runtime_main.py:344-350`).
   If you add a second writer you have destroyed the guarantee, not extended it.
4. **The hazard that seam exists to defeat:** events emitted before the parent is subscribed. It is
   handled on *both* sides — the child subscribes the global bus before `worker.start()` (*"Called
   BEFORE `worker.start()` so the `worker_spawned` that `start` publishes is not missed"*,
   `worker_runtime_main.py:232-235`), and the parent replays a parked `WORKER_SPAWNED` when a
   bridge's session id arrives late (section 3).

### Per-worker event buses, and the live failure mode

Each worker creates its own `EventBus` inside `run()` and registers it —
`self._event_bus = EventBus()` then `register_worker_event_bus(..., epoch=self._generation)`
(`tools/workspace/worker_thread.py:4513-4514`; facade at `:938`; registry method at
`tools/workspace/worker_registry.py:345`). Discovery is `get_event_buses_for_session`
(`worker_registry.py:409-427`), whose docstring names its purpose: *"Used by late-arriving bridges
to discover already-running workers whose WORKER_SPAWNED event was published before the bridge
subscribed."* Recovery exists in two shapes: `_discover_existing_workers`
(`web_ui/backend/bridge.py:935-975`, called at subscribe time from `:862-866`) and the
parked-`WORKER_SPAWNED` replay in `_bind_session_id` (a bridge whose session id arrives after the
event). Re-binding after a respawn is at `bridge.py:957-967`.

**What recovery does not cover.** Getting a bus after a spawn is a *bounded retry*: 5 attempts at
250 ms, ~1.25 s worst case (`bridge.py:869-874`), because it runs on the event-bus publishing thread
and must never block it. Past that deadline the worker's events are simply not forwarded to that
bridge, and nothing raises.

> **If this document teaches one thing, it is this:** a worker that is running but whose bus was
  never subscribed is *invisible*, and from the UI it is indistinguishable from a dead worker.
  Registration happens inside `run()` (`worker_thread.py:4513-4514`), so a worker that dies before
  reaching it has no bus for anything to discover, and a worker whose bus appears after the retry
  window is never bound. Neither case produces an error. Debugging "the worker is silent" by reading
  the worker's own logs will make you look in the wrong process.

## 3. Invariants

| # | Invariant | Why it matters | Cite |
|---|---|---|---|
| I1 | The default runtime mode is `process`; thread mode is explicit opt-in | The whole isolation story is this line | `worker_runtime.py:84`, `:351-373` |
| I2 | An invalid `WORKER_RUNTIME_MODE` raises instead of falling back | A typo must not silently downgrade isolation | `worker_runtime.py:369-372` |
| I3 | All child→parent events go through the outbox, built before `start()` | Drop/reorder of the first incarnation's events | `worker_runtime_main.py:333-341` |
| I4 | The outbox is the process's only writer; `seq` has one owner | Ordering guarantee; a second writer breaks it | `worker_runtime_main.py:344-350` |
| I5 | `_frame_sink` is a non-blocking `put` | A heartbeat tick must never wait on the socket | `worker_runtime_main.py:349-350` |
| I6 | Bus registration happens in `run()`, versioned by `epoch` | Stale incarnations must not overwrite live ones | `worker_thread.py:4513-4514`, `worker_registry.py:345-359` |
| I7 | `_registry_lock` and `_bus_registry_lock` are separate locks | Bus bookkeeping must not block the worker registry | `worker_registry.py:162`, `:170` |
| I8 | No registry-lock critical section contains a spawn or IPC | Section 4 | `worker_thread.py:7758`, `:7883` |
| I9 | Tests import *this* checkout's packages, and a static guard enforces it | Section 5 | `tests/test_import_provenance.py:39` |
| I10 | No file under `tools/` may import another tool class or look up `TOOL_CLASSES` at runtime | Section 6 | `working_agreements.md:545` |
| I11 | Host execution goes through `host_run`, which re-checks identity → kill switch → grant → env → cwd → argv, in that order | Structural, not convention | `thoughtmachine/host_port.py:1-12` |
| I12 | `stop` detaches; `kill` is the only termination path | "Stop" must be recoverable | `worker_thread.py:2631-2637`, `:2688` |
| I13 | Timeout does not stop the worker | A timeout is about the caller, not the worker | `docs/CONTROL_PLANE.md:311` |

## 4. The deadlock we survived

**The invariant, verified.** The registry lock is held around *bookkeeping only*, and the spawn
happens after it is released. In `_action_spawn` (`tools/workspace/worker_thread.py:6992`):
`with _registry_lock:` opens at `:7758` and covers the double-checked existing-worker test, the
live-worker count, the cap check and the registry write (`:7795-7830`); the runtime handle is
deliberately published *after* the block, *"never inside it: the handle store takes its own lock,
and the ordering every caller respects is registry → handle, never the other way round"*
(`:7826-7843`); `thread.start()` is at `:7883`. The code says why the shape cannot be relaxed:

> `register_worker()` cannot be used here: it takes this very (non-reentrant) `_registry_lock`, and
  moving the registration outside the block would re-open the capped-spawn TOCTOU race this block
  exists to close. — `worker_thread.py:7810-7816`

`WorkerRegistry` critical sections are dict operations only
(`worker_registry.py:256, 270, 291, 297, 318, 333, 358, 383, 406, 421, 450, 457`), and
`register_worker` calls `log_worker_event` outside its `with` block (`:255-264`).

**The failure mode it prevents.** Hold a non-reentrant lock across a spawn or an IPC round-trip and
you have created a cycle: the spawn path needs the lock to complete its own bookkeeping, the lock
holder is waiting for the spawn to return, and neither can proceed. It presents as a hang with no
stack in the process you are watching, because the missing progress is in the *other* process. This
is also why the bus registry has its own lock (I7): the cheapest way to avoid a lock cycle is to not
share the lock.

**Reported history (not reconstructable from the code).** It is said that a deadlock through this
lock survived an earlier fix, and that the remedies were a pause signal moved outside the lock and
some "timeouts". Nothing in this tree records that incident: a search for `deadlock` /`dead-lock`
across all `*.py` returns five hits, none about the registry lock across spawn, and no test name,
comment or doc calls the event a deadlock. The mechanism is consistent with the code — pause state
(`_pause_requested`, `controller.pause_event`) is not under `_registry_lock` — but **consistent
with** is not **evidence of**. If you need the history, ask for the commit or PR; until then, treat
it as folklore and reason from I8. See section 11, items 1-3.

## 5. `sys.path` phantom passes

Nine test modules used to prepare the interpreter with `sys.path.insert(1, "/workspace")`. That
literal is correct only when the checkout lives at `/workspace`; run the same suite from a worktree
and it resolves to the **parent checkout**, so those modules imported the parent tree's `security` /
`tools` / `agent` packages — and went green (`tests/test_import_provenance.py:5-12`).

It was measured, not assumed. With the worktree's `security/security_gate.apply_workspace_ceiling`
mutated into a no-op (`if True: # MUTATION PROBE`), **all 208 tests in those nine modules still
passed**, because they were executing the parent checkout's unmutated module. After the literals
were removed, the same mutation turns them red (`tests/test_import_provenance.py:14-18`).

Why it is invisible: the import succeeds. There is no warning, no skip, no error — the suite is
testing a *different program* while reporting on yours. Two smaller variants of the same class of
bug are also guarded now: a child-interpreter probe in `tests/conftest.py:434-479` hard-fails when a
module is not importable without `PYTHONPATH` (`PYTHONPATH`-only importability is a warning; the
escape hatch is `THOUGHTMACHINE_SKIP_CHILD_PROBE=1` at `:458`).

**The rule:** never put an absolute filesystem literal on `sys.path`. Derive it
(`Path(__file__).resolve().parents[1]`), and if you need to know *which* copy of a package is
loaded, assert its origin against the repo root —
`PROJECT_MODULES = ("security", "security.security_gate", "tools", "agent")` must resolve under
`REPO_ROOT` (`tests/test_import_provenance.py:39`). A green test tells you nothing until you know
which file answered.

## 6. Why tool-wrapping is banned

**The rule.** No file under `tools/` may import another tool class or look up `TOOL_CLASSES` at
runtime. Tools are leaves of the permission model, not nodes
(`.thoughtmachine/knowledge/project/working_agreements.md:545`).

**Why.** A wrapper is a second authority on "can this session call this tool". The permission gate
is evaluated on the tool the model named; a wrapper names a narrow tool and runs a different one, so
the model is no longer offered exactly what the policy layer approved — and the gate was never asked
about the tool that did the work. Not hypothetical: `PaginateTool` declared only `filesystem: read`
and would instantiate and run another class out of `tools.TOOL_CLASSES`, so a filesystem-only
profile could reach `HostBashTool`. Because it also dropped `workspace_id` when forwarding context,
the inner call could run under a caller-chosen workspace identity — a containment defect and an
attribution defect at once (`docs/ARCHITECTURE_MACHINE_MODEL.md:284-291`).

The incident is recorded with its three attempts, in order
(`docs/ARCHITECTURE_MACHINE_MODEL.md:293-317`): `8aa17e0d` contained the context fields and failed
*as a class of fix* — the list was built from paths, so a non-path identity field (`workspace_id`)
was never in it; `0c125856` re-gated inside `execute()` and added
`_NEVER_FORWARD = ('workspace_id',)`, keeping honestly-recorded residuals ("Defect 1 with a
seatbelt"); `4a4bd692` deleted the tool — 223 lines gone, 18 tests retired with it, on the rule *"a
safe wrapper is still a wrapper and the class survives wherever the pattern is copied."* The
registry entry is now a pointer comment naming the class rule (`tools/__init__.py:272`, second
pointer at `:74`). The tool is gone (`tools/paginate_tool.py` does not exist).

**The automation that is supposed to enforce it is parked, not running.** The AST guard is
source-only at `.thoughtmachine/working_docs/tool_no_wrapping_guard.py`, whose own header reads
`PARKED GUARD SOURCE -- hygiene/no-tool-wrapping` (`:2`) and states that
`tests/test_tool_no_wrapping.py` does not exist — verified. Nor would it pass if you landed it:
*"The rule this guard encodes has 13 open violations in the tree today (5 x letter (a), 7 x letter
(b), 1 x letter (c)), so 3 of its 4 tests fail on an untouched checkout"* (`:12-17`).

The honesty of the doctrine has improved, and the improvement is itself instructive. An earlier
revision claimed CI enforcement by the nonexistent test file; that is recorded as a defect at
`.thoughtmachine/working_docs/ticket-hygiene-tool-import-declaration-match.md:46` and `:112`. The
line now reads: *"Enforcement. Review time, by humans — not a test. `tests/test_tool_no_wrapping.py`
does not exist and never has on this branch … parked and unactivated: no test module imports it and
no workflow runs it"* (`working_agreements.md:549`). The ticket's quotation of `:549` is therefore
stale; the claim it was complaining about has been corrected in place. Check the line, not the
ticket.

**Bottom line:** the rule is real and binding on humans. The automation does not exist. Read the
guard before citing it as a control, and do not cite this rule as "the linter will catch it".

## 7. The host port is the chokepoint — with an exception you must know about

`thoughtmachine/host_port.py` is the single host-execution port: *"Every process that runs on the
host (not inside a container) must go through `host_run` … the port re-checks identity, the
workspace operator kill switch, the effective grant, credential-shaped env, cwd containment and argv
typing — in that fixed order — before anything is spawned"* (`thoughtmachine/host_port.py:1-12`).
The order is code, not a regex, so the guarantee is structural for every caller that uses it. It
refuses a `host_bash` grain of `ask` by name rather than silently downgrading, because it has no
prompt and no user (`host_port.py:13-22`; `AUTHORIZED_VALUES["host_bash"] == {"allow"}`).

**The absolute version of the claim is false.** The module docstring also says `host_port.py` is
"the ONLY module in `thoughtmachine/` or `tools/` permitted to import `subprocess` ". That is an
*inventory* claim, and the inventory does not hold. There is a direct, self-admitted bypass:

- `tools/mcp_client.py:226-240` — *"DEFERRED HOST EXECUTION — DO NOT MIGRATE YET. This stdio spawn
  still calls `subprocess.Popen` directly and therefore **bypasses the `thoughtmachine/host_port.py`
  chokepoint**."* The migration is blocked on an async port entry point: `host_run_async` is a
  signature-only stub, and every `HostRunHandle` method (`poll`/`wait`/`kill`) raises
  `NotImplementedError`. The deferral is pinned by `tests/test_mcp_client_uses_host_port.py`.

Other `import subprocess` sites verified in the tree — some are container paths or diagnostics, some
are host spawns, and this list is **not** adjudicated: `tools/git_write_tool.py:6`,
`tools/ci_read_tool.py:56`, `tools/git_info_tool.py:6`, `tools/mcp_client.py:11`,
`tools/mcp_server_connect.py:26`, `tools/workspace/worker_execution.py:29`,
`infra/workspace_lifecycle_manager.py:38`, `start_windows.py:27`, plus several under `scripts/`,
plus `thoughtmachine/host_port.py:53` itself.

**Write it precisely.** The correct statement is: *the chokepoint is structural per call site* — for
the tools that use it, the order of checks is enforced and cannot be bypassed by the caller. The
wrong statement, which the code contradicts, is: *"`host_port.py` is the only module permitted to
import `subprocess`."* A related ticket makes the general point that this class of rule must be
expressed as a property rather than an inventory
(`.thoughtmachine/working_docs/ticket-hygiene-host-port-import-rule-is-inventory-not-property.md:83`).
See section 11, item 5 — a per-site ruling is outstanding before anyone writes the absolute.

## 8. The control plane: four pieces, and what each one means

The summary table is `docs/CONTROL_PLANE.md:27-31`, and it is the version to trust. Watch the
naming: the section is `## 10. Implementation: the four pieces and the run-state machine` (`:789`),
its first sub-heading is `### 10.1 Three axes, three objects` (`:795`), and §10.4 is titled "The
eight pieces" (`:843`). Three counts, one section. The doc sets its own convention at `:789-792`:
*"Every statement below is a ruling unless a `file:line` is cited, in which case the citation is
current behaviour."*

| Control | Intent | Turn survives? | Final words? | Where the work goes |
|---|---|---|---|---|
| **Pause** | orderly wind-down | yes — finishes, committed | yes, exactly one `Respond` | parks (`paused`), resumable |
| **Stop** | abort now | no — discarded | no summary, no extra LLM turn | parks (`detached`), resumable |
| **Timeout** | caller gives up waiting | yes, in the worker | the worker keeps working and answers later | worker keeps running; caller got a `timeout` envelope |

- **Stop detaches; it does not terminate.** `stop` is literally an alias: `def stop(self)` →
  `self.detach()`, docstring *"``stop`` is an alias for :meth:`detach`. Termination is
  :meth:`kill`"* (`worker_thread.py:2631-2637`). `detach()` sets `status = "detaching"`, persists
  context (*"a detached worker is expected to be revived later, possibly by another process"*), and
  registers the worker as detached so the stale-container sweep skips it **until the detached TTL
  expires** (`:2639-2686`) — that is the "containers kept" half, and it is time-bounded, not
  indefinite. `kill()` is the only termination path (`:2688`, *"PHASE 2.1a: this is the ONLY
  termination path"*).
- **Timeout leaves the worker running, on purpose.**
  `## 5. TIMEOUT — the caller stops waiting, the worker does not stop` (`CONTROL_PLANE.md:311`); the
  state table says that at expiry the caller gets a `timeout` envelope and the worker is "unchanged,
  still `running` " (`:356-361`). Not an oversight: a timeout is a fact about *one caller's
  patience*, not about the work. Cancelling the turn because a UI gave up would destroy a turn that
  is still making progress, and would make a slow caller a source of data loss. A timed-out caller
  must not blind-retry (`:348-353`).
- **Pause parks — but do not repeat "pause works" as a fact.** Its *intent* is verified (a distinct
  verb, resumable park). Its *enforcement* has a fail-open hole documented by the same doc: the tool
  allow-list is computed behind `getattr(..., 'restrictions_active', False)`, which "silently
  returns 'no restriction' when the attribute is missing or falsy. The gate then treats an empty
  allow-list as 'everything allowed'" (`CONTROL_PLANE.md:41-46`, §6 at `:368-471`). Section 11, item
  10 records that this was not re-checked against the executor.

## 9. Green for the wrong reason

The team's standing anti-pattern is a test that passes because it never exercised the code it claims
to cover. There is a production instance of it.

**`TRUST_TEST_HARNESS`.** The flag is `False` at import (`web_ui/backend/auth_boundary.py:229`),
flipped only through one setter (`:260-261`), and consumed as an early return at `:625`:
`if TRUST_TEST_HARNESS and peer_host(scope) in TEST_HARNESS_CLIENT_HOSTS:` — which skips steps 2-6
of the boundary: Host acceptance, cookie minting, Origin verification, credential check, and the
websocket 1008 close. The allow-list is `TEST_HARNESS_CLIENT_HOSTS = frozenset({"testclient"})`
(`web_ui/backend/local_gate.py:45`, next to `LOCAL_CLIENT_HOSTS` at `:37`) — a name no real peer can
present.

**And it is on for the entire suite.** Verified by direct read of the root conftest:

- `conftest.py:151` — `@pytest.fixture(scope="session", autouse=True)`, whose docstring (`:153`)
  says it enables `auth_boundary.TRUST_TEST_HARNESS` for the whole session because "the allowance is
  process-wide" and "no test should have to remember to ask for it".
- `conftest.py:162-164` — `previous = auth_boundary.TRUST_TEST_HARNESS`, then
  `set_suite_trust_value(True)` and `auth_boundary.enable_test_harness_trust(True)`; restored in the
  `finally` at `:168-170`.
- `_TrustRestoringFinder` (class at `conftest.py:119-132`, installed on `sys.meta_path` by
  `_install_finder()` at `:136-141`) re-applies the flag across module reloads and `sys.modules`
  purges, *"so no test can accidentally see the secure default"*
  (`docs/security-audit-gen64.md:97`).

(The security audit cites the finder as `conftest.py:104-137` at `docs/security-audit-gen64.md:222`;
the actual range is `:119-132`. Its `:153` /`:162` citations are sound.)

**What this means, plainly.** The suite routinely opens the very gate it is meant to prove closed. A
green auth or permission test therefore proves less than it appears to: it ran against a boundary
that is not the one shipped. The audit states the exposure as D4 — *"`TRUST_TEST_HARNESS` is set for
the entire suite, so every web test runs behind an open gate"* (`docs/security-audit-gen64.md:96`) —
and recommends narrowing rather than deleting it (*"Do not delete the pin; narrow it … default the
flag off in the root fixture and require each test to opt in"*, `:222`). The opt-out pattern already
exists in `tests/test_auth_boundary.py:161-167`.

**The standing demand: tests must be mutation-proof.** A test is not evidence until you have watched
it go red against the behaviour it claims to defend. Two worked examples from this tree, one of each
polarity:

- *The model* — `CheckSystem` 's `event_log` read scope is session-scoped, and nothing pinned that a
  workspace-wide match would leak. Two tests were added (`tests/test_event_log_scoped.py:354`,
  `:382`, section marker at `:345`), then the filter loop was mutated to widen the scope into a
  workspace fallback: **2 failed / 9 passed**, and the two failures were precisely the two new
  tests; the file was restored byte-identically and re-ran `11 passed`
  (`.thoughtmachine/working_docs/ticket-test-checksystem-event-log-session-workspace-conflation.md:61-68`).
  That is what "covered" is supposed to mean.
- *The anti-model* — the 208-test phantom pass in section 5: a mutation was applied and every test
  stayed green, which is how the suite was discovered to be importing a different checkout
  (`tests/test_import_provenance.py:14-18`).

The contract ledger applies the same standard deliberately: each of its three invariants names the
test that *would fail if the enforcement were removed*, and says so (`docs/CONTRACTS.md:1-11`). Its
cheapest example is mode isolation — `tests/test_worker_is_a_real_process.py:105` asserts
`reply_pid != parent_pid`, with the failure message *"the 'child process' is a thread wearing a
process-mode label"*.

## 10. Traps and standing corrections

Every item here is "everyone believes X, the tree says Y". Read this list before you trust a
document, a ticket, or a senior engineer's summary.

| Everyone believes | The tree says | Cite |
|---|---|---|
| The onboarding index is a list of files you can read | 17 of the files it indexes are **not on disk** at all (`current_map.md`, `machine_model.md`, `design_layers_and_truth_ownership.md`, `design_permission_facts_ownership.md`, `rulings-permission-facts.md`, `operator_notes.md`, `container_controls_findings.md`, `core.md`, `open_items_register.md`, `structural_debt_register.md`, `design_intent_state_divergence_family.md`, `system_architecture.md`, `reliability_log.md`, `v3_vision_full.md`, `gen30_journey.md`, `run_design_pass.md`, `design_permission_bins_shape.md` under `project/`). The index now labels each one `**(absent — not on disk)**` (§1 from `:97`, §2 to `:177`, and `:200` for `task_tracker.md`/`parking_lot.md`) — repaired in this pass; the files themselves are still absent | `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md:97-177`, `:200`; missing status verified by direct `find` |
| "Thirteen files ≥100 KB, 7 under `personal/`" | **Six** are ≥100 KB — four under `project/`, one under `_archive/personal/`, one under `personal/` — and `personal/` holds exactly **two** files totalling 180.1 KB; `personal/task_tracker.md`, `parking_lot.md` and `personal/bugs_and_fixes.md` do not exist (that last one survives only under `_archive/personal/`). The index asserted the 13/7 split until this pass measured it | `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md:184-191`; `wc -c` |
| `lessons_learned.md` is ~450 KB | It is 107,990 bytes (~105 K) | `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md:193`; `ls -l` |
| The no-tool-wrapping guard is in CI | `tests/test_tool_no_wrapping.py` does not exist; the guard is parked at `.thoughtmachine/working_docs/tool_no_wrapping_guard.py` and would fail on an untouched checkout | `tool_no_wrapping_guard.py:2`, `:12-17`; `working_agreements.md:549` |
| `host_port.py` is the only module permitted to import `subprocess` | `tools/mcp_client.py:226-240` bypasses it on purpose, and `import subprocess` appears at ~10 other sites | `thoughtmachine/host_port.py:4-5` vs `tools/mcp_client.py:226-240` |
| The Contract Gate is a required check | Required-ness is **server-side and not in the repo**; the KB contradicts itself — one entry says the ruleset and required checks held (13/13 succeeded), another says `gh pr checks --required` "returns 'no required checks reported'", i.e. every check is advisory | `docs/testing/ci_process.md:35-40`; `.thoughtmachine/knowledge/project/CI_PROCESS.md:66-70` |
| The design docs describe the shipped shape | They did not: five asserted the thread world long after the process boundary landed (`docs/agent-core-spec.md:886`, `docs/design-worker-lifecycle.md:425`, `docs/design-worker-runtime-process.md:74`, `docs/design-process-isolation.md:20`, `.thoughtmachine/knowledge/project/machine_history_and_vision.md:61`), and all five were corrected in this pass — each now says a worker body is a child OS process per worker by default (`tools/workspace/worker_runtime.py:84`). Treat them as repaired-now, not as independent corroboration of each other | as cited, now correct; `machine_history_and_vision.md:62` still says `host_bash` is, among the tools, the only one that executes on the host — the qualifier the `host_port.py` row above already supplies |
| `docs/security_layer.md` is current | It is marked OBSOLETE on line 1: "describes a pre-migration Qt architecture" | `docs/security_layer.md:1` |
| "Pause parks" is a working control | Its intent is right; the allow-list gate behind it fails open | `docs/CONTROL_PLANE.md:41-46` |
| Doc line numbers are stable enough to cite | Not stable: the most-trusted architecture doc had all four coordinates in its §1 opening paragraph wrong (`:83`, `:1277-1281`, `:1630`, a bare `line 72`); caught and fixed in this pass, and the same rot will recur | `docs/ARCHITECTURE_MACHINE_MODEL.md:30-37` vs `tools/workspace/worker_runtime.py:84,674`, `tools/workspace/worker_thread.py:1880`; §1 above |
| A green suite means the code works | The suite runs behind an open auth gate and has previously tested a different checkout | `conftest.py:151-170`; `tests/test_import_provenance.py:14-18` |

Two files that **are** worth trusting, and should be preferred over the rest when they disagree with
anything: `docs/ARCHITECTURE_MACHINE_MODEL.md` (§1 matches code; §9a's paginate history matches the
tree) and `docs/CONTRACTS.md` (three invariants, each with a code site and a test that fails on
removal).

## 11. Open questions / needs human input

Reproduced from the recon fact sheet, unmodified in substance. These are **unresolved**: none of
them belongs in the body of this document as an assertion, and none of them should be repeated as
fact in a standup.

1. **Deadlock incident provenance.** No artefact in the tree calls the registry-lock/spawn event a
   "deadlock". `deadlock|dead-lock` over all `*.py` gives five hits, none relevant
   (`infra/container_registry.py:544`, `tests/test_worker_multi_process_adoption.py:23`,
   `tests/test_container_registry.py:681`, `tests/test_stop_is_turn_scoped.py:90`,
   `web_ui/backend/bridge.py:601`). Needs a commit sha or PR number. **Section 4.**
2. **"Pause signal moved outside the lock".** Consistent with the code — pause state is not under
   `_registry_lock` — but no record of the change exists. Needs a date or sha. **Section 4.**
3. **"Timeouts" as the fix.** Which timeouts? The only lock-adjacent candidates are the bounded
   bus-resolve retry (`bridge.py:869-874`) and `join(timeout=…)` in force-spawn
   (`worker_thread.py:7054-7062`). Unconfirmed as *the* fix. **Section 4.**
4. **RESOLVED (partial):** 10 required status checks exist server-side on `dev`, evidenced by GitHub's own rejection message on direct push at `a869255d` and `544bd407`: `remote: Bypassed rule violations for refs/heads/dev: - 10 of 10 required status checks are expected.` The check *names* still require `gh api repos/{owner}/{repo}/branches/dev/protection` (operator-admin). Whether branch protection blocks the *merge button*, as opposed to being bypassable by direct push, is still unknown and needs the same `gh api` call.
5. **"The only chokepoint."** Contradicted for MCP stdio, and ~11 other `import subprocess` sites
   remain unadjudicated one by one (git tools, `worker_execution.py`, `sandboxed_execution.py`,
   `doctor.py`, `workspace_lifecycle_manager.py`, `containers/executor.py:1259`). Needs a per-site
   host-vs-container ruling before the absolute is written anywhere. **Section 7.**
6. **Are two `knowledge/project/` files tracked by accident?** `INFRA_ONBOARDING.md` §1 claims
   `architecture_audit_2026-09.md` and `development_guides.md` are. Not verifiable without
   `git ls-files`, which was out of scope for the recon.
7. **Is the `INFRA_ONBOARDING.md:24` restart-check list complete?** `infra/` does exist as a
   package, but the list omits `thoughtmachine/` and `session/`, which are also runtime code.
   Unverified as complete — not shown false.
8. **Six docs with no H1 in their first five lines**, so purpose is unknown:
   `docs/container_registry_design.md`, `docs/docker_usage.md`, `docs/message_metadata.md`,
   `docs/pruning_system.md`, `docs/system_notifications.md`, `docs/LLM_quality insurance.md`.
9. **Which "four pieces"?** `CONTROL_PLANE.md:789` says four, `:795` says "three axes, three
   objects", `:843` says "the eight pieces". Three counts in one section; needs the operator's
   definition. **Section 8.**
10. **Is the control-plane fail-open bug still present?** `CONTROL_PLANE.md:41-46` asserts it as
    "present today"; it was not re-checked against `agent/core/tool_executor.py`. **Section 8.**

When you resolve one of these, replace the affected body text and delete the item. Do not add a new
story without a citation — a confident wrong explanation costs the next engineer more than a blank
space does.
