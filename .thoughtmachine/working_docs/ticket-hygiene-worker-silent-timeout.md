# hygiene/worker-silent-timeout

## Problem

When a worker is soft-timeout'd, the return it hands back is **silent** — or near enough that it makes no difference. The reason is not named anywhere in the return, so a run that was **cut off** and a run that **completed** arrive looking the same. The operator then has to reconstruct which one happened from timing and from the shape of the content, which is guesswork dressed as inspection: a truncated report reads exactly like a short one.

The cost is not cosmetic. A truncated run that is read as complete becomes a false fact in the record — work believed done that was not, findings that stop mid-thought believed finished. Naming the reason is what makes the two distinguishable at the point of return instead of at the point of suspicion.

## Fix

The worker's **return path names the reason explicitly** — e.g. `timed_out at Ns` — instead of returning silently. The timeout metadata needed to say this is **already present**; the fix is to surface it on the return rather than leave it where only a log reader would find it.

This is roughly a **one-line change in the worker's return path**. No new primitive, no handle model, no state machine, no change to the loop's structure. It is a small, shippable naming fix that removes an ambiguity the operator is currently resolving by hand.

## Relation to the primitive

The **job primitive is the real target** for run duration: timeout as a parameter of the job, a `timeout-soon` state, cooperative shutdown writing state and returning a labelled partial result — see `ticket-design-worker-job-lifecycle.md`. That is where duration becomes a first-class thing the system models, and it is the durable answer to the same symptom.

This ticket **names the return reason now**, ships **before** that primitive exists, and does **not substitute** for it. When the job primitive lands, the reason this fix adds becomes the handle's state rather than a string on a return; until then, the string is what keeps the operator from reading a truncation as a completion.

## Not in scope

Do not redesign the worker loop here. No restructure of the agent loop, the worker, or the execution layer; no new handle/state vocabulary; no changes to how work is dispatched or joined. If a change would touch the loop's shape, it belongs in `ticket-design-worker-job-lifecycle.md`, not in this ticket.

## Filed

Filed 2026-10-23 by the main agent at the operator's directive. Base: dev @ bc8c7700.
