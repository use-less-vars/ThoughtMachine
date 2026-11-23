# Branch protection for `dev`

Two different mechanisms carry the name in this file, and they are kept apart on
purpose:

* **What the script asks for.** `scripts/apply_branch_protection.sh` (`REQUIRED_CHECKS`)
  targets the **classic** branch-protection endpoint, which answers 404 on this repo.
  On this host the script is *intended*, not *effective*.
* **What actually blocks a merge.** A repository ruleset, `24214086`. Every membership
  claim in this file is measured against that ruleset — never derived from
  `REQUIRED_CHECKS`, never derived by subtracting from a count.

Dates, because these lists drift faster than the file: the ruleset readings are the
operator's `gh api` reads of 2026-11-19 and 2026-11-21; the workflow-side mapping (which
job reports which context, on which runner, under which trigger) was read off the tree
at `1c4dc622` — the value `git rev-parse origin/dev` returned, not a remembered sha — on
2026-11-23. Re-run the instrument rather than trusting the lists below.

## Enforced on `dev` today: 13 stored entries, 12 unique contexts

Classic protection 404s on this repo, so the only surface that answers this question is
the repository ruleset. The instrument — run it rather than trusting the list below:

```
gh api repos/:owner/:repo/rulesets/24214086 --jq '.rules[] |
  select(.type=="required_status_checks") |
  .parameters.required_status_checks[].context'
```

That returns **thirteen** entries for **twelve** unique contexts. The order below is the
order the call returns, not an order anyone chose: it is neither alphabetical nor
grouped by workflow.

**Verbatim, stored order, duplicate intact** — the reading of 2026-11-21. Every name is
copied; nothing in the block was inferred from a count.

```
Ubuntu - full test suite
Linux - install/startup smoke (blocking)
Linux install + check-only + backend smoke
macOS launcher preflight (gating)
Install + check-only
Unit tests - Python 3.11
Unit tests - Python 3.12
Unit tests - Python 3.12
Unit tests - Python 3.13
Installer version-contract assertions
Contract gate (docs/CONTRACTS.md)
Unit tests - Python 3.14
web-ui-frontend-suite
```

**The duplicate, explained by measurement rather than by counting.** `Unit tests -
Python 3.12` is stored twice, adjacent to itself at positions 7 and 8. It is not two
jobs: the only producer of that literal string in the tree is ONE job,
`python-version-matrix.yml:39` (`unit-tests`, `runs-on` at `:40`),
whose `name:` interpolates `matrix.python-version` over the list at `:44` —
`['3.11', '3.12', '3.13', '3.14']`, a single `3.12`. So the ruleset stores two entries
that name **one** check run; the second is a duplicate of a context *string*, not a
second required job, and GitHub matches both entries to the same run. The consequence
for readers is the reason this paragraph exists: the ruleset page renders **twelve**
rows because the UI de-duplicates, the API returns **thirteen** entries, and neither
number is the membership. Only the API reading is authoritative.

De-duplicated and sorted, the twelve contexts — the same twelve, spelled the way
`scripts/apply_branch_protection.sh:75-96` spells them (the `REQUIRED_CHECKS` array
opens at `:75` and its last entry is `:96`):

```
Contract gate (docs/CONTRACTS.md)
Install + check-only
Installer version-contract assertions
Linux install + check-only + backend smoke
Linux - install/startup smoke (blocking)
macOS launcher preflight (gating)
Ubuntu - full test suite
Unit tests - Python 3.11
Unit tests - Python 3.12
Unit tests - Python 3.13
Unit tests - Python 3.14
web-ui-frontend-suite
```

### Each enforced context, mapped to the one job that reports it

Twelve of twelve **matched**; **zero orphaned** — no stored context lacks a producer in
this tree, so no required check here is a name no job will ever report.

