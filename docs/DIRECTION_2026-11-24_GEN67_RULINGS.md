# DIRECTION - MAIN ENGINEER RULINGS - 2026-11-24 (Gen 67)

**Status:** authoritative for the branches named below. Supersedes every ruling relayed by
chat before this file existed. If a chat message contradicts this file, this file wins and
the chat message is the stale artefact. Cite it by path, not by quotation.

**Why this file exists.** Instructions were being relayed by hand, one paste per agent per
turn, and a branch diff was about to be relayed the same way. A pasted handoff cannot be
superseded, cannot be cited, and becomes unfalsifiable the moment the tree moves. The repo
is the bus. Agents: read this file; do not ask the operator to re-type it.

---

## 0. Delivery - the blocker that outranks all the code

Two agents hold `+815/-27` and `+737/-10` of finished work and neither can push. The
operator cannot push either agent's worktree: he has never seen `3680f567` or `a709bdd8`.
There is currently **no delivery channel that is not a human with a clipboard**.

Until a push-capable remote exists, the required output of every completed unit of work is:

```
git format-patch <base-sha>..<your-sha> --stdout > <name>.patch
```

as a **file**, not as chat text. Long diffs pasted through chat arrive corrupted. The
operator applies them with `git am` onto a branch off `origin/dev`, then pushes.

---

## 1. Branch status as of this file

