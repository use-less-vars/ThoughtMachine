# ThoughtMachine -- Status

Updated: 2026-10-26, Gen 51 close.

**Gen 48 close-out snapshot (historical):** **dev HEAD:** `cf10c358` (merge of #189 — `fix/census-burn-down-no-tool-wrapping`:
  burn down census letters (b) and (c), add registry accessors, fix the guard's
  vacuous scan) *(superseded 2026-10-25 by §Current state below — kept as historical record; see also the 2026-10-22 reading at lines 27–29 for the same convention.)*
**Landed since the Gen-47 reading, in merge order:** `75f51e25` (#166) →
  `cb5d2279` (#168) → `75d992cb` (#169) → `f24648e6` (#170) → `2fde54f8` (#171) →
  `6b392f44` (#172) → `4d6c608d` (#173) → `12fe3bbc` (#174) → `892d2780` (#176) →
  `a5dadaaa` (#175) → `b6382aeb` (#177) → `bc8c7700` (#178) → `5abd2e47` (#179) →
  `e7ff09b8` (#180) → `f08ae63a` (#181) → `9ef9cfd0` (#182) → `5ad00aec` (#183) →
  `09a06495` (#184) → `c0749fc7` (#185) → `3379d68c` (#187) → `531da1e2` (#188) →
  `b519532d` (#186) → `cf10c358` (#189).
  Two things about that chain are recorded rather than smoothed: **no commit on
  `dev` carries #167** (Gen 47 recorded the same shape for #160), and **merge order
  is not PR order** — #176 landed before #175, and #186 landed after #188.
**In flight:** none on `dev`. Three branches sit on top of it, all unpushed, all
  awaiting the operator's host terminal: `docs/gen48-onboarding-contract-tracked`
  @ `65aac9e2`; `docs/gen48-container-base-ticket` @ `11782051` (created on top of
  the first, so it stacks); `docs/gen48-status-and-handoffs` (this commit).
**Verified how:** `git rev-parse dev` = `cf10c35892c0145f2b00fb52c2296b668952b116`.
  Every SHA above was read from the local object DB of the `dev` first-parent
  chain, in the order the merges appear on that chain. No `fetch` was run from this
  seat — remote-tracking refs are last-fetch state, not a live probe of `origin`.

*(2026-10-22 reading, kept because it was true when written: updated 2026-10-22,
bookkeeping seat (chunk B) — "The isolation wave is merged; the long tail is still
indexed"; **dev HEAD** `75f51e25` (#166, gen-47 KB write); **in flight** none.)*


**Gen 49 W4-minimal — ruled, not yet shipped (2026-10-24).** Push
verb will ship on `SandboxedExecution`; `credential_inject=True` is NOT used
and git does NOT route through `host_run`. Decision 1 L33 amended:
"The plumbing is built; the population of `git_environment` is a separate
wave (`feat/credential-resolution`), which is the next thing dispatched
after W4 merges." Status line for this wave, verbatim: "Credential
injection plumbed for `host_run` callers; git path on `SandboxedExecution`;
port migration filed as `design/host-port-git-authorization`."
**Gen 49 housekeeping snapshot (2026-10-25) — read from git, not taken from the brief.** dev HEAD `e0670545` = #199 `feat/credential-w3-w4` (merge of W3b `54a302e2`, W3c `8fc3fbd8`, F-A `aacbbca0`). **PRs #190–#199 merged:** `d1fc8b82`, `b2a3cdf3`, `be0a5f87`, `476fc35f`, `7160f58b`, `10e4daf9`, `abcb80ee`, `e92f479f`, `7b4c2e96`, `e0670545`. **W4 code-complete; being committed in this PR:** 11 modified, 7 untracked (`git status`, 2026-10-25). The wave status line above — the block ending "port migration filed as `design/host-port-git-authorization`" — is re-affirmed verbatim and is **not** superseded by this note. Open state: "11 findings still awaiting a keep-or-delete ruling" (client claim); no-tool-wrapping census stands at 13 sites at park, 8 burned down in `cf10c358`, residual letter (a) recounted by nothing in CI.

## Current state — 2026-10-26, Gen 51 close

**Supersedes the 2026-10-26 Gen 51 mid-session block and the 2026-10-25 Gen 50 close (both kept below as historical record).**

**dev HEAD:** `392e6b7a` — merge of PR 211 (`docs/d4-caller-identity`); tree clean, nothing in flight.

**Auth Arc complete (D1–D4).**
- **D3 (PR 207):** `_ownership_refusal_ids` primitive + adapters.
- **D2 (PR 209):** `_bind_workspace` authority + WS handshake binding + 2 unaudited site closes.
- **D1 (PR 210):** `_origin_check_request` on all 37 non-GET routes + enumeration test + `/api/browse` guard.
- **D4 (PR 211):** Design note — caller identity as predicate's first operand.

**Delivery Wave queued.** The friend incident proved Layer 5 (fresh install) is the sharpest gap. Ruled: `feat/fresh-install-diagnostic` (startup banner + doctor) queues behind auth arc. `host_bash` default-off must surface at install time. Provider URL validation is partial.

**Tickets filed (PR 208):** SSRF (CRITICAL), logging-config (HIGH), macOS advisory (LOW).
**Hygiene filed:** `route-workspace-mutations-through-bind-authority`, `site-2543-bootstrap-bypass`.

## Current state — 2026-10-26, Gen 51 mid-session

**Supersedes the 2026-10-25 Gen 50 close (kept below as historical record).**

**dev HEAD:** `23a57a61` (merge of PR 209, D2 bind-authority).

**Merged since Gen 50 close:** #206 (KB close), #207 (D3), #208 (three tickets), #209 (D2).

**D3 (PR 207):** id-form primitive `_ownership_refusal_ids` at `bridge.py:308` — identity comparison, no registry lookup, None fails closed both sides. Path adapter `_ownership_refusal` at `:372`; session adapter `_ownership_refusal_by_id` at `:349` (D2's hook, zero call sites). Three collapses: `bridge.py:2327/:2346`, `server.py:2388`, `server.py:3096`. Two commits: `16e79d68` (code) + `fd9050f1` (ripple, 4 test files, +10/−0).

**D2 (PR 209):** `_bind_workspace(workspace_id, mode)` at `bridge.py:575` — central binding authority (None refused, unknown mode refused, both modes bootstrap unbound, equal no-op, bootstrap refuses rebind, switch delegates to `_ownership_refusal_ids`, never resets). WS handshake binding; three `register_by_root` bootstraps routed; site 1 (`server.py:2627`, inverted-guard) closed with reordered path-update; site 2 (`bridge.py:2506`, bootstrap-bypass) routed; seven tests (`86 passed` on fresh-worker re-run). Two commits: `52fbcb0d` (code) + `82200114` (four comments naming the unreachable-refusal assumption).

**Tickets filed (PR 208):** `bug/onboarding-ssrf-ambient-credentials` (CRITICAL), `bug/logging-config-unauthenticated-write` (HIGH), `hygiene/macos-advisory-check-triage` (LOW, triage in delivery wave).

**Hygiene tickets filed at this KB close, not dispatched:** `hygiene/route-workspace-mutations-through-bind-authority`, `hygiene/site-2543-bootstrap-bypass`. Both read D2's §Corrections classification as input.

**Classes and patterns earned (this PR, `working_agreements.md`):** `contract-change-fixture-ripple`, `audit-names-guards-flow-has-more-assignments`, `verification-accepted-without-reading`, `sha-pinned-blocks-go-stale-on-correction` — all Gen 51.

**Next: D1** — request-time form of the `--allow-remote-bind` boot-time declaration, with `origin_policy._origin_allowed` as default for state-changing routes. Then D4-note. Then credential-delivery.

**On disk only (gitignored):** `redteam-surface-complete.md`, `vault-ownership-sites.md`, `brief-d3-ownership-refusal.md` (§Corrections 13 items), `brief-d2-binding-authority.md`, `pr-body-d2.md`, `pr-body-three-tickets.md`, `pr-body-kb-close.md`.

### D1 wave — branch `feat/d1-origin-guard` (2026-10-26, Gen 51)

**Commit:** `f00aebe9` — `feat(security): refuse foreign-origin calls on all 37 non-GET routes` (15 files, +828/-46). Branched from dev `23a57a61`. Not yet pushed or PR'd at the time of writing.

**Mechanism:** `origin_policy._origin_check_request(request, route_name)` at `:86`, additive — `_origin_allowed` is untouched. absent origin/referer -> allow + log; loopback -> allow + log; anything else -> refuse + log. `route_name` has no default on purpose. 36 non-GET sites call it inline; `/api/vault/repair/apply` keeps the fail-closed `_origin_allowed`. `GET /api/browse` is guarded as well (filesystem read). Census: 81 routes = 44 GET + 37 non-GET; 39 guard sites = 37 non-GET + 2 guarded GETs.

**Tests:** `tests/integration/test_origin_guard_enumeration.py` — 5a AST presence 37/37, 5b OpenAPI parity (81 operations / 37 non-GET), 5c pinned-tuple resolution to source decorators, 5d four-route behavioural sample (POST-with-body / POST-no-body / PUT / DELETE -> 403). `4 passed`. `web_ui/backend/tests` unchanged at 153 passed / 5 xfailed. 12 pre-existing tests updated for the new calling convention across 3 files (keyword-form `request=` with a real Starlette Request; no MagicMock, whose truthy `headers.get()` would trigger a misleading 403).

**Disclosed, not fixed:** FastAPI validates the body before the handler runs, so a malformed body paired with a foreign Origin answers 422 rather than 403. Refusal is proven behaviourally on 4 of 37 routes; the other 33 are presence-only by design. 5a proves presence, not guard position — a separate read-only audit confirmed 37 of 39 guard sites are the first statement after the docstring, the 2 exceptions being vault-repair's deliberate lazy-import sites.

**Next: D4-note, then credential-delivery.**

## Current state — 2026-10-25, Gen 50 close

**dev HEAD:** `f69761b4` (merge of PR 205, `docs(gen50): design-auth-layer + manifest-revision ticket`).

**Merged this session, five PRs:**
- #201 — Gen 49 housekeeping (class-shapes, seat disciplines, MANDATORY_STOP 1c)
- #202 — conftest child-interpreter probe + pydantic layering ticket
- #203 — red-team wave 2: seven tickets (3 CRITICAL, 1 HIGH, 3 MEDIUM)
- #204 — two hygiene tickets (working-docs re-include, venv disclosure)
- #205 — `docs/design-auth-layer.md` + manifest-revision ticket + `.gitignore` negation

**Design pass closed.** `design/auth-layer` — D1 DECLARED, D2 MUST-BIND (transition), D3 `_ownership_refusal` wins, D4 design note. All four accepted by Main 40. **First code wave is D3:** collapse three re-spellings to `_ownership_refusal`, add id-shaped stub, invert three tests, defer `test_browse_root_allowed` to D1's wave.

**On disk only (gitignored, not committed):**
- `.thoughtmachine/working_docs/redteam-surface-complete.md` — 112 rows, C1–C10 errata, 3 new findings
- `.thoughtmachine/working_docs/vault-ownership-sites.md` — 46 rows, D1–D8
- `.thoughtmachine/working_docs/ticket-bug-onboarding-ssrf-ambient-credentials.md` — CRITICAL draft, awaits Main 40 review
- `.thoughtmachine/working_docs/ticket-bug-logging-config-unauthenticated-write.md` — HIGH draft, awaits Main 40 review
- `.thoughtmachine/working_docs/brief-d3-ownership-refusal.md` — D3 brief, awaits Main 40 review

**Open holds:** Main 40 reviews the D3 brief and the two draft tickets before they commit or dispatch.

## The generation in one line

Gen 48 **deleted a tool instead of patching it.** `PaginateTool` declared
`required_categories = ["filesystem:read"]` and then constructed and ran any class
in `tools.TOOL_CLASSES` on the caller's behalf, because the executor gated the
wrapper and never the tool the wrapper ran. Two controls were written first
(`0c125856`). Then the wave concluded the controls were the wrong shape and removed
the tool (`4a4bd692`), and the census that followed found the same wrapper pattern
in thirteen more places (#185, burned down in #189). Around that: `credential`
became the eighth permission grain (#179), and the last of the `workspace_id` scope
leaks closed (#171, #172, #175, #177, #178).

## Waves merged this generation (#168 → #189)

Twenty-two PRs. No commit carries **#167**, the same gap Gen 47 recorded for #160.

| PR | Branch | Merge SHA |
|---|---|---|
| #168 | `docs/gen48-bookkeeping` — macOS preflight coverage gap; `CAPABILITIES` §6 CI polarity corrections; `host_bash` asks twice under `ask`; worker panel drops tool calls (stale bus subscription, never retried); cross-test pollution | `cb5d2279` |
| #169 | `fix/worker-panel-payload-drop` — re-bind the per-worker event bus on respawn, bounded retry, visible degradation; carries the `EventBus.subscribe()` listener-leak ticket | `75d992cb` |
| #170 | `design/workspace-isolation-matrix` — design pass, doc only, not a gate | `f24648e6` |
| #171 | `fix/w0-credential-store-reconciliation` — credential store reconciled onto `vault_root`, global scope added | `2fde54f8` |
| #172 | `fix/open-sessions-workspace-bucketing` — open-session tabs bucketed by owning workspace | `6b392f44` |
| #173 | `fix/w1-session-sink-redaction` — GitHub fine-grained PATs redacted; secrets redacted on both session-sink write paths | `4d6c608d` |
| #174 | `docs/gen48-kb-carryover` — Gen-47 audit readers guide, corrections, stub-heading repairs | `12fe3bbc` |
| #176 | `fix/ci-docker-hub-rate-limit` — anonymous Docker Hub limits no longer break `unit-tests` | `892d2780` |
| #175 | `fix/w1-w2-working-doc-and-paginate-scope` — `WorkingDocument` `doc_id` contained; `PaginateTool` stops dropping tool context | `a5dadaaa` |
| #177 | `fix/w8-executor-workspace-path-authority` — executor config always wins over a caller-supplied `workspace_path` | `b6382aeb` |
| #178 | `fix/worker-panel-cross-session-unsubscribe` — per-worker unsubscribe guarded against foreign-session completions | `bc8c7700` |
| #179 | `feat/w2-credential-resource` — `credential` as the eighth permission grain, plus three audit events | `5abd2e47` |
| #180 | `docs/gen48-job-primitive-cluster` — tool-resource contract, worker job lifecycle, agent role control filed as **one cluster**, plus `hygiene/worker-silent-timeout` | `e7ff09b8` |
| #181 | `docs/gen48-paginate-gate-bypass` — the CRITICAL `required_categories` bypass ticket | `f08ae63a` |
| #182 | `fix/paginate-tool-gate-bypass` — wrapped tool authority re-gated; control-plane targets refused | `9ef9cfd0` |
| #183 | `docs/gen48-vault-ownership-manifest` — vault ownership manifest | `5ad00aec` |
| #184 | `fix/delete-paginate-tool` — **`PaginateTool` deleted**; the wrapper pattern is the defect | `09a06495` |
| #185 | `docs/gen48-no-tool-wrapping` — the no-tool-wrapping ticket with its 13-site census | `c0749fc7` |
| #187 | `docs/gen48-workspace-id-validation` — the unvalidated `workspace_id` ticket | `3379d68c` |
| #188 | `docs/gen48-session-attribution` — session/workspace attribution unresolved | `531da1e2` |
| #186 | `docs/gen48-paginate-residuals` — two residuals added to the paginate ticket | `b519532d` |
| #189 | `fix/census-burn-down-no-tool-wrapping` — census letters (b) and (c) burned down, registry accessors added, the guard's vacuous scan fixed | `cf10c358` |

## In flight (local, unpushed): the three close-out branches

All three are on top of `cf10c358`. None has a PR. None has been through CI.

1. **`docs/gen48-onboarding-contract-tracked` @ `65aac9e2`** — nine `.gitignore`
   lines and seven newly tracked KB files, so a seat can be onboarded from a clone.
   `MANDATORY_STOP.md` had never been in git. **The PR body must disclose that this
   contradicts `docs/KB_POLICY.md` as written** — see *Ruled this generation*, item 3.
2. **`docs/gen48-container-base-ticket` @ `11782051`** — a 76-line ticket. It was
   created at `65aac9e2`, so it **stacks on branch 1**; its PR should target branch 1
   or wait for branch 1 to merge.
3. **`docs/gen48-status-and-handoffs`** — this block and the Gen 48 → Gen 49 handoff.

## In flight: `fix/workspace-id-validation-and-d2-doctrine` @ `31d8b4c6`

Two things on one branch, neither reviewed nor in CI.

1. **The `workspace_id` validation fix.** `workspace_id` is validated at the
   `_workspace_dir` choke point; the single-segment rule is extracted to
   `thoughtmachine/validation.py` and shared with the credential validator; 52 new
   tests.
2. **The no-tool-wrapping correction.** The class rule is standing doctrine, but it
   is **not CI-enforced**, and there is no `tests/test_tool_no_wrapping.py` — there
   never was on this branch. The guard exists, parked and unactivated, at
   `.thoughtmachine/working_docs/tool_no_wrapping_guard.py`, wired into no test run
   and no workflow. The census is a point-in-time figure: 13 sites at park, 8 of
   those (letters (b) and (c)) burned down in `cf10c358` (#189); the residual is
   letter (a), recounted by nothing in CI.

## Next (Gen 49)

1. **Push the three branches.** Nothing else in this section can be verified until
   they exist on `origin`. The agent cannot push, open a PR, or read CI.
2. **Amend `docs/KB_POLICY.md`** — operator ruling needed, see item 3 under *Ruled*.
   Until it lands, the policy document forbids the tree.
3. **Add the registry membership invariant** — the one thing the container-base
   ticket actually asks for. Nothing today iterates `TOOL_CLASSES` to assert that
   every member overrides `execute` and that no underscore-prefixed class is a
   member. `_ContainerControlBase` is not abstract and would be accepted silently.
4. **Census phase 3** — B1: `GitWriteTool` into a non-tool module (39 of 91 closures
   call no `super()`); B2: `code_modifier_utils.py` into 11 free functions (0
   self-writes). Eleven non-test modules still import `TOOL_CLASSES` directly.
5. **The operator's 2026-10-23 dispatch shape**, already on record in
   `personal/handoffs.md`: scoping wave → credential/env/push waves → **the CI loop**
   (the first shape where the agent builds without the operator in the per-step
   loop) → job primitive → overnight runs. The CI loop depends on push capability
   landing; the waves before it are not optional.
6. **Client-claim corpus** — 11 findings still awaiting a keep-or-delete ruling.
   One of them (`--allow-remote-bind`) is now partly answered; see *Ruled*, item 5.

## Ruled this generation (do not re-litigate)

- **The wrapper pattern is the defect, not the wrapper's gate.** `PaginateTool` was
  deleted (`4a4bd692`) rather than gated a third time. A meta-tool that constructs
  from the registry on the caller's behalf is the shape, and the shape is wrong. The
  13-site census of the same pattern is #185; letters (b) and (c) are burned down in
  `d4bc1e35`.
- **Credential events are observed, not audited.** `CREDENTIAL_USE`, `CREDENTIAL_ROTATED`
  and `CREDENTIAL_DELETED` emit and are tested, but are **not** in `event_class_map`,
  have **no subscriber**, and are **not** routed to the tenant event log. Do not
  soften this to "audited" in any document until a subscriber exists.
- **The onboarding contract set is tracked deliberately — and the policy is now the
  stale document.** Seven KB files are tracked on purpose so a seat can be onboarded
  from a clone. `docs/KB_POLICY.md` still says the opposite in as many words: L110-111
  reads "**Do not add more.** A new file under `.thoughtmachine/knowledge/` stays
  untracked. No exceptions, no 'it's useful for PR review'", and the tier table marks
  both Internal and Diary as `.gitignore`. **This is a written-policy contradiction
  created on 2026-10-23 and it needs an operator amendment, not silent drift.** This
  also partly supersedes the Gen-47 ruling below it ("tracked KB files are drift, not
  policy"): the ten accidentally-tracked blobs are still drift, but the seven
  contract files are now intent. `git rm --cached` of the ten remains **dropped** —
  that half of the Gen-47 ruling stands.
- **`_ContainerControlBase` is not a registered tool.** The premise that prompted
  its ticket is false, verified twice by independent reads (one of them adversarial):
  `len(TOOL_CLASSES)` = `len(SIMPLIFIED_TOOL_CLASSES)` = 37, membership `False` in
  both, `get_tool_class('_ContainerControlBase')` → `None`,
  `from tools import _ContainerControlBase` → `ImportError`. No dynamic registration
  exists anywhere (`__subclasses__`, `inspect.getmembers`, `getmembers_static`,
  `pkgutil`, `entry_points`, `iter_modules`, bare `import *` — all zero hits). The
  real defect is that the base is **not abstract** (`__abstractmethods__` is
  `frozenset()`, `execute()` is a plain `raise NotImplementedError` with an empty
  message) and that **no registry invariant test exists**. Severity LOW.
- **`--allow-remote-bind` is a refusal-by-default with an escape hatch, not an
  invariant** (`d776f454`, #153). `_validate_bind_host()` exits 2 on a non-loopback
  host, but the flag waives it and prints a warning. The commit's own peer-address
  gate covers `/health`, `/api/health` and `/api/health/containers` only, and its body
  concedes it cannot stop proxied exposure. `/api/system/health` was left untouched.
- **The turn limit is orthogonal to run duration** (operator directive, 2026-10-23).
  The turn limit is operator-controlled and raisable. What the operator cannot
  control is run duration, and the cause is the **blocking tool call**, not the turn
  count. Raising the turn limit fixes nothing. The job primitive targets run
  duration. Two cheap partials first: name the return reason in the worker return
  path (`hygiene/worker-silent-timeout`), and on this side treat
  `elapsed_seconds ≈ timeout_seconds` as the signal to shrink scope.

# Previous session (Gen 47)

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
