# design/agent-role-control

## Cluster

This ticket is one of three passes over the **same** problem, taken from three angles:

- **WHAT runs** — a *Resource*: a typed envelope, no shell. `ticket-design-tool-resource-contract.md`.
- **HOW it returns** — a *Job*: a handle, not a result. `ticket-design-worker-job-lifecycle.md`.
- **WHO calls it** — a *Role*: plan-shaped Control verbs. This file, `ticket-design-agent-role-control.md`.

The convergence requirement is explicit and binding: these three passes must land on **one** handle/state/timeout model, not three parallel ones. The Control verbs this ticket assigns to each role are the *same* verbs that operate the handle in `ticket-design-worker-job-lifecycle.md` — `join` is not a different act from waiting on a handle, and `cancel` is not a different act from cancelling one. If the role split invents its own control vocabulary, the model forks and the cluster has failed.

## The role problem

The operator's words, verbatim:

> "when the main agent starts working himself, that's bad"

The failure being named is not slowness and not tool use as such. It is **role collapse**: the main agent, which is supposed to hold the plan, the dispatch decisions, and the judgement about what the work means, drifts into doing the work. When that happens there is no layer left that is planning — the thing that was supposed to be steering is instead typing, and nobody is watching the shape of the whole task. The plan stops being revised because the planner is busy.

## The fix is not "remove tools"

The obvious response — take the execution tools away from the main agent — is wrong, and it is wrong in a way that makes things worse. Tool removal is a blunt instrument: it removes capability without expressing intent, so the boundary becomes "what we remembered to unregister" rather than "what this role is for". It also produces an agent that cannot check a fact, which pushes it back toward the same behaviour by other means, and it leaves the actual distinction — *what kind of act is this* — unstated and therefore unenforced.

The fix is **role-scoped Control verbs**. The roles are distinguished by the *verbs* they hold, not by the accident of which tools are wired in. A verb says what an act is for; a tool name says which implementation happens to exist this week. The first is a design; the second is an inventory.

## Main agent Control

The main agent holds plan-shaped verbs:

- **plan** — decide what the work is, decompose it, revise the decomposition as evidence arrives.
- **dispatch** — hand a piece of work to a worker.
- **join** — wait on dispatched work and take its result.
- **cancel** — end dispatched work that should not continue.
- **read results** — take in what came back and decide what it means.
- **write KB/docs** — record findings, decisions, and state where they will be found again.

Every one of these operates on the *shape* of the task. None of them is "perform the task". That is the boundary, stated positively.

## Worker Control

The worker holds execution verbs:

- **execute tools** — run the tools the work requires.
- **write code** — produce the change.
- **run tests** — establish whether the change is real.
- **commit** — land the change (within whatever write permissions the session holds).
- **report** — return what was done, what was found, and what remained.

The worker's verbs are the ones that touch the artefacts. A worker that plans is doing the main agent's job; a main agent that executes is doing the worker's. The split is symmetric in what it forbids, which is what keeps it from reading as a hierarchy of importance.

## Framing

This is **the Resource Contract applied to the agent role itself**. The same move that turns "a tool that shells out" into a typed capability applies to the dispatcher: the role declares what it can be asked to do, and it is handed a closed verb set rather than an unbounded surface. `ticket-design-tool-resource-contract.md` makes the *call* typed; this ticket makes the *caller* typed. Seen from that angle, the role split is not a policy layered on top of the architecture — it is the same architecture, one level up.

## Gates

The permission gates are **the same in both roles**. The fourth-gate removal is preserved: there is no additional gate for the main agent, and no gate is reinstated as a side effect of the role split. A Resource capability is gated once, the same way, whoever holds the handle.

What differs between the roles is the **verbs available to each role** — not the strength of the gates behind them. A verb a role does not hold is not a verb that is gated harder; it is a verb that is not in that role's Control surface at all. Keeping those two mechanisms distinct is what stops the role split from quietly becoming a second permission system.

## Filed

Filed 2026-10-23 by the main agent at the operator's directive. Base: dev @ bc8c7700.
