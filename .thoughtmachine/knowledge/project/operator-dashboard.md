**INTERIM — STALE AT 4c700791 — DO NOT TRUST AS COMPLETE UNTIL chore/ticket-front-matter-backfill CLOSES**

**Partition:** 197 = 10 closed + 3 superseded + 148 open + 36 unclassified (40 strict regex). **as of 4c700791**

**INTERIM — STALE AT 4c700791 — DO NOT TRUST AS COMPLETE UNTIL chore/ticket-front-matter-backfill CLOSES**

# Operator dashboard

**Status:** **197** `ticket-*.md` files on disk. The buckets below are an explicit partition —
every file is counted exactly once, and the four file buckets sum to the total:

| bucket | files |
|---|---|
| closed with evidence — the ticket's own `Status:` line opens CLOSED / SHIPPED / FIXED / RESOLVED | **10** |
| superseded by merge, or retired — `Status:` opens SUPERSEDED / RETIRED | **3** |
| open — diagnosed, plan known, work unstarted | **148** |
| stale — **no `Status:` line in the file at all** | **36** |
| **total** | **197** |

Five tickets counted in `open` were **filed by bookkeeping passes** —
`ticket-bug-state-bridge-engineer-mode-deletes-custom-prompt.md`, an operator report that had no
file on disk; `ticket-chore-ticket-front-matter-backfill.md`, the queue entry this page's own
"Interim snapshot" note refers to; and three filed by the Gen 55 onboarding pass —
`ticket-hygiene-flaky-supervisor-soft-timeout.md`, `ticket-docs-status-md-stale.md` and
`ticket-docs-dashboard-state-of-play-stale.md`. That is why the total is 197, not the 192 measured
before the pass; none of the five is yet placed in a body section below.

`non-ticket-rows`: **1** — the committed `.gitignore` merge conflict under `Unknown / needs
investigation`. It is a page row, not a ticket file, so it sits outside the 197. The open set is
overwhelmingly *diagnosed*: the plan is known and the work is unstarted. **8 rows lack a diagnosis**
— they are the only ones under `Unknown / needs investigation`, and **7 of those 8 are tickets**.
**Updated:** 2026-10-31 (Gen 55 onboarding) — counts re-measured; body prose from Gen 54.
Built from the `Status:` line of every ticket,
`project/core.md` (C1-C9 + §0.1), and the newest section of `project/status.md`.
**Nothing here was executed.** No fix was attempted while writing this page.
**Interim snapshot — how the counts were obtained:** every number here is scraped from each
file's `Status:` **prose** line. Nothing in a ticket is machine-read except that one line, and **36
files do not have one**, so their disposition cannot be scraped at all — they are counted as `stale`
rather than guessed at. A front-matter backfill (a parsed `status:` field per ticket, this page
generated from it) is queued and has not started. Until it lands, re-derive the counts before
quoting them: the numbers are accurate to the scrape, not to the second.

## State of play (read this first)

- **Branch:** `fix/phantom-config-notification`, HEAD `6399efb4` — *"fix(agent): stop base_url
  spelling variants forcing a restart and a phantom notification"*.
- Known: URL normalisation bug in agent/core — fixed in `6399efb4`, awaiting push. Do not panic.
- **Two commits sit unpushed locally on this branch. CI has not run on them.** Nothing has
  validated them but the local gate, and no reviewer has seen them.
- The branch's parent is `fbdef41b` (merge of PR #255, the sysprompt implementation brief), so
  `project/status.md`'s "dev HEAD `12bd6408`" (2026-10-28) is stale — the chain has moved on.
- **PR / CI state: UNAVAILABLE OFFLINE.** No network from this seat. There is no PR status to
  report, no CI conclusion to report, and none is guessed at on this page. If you need it, you
  have to look at the remote yourself.
- **First action:** push the two local commits, then read CI on them. Everything below is
  unaffected by whether they land.

## We know what this is and we have a plan

### Security — unauthenticated surfaces, origin checks, ambient credentials

