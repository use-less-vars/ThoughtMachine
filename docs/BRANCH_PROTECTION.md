# Branch protection for `dev`

This file describes one surface: the repository ruleset that actually gates `dev`.
Every claim carries the evidence that produced it, and anything never measured is
labelled unmeasured instead of being phrased to sound measured.

**How to read a root.** `TOOL:<command>` — an operator ran it against the live
repo; the date is in the section. `DISK:<path>:<line>` — read out of this tree at
that line. `scripts/apply_branch_protection.sh::<SYMBOL>` — read out of that
script, named by symbol rather than by line on purpose: the array, the exit
behaviour and the verify labels have all changed under this document, so an anchor
like `:78-96` quietly points at a *different* array after the next edit, whereas
`::REQUIRED_CHECKS` either resolves or does not. Line numbers are cited only where
the shape of the file is not in play (workflow `name:` and `on:` lines), and the
job id is given alongside so the line can be found again if it moved.

## Enforced on `dev` today: 13 required contexts

Root: `TOOL:gh api repos/use-less-vars/ThoughtMachine/rulesets/24214086`, run
2026-11-23T15:20+01:00. Run the instrument rather than trusting the list below:
the list drifts, the command does not.

```
gh api repos/:owner/:repo/rulesets/24214086 --jq '.rules[] |
  select(.type=="required_status_checks") |
  .parameters.required_status_checks[].context'
```

Thirteen contexts, in the order the call returns:

```
Contract gate (docs/CONTRACTS.md)
Install + check-only
Installer version-contract assertions
Linux - install/startup smoke (blocking)
Linux install + check-only + backend smoke
Mutation gate (contracts)
Ubuntu - full test suite
Unit tests - Python 3.11
Unit tests - Python 3.12
Unit tests - Python 3.13
Unit tests - Python 3.14
macOS launcher preflight (gating)
web-ui-frontend-suite
```

All thirteen carry `integration_id: 15368` (GitHub Actions) — the check runs of
this repo's own workflows. A context added to the rule without that id is matched
against a different integration's statuses, and the rule then waits on a name
nothing will ever report.

Each name below is a job's `name:` line in this tree:

| check-run name | workflow | job id | where |
| --- | --- | --- | --- |
| Contract gate (docs/CONTRACTS.md) | `contract-gate.yml` | `contract-gate` | `DISK:.github/workflows/contract-gate.yml:50` |
| Mutation gate (contracts) | `contract-gate.yml` | `mutation-gate` | `DISK:.github/workflows/contract-gate.yml:148` |
| Install + check-only | `first_start_ci.yml` | `first-start` | `DISK:.github/workflows/first_start_ci.yml:43` |
| Installer version-contract assertions | `python-version-matrix.yml` | `installer-contract` | `DISK:.github/workflows/python-version-matrix.yml:159` |
| Unit tests - Python 3.11 / 3.12 / 3.13 / 3.14 | `python-version-matrix.yml` | `unit-tests` | `DISK:.github/workflows/python-version-matrix.yml:39` |
| Ubuntu - full test suite | `platform-matrix.yml` | `ubuntu-suite` | `DISK:.github/workflows/platform-matrix.yml:68` |
| Linux - install/startup smoke (blocking) | `platform-matrix.yml` | `linux-install-smoke` | `DISK:.github/workflows/platform-matrix.yml:113` |
| web-ui-frontend-suite | `platform-matrix.yml` | `web-ui-frontend-suite` | `DISK:.github/workflows/platform-matrix.yml:501` |
| Linux install + check-only + backend smoke | `cross-platform-smoke.yml` | `linux-install` | `DISK:.github/workflows/cross-platform-smoke.yml:44` |
| macOS launcher preflight (gating) | `cross-platform-smoke.yml` | `macos-launcher` | `DISK:.github/workflows/cross-platform-smoke.yml:254` |

`Unit tests - Python 3.11/3.12/3.13/3.14` is **one** job whose `name:`
interpolates `${{ matrix.python-version }}`
(`DISK:.github/workflows/python-version-matrix.yml:39`). Four required contexts,
one file to edit.

Note also which macOS context is required: `macOS launcher preflight (gating)`,
a job of `cross-platform-smoke.yml`. Its name says "gating" because it gates its
own workflow. It is not one of the three `platform-matrix-macos.yml` jobs
discussed below.

### What earlier readings of the same ruleset said, and why that text is gone

Two earlier readings are recorded because both produced sentences this file used
to carry and can no longer carry:

* **2026-11-21** (`TOOL:gh api`, same ruleset id) returned thirteen **stored**
  entries for twelve **unique** contexts: `Unit tests - Python 3.12` was stored
  twice, and the ruleset page rendered it once because the UI de-duplicates. The
  2026-11-23 reading above returns thirteen entries with no duplicate, so the
  twelve-versus-thirteen discussion, and the verbatim transcript of the
  duplicated list, are **deleted** from this file rather than corrected — they
  described a rule that no longer exists. A duplicate that has been collapsed is
  not a fact about today's rule.
* `Mutation gate (contracts)` was **not** in the 2026-11-21 reading and is
  required today. It is the check run of the `mutation-gate` job
  (`DISK:.github/workflows/contract-gate.yml:148`), which runs
  `scripts/mutation_gate.py` (`DISK:.github/workflows/contract-gate.yml:179`).

Neither of those additions was accompanied by a change to
`scripts/apply_branch_protection.sh` at the time — see *The script declares the
same set the rule enforces* below, which is the practical consequence.

## Not required, but reported on every PR: the candidates

**Definition used in this file.** A *candidate* is a context whose job runs on
every PR head commit, reports its own check run, and can conclude failure — and
which the ruleset does not list as required. A red candidate is visible on the
PR and stops no merge. "Candidate" describes today's state; it promises nothing
about promotion.

Six candidates, all of which reported green on PR #462
(root: `TOOL:PR #462 check runs`):

| check-run name | workflow | job id | where |
| --- | --- | --- | --- |
| Linux - auth e2e (browser) | `platform-matrix.yml` | `linux-e2e-auth` | `DISK:.github/workflows/platform-matrix.yml:180` |
| web-ui-backend-suite | `platform-matrix.yml` | `web-ui-backend-suite` | `DISK:.github/workflows/platform-matrix.yml:442` |
| Windows - import smoke | `platform-matrix.yml` | `windows-import-smoke` | `DISK:.github/workflows/platform-matrix.yml:347` |
| Windows - thin pytest slice | `platform-matrix.yml` | `windows-thin-pytest` | `DISK:.github/workflows/platform-matrix.yml:383` |
| Windows Python 3.14 gate proof | `python-version-matrix.yml` | `windows-python-314` | `DISK:.github/workflows/python-version-matrix.yml:235` |
| Windows venv + smoke script | `cross-platform-smoke.yml` | `windows-smoke` | `DISK:.github/workflows/cross-platform-smoke.yml:124` |

These six are exactly the names
`scripts/apply_branch_protection.sh::CANDIDATE_CHECKS` holds and the ruleset
does not require. That is what makes them worth naming: the intent to require
them exists in this tree — in that one array, which `REQUIRED_CHECKS` does not
contain — and the rule does not reflect it.

Promoting one is a decision, not a measurement: add the name to ruleset
24214086, then re-run the instrument at the top of this file. Nothing in this
tree will do it for you.

### Three more jobs that are produced and not required

| check-run name | workflow | job id | where |
| --- | --- | --- | --- |
| macOS - full test suite (advisory) | `platform-matrix-macos.yml` | `macos-suite` | `DISK:.github/workflows/platform-matrix-macos.yml:75` |
| macOS - install/startup smoke (advisory) | `platform-matrix-macos.yml` | `macos-install-smoke` | `DISK:.github/workflows/platform-matrix-macos.yml:133` |
| macOS - Docker availability probe (advisory) | `platform-matrix-macos.yml` | `macos-docker-probe` | `DISK:.github/workflows/platform-matrix-macos.yml:310` |

They are not "candidates" in the sense above, and the difference is verdictable,
not stylistic: these three appear in neither the ruleset nor either array
(`REQUIRED_CHECKS` or `CANDIDATE_CHECKS`), so there is no intent-in-the-tree for
the rule to have failed to honour. Keeping them out is a **recorded decision** —
`macOS - full test suite (advisory)` was measured red on a PR while its workflow
run stayed green, back when the job carried `continue-on-error`; requiring it
would have blocked a merge on the runner rather than on the code. If one is ever
required, do it on a measured green run.

What changed since: the flag is gone. A sweep of `.github/workflows/*.yml` for a
job-level `continue-on-error:` key finds **0**; the string occurs twelve times in
those files and every occurrence is a comment
(`DISK:.github/workflows/platform-matrix-macos.yml:4` explains why). So a red
macOS job now fails its own workflow run, while still stopping no merge — the
failure is visible in the run conclusion and invisible to branch protection.

## Bypass: the rule does not bind everyone

Root: the same ruleset GET (`TOOL:gh api repos/use-less-vars/ThoughtMachine/rulesets/24214086`,
2026-11-23T15:20+01:00).

```
bypass_actors: [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
current_user_can_bypass: "always"
```

