# ThoughtMachine -- Status

Updated: 2026-10-22, bookkeeping seat (chunk B). **The isolation wave is merged; the long tail is still indexed.**

**dev HEAD:** `75f51e25` (merge of #166 — docs: gen-47 KB write: KB_POLICY ruling
  + container-create-admission ticket correction)
**Landed since the Gen-47 reading, in order:** `d753c284` (#165,
  `fix/ws-dispatch-ownership` — landed at **twelve** commits @ `28b3576c`) →
  `40aa9084` (#163, macOS path-test / advisory-gate / version-revision tickets) →
  `75f51e25` (#166, gen-47 KB write). Chain on `dev`: `d60ffc92` → `d753c284` →
  `40aa9084` → `75f51e25`.
**In flight:** none known at `75f51e25`.
**Verified how:** the four SHAs were read from the local object DB; local
  remote-tracking refs read `origin/dev` = `75f51e25` and
  `origin/fix/ws-dispatch-ownership` = `28b3576c`. No `fetch` was run from this
  seat — remote refs are last-fetch state, not a live probe of `origin`.

*(2026-10-21 reading, kept because it was true when written: updated 2026-10-21,
Gen 47 seat — "isolation wave built and pushed, not yet merged"; **dev HEAD**
`d60ffc92` (events: scope the `event_log` query to the calling session and redact
message bodies, #162); **in flight** `fix/ws-dispatch-ownership` @ `51a87058`,
11 commits, pushed to origin, NOT merged.)*

## The generation in one line

The boundary is closing. **Nineteen PRs merged** (#143 → #162; no commit carries
#160) confining client-supplied paths, origins, approvals, deep links,
registry-resolved mount sources, the store layer, the event log and the global KB.
*(2026-10-22: twenty-two PRs, counting #163, #165 and #166.)*
On top sits an **eleven-commit branch**, unmerged, that makes the WebSocket
dispatch obey workspace ownership from *every* command — not just the one that
was tested. *(2026-10-22: merged as #165 `d753c284`; the branch finished at
**twelve** commits — `28b3576c` added the tests that assert the `security_response`
deny and prove they can fail.)*

## Waves merged this generation (#143 → #162)

| PR | Wave | SHA |
|---|---|---|
| #143 | Admission: container cap made class-aware | `4d95bc01` |
| #144 | Container ops protocol, KB policy, two tickets | `124477b7` |
| #145 | Gen-47 container-ops findings (renames, amendments, protocol) | `28948e5b` |
| #146 | Gen-47 pen-test findings — 11 filed (F11–F20, F22) | `7e6b4924` |
| #147 | `check_system`: `running_workers` scoped to the calling session | `d45cebb0` |
| #148 | `check-system`: version and containers always allowed | `97f4c117` |
| #149 | Close the global-KB write bypass; harden write target; tell the truth about the path | `9aa11790` |
| #150 | Global knowledge base read-only for agents (F24) | `37d62f6e` |
| #151 | Aggregate container envelope — design pass | `d2ce7736` |
| #152 | Default `mem_limit` 1 GB → 512 MB | `b8e2e607` |
| #153 | Server refuses to start on a non-loopback bind | `d776f454` |
| #154 | Vault: re-sync factory-owned seeds from shipped resources (content, not version) | `9d5ed869` |
| #155 | Isolation-audit ticket corpus | `9cd06b34` |
| #156 | Gen-47 refinement (mount-source, commit-hook gate, panel payload, stale hook doc, venv correction) | `4a57d370` |
| #157 | Store layer honours `workspace_id` — six fixes | `1b83692c` |
| #158 | Gen-47 refinement (commit-hook gate, pre-commit feat, presenter, `create_session`) | `9d2558db` |
| #159 | Confine client-supplied paths, origins, approvals, deep links | `0d9a7764` |
| #161 | Validate registry-resolved workspace paths before they become mount sources | `8efd798b` |
| #162 | `event_log` query scoped to the calling session; message bodies redacted | `d60ffc92` |

No commit on `dev` carries **#160** — either unmerged or renumbered. Recorded
rather than guessed; check `origin` before assuming a gap in the sequence.

*(2026-10-22: three further merges belong to this generation but are not in the
table above — #163 `40aa9084`, #165 `d753c284`, #166 `75f51e25`. The table was
not rebuilt; the header block carries their descriptions.)*

## In flight: `fix/ws-dispatch-ownership` @ `51a87058` (11 commits, unmerged)
*(merged 2026-10-22 as #165 `d753c284`; branch tip moved on to `28b3576c`,
twelve commits — the table below stops at `51a87058` as written)*

One idea, committed eleven times: **ownership is a property of the socket, and
every command must honour it.**

| Commit | What it closed |
|---|---|
| `a48d60bb` | Verify socket workspace before load/delete of a foreign session |
| `c94e665b` | `close_session` refuses a session outside the socket's workspace |
| `0821e4ec` | A workspace-bound connection cannot be re-scoped by `new_session` |
| `b7a852e9` | A bound socket keeps its workspace across `set_project` and `new_session` |
| `ce4bb1ca` | `list_sessions` only enumerates the socket's own workspace |
| `0f7dd27c` | Worker shutdown sweep scoped to the closing session |
| `d55a96d5` | Default-deny unasked security responses; stop the session list going silent |
| `a4c3296f` | One ownership rule for `apply_config` and `set_project`, realpath-normalised |
| `8817acf0`, `dfccdd44`, `51a87058` | Tests: attack the workspace boundary and the ownership rule from both commands |

The pattern worth keeping: the three `test(ws)` commits exist because the first
fixes were written *against the one command that reported the bug*. Each test
attacks a **second** command to see whether the rule holds there too. That is how
this wave found `apply_config`.

## Next

1. **Merge `fix/ws-dispatch-ownership`** — the isolation wave is built, tested,
   pushed; it needs the merge gate, not more code. *(2026-10-22: done — merged as
   #165 `d753c284`.)*
2. **Credential Resource W0/W1** — *unblocked and waiting.* Operator ruling
   (2026-10-21): deferring it was "already in the plan, not a derail".
   Recon: `working_docs/credential-resource-recon-findings.md`. Shape unchanged
   from Gen 46: vault store, purpose-named identity, four grains
   (`read`/`use`/`rotate`/`delete`), no value grain, proxy at the tool boundary,
   rotate/delete operator-only.
3. **Crosstalk audit** — must complete before **W2**. Its sharp end is the
   operator's own question: *can `CheckSystem` learn anything about another
   workspace?*
4. **Session ghost** — the audit re-dispatch blocker.
5. **Container 10-step ops protocol run** — carried forward from Gen 46, still not
   run. Protocol on disk: `working_docs/protocol-container-ops.md`.
6. **Build-path test** — the vault Dockerfile build path still has no automated
   guard.

**Everything else is in `brain_pour_2026-10-21.md`.** The operator's 2026-10-21
pour (~22 thoughts that map to no file) is indexed there with per-thought verdicts.
Consult it before proposing work; it is the authority on the long tail.

## Ruled this generation (do not re-litigate)

- **The dead `admission` branch is not a wrong-envelope hole.**
  `container_create.py:407` is provably unreachable — `AdmissionContext` has no
  production construction site. The envelope guarantee lives in
  `security_gate.resolve_container_config` (fail-closed three ways) and `admit()`
  has exactly four live call sites. Residual risk is *upstream / per-caller*, not
  by construction ⇒ **Medium hygiene, not P0**. Ticket corrected, conclusion stands.
- **Tracked KB files are drift, not policy — and `git rm --cached` is dropped.**
  The public/private split is deferred, not decided. See `docs/KB_POLICY.md` and
  `hygiene/kb-public-private-split`.
- **The `.githooks/pre-commit` deletion (`85035605`) rested on a false premise.**
  The "`core.hooksPath` is UNSET ⇒ dormant" reading came from a bare shell; the
  container path sets it. Recorded in `personal/bugs_and_fixes.md`.

---

# Previous session (Gen 46)

Updated: 2026-10-20, Gen 46 seat. **Container sprint closed; display-truth wave merged.**

**dev HEAD:** `44a638b8` (Display truth: policy drift direction, honest fix
  hints, pinned finding order, #142)
**In flight:** `fix/container-cap-class-aware` (container cap, class-aware —
  not yet merged) *(merged as #143 `4d95bc01` in Gen 47)*

## Waves merged since Gen 45 (2026-10-19 → 2026-10-20)

| PR | Wave | SHA |
|---|---|---|
| #132 | Sprint close-out (badge / spinner / created_by / stop-grace latency) | `48905d14` |
| #133 | Persistence finally-invariant + vitest serial workers | `54d5a0cb` |
| #134 | Resize preserves envelope + created_by origin | `7cebe2fc` |
| #135 | Recreate path preserves envelope + created_by | `db88f528` |
| #136 | Duplicate merge of #135's subject line | `11de8231` |
| #137 | **Worker obeys session grants — the fourth gate removed** | `4e96c32c` |
| #138 | Worker force-spawn: sweep scoped to the session; stop resurrecting stopped workers | `41ec021c` |
| #139 | Machine model doc + git identity | `7d0e3dc7` |
| #140 | Portable audit log path; audit failure never blocks execution | `d20ea879` |
| #141 | Route the two remaining `/tmp` audit writes through the sink | `25af48c4` |
| #142 | Display truth: policy drift direction, honest fix hints, pinned finding order | `44a638b8` |

## The fourth gate is gone (#137)

Workers carried a permission snapshot captured at spawn that acted as a
*cap*: `effective = min(spawn_snapshot, session ∩ ceiling)`. It appeared
in no doctrine, no UI, and not in the `permission_store` docstring. It
was asymmetric — a revoke took effect immediately, a grant needed a
respawn — and it violated Inv.1 and Inv.5. Removed in `4e96c32c`.

It was not vandalism: when workers were introduced permissions were not
disk-authoritative, and the snapshot was the only mechanism available.
The disk-authoritative rewrite made it obsolete. It outlived its reason.

Record: `operator_observations.md`, entry of 2026-10-20. **Do not
reintroduce it. Read the record before touching worker permission
resolution.**

## Display truth (#142)

The panel now shows policy drift with a direction, fix hints name the
honest remedy rather than a generic "ungranted", and finding order is
pinned so two runs of the same state produce the same list. The drift
banner false-positive from the #131/#132 sprint is closed.

## Next

1. **Container cap, class-aware** (`fix/container-cap-class-aware`) — merge.
2. **Container 10-step ops protocol run** — create / exec / stop / start /
   restart-running / restart-stopped / resize / remove / agent-remove-
   persistent. Protocol on disk: `working_docs/protocol-container-ops.md`.
3. **Build-path test** — the vault Dockerfile build path has no automated
   guard.
4. **Surface completion** — `feat/git-identity-ui`, then
   `feat/executor-image-provisioning`.
5. **Session ghost** — the audit re-dispatch blocker.
6. **Crosstalk audit** — must precede the Credential Contract.
7. **Credential Resource Contract** — the P1 unlock for V3. Recon at
   `working_docs/credential-resource-recon-findings.md`. Main's ruling
   (2026-10-17): vault store, purpose-named identity, four grains
   (`read`/`use`/`rotate`/`delete`), no value grain, proxy at the tool
   boundary, rotate/delete operator-only.

## Blocked

- **Audit re-dispatch** — blocked on the session ghost.
- **Credential Contract W2** — blocked on the crosstalk audit; it must
  precede the Contract, not follow it.

---

# Previous session (Gen 45)

Updated: 2026-10-18, Gen 45 seat. **Container sprint closing.**

**dev HEAD:** `cc58c32e` (Wave: container sprint close #131 merged)
**In flight:** PR #132 (Wave: container sprint close-out — badge fix,
  spinner, created_by label, stop-grace latency).

## Container Resource — CLOSED with annotations

The Resource Contract closing is *functional*, not *audit-clean*.
The five facts:

| # | Fact | Owner | Status |
|---|---|---|---|
| 1 Existence | `thoughtmachine/container_record/` | **OWNED** | clean |
| 2 Envelope | `security_gate.resolve_container_config` | **OWNED** | clean, verified fail-closed 3-way AND |
| 3 Lifecycle | `ContainerLifecycle.reconcile` | **ANNOTATED** | TTL exited-only; `ttl_remaining_s` dead field; resize is a known recreate (no longer silent) |
| 4 Control | agent tools + `workspace_container_action` | **ANNOTATED** | all four operator verbs work post-#132; agent refuses persistent (interim rule); note-edit is creation-only (P1); classifier-divergence filed (Inv.6); panel-cannot-show-deny-drift |
| 5 Observability | `ContainerStatusTool` + panel + audit | **ANNOTATED** | `CONTAINER_DESTROY` audit landed (F2); `created_by` label populated; `ttl_remaining_s` dead; two label bugs filed (`fact5-uptime`, `fact5-id-echo`) |

Full grid detail: `design_resource_grid.md`.

## Waves merged Gen 45 (2026-10-17 → 2026-10-18)

| PR | Wave | SHA |
|---|---|---|
| #123 | 10b: docker_executor move | `586aa2df` |
| #124 | Endpoint display funnel (4 commits) | `6d715876` |
| #125 | Panel container-action path (2 commits) | `badfa197` |
| #126 | Drop `container:remove` sub-grain (superseded by #127) | `fe6127a6` |
| #127 | Interim rule: operator-only persistent remove | `aa2673b4` |
| #128 | Container P0 fixes (7/7) | `9096ea51` |
| #129 | Display + destroy auditability + /tmp tmpfs | `d1e3a1ee` |
| #130 | Hot-fix: destroy-before-validate (P0 data loss) | `d5bfea0c` |
| #131 | Sprint close (6 fixes + drift diagnostic) | `cc58c32e` |
| #132 | Sprint close-out (badge/spinner/created_by/latency) | merged — see `48905d14` above |

## What the two-sided protocol + parallel audit found

The suite was green on **all** of these. The panel and the audit told us
they were broken:

- Panel action buttons (Stop/Start/Restart/Resize) — silent 401s
- Endpoint display — SAFE_DEFAULTS shown as effective for every workspace
- Resize — 404 after click (record reaped by resize route)
- Apply button — stayed blue after successful PUT
- Build hash — computed repo sources, not the vault Dockerfile
- `_audit("CONTAINER_CONFIG")` — logged raw permissions to `/tmp`
- TTL — exited-only; no running-container cap
- `free_use` label — beat fail-closed default
- **Resize** — accepted below Docker 6MB floor, destroyed container, 400→503
- **Silent recreate** — was the resize itself, not a background reaper
- **Panel banner** — showed `warn` as "older permissions"; real `deny` never
  surfaced (HTTP route uses `session_id=None`)

## Sprint-close tickets (filed, not blocking close)

- `bug/classifier-divergence` — migration `_derive_class_owner` doesn't
  delegate to `lifecycle_class_from_labels` (Inv.6)
- `bug/panel-cannot-show-deny-drift` — HTTP route can't compute
  `unavailable_reason`; real hazard invisible in the panel
- `feat/container-note-on-existing` — P1, creation-only notes today
- `design/ensure-container-refuse-sibling` — §9 fast-path mini-spec
- `feat/record-created-by-provenance` — Record field if ever needed
- `hygiene/build-log-length-metadata` — tool should return length separately
- `hygiene/fix7-blast-radius`, `hygiene/sandbox-vitest-cpu`,
  `hygiene/FakeManager-signature` — small cleanups
- `hygiene/executor-dead-code` — VCI dead branch + stale docstrings
- `hygiene/test-prod-audit-log` — test suite writes to prod audit log
- `hygiene/sweep-grace-window` — record-age grace window decision
- `bug/actor-not-in-destroy-audit` — thread `actor` through destroy paths
- `fact5-container-status-id-echo`, `fact5-uptime-seconds-semantics` —
  label bugs
- `bug/panel-remove-no-feedback` — two clauses (no-feedback + no-refresh-on-failure)
- `bug/admit-count-includes-recreated-record`, `bug/two-container-counters-disagree`
  — needs live repro
- `feat/executor-image-provisioning` — Delivery arc
- `feat/git-identity-ui` — surface for the `git_identity` config key (#139)
- `design-self-driving-session-prerequisites` — P1–P5
- `feat/workspace-deletion`, `hygiene/panel-polls-no-subscribe`,
  `hygiene/panel-host-bash-display`, `hygiene/capabilities-inert-panel-tag`,
  `hygiene/checksystem-allowlist-test-literals-drift`,
  `feat/dispatch-report-duration` (partially shipped),
  `bug/event-log-global-no-workspace-scope`,
  `hygiene/infra-onboarding-git-ls-files-correction` (fixed)

## Not up for re-litigation

- Security layer (path layer). Red-teamed, sealed.
- Four-layer intelligence model.
- Process rules in `working_agreements.md`.
- Resource Contract as named architecture.
- Permission model: intent vs domain (with scoping clause).
- Envelope is hard; Control inside a passing envelope is soft for
  workspace-owned persistent containers (2026-10-17 ruling; interim
  operator-only for agent).
- Worker permission resolution is `session ∩ ceiling`, full stop. No
  spawn-time snapshot (2026-10-20, #137).

## Session-end KB hygiene (Gen 45 notes, still open)

- `INFRA_ONBOARDING.md` §1 fixed (`find` not `git ls-files` note added).
- Worker sandbox pytest skips: baseline host 16 skipped vs sandbox 64.
  Absolute pass count differs; delta matches. Added to calibrations.
- Deferred: `personal/lessons_learned.md` (~125 kB) and
  `personal/bugs_and_fixes.md` (~99 kB) archiving;
  `operator_observations.md` split (~137 kB as of 2026-10-20).
