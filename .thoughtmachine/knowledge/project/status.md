# ThoughtMachine -- Status

Updated: 2026-10-28, Gen 54 mid-session.

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

## Current state — 2026-10-28, Gen 54 mid-session

**Supersedes the Gen 52 close (kept below as historical record).**
**dev HEAD:** `12bd6408`

Gen 53 (`fix/stream-loop-thread-safety`, `44d0dda`) and the `docs/kb-catchup` wave (#235, `d8fda869`) closed without a status.md block; they are recorded here so the chain has no gap.

## The generation in one line

Six waves landed; the last one — tool refusals reported to the UI as success — is built, gated and pushed but NOT yet merged, and a spec-level lie was found underneath it.

## Waves merged this generation (#233 → #236)

- #233 `ca413e7f` — `fix/resource-leaks`.
- #234 `2ac3148c` — `test/core-concurrency-wall`: pins per-thread loop isolation, thread-death cleanup and event-bus concurrency; its controller case is mutation-verified (it goes red if the teardown call is deleted).
- #235 `d8fda869` — `docs/kb-catchup`.
- #236 `12bd6408` — `fix/integration-bugs` (`56479062`): Stop now cascades to the session's worker sub-agents. The turn-scoped path (`server.py:1507` → `bridge.request_stop()` → `controller.request_halt()`) was the only Stop path that did not reach workers.

## In flight: `fix/tool-refusal-structure` @ `c37943a1`

- A denied tool call was reported to the UI as `success: True`. The cause is structural, not cosmetic. (1) No producer in the tree emits the `TOOL CALL REJECTED` literal the classifier greps for — only `agent/core/agent.py:1653` reads it, while gate denials say `Permission denied: …` and `_create_tool_rejection_message` says `Tool not allowed. …` — so the sniffer could not fire for a real refusal. (2) The per-call verdict was dropped at the `execute_tool_calls` boundary, which built `executed_tools` entries as `{name, arguments, result}` only.
- Fix: the nine `_execute_single_tool` refusal/failure returns gain `'success': False` with byte-identical prose; the three non-execution appends in `execute_tool_calls` (state rejection, invalid JSON, unknown tool) gain it too; the verdict is forwarded into `executed_tools`; and `agent.py` reads `tool_info.get('success')` first, keeping the prose sniff only as a fallback for producers outside the executor path.
- Success paths stay key-less on purpose: `agent/presenter/event_processor.py:300-306` does `event.get('success', True)`, so flipping that default would turn every legacy producer into a failure. An absent key keeps meaning "no verdict".
- Tests: `TestToolFailureSuccessFlag` (6) in `tests/test_tool_executor.py`; `tests/test_provider_token_truth_midturn.py` flips its documented KNOWN-BUG pin `success is True` → `is False`, which that test's own comment required once the bug was fixed.
- Gate: `python -m pytest -q` → 5592 passed, 66 skipped, 5 xfailed, 12 subtests passed, exit 0.
- Not on dev: `grep -c "'success': False" agent/core/tool_executor.py` is 0 at `12bd6408`. Any earlier statement that this had already landed on dev is wrong.
- `docs/agent-core-spec.md` still documents the pre-fix behaviour — §8.6 at `:619-625` ("a refused call is emitted with `success: true`"), plus `:420`, `:477`, `:1106` — and its line citations have drifted (`:496` cites `agent/core/agent.py:1324-1333`; the classifier is at `:1650-1666`). Correction owed with the merge; not done in this wave.

## Seam B — verdict

- Seam B in `docs/design-concurrency-and-affinity.md` §3.B ("The Worker-Spawn Path") is CLOSED as an ownership seam. A worker is an in-process daemon thread (`tools/workspace/worker_thread.py:901`, `:939`) that lazily builds its own `Agent` on its first query (`:2522`, `:2534-2538`); `grep -c asyncio tools/workspace/worker_thread.py` is 0; its loop comes from the thread-local at `agent/core/agent.py:151` (defined `:106`). It never touches the controller's loop or its bound httpx pool.
- It is OPEN as a teardown leak. `_close_stream_resources` is called only from `agent/controller/__init__.py:784-786`; `WorkerThread.run()`'s `finally` (`worker_thread.py:2925-2965`) closes no loop, provider or client, and `reset()` (`:1584`) drops `self._agent` without `LLMClient.close()` (`agent/core/llm_client.py:344`). Hygiene on thread death, not cross-thread corruption. Filed as `ticket-hygiene-worker-stream-loop-teardown-leak.md`.

## Next

- Merge `fix/tool-refusal-structure` (PR pending; `12bd6408` is its base, so it merges without a rebase).
- Correct `docs/agent-core-spec.md` §8.6 in that PR or immediately after it.
- `fix/vault-schema-root-and-git-identity` in flight on a disjoint front: declare `root` and `git_identity` in `agent/config/schema_manifest.json` under `workspaces/*/config.json`, and route the engine-read keys through one `_ENGINE_READ_KEYS` frozenset in `thoughtmachine/vault_repair.py` so `--apply` can never quarantine them.
- Open operator rulings: idle-stop ack honesty (pinned by `tests/integration/test_state_transitions.py::test_ws_stop_session_status_only`), the CI advisory-vs-blocking flip, and keep-or-revert plain-text-while-streaming in `MessageBubble.jsx`.
- macOS path-normalisation test failures are tracked separately and are not to be fixed.
- Publication-tier contradiction, unresolved: `working_agreements.md:60` ("Never staged, never committed: everything under `.thoughtmachine/knowledge/`") contradicts `.gitignore:228-247`, which deliberately re-includes the seven-file onboarding contract set (this file among them) and `docs/KB_POLICY.md:127`. This commit follows the `.gitignore` intent; the agreement text needs an operator ruling.

## Ruled this generation (do not re-litigate)

- Failure is carried as data, not prose. Do not re-robustify the string sniffer and do not delete the marker fallback.
- Do not flip `event_processor.py`'s `event.get('success', True)` default. An absent key means "no verdict".
- Success paths stay key-less. Blanket `success: True` on every return is not wanted.
- Workers own their loops. The remaining Seam B work is teardown only, not re-architecture.

## Current state — 2026-10-27, Gen 52 close

**Supersedes the 2026-10-26 Gen 51 close (kept below as historical record).**

**dev HEAD:** `45b0ca68`

**The core is streaming-only.** The synchronous turn loop has been deleted.
- **Wave 0:** API-key redaction (`Secret[str]`), Anthropic `tool_result` blocks, ConfigService delegation.
- **Wave 2:** Canonical IR types (`agent/core/ir.py`), OpenAI streaming adapter (`stream_completion`).
- **Wave 3:** The streaming turn loop rewrite (`process_query`). D1 (Respond+Summary) dissolved via `final_pending`. D2 (first tool call) fixed via `UsageUpdate`. E2E stub updated to SSE.

**What was deliberately dropped:**
- **Mid-stream cancellation:** `should_stop()` returns `'PAUSED'`. Checkpoint [1] cancellation remains. A `should_halt()` boolean is a future ticket.
- **The `streaming` flag:** Deleted. Zero production call sites.

**What is next:**
- `fix/tool-refusal-structure` (Fix the classifier to use structured fields, not string matching).
- Wave 4 (Responses API adapter for GPT-6).
- Wave 5 (Sysprompt addendum, multimodal).

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
