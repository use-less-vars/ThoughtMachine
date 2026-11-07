# Ticket: bug/permission-refusal-names-session-not-both-layers

**Filed:** 2026-10-20
**Source:** gen-47 container-ops run — dispatched finding F11 (orchestrator summary).
**Severity:** medium — operator-actionable misdiagnosis, no security exposure.
**Status:** filed, not dispatched.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` — read in full (384 lines). `## Findings` (L318) carries **F1–F10 only**; no Appendix A/B, no F11–F22 exists in the file. F11 therefore has **no citable report line numbers**; content is the dispatch summary verbatim.

## Defect

Two permission layers can disagree: the workspace config grants `container: allow`, the session layer holds `container: banned`. The resolution is correct (most-restrictive wins — the session ban applies), but the refusal text names **only the session layer** (`session_permissions['container']`). An operator who has just edited the workspace config to grant access reads a refusal that points at a different layer, concludes the edit did not take effect, and re-edits the same key.

The refusal is not wrong — it is uninformative about *why*: it reports one input, not the conflict. A two-layer decision that surfaces one layer is indistinguishable, at the message level, from a single-layer denial. The operator cannot tell "your grant was not read" from "your grant was read and overridden".

`infra/container_manager.py:4289-4295` is an instance of this shape (`refusing to build: session container grant denied (read it in the session permission config: session_permissions['container'])`) — the workspace layer that must also have been consulted does not appear in the message.

## Fix shape (not written)

When a refusal is produced by the effective-permission computation, render the decision, not the input: name both layers, the value each held, and which one won and by what rule (e.g. `workspace=allow, session=banned -> denied (most-restrictive wins)`). One formatting change at the refusal sites; the computation already holds both surfaces. Do not soften the refusal or add an override path.

## Bearing

Low blast radius, high operator cost: this is the class of message that turns a five-second config check into a debugging session, and it is the same family as `ticket-escalation-reword.md` (messages that state a fact but not the actionable one). Worth fixing wherever the effective-permission object is already in scope rather than per-call-site.
