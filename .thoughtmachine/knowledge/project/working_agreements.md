# Working agreements — how this project is run right now

> **Scope:** process between operator, Main and the agent. Not a design doc, not a roadmap.
> `project/v3_roadmap.md` owns phases; this file owns *how we work*. Established 2026-09-15 (operator instruction).

## 1. "Collect it, order it later" is the current mode
The operator is gathering information about the system and building it **in the correct order**. Therefore:

- **Nothing gets fixed on discovery.** A defect, surprise or missing capability found during reconnaissance
  is **recorded**, not repaired. Fixes travel through **pipelines** (brief → worker/branch → review) —
  never as an opportunistic side-edit by the agent.
- **The agent's job is bookkeeping and information quality**, not repair: digest the operator's ideas and
  Main's structural planning, put them somewhere durable and findable, keep status honest.
- **"Good info" is the deliverable.** A finding is only useful if it is *located* (file:line or anchor),
  *attributed* (verified vs assumed), and *consequence-labelled* (what it changes downstream).
- **One strand at a time.** Do not open a second front because it happens to be nearby. Cross-references
  are fine; new work is not.
- **Accepted cost:** KB clutter grows faster than it gets organised. This is deliberate — ordering happens
  later, in bulk, by the operator/Main. Do not "help" by reorganising mid-strand.

## 2. The agent is not the fixer
- **Recording ≠ scheduling.** Writing something down does not authorise acting on it.
- If a fix looks trivial, the correct move is still: **record it, name the pipeline it belongs to, stop.**
- **Exception — the agent's own bookkeeping artifacts.** If the agent itself mangles a KB file or creates a
  formatting artifact, clean it immediately so the record stays readable. That is tidying, not repairing.

## 3. System-prompt wishes are RECORDED, not announced
- If the system prompt or agent behaviour ought to change, it goes in **§4 below** and is **not raised in
  conversation**. The operator batches these into prompt revisions deliberately.
- Do not lobby for a prompt change mid-strand.

## 4. System-prompt & agent-behaviour wishlist (accumulating)
Format per entry: *what the prompt should say*, *why it was noticed*, *where it bit*.

- **W1 — no opportunistic fixes.** The prompt should state that the agent records defects rather than
  repairing them on discovery (this file's §1). *Noticed 2026-09-15:* the landing-page provider bug was
  found and the reflex was to start fixing it; the correct behaviour was to stop at the finding.
  *Cross-ref:* `personal/parking_lot.md` FUTURE IDEAS **F-A (sysprompt addendum)** — possibly the same
  target, needs reconciling before either is actioned.
- **W2 — cite a stable anchor when a line number is approximate.** Reconnaissance must give an anchor
  (a distinctive comment or symbol) whenever a line number is not exact, and must label anything
  unconfirmed as unconfirmed. *Noticed 2026-09-15:* the container-controls Pass 2 digest flags one
  approximate line (`server.py:3316`) **and** supplies the anchor (`# Container logs:` at `:3319`) — that
  is the wanted shape; promote it from good practice to a rule.

## 5. Where things live (so "written down" means "findable")
| Content | Home |
|---|---|
| Phases, ordering, branches, gates | `project/v3_roadmap.md` |
| Vision & threads | `project/v3_vision_full.md`; promise table → `project/machine_model.md` |
| Bugs & open items (bucket) | `personal/parking_lot.md` |
| Closed items + root causes | `personal/bugs_and_fixes.md` |
| Architecture | `project/system_architecture.md`, `project/security_architecture.md` |
| Dev workflow + sandbox facts | `project/development_guides.md` |
| Reusable methodology & traps | `personal/lessons_learned.md` |
| Pre-brief collected findings | a named `project/*_findings.md` doc (e.g. `project/container_controls_findings.md`) |
| Handoff state across sessions | `personal/handoffs.md` |
| How we work (this file) | `project/working_agreements.md` |

**Never staged, never committed:** everything under `.thoughtmachine/knowledge/`.

## 6. Rules added 2026-09-19 (recording pass)

- **Structural-debt rule (creation discipline).** *No new create site, resolver, registry, or state
  machine until the existing one is either **the same site**, or **named as legacy with a checkable
  retirement criterion**.* Live register — the M-list, risk scores, sequencing and per-item status —
  is `project/structural_debt_register.md`; this file only states the rule.
- **A7 rule.** *A delegated agent's self-report is a **claim, not evidence**. A brief's imperative
  prohibition ("do NOT execute X") is **not a control** — only environmental enforcement (read-only
  mount / denied target path) or an **independent post-hoc state check** is.* Earned 2026-09-18
  (worker overstep; full class note in `project/run_design_pass.md` §7 / A7). In practice: verify a
  worker's result by reading environment state (hashes, `git status`/`git log` byte-identity,
  negative greps), never by accepting its summary.
- **Symbol-pin rule.** *Cite code by **symbol + file**, never by a bare `:NNN`; treat any line number
  as an **assumption to re-verify**.* Worked example: the same create site read
  `infra/container_manager.py:1463` in the 2026-09-18 audit and `:1612` at HEAD `bef93cd`
  (2026-09-19) with the enclosing method unchanged. W2 (supply an anchor when a line is
  approximate) is the weaker cousin of this rule.


## 2026-10-05 — Checkout-SHA rule (Main Gen 35)

- **Every read reports its checkout SHA.** Findings are valid only at that SHA; ticket citations must be re-derived at HEAD before entering a ticket. Same class as the symbol-pin rule (symbol, not line number) — the working tree and git HEAD are two holders of "current repo state," no reconciler. Earned 2026-10-05 on Edge 1: a recon worker read at `dev @ ad5c8d3` (parent) while the agent's HEAD was `be2e9a4`+, so its citations (`security_gate.py:887`, `:1149-1152`) were pre-commit; at HEAD those are ~L925-931 and ~L1249-1256.

## 2026-10-06 — Parent-commit comparison (Main Gen 35)

- **When a branch's full-suite red is claimed pre-existing, verify by running the same suite on the parent commit and comparing failure/error sets.** Pre-existing is a claim, not an assumption. The comparison is the decisive test. Earned 2026-10-06 on Edge 1: a "pre-existing" claim was verified by running `0bbdc49` — same 18 errors on parent and branch, 12→3 failures. Cheap, general, no ambiguity.