| check-run name (verbatim) | workflow | job id | runs-on |
| --- | --- | --- | --- |
| Contract gate (docs/CONTRACTS.md) | `contract-gate.yml:39` | contract-gate (`:38`) | ubuntu-latest (`:40`) |
| Install + check-only | `first_start_ci.yml:43` | first-start (`:42`) | ubuntu-latest (`:44`), container `debian:13` (`:45`) |
| Installer version-contract assertions | `python-version-matrix.yml:159` | installer-contract (`:158`) | ubuntu-latest (`:160`) |
| Linux install + check-only + backend smoke | `cross-platform-smoke.yml:44` | linux-install (`:43`) | ubuntu-latest (`:45`) |
| Linux - install/startup smoke (blocking) | `platform-matrix.yml:113` | linux-install-smoke (`:112`) | ubuntu-latest (`:114`) |
| macOS launcher preflight (gating) | `cross-platform-smoke.yml:254` | macos-launcher (`:253`) | **macos-latest** (`:255`) |
| Ubuntu - full test suite | `platform-matrix.yml:68` | ubuntu-suite (`:67`) | ubuntu-latest (`:69`) |
| Unit tests - Python 3.11 | `python-version-matrix.yml:39` | unit-tests, `matrix.python-version` = 3.11 (`:44`) | ubuntu-latest (`:40`) |
| Unit tests - Python 3.12 | same job | unit-tests, 3.12 | ubuntu-latest |
| Unit tests - Python 3.13 | same job | unit-tests, 3.13 | ubuntu-latest |
| Unit tests - Python 3.14 | same job | unit-tests, 3.14 | ubuntu-latest |
| web-ui-frontend-suite | `platform-matrix.yml:501` | web-ui-frontend-suite (`:500`) | ubuntu-latest (`:502`) |

The four `Unit tests - Python 3.x` names are ONE job whose `name:` interpolates the
matrix, so adding or dropping a version in `python-version-matrix.yml` changes the
required set — and editing that matrix without editing the ruleset un-gates a version
silently. The duplicate entry above is what a ruleset looks like when the same name is
written twice by hand; it is not evidence of a fifth version.

### Can each of them report on a PR?

Measured triggers, every workflow this table touches:

| workflow | `on:` | line |
| --- | --- | --- |
| `contract-gate.yml` | `pull_request: branches: [dev]` + `workflow_dispatch` | `:26-28` |
| `cross-platform-smoke.yml` | `push: branches [dev, master]` + `pull_request:` | `:29-31` |
| `first_start_ci.yml` | `push: branches [dev, master]` + `pull_request:` | `:33-35` |
| `platform-matrix.yml` | `push: branches [dev, master]` + `pull_request:` | `:49-51` |
| `python-version-matrix.yml` | `push: branches [dev, master]` + `pull_request:` | `:29-31` |

All twelve required contexts therefore report on a pull request. One of them is
branch-filtered: `contract-gate.yml` runs only for PRs whose **base is `dev`** (plus
manual dispatch), so a PR opened against any other base leaves
`Contract gate (docs/CONTRACTS.md)` at "Expected" — which only matters for the merge
into `dev`, where it does run.

## Measured absence: the nine job names the ruleset does not require

The tree has 18 jobs producing 21 check-run names (`unit-tests` yields four). Twelve are
required, above; the other nine are measured absent from the thirteen stored entries.
The `note` column distinguishes *declared but not enforced* from *deliberately out*.

| check-run name (verbatim) | workflow | job id | runs-on | note |
| --- | --- | --- | --- | --- |
| Linux - auth e2e (browser) | `platform-matrix.yml:180` | linux-e2e-auth (`:179`) | ubuntu-latest (`:181`) | in `REQUIRED_CHECKS`, **not** enforced |
| web-ui-backend-suite | `platform-matrix.yml:442` | web-ui-backend-suite (`:441`) | ubuntu-latest (`:443`) | in `REQUIRED_CHECKS`, **not** enforced |
| Windows - import smoke | `platform-matrix.yml:347` | windows-import-smoke (`:346`) | windows-latest (`:348`) | in `REQUIRED_CHECKS`, **not** enforced |
| Windows - thin pytest slice | `platform-matrix.yml:383` | windows-thin-pytest (`:382`) | windows-latest (`:384`) | in `REQUIRED_CHECKS`, **not** enforced |
| Windows Python 3.14 gate proof | `python-version-matrix.yml:235` | windows-python-314 (`:234`) | windows-latest (`:236`) | in `REQUIRED_CHECKS`, **not** enforced |
| Windows venv + smoke script | `cross-platform-smoke.yml:124` | windows-smoke (`:123`) | windows-latest (`:125`) | in `REQUIRED_CHECKS`, **not** enforced |
| macOS - full test suite (advisory) | `platform-matrix-macos.yml:75` | macos-suite (`:74`) | macos-latest (`:76`) | deliberately out |
| macOS - install/startup smoke (advisory) | `platform-matrix-macos.yml:133` | macos-install-smoke (`:132`) | macos-latest (`:134`) | deliberately out |
| macOS - Docker availability probe (advisory) | `platform-matrix-macos.yml:310` | macos-docker-probe (`:309`) | macos-latest (`:311`) | deliberately out |

