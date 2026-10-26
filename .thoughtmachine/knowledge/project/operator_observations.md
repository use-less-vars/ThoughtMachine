# Operator observations

Private KB. Append-only, newest at top. One line per item, tagged.
Capture rule: an operator statement that sounds like a finding, intent, preference, or recheck gets written here in the same session, or it did not happen.

Tags: `[intent]` wanted feature/direction · `[finding]` observed problem · `[preference]` how the operator wants things to work · `[recheck]` verify again later · `[question]` open, no owner.

> ### 2026-10-17 — Gen 45 — CORE WORK HAS MOVED. This file is a LOG, not a backlog.
>
> **All *pending* core work now lives in one place: `project/core.md`.** Contents there:
> **C1–C9** (feature backlog) · **§0.1 D1–D3** (verified core-loop defects — *gates*, not candidates) ·
> **§6** (pointers to core-adjacent items deliberately kept elsewhere) · **§7** (proposed sprint order,
> with D1–D3 as step 0).
>
> Why: a 2026-10-17 consolidation found core items had leaked out of `project/core.md` (stale since
> 2026-10-11) into *this* log, plus `project/current_map.md` and `project/four_arc_map.md`
> ("Arc 4 — Core"), `project/thoughtmachine_current_state.md` ("Named and held → Core backlog"),
> `personal/parking_lot.md`, `personal/task_tracker.md`, `project/roadmap.md` and
> `project/open_items_register.md` §11/§16. Eight pending items had no home in the file that was
> supposed to be the single home.
>
> **Searcher: start at `project/core.md`.** Add new pending core items **there**, not here. Entries
> below remain as the historical record of when each item was observed.
>
> **Named pattern (Main Gen 38, 2026-10-17): `log-reads-as-backlog`** — a chronological log whose
> *shape* signals a backlog *role* it does not serve. A `displayed≠enforced` variant at the KB level.
> This badge is the remedy; keep it here permanently. Canonical entry:
> `project/working_agreements.md` → "Named pattern: `log-reads-as-backlog`".

## 2026-10-17 — Gen 45 seat, operator intent (token-restriction surface)

### [intent] Critical token warning must re-appear on EVERY tool-call attempt while critical
Operator, 2026-10-17 (relayed via Main Gen 38): *"we need the critical token warning to appear with
 each attempt to call a tool when in critical token state. Otherwise the agent tends to forget how he
 can get out of the tool restrictions (that is: by summarizing). The message should turn up every
 time, not just once."*
Today the notice is **edge-triggered and latched** — a single CRITICAL crossing, then silence; the
state that *justifies* the notice stays on while the notice stops being delivered (mechanism in
`project/core.md` §C3). Main's ruling: this is the **surface half of D2** and ships **with D2's fix**,
not as its own strand. Recorded as the operator's chosen verdict in `project/core.md` §C3 and
cross-referenced from §0.1 **D2**. **No new ticket number** — it belongs to **C3**.
**Adopted 2026-10-17 (Main Gen 38).** C3 at **full strength** (100% duty cycle while CRITICAL)
confirmed. The **refusal payload** (`ToolExecutor._create_tool_rejection_message`) is ruled the
**primary** carrier — shape 3 in §C3 — on the reasoning that it is not an extra emission but the
emission the agent already provoked. Shape 1 + shape 3 ship **together**, in the same wave as the
surface half of **D2**. The Gen 45 session's own critical-threshold hit is banked in §C3 as the live
reproduction C3 was missing. Open gaps accepted, not urgent: **C8** has no description anywhere
(re-derive from the truncation path before briefing) and **C6**'s dispatch choke point is unpinned
(hard rule: timing is descriptive only, never parsed back into control flow).

## 2026-10-08 — Gen 36 seat, operator additions (UX, vault, pipeline, GC, host_bash logging)

### [finding] Frontend messages to main panel persist and share color with core messages
Two distinct issues in one surface:
  - Frontend-emitted messages (e.g. the "pausing" message when user presses
    pause) persist in the transcript until F5. They need a dismissal
    mechanism — either auto-clear on state resolution, or don't inject
    into the transcript at all.
  - Frontend-emitted messages use the reserved red-ish color, which
    should be core-system-messages only. Non-core emitters need distinct
    colors so the user can tell at a glance: this is not the core.
Extends the 2026-10-02 "display color conventions" observation.
Needs: a read to see which emitters write to the main panel and how they
currently persist/clear. Then a decision: dismiss mechanism + color scheme.

### [finding] Vault integrity check shows ~11 issues; mechanism is incomplete
The vault integrity tool exists for the user to see if the vault is
"old" or has drifted. Currently shows ~11 problems that the operator
believes are not real. Historical approach (delete and rebuild the vault)
is no longer feasible. Need a mechanism that knows:
  (a) what vault files SHOULD look like,
  (b) what they ACTUALLY look like,
  (c) what to do about a mismatch (repair, ignore, warn).
Must be future-proof — new schema versions must produce clean reads, not
false positives. Related: "Vault integrity check still shows errors.
Root cause unknown" (from Bugs / suspected, 2026-10-01). Not urgent,
but the machine's vault is becoming load-bearing; opaque integrity is a
state-layer violation.

### [finding] Core stops but response doesn't come through — pipeline loss
Agent halts at what should be end-of-turn; the response is logged as a
"final message" in core logs but never renders in the GUI. User asks
"please repeat your answer" — works. So the loss is in the delivery
pipeline, not the core. Related: earlier-named "final message sometimes
not shown in GUI" and bug/main-panel-drops-empty-content-assistant-message.
Operator suggestion: tackle one-by-one. Needs a read to trace where the
message is lost (bridge? bridge normalization? WS? frontend store?).
Not urgent; user-facing confusion.

### [finding] Systematic pollution check — who writes what where, and who cleans up
Discovered by luck: an infinitely long list of dead containers found
during an unrelated host docker command. The system polluted itself and
nobody noticed. Container cleanup has had an effort, but the broader
question is unanswered: every subsystem that writes to disk (vault,
containers, records, logs, event streams, caches, ephemeral artifacts)
needs a named reaper and a named owner. Systematic sweep needed:
  - Enumerate writers.
  - For each, name the reaper.
  - For each, name the trigger condition (age, TTL, event, orphan).
  - Report gaps.
Related: existing hygiene items (dangling-image-accumulation,
storage-reaper-gaps, resource-record-reaper). This is the meta-ask:
a single audit that names every reaper and every gap.

### [finding] host_bash needs logging
host_bash is a "dangerous command" by definition. It should produce an
audit trail — who called it, when, with what command, on whose session.
Check whether logging exists today; if partial, name what's missing.
Extends the security-layer "cameras" principle to the highest-risk tool.

## 2026-10-08 — Main Gen 36: R2 scope ruling (container ceiling)
### [ruling] Workspace container ceiling scopes persistent containers only
Resource containers are NOT in scope. Ephemeral containers are NOT in scope.
Rationale:
- Operator intent (container flow, 2026-10-01): "Persistent = 'office
  machines,' not scratch. Container cap 2-6 per workspace: avoid UI
  overwhelm AND discourage lazy container creation." Both purposes attach
  to persistent only.
- Three classes have different creation authorities: persistent = user,
  capped; resource = workspace import, bounded by resource type; ephemeral
  = session, churning, not a durable thing to cap against.
- User sense: "6 containers in my workspace" means 6 office machines, not
  "6 machines including the git room and dead scratch."
- The label-based count was expedient, not designed. Intent and mechanism
  diverged on scope. This ruling restores intent.
Consequences:
- R2 (container-ceiling divergence) is NOT security-adjacent. The
  permission ceiling is untouched; container-quota overruns are a
  resource-discipline concern, not a gate bypass.
- Fix shape: enforcement counts persistent-labelled containers only. The
  ignoring sites read config.json. ResourceContainerManager is out of cap
  scope; its bound is resource-import count. Ephemeral reaping is its own
  problem.
- Folds into `bug/container-quota-two-counters-one-fact` strand — same
  correctness argument, "correct scope of what counts toward what."
- Separately: this is the answer to Gen 35's container-counter pain.
  Permission testing filled the quota because ephemeral containers were
  counting. They shouldn't have been.

## 2026-10-08 — Gen 36 seat, operator side notes (out-of-context, other TM user + UX)

### [finding] Agent loop has two stop conditions, formatted differently
The agent loop stops either on a `respond` tool call OR on an assistant
message with no tool call. Both terminate the turn but produce differently
formatted outputs. Decision needed: unify to one mechanism for stopping
the loop, so the end-of-turn output has one shape. Not urgent, but the
inconsistency will keep surfacing in the GUI.

### [finding] Final message sometimes not rendered in GUI
The agent's final message is sometimes not shown in the UI; the user has
to re-query to see the last output. This is a delivery-pipeline bug, not
a display-cosmetic. Confusing for the user. Solve in a timely manner.
Related: `bug/main-panel-drops-empty-content-assistant-message` may be
adjacent — check whether same surface.

### [finding] UI performance — tab switching slow, CPU pegged
Tab switching is slow. Operator observes the CPU being overly used;
source unknown. Not profiled. Related to existing named item
"session tab switching slow (cause unknown)" from 2026-08-25 handoff.
Needs the performance investigation (Phase C, Kind A, thread 1 — "Slowness").
Operator says: soon, not urgent.

### [finding] Two distinct scroll behaviors in main vs worker panel
- Main agent panel: when scrolled up and a new message arrives, the view
  is pulled back to bottom. Annoying — interrupts reading.
- Worker panel: screen does not auto-scroll on new content; view sticks
  at a random point; user scrolls manually.
Different problems in different panels; different fixes. UX, not urgent,
but user-facing and confusing.

### [preference] Plan adherence confirmed
Operator: "Lets stick with the plan, but not forget about stuff."
None of the above is ultra urgent — recorded so it is covered soon, not
jumped. Queue order stands: Edge 5 → Pass 2 → container lifecycle design
pass. Side notes above queue as they surface for scheduling.

## 2026-10-04 — Gen 35 seat entry, operator Q&A

