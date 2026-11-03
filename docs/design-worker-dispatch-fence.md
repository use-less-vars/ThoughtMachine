# Design: Worker Dispatch Fence — placeholder

**Status:** placeholder for a design pass that is **deferred to the next major wave**. No code
has changed for this document and **none may change in this wave**; nothing below is a settled
solution, a decision register, or an approved option. The document exists so the problem, the
evidence and the open questions are not re-derived from scratch when the pass starts.
**Date:** 2026-11-03.

**Scope:** the gap between *what a `force=True` spawn promises* and *what the caller can verify
about which worker instance answered*. It does not design the fence, does not pick a registry of
record, and does not touch the lifecycle routes.

**Claim tags:** **[observed]** — seen in live work (a work session driving the `Worker` tool);
**[verified]** — read out of the current tree in this session; **[inferred]** — reasoning, not a
code fact. Every `file:line` is a snapshot anchor and drifts; symbols win over line numbers.

---

## 1. The problem

The `Worker` tool documents `force=True` as: *"set `force=True` to automatically stop the
existing instance(s) of the same worker belonging to the **current session** before creating a
fresh one"* — `tools/workspace/worker_thread.py:4119-4124` **[verified]**.

In practice a force-spawn request can be answered by an **existing** instance rather than a
fresh one, and **the caller cannot tell from the returned envelope which instance ran the
query** **[observed]**.

The work still completes correctly. The defect is **observability, not execution**: an
orchestrator that spawns a fresh worker specifically so it starts with a clean context has no
field to check, so a pipeline step built on "spawn fresh so the worker forgets the previous
stage" is reasoning on an assumption it cannot verify.

## 2. Evidence, and its limits

| Fact | Status | Anchor / limit |
|---|---|---|
| A force-spawn can be answered by an existing instance and still succeed: the envelope returns `"spawned": false` with `"status": "ready"` | **[observed]** twice in one work session | In **both** cases the dispatched query returned correct results. `spawned: false` is therefore **benign**: it means "no NEW instance was created for this call, an existing one was reused". It is **not** a failure signal and must not be documented as one. |
| The envelope names **no** instance identity for the instance that answered | **[observed]** | The defect survives even though the outcome was fine: the missing field, not the reuse, is the bug. |
| The reuse branch's envelope shape matches the observation exactly — `"spawned": False`, `"status": reuse_thread.status`, `"response"`, elapsed | **[verified]** | `tools/workspace/worker_thread.py:4838-4845`. Which branch actually produced the observation is **not** established (see Q5). |
| Force first tries to terminate its own session's instances with `kill()` + join budget; a thread that survives keeps its registry entry ("its registry entry is kept so the instance is not orphaned") | **[verified]** | `:4613` `if self.force:`; `:4630` `thread.kill()`; `:4654-4672` `survived_force_stop`. A survivor is by construction a live instance a later delivery can reach. |
| The non-force reuse branch fires on `len(live_same) == 1 and not self.force` | **[verified]** | `:4771`, with the delivery at `:4794-4845`. Whether the force flag reached the branch that consumes it in the observed case is **open** (Q5). |
| `spawned` is emitted by **more than one layer**: the tool action's own result dict, and the manager layer's `force_replace` / `request_worker` dicts | **[verified]** | `tools/workspace/worker_thread.py:4808`, `:4832`, `:4842`, `:5273`; `tools/workspace/worker_manager.py:643`, `:699-703` (`envelope["delivery"] = {"reused": ..., "spawned": ..., "force_replaced": ...}`). These are different objects produced by different layers and are easy to conflate. A design must say which layer owns which name. |
| `stop` **DETACHES**; it never terminates: "the thread, its containers and its registration all survive, and queued queries are kept" | **[verified]** | Tool docstring `tools/workspace/worker_thread.py:4126-4130`. So reuse of a parked instance is a **normal** event, which is exactly why the envelope must be able to say so. |
| Unknowns not examined this session | — | Whether any envelope field carries `instance_label` on every path; whether the process-mode runtime handle participates in delivery at all. Both are inputs to Q1/Q3, not answers. |

## 3. Questions the design pass must answer

Posed, deliberately **not** answered here.

1. **What identity does a caller actually need?** Is an instance id present in *every* returned
   envelope sufficient, or is a query-id → instance mapping required (the caller asks about a
   query, not about a spawn)?
2. **Fence or opt-in?** Should a force-spawn be a hard fence — refuse rather than silently
   reuse — or should reuse be explicit and opt-in at the call site?
3. **Which registry is authoritative?** There is a dispatch-layer handle registry that is
   deliberately **fail-open**: `runtime_handle_for` never raises, logs a miss at DEBUG and
   returns `None` so the command-file path takes over
   (`tools/workspace/worker_runtime.py:102`, `:154-193`; its docstring also still claims the
   registry is "EMPTY IN PRODUCTION TODAY" at `:161-164`, which must be re-checked). There is
   also the separate tool-layer `_worker_registry` guarded by `_registry_lock`
   (`tools/workspace/worker_thread.py:4666`, `:4691`). A fence design has to say which one
   adjudicates identity, and what a fail-open miss means for a fence that must not fail open.
4. **Detach semantics.** Given `stop` is a detach, should enqueueing work to a detached instance
   be refused, queued, or reported? Should a detach report whether the parked instance still
   holds an in-flight job?
5. **What made the observation possible?** Before designing anything: establish which branch
   produced `spawned: false` — a surviving force-stop thread, a spawn request whose `force` flag
   never reached its consumer, or the manager layer's delivery descriptor. A fence designed on
   the wrong branch fixes nothing.

## 4. Testability requirement

The pass is only done when it admits a test that **fails today and passes after**:

- a `force=True` spawn yields an instance identity **different** from the one before it; and
- the envelope **names** the instance that ran the query.

Nothing less is a fix; a test that only asserts `spawned: true` re-states the assumption under
test.

## 5. Not in scope

- No code change in this document's wave.
- The known v1 limitation that a detach can drop an in-flight result is recorded elsewhere and
  is **not** part of this design.
- The existing lifecycle routes are not to be reshaped by this work.