Which role `actor_id: 5` is has **not** been measured. Resolve it with
`gh api repos/<slug>/roles` or the ruleset page in the UI; do not infer it from
the number, and do not name a role in this file until that call has been run.

The load-bearing part is `bypass_mode: always`: whoever holds that role merges
past all thirteen contexts above. Read "required" in this file as "required of
everyone without the bypass". A rule that a role can bypass is still a rule —
it is just not the unconditional gate the word suggests.

The script's classic payload expresses the neighbouring idea by negation,
`enforce_admins: true` (`scripts/apply_branch_protection.sh::build_payload`),
i.e. "admins are not exempt". That payload does not land on this repo (see *The
script declares the same set the rule enforces*), and the ruleset states the
exemption positively rather than by negation — so read `bypass_actors`, above,
not `enforce_admins`, to know who can merge anyway.

## Strict: required checks are not required against the current base

`strict_required_status_checks_policy` is `false` (root: the same ruleset GET).
Consequence: a PR branch that is behind `dev` merges on checks that ran against a
stale base, so green does not mean "green on top of what is in `dev`". Rebase and
re-run if the merge order matters.

This happens to agree with the classic payload's `strict: false`
(`scripts/apply_branch_protection.sh::STRICT_UP_TO_DATE`, emitted in
`::build_payload`). The agreement is luck, not provenance: the field that binds is
the ruleset's.

Also from the same GET: `do_not_enforce_on_create` is `false`, and the ruleset's
other rules are exactly `deletion` and `non_fast_forward`. So force-push and
branch deletion are denied, and the ruleset itself imposes **no** review
requirement and **no** linear-history requirement. The classic payload's
`required_approving_review_count: 0`
(`scripts/apply_branch_protection.sh::build_payload`) is a statement about a
payload that is never sent. If a PR must be reviewed to merge, that comes from
settings outside this ruleset — **not measured here**.

## The script declares the same set the rule enforces

`scripts/apply_branch_protection.sh::REQUIRED_CHECKS` holds **13** names on this
tree, and they are exactly the thirteen contexts ruleset 24214086 enforces —
same set, same order, including `Mutation gate (contracts)`. Roots:
`scripts/apply_branch_protection.sh::REQUIRED_CHECKS` read against the ruleset GET
at the top of this file. The correspondence was compared name by name; it is not
the result of subtracting one total from another.

The six names the ruleset does not require are **not** in that array. They are
`scripts/apply_branch_protection.sh::CANDIDATE_CHECKS`, a separate array of six.
So a name being declared in the script no longer claims that the rule requires it:
required and produced-but-not-required are two lists, and only the first is
compared with the ruleset.

What the earlier revision of this section said, and why it is gone: the revision
of the script this file was first written against held **18** names in
`REQUIRED_CHECKS`, and against the thirteen enforced contexts it was wrong twice
over — it named all six candidates (which the ruleset does not require) and it
omitted `Mutation gate (contracts)` (which the ruleset does). That was a real
defect, and the habit that produced the earlier wrong numbers here — subtracting
one total from another to derive the enforced set — is what the arrays were split
to end. The 2026-11-23 reading above and this tree's arrays now agree; count
neither side, read the ruleset.

Why the gap could not be closed by running the old script, and still cannot: it
PUTs to `repos/<slug>/branches/${BRANCH}/protection`
(`scripts/apply_branch_protection.sh::api_url`), the **classic** endpoint. No run
of it, dry or `--apply`, writes ruleset 24214086. Treat its output as a
description of what it *would* ask for on a host where the classic endpoint
works.

What it does do about the ruleset layer is refuse to guess:
`scripts/apply_branch_protection.sh::observe_governing_rulesets` reads
`gh api repos/<slug>/rulesets`, and while a branch ruleset governs `dev` the run
stops with exit **3** rather than reporting a classic apply that decides nothing
(`::refuse_governed`). It never writes that layer: reading the list is the limit,
and `--force-classic` only lets the classic path proceed with the ruleset layer
named as unconfirmed.

## Keeping the names honest: `--verify-names FILE`

```
gh api repos/use-less-vars/ThoughtMachine/commits/<sha>/check-runs \
  --jq '.check_runs[].name' > /tmp/observed.txt
scripts/apply_branch_protection.sh --verify-names /tmp/observed.txt
```

Local only: no network, no `gh` call, no ruleset observation. `FILE` is trimmed of
surrounding whitespace; blank lines, `#` comments and duplicate names (a job with
several completed runs appears once per run) are ignored. Three labels, read out
of `scripts/apply_branch_protection.sh::--verify-names`:

* **`REQUIRED BUT ABSENT`** — a name in `REQUIRED_CHECKS` that the file lacks: the
  rule can never be satisfied, so every PR sits at "Expected — waiting for status
  to be reported". Exits **1**.