### [intent] Core LLM injections — `design/core-injection-and-message-truthfulness`
Permissions used to be part of core agent config; when core config changed, a core mechanic injected a change-summary into the LLM context at the next pause, keeping the LLM aware of its environment. Permissions are now a separate layer, so that injection path does not fire for permission changes — the LLM does not learn its permissions changed. Need a general external-injection mechanism: bounded token count, fired only at points where needed, rejected when too big, must go onto `user_history` (the core's backbone). Same realm as the sysprompt-addendum; handle with care before building, not after.

### [finding] Core messages must be truthful to where they enter `user_history` — `bug/core-messages-not-truthful-to-user-history`
Observed: "API failed" messages (red-ish, GUI) accumulated, duplicated, disappeared on F5, switched position. Interpretation: composed outside the core and rendered without deterministic queueing into `user_history`. Target principle: *what the user sees in the red-ish color on the GUI is what the LLM sees, at the point where it shows up.* Three requirements: (1) truthful queue point, (2) allow external injections, (3) allow live sysprompt updating (analyze diligently before building the addendum).

### [intent] Sysprompt addendum
Purpose: agent writes down what does not fully work + workarounds, permanently. Example: "remember to name the worker you want to query" — avoids a wasted correction turn. Or user-driven: "remind the agent not to use host bash when not absolutely necessary." This is live sysprompt updating; analyze before building.

### [preference] "Forgives operator error" — reframed
Not a Stage 1/2/3 gate. About non-technical users. Defensive sysprompts / beginner modes that warn when private data is bound for a public LLM API, warn about readable keys/passwords/secrets, explain when host bash is requested and remind user to switch it off after, help set permissions correctly, explain security implications on request.
Catastrophe taxonomy, ordered:
  - catastrophe: attack host
  - very bad: destroy user data
  - annoying: destroy rebuildable/recoverable files
  - benign: extra turn due to tool misuse
Timing: sysprompts and modes are last, near V3. Also remove user-blockage on sysprompt/tool editing in pre-set modes (engineer, agent) — modes must become flexible and extendable. Named: `design/beginner-mode-and-guidance`.

### [intent] Modes roadmap — `design/modes-and-worker-tool-split`
Modes to exist: welcome/setup (help user configure workspace), async multiagent, sync multiagent, normal agent (no workers), more TBD.
Problem today: async main agent polls for worker completion, filling its own context — the exact thing multiagent exists to avoid. Worker tools for async (`sleep`, `join`, TBD) not built. Current `worker` tool does sync and async; split into `worker_sync` / `worker_async` with per-mode sysprompts. Not urgent but should work before V3.

### [preference] Queue handling
"I am not in a hurry. Keep working one by one." No imposed urgency.

### [intent] Miscellaneous bucket
Cross-partition workspace creation (workspaces only on C: today) — not urgent, "someday, don't forget." Also: no delete-workspace button, workspace-level container metrics, worker config panel gap. Queue opportunistically.

## Persist-verbatim recheck — escalation threshold (2026-10-03, Main Gen 34)
- `[recheck]` Brief verbatim loss — 5 instances in one session. Watch. If it
  crosses 7, it's a brief-format design question, not just practice. The
  architecture question at that point: does the brief format itself need a
  pinning primitive?

## Edge 4 complete (2026-10-03, Main Gen 34)
- `[ruling]` Edge 4 landed. PR #59, 4 commits. Container record's state field
  is now a resolver (live cross-check with freshness marker), heal reads
  intent_snapshot (not current policy), updated_at mutation-only under test.
  Semantic ruling: creating + absent-within-expiry stays fresh=True; the
  original ruling's expiry qualifier was load-bearing and restored by the
  agent.
- `[ticket]` bug/container-record-periodic-lifecycle-sweep filed. Boot scan
  catches stuck-at-boot; periodic scan gap is real, not urgent.

## Edge 4 residual — no lifecycle sweep (2026-10-03, Infra Gen 31)
- `[finding]` B2 deferred from the Edge 4 migration: no sweep enumerates
  container-bearing records while holding a Docker client. The boot-time drift
  scan runs once at lifespan startup; there is no periodic lifecycle check.
  Stuck-creating records created AFTER boot will not be flagged until the next
  process restart.
- `[ruling]` Ticket `bug/container-record-periodic-lifecycle-sweep` filed,
  read-first, queued behind Edge 4 + the two queued reads. Not urgent (boot
  scan catches the common case), but the gap is real.

## bug/knowledgebase-tool-update-duplicates-section (2026-10-03, Main Gen 34)
- `[finding]` `KnowledgeBaseTool` `mode=update` **inserts/duplicates** the targeted section body instead of replacing it (observed during the Edge-4 pass; the damaged region was repaired and verified by hash). Do not use `mode=update` on a live section until fixed.
- `[ruling]` Ticket filed; read-first; queued behind Edge 4.
- `[source]` Folded from Main Gen 34's brief as relayed through this session's handoff; wording is condensed, not verbatim-verified — see the next entry.
- `[finding]` 2026-10-08: sibling defect of the mode=update bug — `KnowledgeBaseTool` `mode=append` WITHOUT `append_section` fabricates a spurious top-level heading (`## <date> — <first ~60 chars of the text>`) and lands the body as a NEW end-of-file section instead of the intended section. Observed 3 instances this session (status.md Pass-2 evidence bullet; operator_observations.md CI addendum; b3-ticket stub) plus 4 legacy stubs in `working_agreements.md`. Workaround: use `append_section`, or `mode=update`, to target a section.

## Verbatim-loss recheck (2026-10-03, Main Gen 34)
- `[recheck]` Brief verbatim content was lost to the context roll ~4 instances this session. Practice: pin must-survive content via keep-last, or write-to-file-and-read-back before the summary lands. The fix is **practice, not code**.
- `[principle]` The operator's sysprompt-upgrade intent covers the mechanism at a higher level; this is the low-level complement, not a duplicate.

## Edge 4 read finding — latent divergence (2026-10-03, Main Gen 34)
- `[finding]` Container record's `state` field is WRITE-ONLY today: no consumer
  makes a decision on it (verified this session; only display read at
  web_ui/backend/server.py). Not a divergence-family instance (two holders, no
  reconciler) — a LATENT one: one holder, no reader, meaning unanchored. The
  divergence activates on the first real reader. Different sub-shape, same
  class. Evidence for `design/layers-and-truth-ownership`.

[attribution] The two blocks below are Infra Gen 31 fold-ins from Main Gen 34's
ruling. Main's verbatim block text was lost when this session's context was
summarised before the fold-in landed, so the wording is reconstructed from this
session's independently verified evidence plus the ruling's own summary.
Overwrite if the wording differs from Main's.

## CI — web_ui/backend/tests sits outside testpaths (2026-10-03, Infra Gen 31; Main Gen 34 ruling)
- `[finding]` `pyproject.toml` pins `testpaths=["tests"]`, so `web_ui/backend/tests/`
  (11 files) is NEVER collected by the default suite — a green run says nothing
  about the directory that holds router/permission tests. Measured on
  `fix/edge4-record-vs-live-state`: configured suite = 4037 passed / 62 skipped /
  0 failed, while `web_ui/backend/tests/` = 19 failed / 106 passed.
- `[finding]` All 19 failures are PRE-EXISTING, proven by running that directory
  against the branch base `5e90c71` in a throwaway worktree: identical 19 node
  ids, identical 19 failed / 106 passed. Separately, `web_ui/backend/tests/test_ws_mock_provider.py`
  is a 0-BYTE file. A branch `fix/pre-existing-test-failures` already exists for
  the failures themselves.
- `[ruling]` Ticket `ci/web-ui-backend-tests-outside-testpaths` filed. Not fixed
  on Edge 4 (Edge 4 = record-vs-live-state; this = CI coverage).
- `[principle]` A green run only speaks for the paths it collected. "Suite is
  green" must name its `testpaths`, or it is a claim about a subset reported as a
  claim about the whole.
- `[addendum]` 2026-10-08 (Edge 3 Stage 1 close-out re-check, Main Gen 35): the
  pre-existing red set in this directory includes
  `web_ui/backend/tests/test_permission_routes.py` (9 reds). Appended here so the
  ticket `ci/web-ui-backend-tests-outside-testpaths` NAMES the file; the addendum
  does not re-scope the ticket (Edge 3 does not fix CI coverage). The earlier
  text here named only `test_ws_mock_provider.py` (0-byte) and the directory
  totals (19 failed / 106 passed).

## Installer distro assumption — addendum (2026-10-03, Main Gen 34)
- `[ruling]` Ticket `bug/installer-linux-distro-agnostic-tool-check` — filed,
  read-first, queued behind Edge 4 (restates the ruling recorded above; no change).
- `[ruling]` Ticket `ci/linux-distro-matrix-fedora` — filed, queued: exercise a
  non-Debian path in CI instead of only reasoning about it.

## Brief-template rule — no line numbers (2026-10-03, Main Gen 34)
- `[ruling]` Report citations must be symbol + file. Do NOT emit line numbers —
  they rot and mislead. If you cannot name the symbol, say so rather than cite
  a number. Line numbers are allowed inside a worker's own scratch work; not in
  reports or briefs. Enforce in brief templates.

## Installer distro assumption (2026-10-03, operator + Main)
- `[finding]` Linux install script assumes Debian-derived distro: hardcodes
  `apt-get` for package install and gates tool presence on Debian package
  names. On Arch, `util-linux` is base system (`sg` present), packages install
  via `pacman` — script fails at tool check even though tools exist. Third
  recent instance of a Debian-shaped assumption tripping a non-Debian path
  (Windows CI earlier, Arch now).
- `[intent]` Make distro detection a first-class step. Check capability
  (`command -v sg`), not package name. Detect package manager
  (`apt-get`/`pacman`/`dnf`), do not assume.
- `[ruling]` Ticket `bug/installer-linux-distro-agnostic-tool-check` filed,
  read-first. Queue behind Edge 4. Not urgent.

## Design pass 2 evidence — duplicate normalizer (2026-10-03, Main Gen 34)
- `[finding]` `web_ui/backend/bridge.py::_normalize_for_frontend` and `web_ui/backend/session_manager.py::_normalize_for_frontend` hold BYTE-IDENTICAL gate logic (same `if assistant_msg.get("content"):` drop condition, same sibling drop). One fact, two holders, no reconciler — divergence-family instance. Bug fixed in bridge.py first; session_manager mirror required a separate fold-in because the initial brief scoped one file. Evidence for `design/layers-and-truth-ownership`.

## Merge-gate KB correction — mergeStateStatus (2026-10-03, Main Gen 34)
- `[correction]` Observed 2026-10-03 (PR #57): `gh pr view --json mergeStateStatus` returned BLOCKED with all 13 required checks green; `gh pr merge` succeeded anyway. Cause not isolated. Two candidates: (a) state changed between view and merge, or (b) `gh pr merge` does not gate on `mergeStateStatus` as previously documented. Do not rely on `mergeStateStatus` alone as a pre-merge check until this is isolated. Required-checks green + a manual look at the merge button state remains the operative check.
- **CAUTION:** This is about how the CLI reports merge eligibility, NOT about Edge 8. Edge 8 was about required checks being configured. Edge 8 stands; the ruleset + required checks held (13 rows ran, 13 succeeded). Do NOT conflate. Do NOT reopen Edge 8.

## Banner-hygiene + worker-tool-call-started (2026-10-02, Infra Gen 31)
- `[ruling]` Ruling 1: server.py:1991-1994 stale banner comment folded into
  fix/banner-docstrings. Comment-only, branch now 4 files (3 test files + one
  backend comment).
- `[ruling]` Ruling 2: toolcalls_streaming_backend_contract.md flipped to
  reflect the built tool_call_start event. Scope: flip stale assertions only,
  no new prose. Folded into feat/worker-tool-call-started.
- `[finding]` Read B follow-up: dominant empty-content case is (b) tool-only
  turns - assistant messages with empty content but populated tool_calls,
  dropped at bridge.py::_normalize_for_frontend, condition
  `if assistant_msg.get("content"):`. Design question pending Main: should the
  main panel render a tool-only turn as a tool-call block, or deliberately
  not render?

## Network grain ticket + design pass 2 evidence (2026-10-02, Main Gen 34)
- `[finding]` Read C resolved: `network:write` and `network:outbound` are NOT synonyms and NOT different grains. They are two ordinal levels of the SAME session-level network grain. `security/resource_catalog.py::GRANT_LEVEL_RANKS` write=3, outbound=3.5; `WORKSPACE_CEILING_LEVELS_RANKS` write=3.0, outbound=4.0. Both resolve to Docker `network_mode="bridge"`. Requirement string `network:outbound` used by git_write_tool clone and mcp_manager HTTP/SSE; absent from every YAML/JSON/MD schema.
- `[ruling]` Ticket `bug/network-grain-outbound-vs-write` filed, marked "blocked on design pass 2". Not fixed now. The read named the shape; the shape resolves by design decision, not by a fix brief.
- `[evidence]` For `design/layers-and-truth-ownership`: "The fact 'what does network:write mean' is held in four places — GRANT_LEVEL_RANKS (3), WORKSPACE_CEILING_LEVELS_RANKS (3.0), the Docker mapping (both → bridge), and the requirement-string surface (absent from every schema). Four holders, no reconciler."

## Ticket rename — bug/main-panel-swallows-assistant-only-without-thinking-block (2026-10-02)
- `[correction]` Ticket premise refuted by read. No thinking-block guard exists anywhere in the render path. The actual drop site is `web_ui/backend/bridge.py::_normalize_for_frontend`, condition `if isinstance(content, str) and content.strip() == "": continue` — empty content, thinking block or not. Ticket RENAMED to `bug/main-panel-drops-empty-content-assistant-message`. Old name preserved here for lookup. Fix PR will name the actual surface.

## Display color conventions (2026-10-02, operator)
- `[preference]` Whoever writes to the display needs their own color. Core system messages use the reddish color — that's correct and stays. Non-core emitters must use a different color so they don't read as core messages. Earned: the "Session <uuid> loaded. Click Run to continue." banner rendered in the system-notification color, misleading the operator into reading a UI confirmation as a core internal message.
- `[intent]` Original intent of the "session loaded" banner was to signal a cold load (session loaded from disk, took a little longer) vs a cached session. Intent is now retired — banner removed entirely rather than re-homed to status chrome.

## Context/summary mechanism — sysprompt upgrade (2026-10-02, operator)
- `[intent]` The context/summary mechanism belongs in the final sysprompt upgrade before V3 release. Mechanism: agents run forever; context rolls via summarization; previous context replaced by summary + fresh context; keep-last-n-turns rescues verbatim; worst case paste-again. No micromanagement — works ~99%; failures are usually misunderstanding the mechanism, not the mechanism failing. Do not forget this in the sysprompt upgrade pass.

## DockerCodeRunner pool fix — landed (2026-10-02, Main Gen 34)
- `[finding]` DockerCodeRunner reused pooled exec containers by name alone. Docker fixes network mode at container-creation time; a container created under `--network none` keeps that mode forever. A permission change silently reused a stale container. Root: the container name (= pool key = durable label/record key; same string) did not include the capability envelope.
- `[ruling]` Fix landed PR #53, commit `5208d3c` on dev: container name keyed by lifecycle class + envelope. `agent-exec-persistent-{ws_hash}-{env_hash[:8]}` / `agent-exec-ephemeral-{ws_hash}-{session_tag}-{env_hash[:8]}`. `env_hash = sha256(f"{network_mode}:{workspace_mode}").hexdigest()[:8]`. All three name-computation sites route through `build_container_name`. Evidence for design pass 2 (`layers-and-truth-ownership`) — the class reproducing on itself: intent vs state, no reconciler.
- `[finding]` Sibling sites remain unfixed by design: `docker_executor.py` `_build_image` (~L1006), `_remove_container_by_workspace` (~L1121), `get_container_status` (~L1240). Ticket `bug/docker-executor-name-sites-remain`. Queued, not scheduled.
- `[finding]` Legacy-named containers (`agent-exec-{hash}` with no envelope segment) will never be re-found by the new lookup (miss → create fresh). Ticket `hygiene/legacy-agent-exec-container-gc` — name-agnostic sweep, delete if no session holds them. Queue behind current work.
- `[finding]` Three name-computation sites in `docker_executor.py` were found during the adjudication read; grep-before-naming rule worked. Not a defect — the discovery.

## Tickets filed (2026-10-02, Main Gen 34)
- `bug/docker-executor-name-sites-remain` — three sibling `agent-exec-*` name-computation sites unfixed by design (`docker_executor.py` ~L1006 `_build_image`, ~L1121 `_remove_container_by_workspace`, ~L1240 `get_container_status`). Different reuse-or-lookup arguments; not folded into PR #53 to keep one PR = one correctness argument. Queued, not scheduled.
- `hygiene/legacy-agent-exec-container-gc` — name-agnostic sweep for old `agent-exec-{hash}` containers with no envelope segment, not held by any session. Queue behind current work. Not folded into Edge 4 (Edge 4 = structural; this = cleanup).

## Container/session permission model — final (2026-10-01, Main Gen 34)
- `[ruling]` Rule: session may use a container iff container.capability ⊆ session.grant. One comparison, at selection time. No state machine.
- `[ruling]` No per-session container ownership. No session-scoped persistent containers.
- `[ruling]` Drift: if grant tightens mid-job, job continues; new calls in out-of-bounds container denied; Live View tags it; user decides (wait / kill / re-grant).
- `[ruling]` Live View marks out-of-bounds containers with reason ("needs network:write; session has network:none"). Per-row tag only when out of bounds — not a warning on every shared container.
- `[ruling]` No call-time internet gate exists today. Container network config IS the enforcement. Comm gate (future) is per-host/per-protocol/credential egress, not a second on/off layer.
- `[correction]` Earlier ruling said "gate checks session permission at call time." That is the destination, not today's state. Today's check is at container selection.
- `[correction]` Second ruling of the same form: "session may use a container iff container.capability ⊆ session.grant" (this block, above) is ALSO a destination, not current state. Verified read 2026-10-02: the gate is session grant × workspace capability; no container object is ever passed in (agent/core/tool_executor.py::ToolExecutor.execute_tool_calls → _execute_single_tool invokes security/security_gate.py::get_effective_permissions / get_workspace_capabilities / check_required_categories per tool call). Container capability enters only downstream in-tool. Same shape as the first correction above: intent vs state, no reconciler.
- `[preference]` Do NOT build a full session×container permission matrix. Common case is matching grants; matrix serves a rare case and costs real complexity.
- `[read]` Does the tool-executor gate consult container capability, or only session grant? One brief. If only session grant, small change at tool-executor boundary.

## Container flow (2026-10-01, operator)
- `[ruling]` Persistent container flow: session looks for container whose capability envelope contains its grants; uses it. If none, creates one at session's grant level (not higher). If cap full, surfaces to user — user decides.
- `[preference]` No session-to-session guarding. Everyone in a workspace is friends. Sticky notes on containers are the social mechanism.
- `[recheck]` Does the container record have a note/annotation field for "don't kill — long job running"? Confirm; add if missing.
- `[preference]` Persistent = "office machines," not scratch. Container cap 2–6 per workspace: avoid UI overwhelm AND discourage lazy container creation.

## Bugs (2026-10-01, operator)
- `[finding]` Worker panel doesn't show tool calls while pending; shows only after result. Main panel shows correctly. Ticket: `bug/worker-panel-tool-call-not-shown-while-pending`.
- `[finding]` Main panel swallows assistant-only messages without a thinking block; suspected cause of missing final messages. Ticket: `bug/main-panel-swallows-assistant-only-without-thinking-block`. Read-first: check render condition.

## Phase 3 / future vision (2026-10-01, operator)
- `[intent]` Model: global = catalog; workspace = imported set; session/worker = what this employee/intern can use, bound by permissions + ceiling. Same shape for session and worker config.
- `[intent]` Worker config gap: no UI for a worker's tools, provider, sysprompt, core config. Strand: `ui/worker-config-panel`. Rule: only workspace-imported items appear in a worker's picker.
- `[intent]` Phase 3 shape: full resource configurability — landing page to set up resource → import to workspace → use in session/worker.
- `[intent]` Query runtime statistics — turns, wall-clock, tokens per turn. Design pass: `design/query-statistics-and-observability`.
- `[intent]` `respond` tool carries a timestamp. Strand: `feat/respond-tool-timestamp`.
- `[intent]` Per-workspace default container settings. Strand: `backend/workspace-container-defaults` (already named/held).
- `[intent]` Workspace page must become a real page, not a blocking panel. Full container control: resize, restart, stop, remove, edit Dockerfile/size/CPU quota/tmpfs.

## Session construction and modes (2026-10-01, operator)
- `[ruling]` Session = mode (sysprompt + tools) + defaults (core params + provider) + user-set permissions + workspace-imported resources/workers, bound by workspace ceiling.
- `[ruling]` Mode is a template; session owns its own sysprompt/tool copy after edits.
- `[ruling]` "Save as default" stores: temperature, max_turns, truncation_limit, token_max, provider. NOT permissions.
- `[ruling]` Tools unavailable in workspace are masked from session's effective set.
- `[intent]` Rename: engineer/agent → sync-multiagent / single. Async multiagent is future.

## Live View tab (2026-10-01, operator + Main)
- `[ruling]` Live View shows: workspace path, session mode, running workers, current permissions, active containers (persistent tagged `shared`, ephemeral while alive, resource). No birth certificate log — that's the workspace page.
- `[ruling]` Show all workspace containers, tag `shared`. Do NOT track per-session container usage.
- `[preference]` Office/employee/intern model confirmed: workspace/office, session/employee, worker/intern, persistent container/machine, resource/gated room.
- `[preference]` Shared banner stays.

## Q-1 / Q-2 rulings (2026-10-01, Main Gen 34)
- `[ruling]` Q-1: shared containers do NOT render cross-session permission divergence. Container row shows its own state; session-relative affordances live on buttons, not rows.
- `[ruling]` Q-2: no starter-ownership. Persistent containers are workspace property. Session-panel stop/restart gated on idle (no worker holds it); held rows show holder. Removal is workspace-page only.

## Layers and structure
- `[intent]` Three-layer mental model: Layer 1 Global (building — workspaces, resources, providers, creds, sysprompts, modes, recent sessions). Layer 2 Workspace page (office — imported creds/resources, resource run-mode host vs container, allowed tools, worker config, persistent workspace containers). Layer 3 Session view (three panels: config/session panel, main agent, worker panel).
- `[preference]` Workspace is its own page, not a panel inside a session.
- `[preference]` Session config panel should be named "session panel," not "config panel."
- `[preference]` The session panel's "workspace" tab should be renamed — it's the *live view* of workspace machinery, not the workspace itself. Candidates: activity, live view.
- `[preference]` The session's workspace tab should show containers again: persistent (workspace-shared), resource, ephemeral.
- `[preference]` Shared-banner stays: "these are workspace containers, not necessarily used by this session." Do not track per-session container usage; simpler is better.
- `[preference]` Workspace containers are few (2–6) — number manageable from the workspace page, not the session tab.

## Modes
- `[intent]` Modes become user-editable: tools + sysprompt pairings, not fixed. Currently agent/engineer/custom, hardcoded.
- `[intent]` Rename: engineer/agent → multiagent/single-agent.
- `[intent]` Future: multiagent split into async vs sync. Not now — tooling and sysprompts insufficient.
- `[intent]` Future modes: "introduction" (helps user set up a new workspace — explain permissions, resources, write Dockerfile), "website building," others.
- `[question]` If a mode expects a tool the workspace hasn't imported, what happens? Probably mask out. Rule later.
- `[finding]` Tools cannot currently be changed by user. Was called "a stupid idea" in retrospect.

## Save as default
- `[intent]` "Save as default" button: persist a session's setup so the next session uses it. Contents: agent core config (temperature, truncation limit, token limit, max turns) + provider. *Not* permissions — new sessions set those fresh.
- `[recheck]` Sysprompt ownership: who owns the session's sysprompt? Does a session file reference a global sysprompt, or carry its own? Operator preference: users can edit; this is their machine.

## Session/agent config
- `[recheck]` Are tools part of agent core config? Same class as permissions were (permissions were ripped out because agent config only updates at idle). If tools are in agent config, same surgery awaits.
- `[finding]` Permissions were ripped out of agent core config because agent config only updates when agent is idle; permission changes took effect only on next run. Same problem may exist for other config.

## Workspace features missing
- `[finding]` No delete-workspace button.
- `[finding]` Users cannot create workspaces on other partitions (e.g., D: on Windows).

## Bugs / suspected
- `[finding]` CheckSystem tool — does it show what the machine actually has? Unknown.
- `[finding]` Worker changes — do they propagate to all sites where the worker is shown? Unknown.
- `[finding]` Vault integrity check still shows errors. Root cause unknown.
- `[finding]` WorkingDocument tool ignored a requested id and minted a hash.
- `[finding]` Agent halts mid-strand (ticketed).
- `[finding]` Container/workspace-id mismatch (ticketed).
- `[finding]` Session workspace tab lost containers in recent migration.

## Preferences / communication
- `[preference]` Panel shows machines, not records. Live state, not history. Dead records collapse behind "show archived."
- `[preference]` "Shared" banner on resource containers is good — keep.
- `[preference]` Lead with concrete, then name the abstraction.
- `[preference]` Rulebook is a plan; code is enforcement.
- `[preference]` Five-thermostats analogy for the design pass.
- `[preference]` `user_history` precedent — LLM output goes somewhere it can't be changed.

## Process
- `[intent]` Mandatory-stop onboarding doc for new Main engineers — see `MANDATORY_STOP.md`.
- `[intent]` "Write it once and forget about it" — the operator has repeated these points to every Main; they must persist somewhere permanent.

## Banner UX (2026-10-01)
- `[finding]` The one-shot "Session <id> loaded. Click Run to continue." banner persists in the transcript after the session is live — permanently in sight, above the agent output. Annoying even after the accumulation fix (#50). Two candidate remedies, operator undecided: (a) auto-dismiss after first user action / first query; (b) demote out of the transcript to a less prominent surface.
- `[recheck]` Operator recalls an earlier "session reloaded" behavior that did not persist this way — an earlier UX existed and was lost. Confirm against git history before designing.
- `[intent]` Separate ticket from accumulation. Suggested name: `ui/session-loaded-banner-persistence`.

## Method — ticket-name drift (2026-10-01)
- `[finding]` Ticket names can describe a different surface than where the fix lands. When closing, the PR body names the actual surface, not just the ticket name. Earned: `bug/session-workspace-tab-missing-containers` was closed by PR #51, but the fix landed on `WorkspaceDetailPage` (the workspace page's container tab), not the session panel's workspace tab. Fourth live instance of the divergence family — name vs. actual surface, no reconciler.

## HAR resolution — two fetch paths (2026-10-01)
[finding] The session panel's "Live View" (ex-"workspace") tab and the workspace page (`#/workspace/:id`) use entirely separate fetch paths. HAR-verified 2026-10-01: the panel fires only `/workers/active?session_id=…`; the page fires `/summary`, `/domain_allowlist`, `/dockerfile`, `/containers`, `/workers/blueprints`. The panel never calls `workspaceStore.fetchWorkspaceConfig`, so PR #51 had no effect there. Infra's "six siblings fire together" clue is retracted — a filtered-capture artifact.

## Two retracted premises, verified (2026-10-01, branch ui/session-panel-live-view → 73b6521)
[correction] (a) "The retired panel's shared-banner wording must be recovered verbatim from `d395b35^:WorkspacePanel.jsx`" — FALSE. `show_file ref=d395b35^ web_ui/frontend/src/components/WorkspacePanel.jsx` shows the retired panel had NO banner element at all. Its container block was exactly `<label><strong>Containers</strong></label>` + `<ContainerListPanel workspaceId={workspaceId} />`; the re-mount now reproduces that block byte-for-byte — nothing to recover. The only "shared" affordance is ContainerListPanel's own per-row chip (SHARED_TITLE = "Stopping affects other sessions in this workspace.", folded into the stop/remove confirm), restored transitively by the re-mount.
[correction] (b) "[candidate] no live consumer of the containers store slice → 5th divergence-family instance" — FALSE. `workspaceStore.currentWorkspace.containers` IS live: SessionSidebar.jsx:87 reads it and renders the Containers section (SessionSidebar.jsx:224-259), mounted SessionTab.jsx:1253 behind the "Details" toggle (SessionTab.jsx:1136). Only `workspaceList[*].containers` is write-only (written workspaceStore.js:597/599; read only by tests). d395b35 did NOT remove a store-slice reader (it removed the ContainerListPanel/WorkerBlueprintPanel mounts, and ContainerListPanel self-fetches). NOTE: verify a slice is genuinely unread before claiming a divergence instance.
[observation] Consequence of the re-mount: the session view now shows containers on TWO independent paths — SessionSidebar (store slice via fetchWorkspaceConfig/:419 + fetchContainers/:597) and the Live View tab (ContainerListPanel, self-fetch `/api/workspace/{id}/containers`, ContainerListPanel.jsx:176). Same data, two fetch paths — the same divergence shape as the HAR finding above, now surfaced inside one screen.

## Session panel label/tab rename (2026-10-01, ui/session-panel-live-view @ 73b6521)
[decision] Panel header "Config" → "Session Panel"; tab key `'workspace'` → `'live_view'`, label "Workspace" → "Live View" (default tab + pane guard flipped, ConfigPanel.jsx:128/:491/:492/:606). No other 'workspace'-as-tab-key existed in src/. Only 2 assertions repo-wide referenced the old header text; both flipped, none weakened. Full suite 47 files/572 tests green (dev baseline 46/569 + 1 new file/3 tests).

## Session panel / workspace page vision (2026-10-01, operator)
- `[intent]` Session panel (Live View tab) is the primary view. In one place: workspace path, session mode (engineer/agent → sync-multiagent/single; async future), current workers + agent-property controls, and all workspace containers.
- `[preference]` Show ALL workspace containers in the session view, tagged `shared`. Do NOT track per-session container usage — the cross-session state machine is explicitly rejected as a nightmare. Simpler is correct.
- `[intent]` Ephemeral = session-private (shown while alive). Persistent + resource = workspace-shared (shown always, tagged).
- `[intent]` Workspace page must become a real page, not a blocking panel. Full container control: resize, restart, stop, remove, edit Dockerfile / size / CPU quota / tmpfs.
- `[intent]` Per-workspace default container settings (the "stronger office computers for the training workspace vs. HR office" model).
- `[intent]` Container cap per workspace (2–6). Purpose: avoid UI overwhelm AND discourage agent laziness creating persistent containers. Persistent = "office machines," not scratch.
- `[question]` Q-1: cross-session permission divergence on shared containers — if session A grants network:write, session B does not, and a shared container has internet on, does A show "out of sync with session permissions" while B doesn't?
- `[question]` Q-2: container ownership model across sessions — is there a rule (only starter may free) or is it "everyone be careful"?

## F5 permission revert (2026-10-07, Edge 1 strand close)
- `[finding]` F5 permission revert -- the operator flagged that after Apply + F5, filesystem showed `read`, not the expected value. He thought it was a fail-closed default. It was Class B: an unreadable grants read on the enforcement path returning the pydantic permissive default. Confirmed post-hoc. **He was right before the mechanism was named.** Tag as catch, not note.

## design/b3-fold-strongest-vs-smallest (2026-10-08, Main Gen 35 close-out)

- `[deviation 30]` B3 (`8f59d03`, Edge 3 Stage 1 sub-strand B) ships a
  STRONGEST-WINS fold across the stacked permission tables. The plan prose said
  SMALLEST-WINS.
- `[ruling]` PRESERVE strongest-wins. The 12 tests in the B3 set are the
  authority; the plan prose is wrong. Do NOT change code.
- `[ticket]` `design/b3-fold-strongest-vs-smallest` — reconcile the plan text with
  the shipped fold (or record the plan prose as superseded). No code change.

## bug/docker-build-egress-not-gated
Filed 2026-10-12 from spare-agent live test on `fierce-owl-43`. Severity: security.
Class: UI-lies — the config panel says `network: banned`, the build path egresses anyway.

Reproduction: session with `network=banned`, `container=true`. `MCPValidator(test_connection=true)` denied as expected. `ContainerBuildTool(tag="tm-r2-build-probe")` succeeded, pulled ~60 MB from PyPI (fastapi, uvicorn, pydantic, openai, anthropic, tiktoken, docker, mcp, cryptography, lxml, uvloop), built and tagged image.

Root cause: `ContainerBuildTool` declares only `container:true` (`tools/container_control.py`). Egress happens inside the Docker daemon's build path; TM's permission layer never sees it.

Impact: `network=banned` does not prevent remote fetch via build. Config UI gives the operator a false sense of egress control.

Candidate mitigations: (a) declare `network:outbound` on `ContainerBuildTool`; (b) pass `--network=none` to build steps when network is banned; (c) gate `RUN`/`FROM` fetches behind explicit build-time egress allowance.

Regression test: assert build under `network=banned` cannot reach network.

Sequencing (Main Gen 38): fix-strand, not emergency. Queue after testpaths, in the container-envelope group. The container infra audit informs the fix.

## bug/container-ceiling-three-sites
Filed 2026-10-12 from spare-agent live test. Severity: needs-ticket.
Three contradictory ceiling counters:
- `security/admission_gate.py::_counts_toward_ceiling` — counts by lifecycle class `persistent`, no status filter ⇒ exited containers occupy slots. Enforces at `:442-456`.
- `infra/container_manager.py` — enforcement site ~1590-1599; the counting
  helper `_counts_toward_limit`/`_active_containers` lives earlier in the
  same file (~745-775). Corrected on recon 2026-10-12.
- `infra/container_registry.py:529-535` — third counter.

Admission gate wins; the other two are misleading. `DEFAULT_MAX_CONTAINERS = 6`.

Reproduction: stop `tm-cap-5`, try create `tm-cap-8`/`tm-cap-9` ⇒ both refused `container limit reached (6/6)`. `ContainerListTool` count never drops.

---
RECON UPDATE 2026-10-12 (Infra Gen 38): the "three counters" framing is
too coarse. Accurate shape is ONE live contradiction + ONE dead path.

- (a) `security/admission_gate.py::_counts_toward_ceiling` — status-BLIND,
  enforcer, UNTESTED (gate fakes carry no `status` attribute),
  UNDOCUMENTED. Executed mutation: patching it to exclude terminal states
  → 0 tests fail across the two counter suites.
- (b) `infra/container_manager.py::_counts_toward_limit` — status-FILTERED
  (excludes exited/dead/removing), documented, 8 tests.
- (c) `infra/container_registry.py::ContainerRegistry.request_container` —
  DEAD. Zero shipped callers. `use_container_registry` is a retired
  session-config key popped by a legacy shim; factory now passes
  feature_check=None.

Fix shape: one-file fix in `admission_gate.py` + new tests in
`tests/test_admission_gate.py`, pending Main's STOP-CHECK ruling on
whether `exited` persistent containers should occupy a ceiling slot.
Do NOT fold dead-counter cleanup into this fix.

---
RULING 2026-10-12 (Main Gen 38): INCLUDE — a stopped persistent container
DOES occupy a ceiling slot. A stopped container still clutters the panel,
still takes disk, still exists as workspace property. The manager's
message — "Stop or remove a running container to free a slot." — is the
bug. Correct message: "Remove a persistent container to free a slot."
Stop does not free; only Remove does.

Supersedes the "pending Main's STOP-CHECK ruling" clause above.

The gate's include-behaviour is CORRECT as-is. What is missing is the
tests: `tests/test_admission_gate.py` carries status-less fakes, so the
behaviour is currently unenforced by the suite. Same fix-strand: add
tests pinning Include.

The final line above — "Do NOT fold dead-counter cleanup into this
fix." — still stands.

## bug/container-build-false-success
Filed 2026-10-12 from spare-agent live test. Severity: needs-ticket.
`ContainerBuildTool` with same tag returns `success: true` in 11–17 ms with empty `build_log` — a silent no-op reported as success. No `force`/`rebuild` parameter; passing `build=true` is silently ignored.

Fix: add `force`/`rebuild`; distinguish `built: false, reason: "image up to date"` from an actual build. Never return `success: true` with empty log for a skipped build.

Also: docstring at `tools/container_control.py:504-513` contradicts observed behaviour re `requirements.txt` staging.

## bug/container-remove-tool-missing-from-schema
Filed 2026-10-12 from spare-agent live test. Severity: needs-ticket.
CAVEAT: likely session-staleness; the machine has been restarted since. Re-verify before fixing.
`ContainerRemoveTool` exists in the tree, is registered in `tools/__init__.py`, but was absent from the running session's tool schema. `CheckSystem runtime_state` reported `enabled_tools_count: 0`. A fresh import returns 37 tool classes including it.
Impact: both Bug 1 (round 1) and the reuse-with-different-image message (round 2) instructed the user to remove a container via a tool the session didn't expose.

## bug/file-editor-line-numbers-list
Filed 2026-10-12 from spare-agent live test. Severity: needs-ticket.
`FileEditor line_numbers=[1, 6]` errors "Invalid line_numbers parameter: [1, 6]" even though the tool's own schema documents a JSON list as valid. `"1-6"` (string) and bare ints work.

## design/exec-blocked-by-fs-read
Filed 2026-10-12 from spare-agent live test. Severity: needs-ticket.
Only `ContainerExecTool` overrides the base `container:true` category — it *adds* `filesystem:write`. Under `filesystem=read`, exec is denied; the coupling silently disables the entire in-container execution surface (exec + DockerCodeRunner) when filesystem is tightened. Non-obvious from the config UI.

Two decisions: (a) allow exec under `fs=read` in a read-only-inspection mode; (b) or document the coupling prominently in the permission panel.

## hygiene/respond-requires-fs-write
Filed 2026-10-12 from spare-agent live test. Severity: hygiene.
`Respond(report_body=…)` is gated on `filesystem:write`. Under `fs=read` the agent cannot deliver a downloadable report — correct in spirit (it writes a file), but the user experience is a silent loss of the delivery channel.
Fix: explicit message, or fallback to inline content.

## hygiene/cross-session-container-reach
Filed 2026-10-12 from spare-agent live test. Severity: hygiene, security-adjacent.
`ContainerStop` / `ContainerExec` / `ContainerStatus` accept any container_id in the workspace, including other sessions' containers. `ContainerStatusTool recent_commands` exposed other sessions' command history via the shared host-bind `/home/agent/.local/usage.log` (9.7 MB append-only — a cross-session command-history leak).
Not exercised destructively. Two decisions: should one session reach another's containers? Should the shared usage.log be per-container?

## design/inv5-readonly-audit-gap
Filed 2026-10-12 per Main Gen 38, live Inv.5 violation.
Pass 2a's Inv.5 — "an agent cannot be refused by a mechanism it cannot inspect." Under `filesystem=read`, exec is denied (per design/exec-blocked-by-fs-read), and `ContainerStatusTool` does not report network mode or mounts. A read-only operator therefore cannot audit what a running container can reach.
Fix: add network mode + mounts to `ContainerStatusTool`, so read-only inspection is possible without exec.

## bug/agent-response-lost-in-transit
Filed 2026-10-12 (operator, recurring).
Symptom: agent produces a report, but it does not reach the display. The log shows the core produced it; the last visible action is a random tool call. The session appears to stop mid-task.
Frequency: infrequent but recurring.
Impact: user sees the machine "stop in the middle of nowhere," loses the report, has to re-ask.
Not for immediate fix. Goal: logging in place for the next occurrence. Proposed: (a) an explicit "assistant message emitted" log line at the relay boundary; (b) a "message delivered to display" log line; (c) diff between the two when they diverge. When it recurs, the operator captures both log files and we have evidence.
Filed as a plan, not a task.
## 2026-10-12 — **RECURRENCE 2026-10-12 (Gen 38, Main) — escalation, filed a...

**RECURRENCE 2026-10-12 (Gen 38, Main) — escalation, filed at the operator's explicit request: "tell the engineer that this loss of responses is becoming a problem."**

- **Symptom:** the agent's final response is produced by the core but never reaches the display. The operator's last visible action is an unrelated tool call; the message content is simply absent. Recurs: twice today, and ≥4 times across the last 3 sessions.
- **Evidence this occurrence:** the core emitted the full consolidated report for the ceiling strand (KB append → branch/HEAD → STOP-CHECK 5 items → RED → fix → GREEN → pinning test + mutation probe → full suite → diff summary) and none of it was displayed.
- **Key diagnostic fact:** *no work product was lost.* All state survived on disk (git branch `fix/container-ceiling-include-semantics` @ 171f9b44 + five modified files). The failure is in **delivery/display of the message payload**, not in agent execution. That narrows the fix to the session/display plumbing around `Respond`, not to the agent or the worker layer.
- **Impact:** every loss costs a full verbatim re-post (expensive; also risks the operator acting on a stale view), and it silently degrades the audit trail for stop-and-report workflows where a single unsent message is the only record of a mission-critical decision.
- **Requested of the engineer (Main):**
  1. **Instrument the delivery path:** log a `response_id` + payload hash at emit and at delivery; surface any mismatch or drop instead of failing silently.
  2. **Idempotent replay affordance:** let the operator retrieve the last final response without re-running the turn (a "replay last response" escape hatch), since the content still exists core-side.
  3. **Priority:** treat as P1 in the WA refactor — same failure family as the D2 literal-loss class (content produced, content not preserved end-to-end).
- **Local mitigation currently in use:** checkpoint work to disk/KB **before** calling `Respond`, keep a compact handoff summary, and re-post verbatim on request. Adequate as a workaround; not adequate as a fix.

## bug/periodic-sweep-liveness-gap
Filed 2026-10-12. Severity: hygiene (handoff §3 framed it security-adjacent;
recon refuted the fail-open reading).

What the code actually does: the `_run_container_sweeps` table in
`web_ui/backend/server.py` guards exactly one of three rows
(`orphan-resource` gets `_periodic_registry_has_workspaces`;
`exited-workspace` and `orphan-records` get `guard=None`). The asymmetric
guard is DELIBERATE and correct: guard present ⇔ callee dangerous. The two
unguarded rows are safe because their callees fail-closed on empty input.
There is NO live fail-open path.

What IS real:
1. `_periodic_registry_has_workspaces` returns False for BOTH an empty and an
   unreadable registry — indistinguishable to callers. A persistent registry
   fault silently disables the orphan-resource sweep on every tick; WARNING
   log only, no metric, no alert.
2. `_sweep_orphan_resource_containers` guards against fault but not clean-empty
   — passes `[]` into the fail-open callee `sweep_stale_resource_containers`.
   Safety rests on caller discipline; pinned as deliberate by
   `tests/docker/test_resource_sweep_registry_fault.py::test_valid_empty_registry_still_sweeps`.
3. The two `guard=None` rows are unlocked by tests: add a guard to either
   sibling → no test fails; remove the orphan-resource guard → 2 tests fail.
   The asymmetry is enforced in one direction only.

Worst case: persistently empty OR faulting registry ⇒ all three sweeps stop
doing work; orphans accumulate indefinitely; signalled only by WARNING lines.

Fix shape when capacity opens:
- Distinct log/metric for fault vs empty on the three skip paths.
- Add entry-level tests for the two `guard=None` rows (lock the design in
  both directions).
- Do NOT change the clean-empty behaviour of `_sweep_orphan_resource_containers`
  — deliberately pinned by test.

Related slugs already in test files: `bug/periodic-sweep-no-registry-recheck`,
`bug/boot-sweep-registry-empty-not-fail-closed`. Not duplicates — this
captures the fault-≡-empty indistinguishability and the missing metric.

## [principle] 2026-10-12 — Reduce host dependency
Every arc should name what host responsibility moves to the agent's tool
surface. History: no git tool → git `ask` → git `write_on_feature_branch`.
Pattern: a capability moves from host → agent when the machine can verify
the action's safety structurally. Ask first, then allow when the gate is
proven. The relay tax reduces as a consequence.
Long-range target: a session that acts as an engineer, dispatching work to
a working session, replacing the manually-operated engineer layer.
Not now — named as the destination.
Source: operator mental-model dump 2026-10-12 §5.

## design/build-image-dockerfile-fallback-asymmetry
Filed 2026-10-12, from Branch B recon.

`ContainerManager.build_image` resolves the Dockerfile vault-first, with
a fallback to `<workspace_root>/Dockerfile` if the vault file is missing.
Requirements used to have the same shape; Branch B closes the requirements
side. The Dockerfile fallback remains.

Main's ruling 2026-10-12 scoped the fix to requirements. The Dockerfile
fallback is a separate decision — either deliberately kept (as a legacy
path) or aligned with the vault-only pattern. Not urgent; named here so
it is not forgotten.

## [findings] 2026-10-12, Infra Gen 39 — container-infra audit

Per-path execution-class table delivered to Main. Zero paths inferred-from-
failure. Four off-taxonomy call sites; the four below are independent of the
MCP severity call. MCP spawn classification deferred pending a caller-chain
read.

### hygiene/mcp-client-duplicate-file

`tools/mcp_client.py` and `tools/mcp_client_new.py` are near-identical.
`StdioMCPClient` is byte-equal modulo one method name (`_read_loop` vs
`_receive_messages`). D-class duplication (Inv.6). Which is live? Neither
file documents deprecation.

### bug/check-system-event-log-host-shell-out

`tools/workspace/check_system.py::CheckSystemTool._query_event_log` runs
`subprocess.run(["tail","-n","50",file_path], ...)` on host with no
SandboxedExecution wrapper, no class declaration, no permission check.
Fixed argv; bounded. Correctness/hygiene, not security.

### hygiene/worker-exec-docker-cli-fallback

`tools/workspace/worker_execution.py::_docker_exec_kill` falls back to
`subprocess.run(["docker","exec",cid,"kill",pid])` when
`container_manager.exec_run` is unavailable. Asymmetry worth naming: the
git family raises loudly in the equivalent situation; this one shells to
the docker CLI. Teardown context, not agent-invoked. Hygiene.

### missing/recon-note-execution-class-taxonomy

`machine_model.md:162-163` says "Open gap: one host shell-out is not yet on
this taxonomy (see recon note)." No recon note is on disk under
`.thoughtmachine/knowledge/`. `open_items_register.md:57` and `:200` repeat
the claim. Candidate: MCP stdio spawn. The pointer itself is missing; the
reference should be updated to point at a real finding, not a missing note,
once the MCP read returns.


### hygiene/mcp-validator-dead-code

`tools/mcp_validator.py::MCPValidator.execute()` returns immediately:

    return "Error: MCPValidator is deprecated. Use MCPServerConnect with a registered server name instead."

Everything below that line is unreachable — including the `create_mcp_client`
calls that would have reached `StdioMCPClient.start`. The tool is a stub.

Yet `MCPValidator` is still in `agent_config.json::enabled_tools` — so it
occupies a slot, appears in tool listings, and answers only with the
deprecation string.

Confirmation of the audit finding: the P1-shaped suspicion (agent-supplied
command → host spawn) does **not** hold. The dead body cannot execute; the
live MCP path (`tools/mcp_server_connect.py::MCPServerConnect`) routes through
`SandboxedExecution` with `required_category="mcp:connect"` and reads a
vault-only, agent-immutable registry. Live path correctly classified.

Operator ruling (2026-10-12): nothing MCP is in use, no legacy to preserve.
The MCP layer can be rebuilt fresh when Phase 3 reaches it. Recommend: remove
`MCPValidator` from `enabled_tools` and delete the stub file; `mcp_client_new.py`
is already filed as `hygiene/mcp-client-duplicate-file` (dead code, no
importers). Whether to remove the wider MCP layer (mcp_manager.py, mcp_client.py,
mcp_examples/) or keep as scaffolding is a Main ruling.


### hygiene/kb-gitignore-asymmetry

`.thoughtmachine/knowledge/project/architecture_audit_2026-09.md` is tracked
by git while the rest of `knowledge/` is gitignored. Almost certainly an
accident: a file force-added before the ignore rule landed, kept tracked by
git's "already-tracked stays tracked" behavior. Either `git rm --cached` it,
or document why it's an exception. Decide, don't leave ambiguous.

Ruling by Main 2026-10-12: KB is intentionally host-local; the audit header
exception is not intended. File as hygiene, do not fix this strand.

### design/kb-durability-and-backup

The KB is single-host by design. If the disk dies, findings die with it.
The durability mechanism is the handoff + operator carrying the machine
forward + paste-relay, not git. Named, not scheduled.

Options later: periodic backup, encrypted export, operator-driven sync.
Do not confuse "KB durability" with "tracked KB files" — the latter is
explicitly rejected (see hygiene/kb-gitignore-asymmetry).

Ruling by Main 2026-10-12: named, not this strand.


## [findings] 2026-10-12, Infra Gen 39 — post-egress-fix

### hygiene/worker-terminology-collision

"worker" means two distinct things in this project:
- **TM worker** — session sub-agent (cognitive work, `tools/workspace/worker_thread.py`).
  Product concept.
- **Dispatch** — the containerized agent that runs briefs and reports back.
  Infra's tool.

Ruling by Main 2026-10-12: do not unify; rename Infra's term to avoid the
collision. **Chosen term: `dispatch`** (short form) / **`containerized dispatch`**
(full). All future Infra briefs, reports, and KB entries use "dispatch" for
the containerized agent. "worker" is reserved for the TM product concept.

### bug/worker-tool-result-truncation-silent

Observation: the dispatch's tool result has a ~10k-token cap that silently
truncates large outputs. Observed on `pytest --tb=long` on docker-py failures
(~135 KB / 2719 lines); output was elided without a visible marker.

Two questions to answer before classifying:
1. Is the cap silent? Does truncation leave a marker in the tool result?
   If silent — real bug (UI-lies class at the tool-result boundary).
2. Is it the mechanism behind `bug/agent-response-lost-in-transit`?
   Same shape: content produced, content not preserved end-to-end.

Ruling by Main 2026-10-12: own read, **not hygiene**. Queue **after** the
container-envelope group. Do NOT fold into envelope group.


## [findings] 2026-10-12, Infra Gen 39 — post-egress-fix

### hygiene/worker-terminology-collision

"worker" means two distinct things in this project:
- **TM worker** — session sub-agent (cognitive work, `tools/workspace/worker_thread.py`).
  Product concept.
- **Dispatch** — the containerized agent that runs briefs and reports back.
  Infra's tool.

Ruling by Main 2026-10-12: do not unify; rename Infra's term to avoid the
collision. **Chosen term: `dispatch`** (short form) / **`containerized dispatch`**
(full). All future Infra briefs, reports, and KB entries use "dispatch" for
the containerized agent. "worker" is reserved for the TM product concept.

### bug/worker-tool-result-truncation-silent

Observation: the dispatch's tool result has a ~10k-token cap that silently
truncates large outputs. Observed on `pytest --tb=long` on docker-py failures
(~135 KB / 2719 lines); output was elided without a visible marker.

Two questions to answer before classifying:
1. Is the cap silent? Does truncation leave a marker in the tool result?
   If silent — real bug (UI-lies class at the tool-result boundary).
2. Is it the mechanism behind `bug/agent-response-lost-in-transit`?
   Same shape: content produced, content not preserved end-to-end.

Ruling by Main 2026-10-12: own read, **not hygiene**. Queue **after** the
container-envelope group. Do NOT fold into envelope group.


## [findings] 2026-10-12, Infra Gen 39 — envelope gate read

### bug/selection-time-envelope-gate-absent

Ruling by Main 2026-10-12. Confirmed by read-only recon.

`tools/container_control.py` declares only blanket categories:
- `_ContainerControlBase.required_categories = ["container:true"]` (line 97)
- `ContainerExecTool.required_categories = ["container:true", "filesystem:write"]`
  (line 289) — the only override

No tool body inspects the target container's envelope (`network_mode`,
`workspace_mode`, mem_limit, cpu_quota) against the calling session's grants.
All 8 use paths (Start, Exec, Stop, Status, List, Build, Logs, Remove) call
the manager directly with the container_id.

`operator_observations.md:317` already recorded the destination:
"session may use a container iff container.capability ⊆ session.grant" —
marked as destination, not current state; "no container object is ever passed in."

PR #84 keyed container *names* by envelope, which prevents stale reuse by
identity collision but is not a gate. The friend's finding (session with
`ro + network:banned` used container with `rw + internet`) reproduces today.

Fix shape (per Main ruling): one function, called from every use path — same
shape as the ceiling. Not 8 separate checks. Take a container spec + session
grants, return pass/fail. Write it so it survives the bins migration (after
bins, the check becomes "container's bin per action ⊆ session's bin per action";
do not bake rank comparisons into 8 call sites).

Coverage caveat pending one more read: `ContainerManager.exec()` and sibling
methods may check admission in the callee. Confirmatory read dispatched.

### bug/container-list-workspace-scoped-not-session-scoped

Ruling by Main 2026-10-12. Confirmed by read-only recon.

Read paths filter by {workspace-label, class, session-membership} — three
axes, none of which is the per-container envelope.

- `ContainerManager.list_containers` (container_manager.py:3260) — filters by
  label `thoughtmachine.workspace_id=<ws>`, then drops by *class*
  (`_agent_access_denial(self.class_of(container))`). No envelope, no session.
- `ContainerRegistry.get_containers_for_session` (749) — narrows by session
  against the registry's own map.
- `ContainerRegistry.list_all` (759) — no filter at all.
- `_list_workspace_live_containers` (server.py:3550) — filters by workspace
  label.
- `ContainerListTool.execute` (container_control.py:482) — passes
  `list_containers()` through unchanged.

Under `network:banned`, a session sees every container in the workspace,
including ones with broader envelopes. Partial leak.

Fix shape (per Main ruling): visibility over force — show them, tag them, do
not hide them. An action whose effective availability is denied should be
shown tagged "not available this session" (Inv.5 legibility). But *tagged*,
so the agent plans correctly.

### hygiene/dispatch-bootstrap-missing-container-provision

Ruling by Main 2026-10-12. The containerized dispatch (Infra's orchestration
tool) has no container-provision step at session start. This session's
dispatch reported no shell: `host_bash:banned`, `ContainerExecTool` unable to
find its container (persistent `egress-fix` gone), toolset without exec.

Read-only recon worked; the *next* strand (the envelope-gate fix) needs
pytest, which needs a shell. RED-first is impossible without one.

Bootstrap shape (per Main ruling): at session start the dispatch session has
`container:true` + `filesystem:write` in grants; a fresh persistent container
is created at the session's envelope; the container ID is noted in the
dispatch's initial context; if missing or unreachable, the dispatch stops
before doing work it cannot verify.

Do NOT use `host_bash:allow` for a dispatch session (contradicts security
posture). Do NOT fall back to host-side operator test runs (the relay tax).


## CI — Platform Matrix/web-ui-backend-suite (advisory) (2026-10-12)
- Check: `Platform Matrix/web-ui-backend-suite (advisory)` — workflow `.github/workflows/platform-matrix.yml`, job `ubuntu-suite` (sibling of the required `Ubuntu - full test suite`).
- Baseline: red on `dev` at run `3826225` on the same workflow; confirmed independent of PR #91.
- Recommendation: promote the advisory check to required once it is green on `dev`; today it is `[informational]`, becomes `[blocker]` on promotion.

## Wave 1b read-first - informational findings (2026-10-12)

- STALE-DOC - `design_intent_state_divergence_family.md` Edge 4 says "`_heal_missing` rebuilds from current policy, not `intent_snapshot`"; the shipped code contradicts it - the rebuild source IS the record's stored `intent_snapshot` (via `rebuild_config_from_snapshot`). The doc now describes the deleted behaviour and should be corrected.
- NAMING DEBT - the constant `_HEAL_AUDIT_RECREATED` has the value `CONTAINER_START_MISSING_HEAL`; the name implies an operator recreate but it belongs to the agent auto-heal path (`_heal_missing`). Both in `infra/container_manager.py`. The operator recreate has no dedicated audit constant (it audits via the generic `CONTAINER_CREATE source=recreate`).
- SERVICE-CLASS UNHANDLED - `ContainerManager.start()` never converts a stale `docker_id` to unbound for resource/service records (`policy_for(...).own_lifecycle is True` skips `_reconcile_record_unbound`). SERVICE is additionally skipped by `sweep_orphan_container_records`, refused by `reap_container_record`, and skipped by `_gc_should_skip`, so it retains a stale `docker_id` indefinitely. No live service records exist today (the class is declared but unpopulated), but the gap is real. In `infra/container_manager.py`.

## Operator resource edit - informational defect (2026-10-12)

- **DEFECT:** operator resource edit at `workspace_container_resources` (`web_ui/backend/server.py`, `PATCH /api/workspace/{ws}/containers/{rid}/resources`) is user-visibly broken - mem/cpu edits neither take effect nor persist. The write lands in `intent_snapshot` via `update_record`; `start(allow_fresh=True)` rebuilds from `_compute_config` (session policy), and `_run_container` sources mem/cpu from `ContainerManager.__init__` instance state. Guardrail 7 then overwrites the record's snapshot from live attrs on the same path. **Two symptoms, one finding** - fixing one without the other leaves the divergence alive under a different shape.
- **CLASS:** same divergence family as Wave 1b beta - recorded intent vs live/current-policy state; operator write to the intent is not honoured at rebuild.
- **SEVERITY:** `[informational]` today (edits are no-ops; no data loss). Promotes to `[blocker]` if the endpoint is documented as functional.

## CORE BUG — SummarizeTool + Respond batched drops the summary (2026-10-13)

- **DEFECT:** in `agent/core/agent.py`, the post-batch summary block (`_apply_summary_pruning` and everything after) sits below the `final_detected` early-return. A batch containing both `SummarizeTool` and `Respond` commits tool results, skips pruning entirely, exits. Effects: `session.summary` never written, token estimate never updated, context-builder cache never cleared, `context_summarized` event never emitted, no `[SYSTEM NOTIFICATION]` to the model.
- **SELF-SUSTAINING:** `agent/core/state.py` returns exactly `['Respond', 'SummarizeTool']` under critical token restriction — the only two tools allowed are the pair that triggers the bug. Live-lock until reload; reload restores the still-huge context. Workers affected (`worker_thread.py`).
- **GATE:** prerequisite for the system-prompt addendum / LLM-injection work. Do not start that work until this is fixed — same turn loop.

Diagnostic fingerprint: `core.summary` present, `core.pruning` absent, same turn.

## FINDING — system prompt ownership, timing, and silent locks (2026-10-13)

- **OWNERSHIP:** mode owns it, session holds the snapshot, agent re-asserts from in-memory `AgentConfig` every turn — a disk edit is invisible to a live session until restart. `docs/param_ownership_map.md` names `global_defaults` / mode resource as owner.
- **SILENT LOCK:** `SessionConfig.update_prompt()` is a no-op unless `mode == 'custom'` — returns early with no error, no log. Non-GUI clients POSTing a prompt in agent/engineer mode get an unwarned discard. GUI hides it with a read-only preview.
- **CONTEXT-WIPE:** `ensure_system_prompt` deletes every non-summary `role=='system'` message on each turn. Injecting system-role context mid-conversation does not survive. Relevant to the sysprompt-addendum work.

## [bug from use — Windows blocker] git_write mangles paths (2026-10-13)

- **DEFECT:** `git_write` resolves paths against the **Windows host** path space, then hands git a backslash path. Git runs in a **Linux container**, where `\` is a literal filename character. Result: `scripts/select_container.py` arrives as `scripts\select_container.py` — "did not match any files." Every file-explicit `git add` fails on Windows.
- **SECONDARY:** `working_dir` is ignored — git always runs from the repo root. Absolute container paths get rewritten to host paths; the tool believes the workspace root is the repo's **parent**.
- **SEVERITY:** `[blocker]` for Windows users. Workaround exists (directory-only pathspecs), but it's not general — fails when a directory contains untracked files the user doesn't want staged, or has a non-unique name.
- **CLASS:** Layer 5 (Delivery). `machine_model.md` names fresh-install / cross-OS as the sharpest untested gap. This is that gap, hit in the wild.
- **FIX SHAPE:** normalise to POSIX separators before building argv for the containerised backend; validate against the **container** path space, not the host's. In the tool, not the caller's prompt.

**Pattern:** every bug found this session is an assumption about a boundary that no test covers. #90 assumed a kwarg name (test mocked the call). The build gate assumed a live-fire test could verify (cache short-circuited it). This one assumed host paths survive to a Linux backend (CI tests code, not the tool, on Windows). **Boundaries are where untested code hides.**
## bug/network-grant-else-none-fail-open (2026-10-13)
- **DEFECT:** `networkmode=("none" if _net_mode == "none" else None)` at both build call sites (`infra/container_manager.py`, `docker_executor.py`). The SSOT is an allow-list (granted only on explicit allow); the call-site ternary is the inverse (deny only on explicit ban). They agree only because the SSOT is total. Any future mode string that isn't literally `"none"` silently widens the build network.
- **SHAPE:** invert to allow-only. Grant network only on explicit positive mode; every other value -> `"none"`. One-line change, same test surface.
- **QUEUE:** fix-strand. Distinct argument from Wave 4a (when is network granted); not folded.

## Residual — trust gate compares 2 of N envelope dimensions (2026-10-13)
- **FACT:** `_check_exec_drift`'s predicates compare `network` and `workspace_mode` only. `INTENT_SNAPSHOT_KEYS` carries more: `mem_limit`, `cpu_quota`, `oom_score_adj`, `image_ref`, `image_hash`, `hardening`, `restart_policy`, `user`. The trust gate sees two of the key set.
- **QUESTION:** for trust purposes, is the envelope just network + workspace, or the full key set? Wave 4a's brief must read the trust contract doc and D.1 and answer — close the residual with reasoning if the two-axis definition holds, else name it as a follow-up.
- **SEVERITY:** informational. Not a breach by itself; a gap in what the gate declares it covers.

## ci/fresh-install-smoke-per-os (2026-10-14, Infra Gen 41)
- **FINDING:** CI does not test that the machine boots. It runs the Python test suite; it never runs `start_thoughtmachine.sh`. A fresh Mac user could not start TM (PR #101), and no CI lane caught it because none runs the boot ceremony on any OS.
- **FIX SHAPE:** Add a CI job that runs `start_thoughtmachine.sh --check-only` on each OS in the platform matrix. Would have caught the macOS bug (PR #101). Flag surface verified present: `--check-only` also honours `TM_CHECK_ONLY=1`; the underlying checks are `scripts/doctor_checks.py --check-{tools,docker,stale-containers,port,node,dotthoughtmachine}`.
- **CAVEAT — `--check-only` IS NOT A DOCKER GATE:** a failed Docker check is downgraded to a warning and the script continues, exiting 0 (`start_thoughtmachine.sh` L242-287, L397). On a Docker-less runner the job passes vacuously. The job must assert on the doctor output, not just the exit code.
- **DOES NOT CATCH:** substrate-class bugs (VirtioFS "dubious ownership"). No CI lane uses Docker Desktop + VirtioFS. Name this boundary explicitly so the next reader does not expect one job to close both classes.
- **UNVERIFIED:** Darwin-skip location. No `darwin`/`uname` handling found in `start_thoughtmachine.sh`; expected in `scripts/doctor_checks.py`.
- **VERIFIED (this host, 2026-10-14):** the cited merge commit for PR #101, `f2e8bbb8`, is **not an ancestor of `dev`** (`git merge-base --is-ancestor f2e8bbb8 dev` → exit 1). Either the SHA is misquoted or the merge is absent from this clone. Do not restate "merged to dev @ f2e8bbb8" without re-checking.
- **CLASS:** infrastructure gap. Queue behind 4a.

## bug/resource-reap-no-user-axis (2026-10-14, Infra Gen 41)
- **FINDING:** `ResourceContainerManager.ensure_resource` stale-check compares image-id drift, `workspace_id` label, `resource` label — never `Config.User`. Stale 1000:1000 containers reused indefinitely.
- **FIX:** Mirror `ContainerManager._user_drift_axis` (`infra/container_manager.py` ~L399) into the reap predicate. Use `host_user() or "0:0"` (on Windows `host_user()` returns None). Check import direction before choosing reuse vs copy.
- **EVIDENCE:** Code gap confirmed by read. Affected-container names from the handoff are unverified on this host — do not assert "three workspaces fail deterministically."
- **STATUS — FINDING DOES NOT HOLD ON THIS TREE (read 2026-10-14):** `ensure_resource` is at `infra/resource_container_manager.py:1299` and it already consults the axis at `:1392` — `if _user_drift_axis(container, host_user() or "0:0") is not None: stale = True` (imported at `:117`, rationale comment at `:1387-1391`). The working tree is checked out on branch `bug/resource-reap-no-user-axis` — i.e. the recommended fix is present on the very branch dispatched to make it. Before acting on this ticket, re-read `:1380-1395` and establish whether the original gap was in a different reaper (session/executor class) or only on pre-fix `dev`.
- **DISPATCHED:** branch `bug/resource-reap-no-user-axis`.
- **CLASS:** correctness.

## 2026-10-14 — Gen 41 seat, operator items + strand filings

### [principle] KB is a book of memories; archive, do not delete
The KB is not a working cache. Old work is reconstructible history, and the
operator uses it. Artifacts (compiled files, logs, clones) may be removed.
Records (reports, backups, findings, handoffs) are kept — active or archived,
never removed. Encoded in `working_agreements.md`.

### [preference] Vault vs workspace naming collision
`.thoughtmachine/` in HOME is the vault (runtime state). `.thoughtmachine/`
in a workspace is the KB. Same name, two things. Rename vault to `.tm_vault`
someday; migration touches every path reference. Filed as
`design/vault-vs-kb-naming-collision`.

### [intent] Global KB revival
The global KB lives in the vault. The agent reads it at runtime. Untouched
in months, likely stale. Target: the place TM looks for "how to deal with
system problems" — the mac/git/container substrate fix is exactly a
candidate entry. Filed as `design/global-kb-revival`.

### [intent] README opens with sec-breach / trust-breach threat model
Security breach = agent reaches host. Trust breach = agent uses beyond grant.
The machine's promise is about trust; the security layer is the substrate.
User-facing. Filed as `docs/readme-threat-model`.

### [intent] docker_executor.py leaves repo root
Target: `infra/` or a new top-level `executor/` module. Design call, not a
mechanical move. Filed as `hygiene/docker-executor-out-of-repo-root`.

### [strand] design/vault-vs-kb-naming-collision — filed, not dispatched
### [strand] design/global-kb-revival — filed, not dispatched
### [strand] docs/readme-threat-model — filed, not dispatched
### [strand] hygiene/docker-executor-out-of-repo-root — filed, not dispatched

### [strand] feat/agent-ci-monitor-read-only (2026-10-14, Infra Gen 41; Main blessed)
- **WHAT:** Read-only CI/PR status reader. Reports; does not act. First probe into the Delivery (Layer 5) arc.
- **SHAPE — four rules, do not drift:**
  1. Read-only. No push, no comment, no merge, no PR modification.
  2. No credential story. Public repo: no token. Private: scoped read-only PAT. Does not touch the vault credential surface.
  3. Sibling to `git_read`, not an extension. A `pr_read` / `ci_read` tool — clean surface, one job.
  4. Reports, doesn't act. Output shape: "PR #N is red on check X, failure is Y, link." If the report says "I merged it," the shape is wrong.
- **ARC:** Opens the Delivery (Layer 5) arc. Not a between-waves slot-in.
- **SEQUENCE:** Filed. Dispatch when 4a's PR is waiting on CI anyway.
- **CONTAINMENT NOTE:** First strand that gives the agent a network read against a real service. Small and read-only, but IS a widening of reach. If any of {no credential, no write, no persistent state} changes, it becomes a different strand with a different ruling.
- **FULL PUSH AGENT:** Behind Phase 4 (credentials as resource). Not schedulable until then.

### [strand] bug/build-refusal-names-effective-not-contributing-fact (2026-10-14, Infra Gen 41; Main ruled)
- **FINDING:** 4a's refusal message names the *session* config
  (`session_permissions['container']`) but the gate keys on the ANDed axis
  (`session.container AND workspace.allow_docker`). A denial caused by the
  workspace ceiling points the reader at the wrong file.
- **FIX SHAPE:** message names both possible sources, or the gate distinguishes
  which side failed and names that one. Not a blocker for 4a — filed as residual.
- **QUEUE:** behind 4b.

### [finding] Substrate bug fixed in 30 minutes — the machine working (2026-10-14, Infra Gen 41)
- **EVENT:** friend's `git_read` "dubious ownership" failure diagnosed, root
  cause identified (Docker Desktop VirtioFS on macOS returning inconsistent
  file-ownership metadata), cured (gRPC-FUSE), and closed in one session.
- **WHY IT MATTERS:** substrate bug, no code change, no `safe.directory`
  masking, no workaround. Fast, honest, and the operator's friend is
  unblocked. This is the shape we want: find the real cause, name it, cure it,
  move on.
- **CONTRAST:** the Mac start-script failure was a different class — a code
  assumption (Linux-only tools) that shipped because no CI lane exercises the
  Darwin boot path. Both landed the same week. Naming both is useful: substrate
  bugs are environment cures; code-assumption bugs are fixable in CI.

### bug/workspace-bootstrap-test-lags-shadow (2026-10-14, Infra Gen 41)
- **FINDING:** `web_ui/backend/tests/test_workspace_auto_register.py::TestMultipleWorkspaces::test_both_have_full_bootstrap` expects 6 bootstrap files, sees 8. The two new ones are `.git-shadow-mask` (Wave 3b) and `requirements.txt` (vault-requirements work). The bootstrap grew; the test didn't follow.
- **SEVERITY:** advisory. The check `Platform Matrix/web-ui-backend-suite (advisory)` is not required, so it does not gate merge. But it's the only live signal for that directory — the audit already filed `ci/web-ui-backend-tests-outside-testpaths` for the larger coverage gap.
- **NOT A REGRESSION FROM 4a.** The failure existed before 4a landed. Confirmed by the file set: `.git-shadow-mask` was added in Wave 3b, `requirements.txt` earlier. 4a touches neither.
- **FIX SHAPE:** update the expected set to include the two new files, or make the test assert an inclusive superset rather than an exact set. The exact-set form is brittle to intended growth.
- **QUEUE:** behind 4b.

### [principle] When the machine is honest about itself, substrate bugs go fast (2026-10-14, Main ruled)
- **EVIDENCE:** the friend's `git_read` "dubious ownership" bug — diagnosed,
  root-caused (Docker Desktop VirtioFS on macOS), cured (gRPC-FUSE), closed in
  one session. No code change, no masking, no workaround.
- **WHY:** the machine could name what it was doing. Honest instrumentation →
  honest diagnosis → honest fix. The 30-minute close is the shape we want.
- **CONTRAST:** the Mac start-script bug is the other class — a code assumption
  that no CI lane exercised. Both fixed the same week; only the substrate one
  was fast, because only the substrate bug was inspectable in the field.

### bug/cross-session-container-command-leak (2026-10-14, Infra Gen 41; Main ruled)
- **PREDECESSOR:** `hygiene/cross-session-container-reach` (2026-10-12) filed the
  same surface at hygiene severity. Main ruled 2026-10-14: bump to bug.
  Isolation, security-adjacent. The friend-class of bug — small but real.
- **FINDING:** (1) ephemeral containers are reachable across sessions via
  `ContainerStop`/`ContainerExec`/`ContainerStatus` accepting any workspace
  container_id. (2) `ContainerStatusTool recent_commands` exposes other sessions'
  command history via the shared host-bind `/home/agent/.local/usage.log`
  (9.7 MB append-only).
- **VECTOR SPECIFIC:** the operator's container model says ephemeral is
  session-private. Two sessions in the same workspace should not see each
  other's ephemeral containers, nor read each other's commands.
- **SHAPE — separate strand from 4b.** 4b is read-path envelope tagging.
  This is isolation enforcement. Different correctness argument, different tests.
- **QUEUE:** behind 4b, above 5.

### design/resource-container-visibility (2026-10-14, Infra Gen 41; Main ruled)
- **QUESTION:** Should the agent's *tool surface* ever show resource containers
  (git sandbox etc.)? The UI Live View shows them (correct — operators own the
  machine). The tool surface hides them (`_agent_access_denial` denies RESOURCE
  in `list_containers`).
- **MAIN'S REASONING:** resource containers are infrastructure, not
  general-purpose containers. The agent reaches git via the git tool; the git
  tool routes to the resource container internally. The container is an
  implementation detail of a tool, not a container the agent should manage.
  "Visibility over force" applies to containers the session *could* use but
  can't for envelope reasons — resource is not in that category.
- **DISTINCT FROM:** UI panel visibility (Live View shows resource — different
  audience, coherent as-is).
- **QUEUE:** behind 5.

### bug/get-logs-session-check-absent (2026-10-14, Infra Gen 41; filed from 4b)
- **FINDING:** `ContainerManager.get_logs(container_id)` takes any container id
  and fetches logs with no session check. A session can read another session's
  ephemeral container logs by id. Same class as
  `bug/cross-session-container-command-leak`, different vector (logs vs commands).
- **NOT FIXED IN 4b:** 4b is read-path scoping on `list_containers`. `get_logs`
  is a separate surface with its own correctness argument.
- **QUEUE:** behind `bug/cross-session-container-command-leak`, which is itself
  behind 4b (now merged at 4b's PR).

### bug/class-of-degradation-defeats-session-filter (2026-10-14, Infra Gen 41; filed from 4b)
- **FINDING:** `ContainerManager.class_of` falls back to `LIFECYCLE_PERSISTENT`
  when the container's record is missing or unresolvable (documented in its own
  docstring). An ephemeral whose record is unreadable therefore escapes 4b's
  session-name filter and gets the persistent-envelope treatment instead.
  Fail-closed-ish (envelope tag applies) but the row is visible when it shouldn't be.
- **NOT FIXED IN 4b:** the record lookup's vault-resolution divergence is the
  root cause, named in the docstring; fixing it touches the writer too. Separate
  strand.
- **QUEUE:** behind 5.

## 2026-10-14 — [pattern] Anti-pattern family: the report names the wrong mechanism (Gen 41, recorded Gen 42)

Three named members, one shape: **the report names the mechanism it should
have used instead of the one it did.** Each is an honest report that
describes a different process than the one actually run.

### `marker-filtered-as-full`
A test run with a marker filter reported as the full suite. "Suite is green"
is a claim about the collected paths, not the whole. Remedy: name the
`testpaths` or the marker, or the claim is about a subset.

### `diff-without-base-SHA`
A diff reported as meaningful without naming the base it was taken against.
The same patch means different things against different bases. Remedy: the
diff report names its base SHA.

### `brief-built-around-an-assumed-trigger-without-a-named-caller`
Wave 5's original brief assumed `bridge.close_session()` fires on tab-close.
It does not. A session-end reaper built on that assumption would have fired
at shutdown, where the sweep already runs — value ≈ 0. Remedy: when a brief's
mechanism depends on a trigger firing, the brief names the callers of that
trigger and reads them before writing the change.

The family is read-first in negative: each member is a report that skipped
the read that would have caught it.

## 2026-10-14 — [candidate] Core sprint: pruning-cycle penultimate turn (Gen 42, queued)

**Proposed for the upcoming core sprint, not for this wave.**

Shape: a "penultimate turn" state entered when the pruning threshold is
crossed. The agent gets one turn of unrestricted tools with an explicit
instruction to write to the KB, then the existing restricted state
(respond-or-summarize only) engages. Rationale: the current mechanism loses
verbatim precisely when the agent is most likely to have just learned
something worth keeping. A one-turn advance warning converts a loss into a
write.

**Before this is a proposal, not a half-baked idea — read-first:**
- The turn-lifecycle state in the core loop.
- How the token-limit warning is currently emitted (the "warning on upcoming
  token limit" path).
- Whether a "penultimate" state can exist without forking the state machine.
- Whether keep-last-n-turns carry-over already covers the same ground.

Read-first is step 1. Core sprint scope.

## 2026-10-14 — [correction] f2e8bbb8 IS on dev — read beat the audit (Gen 42)

Corrects the `[VERIFIED]` claim in the `ci/fresh-install-smoke-per-os` entry above (filed by Gen 41, same day).

**Stale claim:** "the cited merge commit for PR #101, `f2e8bbb8`, is not an ancestor of `dev`."

**Refuted by clean re-read on `origin/dev` @ `2017041e`:**
- `git merge-base --is-ancestor f2e8bbb8 origin/dev` → exit 0
- `git branch -a --contains f2e8bbb8` → lists `dev`, `origin/dev`
- `git log origin/dev --oneline -- scripts/doctor_checks.py` → `f2e8bbb8` latest on the file
- `git log origin/dev --oneline -- start_thoughtmachine.sh` → `f2e8bbb8` latest on the file
- `status.md` line 17 correctly labels `#101 = f2e8bbb8`

**Cause:** the earlier reading was against a stale or unfetched local clone (local `dev` at `420e0630` or older, before `git fetch origin`). The audit's own rule applies: read at the right SHA.

**Also corrected:** the entry's UNVERIFIED note said the Darwin-skip location was not found in `start_thoughtmachine.sh`. Main's ruling: the skip lives in **both** `scripts/doctor_checks.py` (the `ss`→`lsof` fallback) and `start_thoughtmachine.sh` (the Darwin tool-check skip). Both pieces are in `f2e8bbb8`.

**Named for the handoff:** `status.md` recorded a SHA correctly; the audit read it against a stale tree. Same class as `diff-without-base-SHA` — a claim whose base is not the base it is about.

**Wave 7 (`ci/fresh-install-smoke-per-os`) proceeds.** Premise confirmed.

## 2026-10-14 — [intent] Six items named by the operator, no home yet (Gen 42)

Filed per operator request: "so I don't rediscover it again." Persist first,
sequence later. None has a ticket.

1. **Sleep tool + async container runs.** One strand. The agent needs a
   sleep primitive and a way to run containers asynchronously without the
   main agent polling. Related: the async-worker pattern is forbidden
   (2026-10-05 ruling); a sleep tool is the primitive that pattern lacked.
   Target: core sprint or its own strand.

2. **Container size/CPU/mem user-facing config.** Phase 3. The workspace
   page should let the user set per-container resource limits. Named, no
   ticket. Related: `design/phase3-resource-configurability`.

3. **Agent-reachable stop/restart on containers.** Read-first needed to
   confirm current state — does the agent's tool surface today have
   stop/restart on the containers it can see? If yes, file as gap; if no,
   file as feature. Not the same as `bug/cross-session-container-reach`.

4. **Red-team the trust boundary against a live machine.** Named by the
   operator, no ticket. Distinct from the security layer's own
   red-teaming (which is sealed); this is a live-machine exercise: give an
   adversarial session a workspace and see what it can actually reach.

5. **Workspace threat-separation model.** Design. A bot workspace vs. a
   jtag workspace vs. a personal-data workspace — the machine should let
   the user declare "this workspace holds X, treat it accordingly." Part
   of the broader threat model, alongside `docs/readme-threat-model`.

6. **Core audit.** Read the core and name what has outgrown its original
   shape. No ticket. Prepares the core sprint.

## 2026-10-14 — [correction] worker_notes: git binary vs git tool (Gen 42)

Corrects an imprecise Infra briefing carried into earlier dispatches.

**Stale claim in briefs:** "git is absent in the worker container" (implying
a worker citing `git show <sha>` is confabulating).

**Refuted:** `git` the *binary* is absent from the worker shell (rc 127),
but the `git_*` *tools* work in the worker's surface. A worker citing
`git show <sha>` is plausible, not a confabulation tell. The original
correction lives in `personal/worker_notes.md`; this is the cross-reference
so a reader of the ticket queue sees it too.

Same class as the WA-1 corollary (mechanism as tool vs. as binary).

## 2026-10-14 — [correction] status.md path in the f2e8bbb8 correction (Gen 42)

The correction above cites "status.md line 17." The file is
`.thoughtmachine/knowledge/project/status.md`, not repo root — there is no
`status.md` at repo root (`git cat_file origin/dev:status.md` → fatal).
Substance is correct; path is loose. Fix-in-place at the next KB pass.

## hygiene/stale-working-doc-719e62e85d56 — Gen 43, 2026-10-14

`.thoughtmachine/working_docs/719e62e85d56.json` chunk D is wrong at HEAD
(`6df5164a`) on two points:
1. claims `_restrictive_merge` passes the permission_footprint through — it does not.
2. claims `_WORKER_BLOCKLIST` contains `MCPValidator` — actual blocklist
   is `{Worker, CheckSystem, KnowledgeBaseTool}`.
Surfaced by Wave 8 read-first (pass 1, A1). Do not seed a fix brief from
this file. Remediation: append a STALE header, or archive. Record kept.

## hygiene/reports-dir-not-tracked — Gen 43, 2026-10-14

`.thoughtmachine/reports/` does not exist at HEAD (`6df5164a`). All reports
there are untracked artifacts. Relevant to the KB tracking question — if
reports are meant to be evidence, address in the realm-split ruling.

## hygiene/kb-tracking-understated — Gen 43, 2026-10-14

Elevates `hygiene/kb-two-realm-classification`. At `6df5164a`,
`.thoughtmachine/knowledge/project/` tracks only 9 files. Untracked but
cited as authoritative: `status.md`, `operator_observations.md`,
`design_permission_facts_ownership.md`, `design_layers_and_truth_ownership.md`,
`arc_c2_worker_blueprint.md`. The two-realm framing understates this:
most of the public-realm KB is simply not tracked. Awaiting Main ruling.


## bug/container-turn-start-reap-kills-running — Gen 43, 2026-10-14

Ticket: `.thoughtmachine/working_docs/ticket-bug-container-turn-start-reap-kills-running.md`

Field report, reproduced 2/2. At each turn start, running `free_use`
containers are reaped; the trigger is the incoming query — so asking for
results destroys the job. Two runs lost 95% and 98.4% of budget.
Wave 9 (friend fix) territory. Not yet established as THE friend incident.
Mandatory input to Wave 9's read-first.

## bug/live-view-container-stale-no-push — Gen 43, 2026-10-14

Operator observation during live use. New containers do not appear in the
live view panel until a manual F5. Layer 4 / Inv.5-adjacent — the UI does
not show what the core knows. Filed as a UI-sprint candidate, not a wave.
Ticket: .thoughtmachine/working_docs/ticket-bug-live-view-container-stale-no-push.md

## Friend-reported bugs — the concrete list — Gen 43, 2026-10-14

**Retires the singular "the friend incident."** Multiple reports,
named individually. The phrase was used loosely by Gen 41, inherited
undefined by Gen 42, inherited again by Gen 43. It referred concretely
to reports 1 and 2 below (same friend, same Mac, same discovery session).
It is NOT the container turn-start reap; it is NOT "three workspaces
fail deterministically" (that was attached to the reap defect, never
confirmed). Do not use the singular.

