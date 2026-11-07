# design/worker-job-lifecycle (core-adjacent)

**core-adjacent:** this pass touches the core agent loop. It does **not** dispatch without the operator's explicit awareness, and no part of it proceeds on the assumption that the loop rewrite is already authorised.

## Cluster

This ticket is one of three passes over the **same** problem, taken from three angles:

- **WHAT runs** — a *Resource*: a typed envelope, no shell. `ticket-design-tool-resource-contract.md`.
- **HOW it returns** — a *Job*: a handle, not a result. This file, `ticket-design-worker-job-lifecycle.md`.
- **WHO calls it** — a *Role*: plan-shaped Control verbs. `ticket-design-agent-role-control.md`.

The convergence requirement is explicit and binding: these three passes must land on **one** handle/state/timeout model, not three parallel ones. The states named here are the same states the Resource contract declares in its lifecycle fact, and the verbs that drive them are the same verbs the role split grants. If this ticket and `ticket-design-tool-resource-contract.md` end up with two vocabularies for "timed out", the cluster has failed regardless of how good either pass is on its own.

## The layer tangle

Three layers that should be independent are currently coupled by **blocking calls**. The **agent loop** calls down and waits. The **worker** runs and waits. **Execution** (tool call, container run) happens and the caller waits for it to finish. Each layer's control flow is a function call that does not return until the layer below is finished, which means:

- No layer can make a decision while work is in flight — it is inside a blocking call, not in a loop that could observe anything.
- Time is implicit. Because nothing above models duration, duration can only be imposed from outside, by a timeout wrapped around a call that does not know it has a deadline.
- Cancellation is not expressible. There is no handle to cancel, only a call to interrupt.
- The worker's own lifetime is not a thing the system can name, so a worker that ends early ends silently (see `ticket-hygiene-worker-silent-timeout.md` — that ticket names the reason *now*; this one is where the reason becomes structural).

The tangle is not three bugs. It is one missing primitive, inherited three times.

## The job primitive

**Every execution returns a HANDLE, not a result.** That is the entire change in one sentence. The caller receives a reference to work that is happening, plus the vocabulary to ask about it, wait on it, or cancel it. The result, when there is one, is fetched *through* the handle rather than being the return value of the call.

This is the same primitive at all three layers below, which is precisely why it is one primitive and not three: the reason the layers are tangled is that each of them lacks the same abstraction, so each of them invented blocking instead.

## Handle fields

A handle carries, at minimum:

- **Stable identity** — a name that still means the same thing after a process restart and in a log line written by a different layer. Identity is what lets a handle be stored, passed, compared and resumed; a pointer or a PID is none of those.
- **State** — `queued` / `running` / `done` / `timeout-soon` / `timed-out` / `errored` / `cancelled`. Note that `timeout-soon` is a distinct state, not a warning string: the system must be able to *tell* a cooperative shutdown that it is about to be ended, which requires the state to exist before the ending.
- **Result, or a pointer to it** — either the value or an addressable location. Which one is a size decision, not a contract decision; the caller must not need to know which it got.
- **A cancel signal** — the handle is the thing you cancel. Cancellation is an operation on the handle, not an interruption delivered to a call site.
- **A detach affordance** — the ability to stop waiting while the work continues, and keep the handle. Detach is what makes a long run survivable across the caller's lifetime, and it is why the container case below is not a special case.

## The same primitive at three layers

- **The worker is a job.** A dispatched worker has an identity, a state, a result (the report) and a cancel; the main agent holds a handle to it rather than being inside a call that ends when the worker ends.
- **A tool call is a job.** Every tool invocation has a state and a duration; the caller should be able to observe a long tool rather than being suspended by it.
- **A docker run is a job.** Container execution has all of the above *and* it can outlive the caller, which is exactly what the detach field is for.

Same fields, same states, same verbs, three times. That is the point of the cluster.

## Timeout semantics

**Timeout is a PARAMETER OF THE JOB, not something implicit in the tool call.** Today a deadline is imposed from outside on a call that has no concept of one; the job model makes the deadline part of the job's own definition, known to the work while it runs.

Consequences:

- At **T** (the point where the job is nearly out of time) the worker receives a **"now ending"** signal. This is the `timeout-soon` state doing real work.
- Shutdown is **cooperative**: on that signal the worker **writes state and returns a partial result**. Partial is a legitimate, named outcome — not a truncated success and not a silent nothing.
- A **container detaches**. It is **its own job with its own handle**, not a child call of whoever started it. The container's lifetime is not the caller's lifetime, so its timeout is its own timeout, and ending the caller's job must not be the mechanism by which the container's job ends.

## The core loop change

**`act` returns a handle; `observe` is poll/join/sleep; the loop never blocks.**

This is **the deepest piece of the cluster** and it is **core-adjacent**, because it changes the shape of the core loop rather than the shape of a leaf. `act` no longer means "do this and come back when it is done"; it means "start this and give me the reference". `observe` becomes the only place where waiting happens, and waiting is explicit, bounded, interruptible, and observable — poll, join, or sleep for a turn. The loop iterates instead of descending.

Because this is the deepest piece, it is also the piece that must not be dispatched blind: the operator's explicit awareness is a precondition, not a formality.

## The 5-minute-return problem

Today a worker can come back at about the five-minute mark with a return that does not say why. That return **is a timeout without a named reason**. Nothing in the shape of the return distinguishes "the work finished" from "the work was cut off"; the truncation is real but unnamed, so the operator has to infer it from timing and context.

The job primitive makes the **return reason explicit**: the handle's state *is* the reason (`done` vs `timed-out` vs `errored` vs `cancelled`), and the partial result is labelled as partial. That is the structural answer to the same symptom `ticket-hygiene-worker-silent-timeout.md` addresses cheaply and immediately.

## Cross-reference

The **detach** case is a **boundary decision as well as a lifecycle one**. When a container detaches from the job that started it, the container's `workspace_id` scoping must **survive the detach** — the detach must not be able to move a container out of its workspace's ownership, and a detached container must remain attributable to the workspace that owns it. Cross-reference the `design/vault-ownership-manifest` ticket for the container-record ownership model on detach; the lifecycle pass may not settle that model on its own.

## Filed

Filed 2026-10-23 by the main agent at the operator's directive. Base: dev @ bc8c7700.
