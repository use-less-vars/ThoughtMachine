# Ticket: bug/agent-creates-persistent-container-cannot-remove

**Filed:** 2026-10-20
**Source:** gen-47 container-ops run — dispatched finding F14 (orchestrator summary).
**Severity:** high — the agent can create an object it cannot clean up, and each verification run leaks one.
**Status:** filed, not dispatched.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` — read in full (384 lines); `## Findings` (L318) has **F1–F10 only**, no Appendix A/B, no F11–F22. F14 has **no citable report line numbers**; content is the dispatch summary.

## Defect

`ContainerStartTool` can create a container in the `persistent` lifecycle class. `ContainerRemoveTool` refuses that class to agent sessions (its own contract: persistent containers are operator-only because the coordinating `note` can only be set at creation, so no agent-side removal is trust-safe). The two halves do not compose:

- create: agent-permitted for `persistent`
- remove: agent-refused for `persistent`

An agent that follows the documented verification loop (start a container, execute, remove it) can therefore enter a state it cannot exit. The refusal is correct and fail-closed; the asymmetry is the defect. Each verification run leaves one undeletable container behind, and the leak is only visible to an operator holding the operator-only grain. It also interacts with capacity surfaces already ticketed separately (`ticket-bug-resource-containers-compete-with-workspace-cap.md`, `ticket-bug-admit-count-includes-recreated.md`).

## Fix shape (not written)

Two acceptable resolutions, pick one:

1. **Session-scoped remove** — extend `ContainerRemoveTool` to permit removal of `persistent` containers whose creator label identifies the calling session, keeping operator-only removal for everything else. This preserves the trust model (an agent may only destroy what it created) and makes the create/remove pair symmetric.
2. **Ephemeral option on start** — expose an explicit `lifecycle="ephemeral"` on `ContainerStartTool` so the verification loop never needs the operator class at all.

Prefer (1) as the general fix and (2) as the ergonomic one; doing only (2) leaves the existing create/remove asymmetry reachable. Do not simply allow unrestricted agent removal of persistent containers.

## Bearing

This is the "agent is handed a capability it cannot undo" shape — the same family as `ticket-bug-container-remove-ui-no-op.md` (removal surface that does not do what it appears to) and the audit gaps in `ticket-bug-removal-audit-unreliable-and-unattributable.md`. Small change, removes a leak class from every future run.

## Update 2026-10-21 (gen-47 pen-test)

**Observed instance:** `agent-exec-persistent-2eebf626418a-6509e183`, workspace `lively-bear-27` — a persistent container created by the agent during a verification run and left undeletable by it.

Severity raised medium -> **high**: this is not a tidy-up nuisance, it is create/remove asymmetric authority. The tool surface lets a caller cross a class boundary on the create side that it cannot cross on the destroy side, and nothing in the create response signals that the resulting object is outside the creator's destroy scope.

**Fix options, stated fully (still not picked for the operator):**

- **(a) Session-scoped remove** — an agent may remove a persistent container it created in its own session.
- **(b) Fail-closed on create** — `ContainerStartTool` refuses to create `persistent` unless the session also holds the remove grant. Nothing can be created that its creator cannot destroy.
- **(c) Explicit ephemeral option on start** — verification containers are born ephemeral.

(b) is the fail-closed option; (c) is the honest one (it makes the verification loop state its intent). (a) is the most permissive and needs the creator label to be trustworthy, which `ticket-bug-container-created-by-not-a-record-field.md` shows it currently is not.