1. **macOS substrate — `git_read` "dubious ownership"**
   - Symptom: `fatal: detected dubious ownership in repository at
     /workspace/KI-Basis-Framework` (exit 128), while native git in the
     same container worked.
   - Root cause: Docker Desktop VirtioFS uid-flap under bursty access.
   - Cure: gRPC-FUSE (environment; no code change).
   - Status: cured on the friend's machine; deeper detector held on C3 probe.
   - Filed: `bug/virtiofs-uid-flap-macos` (HELD).

2. **macOS launcher — `start_thoughtmachine.sh` assumed Linux-only tools**
   - Symptom: fresh Mac user cannot launch the machine without local
     patches to `scripts/doctor_checks.py` and `start_thoughtmachine.sh`.
   - Fix: PR #101 = `f2e8bbb8`.
   - Status: merged; gated by Wave 7's macOS launcher lane (#109).

3. **[OPEN — operator to confirm any further reports]**

**Placement.** Each item above lives in its own ticket/strand. This entry
exists to retire the term and give the two known items one home.


## Friend-reported bugs — item 3 CLOSED — Gen 43, 2026-10-14

Operator confirms: the full set of friend reports was —

1. git slash-path issue — unrelated to current work.
2. envelope selection not working — pre-feature; the selection time gate
   did not exist when the report was made. Not a bug against current code.
