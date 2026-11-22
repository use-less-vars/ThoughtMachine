State measured at `dev` = ea59dcda on 2026-11-22.
Supersede procedure: when the four properties are next scored, a NEW dated record replaces this one. This file is never edited in place; a dated record that claims to be current is the defect it documents.
Status: recorded, not enforced. §1 is the measurement.

## 0. What this file is, and is not

Not the architecture (`docs/ARCHITECTURE_MACHINE_MODEL.md`). Not the thesis or the arc (`docs/host-port.md` §1, `.thoughtmachine/knowledge/project/machine_history_and_vision.md`). Not the graded ambition (`vision_ledger.md`). Not the invariant table (`docs/MACHINE_INVARIANTS_AND_LORE.md` I1–I14): duplicating a table is how two tables start disagreeing.

It is the dated scoring of four properties (F1–F4) and the direction those four unblock. Where this file disagrees with any of the artefacts above, they are the requirement and this file is the gap.

## 1. Four properties, scored

| property | claimed by | verifier | state | what would make it true |
|---|---|---|---|---|
| **F1 — what the operator SEES must not lie about permissions; the display must not out-claim the gate.** | #435 (`a82f07f1`) | UI tests assert rendered rows from a supplied payload — a proxy. Degrade paths: `web_ui/backend/session_routes.py:433`, `web_ui/backend/workspace_routes.py:1261`. `web_ui/backend/server.py:34` states there is no auth layer. | **NOT ENFORCED** | a test that fails when the display asserts a capability the gate would refuse, plus a named refusal at every degrade path instead of an empty value |
| **F2 — the agent must never execute on the host outside a named gate.** | I11, `thoughtmachine/host_port.py` | `tests/test_host_port.py:326` and `:613`, `tests/security/test_git_no_host_fallback.py:166`, `tests/security/test_git_switch_bites.py` — the property is asserted on the GATED path only. | **PARTIAL** | an enumerating test over every host-executing site. The universal claim is documented false in this repo: `MACHINE_INVARIANTS_AND_LORE.md` §10 records `tools/mcp_client.py:226-240` bypassing the port and roughly ten other `import subprocess` sites, and `tests/test_tool_import_declaration_match.py` catches four static forms — it cannot see reach built at runtime. |
| **F3 — the kill switch must be absolute.** | Law III (`docs/gen64-laws.md`), I11; the switch is `allow_host_resources` | `tests/security/test_git_switch_bites.py` asserts the switch BITES rather than that a flag is read — the strongest shape in this repo; `tests/test_host_port.py:613` asserts absent, missing and malformed config resolve to OFF. | **PARTIAL** | proof that every host-executing site consults the switch. "Bites where tested" is not "absolute". |
| **F4 — one workspace's authority must never reach another.** | no invariant: I1–I14 say nothing about workspace authority isolation | read path: `ContainerStatusTool._workspace_ownership_refusal`, `tests/test_wave9c_container_scope.py` (5 tests). delete path: `tests/test_container_remove_scope.py` (8 tests) on branch `fix/delete-scope`. | read **ENFORCED**; delete **ENFORCED on the branch, not yet on `dev`** | merge `fix/delete-scope`; then state F4 as an invariant; then the same enumeration for every other destructive verb. |

**(a) The mutation finding — the executor pin is a proxy.** Removing the executor-side workspace pin does not turn the tests red, because `agent/core/tool_executor.py` already injects `workspace_id` inside the gate branch. What genuinely goes red is the ownership refusal and the `SkipJsonSchema` exclusion. A test that cannot be made to go red is labelled a proxy wherever it is cited — including in this table, about my own fix.

**(b) F4 is named by no invariant.** The gap is documentary as well as technical: a reader of I1–I14 alone would conclude that the delete path needs no scoping.

**(c) Degrade-to-empty is the concrete F1 lie.** When the gate cannot answer, the display shows empty or neutral values, so empty, denied and unknown render identically. The machine is not lying about a value it holds; it is lying by showing a value where it holds none.