- The API accepts cross-origin browser traffic (`Access-Control-Allow-Origin: *`) — `ticket-bug-cors-wildcard-exposes-api-to-browser-origin.md` → tighten to an allow-list; finding verified.
- WebSocket handshake skips the CORS/origin check the HTTP routes have — `ticket-bug-ws-handshake-bypasses-cors-origin-check.md` → apply the same origin check; verified.
- WS security responses allow anonymous escalation — `ticket-bug-ws-security-response-allows-anonymous-escalation.md` → bind to the handshake identity; verified.
- WS `set_project` bypasses vault confinement — `ticket-bug-ws-set-project-bypasses-vault-confinement.md` → route through the vault path guard; verified.
- WS commands are unauthenticated — `ticket-bug-ws-commands-unauthenticated.md` → gate the command surface; verified.
- Session delete ignores workspace scope — `ticket-bug-session-delete-ignores-workspace-scope.md` → bind to `_bind_workspace`; verified.
- Provider API keys can be read unauthenticated — `ticket-bug-provider-apikey-read-unauthenticated.md` → redact + gate; verified.
- Logging config is writable unauthenticated — `ticket-bug-logging-config-unauthenticated-write.md` → gate the write; verified (draft).
- Dockerfile write is unauthenticated — `ticket-bug-dockerfile-write-unauthenticated.md` → gate the write; verified.
- Worker blueprint prompt write is unauthenticated — `ticket-bug-worker-blueprint-prompt-write-unauthenticated.md` → gate the write; verified.
- Prompt injection reaches an unauthenticated surface — `ticket-bug-prompt-injection-unauthenticated.md` → treat injected text as data at the boundary; verified.
- Onboarding test-connection is SSRF with ambient credentials — `ticket-bug-onboarding-ssrf-ambient-credentials.md` → block private ranges; verified (draft).
- Domain allowlist is an inert control (does nothing) — `ticket-bug-domain-allowlist-inert-control.md` → make it enforce or delete it; verified.
- The unauthenticated-route list stops at `/health` and is a pointer, not a census — `ticket-bug-unauthenticated-routes-beyond-health.md` → finish the route census.
- Workspace/vault isolation is a design question, not a bug — `ticket-security-workspace-vault-isolation.md` → ruling needed before code.
- Commit-hook gate is not installed — `ticket-security-commit-hook-gate-not-installed.md` → install it; verified.
- The commit-hook gate is also neutralised by the agent's own git tool — `ticket-bug-commit-hook-gate-neutralised-by-agent-git-tool.md` → close the bypass; verified.
- Auth layer is an open placeholder — `ticket-design-auth-layer.md` → design first; nothing depends on it yet.

### CheckSystem, and every global / unscoped read

- Event log is global with no workspace scope — `ticket-bug-event-log-global-no-workspace-scope.md` → scope at the writer (re-scoped already).
- Event log leaks across sessions — `ticket-bug-checksystem-event-log-cross-session-leak.md` → scope the reader too.
- Running-workers reads are global — `ticket-bug-checksystem-running-workers-global-leak.md` → scope by workspace.
- Vault status is reported unscoped — `ticket-bug-checksystem-vault-status-issues-unscoped.md` → scope it.
- `/health` reports running workers, bypassing CheckSystem — `ticket-bug-health-route-running-workers-bypasses-check-system.md` → one scoped source.
- The allowlist fails *open* when set to `none` — `ticket-bug-checksystem-allowlist-fails-open-on-none.md` → fail closed.
- Containers query was denied by the allowlist (largely resolved — version/containers now permitted) — `ticket-bug-checksystem-containers-denied-by-allowlist.md` → confirm the remaining denials.
- Version reporting reads the loaded revision, not HEAD — `ticket-bug-checksystem-version-reports-loaded-revision-not-head.md` → doc-only fix; the capture is deliberate.
- Allowlist test literals drift from the real vocabulary — `ticket-hygiene-checksystem-allowlist-test-literals-drift.md` → generate the list.
- No payload fingerprint on CheckSystem responses — `ticket-hygiene-checksystem-payload-fingerprint.md` → add one; cheap drift detector.
- Presenter session store is unscoped — `ticket-bug-presenter-session-store-unscoped.md` → scope it; verified.

### Session / workspace attribution