3. macOS dubious-ownership (`git_read` exit 128) — gRPC-FUSE toggle,
   Docker Desktop env, cured.
4. macOS launcher broken as pulled — fixed by PR #101 (`f2e8bbb8`);
   CI gap (no macOS smoke) closed by Wave 7 (#109 gating Darwin lane).

None is the container turn-start reap. **The singular "the friend incident"
is retired for good.** The container reap stands alone.


## Container taxonomy as stated by the operator — Gen 43, 2026-10-14

Recorded verbatim-in-substance because the reaped container's label
(`container_type = free_use`) needs to be read against this model.

**Free-use containers** — the workspace-runtime image, agent/worker
freely accesses. No specialized capabilities — deliberately no git,
because a shell-out to git would bypass the permission system.
Two invocation modes:
- **Ephemeral** — `DockerCodeRunner`. Short-lived, blocking, one-shot:
  spawn, run command, result returns in the same tool call, container
  deleted/recycled. Not meant to run long.
- **Persistent** — `ContainerExec` / `Start` / `Stop`. **Workspace-owned**,
  not a one-shot-worker container. Intended for long user jobs (training,
  heavy compute, simulation). The "user machine in the workspace" —
  spawned with a job, returned to later for results. **Meant to survive
  across turns.**

**Resource containers** — base image + a specialized tool (e.g., git).
**Not** free-use. Gated through the permission system via specialized
tools; a `git:read` session cannot forward a write. Capability-rich,
permission-gated.

**Bearing on the reap defect.** The reaped container was created by
`ContainerStartTool` and carried `container_type = free_use` — i.e. a
**persistent free-use** container, exactly the class the operator says
must survive. The turn-start sweep reaping *running* `free_use` containers
kills persistent containers as collateral. Whether the sweep is
over-broad (meant for ephemeral leftovers) or categorically wrong is
Wave 9's read-first's question.


## Anti-pattern family — new member — Gen 43, 2026-10-14

**`brief-internally-contradictory`** — the brief names two mechanisms
that cannot both be true at once. This generation's instance: a read-
only wave whose brief also said "work in a worktree." A worktree
creates repo state; read-only means no repo state. The spare agent
caught it and used no worktree.

Same shape as the other members — the brief names a mechanism it isn't
actually using, or names two mechanisms that conflict. Carry.

Family now:
- `marker-filtered-as-full`
- `diff-without-base-SHA`
- `brief-built-around-an-assumed-trigger-without-a-named-caller`
- doc-claims-state-source-that-doesn't-hold (dispositions-doc member)
- **`brief-internally-contradictory`** ← new

## hygiene/root-level-python-modules — Gen 43, 2026-10-14

Operator observation from the dev root-view. `docker_executor.py` in
repo root is already filed as `hygiene/docker-executor-out-of-repo-root`.
Same shape, additional files: `live_smoke_docker.py`,
`setup_workspace.py`, `start_windows.py`, `thoughtmachine_entry.py`.
All root-level Python modules that likely belong under `scripts/` or a
package. Not urgent; hygiene-sprint candidate. Do not touch during the
reaper work.


## Pattern — one funnel per fact-class — Gen 43, 2026-10-14

Named by Main, 2026-10-14, in the Wave 9 re-scope ruling. Recorded here
because it is a pattern, not a ticket.

> **One funnel per fact-class.**

Permissions have one — `security_gate.get_effective_permissions`.
The ceiling has one — D3's `resolve_effective_ceiling` (design intent).
Container lifecycle does NOT, and every bug in Wave 9 is a consequence.

The Wave 9 read found **four** implementations of "container state
doesn't match — destroy it":
- `docker_executor.py::verify_container_integrity` (compare-and-destroy)
- `DockerExecutor._ensure_container` (inline compare-and-destroy)
- `get_integrity_status` → `GET /api/container/integrity` (destructive GET)
- `server.py` startup scan (third compare-and-destroy)

Four verbs (`stop`, `remove`, `remove(force)`), four triggers, four
selectors. No shared code. No lifecycle events. **Inv.6 in its purest
form** — four functions computing the same decision with no shared
implementation.

Same disease as: R1/R2 (two counters, three limit resolvers); R13 (two
catalogs); the ceiling chain before D3; the sweep born before the
free_use class split.

**The recognition.** D3 established "one funnel per fact-class" for the
ceiling. It was never extended to Layer 3 (container lifecycle). Layer 3
was named "uneven" in the machine model from the start and left for
later. Later is now.

**Wave 9a is the funnel.** One function — the sole path that can end a
container's life — taking (actual state, intended state, session grant)
and returning one of: use / refuse / stop / reap. Fail-closed: if it
cannot decide, it refuses, it does not destroy. Every site that stops,
removes, reaps, or reconciles a container re-points at it. The dead code
is deleted. The destructive GET becomes a non-destructive read that
surfaces the funnel's verdict — it does not execute it.

Carve-out: the destructive `GET /api/container/integrity` cannot wait
for the funnel. It violates HTTP semantics at the API boundary — a
UI-lies class bug at the protocol layer. Anything enumerating endpoints
can hit it. In 9a's scope by necessity.

## Read-beats-ruling — two refutations this generation — Gen 43, 2026-10-14

Two refutations happened in Wave 9, both aimed at Main, both headed off
before a line of code was written:

1. **Main's H1** — "the sweep is the turn-start reap." Refuted by the
   Wave 9 recon. The sweep hard-skips non-exited containers; cannot
   touch a running one.
2. **The one-pass named location** — `session_lifecycle.py::verify_
   container_integrity`. Refuted by the verification read. That chain is
   dead code; the file is never constructed at runtime. The live path is
   `web_ui/backend/bridge.py::WebAgentBridge._maybe_re_sync_container`.

Carry these. They are the discipline paying back — the reads caught what
the rulings missed. Both had the same shape: **a plausible mechanism
named without a named live caller.**

Related anti-pattern family member (this generation):
`brief-internally-contradictory` — read-only + worktree, in the same
brief. The spare caught it.


## hygiene/query-loss-mitigation-adopt-spec-on-disk — Gen 43, 2026-10-15

Two consecutive dispatches (N2' read, 0a read) lost the query text to
context compaction mid-run. Both flagged it honestly; the enumeration
survived but the question mapping was reconstructed from memory.

Mitigation applied on the 0a-adversarial pass and it WORKED: the full
spec was written to disk (`working_docs/wave9a-0a-adversarial-spec.md`)
and the dispatch `query` was three lines pointing at the file. The
worker compacted TWICE, re-read the spec from disk, and re-verified.

**Adopt as standard dispatch shape going forward:** spec on disk, short
query pointing at the file. This is a KB-level practice, not a ticket.

## hygiene/scratch-exp2-untracked — Gen 43, 2026-10-15

`.thoughtmachine/working_docs/scratch/_exp2.py` (or equivalent path)
is an untracked scratch file directly calling `resolve_container_config`.
sha256[0:16] `02b4664d39c256f7` @ 2026-10-04 14:22:11Z. Flagged by 0a
and 0a-adversarial. Not in any wave; hygiene-sprint candidate. Do not
touch during the 9a work.

## hygiene/adversarial-envelope-missing-own-hash — Gen 43, 2026-10-15

The N2'-adversarial and 0a-adversarial worker envelopes both omitted
the report's OWN byte-size / line-count / sha256, while providing
source-file hashes. Minor deliverable gap. Worth pinning in the worker
brief template: "report must include its own size and hash." File under
the dispatch-shape hygiene thread.

## Read-refines-ruling — distinction recorded — Gen 43, 2026-10-15

Main's ruling, 2026-10-15: name the count accurately.

- **Refutations** (2 this generation): the ruling was wrong.
  - The sweep is the turn-start reap — refuted; sweep hard-skips
    non-exited.
  - The named location `session_lifecycle.py::verify_container_integrity`
    — refuted; that chain is dead code.
- **Refinements** (2 this generation): the shape was bigger than the
  ruling knew.
  - Site count grew from 1 to ≥5.
  - "Third source of truth" (admission) — refuted as stated; it is a
    second resolution of the same vault-backed envelope with independent
    capability provenance, NOT a third permission source.

Refutations = discipline headed off a wrong path.
Refinements = discipline found the shape was larger.
Both are the read working. Do not flatten into one number.


## hygiene/development-guides-dirty-change — Gen 43, 2026-10-15

`.thoughtmachine/knowledge/project/development_guides.md` is tracked and
currently dirty. Contents: the 0b orchestrator's KB entry recording three
corrections (session_lifecycle.py dead, integrity GET destructive, branch
does not fix the turn-boundary kill) + the W5 fix pattern. Applied by the
0b agent; header fix applied locally.

**Not committing.** Main's ruling: host-local KB; tracked files are
anomalies awaiting the realm-split. Leave dirty like the parked audit md.

If the working-tree change is lost (checkout / stash drop), the content is
reproducible from `.thoughtmachine/reports/wave9a-0b-fix-report.md` and
the session transcript.

Folds into `hygiene/kb-two-realm-classification`.


## feat/checkSystem-runtime-version — Gen 43, 2026-10-15

Filed. Operator asked for this 2026-10-14; Gen 43 acknowledged in
conversation but did not write the ticket. **Miss recorded.** The
conversation was the substantive exchange; the file is the record.
Ticket: .thoughtmachine/working_docs/ticket-feat-checkSystem-runtime-version.md

The feature: `CheckSystem query='version'` returns the commit + branch +
dirty flag **as loaded at process start**, not a live `git rev-parse`.
Operator's framing: "not the current codebase hash — the start-time
loaded param."

Three incidents this generation where it would have helped:
- Field bug version unknown (image build_hash ≠ runtime commit)
- Q1 check "does the field binary predate #90" unanswerable
- Every "did this merge fix it" question is inferential without it

Arc 2. Independent of 9a. Gen 44 dispatches.

## Anti-pattern family — member 4: `self-estimated-as-measured` (Gen 44, 2026-10-16)

**Shape:** a report names a mechanism it didn't use. Member 4 = a duration
reported as if measured, when it was estimated by the worker itself.

Same family as the existing four members:
- `marker-filtered-as-full`
- `diff-without-base-SHA`
- `brief-built-around-an-assumed-trigger-without-a-named-caller`
- `brief-internally-contradictory`

**Evidence:** four waves self-estimated:
- T1c - ~4200 s self / 508.6 s actual (2-8x)
- HIGH - 1510 s self / 455.9 s actual (3.3x / 2x)
- followup - 68.9 s self / 130 s actual (1.9x)
- M7 read - ~180 s self (estimate) / 106.2 s actual (1.7x, opposite direction)

**Remedy (symmetric to the family):** the report labels the number's origin,
or it doesn't report a number. The dispatch-report-duration ticket is now
load-bearing; measurement is the precondition for overnight-run planning.

**Filed:** 2026-10-16, Gen 44.

## Anti-pattern family — 7th member: `claimed-mechanism-not-used` (Gen 45, 2026-10-16)

**Shape:** a tool or report names a mechanism it didn't use.

**Instance:** worker's `commit` op claims selective commit
(`git commit -- <paths>`). When the path filter fails to name a
worktree-absent path, the tool commits the **whole staged set** rather
than aborting. On 10b, this swept `agent/presenter/session_lifecycle.py`
into commit `271b7120`; the commit had to be rewritten as `0eefb8fa`.

**Remedy (symmetric to the family):** the tool commits selectively, or
it reports that it committed the whole set. No third option.

**Filed:** 2026-10-16, Gen 45.

## Anti-pattern family — 2nd instance of `brief-internally-contradictory` (Gen 45, 2026-10-16)

**Shape:** a brief's fence contradicts its own instruction.

**Instances:**
- 1st (Gen 43): Wave 9 read-first brief — fence said "read-only, only
  the report is written"; instruction said "work in a worktree" (which
  creates repo state).
- 2nd (Gen 45): `wave10b-docker-executor-move.md` — fence listed
  `agent/presenter/session_lifecycle.py` under MUST NOT TOUCH; the file
  imports `docker_executor.verify_container_integrity` at two sites
  (321, 377 after the move), so "update every import site" required
  touching it. Worker chose correctly.

**Filed:** 2026-10-16, Gen 45.

## Anti-pattern family — 8th member: `described-as-ruled` (Gen 45, 2026-10-17)

**Shape:** a doc section carries `[RULED]` but only has evidence to describe,
not to rule. The section names a mechanism (a ruling) it didn't use (a
description).