* **`CANDIDATE, PRESENT`** — a name in `CANDIDATE_CHECKS` that the file reports.
  Informational: produced, measured not required, and it does not affect the exit
  code.
* **`PRESENT BUT UNDECLARED`** — a name in the file that is in neither array: not
  a gate, and not classified yet. Informational; exits **0**.

Exit **2** is usage error (no `FILE` argument, file missing or unreadable).

Read the third label for what it is: it diffs the file against the two arrays, not
against the ruleset. A clean run therefore says the script's own declaration
matches what was observed — nothing about the rule. The consequence worth knowing
before trusting a label: the six candidates are a separate array, so a run that
reports all six labels them `CANDIDATE, PRESENT` instead of silently passing them
through, and the three macOS names — in neither array — are listed as
`PRESENT BUT UNDECLARED`, which is the honest reading of "produced, not
classified". Run the check after any job rename and after any change to
`REQUIRED_CHECKS`, and compare its answer with the ruleset instrument at the top
of this file — they answer different questions.

## What a required context is

GitHub matches a required status check by its **check-run name**: the job's bare
`name:` (the bare job id when `name:` is absent), with `${{ matrix.x }}`
interpolated. The workflow-level `name:` is not part of it; `runs-on`,
`container:` and step-level `if:` do not change it; a matrix job yields one check
per combination. Two consequences that matter to a reader of this file: no two
jobs may share a name (the rule cannot tell them apart), and renaming a job
silently un-gates the branch — the rule waits for a name nothing reports.

That is GitHub's matching rule, not a measurement taken here. Where it shows up
in this tree is the `unit-tests` matrix: adding or removing a Python version
there adds or removes a **required** context without anyone editing the ruleset.

### One candidate whose steps are conditional

`Linux - auth e2e (browser)` (`DISK:.github/workflows/platform-matrix.yml:180`) is
a candidate worth understanding before promoting. A "Detect auth-relevant
changes" step writes `auth=true|false`
(`DISK:.github/workflows/platform-matrix.yml:218-240`), the suite steps run only
on `'true'` (`:271-320`), a "Report the skip" step covers the other branch
(`:264-265`), and the job itself has no `if:` (`:179-181`). So the check always
reports, and a skipped run reports **success**. Requiring it would block exactly
the PRs that touch auth-relevant files and prove nothing on the rest — acceptable
as a gate, never as coverage.

### Triggers

Every workflow that owns a name in the enforced table carries `pull_request:`
with no path filter — `DISK:.github/workflows/contract-gate.yml:37`,
`cross-platform-smoke.yml:31`, `first_start_ci.yml:35`, `platform-matrix.yml:51`,
`python-version-matrix.yml:31` — which is what a required context needs.
`contract-gate.yml` is the narrow one: `pull_request: branches: [dev]` plus
`workflow_dispatch`, and **no** `push:` trigger (`:37-39`), so a direct push to
`dev` runs neither the contract nor the mutation gate. Correct, if the pushed
commit is also what a PR would test; a required check only ever sees PRs.

## Applying and verifying

The dry run is the safe half, and on this repo it is the only half that reaches
the gate in force:

```
scripts/apply_branch_protection.sh            # print payload + command, change nothing
scripts/apply_branch_protection.sh --apply    # PUT to the classic endpoint (needs gh + admin token)
```

Both address classic branch protection; the enforced list comes from the ruleset
instrument at the top of this file, and the script cannot write to it. (The
classic endpoint answering 404 on this repo is an operator observation carried
over from earlier revisions of this file; it is not re-measured here. The
conclusion does not depend on it, because the script contains no ruleset write at
all.) To change what is enforced, edit ruleset 24214086 and then read it back
with the instrument.

`--unprotect --apply` (`gh api --method DELETE .../protection`,
`scripts/apply_branch_protection.sh::--unprotect`) drops classic reviews,
required checks, `enforce_admins`, force-push and deletion protection in one
call. It is not the emergency hatch it reads as here: there is no classic
protection on this repo to delete, and it never touches the ruleset, so it cannot
unstick a required context that cannot go green. That hatch is the ruleset — edit
it, or use the bypass role. Reserve the classic path for a host where the classic
endpoint is the gate.

## Not measured here

* Which role `actor_id: 5` is (see *Bypass*), and therefore who can merge past
  the rule.
* Whether each of the thirteen is currently green, and whether any named context
  fails on a regular basis.
* What a PR produces in total: no count of all reported contexts is given in this
  file, because nobody counted it for this revision. Counting it, or arriving at
  it by subtraction, is how the previous revisions of this file went wrong.
* Any review or linear-history requirement coming from repository settings
  outside ruleset 24214086.
