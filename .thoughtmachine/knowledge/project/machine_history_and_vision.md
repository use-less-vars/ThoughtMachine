# machine_history_and_vision.md — how the boundary was found, and what it is for

**What this file is.** The machine's history told in the order its rules were learned,
followed by the vision those rules serve. Every structural rule in `MANDATORY_STOP.md`
and `architecture_invariants.md` was paid for by an incident; this file is where the
incident and the rule sit side by side, so a reader can tell a law from a preference.

It is not the architecture (`docs/ARCHITECTURE_MACHINE_MODEL.md` — what runs where), not
the commitments ledger (`vision_ledger.md` — V1–V5 with verdicts), not the incident log
(`reliability_log.md` — dated rows, several still **UNVERIFIED**), and not the
celebration file (`hall_of_fame.md` — entries that must name the commit that made them
true).

**Rules for this file.**

- Every historical claim names its source: a file and section, or a commit. A claim with
  neither is written as a claim.
- Nothing is called "fixed" unless a commit or PR is named that made it true on `dev`
  (the standing rule in `hall_of_fame.md`).
- Where `reliability_log.md` records a mechanism as **UNVERIFIED**, this file repeats
  the UNVERIFIED and does not upgrade it into narrative.
- Append-only. New history goes at the end of §3 with its own source line.

---

## 1. The idea the machine is built on

> Safety does not depend on tools being programmed correctly; it depends on the
> execution boundary refusing at the point of action.
> — `docs/host-port.md` §1

That sentence is the whole thesis, and everything in §3 is a generation learning it the
hard way. Before it, every guarantee about what a tool could reach depended on that
tool's author having remembered to check. A gate that each tool is trusted to consult is
not a gate; it is a convention with a security claim stapled to it.

The thesis decomposes into three obligations, and the machine now tries to keep all
three:

1. **Resolve authority once, and report the resolution.** The effective grant is a
   computed value with a named owner, not whatever the nearest layer happened to say
   (`security/security_gate.py`, `thoughtmachine/timebudget.py`).
2. **Make one chokepoint the only way through.** Not "every access goes through it" —
   "every site is either through it or enumerated as a named exception, never simply
   unaudited" (`docs/host-port.md` §1, `current_map.md`).
3. **Name the layer that denies, at the moment of denial.** A refusal that blames the
   wrong layer teaches the reader a false model of their own machine (§3.4).

The vision half of this file (§4) is what those obligations are *for*: an operator who
can delegate real work without auditing every tool call, and a machine whose memory and
state survive the seat that wrote them.

---

## 2. The machine in one page

Only the facts the history below keeps touching.

- **Four regions** (`docs/ARCHITECTURE_MACHINE_MODEL.md` §1). Two matter for the
  boundary: workers are **threads inside the server process**
  (`WorkerThread(threading.Thread)` in `tools/workspace/worker_thread.py`), and
  `host_bash` is the **only** path that executes on the host.