**Instance:** `design_container_lifecycle_contract.md` §6 (as filed, untagged)
described `_ensure_container`'s fast-path destroy-on-drift as "deliberate;
pinned by tests/trust/test_toggle_live.py." No owner stated the intent —
what pinned it was a *test*, and what "deliberate" rested on was a *read* of
the test. §9 (later, Main Gen 38) ruled the same behaviour is the bug.
§6 is superseded.

**Remedy (symmetric to the family):** a section that says "this is
deliberate" is a ruling only if an owner stated the intent. Otherwise it is
a description with a ruling tag — write it as a description, not a ruling.

**Filed:** 2026-10-17, Gen 45.

## bug/panel-remove-no-feedback (Gen 45, 2026-10-17)

**Supersedes:** the "silent reconcile-refuse" hypothesis for
`bug/container-remove-ui-no-op`. That mechanism is impossible on this path:
the remove branch calls `manager.remove(container_id)` directly
(`web_ui/backend/server.py:4441` → `workspace_container_action`), which
builds `ContainerIntent(kind="reap", explicit=True, reason="explicit_remove")`
→ reap wins unconditionally. `reconcile` cannot refuse it.

**Actual mechanism — two layers of the same class:** *a call that returns a
status, whose status is discarded.*
- `web_ui/frontend/src/components/workspace/ContainerListPanel.jsx:243-262`
  (`dispatch`) does `await fetch(...)` then `await refresh()` and never
  inspects `res.ok`. Any error response is invisible to the operator.