## The declared-versus-enforced gap, now by name

Six of the eighteen names `REQUIRED_CHECKS` asks for are not in the ruleset:
`Linux - auth e2e (browser)`, `web-ui-backend-suite`, `Windows - import smoke`,
`Windows Python 3.14 gate proof`, `Windows - thin pytest slice`, and
`Windows venv + smoke script`. Every one of those is a check that runs, can go red, and
stops no merge.

`web-ui-frontend-suite` used to be the seventh. The question this file carried after the
second reading — *if `web-ui-frontend-suite` was among the declared-but-unenforced
names, six remain; that diff has not been re-measured* — is now closed by name: it was,
and it is enforced, and six remain. The measured consequence of it having been outside
the ruleset is already recorded: `327d931f` (#424) merged with that job **red** and `dev`
carried the failure until a human repaired it by hand (#426, `b8587e5`).

## macOS: exactly one required context, and it is not from the macOS workflow

Read off the thirteen stored entries: **one** macOS context is required,
`macOS launcher preflight (gating)` (`cross-platform-smoke.yml:254`, `runs-on:
macos-latest` at `:255`). The three names in `platform-matrix-macos.yml` are measured
**absent** — those are the three `(advisory)` rows above.

So "no macOS check is required" is false as stated, and this file does not say it. The
measured statement is narrower and true: **the macOS-advisory workflow contributes
nothing to the required set, while one macOS runner context does gate a merge.** The two
facts are easy to collapse into each other, which is why both are written down.

What keeps the three advisory names out is a recorded decision, not a reading of the
workflow text. They are *not* dormant: `platform-matrix-macos.yml:52-54` says so in its
own header — *"No job here is advisory: a red macOS job fails this workflow's run. The
three check names are absent from REQUIRED_CHECKS, so that red does not block a merge --
but a green run now means the jobs passed."* No job in any of the six workflows carries
`continue-on-error`, so all 21 names gate their own workflow run and nine of them stop
no merge.

## What a required context is

GitHub matches a required status check by its **check-run name**: the bare job
`name:` (the bare job id when `name:` is absent), with `${{ matrix.x }}`
interpolated. The workflow-level `name:` is NOT part of it, so no two workflows
may reuse a job name, and renaming a job silently un-gates the branch.
`runs-on`, `container:` and step-level `if:` do not change the name; a matrix job
yields one check per combination.

Every name in the tables above matches that rule against a real job — including
`Install + check-only`, whose job runs inside `container: debian:13` on
`ubuntu-latest` (`first_start_ci.yml:44-45`) and whose context is still the bare job
name, and `Contract gate (docs/CONTRACTS.md)`, whose parenthetical is part of the job's
`name:` (`contract-gate.yml:39`), not a workflow prefix.

## How the names were measured

Every `X.yml:NN` citation in this file names `.github/workflows/X.yml`; the directory
is dropped because each of those basenames is unique in the tree, which is also why
`scripts/verify_doc_citations.py` can resolve them.

```
gh api repos/use-less-vars/ThoughtMachine/commits/<PR #401 head sha>/check-runs \
  --jq '.check_runs[].name'
```

That prints one bare name per reported check run. The reading of 2026-11-18 returned
21 distinct names on PR #401; the tree at `1c4dc622` holds 18 jobs and, because
`unit-tests` expands over four matrix values, 21 check-run names. `gh pr checks <n>`
is **not** usable for this: it prints a grouped
`<workflow> / <job>` display form, and contexts derived from that display name
checks no job ever reports. Every name in this file is either read from that response
or read from the ruleset instrument at the top; nothing is derived from a display form.

## Keeping the list honest: `--verify-names FILE`

```
gh api repos/use-less-vars/ThoughtMachine/commits/<sha>/check-runs \
  --jq '.check_runs[].name' > /tmp/observed.txt
scripts/apply_branch_protection.sh --verify-names /tmp/observed.txt
```

Local only — no network, no `gh` call, nothing written to disk.
`FILE` is trimmed of surrounding whitespace; blank lines, `#` comments and
duplicate names (a job with several completed runs appears once per run) are
ignored. The diff runs in **both** directions because the two mean different
things:

* **required but absent** → the rule can never be satisfied: every PR sits at
  "Expected — waiting for status to be reported", and with
  `enforce_admins: true` only `--unprotect` gets past it. Exits **1**. Measured
  today: this direction is empty — all twelve stored contexts have a producer.
* **present but not required** → a check runs that protection does not gate on.
  Informational: the nine names above are *meant* to appear here, and so is every
  newly added job. Exits **0**.

Exit 2 is a usage error (file missing or unreadable). Run it after any job
rename, and after any change to `REQUIRED_CHECKS`.

## Policy chosen in the payload

This section is about the **classic** payload the script builds, which this host does
not answer with — read it as intent, not as the ruleset's settings.

`enforce_admins: true` — no admin bypass, which is the point, and also its
cost: a maintainer's own direct push to `dev` is rejected like anyone else's, so
the normal loop is branch → PR → green checks → merge. `required_approving_
review_count: 0` (a PR is required, a second human is not — a PR author cannot
approve their own PR, so `1` plus `enforce_admins` deadlocks a single-maintainer
repo), `strict: false` (no "branch must be up to date" churn on a long CI),
force-push and deletion denied, stale reviews dismissed, conversation resolution
required, linear history off, `lock_branch: false`, `allow_fork_syncing: false`.

`lock_branch` and `allow_fork_syncing` are newer keys than the rest of the
payload. On an API (or GitHub Enterprise version) that does not know them the
PUT answers **422 Invalid request** rather than ignoring them; drop those two
lines from `build_payload` and retry — the rest of the rule is unaffected.

## Applying and verifying

Both commands below address classic branch protection. On this repo that endpoint
answers 404, so read them as the intended path for a host where it does not; the
list enforced today comes from the ruleset instrumented at the top of this file.

```
scripts/apply_branch_protection.sh            # print payload + command, change nothing
scripts/apply_branch_protection.sh --apply    # PUT it (needs gh + an admin token)
gh api repos/use-less-vars/ThoughtMachine/branches/dev/protection \
  --jq '.required_status_checks.contexts[]'   # after: read back what is enforced
```

Emergency hatch: `--unprotect --apply` runs
`gh api --method DELETE .../protection`, dropping reviews, required checks,
`enforce_admins`, force-push and deletion protection at once. Reserve it for a
required check that cannot go green (renamed job, dead runner, outage) and
re-apply afterwards — with `enforce_admins: true` nobody merges past a stuck
check without it.

## Corrections kept, so the next reader can tell a measurement from a derivation

1. An earlier revision listed 17 contexts and left `Contract gate (docs/CONTRACTS.md)`
   out on the grounds that its name had never been observed on a real PR — a derivation
   from `contract-gate.yml`. It has since been measured green on PR #401 in ~40 s, and it
   is in the ruleset. An unobserved required context parks `dev` at "Expected" forever,
   which is why the conservative call was made; the ruleset reading overrode it.
2. The previous revision's table carried a `gating` column that read **required** for
   every name in `REQUIRED_CHECKS`. That was a restatement of the script's intent
   dressed as enforcement, and it was wrong for six names. The column is replaced by
   measured membership, one row per check-run name, with the job that produces it.
3. An even earlier revision reconstructed membership by subtracting from a count. It
   landed on the right eleven names, which is exactly why it is recorded here: a good
   guess and a transcript are indistinguishable in the output and only the transcript
   survives the next ruleset edit. Membership is now quoted, and the count — 13 stored,
   12 unique — is stated next to the evidence that the two numbers differ.

Unverifiable from here: per-name conclusions and trigger behaviour can only be
confirmed against live runs, and the 422 behaviour described above depends on
the API version the repo's host is running.
