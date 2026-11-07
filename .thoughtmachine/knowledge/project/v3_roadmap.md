# ThoughtMachine — V3 Roadmap

**Status:** active. Supersedes `project/battle_plan.md`.
**Owner:** Main Engineer.
**Updated:** at phase boundaries only. Not per-merge.
**Source of truth for:** branch sequence, ownership, phase gates.

---

## The frame

V3 is not a feature set. V3 is the moment the machine's promises become
**explicit, owned, and verified** — and the product surface sits on that
foundation instead of leaking through it.

Three pillars:

- **Provable.** Every promise has a test and an owner.
- **Comprehensible.** The machine is held in the head as a promise table,
  not as implementation.
- **Ownable.** Users see every container, workspace, and session. They can
  act on them. Agents leave notes. Every container has an owner.

Two tracks run in parallel:

- **Foundation (horizontal).** Claim Ledger, delivery path. Makes the
  machine provable. Does not block on features.
- **Product (vertical).** Container architecture, then V3 features. Makes
  the machine useful.

Foundation gates the product at checkpoints, not as a blocker. Build on
the layers that are already strong. Fix weak layers in parallel.

**The bottleneck is the operator's attention. Every decision in this
roadmap should reduce it.** Decide, report, adjust. Not: ask, wait, decide.

---

## Phase A — Container substrate

**Goal:** the last subsystem without a durable identity becomes strong.
Layer 3 (Subsystems) stops being "uneven."

**Why first:** every V3 feature sits on containers. Until containers have
a record, drift events, an admission gate, and lifecycle policy, layer 3
stays uneven and everything above it is built on sand.

| Order | Branch | Owner | Purpose | Status |
|---|---|---|---|---|
| 1 | `fix/start-drift-mutation` | Infra | No silent recreate on drift. Refuse-with-drift-detail. | in flight |
| 2 | `refactor/lifecycle-classes` | Infra | Four classes with per-class policy. Dissolves scattered `if type == "resource"` guards. Prerequisite for restart policy. | **landed** (`d7c4e5d`) |
| 3 | `feat/drift-events` | Infra | Drift as events. User-decidable. Boot-drift visible. Kills the silent kill. | **landed** (`a8a0b4f`) |
| 4 | `feat/restart-policy` | Infra | `unless-stopped` for persistent. Drift → stop + UI notification. No drift → restart. | not started |
| 5 | `feat/registry-transition` | Infra | Dual-write during transition. Legacy manager reads through record. Retirement is a later decision. | not started |
| 6 | `fix/notes-as-record-field` | Infra | Close the `container_notes.json` race. Small. | not started |
| 7 | `feat/record-lifecycle` | Infra | Record GC. `state`/`docker_id` refresh. | not started |

*Order = dependency order, not sequence.
`docs/container_subsystem_target_architecture.md` §9 is the build order.
The two tables do not contradict; they answer different questions.*

**Parallel — GUI container panel.** Spec first, then read-only build,
against the record REST API (`GET /containers`, `GET /containers/{id}`).
Write actions spec'd as API requirements; Infra implements after
lifecycle classes and drift events land. Owner: GUI.

**Workflow rule (adopted):** branches touching `infra/container_manager.py`,
`docker_executor.py`, `infra/container_registry.py`,
`infra/resource_container_manager.py`, `security/admission_gate.py`, or
`thoughtmachine/container_record/hook.py` get a **host** gate, not a
sandbox gate. The sandbox skips all `@needs_docker` tests.

**Exit criteria:** container subsystem has a promise row in
`machine_model.md` — claim, verification, owner. Written by Infra,
reviewed by Main, as part of the final merge of the phase.

---

## Phase B — Claim Ledger and delivery

**Goal:** make the machine's promises continuously verifiable. Close the
delivery-path gap. Runs parallel to Phase A.

| Order | Work | Owner | Purpose |
|---|---|---|---|
| 1 | Claim Ledger: registry, chokepoint protocol, findings store, dashboard | Infra (brief) | See `claim_ledger_design.md`. |
| 2 | Fresh-install smoke test | Agent executes; Operator sequences | Wipes a test vault, runs install, runs boot, verifies a session works. Closes the "install → boot → first run" gap. |
| 3 | Release ritual formalized | Main | Full host suite + fresh-install smoke + claim findings green = ship. Red = fix. No exceptions. |
| 4 | First three claims wired | Infra | `session_permissions ⊆ workspace_ceiling`; `every_container_has_a_record`; `no_vault_write_without_user_approval`. |