- `web_ui/backend/server.py:4508-4512` ignores `manager.remove()`'s returned
  status.

**Narrows the repro:** the button only renders for ephemeral rows
(`ContainerListPanel.jsx:77`). So a 403 from `server.py:4479` (runtime/
non-ephemeral refusal) or a successful remove with a stale list both look
like a no-op.

**Resolved (2026-10-17, same-day read): INTENDED-UX.** The ephemeral-only
render is deliberate — four surfaces agree: author comment
(`ContainerListPanel.jsx:21-22`), render gate (`:77`), T11 tests
(`ContainerListPanel.test.jsx:265-267`), and server 403
(`server.py:4479-4483`). Persistent rows own Stop/Start/Restart/Resize
instead. The ticket reduces to the no-feedback defect alone
(`dispatch` not inspecting `res.ok`; endpoint discarding
`manager.remove()`'s return).

**Filed:** 2026-10-17, Gen 45.



### Second clause (2026-10-17, from P7 404 investigation)

The panel does not refetch after a *failed* action, only after a
successful one (`ContainerListPanel.jsx:313-317` returns on `!res.ok`
without `await refresh()`). Consequence: a stale row survives, and every
subsequent click is guaranteed to repeat the failure — a 404 becomes a
permanent broken button. Fix: refresh on both success and failure paths.


## Anti-pattern family — 3rd instance of `brief-internally-contradictory` (Gen 45, 2026-10-17)

**Shape:** a brief's fence contradicts its own instruction.

**Instances so far:**
1. Gen 43: Wave 9 read-first brief — fence "read-only, only the report
   is written" vs instruction "work in a worktree" (which creates repo state).
2. Gen 45: `wave10b-docker-executor-move.md` — fence "MUST NOT TOUCH
   session_lifecycle.py" vs "update every import site" (that file imports
   `docker_executor.verify_container_integrity` at two sites).