- Creating a session drops the workspace id — `ticket-bug-create-session-drops-workspace-id.md` → carry it through; verified.
- Loading a session pivots the workspace — `ticket-bug-load-session-workspace-pivot.md` → pin the workspace; verified.
- Session-to-workspace attribution is unresolved — `ticket-bug-session-workspace-attribution-unresolved.md` → ruling on the model first.
- Workspace id is unvalidated at path construction — `ticket-bug-workspace-id-unvalidated-at-path-construction.md` → validate before joining.
- Workspaces with no workspace configured get permissive defaults — `ticket-no-workspace-permissive-defaults.md` → fail closed instead.
- API-created workspaces never get `cfg["root"]` and stay host-bash-ish — `ticket-workspace-create-missing-root-write.md` → write `root` at creation.
- `_canon_path("root")` returns `""` and collides with the locate-root label — `ticket-vault-canon-root-empty-path.md` → fix the canonicaliser.
- An agent's vault view is not the system's vault — absence is not proof — `ticket-agent-vault-visibility-scope.md` → make scope explicit in the answer.
- Worker stop/reset is scoped to the session, not the worker — `ticket-bug-worker-stop-reset-unscoped-to-session.md` → scope to the worker; verified.
- Two workers in one workspace can collide on name — `ticket-bug-worker-name-collision-same-workspace.md` → unique key.

### Container truth, accounting and identity

