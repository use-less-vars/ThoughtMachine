# Contracts — the invariants this machine guarantees

This ledger names the invariants the machine actually guarantees, each with the exact code site that
enforces it and a test that would FAIL if that enforcement were removed. Every test it names is run by
the fast contract gate (`.github/workflows/contract-gate.yml`), which runs more than it names: the gate
enumerates eighteen test files today and this ledger cites ten of them, because
`tests/test_contracts_ledger_parity.py` enforces *cited ⊆ run* rather than equality, and the uncited
extras are gate-only additions explained in that workflow's own header. Parity with the other ledger —
the invariant list in `.thoughtmachine/knowledge/project/architecture_invariants.md` — is enforced by
`tests/test_invariants_ledger_parity.py`, so every numbered invariant there must name a contract here
or an explicit gap. It does not claim the wider suite is worthless — it claims that these three
invariants are the merge-blocking guarantees: a change that breaks one of them must not reach `dev`,
and a change that breaks anything else is still caught by the main matrix.

Verdict vocabulary: **ENFORCED** — the code site enforces it and a named test fails on removal.
**PARTIAL** — enforced, but at least one part of the invariant is not pinned by the tests named here.
**GAP** — a part of the invariant has no detecting test at all.

## Contract 1: The permission boundary — a caller-supplied session id is unrepresentable

**Invariant:** No tool's model-visible JSON schema advertises `session_id`, and a `session_id` supplied
by the caller (the model hallucinating one, or an attacker injecting one into the arguments) never
reaches the tool as an authority fact.

