# Branch protection for `dev`

Measured 2026-11-18 (C93) off the check-runs API on PR #401, not derived from
the workflow files. Enforced by `scripts/apply_branch_protection.sh` (dry run by
default).

Read that last sentence as *intended*, not *effective*. The script targets the
**classic** branch-protection endpoint, which answers 404 on this repo; the gate
that actually blocks merges is a repository ruleset. So `REQUIRED_CHECKS`, and the
eighteen-row table further down, are what the script asks for, while what is
enforced is the eleven contexts measured in the next section.

## Enforced on `dev` today: 11 unique contexts (measured 2026-11-19)

Classic protection 404s on this repo, so the only surface that answers this
question is the repository ruleset. The instrument — run it rather than trusting
the list below, which will drift before this file is edited again:

```
gh api repos/:owner/:repo/rulesets/24214086 --jq '.rules[] |
  select(.type=="required_status_checks") |
  .parameters.required_status_checks[].context'
```

That returns **twelve** entries for **eleven** unique contexts. The duplicate is
`Unit tests - Python 3.12`, stored adjacent to itself at positions 7 and 8 of the
stored order — which is the entire reason the stored count and the enforced count
disagree. Nothing is broken by it, but without that sentence the twelve-versus-eleven
gap reads as a measurement error and gets "corrected" in the wrong direction. The
order below is the order the call returns, not an order anyone chose: it is neither
alphabetical nor grouped by workflow.

**Verbatim, as the operator pasted it on 2026-11-19** — twelve entries, stored order,
duplicate intact. This is a transcript, not a reconstruction: every name below is
copied, and nothing in the block was inferred from a count.

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
```

De-duplicated and sorted, those are the eleven unique contexts — the same eleven,
spelled the way `scripts/apply_branch_protection.sh` spells them:

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
```

Seven names that `REQUIRED_CHECKS` asks for are **not** enforced:
`Linux - auth e2e (browser)`, `web-ui-backend-suite`, `web-ui-frontend-suite`,
`Windows - import smoke`, `Windows Python 3.14 gate proof`,
`Windows - thin pytest slice`, `Windows venv + smoke script`. Every one of those
is a check that runs, can go red, and stops no merge.

None of the seven appears in the transcript above. That is a stronger statement than
the one this sentence made an hour ago: the list was then derived by subtraction from
a total, and it is now read off by name.

**Provenance, and the one paragraph of this file that evidence rewrote.** The eighteen
declared names are byte-exact from `REQUIRED_CHECKS`
(`scripts/apply_branch_protection.sh:78-96`). The enforced twelve are byte-exact from
the operator's unedited paste of the instrument's output, reproduced above in the
order it returns. An earlier version of this paragraph reconstructed the membership
instead — eleven out of eighteen by subtraction, three exclusions re-confirmed by
name, four carried over from a separate capture. That reconstruction landed on
exactly these eleven names, and it is recorded here rather than tidied away, because
the next reader should be able to tell a transcript from a good guess and should not
conclude from this file that counting is as good as quoting. It is not: the seven
absences above are now read off the transcript, where before they were inferred from
a total. If the command and this file disagree, the command wins and this file is what
gets edited.

**A second reading of the same ruleset — and the two views disagree.** The transcript
above is the earlier reading of ruleset 24214086: twelve stored entries, eleven
unique. A later reading of the same ruleset (operator, `gh api`) returns **thirteen**
stored entries and **twelve** unique contexts — `web-ui-frontend-suite` has been
added — while the ruleset page itself renders only **twelve** rows, because the UI
**de-duplicates** the repeated `Unit tests - Python 3.12`. That divergence is why, on
the operator's account, a duplicate could survive months of people reading that page:
the two views differ by exactly one row, only the API tells the truth, and a reader
looking at the page had no reason to suspect it. The transcript above is kept as what
it is — a transcript of one reading, not the current set. **Open:** the seven names
`REQUIRED_CHECKS` asks for and the ruleset did not enforce were read off that
transcript; if `web-ui-frontend-suite` was among them, six remain. That diff has
**not** been re-measured against the new reading.

## What a required context is

GitHub matches a required status check by its **check-run name**: the bare job
`name:` (the bare job id when `name:` is absent), with `${{ matrix.x }}`
interpolated. The workflow-level `name:` is NOT part of it, so no two workflows
may reuse a job name, and renaming a job silently un-gates the branch.
`runs-on`, `container:` and step-level `if:` do not change the name; a matrix job
yields one check per combination.

## How the names were measured

```
gh api repos/use-less-vars/ThoughtMachine/commits/<PR #401 head sha>/check-runs \
  --jq '.check_runs[].name'
```

That prints one bare name per reported check run; 21 distinct names came back.
`gh pr checks <n>` is **not** usable for this: it prints a grouped
`<workflow> / <job>` display form, and contexts derived from that display name
checks no job ever reports. Every name below is a literal string observed in
that API response.

## Declared contexts: the 18 `REQUIRED_CHECKS` asks for (21 measured on PR #401)