- Turn-start reap kills a *running* container — `ticket-bug-container-turn-start-reap-kills-running.md` → fix; **confirmed 2/2 live**, highest-value container bug on the list.
- Remove from the UI silently no-ops — `ticket-bug-container-remove-ui-no-op.md` → folds into the Wave 10a container-control work.
- Admission count includes recreated containers — `ticket-bug-admit-count-includes-recreated.md` → one live repro first.
- Two different container counters disagree — `ticket-bug-two-container-counters.md` → collapse to one authority.
- Stop acknowledgement is optimistic (UI says stopped before it is) — `ticket-bug-container-stop-ack-optimistic.md` → one live repro, then ack on truth.
- Restart does not re-assert resource limits — `ticket-bug-container-restart-does-not-reassert-resource-limits.md` → re-apply on restart.
- The resource overlay is not rebuilt with the runtime — `ticket-bug-resource-overlay-not-rebuilt-with-runtime.md` → rebuild at start.
- Resource containers compete with the workspace cap — `ticket-bug-resource-containers-compete-with-workspace-cap.md` → separate budgets.
- The git resource container ignores the workspace container limit — `ticket-bug-git-resource-container-ignores-workspace-container-limit.md` → count it.
- Limit errors suggest a remedy the operator cannot perform — `ticket-bug-container-limit-error-remedy-unactionable.md` → honest error text.
- An agent can create a persistent container it cannot remove — `ticket-bug-agent-creates-persistent-container-cannot-remove.md` → remove must outrank create.
- `created_by` is not a record field — `ticket-bug-container-created-by-not-a-record-field.md` → add it to the record.
- Recreate loses the original `created_by` — `ticket-bug-recreate-handler-created-by-origin-lost.md` → carry it across.
- The note is mutable and lives on the record — `ticket-bug-container-note-is-mutable-and-on-record.md` → make notes append-only or off-record.
- Runtime commit is server-scoped on container records — `ticket-bug-container-record-runtime-commit-server-scoped.md` → pin per record.
- Removal audit is unreliable and unattributable — `ticket-bug-removal-audit-unreliable-and-unattributable.md` → fix both halves together.
- The actor is missing from the destroy audit — `ticket-bug-actor-not-in-destroy-audit.md` → record the actor.
- Sweep removal emits no event and issues no stop — `ticket-bug-sweep-remove-no-event-no-stop.md` → emit + stop.
- `docker rm` vs explicit stop are inconsistent in the sweep — `ticket-bug-container-sweep-rm-vs-explicit-stop.md` → one path.
- `sweep_stale_resource_containers` has no empty-input guard — `ticket-hygiene-sweep_stale_resource_containers-empty-input-guard.md` → guard it.
- Container lifecycle events are missing from the bus — `ticket-bug-container-lifecycle-events-missing.md` → prerequisite for every push-based panel fix.
- Panels poll status endpoints and never subscribe — `ticket-bug-panels-poll-instead-of-subscribe.md` → push-first, poll-fallback (merged ticket).
- The EventBus subscribe call returns `None` and the bridge leaks listeners — `ticket-hygiene-eventbus-subscribe-returns-none-bridge-leaks-listeners.md` → fix subscribe/teardown; recorded, not fixed.
- A bridge is not evicted when a new session starts — `ticket-bug-bridge-not-evicted-on-new-session.md` → see `Unknown`.
- Container *type label* vocabulary collides with the lifecycle class — `ticket-bug-container-type-label-vocabulary-collides-with-lifecycle.md` → one vocabulary.
- Type label vs lifecycle class diverge (second view) — `ticket-bug-container-type-label-vs-lifecycle-class.md` → same fix.
- Labels vs lifecycle class, from the fact-check pass — `ticket-fact1-label-vs-lifecycle-class.md` → same fix.
- Container class derivation diverges between call sites — `ticket-bug-container-class-derivation-divergence.md` → one derivation.
- The status tool's fail-open path is unregistered — `ticket-bug-container-status-fail-open-unregistered-path.md` → register or remove; verified.
- Container status reports its own id back as a field — `ticket-fact5-container-status-id-echo.md` → drop or document.
- `uptime_seconds` semantics are ambiguous — `ticket-fact5-uptime-seconds-semantics.md` → define and document.
- Mount source is not validated — `ticket-bug-container-mount-source-unvalidated.md` → validate; verified.
- Status "read" actually executes something — `ticket-bug-container-status-tool-executes.md` → make reads passive.
- Integrity status docstring drifted from behaviour — `ticket-bug-integrity-status-docstring-drift.md` → fix the docstring; verified.
- The create admission branch is dead in production — `ticket-hygiene-container-create-admission-branch-dead-in-production.md` → delete or wire it.
- Envelope refusal after reap is unverified — `ticket-bug-post-reap-envelope-refuse-unverified.md` → see `Unknown`.
- Structured `/health` and purge interact badly — `ticket-bug-structured-health-purge-interaction.md` → see `Unknown`.
- Displayed-vs-enforced container display bugs (several) — `ticket-hygiene-displayed-enforced-container-display-bugs.md` → one pass over the panel.
- Container image size carries compiler overhead — `ticket-hygiene-container-image-size-compiler-overhead.md` → slim the layer.
- `/tmp` mounts are `noexec` and undocumented — `ticket-hygiene-container-tmp-noexec-doc.md` → document.
- Container-control design queue: sibling refusal — `ticket-design-ensure-container-refuse-sibling.md`; resize envelope-vs-ceiling — `ticket-design-envelope-vs-ceiling-on-resize.md`; recreate-alongside — `ticket-design-recreate-alongside-container.md`; start unification — `ticket-design-start-unification.md`; tool/resource contract — `ticket-design-tool-resource-contract.md`. → rulings, then one implementation wave.
- Container fixes already patched but unmerged: refused-return dead — `ticket-fix-ensure-container-refused-return-dead.md`; funnel stop/remove — `ticket-fix-funnel-container-manager-stop-remove.md`. → verify and merge.

### Git-write, credentials, and the commit gate

- Git write is not bound to the filesystem grain — `ticket-bug-git-write-not-bound-to-filesystem-grain.md` → bind the two grains.
- Build tool's filesystem grain is unchecked — `ticket-bug-build-tool-filesystem-grain-unchecked.md` → check it.
- The build tool chain is untested — `ticket-bug-build-tool-chain-untested.md` → add the chain test.
- The git resource audit doc claims a hook that does not exist — `ticket-hygiene-git-resource-audit-doc-claims-nonexistent-hook.md` → correct the doc; open.
- Git exec in the container is raw and not port-audited — `ticket-hygiene-git-exec-container-raw-not-port-audited.md` → audit.
- Credential injection is incompatible as shipped — `ticket-hygiene-credential-injection-incompatible-as-shipped.md` → fix or withdraw it.
- The credential regex collides with git askpass — `ticket-hygiene-credential-regex-collision-git-askpass.md` → narrow the pattern.
- The pre-commit gate is being restored — `ticket-fix-restore-precommit-gate.md` → this supersedes `ticket-feat-pre-commit-gate-hook.md`; do not action both.
- Git identity in the UI — `ticket-feat-git-identity-ui.md` → the fix is already shipped per the ticket body; settle the ticket's status.