**Why parallel:** the smoke test does not depend on the container
architecture. It depends on the delivery path, which is broken now. The
vault bug lived here. The friend incident lived here.

**Exit criteria:** layer 5 (Delivery) has a promise row. First three
claims are green or surface findings. Release ritual is documented and
followed at least once.

---

## Phase C — Resource foundation

**Trigger:** containers stable, Claim Ledger seeded. Layer 3 strong.
Layer 5 strong or actively being fixed.

| Order | Feature | Owner | Purpose |
|---|---|---|---|
| 1 | Workspace base image setup | Infra | User-configurable via vault. Graceful Dockerfile error with fix plan. |
| 2 | Resource catalog: define / import / gate | Infra | Resources become first-class: name, permission set, Dockerfile, tool. |

**Completion condition:** a user can define a resource with a name, a
permission set, a Dockerfile, and a tool; the workspace can import it and
use it.

**Risk:** over-architecture. The six moves (target-arch §3) are correct,
but the record system must be used by the UI. Mitigation: ship the record
and a minimal UI in the same quarter. GUI's container panel is not polish;
it is the proof the record is real.

---

## Phase D — Resource surface

| Order | Feature | Owner | Purpose |
|---|---|---|---|
| 1 | Welcome session | GUI + Agent | New workspace → setup session that helps the user configure. |
| 2 | Resource Factory | Agent-led; Infra supports | A session that builds tools and resources from user briefs. |

**Completion condition:** a new user lands in a workspace with a welcome
session that helps them build their first resource end-to-end.

---

## Beyond V3 — moved to `v3_vision_full.md`

**Superseded.** The parking list that used to live here is now folded into
the six threads of `project/v3_vision_full.md` — the compass for work after
Phase B. That document also extends the sequencing below it: Phase C gains
two read-only investigations ahead of the resource foundation, and Phase D
gains credentials, worker async/join, modes redesign, and performance fixes.

Where the old list went:

- **Comm Gate** → thread 4 (Kind B). Design known; enforcement is a
  tool-layer concern, not a container feature.
- **Credential management** → thread 5 (Kind C). Credentials become a
  first-class resource.
- **Modes / save-as-default / worker config UI** → thread 6 (Kind C). Mode
  becomes a named preset; the five silent-edit layers come down.
- **MCP integration** → **no thread claims it.** Still unscheduled; the
  vision doc does not cover external MCP servers as resources.

Read `v3_vision_full.md`, not this section.

---

## Cross-cutting, later

- **Permission injection into LLM context** — post-V3. An "emotion
  engine" evaluating frequent changes, injected into LLM context. Note
  for organization; not scheduled.
- **Worker timeout / output-loss** — parking lot. Owner to assign.
- **Vault repair class list to KB** — KB agent report.
- **Doc hygiene sweep** — stale anchors, drift-as-events naming
  inconsistency, `4 create paths` naming throughout.
- **Kill-switch semantics** — principle, not feature. Enforced via
  admission gate + workspace defaults.

---

## Standing guardrails

- Record holds state only. No policy, no execution, no binary.
- The six components land incrementally, not big-bang.
- No new framework (no ORM, no event bus library, no K8s-style scheduler).
- The table is a map, not a gate. It tells us what's safe to build on.
  It does not tell us to stop shipping.
- Reproducer-first. No fix code before raw evidence.
- Briefs cite provenance (`file@commit`). Verify against HEAD before
  dispatching.
- Small focused commits. Exact file paths. Never `git add -A`.
- Never stage `.thoughtmachine/knowledge/` or `working_docs/`.
- Full host suite green before merge. `--no-ff` merges. No squash.
- Never `--no-verify` on real merges to `dev`.

---

## Checkpoint at every branch landing

At the end of every landed branch, Main asks and records in the parking
lot: **"What can I now touch?"** The container family is long. The trap
is waiting for a perfect moment. The working set evolves visibly.

---

## Claims per PR

Every PR description includes one line: `Claim added/changed: <text>`.
Every phase boundary includes a 15-minute `machine_model.md` review.
That is the entire mechanism. No new ceremony.