| branch | sha | state |
|---|---|---|
| `feat/crash-receipt-phase1` | `fcebb100` | content is on dev via squash `ee3c654f`. Blobs verified: `kill_thoughtmachine.sh` = `85037b03`, `tests/test_crash_receipt.py` = `35157aad`. The individual shas are NOT ancestors of dev. Never re-merge this branch. |
| `fix/images-survive-restart` (#472) | `2a3541a7` | rebased onto `origin/dev`; `tests/test_crash_receipt.py` blob confirmed = `35157aad`. Agent 2's three files only. |
| `fix/live-container-list` (#473) | rebased | awaiting lane verdicts. |
| `fix/container-retention-charter` (Agent 1 step 1) | `3680f567` | exists only in Agent 1's clone, base `9de79d42` (18 commits stale). Not in the operator's repo. Blocked on delivery. |
| `feat/image-tombstone-retention` (Agent 2 S2) | `a709bdd8` | exists only in Agent 2's clone, base `origin/dev @ 5bce3855` (stale). Not in the operator's repo. Blocked on delivery. |

---

## 2. Current red lanes - one hypothesis, not three bugs

After the restart, three lanes are red on `2a3541a7` and the two Linux lanes are the new
information: `Platform Matrix #1226`, `Python Version Matrix #1278`, and
`Platform Matrix (macOS, advisory) #244`. On `ee3c654f` the two Linux lanes simply had not
run (grey), so their verdict was never evidence.

**Hypothesis (unverified): one cause.** `requirements.txt:5` is `openai>=1.0.0` - lower
bound only; no `setup.py`, `setup.cfg`, `constraints*.txt`, `uv.lock`, `poetry.lock` or
`Pipfile` exists anywhere. Two test fakes build the SDK exception by hand and the current
SDK dereferences a field they never set:

- `tests/providers/test_openai_streaming.py:459` - `return APITimeoutError(request=SimpleNamespace())`
- `tests/test_llm_timeout_config.py:158` - `raise openai.APITimeoutError(request=SimpleNamespace())`
- latent third: `tests/test_llm_timeout_config.py:180` - `raise anthropic.APITimeoutError(request=SimpleNamespace())`

The failure is raised inside site-packages (`openai/_exceptions.py:49`,
`if not any(name in request.headers for name in SENSITIVE_HEADERS)`), not in repo code.

**Nobody may chase this on a feature branch.** It is a dependency-pin PR of its own:
one fixture helper used at all three sites, an upper bound on `openai`, and a pinned
interpreter on the macOS lane (`platform-matrix-macos.yml:56-57` sets a scalar
`PYTHON_VERSION: "3.14"`). Do not delete or ski p the tests: `scripts/ci_assert_collected.py
--expected 7600` (`:86-89`) exists to catch exactly that.

---

## 3. Rulings for Agent 2 (S2, image tombstone)

1. **Rebase onto `origin/dev` before opening the PR.** Base is `5bce3855`; dev is `ee3c654f`.
   Quote where every number came from, and re-derive counts after the rebase: a test count is
   a property of the tree, not of the file. Your own 30-vs-36 correction will move again.
2. **40 image-bearing messages / 32 MiB: accepted.** Keep the guess label on 40 and keep the
   ceiling written as `MAX_WORKSPACE_IMAGE_BYTES // 8` so it tracks the workspace budget.
3. **The UI silence is part of this feature, not a follow-on issue.** A feature that makes the
   UI stop showing something the user pasted, with no explanation, is a regression we created.
   Add the render hint keyed on `images_tombstoned > 0 and images.length == 0` in
   `MessageBubble.jsx` (around `:390-407`), same sentence as the adapters. Pin it with a text
   test that reads the JSX and asserts the literal matches the Python placeholder, because
   there is no shared constant across the boundary and drift is silent otherwise.
4. **Parity surface #4 stands.** Both adapters must say the same thing for a tombstoned
   message; your knitting test is the right shape.
5. **`content` stays byte-identical to what the user typed.** Your round-trip test is the
   acceptance criterion; keep it named.
6. **Your verification numbers are UNVERIFIED by me** and will stay that way until CI runs on
   the pushed commit. That is the rule for everyone, including me.
7. **No merge-order constraint with #472.** S2 never deletes; the file sets are disjoint.
   Confirmed.

---

## 4. Rulings for Agent 1 (step 2, retention reap)

Measured on the current tree (946 files searched), not recalled:

- `Record` lives at `thoughtmachine/container_record/models.py:181-218`, 18 fields,
  `SCHEMA_VERSION_CURRENT = 5` (`:47`), `state: str = ""` (`:204`).
- `STATE_EXITED`: **NOT FOUND repo-wide.** Only `STATE_CREATING` (`:71`) and
  `STATE_RUNNING` (`:76`). Your central claim - the ledger cannot answer "did this exit?" -
  is confirmed by measurement.
- No `retention_mode`, `retention_until` or `retention_source` exists on dev; the only
  retention field is `retention_days: int | None` (`:213`). Expected: your branch is not on dev.

**R1 - accepted.** Two branches, because each sweep sees a different half.

**R2 - `EXIT_EVIDENCE_GRACE_S = 60`, not 300 and not 0.** The grace exists so `_emit_oom_event`
and a readable exit code land BEFORE the record carrying them is deleted. A reaper that
outruns its own evidence destroys the audit that justifies it. Exit code unknown => skip,
fail closed.

**R3 - rejected as stated.** Branch B with no grace repeats the bug you are already fixing: a
container created between the snapshot and the record write is briefly absent from `live_ids`
and would be reaped instantly. Branch B requires `B_MIN_RECORD_AGE_S = 120`. Still a pure
function of disk state plus one snapshot, so section 5 survives.

**R4 - accepted, and it is load-bearing.** Keep the legacy age branch live for ephemeral
records. Nothing becomes immortal by accident; a never-started ephemeral record still dies.

**R5 - accepted.** Aggregate INFO per pass; per-record DEBUG behind
`THOUGHTMACHINE_RETENTION_TRACE=1`; reasons from a closed enum, never prose.

**R6 - your question is the wrong one.** The field's existence is not the test. `Record` has
`to_dict()` at `:220` and `from_dict()` at `:243`; provenance that is not round-tripped by
them is lost on the next load and the log line would be inferring from nothing. Quote
`to_dict`/`from_dict` and show `retention_source` surviving a write-read cycle. If it does
not survive, emit nothing rather than infer. This is not hypothetical: `from_dict` is where
you already found the `retained_until` crash.

Three findings the independent read produced that the design should absorb:

1. `_DESTROY_AUDIT = CONTAINER_DESTROY` exists at `infra/container_manager.py:5158` and
   `_reap_bound_record` never uses it. Your task is not "add an audit", it is "connect the
   audit constant already sitting next to the function".
2. All seven refusal paths in `_reap_bound_record` (`:5161-5194`) are silent, and two are
   indistinguishable by construction: `except Exception: record = None` (`:5176-5179`) merges
   "load failed" with "no record found". Under a reaper that is the difference between safe
   and unknown.
3. The guard at `:5188-5189` reads `policy.own_lifecycle` and refuses EVERY lifecycle-owned
   class except `LIFECYCLE_RESOURCE`. So `LIFECYCLE_SERVICE` records can never be reaped
   through this path, silently. Your `service -> retained_until` class default currently has
   no exit ramp at all.

**Step 1 is UNVERIFIED and the baseline is gone.** The `157 passed` run has no sha attached
and the tree has since moved. A pass without a sha becomes unauditable the moment the tree
moves. Re-run the six files on the operator's machine with `git rev-parse HEAD` recorded in
the same breath, after the branch arrives.

---

## 5. Standing rules (all agents, including me)

1. **Numbers need a root.** A pass count is evidence only with a pasted transcript carrying
   a sha or a commit. No worker pass count is evidence at all. Code claims arrive as
   `file:line` plus quoted text.
2. **Rebase onto `origin/dev`, never local `dev`.** A stale local ref has already produced
   one false report in this project and one wasted rebase.
3. **Cite the commit that is actually on dev.** After a squash, the individual shas are not
   ancestors of dev; `git merge-base --is-ancestor <sha> dev` correctly says no.
4. **A skip is never a pass, and a green on the wrong tree is not a pass.** GitHub runs PR
   checks on the merge ref, so a branch's red may be a statement about branch-union-dev, not
   about the branch.
5. **A control that cannot do its job must REFUSE, never degrade into a different control.**
   Advisory lanes red on every PR, caps that cannot see the largest object they name, and
   reapers that swallow their own refusals are all the same defect.

---

## 6. Open asks

- Operator: the failing TEST NAMES (not the log) from `Platform Matrix #1226` or
  `Python Version Matrix #1278` on `2a3541a7`, to confirm or kill the one-cause hypothesis.
- Agent 2: `format-patch` file for `feat/image-tombstone-retention`, rebased onto `origin/dev`.
- Agent 1: `format-patch` file for `fix/container-retention-charter`, plus the `to_dict`/
  `from_dict` quotes for R6.
- Operator: one `format-patch`-capable channel so this file is never again supplemented by a
  pasted diff.