## 2026-10-06 — Agent git surface tracks the LIVE worktree (settled)

- **Settled 2026-10-06 (Infra Gen 33): the agent's git tools see the same worktree the operator's shell sees — there is ONE holder of repo state, not two.** Over the same session the worktree moved from detached `0bbdc49` (the parent-commit diagnostic) to branch `fix/edge1-session-grants-canonical` @ `5346df9`; the agent's own reads (`git_read branch` -> `* fix/edge1-session-grants-canonical`, `git_read rev_parse HEAD` -> `5346df9f98d7822ce97ae41957d9128a2a674eb1`) reflected the branch at the same SHA as the operator's shell, with a clean status. No divergence. Consequence: the earlier 'can the agent switch branches on its own' question is answered YES -- the surface is live, not a snapshot. Standing constraint unchanged: the agent must not issue `git_write checkout` without the operator's explicit word.

## 2026-09-19 — BRIEFS MUST BE PERSISTED **AND** INLINED.


BRIEFS MUST BE PERSISTED **AND** INLINED.

A brief is not delivered until it exists in two places:
 (1) PERSISTED — in the working doc (`.thoughtmachine/working_docs/`) or the KB, so it survives summarisation and session loss; and
 (2) INLINED — the full text pasted into the worker's `context["query"]` string.

Reason: the harness drops `context["briefing"]` (and other non-`query` keys) in transit, so a persisted-but-not-inlined brief never reaches the worker, and an inlined-but-not-persisted brief vanishes with my context window. Both halves are required. Cost of inlining is a few hundred tokens per job; cost of a worker acting on a truncated brief is a whole wasted 270s job plus a possible bad commit.


## 2026-09-19 — Commit granularity & merge discipline (operator-accepted clarifications) + master-vs-dev OPEN ITEM

**Commit granularity — accept the agent's shape.** One branch, MANY fine-grained commits (~20 on a branch is fine). Fewer *branches*, same *commit* discipline. When something goes wrong the agent stops and hands back a recovery point **in words** ("check out commit XY — from there we go further"). We do not need one branch per step. Scrutiny is applied at the **merge**, not per commit.

**Merge discipline — `dev` is the guarantee boundary, not every commit.** Invariant: **after every merge, `dev` is same-or-better than the previous `dev` commit** ("dev is always same or more secure than the last dev commit"). Between merges, commits on feature branches are the agent's workspace: they may be imperfect, and a branch may be momentarily RED. The gate lives at the merge into `dev` — host gate + security-relevant tests + the RED-first proof check.

**Dispatch wording rule (supersedes stricter phrasings):** *"branch CI is a signal; merge into `dev` requires green + host gate."* Do not describe this as "CI must be green before any commit" — that was over-strict.

**OPEN ITEM — master vs dev (operator to decide when release timing matters; NOT now).** `dev` is the real integration branch; `master` is stale; for a one-person operation `dev` *is* master. Options: **(a) Freeze `master` explicitly** — mark it "historical, do not use", document `dev` as canonical (one paragraph here). **(b) Make `master` the release ritual** — full host suite + install-and-start smoke + security-relevant tests on every `dev`→`master` merge, plus a tag; needs a person (the operator) to run it. **(c) Delete `master`** — honest if no public release tag is ever wanted; risky if one is. Recorded, not scheduled.

## Merge gates (2026-09-28)

Merge gates use the command's exit code, never parsed human-readable output.
Correct form: `gh pr checks <PR> --watch || exit 1` before merge.
Anti-pattern: parsing check output for the word "failing" — the watch form
and the non-watch form use different words, so a text gate can silently
never fire. This is the merge-form of the d70ceb8 commit anti-pattern:
run the check, then proceed unconditionally. One rule closes the class. Interim (no required checks configured — see the 2026-09-28 RECOMMENDATION below): the operative form is the name-filtered text gate below; a workaround, not the rule.

Why the first form failed: the gate was written as `gh pr checks <PR> --watch || { echo "..."; exit 1; }`. Pasted into an interactive shell, `exit 1` kills the terminal, so the operator removed it — and without it the block fell through and merged anyway. Script-style control flow and interactive pasting have different semantics.
The fix is a one-liner that cannot kill the shell: `gh pr checks <PR> --watch && gh pr merge <PR> --merge --delete-branch`. Short-circuit on failure, no `exit`.

Required checks only, not the unfiltered set. Attempted form `gh pr checks <PR> --required --watch` returns "no required checks reported" in this repo — branch protection has no required checks configured, so every check is advisory and --required gates on the empty set. Operative form: filter the check list by name. The repo suffixes advisory checks with "(advisory)".

  NONADV=$(gh pr checks <PR> | grep -v 'advisory')
  echo "$NONADV"
  if echo "$NONADV" | grep -qE 'fail|pending'; then
    echo "NON-ADVISORY FAILURE OR PENDING — do not merge"
  else
    gh pr merge <PR> --merge --delete-branch
  fi

Why the unfiltered form failed: gating on every check blocks merges on flaky advisory tests the repo has already declared non-blocking, forcing a judgment waiver each time and reintroducing the failure mode the gate closed. Why no `exit`: script-style `exit 1` kills an interactive shell, so the operator removes it, and the block then falls through and merges anyway. Why the if-block: interactive-safe equivalent — no `exit`, cannot kill the shell.


### RECOMMENDATION TO THE OPERATOR (held, not a strand) — make the merge gate structural, not conventional

No required status checks are configured on this repository, so `gh pr checks <PR> --required --watch` gates the EMPTY SET: it can never fail and therefore never protects anything, while still blocking the interactive shell with a `--watch`. The gate we actually operate is a *convention*: a human reads the full `gh pr checks` output and filters out checks suffixed `(advisory)` before deciding to merge. That convention holds the same fact (which checks must pass) in a second place — the operator's head — and it is unverified by construction: nothing detects that the filter was applied, that the list was complete, or that a new non-advisory check appeared since the last PR.