### Tool exposure, refusal semantics, permissions

- A refused tool call is reported to the UI as `success: True` — `ticket-bug-tool-refusal-reported-as-success.md` → **fix is built and pushed at `c37943a1`, NOT merged**; merge it, then correct `docs/agent-core-spec.md` §8.6.
- The verdict classifier diverges from the producers (follow-up wave) — `ticket-bug-classifier-divergence.md` → structured fields, drop the prose sniffer.
- A permission refusal names the session layer, not both layers — `ticket-bug-permission-refusal-names-session-not-both-layers.md` → name both.
- The paginate tool bypasses required categories — `ticket-bug-paginate-tool-bypasses-required-categories.md` → enforce.
- No guard on tool-import declaration matching — `ticket-hygiene-tool-import-declaration-match.md` → add one.
- Prompt-asserted guards have never been audited — `ticket-hygiene-prompt-asserted-guards-audit.md` → audit; filed.
- The no-tool-wrapping rule has no guard — `ticket-hygiene-no-tool-wrapping.md` → rule accepted, guard parked.
- FileSearchTool brace globs fail silently — `ticket-hygiene-filesearchtool-brace-glob-silent-failure.md` → fail loudly; open.
- Escalation wording — `ticket-escalation-reword.md` → wording pass.

### Workers

- Worker context is not shown in the panel (SERVER-PAYLOAD verdict) — `ticket-bug-worker-context-not-shown-in-panel.md` → fix the payload, not the view.
- The worker panel intermittently misses tool calls — `ticket-bug-worker-panel-intermittent-missing-tool-calls.md` → fix implemented on a branch; verify and merge.
- Worker context has null fields — `ticket-hygiene-worker-context-null-fields.md` → fill or omit.
- Workers time out silently — `ticket-hygiene-worker-silent-timeout.md` → make it audible.
- Worker stream-loop teardown leaks — `ticket-hygiene-worker-stream-loop-teardown-leak.md` → Seam B's teardown half (owned loops are ruled; this is not re-architecture).
- Worker job lifecycle design — `ticket-design-worker-job-lifecycle.md` → core-adjacent; ruling first.
- The smoke-test config is never collected — `ticket-bug-smoke-test-config-never-collected.md` → collect it.

### The core — `project/core.md` (recorded, **not scheduled, not fixed**)

> Gate, ruled 2026-10-13: fix §0.1 D1 and D2 **before** starting C1/C2. Everything in this section
> is a wish until someone schedules it; nothing here is a live ticket.

- **§0.1 D1** — batching `Respond` + `SummarizeTool` silently discards the entire pruning pass (`agent/core/agent.py` turn loop: the summary branch sits below an unconditional `return`). Self-sustaining: at CRITICAL the only two allowed tools are the pair that trips it. → fix first.
- **§0.1 D2** — the provider's authoritative `usage.prompt_tokens` never re-arms the restriction, so the **first tool call of every turn runs unguarded**; measured drift 11.7% and 68.6% live. → fix with D1; the refusal-payload wording ships with it (C3).
- **§0.1 D3** — `agent_responded` calls `auto_save_current_session()` unconditionally; `WorkerSessionLifecycle` has no such method, so every worker turn logs a warning and is partly processed. → capability check.
- **C1** — sysprompt ADDENDUM: a session-visible, agent-editable info page. → needs a delivery mechanism (feeds C5).
- **C2** — external-message injection channel so runtime changes reach the agent's context. → design.
- **C3** — context-limit warning REPETITION: a notice that recurs until acted on; today a refusal says `Tool 'X' not allowed. Reason: token.` with no count and no instruction.
- **C4** — restriction-model coherence (found during the C3 investigation) → one model, not three flags.
- **C5** — system-prompt content wishes that need a delivery mechanism (feeds C1).
- **C6** — per-call / per-response TIME DELTAS: the core measures query-start → now. → the duration source the metrics explorer needs.
- **C7** — pruning-cycle PENULTIMATE TURN: one advance-warning turn before the tools lock.
- **C8** — `core/truncation-footer-true-size`: named, held, **never described**. → needs a description before it can be scheduled.
- **C9** — core-adjacent items named as Arc 4 candidates with no home (`four_arc_map.md`). → home them or drop them.
- **§0.1 D4 candidate** — turn-boundary delivery (recorded 2026-10-18). → candidate only.