**Enforcement site:** `tools/base.py:85-88` — `ToolBase.session_id` (with the C68 comment at
`tools/base.py:77-84`), the reader that consumes the fact at `tools/base.py:308-313`
(`session_id = getattr(self, 'session_id', None)`, then a `SessionRegistry` lookup), and the authority
pair in `agent/core/tool_executor.py` that makes a caller-supplied value unreachable: the discard at
`agent/core/tool_executor.py:489` (`tool_args.pop('session_id', None)`, before validation) and the
injection at `agent/core/tool_executor.py:798-800` (`tool_args['session_id'] = session_id`, from the
executor's own authenticated value):

```python
    session_id: SkipJsonSchema[Optional[str]] = Field(
        default=None,
        exclude=True,
        description="Session ID injected by ToolExecutor",
    )
```

`SkipJsonSchema` removes the field from `model_json_schema()` and therefore from
`model_to_openai_tool`; the value is an authority fact injected by `ToolExecutor` (popped out of the
caller's arguments before validation), never an argument.

**Mutation-proof test:** `tests/test_worker_security.py` — `120 assert "session_id" not in properties,
sorted(properties)` (replacing `SkipJsonSchema` with plain `Optional[str]` makes the field reappear in
the schema and this assertion fail), plus `84 assert recorded["session_id"] == AUTHENTICATED, recorded`
and `92 assert recorded["session_id"] != CALLER, recorded` (injection precedence). Also
`tests/tools/test_tool_schemas.py` — `74 assert worker.session_id == "sess-injected"` and
`80 assert "session_id" not in dumped` (the `exclude=True` half).

**Verdict:** ENFORCED — both halves are asserted: the field cannot be advertised, and a caller value
cannot win.

Note: the design brief's tests/test_identity_injection.py does not exist in this tree, so no such file
is cited here; the property is covered by the two real files above.

## Contract 2: The trust boundary fails closed

**Invariant:** When the permission store cannot be read (missing, corrupt, or raising), the effective
permission profile is the deny-all shape rather than a permissive default; and every allow/deny
decision denies on an unknown category, on a value the session does not grant, and on ASK with no
interactive approver.

**Enforcement site:** `security/security_gate.py:1104` — `get_effective_permissions`, whose disk-mode
exception handlers force the deny-all sentinels (`1210-1212` for the unreadable-store handler, its
unreadable-grant twin at `1240-1241`, and the unusable-ceiling handler at `1276-1293`, which denies
rather than keeping an un-capped, fail-open session):

```python
            fail_closed_reason = f"{type(exc).__name__}: {exc}"
            session = _DISK_FAIL_CLOSED_SESSION
            workspace_permissions = _DISK_FAIL_CLOSED_CEILING
```

plus the allow/deny deciders `check_required_categories` (`security/security_gate.py:1713-1894`),
`check_atomic_operation` (`security/security_gate.py:1605-1640`) and `check_requires_resource`
(`security/security_gate.py:1643-1710`):

```python
        allowed = effective.get(category)

        if allowed is None:
            return False, f"Permission denied: Unknown category '{category}' required by tool."

        result = _value_satisfies(required_value, allowed)

        if result is False:
```

`_denial_layer_label` (`security/security_gate.py:723-735`) is NOT an enforcement point: it returns
denial-message text and is called only from `_denial_clause` (`security/security_gate.py:738`, the
call at `:762`). It can
neither allow nor deny a call and must not be cited as the trust boundary.

**Value direction — which way an unusable ceiling fails.** `apply_workspace_ceiling`
(`security/security_gate.py:281`) answers two questions that are easy to conflate. An unknown ceiling
**value** on a KNOWN resource — a level token outside that resource's vocabulary, or a value of the
wrong type for it — is fail-**CLOSED**: it is logged and the grain is denied outright (`banned`, or
`False` for the boolean `container` grain) via `_ceiling_fail_closed_value`
(`security/security_gate.py:272-278`, used at `:457`, `:491` and `:504`), so a typo in a security
ceiling can never silently mean "no restriction". An unknown ceiling **resource** key stays
fail-**OPEN** (warn and ignore, `:355-362`), because a workspace map written by a newer build must not
break session resolution; the *mapping-missing* case is no longer silent either (`:363-377`, see
Known gap 6). Unreadable on-disk configuration is fail-closed — Contract 2's first half.

**Precedence inside the cap.** The evaluation order in `apply_workspace_ceiling` is part of the
invariant, not an implementation detail: (1) unknown resource key → warn, ignore (`:355-362`); (2) a
recognised resource with no session-side mapping → warn, skip (`:363-377`); (3) `host_bash` is
intercepted onto its own scale `banned < ask < allow` before generic rank normalisation (`:390-433`),
so an `ask` ceiling over an `allow` grant caps to `banned` instead of fabricating a prompt level;
(4) legacy spellings are translated by `_CEILING_VALUE_ALIASES` (`security/security_gate.py:263-266`)
subject to a two-way legality guard — translated only when the raw token is NOT already legal *and*
the target IS (`:471-479`) — so `full` caps to `write` on `git`/`filesystem`/`network` while a legal
`full` on `mcp` is untouched; (5) vocabulary membership, else fail closed (`:480-492`); (6)
`_WORKSPACE_CEILING_LEVELS` rank lookup, else fail closed (`:496-505`); (7) rank ≥ 4.0 is unlimited
(`:506-507`); (8) otherwise the more restrictive rank wins (`:509-535`).

**Mutation-proof test:** `tests/test_security_gate_disk.py` for the fail-closed source —
`139`, `156`, `174`, `189`, `228`, `341` and `399` each assert `eff == ALL_BANNED` for missing session
record, corrupt sidecar, missing config, corrupt config, present-config-without-`permissions`,
schema-invalid ceiling and the unknown-I/O policy; `85 assert eff["filesystem"] == "read"  # capped by
ceiling` dies if the ceiling cap is dropped; `258 assert disk == legacy` pins disk/legacy parity;
`404 assert getattr(eff, "_fail_closed_reason", "") != ""` dies if the reason is not recorded. For the
deciders, `tests/trust/test_gate_contract.py` — `601 assert ok is True, ...`, `613 assert ok2 is False,
msg2` and `614 assert "denied" in msg2.lower()` (a worker footprint narrowing write→read must deny the
write; returning `True` or a non-denial message fails). For the ceiling direction, three files:
`tests/test_ceiling_value_fail_closed.py` — `27 assert result == {"filesystem": "banned"}` (an
unknown level denies instead of leaving the grant standing), `51 assert result == {"filesystem":
"write"}` (an unknown *resource* still fails open), `92`/`98` (a stray `full` caps to `write` where
that level is legal) and `115` (a legal `full` on `mcp` is untouched); loosening any of them back to
fail-open turns exactly those assertions red. `tests/test_ceiling_map_completeness.py` — `25 assert
missing == []` (every recognised ceiling resource has a session mapping) and `48 assert
apply_workspace_ceiling({"ci": "banned"}, {"ci": "read"}) == {"ci": "banned"}` (the historical `ci`
no-op is closed). `tests/test_permission_hierarchy.py` — `208 assert got == _expected(grain, session,
ceiling)` across the full grain × session × ceiling matrix, `255 assert got == "banned"` (a denied
ceiling beats the most permissive session), `272 assert got == "banned", "an ask ceiling must never
emit a prompt level"` (host_bash) and `274 assert got == "ask"` (the ask-legal grains, where the ask
ceiling resolves to the ceiling level itself).

**Verdict:** ENFORCED for `get_effective_permissions` and for `check_required_categories` as exercised
by those two files (verified in this tree: 11 passed and 43 passed respectively), and now also for the
ceiling direction itself — the three ceiling files fail if an unknown ceiling value is loosened back
to fail-open, if any catalog resource loses its session mapping, or if the grain × session × ceiling
matrix drifts. `check_atomic_operation`
and `check_requires_resource` are not imported by either cited file — see Known gaps.

## Contract 3: Worker process isolation

**Invariant:** With `WORKER_RUNTIME_MODE` absent, the resolved worker runtime mode is
`DEFAULT_RUNTIME_MODE = MODE_PROCESS`, and process mode is answered by a real OS child process, not by
the calling interpreter.

**Enforcement site:** `tools/workspace/worker_runtime.py:90` (`DEFAULT_RUNTIME_MODE = MODE_PROCESS`,
resolver at `tools/workspace/worker_runtime.py:357`) and the substrate in
`tools/workspace/worker_runtime_process.py:486-495`:

```python
        process = context.Process(
            target=worker_runtime_main,
            args=(child_sock, self._spec),
            kwargs={"worker_factory": self._worker_factory},
            name=(
                f"worker-runtime-{self._spec['worker']}-{self._spec['instance']}"
            ),
            daemon=False,
        )
        process.start()
```

**Mutation-proof test:** `tests/test_worker_is_a_real_process.py` — `109 assert reply_pid != parent_pid`
and `115 assert child_process_exists` for the honest handle, mirrored by `123 assert reply_pid ==
parent_pid` / `128 assert not child_process_exists` so the branch cannot pick the vacuous side;
`258 assert not _owns_a_real_child_process(handle)` and `269 assert "process" in str(excinfo.value)`
reject a handle that declares `process` but answers in-process; `169 assert result.get("spawned") is
True` dies if the production spawn path stops building a handle; `157` asserts the env flag is really
absent, so the default is the thing under test; and `343 assert DEFAULT_RUNTIME_MODE == "process"` /
`349 assert resolve_runtime_mode({}) == "process"` spell the default out as a literal, so a one-token
flip of the constant is a red test rather than a green one.

**Mutation gate:** the claim is not left to a human reading `assert` lines. `scripts/mutation_gate.py`
is pointed at `tests/mutation_gate/specs.py`, which declares one mutation for this contract:
`contract-3-default-mode` — the exact substring `DEFAULT_RUNTIME_MODE = MODE_PROCESS` rewritten to
`DEFAULT_RUNTIME_MODE = MODE_THREAD` in `tools/workspace/worker_runtime.py` — with
tests/test_worker_is_a_real_process.py as the file whose greenness the contract stands on:

```text
MUTATION contract-3-default-mode: KILLED
GATE_SUMMARY declared=1 ran=1 killed=1 survived=0 refused=0 restore_failed=0 exit=0
```

The verdict flip is the evidence: before the literal-pinning test existed, the same declaration came
back SURVIVED (exit 1) with all three named tests green — Known gap 1 reproduced by the gate itself —
and KILLED afterwards (the gate also refuses an anchor that is absent or ambiguous, a node id it
cannot collect, and a red baseline, so SURVIVED and KILLED are both earned).

**Verdict:** ENFORCED — the substrate law ("the mode is a fact about where the code runs") is
mutation-proven, and the literal default is now pinned instead of compared against itself: the two
remaining self-comparisons at `297 assert handle.mode == DEFAULT_RUNTIME_MODE` and `304 assert
resolve_runtime_mode({}) == DEFAULT_RUNTIME_MODE` were true and stayed true across the flip, which is
exactly why `343` / `349` assert the value outright. Scope, stated honestly: the gate is a script
(`python3 scripts/mutation_gate.py`) that a human or a job runs — it is NOT yet wired into CI, so
Contract 3 is enforced wherever that command runs and never by `pytest` alone. Wiring it in is the
next step, not a property of this commit.

## Contract 5: An unreadable permission is UNKNOWN, never assurance

**Invariant:** When a permission value cannot be read, every surface renders it as unknown. A failed
read must not become a claim in either direction — not "host resources are forbidden, this workspace
can withstand untrusted content", and not a purpose preset published as the agent's effective grant. It
must not change the SHAPE of a permission table either: an empty panel and an em-dash both make no
claim, while a table resized out of synthesised unknowable rows is still a fabricated surface.

**Enforcement sites:** `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:583` —
`const hostResourcesKnown = typeof summary?.allow_host_resources === 'boolean'`; both the security-posture
string and the host-execution card are gated on it, and the unknown branch carries its own
`wdp-posture-unknown` class instead of the green `-safe` or amber `-host` colour that would read as a
verdict. `web_ui/frontend/src/store/workspaceStore.js:416` — `permissions = []` in the
`fetchWorkspaceConfig` else-branch; the `else if (purpose)` fallback that published
`purpose.defaults.permissions` as `effective` (with a ceiling fabricated from `PERMISSION_CEILINGS`) is
deleted, and the truthful branch keeps
`ceiling: Object.prototype.hasOwnProperty.call(wc, name) ? wc[name] : undefined` so an absent ceiling
stays undefined and the panel renders its em-dash.

**Mutation-proof tests:** `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.unknownPosture.test.jsx`
— three cases: `allow_host_resources` absent, `allow_host_resources: null`, and a failed summary fetch.
The first two assert `queryByText(POSTURE_SAFE)` and `queryByText(POSTURE_HOST)` are both null and that
the unknown string renders with `wdp-posture-unknown`, so neither side of the branch is vacuous: restoring
"unknown → the forbidden string" turns both red (measured by hand, 2 failed). The third case dies if a
failed fetch ever starts rendering a posture claim. `web_ui/frontend/src/store/__tests__/workspaceStore.unknownEffective.test.jsx`
— an HTTP 500 and a throwing fetch, each asserted at the store (no permission row carries a defined
`effective` or `ceiling`) and at the rendered sidebar (the empty state appears and none of the preset's own
values `write`/`banned`/`ask`/`read` appears anywhere). Restoring the preset fallback turns both red
(measured by hand, 2 failed).

**Verdict:** ENFORCED for these two surfaces, BY MANUAL MUTATION — NOT BY A GATE. This tree carries no
mutation-testing configuration at all: no Stryker config, no `mutation` script in
`web_ui/frontend/package.json`, no mutmut or cosmic-ray. The two mutation proofs recorded in this
contract's commit message are hand measurements, not a CI check, and nothing will re-run them.
`.github/workflows/contract-gate.yml` is pytest-only over a hand-enumerated list of eighteen files under
`tests/`, so it cannot run these JS suites — which is also why the JS paths above are cited without the
backticked repo-relative form that `tests/test_contracts_ledger_parity.py` parses. What does run them is
`platform-matrix.yml`'s `web-ui-frontend-suite` job (vitest over `src`), which cannot detect a mutation.

**Deferred — unreadable-value lies this contract does not yet cover. Each is a named follow-up, not a
pass:**

1. `web_ui/frontend/src/components/SessionWorkspaceTab.jsx` — the capability pills are labelled as
   *merged* capabilities, so a pill cannot say which resource granted it; a merged value reads as a
   direct one.
2. `web_ui/frontend/src/data/permissionVocab.js` — `effectivePermissionLabel('ask')` prints a
   deferral ("ask") inside the column that reports an outcome, so a question is rendered as an answer.
3. `web_ui/frontend/src/components/ConfigPanel.jsx` — `if (!sessionPerms) return;` makes a save a
   silent no-op: nothing is written and nothing is said, so an unreadable permission surface produces a
   success-shaped silence.
4. `web_ui/frontend/src/store/workspaceStore.js:457` with
   `web_ui/frontend/src/components/SessionSidebar.jsx:340` — a container read that failed (`tryGet`
   returns null) becomes `[]`, and an HTTP 200 empty payload renders identically: "No containers for this
   workspace." An unreadable set and an empty set are the same pixels.

## Known gaps

1. **Contract 3 — the literal default is unproven (CLOSED).** No test in the tree asserted
   `DEFAULT_RUNTIME_MODE == "process"`; every reference compared against the constant — in the cited
   file at `297`/`304`, and in tests/test_worker_runtime_protocol.py at `840-842`, `864`, `1007-1008`
   and tests/test_worker_runtime_seam.py at `242` — so flipping the constant to `MODE_THREAD` was a
   one-token change the entire named set would still pass. The literal is now asserted outright at
   `tests/test_worker_is_a_real_process.py:343` and `:349`, and the flip is a declared mutation
   (`contract-3-default-mode`) that `scripts/mutation_gate.py` reports KILLED. The other files named
   here are still comparisons against the constant and still would not notice the flip; what stops
   them from mattering is that one literal, which is the file the mutation declaration names. What
   remains open is automation, not the claim — the gate is not yet wired into CI. Contract 3 is cited
   as ENFORCED for exactly this reason.
2. **Contract 2 — two of the three deciders are unproven by anything named here (GAP).**
   `tests/trust/test_gate_contract.py` imports only `check_required_categories` (its line 25); neither
   `check_atomic_operation` (`security/security_gate.py:1605-1640`) nor `check_requires_resource`
   (`security/security_gate.py:1643-1710`) is imported or called by `tests/test_security_gate_disk.py`
   or `tests/trust/test_gate_contract.py`. Their `ASK`-is-denied, unknown-resource and
   malformed-`operation_required` branches have no detecting test in this ledger.
3. **Contract 2 — `_denial_layer_label` is untested (GAP, cosmetic).** Correctly not cited as
   enforcement; it has no test at all, so a regression there is a wrong *message*, never a wrong
   decision.
4. **Contract 2 — the ceiling-value fail-OPEN asymmetry (CLOSED).**
   An unknown workspace-ceiling *value* used to be fail-open in `apply_workspace_ceiling`; it is now
   fail-CLOSED (vocabulary guard `security/security_gate.py:480-492`, rank guard `:496-505`, deny
   value `_ceiling_fail_closed_value` `:272-278`) and pinned by `tests/test_ceiling_value_fail_closed.py`
   and `tests/test_permission_hierarchy.py`, both cited above and both run by the gate. Only the
   unknown-*resource* side stays fail-open, and that is a deliberate forward-compatibility choice,
   not a lingering asymmetry.
5. **Contract 1 — coverage is split across two files, and the cited name from the brief is absent.**
   Schema advertising is asserted in `tests/test_worker_security.py`, `model_dump` exclusion in
   `tests/tools/test_tool_schemas.py`. A future refactor that moves the field to a new base class could
   drop one half without the other noticing.
6. **Contract 2 — the `ci` ceiling was a silent no-op (CLOSED).** `ci` was a recognised ceiling
   resource with no entry in `_WORKSPACE_RESOURCE_MAP`, so a configured `ci` ceiling applied nothing
   and logged nothing. The mapping now exists (`security/security_gate.py:221`), a mapping-less
   resource warns instead of skipping silently (`security/security_gate.py:363-377`), and
   `tests/test_ceiling_map_completeness.py` fails if any catalog resource loses its mapping.
7. **The design spec is not citation-checked (GAP, tracked).** Every citation in this ledger is now
   machine-checked: the gate runs `scripts/verify_doc_citations.py --doc docs/CONTRACTS.md`. The
   narrative design spec `docs/design-worker-lifecycle.md` is not yet checked, and it has 76 failing
   citations today — 1 missing file (a backticked negative mention the parser reads as a citation), 20
   out-of-bounds line numbers and 55 shorthand references that name no file in their own section. It is
   therefore a tracked known gap rather than a silent omission, and the checker already accepts
   `--doc docs/design-worker-lifecycle.md` for whoever closes it.

## How this document stays honest

Every test path in this document is written inside backticks and is parsed by
`tests/test_contracts_ledger_parity.py`, which fails if a named path does not exist on disk, and fails
if a named path is not present in the pytest invocation of `.github/workflows/contract-gate.yml`. That
is what stops this ledger from rotting into a list of files that moved or that the gate no longer runs.
Deliberately, a path mentioned as *non-existent* (see the note under Contract 1) is written without
backticks, so the parser cannot mistake a negative mention for a citation.

## What CI actually runs — and what this document does not claim

Added by the onboarding documentation pass. Every line below was read off the six workflow files under
`.github/workflows/` in the working tree — not from a CI run and not from the GitHub API, since no
`gh` call and no `git fetch` was possible in the environment where this section was written.

Eighteen jobs across six workflows. **Gating** here means one thing only: *a failure fails that
workflow's run*. That is a statement about the workflow file, not about branch protection — see the
open item below.

| Workflow file | Jobs | Gating | Advisory (`continue-on-error: true`) |
| --- | --- | --- | --- |
| `.github/workflows/contract-gate.yml` — "Contract Gate" | `contract-gate` | 1 of 1 | — |
| `.github/workflows/cross-platform-smoke.yml` — "Cross-Platform Smoke" | `linux-install`, `windows-smoke`, `macos-launcher` | 3 of 3 | — |
| `.github/workflows/first_start_ci.yml` — "First-Start CI" | `first-start` | 1 of 1 | — |
| `.github/workflows/platform-matrix.yml` — "Platform Matrix" | `ubuntu-suite`, `linux-install-smoke`, `linux-e2e-auth`, `windows-import-smoke`, `windows-thin-pytest`, `web-ui-backend-suite`, `web-ui-frontend-suite` | 7 of 7 | — |
| `.github/workflows/platform-matrix-macos.yml` — "Platform Matrix (macOS, advisory)" | `macos-suite`, `macos-install-smoke`, `macos-docker-probe` | 0 of 3 | all three |
| `.github/workflows/python-version-matrix.yml` — "Python Version Matrix" | `unit-tests` (matrix over `python-version`), `installer-contract`, `windows-python-314` | 3 of 3 | — |

What the trigger blocks and job names add to that table:

- `.github/workflows/contract-gate.yml` runs on `pull_request` targeting `dev` and on
  `workflow_dispatch`. **It has no `push` trigger**, so a direct push to `dev` does not run the
  contract gate, while it does run the other five workflows. Those five run on `push` to `dev` and
  `master` and on `pull_request`.
- `contract-gate` is the only workflow with any relationship to this document, and that relationship is
  manual. It runs `python -m pytest -q -p no:cacheprovider` over a **hand-enumerated list of eighteen
  test files** — no glob, no `-m` expression. Two consequences worth knowing: a new test that proves a
  contract does not join the gate by existing (it must be added to the workflow *and* cited here), and
  the parity check described in the previous section is itself one of the eighteen, so it fails the
  gate it is part of. Note the direction: the gate runs eighteen files and this ledger cites ten of
  them, because the parity check enforces *cited ⊆ run*, not equality. The job carries
  `timeout-minutes: 10` and separately reports wall time against a stated 120-second budget; that
  budget is observed, not enforced.
- `platform-matrix.yml`'s `linux-e2e-auth` job skips itself unless auth-relevant files changed, and the
  change detection is written to *run* rather than skip when it cannot decide. A skip in that job is
  therefore not evidence that the auth path was exercised.
- One advisory surface, visible in the workflow text: `.github/workflows/platform-matrix-macos.yml`
  labels itself "(macOS, advisory)" and all three of its jobs carry `continue-on-error: true`. Those
  three of the eighteen jobs cannot fail their run. `.github/workflows/platform-matrix.yml`'s
  `web-ui-backend-suite` job was the fourth until C91 made it BLOCKING (no `continue-on-error`, and
  its name no longer carries the "(advisory)" suffix).

**RESOLVED — measured, and the stored count is not the in-tree count.** The operator read the live
`dev` ruleset with admin credentials on 2026-11-19: it stores **twelve** required-check entries, of
which **eleven** are unique check contexts — `Unit tests - Python 3.12` is stored twice — and
**Contract Gate is among the required set**. This is no longer an attribution: it is re-derivable by
anyone with `gh`, which is what makes it a measurement. That supersedes the count this section
previously carried. The eighteen contexts enumerated from the tree
(`scripts/apply_branch_protection.sh:78-96`, `docs/BRANCH_PROTECTION.md` — the declared-contexts
table) are what the script *declares*, not what the server stores. Historical, and kept rather than
deleted because it is not reconstructible from the checkout: the push rejections at `a869255d` and
`544bd407` reported
`remote: Bypassed rule violations for refs/heads/dev: - 10 of 10 required status checks are expected.`
The eleven context names, and the seven names `REQUIRED_CHECKS` asks for that the ruleset does not
enforce, are recorded in `docs/BRANCH_PROTECTION.md` alongside the command that produced them. Still
open: whether the ruleset blocks the *merge button* — a bypassable direct push is a different rule.
The operator's raw paste is quoted verbatim, in stored order with its duplicate intact,
in `docs/BRANCH_PROTECTION.md` — the membership there is a transcript, not a subtraction.

**Superseding reading (same ruleset, later).** The counts above are the measurement of record for
their date and are kept rather than edited. A later reading of ruleset 24214086 (operator, `gh api`)
returns **thirteen** stored entries and **twelve** unique contexts — `web-ui-frontend-suite` has
been added — while the ruleset page renders **twelve rows**, because the UI de-duplicates the
repeated `Unit tests - Python 3.12`. Two views of one ruleset disagree by exactly one row; only the
API reading is authoritative. **"Require branches to be up to date before merging" is checked** on
this ruleset, and that is worth writing down twice over: every merge into `dev` invalidates every
open pull request until it takes `dev` back in, and the same setting is what would have exposed
#425's red as **inherited** rather than as a regression in the branch under review.

**Instrument.** The enforced list is a `gh` call, not a reading of the tree:

```
gh api repos/:owner/:repo/rulesets/24214086 --jq '.rules[] |
  select(.type=="required_status_checks") |
  .parameters.required_status_checks[].context'
```

`docs/BRANCH_PROTECTION.md` owns the list; this file cites it rather than keeping a
second copy that can drift.

### OPEN ITEM (revised) — required checks: what the push rejection settles, and what it does not

This section previously concluded that required checks "cannot be determined from this repository". That conclusion is retired above. The evidence it rested on is preserved here, because none of it is reconstructible from the checkout (the *enforced* list is no longer in that category — see the instrument above), and one of the two notes rests on an inference the push rejection contradicts.

Two knowledge-base notes, quoted below as whole lines so a later reader can verify them with `sed -n '<line>p'` rather than trusting this document:

```
.thoughtmachine/knowledge/project/operator_observations.md:314
- **CAUTION:** This is about how the CLI reports merge eligibility, NOT about Edge 8. Edge 8 was about required checks being configured. Edge 8 stands; the ruleset + required checks held (13 rows ran, 13 succeeded). Do NOT conflate. Do NOT reopen Edge 8.

.thoughtmachine/knowledge/project/working_agreements.md:127
Required checks only, not the unfiltered set. Attempted form `gh pr checks <PR> --required --watch` returns "no required checks reported" in this repo — branch protection has no required checks configured, so every check is advisory and --required gates on the empty set. Operative form: filter the check list by name. The repo suffixes advisory checks with "(advisory)".
```

The push rejection at `544bd407` settles the underlying fact that EDGE 8 disputed — required status checks do exist on `dev`, 10 of them (eleven unique contexts measured since — see above) — so the first note's premise holds. It does **not** refute the second note, whose subject is narrower: both notes distinguish the configuration of required checks from what the CLI reports about merge eligibility, and `operator_observations.md:314` warns about that conflation in its own words. What is falsified is only the second note's *inference* (`branch protection has no required checks configured`); what it actually measured is that `gh pr checks --required` reports no required checks **by name** in this repo. That is a reporting gap, not an absence of protection. `.thoughtmachine/knowledge/project/working_agreements.md:127` should still be corrected in a follow-up, so it stops asserting the inference.

Counts still do not have to reconcile: run rows are per-job, per-matrix-leg and per-workflow (§ above enumerates eighteen jobs), so "13 rows", "10 required checks" and the eighteen enumerated jobs are three different tallies of three different things.

`CI_PROCESS.md:66-69` reproduces both notes above with different punctuation and backticking — a third rendering of the same two facts. Reconciling it with the files' actual bytes is a follow-up: that file is a hashed bundle member, so any edit to it requires regenerating `HANDOFF.md` in the same pass.