**Recommendation:** configure branch protection on `dev` (and `master`) with the explicit set of checks that must pass, excluding the advisory ones, so that `gh pr checks --required` gates a real, non-empty set. Then the operative command becomes the honest one — `gh pr checks <PR> --required --watch` — and the merge is blocked by the platform rather than by the operator remembering to run the filter. Two consequences worth stating up front: (a) when a new non-advisory check is added to CI it must be added to the required set deliberately, otherwise it silently does not gate; and (b) advisory checks must NOT be listed, or a flaky macOS advisory run starts blocking merges. This is a one-time configuration change on the host, not an agent-side strand; the agent cannot do it (no `gh`, no push, no repo settings), so it is recorded here as work for the operator. Folds into the next hygiene pass.


### 2026-09-29 — TWO ADDITIONS (ruled; no separate strand)
**WA-1 — PROVENANCE RULE (mechanism naming).** Every verification report must name the mechanism it *actually* used, concretely enough to re-run. Tool-name claims are auditable and must be accurate.
*Corollary (learned the hard way this session):* before flagging a provenance claim as false, check whether the named mechanism exists as a **tool** rather than only as a **shell binary**. These differ in this sandbox: there is no `git` binary in a worker container, but a git *tool* may be available to the agent. A report saying "git diff" may be legitimate tool use described loosely — not fabrication. Do not escalate a mechanism claim to a trust finding without checking the tool surface first.
**WA-2 — SANDBOX ≠ HOST (environment parity).** The sandbox runs **Python 3.11.16**; the host runs **3.12**. **Sandbox green never substitutes for host green.** Evidence: boot-sweep strand — sandbox full suite **3908 passed / 62 skipped**, host full suite **3954 passed / 16 skipped**, both exit 0. The **+46 passed / −46 skipped** delta is exactly the Docker-gated suite, which only runs on a host with a working daemon. Both numbers are correct; they measure different environments.
*Related sandbox environment facts (bank them, they cost a worker call each time they bite):* `.venv/bin/python` is a **broken symlink** ⇒ fall back to `python3`; `/tmp` is mounted **noexec** (and is a 64 MB tmpfs — clear `/tmp/pytest-of-agent` before full runs); `/home/agent` is a per-container **rw,exec,size=256M tmpfs**; `/workspace` is the **rw bind** of the session workspace (so anything written there lands in the operator's repo).

## 2026-10-05 — Close-claim gate (Main Gen 35)

- **Close-ready requires full CI green on the PR.** Local pytest is pre-flight only. Sandbox is signal only. Focused runs miss interactions (sys.modules ordering, cross-module fixtures, environment Python minor mismatch). Evidence: Edge 1 branch `fix/edge1-session-grants-canonical` (PR #67) was declared close-ready on file-scoped green runs; full CI returned 12 failed / 18 errors. A branch is not close-ready until the PR's full CI is green.
- **"Commit authorized" is not "branch close-ready."** The diff-read covers the delta; CI covers the branch. Main's commit authorization is delta-scoped by definition — it names what was reviewed. Extending it to branch-close is the extension error. Earned 2026-10-05 on Edge 1.

## 2026-10-06 — Push and merge authorization (Main Gen 35)

- **Merge authorization requires full CI green on the PR.**
- **Push authorization requires:** local full suite clean, diff failure-neutral against parent, and explicit Infra go. The failure-neutral proof must be a required line in the push report — not assumed.
- **No close claim on focused or sandbox runs.**
- **Pre-flight must report the skip count alongside pass/fail.** Tests skipped due to missing local binaries (e.g. `skipif(shutil.which("git"))`) are silent signal-loss — local failures ≠ CI failures when the local set silently excludes tests.

## 2026-10-17 — Named pattern: `read-beats-ruling` (4 instances this generation, Main Gen 38)

- **When the read contradicts the ruling, the ruling was based on the wrong layer.** This is *not* a failure of the read-beats-ruling discipline — it is the discipline working. The cost is one wasted prescription per instance; the thing to fix is **noticing the layer**, not the divergence.
- **Instance 4 (2026-10-17, endpoint divergence).** Ruling: *"two endpoints disagree"* → prescription: *"unify them into one canonical endpoint."* **Both wrong.** The read found both endpoints **correct** — Route A is session-scoped and returning defaults without a `session_id` is by design ("a missing id is a valid transitional state"); Route B is workspace-scoped and returns the declared ceiling. Two different facts; two endpoints is fine. The defect was at the **UI layer**: the panel calls Route A with a bare URL and no `session_id` (so it gets the designed default and displays it as "effective", every workspace, always); the store ignores `workspace_ceiling` and `contradictions` and sets `ceiling := effective` (**the ceiling column is fabricated from the value the store just mislabelled**); and Route B's `effective` key is itself mislabelled (it is the declared ceiling). **Framing at the endpoint layer; truth at the UI layer.**
- **Instance 3 — the sweep read.** The ruling sat on the wrong mechanism (H1).
- **Instance 2 — the session_lifecycle read.** The ruling sat on the wrong file.
- **Instance 1 — earlier this generation; not restated in the 2026-10-17 exchange.** Count it in the tally; do not infer its shape.
- **Same instance, second error — a diagnostic misread.** `workspace_ceiling: {}` was read as *"absent."* `{}` means **present but never restrictive** — the ceiling exists, nothing was capped, so no annotation was written. Proof it was not absent: the non-empty `/permissions` map beside it. **A diagnostic's shape carries meaning; an empty value is not a missing one.**
- **Discipline consequence (Main, 2026-10-17).** Before prescribing a fix for a divergence, ask: **"at which layer is this actually true?"** The error enters when ruling from downstream reports; the source read is what catches it. One `grep`, one file read, one curl — always cheaper than a wave built at the wrong layer.
- **Scope note.** The endpoint thread itself is **Infra's work** (one branch, four commits, before Credential). Only the meta-lesson is recorded here.

## 2026-10-17 — Named pattern: `log-reads-as-backlog` (Main Gen 38)

- **A document's *shape* can signal a *role* it does not serve.** `operator_observations.md` is a chronological append-only log; it *reads* like a backlog. A reader searching "core" lands in the log, finds substantive core items, and reasonably concludes *that* is the backlog — while the file that declares itself the backlog (`project/core.md`) sits stale. Named 2026-10-17 by Main Gen 38 during the core-work consolidation: **`log-reads-as-backlog`**, a `displayed≠enforced` variant at the KB level. Evidence: **eight** pending core items had no home in the file that called itself the single home.
- **Remedy (already applied — keep it permanently).** An explicit badge at the top of the log — *"CORE WORK HAS MOVED. This file is a LOG, not a backlog."* — naming the canonical file, plus a cross-pointer in the superseded `open_items_register.md` §11. The badge states the **role**, which is exactly what the log's shape was accidentally implying.
- **Generalisation.** Whenever a "single home" or "single source of truth" is declared, check for **competing documents whose form mimics the role** — logs, changelogs, observation feeds, audit trails, append-only registers. The danger is not that items are missing; it is that they are findable in the *wrong* place and look authoritative there.

[2026-10-06 11:04:04] ## 2026-10-06 — Named pattern: fixes reveal siblings (Main Gen 35)

- **Third time this session the class reproduced inside its own fix.** Log as a named pattern: *fixes reveal siblings; the sibling must be enumerated in the fix's own scope or the class survives inside the fix.* Deeper form of the partial-fix rule: the fix's diff itself is a place the class can hide. Instances this session: (1) the session_manager mirror of the bridge normalizer; (2) the two-attribute divergence in the git tool's gates (B7); (3) the fixture fix leaving a second sys.modules poisoner. Evidence for `design/layers-and-truth-ownership`.

## Verification

- **Verifier provenance rule.** A verifier must name the mechanism it actually used. A report that names a tool the environment cannot run ("git diff" in a container with no git binary) is not fabrication, but it is misleading. The failure it prevents: a report whose stated mechanism could not have run cannot be trusted to have been produced by that mechanism.

- **Sandbox vs host Python.** Sandbox and host run different Python minor versions (container 3.11.16; host venv 3.12). Sandbox green is a signal; host suite is the gate. The failure it prevents: reading a sandbox pass as equivalent to a host pass when the runtime differs.

- *Cross-ref: the two rules above restate WA-1/WA-2 already recorded under "## 2026-09-29 — TWO ADDITIONS (ruled; no separate strand)" earlier in this file; this section is the canonical home.*

- **Transport-level round-trip tests.** State-changing fixes require a round-trip test through the actual transport (HTTP and WS and refresh path), not a direct function call. Earned from the sidecar-clobber firefight.

---

## 2026-09-29 — consolidation additions

See §Merge gates (2026-09-28) and §Verification for WA-1/WA-2 and the exit-code gate.

### A. Touch policy — who does what with the repo

**Agent** — writes code, commits on named feature branches within an authorized strand. Discloses the pathspec. Never pushes, never merges. **Workers** — no git binary at all; write files, agent stages their output. **Operator** — pushes, opens PRs, merges, runs host bash, restarts, Docker. **Main** — decisions, no code, no host, no dispatch.

*Failure it prevents:* the imprecise mental model "the operator is the only one who touches the repo" — it either blocks an authorized agent commit or bundles a commit into a host request, wasting operator attention. Caught at handoff Gen 27→28.

### B. Deletion gate greps the entire repo — no whitelist

Before deleting any tracked file, grep the whole repo for imports and references. No path whitelist.

*Failure it prevents:* a deletion brief listing only `README.md docs/ install*.sh .github/`, omitting `tests/` — `tests/docker/test_workspace_lifecycle_wiring.py:23` imported the target. CI red, PR #37 closed. Any whitelist-shaped gate will eventually miss the file that matters.

### C. Branch hygiene — `git switch dev && git pull` before `git switch -c`

Always, before cutting a new branch.

*Failure it prevents:* a branch cut from a stale local `dev`, producing a PR against the wrong base and a wasted round-trip. Two seconds, every time, even when local dev is believed current.

### D. Verify diff content, not just diff scope

Read the hunks, not only the stat.

*Failure it prevents:* README PR #38 merged — the stat showed `+12/−7` README-only, but the diff did **not** remove the old subtitle; both openings stacked. A scoped diff is not a correct diff.

### E. Placement and intent must agree in every brief

State the anchor **and** the effect, not just the location.

*Failure it prevents:* README #38's brief said "replace the opening framing" but placed the insertion "before Platform support." The agent satisfied the placement literally and the intent silently failed. Name what the location must look like when done.

### F. Named-paths-only staging on hygiene strands — never `git add -A`

`git add <paths>`. Every path named.

*Failure it prevents:* `web_ui/frontend/.npmrc` — index-resident from an earlier `-A`, invisible to `git status --porcelain`, rode into a commit. Same session, `gen20_pathspec_test.txt` and a KB file rode via `-A`. The first index-sweep hides per-machine files as tracked before `git status` can flag them.

### G. `git_write` deletion-commit pattern

Leave the deletion unstaged (` D`); the tool's own `add` records the removal. `commit` requires `file_path`, rejects `.`, `..`, globs.

*Failure it prevents:* a staged deletion silently re-added by the tool's internal `git add -- <paths>` → no-op commit. And a rejected `.`/glob `commit` call → wasted round-trip.

### H. Restart protocol — labelled `## Operator` block, not prose

Surface a needed restart as a labelled `## Operator` block, "run this on host."

*Failure it prevents:* a fix merged to `dev` while a running agent process keeps executing modules imported at startup — a tool "still fails" and looks exactly like a code bug. The git fix was merged at `a86491dd` while a running process still failed on pre-fix code. A label forces the restart request onto operator attention.

### I. Vault root and repo KB are different directories

**Vault root = `~/.thoughtmachine/`** — runtime state (workspaces, sessions, credentials, logs, agent_config). **Repo KB = `<repo>/.thoughtmachine/knowledge/`** — gitignored working docs; `working_agreements.md` lives here. Different directories, different purposes.

*Failure it prevents:* the Gen 27 handoff conflated the two; New Infra's first read missed on `~/.thoughtmachine/knowledge/...` and cost a turn. The next reader will make the same error.

## 2026-09-29 — recon methodology notes (intent-state-divergence-family)

1. **Counts are recon outputs, not recon inputs.** The number of effective-permission computations was not pre-decided. The dossier named three; Infra guessed four; the recon enumerated five. Do not pre-decide a count the recon is meant to produce.

2. **The recon is the evidence base for the design decision. If the map is wrong, the design inherits the wrong shape. Slow is fine. Incomplete is not.**

3. **Guard conditions derive from the artifact, not from prose about the artifact.** Three instances in one session: a `(.venv)` guard built from a terminal paste; an expected-zero count built from a mental model; `recon-w1`..`recon-w4` worker names assumed without reading `workers.json`. All caught before damage. The rule applies to every artifact where an assumption is embedded — including Infra's briefs.

4. **Audit methodology is a fact about how we investigate, not a state holder of the machine.** Recorded in the recon's methodology section, not assigned a surface ID.

5. **Tool-call budget is advisory, not enforced.** In the recon, the 60-call stop condition fired in no over-budget pass (W1 68, W2 48, W3 126, W4 104). Future briefs treat the budget as soft, or state the stop in a form workers honour.

## 2026-09-29 — loaded-but-id-unset window (machine fact)

`create_session` / `load_session` set `bridge._loaded_session` + `_session_config` + `_workspace_id` but do NOT set `_session_id`. Only `bridge.start` / `_restart_controller` / `_on_controller_event` capture it. Between session load and the first controller event, any push site that passes `bridge._session_id` directly will carry None. Canonical resolution: `bridge._session_id or (bridge._loaded_session.session_id if bridge._loaded_session else None)`. Precedent: 9 sibling sites. Failure this prevents: push surface reporting false-deny-all while a real session exists.
## 2026-09-29 — brief self-check before dispatch (methodology)

Before dispatching any brief, Infra re-checks its own brief against source for:
1. **Base existence** — does the branch/file/call form the brief assumes actually exist at the named base?
2. **Form** — does the code form the brief names actually parse and does it handle the None/empty cases?
3. **Direction** — is the pre/post direction named from a read, or from a mental model?

Failure it prevents: a brief that cannot be built on its named base (ch3b from `dev`); a code form that raises on the branch it will actually run (`bridge._loaded_session.session_id` unguarded); a direction predicted without reading ("narrows" when it broadens). Fifth "expectation from mental model over source" instance logged this session: `(.venv)` guard, ` -- ` expected-zero, `recon-w1..w4` names, `git rm --cached` vanished-path expectation, ch3b base + form + direction. All caught before damage; the remedy is this self-check.

## 2026-10-01 — capture rule, master historical, worker-name registry

- **Operator-observation capture rule.** Operator statements that sound like a finding, intent, preference, or recheck are written to `operator_observations.md` in the same session, or they did not happen.
- **Master is historical.** `master` is historical; `dev` is canonical. Revisit at first release tag.
- **Worker-name registry gap.** This session exposes only `default`/`default2`; the recon-methodology name-collision guard ("fresh worker name") has no registry to check against. The instruction is unenforceable as written — either a registry is built or the guard is retired.

## 2026-10-01 — Read-only recon routes to the agent

Read-only recon (grep / find / file previews) routes to the **agent**, not the operator's host bash. Host bash is reserved for git / CI / pytest / restart / Docker.

## 2026-10-01 — worker budget deleted; ticket-name truth (Main Gen 34)
- **Worker budget — DELETED.** Workers are not turn- or call-budgeted. Main chunks work by scope; if a chunk runs long, Main chunks smaller. Timeout is the safety backstop. The user's max-turns setting is the only system-enforced cap. The real signal is pruning-cycle dilution — if a worker runs so long its findings get watered by context pruning, Main sees it and chunks smaller.
- **Ticket-name truth.** When closing a ticket, the PR body names the *actual surface* fixed, not just the ticket name. Earned from `bug/session-workspace-tab-missing-containers` / PR #51 landing on `WorkspaceDetailPage` (workspace page), not the session panel.

## 2026-10-03 — PREMISE DISCIPLINE (Main Gen 34)

*"Briefs name the premise the fix depends on. The worker's first step is to verify that premise against source. If the premise is false, the worker stops and reports — the fix does not proceed. Premise-false is a correct outcome, not a failure."*

**Filing note (Infra Gen 31).** Main Gen 34 asked for this line "under the Premise discipline section". No such section existed in this file (checked via `mode=summary` over all 17 sections), so it is created here as its own dated section rather than appended into an unrelated one. Nearest existing kin — and deliberately kept separate, because the two halves bind different actors:

- `§2026-09-29 — brief self-check before dispatch (methodology)` = the **agent/author** half (re-check my own brief: base, form, direction).
- this section = the **worker** half (verify the premise the brief names; stop and report if false; do not guess a fix around a false premise).

Earned 2026-10-03 on `fix/edge4-record-vs-live-state`: a ruling-directed refinement named an "emit site in container_record/state.py" that does not exist (state.py is pure/IO-free and only *constructs* findings; emission lives in `drift.emit_drift_findings`). The worker stopped, wrote no code, and returned the false premise — the correct outcome under this rule.

## 2026-10-03 — Infra escalation boundary (Main Gen 34)

- **What escalates vs what Infra rules.** Revising *what a Main ruling means* - its intent, its scope, or
  its accepted consequence - **escalates**. Revising *how that intent is achieved*, where the intent is
  clear and the new shape delivers it better, is **Infra's to rule and disclose**. Same shape as the
  mechanical-vs-model split: mechanism is Infra's, meaning is Main's.
- **Disclosure is mandatory.** Any in-scope re-shaping is reported back in one line, naming the ruling it
  serves, so Main can object without re-deriving the change.
- **Boundary test.** State the ruling in one sentence. If that sentence holds, it was a mechanism call.
  If the sentence would have to change, it was an escalation.

## Phase 2 recon at 0bbdc49 (agent, Gen 34)

## 2026-10-05 — RECON AT HEAD 0bbdc49 — resolved facts (worker-verified, bra...

RECON AT HEAD 0bbdc49 — resolved facts (worker-verified, branch fix/edge1-session-grants-canonical).

1. identity/fingerprint: HEAD 0bbdc490647dca6fc325d653638088143c0f53c3; starred branch `fix/edge1-session-grants-canonical`. security/security_gate.py = `ad08658009f594f34d4b86dbaa3c11eee2d86436d6fb91aa8fc0c6bd110986c4`, 75153 B; tests/test_session_grants_canonical_pins.py = `381a1d644c649d89bf83ac68d828e0675136db02431da1f0bc34d62e26927a2a`, 34544 B; tests/test_worker_permission_hardening.py = `186a010e06430396a23dd02450e82c4a14b7130b2a8a5a06d79ed856a235fa1d`, 56403 B. The worker's git_read surface AGREES with the operator shell ⇒ the agent observes the same worktree state (question closed).

2. FAIL-CLOSED SEMANTICS (refutes the earlier D4/Read-1 note): on a session-permission STORE-READ FAILURE the effective filesystem mode is `banned`, NOT `read`. `_DISK_FAIL_CLOSED_SESSION` (security/security_gate.py L81-88) carries filesystem="banned"; substituted at L935 (get_effective_permissions) and L1247-1250 (resolve_container_config). `(fail-closed: {reason})` literal EXISTS (L1398, L1526) and `_fail_closed_reason` EXISTS (__slots__ L512, init L516, read L521, set L1098). Earlier "absent" note was measured on the wrong tree (dev).

3. PIN LABELS: tests/test_session_grants_canonical_pins.py labels pins B1..B5 with inline banner comments. There is NO literal `2(b)` label. The store-read-fails pin is B4 = class TestDiskModeGateFailsClosed (L204, banner L202) whose `test_gate_all_banned_when_read_raises` (L205) asserts `effective["filesystem"] == "banned"` at L219. The ONLY `== "read"` assertion in that file is L106, inside the empty-sidecar DEFAULTS case (L92 test_present_empty_sidecar_yields_defaults) — not a failure path. Any plan wording "pin 2(b)" must be translated to B4.

4. FIXTURE SIBLING CONFIRMED at HEAD: tests/integration/test_apply_config_coverage.py::contract_server (L111-148, scope=module) is PURGE-WITHOUT-RESTORE — it deletes sys.modules entries matching prefixes ("web_ui.backend","agent.config.provider_profile","thoughtmachine.bootstrap","session") and its teardown only stops the Path.home patcher, restores HOME + OPENAI*/DEEPSEEK*/OPENAI_COMPATIBLE* env, and rmtree's the tmp HOME; there is NO saved_modules dict, no parent-package attr snapshot, no _ABSENT sentinel, no sys.modules restore. Canonical remedy lives in tests/integration/test_bridge_error_notice_dedup.py::bridge_mods (L29-107, landed by bb608a5 / PR #66): snapshot saved_modules + saved_attrs (parent-package attrs re-bound by the re-import, with `_ABSENT = object()` sentinel), delete, re-import, then teardown `del sys.modules[name] for name in _matched_modules()` + `sys.modules.update(saved_modules)` + re-point each parent attr (pop if _ABSENT else assign). FILE IS A SIBLING of PR #66's file, so the Shape-2 save/restore remedy applies.

5. conftest CORRECTION: tests/conftest.py::hermetic_vault (L310-364) DOES call importlib.reload(thoughtmachine.bootstrap) (L329) and importlib.reload(agent.knowledge.global_kb) (L330), but the token `ImportError` occurs NOWHERE in tests/conftest.py (0 grep hits) — the failure is raised by reload() at runtime, not by a guard. Also stale: comments at L127 and L256-260 claim a six-module reload loop runs inside hermetic_vault_env, but that fixture (L274-289) only sets the global base dir and yields — no reloads, and hermetic_vault reloads 2 modules, not 6.

## Worker/Orchestration lessons

## 2026-10-05 — DO NOT use the async worker pattern (submit_query + job_stat...

DO NOT use the async worker pattern (submit_query + job_status polling). Operator ruling 2026-10-05: the orchestrator has no sleep tool, so polling is a busy loop that burns context without bound and cannot be stopped once started. Rule: use BLOCKING Worker calls only (action=spawn with a `query`, or action=query), which wait until the worker finishes; if a run is long, SHRINK THE SCOPE of the call instead of backgrounding it. Corollary for long test runs: never background the full suite; run a bounded subset in a blocking call (single file / single class / order-probe pair), or accept the wait inside one blocking call.


## 2026-10-07 -- /tmp tmpfs trap. /tmp is 64MB tmpfs; full suite consumes ~36MB under TMPDIR. Clear TMPDIR between full-suite runs or the suite hits ENOSPC and emits garbage signature (664F/396E). Report file size after writing.

## 2026-10-08 — Ticket-location hygiene — `hygiene/wa-ticket-location` (Main Gen 35 close-out)

- `[hygiene]` Deviation 40 (Edge 3 Stage 1 close-out): a filed ticket's home is
  `project/operator_observations.md`. `personal/parking_lot.md` is NOT the ticket
  registry. The Edge 3 plan's T3 location hint named `personal/parking_lot.md` and
  was wrong — all six Gen-34 tickets plus the seventh dual-attributed candidate
  landed in `project/operator_observations.md`.
- `[ticket]` `hygiene/wa-ticket-location` — codify: tickets go in the
  `operator_observations` registry (one `##` section per ticket id), never in the
  parking lot.

## KB philosophy — book of memories, not working cache (2026-10-14, Main ruling; Infra Gen 41 encoding)

Do not delete KB content. Archive is fine. The KB is a book of memories,
not a working cache. Old work is reconstructible history, and the operator
uses it. Artifacts (compiled files, logs, clones) may be removed. Records
(reports, backups, findings, handoffs) are kept — active or archived,
never removed.

## Relay-tax discipline (2026-10-14, Infra Gen 41 — Infra discipline, no Main ruling)

Three rules to reduce the operator's relay load. All Infra-internal; none
changes a seat boundary.

### Rule 1 — Docs-before-Main
When a question is already answered by the handoff, the trust contract, the
current design doc, `status.md`, or `working_agreements.md`, Infra decides and
reports. Main is reserved for genuine design questions the docs do not cover.
Asking Main a question the docs answer is a relay tax with no payoff.

### Rule 2 — Batch status reports per closed wave, not per PR
One report to Main per closed wave, led by the merge SHAs. Not one per PR,
not one per commit. Shape: "Wave N closed at SHA. Review flags: [...].
Next: N+1." Intermediate turns (PR up, CI running, CI green) are not
reported to Main — they belong to the operator and the agent.

### Rule 3 — Independent parallel dispatch
Infra may dispatch a strand in parallel with the current wave when all three
hold:
  (a) disjoint file surface — no file touched by both strands;
  (b) no shared design decision — the parallel strand does not need the
      wave's ruling to proceed;
  (c) Main has already blessed the parallel strand as arc work.
When any is absent, serialize.

**Rule 3 — corollary (branch discipline).**
One active agent per host checkout. Do not create the next dispatch's
branch on the host until the current agent has returned and his branch
is pushed. For true parallelism, use `git worktree add <path> <branch>`
— separate working trees, same repo — so two agents never share a checkout.

## A PR under review is not mergeable (2026-10-17)

A PR under Main review is not mergeable, even if the review response
hasn't landed yet. "Review in progress" means stop.

**The pattern worth naming:** the merge-discipline failure happens on
the PRs that *feel safest*, not the risky ones. The risky ones get
reviewed because they're risky. The safe ones — small, green, mechanical
— get raced because they feel like they don't need review. That's the
trap. The green-and-small condition is exactly when the race happens.

**Practiced 2026-10-17:** Infra merged PR #126 (container:remove
cleanup) while Main's pushback was in flight. Reverted by the interim
fix wave (`fix/container-remove-interim`). The recovery was clean, but
the race was avoidable. Treat "review in progress" as a stop, not a
queue.

## A ticket that names a code site cites a symbol (2026-10-17)

When a bug ticket names a code site as the mechanism ("the defect is in
X.jsx at Y"), it must cite the exact symbol (function, method, or
variable). The worker's first step is to verify the symbol actually has
the defect before implementing.

**Why:** two tickets this session named the wrong site:
- `bug/panel-actions-no-op` named `dispatch` in `ContainerListPanel.jsx`;
  actual cause was the 401 actor gate, one layer up.
- `bug/panel-unsaved-state-after-apply` named `SessionSidebar.jsx`;
  actual cause was in `ConfigPanel.jsx`'s `permsDirty` comparison.

In both cases the *symptom* was real and the mechanism was wrong. The
read found a different code path than the ticket named.

**The rule:** "ticket-names-wrong-site" is a sibling pattern to the
anti-pattern family. A ticket that names a mechanism without citing a
symbol is a mechanism-claim, not a mechanism-fact — treat it as a
hypothesis, not a directive.

**Reference:** `operator_observations.md` under "Sibling pattern —
ticket-names-wrong-site".

## 2026-10-18 — Gen 38 seat additions

### Review-in-progress is a stop, not a queue

**A PR under Main review is not mergeable, even if the review response hasn't landed yet. "Review in progress" means stop.**

Earned 2026-10-17 (Infra Gen 45): merged PR #126 (sub-grain removal) before Main's pushback landed, because it was green and the change was small. Both true. Both are exactly the conditions under which the race feels safe. The correction shipped (fix/container-remove-interim) but the merge should not have happened.

**The pattern worth naming:** merge-discipline failures happen on the PRs that feel safest, not the risky ones. The risky ones get reviewed; the safe ones get raced.

Consequence: when Main pushes back on an open PR, that's a signal to hold. Not "we'll merge if CI goes green first." Hold.

### Audit consumption: trust positives, verify negatives

See `operator_observations.md` reading rule. Adopted 2026-10-18.

### Ticket discipline: cite a symbol, worker verifies before implementing

A ticket that names a code site cites a symbol. The worker's first step is to verify the symbol has the defect before implementing. Same discipline as premise-verified-before-RED, applied at the ticket, not just the fix.

Earned 2026-10-17 (Infra Gen 45): two tickets in one wave named the wrong file. The symptom was real; the mechanism was wrong. Both caught by the worker reading source before implementing.

### Hold, not race — the rule from the merge discipline

**When Main pushes back on a PR that is already open, the response is "hold," not "queue and race."**

Same shape as the review-in-progress rule. The specific form: if you have pushback in flight and you're tempted to merge because the change is small and green, that's the exact moment the rule applies. Hold.

### The penultimate-turn KB-hygiene pattern

Before a seat hands off, one turn is spent making the KB match what the seat learned. Not a handoff — a persistence turn. Every finding, ruling, anti-pattern, and read-beats-ruling moment this seat accumulated gets written to its canonical file. The handoff turn reads from the now-honest KB.

Earned 2026-10-18 (Gen 38 seat, on the operator's instruction).

## One command per block for `gh pr create` with a body file (2026-10-18)

When writing a PR body to a file and then creating the PR, split into
**two** host blocks:

1. Write the body file (`cat > /tmp/pr-body.md <<'EOF' ... EOF`)
2. Create the PR (`gh pr create --body-file /tmp/pr-body.md`)

**Why:** today it failed twice, both times the same way — the heredoc
content got truncated mid-write (or the shell read stdin across the
`gh pr create` command), leaving the body file broken or the create
step Ctrl-C'd. The tickets/commits in the same block survived; only the
`gh pr create` step broke.

The fix is process: one command per block. It's not a bash bug; it's a
paste-buffer / read-greedily interaction with long heredocs immediately
followed by another command that reads from stdin.

**Filed:** 2026-10-18, Gen 45.

### Every Resource bug this arc was a lifecycle bug

> Every Resource bug this arc was a lifecycle bug. Not the create path, not the grant path,
> not the read path — the transitions after creation. The compass: "is this a lifecycle
> transition that has no owner?"

The compass generalises past containers. When a thing is created and then destroyed,
stopped, restarted or re-parented, the defect lives in the transition, not in the creation.
Ask it before reading code: name the transition, then name its owner. If the answer is
"nobody", that is the bug — not a missing check, but an unowned edge.

Recorded from `knowledge/project/operator_observations.md:2098-2118`, where the compass is
flagged as belonging in this file "as doctrine. Not there yet." This entry is that doctrine.

Filed 2026-10-22 (bookkeeping chunk A).

### When a test passes, ask what it would take to fail

> When a test passes, ask what it would take to fail.

A green test is evidence of the author's model, not of the system. The question converts it
back into evidence: name the change that would make this test go red. If nothing would, the
test asserts nothing. If the only thing that would is deleting the code the test calls, it
confirms the fix instead of attacking the rule.

This is the Verification row of the three-layer table at
`operator_observations.md:2120-2131` (Code / Prompt / Verification), where a verification
counts only if it is "a test that would go red if the rule were removed".

Filed 2026-10-22 (bookkeeping chunk A).


## 2026-10-23 — ## Grain-addition ripple factor (~4x) — from W2 credential

...

## Grain-addition ripple factor (~4x) — from W2 credential

*Adding a grain to the permission catalog touches ~4× the site count you'll estimate.*

The ruling for W2 enumerated 14 sites. The real surface was 14 sites **+ 61 pre-existing tests** (plus one production regression the grain exposed). The implicit contract — "the permission catalog has exactly 7 keys / the catalog JSON has exactly 9 entries" — is spread across ~20 test files as hard-coded literals: `ALL_BANNED`-style module constants, `_PERMISSION_KEYS` tuples, `_EXPECTED_NAMES` sets, `len(body) == 9` count assertions, and expected-profile dicts.

**Operational rule:** run the FULL suite before declaring a grain change done. The 61 stale literals are invisible until the grain lands — a per-file or per-site test run will look perfectly green. Do not accept "my sites pass" as evidence of completion on this class of change.

**Corollary — snapshot-shaped data needs a tolerant read, not a padded default.** `container_record.permissions` is a write-time snapshot of the grains that existed then. Whole-dict equality against today's `effective` profile means every legacy record reports permanent, unclearable drift once a grain is added. Correct reading: compare the intersection; an absent key means *not recorded*, never *banned*. Padding absent keys with the default fabricates a false *widening* alarm on every legacy record whose effective sits above that default — the same false-alarm-by-construction problem in the opposite direction. Pin the self-heal (a re-written record regains coverage) with a test, or the tolerance silently becomes a coverage hole.

**Corollary — classify before repairing.** In a test-repair task, the highest-value instruction is a firewall: *LITERAL* = the assertion diff shows only an added key or a ±1 count; *SEMANTIC* = anything else, stop and report untouched. Without it, a repair pass greens semantic failures by editing expectations — manufacturing a `declared-but-not-enforced` divergence in the test corpus itself.

## 2026-10-23 — WA-3 (CORPUS NAMING, added 2026-10-23 after the W2 CI break)...

WA-3 (CORPUS NAMING, added 2026-10-23 after the W2 CI break): a "full suite" claim is only valid if the corpus is (a) run through the pyproject `testpaths` (a bare `pytest`) or (b) with every root named explicitly (`pytest tests web_ui/backend/tests`), AND (c) the corpus is NAMED in the report alongside the counts. A positional path argument SILENTLY OVERRIDES `[tool.pytest.ini_options] testpaths`, so `pytest tests` runs 5146 tests and reports green while 148 tests in `web_ui/backend/tests` are never collected — 153 nodes invisible, 1 of them red in CI. CI is the authority for corpus coverage; a sandbox green is not evidence about a corpus it did not collect. When counts differ from CI, reconcile the TOTALS arithmetically (passed+skipped+xfailed+failed) and label any skip-count inference as a hypothesis until the CI log confirms it. Related: WA-2 (sandbox-vs-host corpus gap).

## 2026-10-23 — WA-4 (WORKING-DOC FILING NAMING, added 2026-10-23 after a fi...

WA-4 (WORKING-DOC FILING NAMING, added 2026-10-23 after a filing was silently gitignored). `.thoughtmachine/working_docs/*` is IGNORED by default (`.gitignore` L118-120) and re-included only by explicit negations — `ticket-*.md`, `protocol-*.md`, `reproduce_p0.py/.sh`, `P0_REPRODUCER_README.md`, plus the WorkingDocument `*.json` files. Consequence: a ticket written as `design-<slug>.md` or `hygiene-<slug>.md` lands on disk, `git status` reports CLEAN, and the file never reaches the repo — every subsequent reader assumes it was never filed. **Rule: any filed ticket MUST be named `ticket-<category>-<slug>.md` (or created via WorkingDocument, which yields the tracked `.json`). Verify a filing with `git status` — a clean status after writing files means they are ignored, not that they are committed.** Internal cross-references between clustered tickets must carry the new filename, so sed for the bare name with a `(^|[^-])` guard to avoid `ticket-ticket-` double-prefixing. There is already a standing ticket for this exact trap (`ticket-hygiene-working-docs-cluster-reports-gitignored.md`) — a prior cluster report was lost the same way, so this is a recurrence, not a one-off.

## 2026-10-23 — No tool may invoke another tool (class rule, not CI-enforced)

**Rule.** No file under `tools/` may import another tool class or look up `TOOL_CLASSES` at runtime. Tools are leaves of the permission model, not nodes.

**Why.** A wrapper is a second authority on "can this session call this tool" — one fact (may this session run host_bash?), multiple authorities (the executor says no; the wrapper says yes), they disagree. Inv.6 violation by construction, and it's the class of bug `PaginateTool` exhibited on 2026-10-23: a default-permission session could reach `host_bash` through `PaginateTool(target="HostBashTool", ...)`.

**Enforcement.** Review time, by humans — not a test. `tests/test_tool_no_wrapping.py` does not exist and never has on this branch. A guard does exist — `.thoughtmachine/working_docs/tool_no_wrapping_guard.py`, AST-based, fails on any import of another tool class or runtime registry lookup, no whitelist, no `xfail`, no `skip` — but it is **parked and unactivated**: no test module imports it and no workflow runs it. **The guard must not be vacuous** — it walks up to find the repo root (a parked-location `parents[1]` resolves to `.thoughtmachine/` and scans 0 files; that failure mode was caught in this session and the fix is `_find_repo_root()` + `test_scan_is_not_vacuous`).

**Exception: none.** If a tool needs a capability another tool has, extract a shared internal function — not the tool class. Sequencing is the agent loop's job (plan → dispatch → observe), not a tool's.

**The escape-hatch clause.** "Structural blocklist ≠ call-time gate." If a mechanism exists only at construction/spawn, a wrapper that calls a different object at runtime is a bypass. Every tool named in a construction-time filter must also have a call-time refusal, or explain why not.

**The seat's share of the miss.** Gen 48 briefed the paginate fix as "category bypass" — accurate but understated. The operator read "host access" from it, which was the correct framing, and Main named it before the seat did. The lesson: **briefs name the mechanism, not the class of the check.** "Authority laundering" and "host access" are the true names; "category bypass" is the check that failed.

**Filed:** `hygiene/no-tool-wrapping` (doctrine + census + guard). The guard named above is the **parked, unactivated** one. The census is a point-in-time figure, attributed and not measured today: 13 sites at park, 8 of those burned down in `cf10c358` (#189), the residual letter (a) recounted by nothing in CI.