### Tests, harness, environment

- macOS path-normalisation tests assume a symlink-free `/tmp` — `ticket-bug-macos-tmp-path-tests-assume-symlink-free.md` → **tracked, explicitly not to be fixed**.
- macOS advisory check triage — `ticket-hygiene-macos-advisory-check-triage.md` → triage.
- The macOS advisory hides real failures — `ticket-hygiene-macos-advisory-hides-real-failures.md` → make it visible.
- The macOS preflight proves the launcher, not the start — `ticket-hygiene-macos-preflight-proves-launcher-not-start.md` → assert the start.
- venv broken / pytest unreachable — `ticket-hygiene-venv-broken-and-pytest-unreachable.md` → open, **not blocking**.
- venv upgrade disclosure — `ticket-hygiene-venv-upgrade-disclosure.md` → document.
- Sandbox vitest CPU — `ticket-hygiene-sandbox-vitest-cpu.md` → bound it.
- Tests cross-pollute through the vault base and module state — `ticket-bug-test-cross-pollution-vault-base-and-modules.md` → isolate.
- Test gate import order / subprocess — `ticket-bug-test-gate-import-order-subprocess.md` → open; fix the ordering.
- Phantom xfail refusal pin — `ticket-test-phantom-xfail-refusal-pin.md` → remove the phantom.
- SIGIGN inherited spurious failure — `ticket-test-sigign-inherited-spurious-fail.md` → fix the harness signal handling.
- Container-integrity at the turn boundary is only e2e-tested — `ticket-test-verify-container-integrity-turn-boundary-e2e.md` → expand.
- Preflight fails on port 8000 while a previous instance is shutting down — `ticket-preflight-false-port-8000-fail.md` → distinguish the two states.
- Preflight suggests `vault_repair --apply` for keys `--apply` can never touch — `ticket-preflight-vault-repair-suggest-apply.md` → fix the suggestion.
- Backend startup log is append-only, so acceptance greps can match a stale boot — `ticket-backend-startup-log-append-acceptance.md` → pin the boot.

### Knowledge base, docs and repo hygiene

