# Backlog triage — the three oldest open tickets

**Written:** 2026-10-29, at HEAD `84bfd019`, at operator request
("Memory & Stats Wave", task 3).
**Purpose:** offload the operator's memory. Until now, "which tickets are
oldest and still open" existed only in the operator's recollection.

## The rule this document exists to serve

> **Review the oldest 3 open tickets at the start of every session.**

This file is the answer to that review as of the date above. Re-derive it; do not
assume it stays true.

## Why the obvious method gives the wrong answer

`.thoughtmachine/working_docs/` holds **186** `ticket-*.md` files with no index
and **no `status.md`** (verified: no `status*.md` exists at any depth ≤ 2).

Sorting by filesystem mtime — the obvious approach — is **wrong here**. mtime
records the last rewrite, not the filing. Five Gen-46 tickets all carry mtime
`2026-10-20 18:30` (a bulk write), which sorts them *older* than Gen-43 tickets
whose mtimes are `2026-10-21 00:20` but whose own headers say `2026-10-14`.

**Use the `**Filed:**` line inside each file.** It is the only authoritative
date. This is a trap that has already cost time; a future agent that sorts by
mtime will confidently list the wrong three.

## The three oldest open tickets

### 1. `ticket-bug-live-view-container-stale-no-push.md`
- **Filed:** Gen 43, **2026-10-14** at `d378c206` — oldest in the directory.
- **Status line:** *"filed, not dispatched. Candidate for the UI sprint."*
- **Still open?** **Yes.** Its own 2026-10-20 amendment re-confirms the defect
  live at `124477b7`: a container created and another removed during one run,
  and the panel reflected neither without a manual reload.
- **What it is:** new containers do not appear in the live view panel until the
  operator hits F5. Visibility only, no data loss.
- **Duplicate:** same root cause as `ticket-hygiene-panel-polls-no-subscribe.md`
  (panel polls status endpoints, never subscribes to the EventBus). That ticket's
  status is *"open (FILE ONLY — no fix in wave 9c)"*. **These two should be
  merged before either is dispatched** — one is the symptom, the other is the
  mechanism.
- **Why it may still matter:** it is the most operator-visible item in the
  backlog. Every other ticket in this triage is invisible in normal use; this one
  is visible whenever the operator creates a container.

### 2. `ticket-feat-checkSystem-runtime-version.md`
- **Filed:** Gen 43, **2026-10-15** at `eadd2ca2`. Source noted as *"operator
  request, 2026-10-14; misfiled by Gen 43 (acknowledged miss; conversation ≠
  KB)"*.
- **Status line:** *"filed, not dispatched. Gen 44 dispatches."*
- **Still open?** **No — this ticket appears to be DONE and should be closed.**
  Verified at HEAD `84bfd019`:
  - `CheckSystem query='version'` exists and is live; it is in the tool's current
    published schema.
  - `tests/test_wave10a_runtime_commit_and_version.py::test_version_query_shape`
    asserts the `{revision, branch, dirty}` payload the ticket specified.
  - The ticket's design decision D2 (git-less fallback, never fabricate a hash)
    is satisfied: the revision degrades to an `"unknown"` sentinel — documented
    at `tests/test_wave9c_display_scope.py:85-89`.
  - D1 (start-time capture, not query-time read) is implemented — which is
    exactly what `ticket-bug-checksystem-version-reports-loaded-revision-not-head.md`
    later complains about, confirming the feature shipped.
- **Not verified:** the ticket's D3 also wanted `GET /api/version` for the panel.
  I did not confirm that route exists. If it does not, **re-scope this ticket to
  just that**, rather than leaving it filed as if the whole feature were missing.
- **Follow-up that supersedes part of it:**
  `ticket-bug-checksystem-version-reports-loaded-revision-not-head.md` — the
  `revision` field names the commit the process *loaded*, not HEAD, and nothing
  in the payload said so. Documentation-only fix, also open.

### 3. `ticket-test-verify-container-integrity-turn-boundary-e2e.md`
- **Filed:** Gen 44, **2026-10-15**.
- **Status line:** *"filed, not dispatched."*
- **Still open?** Presumed open — **I did not verify this one** and am labelling
  it rather than guessing. It is a *test* ticket (end-to-end verification of
  container integrity across a turn boundary), so "is it done" means "does the
  test exist and pass", which needs a test run I have not done here.
- **Next step for whoever picks it up:** check whether the e2e exists under
  `tests/` first. If it does, close this; if not, this is cheap and it guards the
  turn-start reap, which has its own open defect
  (`ticket-bug-container-turn-start-reap-kills-running.md`).

## What this triage actually says about the backlog

1. **One of the three oldest is already implemented.** The backlog is not being
   worked down, it is accumulating — 186 tickets, and the oldest "open" feature
   shipped generations ago and was never closed. Closing tickets is cheaper than
   filing them and has not been happening.
2. **Duplicates are being filed as new tickets.** The three container-class
   tickets (`ticket-bug-container-type-label-vs-lifecycle-class.md`,
   `ticket-bug-container-type-label-vocabulary-collides-with-lifecycle.md`,
   `ticket-fact1-label-vs-lifecycle-class.md`) describe one defect from three
   angles; likewise the two panel tickets above. Filing needs a lookup step.
3. **There is no status convention.** Five of the twelve oldest tickets have no
   `**Status:**` line at all, and those that do use at least four different
   forms ("filed, not dispatched", "open (FILE ONLY …)", "filed", none). This is
   why the operator has had to be the system's memory.
4. **Recommendation:** add a machine-readable `**Status:** open|closed`,
   `**Filed:** YYYY-MM-DD` pair to every ticket, and a one-line index. Until
   then, the `**Filed:**` line is the only reliable sort key.

## Maintenance note

`backlog-triage.md` was **not** tracked by git when written —
`.thoughtmachine/working_docs/*` ignores everything except `ticket-*.md`,
`protocol-*.md` and named exceptions (`.gitignore:120-124`). A negation
`!.thoughtmachine/working_docs/backlog-triage.md` was added alongside this file
so that this triage survives a clone. If that line is ever removed, this
document silently stops being versioned — the same failure mode that lost
earlier working docs.