## 2. What "done" means for this envelope

Four conditions per property: a test observed red before the fix and green after, on the same tree; exercised through the real gate rather than a proxy; a negative control, because every negative needs a positive or it is not a result; and, for a universal claim, a test that ENUMERATES every site.

**The stop rule.** When all four are red-then-green, this envelope is provable and this queue ends. The next unit of suspicion then goes to proving that a gate catches a REMOVED enforcement site — not to adding a fifth gate. Precedent: `4e96c32c` (PR #137) removed a fourth gate rather than adding one, because the authority already existed elsewhere.

## 3. The anti-rot rule

- Every claim carries its verifier — a test path, an invariant id, or a `file:line` — or it is tagged `[N]` for narrative.
- Dated records are superseded, never edited. An edited record cannot show what changed.
- The arc and the invariant table are cited, never duplicated.
- Volatile numbers are dated in place or not stated.

The repo has already paid for the absence of this rule. A brief carried `scripts/gen_handoff.py --check` as measured; the script has no `--check`. `.thoughtmachine/knowledge/project/architecture_invariants.md:48` cites `security/security_gate.py:1360-1361` for a statement that now sits at `:1609`. Reported but not re-executable, because the ref it named was deleted: a merge-base run that scored a branch as off-dev [N].

## 4. The feature frontier

The resource class is a closed set of one: `RESOURCE_REGISTRY` is `{git}` (`agent/config/defaults.py:275-277`), `ADMISSION_IMAGE_ALLOWLIST` is two images (`:187`), every other image is `Deny(image_not_allowed)` (`security/admission_gate.py:435-440`), and agents are refused resource containers at listing and at request (`infra/container_registry.py:489-491`).

The chain, scored: a solver wrapped as a resource — REFUSED at the image allowlist; attached to a workspace — creation exists, no attach surface; bound to credentials — designed, not built in code; usable by an agent — refused by design; lifecycle — operator-owned, and that must stay.

The principle that keeps F3 intact while the office gets tidier: **agent USE is not agent CONTROL.**

[N] The credential and resource design docs were not read when this was written. Where they disagree with this record, the docs are the requirement and this record is the gap.

## 5. The near queue

One PR, one test, one receipt each.

1. Merge `fix/delete-scope`.
2. Reclamation keyed to EVIDENCE, not to session death. No session-close event exists: `SESSION_STOP` and `THREAD_FINISHED` are declared in `agent/events.py` and never emitted, and `WORKER_COMPLETED` is emitted with no consumer in `agent/`, `tools/`, `infra/` or `thoughtmachine/`. So reclamation cannot key on it without inventing both the event and a poller. The criterion is `last_used_at` on the record plus a CLAIM that is a LEASE WITH A HEARTBEAT, never a boolean: a boolean claim held by a dead process blocks reaping forever, and a signal-0 liveness probe reads a zombie as alive. That is the defect just fixed in the kill path (#441).
   The motivating defect: the cap is six per workspace (`agent/config/defaults.py:165`); the agent's own code-execution container is created as `LIFECYCLE_PERSISTENT` (`infra/containers/executor.py:999`) and consumes a slot; `ContainerRemoveTool` refuses persistent containers; the admission denial instructs the agent to use that very tool; and no production code calls `ContainerRemoveTool` at all.
3. The panel subscription. `CONTAINER_LIFECYCLE` is emitted (`agent/events.py:94-96`) and nothing under `web_ui/backend/` mentions it, which is why a full page reload is needed before a new container appears.
4. This record's own supersession.

## 6. Open decisions

- `workspace_id` is injected at two sites and `ws_id` is shadowed later in the same function. Harmless today; a reason to centralise on one pin site.
- `created_by_session` is AUDIT, not authority. There are three ownership axes, not two: a persistent container belongs to the WORKSPACE, an ephemeral one to the SESSION, and the code-execution container to the WORKER. Naming them is part of the direction.