- The knowledge tool reports the wrong path (4 of 11 sites fixed) — `ticket-bug-knowledge-tool-reports-wrong-path.md` → finish the 11.
- Global-KB docs claim a writable global scope that does not exist — `ticket-hygiene-global-kb-docs-claim-writable-global-scope.md` → correct the docs.
- Load-bearing working docs are missing from the re-include list — `ticket-hygiene-load-bearing-working-docs-not-in-reinclude.md` → add them (this page is `!.thoughtmachine/knowledge/project/operator-dashboard.md`; see `Unknown`).
- Working-docs cluster reports are gitignored — `ticket-hygiene-working-docs-cluster-reports-gitignored.md` → open; **not to be fixed**.
- Every doc under `docs/` needs a hand-written `.gitignore` negation line — `ticket-hygiene-docs-gitignore-tax.md` → filed, not started; needs a ruling on which of four shapes wins.
- Hidden-files triage — `ticket-hygiene-docs-hidden-files-triage.md` → triage.
- Vault file inventory is disclosed to callers — `ticket-hygiene-vault-file-inventory-disclosure.md` → scope it.
- Vault-writer audit — `ticket-hygiene-vault-writer-audit.md` → audit, do not patch blind.
- Audit sink writes to `/tmp` — `ticket-hygiene-audit-sink-in-tmp.md` → durable sink.
- Catalog count drifts — `ticket-hygiene-catalog-count-drift.md` → derive, do not count.
- Manifest revision — `ticket-hygiene-manifest-revision.md` → open.
- Mechanism/surface split — `ticket-hygiene-mechanism-surface-split.md` → open.
- `docs/agent-core-spec.md` stage-map citations are offset by ~+320 lines — `ticket-spec-stage-map-citation-offset.md` → re-pin or de-pin.
- Build log length metadata — `ticket-hygiene-build-log-length-metadata.md`; build-tool context docstring — `ticket-hygiene-build-tool-context-docstring.md`; both doc-level.
- Executor dead code — `ticket-hygiene-executor-dead-code.md`; orphaned legacy docker-executor path — `ticket-hygiene-legacy-docker-executor-path-orphaned.md` → delete together.
- FakeManager signature — `ticket-hygiene-fakemanager-signature.md`; Fix7 blast radius — `ticket-hygiene-fix7-blast-radius.md`.
- Execution-grain status is report-only — `ticket-hygiene-execution-grain-status.md` → no fix implied.
- Host-port import rule is inventory, not a property — `ticket-hygiene-host-port-import-rule-is-inventory-not-property.md`.
- Localhost check pattern is inconsistent — `ticket-hygiene-inconsistent-localhost-check-pattern.md` → open, not blocking.
- Provider switch mid-session history — `ticket-hygiene-provider-switch-mid-session-history.md` → ruled; code + doc owed.
- Puppet LLM scenario format unify — `ticket-hygiene-puppet-llm-scenario-format-unify.md`.
- Silent API-key overwrite — `ticket-config-silent-api-key-overwrite.md`; empty-dict log noise — `ticket-config-strip-empty-dict-log-noise.md`.
- LLM usage/cost accounting is partly dead code — `ticket-hygiene-llm-usage-cost-dead-code.md` → blocks the metrics explorer's token panel.
- Adversarial gate before boundary merge (process rule, not code) — `ticket-hygiene-adversarial-gate-before-boundary-merge.md` → adopt it.

### UI panels

- Panel actions silently no-op (recon complete) — `ticket-bug-panel-actions-no-op.md` → wire or remove.
- host-bash output shown in the panel — `ticket-hygiene-panel-host-bash-display.md` → open, **FILE ONLY**.
- `host_bash` runs in the wrong workspace — `ticket-bug-host-bash-cwd-wrong-workspace.md` → fix the cwd.
- `host_bash` double-prompts on ask — `ticket-bug-host-bash-double-prompt-on-ask.md` → one prompt.
- WS frame diagnostics — `ticket-bug-ws-frame-diagnostics.md` → add them.

### Queued features and designs (filed, with a plan)

- Kill switch visible in the UI — `ticket-feat-kill-switch-visible.md` → operator-requested, open.
- Container note on **existing** containers — `ticket-feat-container-note-on-existing.md` → P1 since 2026-10-17.
- Container resize tool for the agent — `ticket-feat-container-resize-tool-for-agent.md` → pair with `design-envelope-vs-ceiling-on-resize`.
- Executor image provisioning — `ticket-feat-executor-image-provisioning.md`.
- Record created-by provenance — `ticket-feat-record-created-by-provenance.md` → pairs with the `created_by` bugs above.
- Workspace deletion — `ticket-feat-workspace-deletion.md` → needs the same timeout/ack answer as create.
- Dispatch report duration — `ticket-feat-dispatch-report-duration.md` → sibling of the metrics explorer.
- TM metrics explorer — `ticket-feat-tm-metrics-explorer.md` → **filed today**; read-only page over KB domains, per-turn durations, token usage.
- v3: modes as editable presets — `ticket-v3-modes-as-presets.md` → **filed today**; v3 scope, needs a ruling on live-vs-next-session rebinding.
- Agent role control — `ticket-design-agent-role-control.md` → read before the modes work.
- Audit trail completeness — `ticket-design-audit-trail-completeness.md`.
- Security gate transitive pydantic — `ticket-design-security-gate-transitive-pydantic.md` → OPEN, re-scoped.

## Unknown / needs investigation

Only items with **no diagnosis**. Everything else is above. **8 rows**: **7 tickets** plus **1
non-ticket row** (the `.gitignore` block at the end). **3 of the 7** ticket rows have **no `Status:`
line** — they are file-stale by the partition at the top of this page, and they are listed here on
the strength of their bodies, not their status.

