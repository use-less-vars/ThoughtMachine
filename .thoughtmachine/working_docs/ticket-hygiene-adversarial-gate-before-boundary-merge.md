# Ticket (hygiene): the adversarial verification gate runs BEFORE a boundary wave merges

**Frame / Tag:** hygiene
**Status:** open (standing process rule; not a code defect)
**Severity:** High — every boundary wave currently merges on author-side evidence alone
**Filed:** 2026-10-22, bookkeeping chunk A

## Summary

After every boundary wave, an **independent adversarial verification gate** runs **before
the merge**, not after it. Author-side green CI is **not** evidence. The merge
precondition is an adversarial pass by a seat that did not write the boundary.

This ticket files that as a standing process rule and records why. It is a process
defect in how we have been merging, not a bug in any one file.

The concrete trigger: the isolation wave was merged on green CI. An adversarial gate was
run **after** the merge and found seven breaches, which became four follow-up PRs.
*(The "seven breaches / four PRs" count is operator-supplied. A grep of
`.thoughtmachine/` for `seven breaches|7 breaches|breach(es)? found` finds **no in-repo
write-up of that count** — the report that exists,
`.thoughtmachine/reports/wave9a-adversarial-verify.md`, records findings individually,
not as a tally. Cited from the operator brief, not from a repo document.)*

Had the gate run before the merge, those four PRs would have been one merge commit.

## The rule

1. A boundary wave (any change that makes or removes a gate around a capability:
   permissions, admission, isolation, funnel, container lifecycle) is not mergeable on
   the author's own tests.
2. Before merge, a **different seat** runs an adversarial pass against the boundary.
3. The pass must attempt **escape and bypass**, not re-assert the design. Re-running the
   author's tests, or writing a test that confirms the fix, is not a pass.
4. Findings are either fixed pre-merge or filed with a **named owner**. "Noted" is not a
   disposition.
5. Never merge first and verify second. A post-merge adversarial report is a bug report
   about the merge, not a substitute for the gate.

## Failure mode: `verification-written-by-author`

The same agent that wrote the boundary also writes the "proof" that the boundary holds.
Its tests encode its own model of the boundary, so they pass **by construction**. The
result is a merge that is green and a boundary that is not there.

Naming it matters because it is invisible in the artifacts: the ticket, the test and the
PR all look complete. Only a different input can tell the difference.

This is already written down in the repo, as a rule, at
`.thoughtmachine/knowledge/project/operator_observations.md:2076-2080`:

> The claim and its verification must come from different **inputs** … If you cannot name
> the input you used that the author did not use, you have not verified anything.

and as a three-layer table at `operator_observations.md:2120-2131`, which separates
**Code** (does the rule exist), **Prompt** (is the rule stated where the actor reads it)
and **Verification** (would a test go red if the rule were removed?). The right-hand
column of that table is the sharp end:

| layer | what counts | what does not count |
| --- | --- | --- |
| Verification | a test that would go **red if the rule were removed** | a test that **confirms the fix** instead of attacking the rule |

## Five instances

Four of the five are collected in one place,
`.thoughtmachine/knowledge/project/operator_observations.md:2049-2069`; the fifth is from
the adversarial report itself.

### 1. Ticket evidence authored by the ticket's own author

The container-create admission ticket blames
`container_create.py:351 create_hardened_container`. But **two functions share that
name**: the wrapper actually on the gate path is `infra/container_registry.py:231`, which
gates at `:303` and calls the primitive at `container_create.py:351` with
`admission=None, record=True`; the real hot-path call is `container_registry.py:714`.
The ticket reads as a correct bug report and points at the one call site where the
behaviour cannot occur.
Cite: `operator_observations.md:2049-2056`.

### 2. A deletion justified from the shell the deleter was in

Commit `85035605` removed `.githooks/pre-commit` as "dead" because `core.hooksPath` read
unset. It is unset **in a bare host shell** and set **on the container path**. The reading
was true in the shell that took it and false about the project.
Cite: `operator_observations.md:2057-2061`.

### 3. A status block asserting a HEAD, written by the seat whose branch was checked out

The reported tip was the **checked-out feature branch**, not the landing branch. That is
the true output of `git rev-parse HEAD` in that tree, and a false statement about the
project. Cite: `operator_observations.md:2062-2065`.

### 4. Verification by proxy: staleness from mtime

mtime records **when the bytes changed**, not whether they are still true. Four documents
were ranked fresh/stale on timestamps. A timestamp is not an input the author did not
use — it is the author's own edit history. Cite: `operator_observations.md:2066-2069`.

### 5. The funnel claim, refuted by an independent adversarial pass

"Every destroy site routes through the funnel" was false. The migration covered the
**author's own** 9-site plan (`wave9a-funnel-report.md` §4 / `wave9a-step1-funnel-brief.md`)
and left production destroy/stop paths un-funneled, including the public recreate API
`ContainerManager.stop` / `ContainerManager.remove` (`infra/container_manager.py:3155-3222`).
The plan was the input, and the plan was written by the author.
Cite: `.thoughtmachine/reports/wave9a-adversarial-verify.md:19`, `:28`, `:261`.

Instance 5 is the one that shows what the gate is for: the claim was checkable, the
author had every reason to believe it, and a pass with different inputs falsified it.

Prior adversarial artifacts in the repo (all pre-existing, cited so the practice has a
lineage rather than starting here):

- `.thoughtmachine/reports/wave9a-adversarial-verify.md`
- `.thoughtmachine/reports/wave9a-0a-adversarial.md`
- `.thoughtmachine/reports/wave9a-0b-adversarial.md`
- `.thoughtmachine/reports/wave9-n2prime-adversarial.md`
- `.thoughtmachine/reports/wave9-verification-read-adversarial.md`
- specs: `.thoughtmachine/working_docs/wave9a-adversarial-verify-spec.md`,
  `.thoughtmachine/working_docs/wave9a-0a-adversarial-spec.md`

## The gate's minimum shape

Stated so the rule is executable rather than aspirational.

**Who runs it.** Not the author. A different seat (different agent, or the operator) with
**different inputs**: fresh reads of the tree, the diff read cold, and at least one input
the author did not use — an independent search, a run against the live tree, a check of
the call graph rather than the ticket's description of it. If the verifier cannot name
the input it used that the author did not, it has not verified anything.

**What it must attempt.** Escape and bypass against the boundary being merged:

- call the guarded thing by a route the author did not enumerate (the other call site,
  the public API, the worker path);
- remove the rule and check that something goes red;
- check the actor's prompt text, not just the code — does the actor that must obey the
  rule ever read it?;
- confirm the reading is about the project and not about the verifier's own shell,
  branch, or edit history.

Re-asserting the design, or re-running the author's tests, is not an attempt.

**What counts as pass.** No breach found **under those escape attempts**, and every
finding either fixed before the merge or filed with a **named owner**. A pass that found
nothing because it tried nothing is a fail.

## What this ticket does not do

No code. No CI change. This is the rule and its evidence; wiring the gate into a
checklist or a required check is a separate decision for the operator.

## TODO(operator)

- Choose the enforcement point: a documented merge checklist, a required CI job, or a
  named-seat sign-off line on the PR template. Until one exists this is a stated rule with
  no mechanism behind it, which is itself the failure mode it describes.