---

## What we are NOT doing

- Not rewriting `container_manager.py` from scratch. Refactor, not
  rewrite.
- Not replacing Docker. Those six components are within Docker's model.
- Not building an event bus. The event log is a JSON file per record.
- Not building a scheduler. Admission gate + lifecycle classes are
  enough.
- Not adding a database. Records are JSON files in the vault.
- Not committing to retire the legacy container manager. Dual-write is
  the transition; retirement is a later decision with stated criteria.

---

## 2026-10-31 — Wave ledger W1–W4 (Gen 55, `docs/w4-kb-braindump`)

This section is **additive**: the phase tables above are unchanged. It records where
the **W-series** actually stands, because until now the W-series existed only as
branch names and ticket filenames — there was no W ledger in this file, and this file
is the phase SSOT named by `working_docs/working_agreements.md`.

### Landed (merged; verified in the branch log)

| wave | branch | PR | merge |
|---|---|---|---|
| **W1 — session addendum, slices 1–2** | `w1/addendum-slices-1-2` | #260 | `4c700791` |
| **W1 — session addendum, slices 3–5** | `w1/slices-3-5` | #262 | `85b0ed1a` |
| **W1.5 — writer** | `w1-5/writer` | #263 | `64cfa5d2` |
| **W2 — vitest gating** | `w2/vitest-gating` | #264 | `7b60f68d` |
| **W2 — UI truncated landing page** | `w2/ui-truncated-landing-page` | #265 | `885c28e2` |
| **W2 — session rename staleness** | `w2/session-rename-staleness` | #266 | `3fe28f84` |
| **W3 — isolation fix** | `w3/isolation-fix` | #267 | `389b87b8` |
| **W3 — residuals** | `w3/residuals` | #268 | `6e01a004` |

**W3's audit did not close its findings, it recorded them.** The residuals are in
`working_docs/ticket-residual-w3-isolation-findings-not-fixed.md` and in the finding
table of `docs/workspace_isolation_matrix.md`. Read "W3 shipped" as "the audit is in
the repo", not as "the isolation work is done".

### Next, in order

**W4 — Auth Boundary (design pass first; no code until the decision is recorded).**
The scope is already measured. **F17**: of 85 HTTP routes, **82 have no caller gate**,
and neither does the whole `WS /ws` command surface. The only peer checks in
`web_ui/backend/` are on `/health` (`server.py:4073`), `/api/health/containers`
(`server.py:5910`) and `/api/system/health` (`health_routes.py:234`). Reachability
therefore rests on a start-up listen-address check, `_validate_bind_host`
(`web_ui/backend/server.py:4036`, called `:6224`), which is waived by
`--allow-remote-bind` (`server.py:6178`) and is transparent to a reverse proxy, a
port-forward or an `-L` tunnel in front of a loopback bind. Recorded, not fixed:
`docs/workspace_isolation_matrix.md:340` (sweep at `:237`, rationale at `:166`).
The decision itself is pre-existing and unfilled:
`working_docs/ticket-design-auth-layer.md` — two ruled candidates, (a) a token per
router entry, (b) formalise localhost-only — status *open — placeholder. Do not
start this.* W4 is **not** "pick (a) or (b) and code it"; it is "run the design pass
and record which". Adversarial gate before the boundary merge:
`ticket-hygiene-adversarial-gate-before-boundary-merge.md`.