- **First install: creating the first workspace freezes the UI; a restart clears it** —
  `ticket-bug-first-install-workspace-freeze.md` (filed 2026-10-29). No reproduction, no log, no
  stack. Lead, not diagnosis: first install registers through `POST /api/workspace/resolve`
  (`workspace_routes.py:115`) while every later workspace uses `POST /api/workspace` (`:1770`) —
  two paths, one exercised once per install.
- **Container integrity deletes on session load** — `ticket-bug-container-integrity-deletes-on-session-load.md`.
  Blocked on verification: it is not established that the deletion is real.
- **Envelope refusal after the reap** — `ticket-bug-post-reap-envelope-refuse-unverified.md`.
  Claim recorded, never verified.
- **A bridge is not evicted when a new session starts** — `ticket-bug-bridge-not-evicted-on-new-session.md`.
  Ticket carries no `Status:` line and no mechanism — symptom only.
- **Structured `/health` and purge interact** — `ticket-bug-structured-health-purge-interaction.md`.
  Seven lines, no H1, no hypothesis.
- **Worker internal time budget** — `ticket-bug-worker-internal-time-budget.md`. Named, never described.
- **Raw payload reachability** — `ticket-read-raw-payload-reachability.md`. Named, never described.
- **Committed unresolved merge conflict in `.gitignore` (lines ~301-304)** — not a ticket; a real
  defect in a tracked file:
  ```
  <<<<<<< HEAD
  !docs/brief-sysprompt-implementation-brief.md
  =======
  >>>>>>> origin/dev
  ```
  The markers are committed and are parsed as ignore patterns, so whether that negation is in
  force depends on how git reads the block, and nothing in CI notices. **Needs a decision:** keep
  HEAD's line, take `origin/dev`, or keep both. It was left untouched deliberately.

## Recently closed

Kept out of the open lists above. **13 files** carry a closing `Status:` line — **10 closed with
evidence** and **3 superseded or retired** (two of the three folded into a third ticket by one
merge). One further row, `ticket-bug-container-control-base-registered-as-tool.md`, is listed here
although its ticket file has **no `Status:` line at all**: the refutation exists only on this page,
so by the partition above that file counts as `stale`, not closed.

- `ticket-bug-capabilities-undeclared-fields-silently-inert.md` — shipped (PR #119).
- `ticket-bug-checksystem-vault-status-cross-tenant-leak.md` — shipped (PR #119).
- `ticket-bug-ui-async-tool-pairing.md` — closed (PR #246).
- `ticket-bug-define-hostbash-bug.md` — retired; superseded by later host-bash tickets.
- `ticket-hygiene-knowledge-base-get-global-kb-root-not-dead-code.md` — closed; the claim was refuted.
- `ticket-hygiene-worker-spawn-context-delete-contradiction.md` — resolved by reading the code.
- `ticket-hygiene-container-status-passive-read.md` — fixed in Wave 10a.
- `ticket-hygiene-container-memswap-unbounded.md` — fixed, awaiting merge.
- `ticket-bug-streaming-api-timeout.md` — fixed (PR #248 merged).
- `ticket-bug-worker-spawn-cap-fourth-gate.md` — fixed in the tree.
- `ticket-bug-container-control-base-registered-as-tool.md` — premise false; refuted.
- `ticket-feat-checkSystem-runtime-version.md` — **closed 2026-10-29**: runtime version reporting
  shipped (start-time capture + `CheckSystem query='version'` + `/health` + startup log line).
  Residual nice-to-haves and two unverified branches are listed in its `## Resolution`; they are
  not a reason to reopen it. *(Not the same complaint as
  `ticket-bug-checksystem-version-reports-loaded-revision-not-head.md`, which stays open.)*
- **Merged 2026-10-29:** `ticket-bug-live-view-container-stale-no-push.md` +
  `ticket-hygiene-panel-polls-no-subscribe.md` → `ticket-bug-panels-poll-instead-of-subscribe.md`
  (one defect, two ends). Both originals are marked SUPERSEDED and their bodies are kept.