| check-run name | workflow | job id | gating |
| --- | --- | --- | --- |
| Contract gate (docs/CONTRACTS.md) | contract-gate.yml | contract-gate | **required** |
| Install + check-only | first_start_ci.yml | first-start | **required** |
| Installer version-contract assertions | python-version-matrix.yml | installer-contract | **required** |
| Linux - auth e2e (browser) | platform-matrix.yml | linux-e2e-auth | **required** |
| Linux install + check-only + backend smoke | cross-platform-smoke.yml | linux-install | **required** |
| Linux - install/startup smoke (blocking) | platform-matrix.yml | linux-install-smoke | **required** |
| macOS launcher preflight (gating) | cross-platform-smoke.yml | macos-launcher | **required** |
| Ubuntu - full test suite | platform-matrix.yml | ubuntu-suite | **required** |
| Unit tests - Python 3.11 | python-version-matrix.yml | unit-tests (`matrix.python-version` = 3.11) | **required** |
| Unit tests - Python 3.12 | python-version-matrix.yml | unit-tests (3.12) | **required** |
| Unit tests - Python 3.13 | python-version-matrix.yml | unit-tests (3.13) | **required** |
| Unit tests - Python 3.14 | python-version-matrix.yml | unit-tests (3.14) | **required** |
| web-ui-backend-suite | platform-matrix.yml | web-ui-backend-suite | **required** |
| web-ui-frontend-suite | platform-matrix.yml | web-ui-frontend-suite | **required** |
| Windows - import smoke | platform-matrix.yml | windows-import-smoke | **required** |
| Windows Python 3.14 gate proof | python-version-matrix.yml | windows-python-314 | **required** |
| Windows - thin pytest slice | platform-matrix.yml | windows-thin-pytest | **required** |
| Windows venv + smoke script | cross-platform-smoke.yml | windows-smoke | **required** |
| macOS - full test suite (advisory) | platform-matrix-macos.yml | macos-suite | not required (gating) |
| macOS - install/startup smoke (advisory) | platform-matrix-macos.yml | macos-install-smoke | not required (gating) |
| macOS - Docker availability probe (advisory) | platform-matrix-macos.yml | macos-docker-probe | not required (gating) |

Only eleven of those eighteen are enforced today — see *Enforced on `dev` today*
above. The `gating` column records the script's intent, not the ruleset's state.

The four `Unit tests - Python 3.x` names are ONE job in
`python-version-matrix.yml` whose `name:` interpolates `matrix.python-version`;
adding or dropping a version there means editing `REQUIRED_CHECKS` too.

`Linux - auth e2e (browser)` is in `REQUIRED_CHECKS` but is **not** one of the
eleven enforced contexts. It is worth knowing what it would mean if it were added:
its steps skip when no auth-relevant file changed, the job itself has no `if:`, so
it still reports, and a skip reports success. A required check that usually
executes nothing is acceptable — it blocks exactly the PRs that touch auth files,
and it fails when those break — but it must never be counted as coverage.

Every name in `REQUIRED_CHECKS` belongs to a job that is merge-blocking in its own workflow
(no `continue-on-error`) **and** triggered by `pull_request`, so the check runs
on every PR head commit. That is a property of the workflow, not of the ruleset: a
job can be merge-blocking there and still be absent from the required set, which
is C105. `contract-gate.yml` triggers on `pull_request:
branches: [dev]` plus `workflow_dispatch` — it has **no `push:` trigger**, so a
direct push to `dev` does not run it. Branch protection needs only the
`pull_request` trigger, which is what it has.

## Correction to the previous revision

The earlier version of this file listed 17 contexts and left
`Contract gate (docs/CONTRACTS.md)` out on the grounds that its name had never
been observed on a real PR — a derivation from `contract-gate.yml`, not a
measurement. It has now been measured: it reports green on PR #401 in roughly
40 s, and it is in `REQUIRED_CHECKS`. The reasoning that kept it out was sound
(an unobserved required context parks `dev` at "Expected" forever); the
evidence that overrode it is newer.

The three macOS names stay out, and what keeps them out has changed. On PR #401,
`macOS - full test suite (advisory)` **concluded failure** while its workflow run
stayed green, because the job carried `continue-on-error: true`. That flag is
gone: a red macOS job now fails the `platform-matrix-macos.yml` run, so the
failure is visible in the run conclusion. The three names are still not
required, and that is now a recorded decision rather than a consequence of the
workflow text: the same job was measured red, and requiring it would block a
merge on that runner rather than on the code. If it is ever required, do it on a
measured green run, and note that these are the only contexts in this table
whose job gates its own workflow while protection ignores it.

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
  `enforce_admins: true` only `--unprotect` gets past it. Exits **1**.
* **present but not required** → a check runs that protection does not gate on.
  Informational: the three macOS names are *meant* to appear here — they gate
  their own workflow and are not required — and so does every newly added job.
  Exits **0**.

Exit 2 is a usage error (file missing or unreadable). Run it after any job
rename, and after any change to `REQUIRED_CHECKS`.

## Policy chosen in the payload

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

Unverifiable from here: per-name conclusions and trigger behaviour can only be
confirmed against live runs, and the 422 behaviour described above depends on
the API version the repo's host is running.