**Worker Lifecycle Design Pass.** The parking-lot line above ("Worker timeout /
output-loss — owner to assign") now has two tickets on the other side of the hole:
`ticket-bug-worker-hard-timeout.md` (the hard-deadline fallback exists in
`tools/workspace/worker_timeout.py` and **has zero callers** — workers are daemon
threads, so nothing can stop a wedged one) and `ticket-feat-stop-cascade-detach.md`
(**no ownership edge exists between workers at all**: `_action_spawn` records no
parent, so stop cannot cascade and a spawned worker cannot outlive its parent by
rule, only by luck). These are one design question, not two patches.

**Resource Composition Design Pass.** `ticket-design-tool-resource-contract.md` plus
the resource-container cluster
(`ticket-bug-resource-containers-compete-with-workspace-cap.md`,
`ticket-bug-resource-overlay-not-rebuilt-with-runtime.md`,
`ticket-bug-container-restart-does-not-reassert-resource-limits.md`).

### Caveat on this file

`.thoughtmachine/knowledge/project/**/*.md` is gitignored (`.gitignore:221`), so this
section is **not visible in any branch and will not appear in a PR**. The
branch-visible vehicle for this pointer is `project/operator-dashboard.md`.

## 2026-11-06 — V3 wave plan W11–W14 (Gen 60 handoff, operator's vision)

## 2026-11-06 — Provenance: the **wave intent** below is the operator's `[O]...

Provenance: the **wave intent** below is the operator's `[O]`. Where engineering reality already constrains a wave, it is tagged `[V]`. Do not treat an `[O]` line as a spec — it is the destination, and the design pass for each wave is still owed.

### W11 — Resource Composition
- **Dynamic imports**: resources are resolved/loaded at use time rather than being fixed at session construction.
- **Run locations**: a resource declares where it executes — **Docker** vs **Host** — as a first-class axis, not an implicit consequence of mode flags.
- **The Bins**: granular permission grains instead of today's coarse categories — the canonical example is that `git: push` and `git: read` must be *separately grantable*, so an agent can read history without being able to rewrite shared branches.
- Existing footholds `[V]`: KB `design_permission_bins_shape` (2026-10-09) and `design_resource_grid` / `design_resource_contract` already sketch the shape; branch `docs/w11-bins-design-doc` exists. **Do not re-derive the model — reconcile with those docs first.**
- `[V]` constraint: the current gate is ten legacy keys (`git_read`, `git_write`, `container`, `network`, `system`, `execution`, `mcp`, `git`, `filesystem`, `host_bash`) while the UI shows six catalog entries — a Bins migration has to fix that key-set mismatch, not inherit it.

### W12 — Worker Instances
- **Make workers physical, visible entities**: an instance is a thing with an id, a lifecycle and a face in the UI, not a transient thread behind a name.
- **Sleep / Join as async tools**: a worker can be parked and resumed, and the spawning agent can *join* (await) it — this replaces today's blocking spawn as the default pattern.
- **Fix the worker token reuse bug** `[V, reproduced live 2026-11-06]`: `spawn(force=true)` rehydrates the previous transcript from disk, so a worker can be born already over `critical_threshold_tokens` (observed 92k and 99,298 → every tool refused with `not allowed. Reason: token`, zero commands run, and it answered from stale memory instead of failing). `SummarizeTool` genuinely repairs it (→ 16,660 tokens, tools restored in 15 s), so the fix is to **auto-compact or refuse at spawn**, and to stop reporting a post-prune `token_usage` that hides the saturated start.
- `[V]` existing surface: `Worker` currently exposes list/spawn/check/query/stop/submit_query/job_status/join/wait_for_job/pause/reset; branching already exists at `feat/worker-instance-ui`, `feat/worker-tray-truth-fields`, `feature/worker-cap-reset-continue`.

### W13 — Credentials & MCP
- **Secure vault injection**: credentials are handed to a tool/container at the moment of use, scoped to that call — never exported into an environment a worker can dump (`env | grep` today reveals only `HOSTNAME`).
- **Network allowlists**: gate the internet per workspace/per tool. Today the container simply has no route worth speaking of, which is not the same thing as a policy.
- `[V]` footholds: KB `design_credential_resource` (2026-10-24) and `personal/w2_credential_closeout`; the vault already has schema + drift detection + repair, so this wave is about *injection and egress*, not storage.
- `[V]` note for whoever takes it: the agent container cannot reach Docker at all (no CLI, no `/var/run/docker.sock`), so egress/allowlist work **cannot be validated from inside the sandbox** — it needs a host-side test lane.

### W14 — Tima rebrand
- The final naming migration (ThoughtMachine → Tima). Purely mechanical but touches everything: package names, vault paths, env vars (`TM_*`), UI strings, docs.
- `[O]` **Ruling: last.** Do not start it before W13 lands; a rebase across the W11–W13 surface would be unmergeable, and a half-renamed `TM_*` env surface is a boot-hang class bug (see status: `TM_SKIP_FRONTEND_BUILD`).
- Sequenced order for the next Main: **W11 → W12 → W13 → W14**, with the boot-hang and pagination fixes (status §Owed) taken *before* W11, because both waves land more UI and a 35 s boot tax is paid on every test cycle.


