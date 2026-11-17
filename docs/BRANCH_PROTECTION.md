# Branch protection for `dev`

Measured 2026-11-18 (C93) off the check-runs API on PR #401, not derived from
the workflow files. Enforced by `scripts/apply_branch_protection.sh` (dry run by
default).

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

## Required contexts (18 gating, 21 measured)

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
| macOS - full test suite (advisory) | platform-matrix-macos.yml | macos-suite | advisory — not required |
| macOS - install/startup smoke (advisory) | platform-matrix-macos.yml | macos-install-smoke | advisory — not required |
| macOS - Docker availability probe (advisory) | platform-matrix-macos.yml | macos-docker-probe | advisory — not required |

The four `Unit tests - Python 3.x` names are ONE job in
`python-version-matrix.yml` whose `name:` interpolates `matrix.python-version`;
adding or dropping a version there means editing `REQUIRED_CHECKS` too.

`Linux - auth e2e (browser)` is required even though its steps skip when no
auth-relevant file changed: the job itself has no `if:`, so it still reports,
and a skip reports success.

Every required name belongs to a job that is merge-blocking in its own workflow
(no `continue-on-error`) **and** triggered by `pull_request`, so the check runs
on every PR head commit. `contract-gate.yml` triggers on `pull_request:
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

The three macOS names stay out, and for a measured reason rather than a guessed
one: on PR #401, `macOS - full test suite (advisory)` **concluded failure** while
its workflow run stayed green (`continue-on-error: true`). So these checks do
report, and they can report red without blocking anything. Requiring one would
either block a merge on the very red macOS runner the advisory flag exists to
absorb, or — if GitHub does not count a `continue-on-error` check as passing for
a required context — park `dev` at "Expected". Neither is wanted.

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
  Informational: the three macOS advisory names are *meant* to appear here, and
  so does every newly added job. Exits **0**.

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