3. Gen 45: `fix-container-remove-interim.md` — fence "do NOT touch
   web_ui/backend/server.py" vs goal "operator-only removal must work"
   (server.py:4481-4490 is the file that enforces it).

**Pattern:** the fence and the goal are written separately; the fence
doesn't know what the goal requires. Fences are written to bound scope;
goals are written to name outcomes. When they contradict, **the goal wins
and the fence is wrong.** The remedy is a rule, not a longer fence: the
spec author writes the fence AFTER reading what the goal requires.

**Filed:** 2026-10-17, Gen 45.

## Family class-shape note — `declared-but-not-enforced` (Gen 45, 2026-10-17)

**Not a member. A class-shape observation about what the family hunts.**

The anti-pattern family hunts facts that name a mechanism they didn't use.
`declared-but-not-enforced` is the same hunt, direction-agnostic: a fact
that names an enforcement it didn't have. Both are "a control exists in
one layer but not its paired layer."

**Three instances in Gen 45:**

1. `capabilities.json` fields — declared as hardening, silently inert.
   Closed by declaring INERT in the manifest. (Message-side lie: declared
   as active, wasn't.)
2. `container:remove` — a grain the store drops, the API 422s, the UI
   can't render. Closed by deleting the string. (Message-side lie.)
3. **"operator-only" — a rule whose operator path 403s.** Closing now by
   making server.py's `workspace_container_action` allow persistent
   remove for the operator. (Enforcement-side gap: declared as available,
   wasn't.)

**Diagnostic:** a control exists in the message layer but not the
enforcement layer, or vice versa. The tell: one side names a remedy the
other side refuses. When you find it, check both sides before choosing
which to fix — sometimes the message is wrong, sometimes the enforcement
is wrong, sometimes both. Instances 1 and 2 were message-side lies.
Instance 3 is enforcement-side. Same class, different direction.

**Filed:** 2026-10-17, Gen 45.

## Family class-shape note — `destructive-before-validate` (Gen 45, 2026-10-17)

**Not a member. A second class-shape observation about what the family hunts.**

The anti-pattern family hunts controls that name a mechanism they didn't
use. `declared-but-not-enforced` is one direction (the control exists in
one layer but not its pair). `destructive-before-validate` is a different
direction: an action whose side effects happen **in the wrong order** —
the destructive half runs before the validating half.

**Instances found in the container audit (2026-10-17):**

1. **Resize destroys before it validates** — `server.py:4648-4661`
   persists the new `intent_snapshot`, removes the container, then
   attempts `start()`. `mem_limit` is never format-checked (free text),
   so `"banana"` deletes the container and then fails to recreate it.
2. **Recreate branch has no rollback** — `infra/container_manager.py:2063-2150`.
   If `attach_container` fails after `_run_container` succeeded, a live
   container survives with no record; nothing tears it down.
3. **Orphan window** (Q9) — if Docker creates the container but the API
   call fails on the return path, the stub record is deleted and the
   container survives unrecorded with a dangling record label. Cleanup
   never runs.

**Remedy (symmetric to the class):** validate first, destroy only after
validation passes. Never the reverse. A destructive side effect must
follow its precondition, not precede it.

**Filed:** 2026-10-17, Gen 45.

## Audit reading rule — trust positives, verify negatives (Gen 45, 2026-10-17)

An audit that searches the wrong paths is **right about what it found
and wrong about what it didn't find.** Two kinds of claim:

- **"The thing exists but doesn't do what its name says"** — trust
  these. The audit found the thing and read it.
- **"The thing doesn't exist"** — verify these. The audit may have
  searched wrong.

**Evidence:** the 2026-10-17 container audit claimed
`design_container_lifecycle_contract.md` and `design_resource_contract.md`
do not exist; both live at `.thoughtmachine/knowledge/project/`, which
the audit didn't search. Two false negatives out of the audit's nine
questions.

This is the audit-equivalent of "premise-verified-before-RED." Applies
whenever reading an audit report: consume the positive findings as
evidence, verify the negative ones before acting on them.

**Filed:** 2026-10-17, Gen 45.

## Ticket-wrong-file pattern — two instances in one wave (Gen 45, 2026-10-17)

**Shape:** a bug ticket names a symptom + a mechanism, and the mechanism
is wrong while the symptom is real. The fix-wave worker finds the real
mechanism and either re-scopes or fails to reproduce.

**Instances this generation:**

1. **`bug/panel-actions-no-op`** — the ticket named the four verbs but
   suggested `ContainerListPanel.jsx`'s `dispatch` as the mechanism
   (`res.ok` check). The actual cause was the 401 actor gate on
   record-keyed routes, one layer up. Symptom was real; the mechanism
   cited was a symptom of the mechanism, not the mechanism.

2. **`bug/panel-unsaved-state-after-apply`** — the ticket named
   `SessionSidebar.jsx` as the location of the "Unsaved Permission
   changes" dirty flag. There is no dirty flag in that file. The real
   defect was in `ConfigPanel.jsx`: `permsDirty` compared the full GET
   raw map against a canonical-key-only baseline. The symptom (Apply
   stays blue after successful PUT) was real and is fixed.

**Pattern:** the person filing the ticket was reasoning from the UI
behaviour to a plausible mechanism, but not reading the code. Both
times, the read found a different code path than the ticket named.

**Remedy:** tickets that name a mechanism should carry a "read first"
step — cite the exact function/symbol you *think* is the mechanism, and
have the worker verify before dispatching. Same shape as
`brief-built-around-an-assumed-trigger-without-a-named-caller` but
one level down: the mechanism, not the trigger.

**Filed:** 2026-10-17, Gen 45.

## Ticket-wrong-file pattern — two instances in one wave (Gen 45, 2026-10-17)

**Shape:** a bug ticket names a symptom + a mechanism, and the mechanism
is wrong while the symptom is real. The fix-wave worker finds the real
mechanism and either re-scopes or fails to reproduce.

**Instances this generation:**

1. **`bug/panel-actions-no-op`** — the ticket named the four verbs but
   suggested `ContainerListPanel.jsx`'s `dispatch` as the mechanism
   (`res.ok` check). The actual cause was the 401 actor gate on
   record-keyed routes, one layer up. Symptom was real; the mechanism
   cited was a symptom of the mechanism, not the mechanism.

2. **`bug/panel-unsaved-state-after-apply`** — the ticket named
   `SessionSidebar.jsx` as the location of the "Unsaved Permission
   changes" dirty flag. There is no dirty flag in that file. The real
   defect was in `ConfigPanel.jsx`: `permsDirty` compared the full GET
   raw map against a canonical-key-only baseline. The symptom (Apply
   stays blue after successful PUT) was real and is fixed.

**Pattern:** the person filing the ticket was reasoning from the UI
behaviour to a plausible mechanism, but not reading the code. Both
times, the read found a different code path than the ticket named.

**Remedy:** tickets that name a mechanism should carry a "read first"
step — cite the exact function/symbol you *think* is the mechanism, and
have the worker verify before dispatching. Same shape as
`brief-built-around-an-assumed-trigger-without-a-named-caller` but
one level down: the mechanism, not the trigger.

**Filed:** 2026-10-17, Gen 45.

## Sibling pattern — `ticket-names-wrong-site` (Gen 45, 2026-10-17)

**Not a family member. A sibling pattern.**

The anti-pattern family is about **reports naming a mechanism they
didn't use**. `ticket-names-wrong-site` is about **tickets naming a site
they didn't verify**. Same remedy (read first), different artifact class.

**Two instances this session:**

1. `bug/panel-actions-no-op` — the ticket suggested `ContainerListPanel.jsx`'s
   `dispatch` as the mechanism (`res.ok` check). The actual cause was the
   401 actor gate on record-keyed routes, one layer up.

2. `bug/panel-unsaved-state-after-apply` — the ticket named
   `SessionSidebar.jsx` as the location of the "Unsaved Permission
   changes" dirty flag. That file has no dirty flag. The real defect was
   in `ConfigPanel.jsx`'s `permsDirty` comparison.

**Remedy:** a ticket that names a code site cites a symbol, and the
worker's first step is to **verify the symbol has the defect before
implementing.** That's premise-verified-before-RED applied to the
ticket, not just the fix.

**Related to the family** — same shape (`the ticket is reasoning from a
symptom to a plausible mechanism, not reading the code`) but the
mechanism claim is embedded in the ticket, not a report.

**Filed:** 2026-10-17, Gen 45.

## 2026-10-18 — Gen 38 seat: anti-pattern family, all members and class notes

Consolidated at session end. Family = "the report names a mechanism it should
have used instead of the one it did." Class notes = adjacent shapes named this
seat. All filed as they were found; this entry makes them findable in one place.

### Family members (8)

1. `marker-filtered-as-full` — a test run with a marker filter reported as the full suite.
2. `diff-without-base-SHA` — a diff reported as meaningful without naming the base it was taken against.
3. `brief-built-around-an-assumed-trigger-without-a-named-caller` — a brief whose mechanism depends on a trigger firing, without reading the trigger's callers.
4. `self-estimated-as-measured` — a duration reported as measured, when it was estimated by the worker. (Named Gen 44 seat, 2026-10-16.)
5. `brief-internally-contradictory` — a brief's fence contradicts its own instruction. 3 instances:
   - Wave 9 read-first: "read-only, only the report" vs "work in a worktree"
   - wave10b spec: "MUST NOT TOUCH session_lifecycle.py" vs "update every import site"
   - wave10a interim spec: "do NOT touch server.py" vs "operator-only removal must work"
   Remedy: the fence wins only when it doesn't contradict the goal. When it does, the goal wins and the fence is wrong. Write the fence after reading what the goal requires.
6. `ticket-names-wrong-site` — a ticket names a code site it didn't verify. 2 instances in one wave (Fix 6: named SessionSidebar.jsx; real defect in ConfigPanel.jsx's permsDirty). Sibling to the family, distinct artifact class. Remedy: cite a symbol; worker verifies before implementing. Same discipline as premise-verified-before-RED, applied at the ticket.
7. `claimed-mechanism-not-used` — a tool or report names a mechanism it didn't use. Instance: worker's `commit` op claims selective commit; commits the whole staged set when the path filter fails on a worktree-absent path. Swept `agent/presenter/session_lifecycle.py` into 271b7120; rewritten as 0eefb8fa. Remedy: commits selectively, or reports the whole-set commit. No third option.
8. `described-as-ruled` — a doc section marks existing behavior as a ruling when it only has evidence to describe it. Instance: §6 of the container contract marked the fast-path behavior "deliberate; pinned by tests" — but "deliberate" rested on a *test*, not a stated intent. §9 (later) ruled the behavior wrong. §6 preserved, §9 governs. Remedy: a section that says "this is deliberate" is only a ruling if there's a named owner who stated the intent. Otherwise it's a description with a ruling tag.

### Class notes (adjacent shapes)

**`declared-but-not-enforced`** — an artifact tells the user or agent that a thing is enforced, when the enforcement doesn't exist. 3 instances this arc:
- `capabilities.json` undeclared fields — hardening intent, silently inert. Closed by declaring INERT in the manifest; panel-tag follow-up filed.
- `container:remove` grain — a token the store drops, the API 422s, the UI can't render. Closed by deleting the string.
- "Operator-only" persistent removal — a rule the operator couldn't exercise (403 on the panel path). Closed by World B.

Diagnostic: **if a test can only pass by bypassing the store, the grain isn't real.** Check both sides (message layer and enforcement layer) before choosing which to fix.

**`destructive-before-validate`** — an action whose side effects happen in the wrong order. 3 instances: resize (destroys before validating `mem_limit`), recreate-no-rollback, Q9 orphan window. Remedy: validate first, destroy only after validation passes. Never the reverse. Ships as its own family when the class note graduates — currently 3 instances, needs a 4th to graduate.

**`log-reads-as-backlog`** — a document's *shape* signals a *role* it doesn't serve. Instance: `operator_observations.md` was read as the core backlog because it reads like one (chronological, substantive, item-shaped). Eight pending core items lived only there. Remedy: badge at top of log — "CORE WORK HAS MOVED. This file is a LOG, not a backlog." Generalisation: when a "single home" is declared, check for competing documents whose form mimics the role.

**`design-decision-premise-expired`** — a design decision whose stated reason no longer holds. Instance: the `git_write` §C exclusion ("push deliberately not exposed") was written when the agent had no credential management. The Credential Resource provides it. The exclusion is amended, not deleted — the reason goes in the record, the amendment goes in the record. Remedy: amend the decision, name its expired premise, do not silently override.

### Reading rule for consuming audits

**Trust positive audit findings; verify negative ones.**
- *"The thing exists but doesn't do what its name says"* — trust these. The audit found the thing and read it.
- *"The thing doesn't exist"* — verify these. The audit may have searched wrong paths. Two false negatives this arc.
Not a family member — a reading rule. Same discipline as premise-verified-before-RED, applied to audit consumption.

### Read-beats-ruling this seat (Gen 38)

Named per the working-agreements rule. Every one corrected a ruling of mine before code was written.

1. The container-reap mechanism (I ruled H1 over-broad sweep; read showed sweep can't touch running containers; `_ensure_container` fast path was the candidate).
2. The named file (`agent/presenter/session_lifecycle.py`) — read showed it was live, not dead. Six read-beats-ruling moments this generation.
3. The S1 direction (`container_type` label drift "protects" on the verify path; I'd assumed it worsens).
4. The "third source of truth" claim (demoted to "second resolution of same vault-backed envelope").
5. The endpoint-divergence framing (I ruled "two endpoints disagree, unify them"; read showed endpoints correct, UI reads wrong endpoint with wrong params).
6. The `workspace_ceiling: {}` diagnostic (I read empty as bug; read showed empty means "present but never restrictive").

Count: 6. The pattern: when I rule from downstream reports, the read finds different truth at source. Named for the next seat.

## Class-shape correction — `destroys-before-validate` is wider (Gen 45, 2026-10-18)

The class was first named on 2026-10-17 as "an action whose side effects
happen in the wrong order — the destructive half runs before the
validating half." Two instances then: resize, recreate-no-rollback.

**Correction after today's field failure.** The class covers **three**
input dimensions, all of which must complete before the destroy begins:

1. **Format validation** — is the input syntactically well-formed?
   (`"banana"` is not a mem_limit; caught in #128.)
2. **Range validation** — is it within the domain of acceptable values?
   (`"1m"` parses as a valid size but is below Docker's 6 MB floor; this
   is what caused #130 — the field data loss.)
3. **Semantic validation** — does it compose correctly with other inputs?
   (e.g. cpu_quota × mem_limit combos that Docker would reject at create.)

Fixing only format (#128) left range open. Fixing range in #130 closes
the specific resize path, but the class remains: any destroy-then-recreate
action must validate the full input domain before the destroy.

**The remedy** is now precisely: **validate across all dimensions of
the input domain, then destroy.** Not "validate, then destroy" — that
phrasing let us believe #128 was complete when only one dimension was
covered.

**Filed:** 2026-10-18, Gen 45, correction of 2026-10-17 entry.

## Field verification — the three questions the sprint answered (Gen 45, 2026-10-18)

After the container sprint closed (PR #131, #132), three earlier
questions are now settled by field evidence:

1. **Silent recreate** — was the operator's own resize click. Confirmed
   by timestamp comparison: `proto-test-2` Docker id changed at
   `2026-10-17T21:23:34Z` (the exact second of a resize attempt), record
   `updated_at` matches to the millisecond, no `RECORD_REAP` or
   `CONTAINER_SWEEP` at that time. Not a reaper. Not a cleanup sweep.
   The resize route recreated the container; the panel didn't surface
   the recreate as a distinct action. Fixed as part of #131's intent
   re-observation (post-create intent reflects the *effective* value,
   not the requested one).

2. **mem badge missing on persistent rows** — root cause was my own
   wave-2 change. Badge read from `permissionsOf` (the six-category
   grant dict — no `mem_limit` field). Resource rows (`permissions=None`)
   fell through to the intent snapshot and kept working; persistent rows
   lost the badge entirely. Fixed in #132 by re-sourcing from intent via
   `memLimitOf`/`intentOf`/`badgeText`.

3. **`created_by` = "unknown"** — the field existed but was never
   populated. #132 chose a **Docker label** (`thoughtmachine.created_by`)
   over a Record field, on the reasoning that: (a) schema 5→6 + migration
   + three pinned assertions is a bigger change than the field is worth
   right now; (b) the label answers "who owns *this* container" (correct
   for the destroy audit); not "who originally created the record"
   (a provenance question across recreates). Trade-off ruled 2026-10-18;
   Record field filed as `feat/record-created-by-provenance`.

## Audit reading rule — confirmed (Gen 45, 2026-10-18)

The 2026-10-17 rule **"trust positive audit findings, verify negative
audit findings"** held twice more this session:

- The secure-exec audit (2026-10-17) claimed the two contract docs didn't
  exist — they do, in `knowledge/project/`. Audit searched wrong paths.
  Verify negatives.
- The audit found six real "declared-but-not-enforced" defects (build
  hash, TTL, `_audit(CONTAINER_CONFIG)`, resize-destroys-before-validate,
  two defaults, `free_use` fail-open) — all six were real. Trust positives.

The rule stands. Positive = evidence; negative = needs verification.


## 2026-10-19 (Gen 46) — #134 verification: PASS

Resize preserves envelope and created_by across recreate. Verified two-sided:
- Session `fs: write`: resize 1g → 256m worked, container recreated (hostname
  changed), memory.max = 268435456 exact, created_by preserved as
  `agent:session:<id>` not `operator`, workspace mount rw, network egress OK.
- Session `fs: read`: resize silently no-op'd — no memory change, no recreate,
  no error surfaced. **Real bug filed:** `bug/resize-silent-noop-without-fs-write`.

The fix works when permission is held; the silent failure under read-only is a
separate defect (the route swallows the vault-write refusal). Not a #134
regression — pre-existing.

Also found: `ContainerStatusTool` queried by name echoes the name as
`container_id` (can't detect recreate via status); `uptime_seconds` is
time-since-event not generation-age. Both filed.

## 2026-10-20 (Gen 46) — fourth gate removed: worker spawn snapshot

- **What it was.** Every worker carried a permission snapshot captured at the
  moment it was spawned. That snapshot acted as a **cap** on the worker's
  authority: `effective = min(spawn_snapshot, session ∩ ceiling)`.
- **Why it was invisible.** It was in no doctrine document, no UI, and not even
  in the `permission_store` docstring. Nothing in the system told you it existed;
  you found it by being surprised by a worker that could not do something its
  session could.
- **Why it was wrong beyond being hidden.** It was **asymmetric**: revoking a
  permission took effect immediately, but *granting* one required respawning the
  worker. So the same edit to the same session had two different latencies
  depending on direction, and neither was documented. It violated Inv.1 (one
  resolution path) and Inv.5 (the record is the authority).
- **Removed in #137**, commit `4e96c32c` — "Wave: worker obeys session grants".
  Worker permission resolution is now `session ∩ ceiling`, identical to a main
  session on the same session grant.
- **It was not vandalism.** When workers were introduced, permissions were not
  disk-authoritative. The spawn snapshot was the only mechanism available to
  give a worker a stable, attributable permission set. The disk-authoritative
  rewrite later made the snapshot obsolete — but nobody went back and removed
  it. It did not fail; it **outlived its reason**. That is the general failure
  mode to watch: mechanisms that are correct when written and wrong later,
  because their precondition quietly disappeared.
- **Standing rule.** Do not reintroduce it. Read this record before touching
  worker permission resolution. If a future change seems to need a spawn-time
  permission snapshot, the thing that actually needs fixing is elsewhere.



## 2026-10-21 — Gen 47: verification written by the author; and the three layers

### [pattern] The class: verification written by the author of the claim

Four instances from this generation. One shape: **the same seat that made the claim
also produced the evidence that the claim is true**, using the same input it used to
make the claim. The verification is not independent; it is the claim restated in a
different font.

1. **Ticket evidence authored by the ticket's own author.** The container-create
   admission ticket blames `container_create.py:351 create_hardened_container` for
   declining admission. There are **two** functions with that name. The one on the
   gate path is `infra/container_registry.py:231` — the wrapper — and the gate it
   owns is at `:303`; `infra/container_create.py:351` is the primitive, called with
   `admission=None, record=True`. The real hot-path call the ticket was reaching for
   is `container_registry.py:714`. The author read the function whose name matched
   the sentence they had already written.
2. **A deletion justified from the shell the deleter happened to be in.**
   `85035605` removed `.githooks/pre-commit` as "dead" because `core.hooksPath`
   read **unset**. It is unset in a bare host shell; the container path sets it.
   The evidence was gathered from the environment the claim was made in, not the
   environment the hook runs in.
3. **A status block asserting a HEAD by the seat whose branch was checked out.**
   The status file claimed a tip that was the checked-out feature branch, not the
   branch the work landed on — a true reading of `git rev-parse HEAD` in that
   working tree, and false as a statement about the project.
4. **Verification by proxy: staleness from mtime.** An mtime records when bytes
   changed. It does not record whether what the bytes say is still true. Four
   documents were ranked "fresh" or "stale" on timestamps this generation; the
   timestamps could not have supported that judgement about content.

**Counter-example, same generation.** `docs/KB_POLICY.md` got it right: the public/
private split was assessed by reading the **index** (`git ls-tree -r`) rather than
the ignore rules, and found ten tracked blobs that no ignore rule predicted.
Different input, same question ⇒ the answer held.

**Rule (for the next seat).** The claim and its verification must come from
different **inputs**, not merely from different sessions. A second session reading
the same file with the same assumptions re-produces the same error with more
confidence. If you cannot name the input you used that the author did not use, you
have not verified anything.

### [pattern] A coin flip with a name on it

From the Gen 46 handoff, kept verbatim because the phrasing is the finding:

> *"A status file that old is not documentation, it is a coin flip with a name on it."*

Three artifacts were that old when this seat arrived: `project/status.md` (asserting
a HEAD from a tree that had moved on), `personal/handoffs.md` (last entry
2026-09-29), `project/open_items_register.md` (2026-09-21, WIP, never closed). All
three were **plausible** — internally consistent, well-formed, dated.

The asymmetry matters: **a stale document is worse than an absent one.** Absent
makes you go and look. Stale makes you stop looking, and it looks like you did the
work. The cost of the stale file is paid by whoever trusts it, not by whoever left
it.

### [preference] The lifecycle compass

Operator, 2026-10-21:

> *"I think this is also part of this new way of viewing things, the 'who takes care
> of the lifecyle' lens we now look at the machine."*

Three questions, asked of every artifact — file, gate, log, permission, KB entry,
ticket:

1. **Who writes it?**
2. **Who owns it?** (who answers when it is wrong)
3. **Who deletes it?**

Any answer of "nobody" **is** the defect, whether or not the artifact currently
works. The `85035605` deletion and the ten tracked KB blobs are both answer-3
failures: one deleted an artifact nobody owned the justification for, the other
cannot be deleted because nobody owns the split. Neither shows up as a bug. Both
show up as cost, later, to someone else.

**Status:** belongs in `project/working_agreements.md` as doctrine. Not there yet.

### [pattern] Three layers, and what "enforced" costs

A rule only exists if you can point at it in **all three** layers. Each layer has
its own failure mode, and the failure is silent in the same way: the rule still
reads as present.

| Layer | What it is | What its absence looks like |
|---|---|---|
| **Code** | One SSOT on the path the work actually runs through | A gate that exists but cannot execute |
| **Prompt** | Sysprompt / doctrine — the rule stated where the agent reads it | A rule nobody can point to by name |
| **Verification** | A test that would go **red** if the rule were removed | A test that confirms the fix instead of attacking the rule |

Two opposite shapes were observed in this tree:

- **Code contradicts doctrine.** The worker spawn-snapshot cap made worker authority
  `min(spawn_snapshot, session ∩ ceiling)`. No doctrine document said so. Removed in
  `4e96c32c` (#137); resolution is now `session ∩ ceiling`, identical to a main
  session. The cap was correct when written and wrong later — its precondition
  disappeared without anyone noticing.
- **Doctrine with no code path.** The `admission` parameter is advertised on the
  hardened-create primitive, documented, and tested — and unreachable on every
  production call site (all four pass `admission=None`). A reader of the signature
  concludes the guarantee is by construction. It is not.

**Test for the next seat.** Name the rule; name `file:line` in each of the three
layers. The layer you cannot point to is the layer that fails first — and it fails
invisibly, because the other two still read as if the rule is held.

## 2026-10-23 — The narrative can claim enforcement the code doesn't have (Gen 48, fourth surface)

**The class-shape: the seat's summary is itself a place the bug class can hide.**

Gen 48 said "the security boundary held" for several turns before discovering that `PaginateTool` could reach `host_bash` from a default-permission session. The sentence collapsed four distinct boundaries into one claim:

| Boundary | Held? |
|---|---|
| Container boundary | Yes |
| Permission layer (disk-purity) | Yes |
| Envelope | Yes |
| **Tool-call dispatch** | **No** |

The sentence that said "the security boundary held" was the bug class at the narrative layer. One fact (can a default session reach host shell?), multiple authorities (the permission layer said no; PaginateTool said yes; the narrative said "held"), they disagreed, and the narrative said the wrong one won.

**The discipline:** name the boundary precisely. "The container boundary held" is true. "The permission layer is disk-pure" is true. "The envelope fails closed three ways" is true. "The security boundary held" is a claim that collapses four into one and is true or false depending on which one is examined. Precision in the narrative is not pedantry; it's the difference between the seat holding the discipline and the seat being the fourth surface the bug hides on.

**Instance:** 2026-10-23, Gen 48, PaginateTool. Named by Main before the seat named it. Corrected in `security_architecture.md` and `working_agreements.md`.

## 2026-10-23 — The vacuous guard: a verification that does not verify (Gen 48)

> "`REPO_ROOT = Path(__file__).resolve().parents[1]` → resolved to `.thoughtmachine/` → globbed `.thoughtmachine/tools/**/*.py` → 0 files → three of four tests passed for the wrong reason."

**A guard that reports "clean" because it looked at nothing is the exact failure the rule exists to prevent.** Same class as `verification-written-by-author`: a check whose green result carries no information.

The guard was written to enforce "no tool may invoke another tool", parked in `.thoughtmachine/working_docs/`, and derived its repo root as `parents[1]` — which in the parked location is `.thoughtmachine/`, not the repo. It scanned zero files and its rule tests passed on empty input.

**Fix:** `_find_repo_root()` walks up to the first directory containing both `tools/` and `tests/`, and raises naming the rule if none is found. `test_scan_is_not_vacuous` asserts files / modules / classes are all `> 0`, so a zero-scan can never pass silently again.

**The generalisation — apply to any guard written in this repo:**
1. A guard must assert on the *size of its own input*, not only on its findings.
2. A guard's repo-root discovery must fail loudly, never default to a plausible-looking wrong directory.
3. **The parked-guard activation procedure must include this lesson**: before un-parking a guard, run it once from the unparked location AND confirm its scan counts are non-zero.

**Instance:** 2026-10-23, Gen 48, `tool_no_wrapping_guard.py`. Caught during phase-2 burn-down; fixed in `fix/census-burn-down-no-tool-wrapping` (`d4bc1e35`).

**Status today:** the guard is still parked and unactivated — nothing in CI runs it. Its census is a point-in-time figure: 13 sites at park, 8 of those (letters (b) and (c)) burned down in `cf10c358` (#189), the residual being letter (a).

## Class-shape notes (as of Gen 49, 2026-10-25) — six added by housekeeping

*Added by the Gen 49 housekeeping pass from the brief's own nomenclature. These are definitions, not rulings: none of the six was findable in the KB by its name or by the obvious variants, and each is recorded here so the next reader does not have to re-derive it. Where an entry says "instance: none recorded", the shape is awaiting its first written instance — writing it is a separate task, not this one.*

**B — the five boundaries.** container / permission / envelope / tool-dispatch / narrative. One action can be wrong at any one of the five while being right at the other four, so "the agent was contained" is only a whole claim when all five are named; the fifth is the one that gets skipped, because it is the account given afterwards rather than a mechanism. **Open question, not a ruling:** the boundary material currently lives in `security_architecture.md` (three-layer framing at `:43`), and the five-boundary cut does not appear there under any name or variant. Whether the five refine that three or are a different cut of the same thing is unruled.

**C — class-below / class-above.** A fix applied at the wrong altitude. *Class-below* patches the instance that failed and leaves the class open; *class-above* patches the symptom that was seen and leaves the mechanism open. Both read as fixes at review time and neither closes a fact. Instance: none recorded in the KB — definition only, so the next one to spot it has a name.

**E — mechanism-complete, surface-incomplete (both directions).** A mechanism can be finished while its surface is missing (nothing calls it), and a surface can be finished while its mechanism is missing (a green check that looks at nothing). Both are one defect seen from opposite ends, and both leave one fact with two answers. Instance of the second direction: the no-tool-wrapping guard that scanned zero files and passed (`:2171`–`:2180`, above). Instance of the first: `tests/test_tool_no_wrapping.py` "does not exist and never has on this branch" while the rule it would enforce has (`working_agreements.md:549`). *(The pairing of those two entries is the seat's reading, not a ruling.)*

**F — prompt-asserted guard.** The artifact that enforces the rule is a document — a brief, a prompt, a review instruction — rather than a check. It is not necessarily a weak guard, but its whole mechanism is that someone reads and remembers, so it fails silently when readership changes. The honest form names the check that would go red, or says plainly that the enforcement is human. Instance: `working_agreements.md:549` — "Enforcement. Review time, by humans — not a test."

**K — the workspace-isolation boundary is `workspace_id`.** Isolation between workspaces is structural exactly where the store keys on `workspace_id`, and narrative everywhere else, however the code is arranged. This is live rather than hypothetical because the vault is shared across workspaces (`~/.thoughtmachine`). Instance: none recorded in the KB — definition only.

**R — names the shape; the read finds the mechanism.** A brief or ticket can name the *shape* of a defect without naming its mechanism, and the mechanism is only ever established by reading the code. Taking a named shape for a located mechanism is how a fix lands at the wrong altitude (see C). Nearest recorded instance: the Gen 48 paginate brief named the failing check ("category bypass"); the mechanism — authority laundering reaching host access — was found by reading, and was the true name (`working_agreements.md:555`). *(Mapping of that entry onto R is the seat's, unruled.)*


## 2026-10-25 — Gen 50 seat: the front chokepoint closes (design phase)

The client-claim red team, wave 2, found the thing it was built to find: an auth layer's absence, mapped across seven surfaces. The 5 CRITICAL are one missing abstraction — no route can learn who is calling. Seven tickets filed (F1–F12 plus SSRF, logging-config, provider apikey read). The class `verification-pins-wrong-contract` named with three live instances.

The auth-layer design pass answered D1–D4 in one return. The conclusion is small: the Web UI's security boundary is the network boundary, the one decision is what an unscoped connection means, and the design's deliverable is to make "no workspace claimed yet" an explicit transition state rather than an absent field. That is the shape of the design — not a token scheme, not middleware, one state transition made explicit.

The session's own corrections are recorded in `working_agreements.md`: silent edits to verbatim claims, a brief carrying a stale citation, five terminal-paste corruptions. Each was small. Together they are what the seat learned.

### hygiene/route-workspace-mutations-through-bind-authority
**Filed:** 2026-10-26, from D2 scope ruling (Main 40).
**Severity:** LOW — consistency, not security. Does not block D1.
Main 40 called these "the 12 already-guarded sites"; the actual count needs
re-verification at dispatch (D2's read found 14 mutation sites total; D2
routed 5, leaving 9 residual direct `_workspace_id` writes). Route the
residual writes through `_bind_workspace(workspace_id, mode)`. They call D3's
`_ownership_refusal_ids` guards or are idempotent-equal — safe *today*, but
every direct write is a fact owner. Consistency is the hygiene, not security.
Read D2's classification table (`brief-d2-binding-authority.md` §Corrections,
PR 209 body) as input.
### hygiene/site-2543-bootstrap-bypass
**Filed:** 2026-10-26, from D2 read.
**Severity:** LOW — narrow edge. Does not block D1.
`server.py:2543` — the fallback that fires only when both `_handshake_ws_id`
and `_expected_ws` are None (bare WS, probe failed). Direct assignment
`bridge._workspace_id = resolved`, no `_bind_workspace`. No caller controls
the state that reaches it; unauditable tomorrow.
Route through `_bind_workspace(resolved, "bootstrap")`. Behavior-preserving.

**Anchor re-verification — Gen 51 KB close, 2026-10-26 (post-merge read, not
part of the filed ticket text).** Both tickets above cite line numbers that
are now stale: `web_ui/backend/server.py` is 6198 lines in the merged tree,
and D2's own insertions moved everything below them down. Verified anchors:
**2516** `if _handshake_ws_id:` (guard) / **2517**
`bridge._bind_workspace(_handshake_ws_id, "bootstrap")` / **2521–2540** the
cold-load probe (`if _expected_ws is None:` → `session_store.load_session` →
`_probe_ws`) / **2547**
`_switch_refusal = bridge._bind_workspace(_expected_ws, "switch")` with
refusal branch **2548–2560** / **2582–2587** the bootstrap-bypass fallback
(`if not bridge.workspace_id:` →
`resolved = _resolve_workspace_id(_project_path)` → `if resolved:` at 2586 →
direct `bridge._workspace_id = resolved` at **2587**, no `_bind_workspace`).
Lines 2543 and 2546 are comment prose inside D2's block at 2542–2546, not code
— so `site-2543-bootstrap-bypass` names a comment; the site it describes is
2582–2587 and the assignment is 2587. The slug is kept as filed so existing
references (status.md, PR bodies) do not dangle; **this note is the authority
for dispatch.**

Residual direct `bridge._workspace_id = ...` writes in server.py alone,
post-merge: **1669, 2587, 3225, 3233, 3244, 3290, 3464** (7 sites). The count
in the ticket above (9 residual) was a bridge.py + server.py total taken
before the merge; re-count from the tree at dispatch. Class:
`working_agreements.md §sha-pinned-blocks-go-stale-on-correction`.