- **Three gates, multiplied, not substituted** (`docs/ARCHITECTURE_MACHINE_MODEL.md` §2):
  the per-workspace kill switch `allow_host_resources` (read by
  `tools/host_resource_policy.py`), the workspace ceiling, and the session grant, with
  `effective = min(session_grant, ceiling)`. A fourth gate was removed in `4e96c32c`
  (PR #137) rather than added to.
- **Lifecycle classes** (`thoughtmachine/container_record/models.py`): `ephemeral`,
  `persistent`, `resource`, `service`. `resource` and `service` **own their lifecycle**
  and are gated fail-closed in `infra/container_manager.py`; `ephemeral` is created for a
  request and dies on return; `persistent` is workspace-scoped and shared across
  sessions. `docs/ARCHITECTURE_MACHINE_MODEL.md` §3 documents the first three of these as
  the three container types an agent sees.
- **Three git commit paths with three behaviours** (`docs/ARCHITECTURE_MACHINE_MODEL.md`
  §9b): container (hooks honoured), host tool (hooks neutralised by construction), bare
  shell (the host's hooks fire, because `/workspace/.git` is a gitfile pointing into the
  host worktree). Which path you are on changes what your commit means.
- **One clock** (`thoughtmachine/timebudget.py`): `requested` / `effective` / `cap` /
  `clamped` / `clamped_by` / `source`, one declared ceiling of 600 s.

---

## 3. The history, in the order the boundary was found

### 3.1 Gen 48 — the laundering hop: a tool that ran other tools

**What it was.** `PaginateTool` declared `required_categories = ["filesystem:read"]` and
then accepted a tool *name*, instantiated the class from the global `tools.TOOL_CLASSES`
registry, and ran it — with no second check. The gate evaluated the top-level tool's
declared categories (`agent/core/tool_executor.py:419` → `:468-469`), so a profile that
was allowed only `filesystem: read` could run whatever it named.

**Why it matters.** It was not a category bypass. It was **authority laundering**: a tool
with a cheap declared cost fronting for a tool with an expensive real one. Red-team
evidence pre-fix (`personal/handoffs.md`): a stock filesystem-only profile really did
construct `ContainerRemoveTool` and `HostBashTool`. The same helper also dropped
`workspace_id` from the context it forwarded — a containment and attribution defect in
one, reachable from workers.

**Three attempts, because the first two were patches.**

| Attempt | What it did | Why it was not enough |
|---|---|---|
| `8aa17e0d` | contain `_CONTEXT_FIELDS` | the field was written as a list of paths, and `workspace_id` is not a path |
| `0c125856` | re-gate inside `execute()`, `_NEVER_FORWARD = ('workspace_id',)`, `_NEVER_PAGINATE` blocklist | a re-gate plus a blocklist is "Defect 1 with a seatbelt"; the pattern survives wherever it is copied |
| `4a4bd692` | delete the class — 223 lines and 18 tests retired | — |

**The doctrine it produced.** *Tools are leaves, not nodes.* No tool forwards to another
tool class; there is no "safe wrapper", because a safe wrapper is still a wrapper and the
class survives wherever the pattern is copied. The registry entry is now a pointer
comment naming the class rule `hygiene/no-tool-wrapping`. Filed as
`working_docs/ticket-bug-paginate-tool-bypasses-required-categories.md` (2026-10-23);
`MANDATORY_STOP.md` items 1 and 1a; `hall_of_fame.md` "The Death of PaginateTool
(Gen 48)".

**The naming lesson.** The fix was briefed as "category bypass". The read found two
different defects — authority laundering and host access
(`working_agreements.md`). A brief names the shape; only the read names the mechanism.

### 3.2 Gen 49 — the host port: one chokepoint, checked at the action

**What it is.** `thoughtmachine/host_port.py` — `host_run`, `host_run_async`,
`HostRunResult`, `HostRunHandle`. Every command leaving the sandbox for the host passes
through one function, so the kill switch is checked **at the point of action** instead of
at the point where each tool's author remembered to write it. It is the only module in
`thoughtmachine/` and `tools/` allowed to import `subprocess` without a pinned exception;
every other importer is on the scan's exception list with a named reason.

**The guarantee, stated honestly.** Not "every site is through the port" but "every site
is either through the chokepoint or enumerated in `ALLOWED_EXCEPTIONS` with a named
reason. The pinning is the win; the migration is the follow-up" (`docs/host-port.md` §1).

**The two qualifications that belong in the headline, not a footnote.**

1. **CI enforces the convention in the honest case only.** `tests/test_tool_import_declaration_match.py`
   is an AST scan that catches four static import forms. It cannot catch reach built at
   runtime (`getattr(subprocess, "run")`, `importlib.import_module("subprocess")`).
2. **The port checks authority, not intent.** A caller who legitimately holds
   `host_bash:allow` and asks for something harmful is inside the envelope; review and
   the red team are the controls for that shape.

**The doctrine.** `current_map.md` — a site is either through the port or named as an
exception, never simply unaudited. `docs/host-port.md` §9.3 is the named-exclusion list
(git's `_exec_host_raw`, container git); §9.1/§9.2 keep the port's own audit trail
separate from the tool's.

### 3.3 The git hole: three commit paths, three behaviours

**What happened.** The same repository hook was live on one commit path and dormant on
another. Container commits set `-c core.hooksPath=/workspace/.githooks` explicitly, so
the hook ran. From a bare shell, `core.hooksPath` read as unset, so someone concluded the
hook was dormant and deleted `.githooks/pre-commit` in `85035605`.

**Why it matters.** "One shell's default is not a fact about the machine"
(`MANDATORY_STOP.md`; `docs/ARCHITECTURE_MACHINE_MODEL.md` §9b). The failure mode is not
a missing hook; it is code you did not intend to run, running because of where you stood.
The same asymmetry is why a tool commit can time out with the hook still running, paths
staged and `HEAD` unmoved — **a commit timeout is not a pass**.

**What the machine does now.** The host path neutralises hooks on every invocation
(`core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`, `diff.external=`,
`core.fsmonitor=`, `filter.clean=`, `filter.smudge=`, `--no-verify`), and host mode
**refuses** a commit when `<workspace>/.githooks/pre-commit` exists: *"refusing commit:
repository hooks are configured … Use container mode, or remove the configured hooks to
accept the bypass."* The rule is fail-closed only when hooks are configured **but
unenforceable** — not a blanket ban. Pinned by `tests/security/test_git_hardening.py`;
`MANDATORY_STOP.md` items on `--no-verify` and on never moving the host hook.

**What is NOT established here.** `reliability_log.md` row 3 records, from the operator's
"scared me twice" red-team round, that **git was usable to reach beyond the intended
boundary** — with the mechanism marked **UNVERIFIED in the KB**. That row is the reason
the git surface is treated as a boundary; it is *not* evidence that a repository hook
executed arbitrary code, and this file does not turn it into that. The hook asymmetry is
a separate, fully documented defect with a commit and a test.

### 3.4 Attribution at the point of denial

**What it was.** A denied call reported `but session allows host_bash:banned` — a clause
that contradicted itself, and that blamed the session grant even when a workspace layer
had forced the value. The denial message is the only part of the gate most readers ever
see, so a wrong denier is a wrong model of the machine.

**What it is now.** `security/security_gate.py:_denial_clause` names the layer that
actually refuses: `session grant`, `workspace ceiling (<level>)`, or
`workspace host-resource policy`, with precedence following resolution order
(`_denial_layer_label`). When the whole profile was forced onto the deny-all fail-closed
shape, the caller's trailing `(fail-closed: <cause>)` already names the layer, so the
`(denied by: …)` suffix is suppressed rather than duplicated. Non-refusal grants keep the
historical clause byte-identical. Pinned by `tests/security/test_denial_layer_attribution.py`.

**The doctrine.** A refusal must be attributable to the layer that made it. This is the
same obligation as §3.2's "checked at the point of action", applied to the *message*
instead of the *check*.

### 3.5 One clock — and the part still open

**What it was.** *Timeout independence* (`thoughtmachine/timebudget.py` docstring): the
same logical budget resolved in at least four places with four different numbers, so a
caller asking for 420 s of worker time got 300 s of wait, the worker's own loop timed out
on a third number, and nothing in the envelope said any of it had happened.

**What it is now.** One `TimeBudget`: `requested`, `effective`, `cap` (`DECLARED_CEILING`
= 600 s, owner `agent.config.defaults.HARD_TIMEOUT`), `clamped`, `clamped_by`, `source`.
A clamp is never silent; `strict=True` fails at dispatch instead of quietly reducing the
number. The resolution is dependency-free on purpose so every layer can read one ceiling
instead of inventing one.

**What is still open — C61 (`PRIORITIES.md`).** Two measured defects in the same seam:
the budget stops **new** work and never an in-flight call (a `sleep(200)` dispatched under
a 60 s budget completed in **205.63 s**, `timed_out: false`, exit 0), and the caller's
dispatch window is ~**2×** the effective budget (60 → blocked 120.0 s; 240 → 300 s). Fix
direction is process-level cancellation — SIGTERM, grace, SIGKILL, reap, return partial
stdout with `cancelled_in_flight` — not a longer in-loop check. Read the C-item before
touching this seam.

### 3.6 Memory: the router is generated, and the memory is the constraint

**What it was.** The entry document was hand-written and drifted into invention: at the
Gen 62 correction, `PRIORITIES.md` contained neither C25 nor "Phase 10", both of which
the handoff asserted (`scripts/gen_handoff.py` "Carried traps").

**What it is now.** `HANDOFF.md` is generated ("do not hand-edit"). Section 2 is the
manifest `scripts/build_onboarding.sh` parses and `scripts/check_kb_staleness.sh`
verifies; most entries record a `sha256` of the file's bytes as a merge-time tripwire.
`PRIORITIES.md` is deliberately listed **first and without a digest**: it is the live
queue, edited by every branch that opens, closes or re-words a C-item, so its digest is
branch-local by definition and hard-recording it would teach everyone to hand-edit the
router. `tests/test_onboarding_bundle_contract.py` pins the whole thing: regenerated
bytes, the digests, the member order in the bundle.

**What is still open.**

- **C35** — a worker's own context can hit the ceiling and be force-summarised mid-turn;
  a payload held only in its memory is then *gone*, and the envelope still returns
  `status: "final"`. Mitigation in use: park long verbatim payloads in a `WorkingDocument`
  or a workspace file so a summarised worker can re-read them. Dispatch-integrity
  violation, not fixed.
- **C36** — `personal/bugs_and_fixes.md` is ~81,000 tokens: reading it costs an entire
  context window, so the bug log has become write-only.

**An unrecorded derivation (arithmetic, not a measurement).** No repo file performs this
sum, so treat it as analysis: if a session's transcript grows by `k` units per turn and
every force-summarisation must read the whole transcript so far, then after `N` turns
with summarisation every `k` turns the total tokens spent on memory upkeep are
`Σ_{i=1..N/k} i·k = O(N²/k)` — quadratic in session length, with the constant set by how
often summarisation fires. The recorded facts it explains are C35 and C36; the derivation
itself is not in the KB. The onboarding budget in `scripts/gen_handoff.py` ("100–150 kB in
one to three massive chunks") is the same constraint seen from the other end: the bundle's
size is a governed quantity because every reader pays it.

---

## 4. The vision: `vision_ledger.md` in one page

V-items are properties of the whole system as the operator experiences it. Each has been
violated at least once, visibly and only after the fact. Verdicts below are as recorded in
`vision_ledger.md`; only a measured check changes a verdict.

| | Commitment | Verdict on record |
|---|---|---|
| **V1** | **Dispatch integrity** — what is handed to a worker is exactly what it receives, and what it returns is exactly what is read. No silent truncation, no status contradicting its payload. | **BROKEN** (Gen 60/61) — dropped `context["meta"]`, truncated inline queries, `timeout` beside correct content |
| **V2** | **Process seams are verifiable, not trustworthy** — any cross-boundary claim is reproducible from an artifact: which tree, which layer, which boolean. | **PARTIAL** (Gen 61) — worker git access varies; worker `host_bash` fails closed; vault unreachable |
| **V3** | **No lying renderer** — every number and empty state names the subset it counts; "could not load" is never "nothing there". | **SATISFIED** as of `fbc563d5` |
| **V4** | **Destructive action is reversible or preceded by an inspectable dry run.** | **PARTIAL**, satisfied everywhere it is armed (detach-not-kill, operator-only kill, registry guards) |
| **V5** | **The machine's memory survives the machine** — a file you are told to read on entry is in the repository. | **PARTIAL**, corrected in the Gen 61 commit that added the negation lines |

Read downward and the sections above become one story: V1 is §3.5's envelope work, V2 is
§3.2–§3.4 (the check, the chokepoint, the attribution), V3 is what the UI owes the same
honesty, V4 is what the container lifecycle classes exist to make possible, and V5 is §3.6
plus every `.gitignore` negation line.

---

## 5. How to use this file

- **Before proposing a fix**, find the incident. If the rule you are about to bend came
  from §3, the burden is on the proposal to explain why the old failure cannot recur —
  §3.1 is the worked example of two plausible patches that left the class intact.
- **When you write history, cite.** A file and section, or a commit. `reliability_log.md`
  keeps UNVERIFIED markers for mechanisms nobody has demonstrated; do not launder one
  into narrative here.
- **When you find that a rule is wrong**, say which incident it came from and what the
  new evidence is. Rules in this machine are load-bearing because they were expensive,
  not because they are old.

**If you read only one section:** §1 (the thesis) and §3.1 (what happens when a tool is
allowed to be a node instead of a leaf).
