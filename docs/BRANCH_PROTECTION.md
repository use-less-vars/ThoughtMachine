# Branch protection for `dev`

Derived 2026-11-18 (C93) by reading the six files in `.github/workflows/`, not by observing a run. Enforced by `scripts/apply_branch_protection.sh` (dry run by default).

## What a required context is

GitHub matches a required status check by its **check-run name**: the job's `name:` (the bare job id when `name:` is absent), with `${{ matrix.x }}` interpolated. The workflow-level `name:` is NOT part of it, so no two workflows may reuse a job name, and renaming a job silently un-gates the branch. `runs-on`, `container:` and step-level `if:` do not change the name; a matrix job yields one check per combination.

## Required contexts (17)

| workflow | job id | check-run name |
| --- | --- | --- |
| platform-matrix.yml | ubuntu-suite | Ubuntu - full test suite |
| platform-matrix.yml | linux-install-smoke | Linux - install/startup smoke (blocking) |
| platform-matrix.yml | linux-e2e-auth | Linux - auth e2e (browser) |
| platform-matrix.yml | windows-import-smoke | Windows - import smoke |
| platform-matrix.yml | windows-thin-pytest | Windows - thin pytest slice |
| platform-matrix.yml | web-ui-backend-suite | web-ui-backend-suite |
| platform-matrix.yml | web-ui-frontend-suite | web-ui-frontend-suite |
| python-version-matrix.yml | unit-tests | Unit tests - Python 3.11 / 3.12 / 3.13 / 3.14 (4 checks) |
| python-version-matrix.yml | installer-contract | Installer version-contract assertions |
| python-version-matrix.yml | windows-python-314 | Windows Python 3.14 gate proof |
| cross-platform-smoke.yml | linux-install | Linux install + check-only + backend smoke |
| cross-platform-smoke.yml | windows-smoke | Windows venv + smoke script |
| cross-platform-smoke.yml | macos-launcher | macOS launcher preflight (gating) |
| first_start_ci.yml | first-start | Install + check-only |

Every workflow above triggers on `pull_request` (contract-gate.yml on `[dev]` only, the others unqualified), so each PR head commit reports all of these.

## Not required yet (commented out in the script)

Four names are out of `contexts`, for two different reasons: one whose exact check-run name is still unconfirmed, three advisory jobs.

* **`Contract gate (docs/CONTRACTS.md)`** — pending empirical name confirmation. `contract-gate.yml` does run on `pull_request: branches: [dev]` (plus `workflow_dispatch`), so the name above is a derivation from the workflow file, never an observed check on a real PR — and that gap is exactly why it stays out: a required context that matches no reported check is worse than a missing one, because `dev` becomes permanently unmergeable (every PR parked at "Expected — waiting for status to be reported") and with `enforce_admins: true` nothing merges past it — only `--unprotect` does. **Follow-up:** on the next PR to `dev`, read the real check name off that PR (`gh pr checks <n>` or the Checks tab), add the exact string to `REQUIRED_CHECKS`, and re-run the script with `--apply`.
* **The three macOS advisory names** (`macOS - full test suite (advisory)`, `macOS - install/startup smoke (advisory)`, `macOS - Docker availability probe (advisory)`) — all carry `continue-on-error: true` in `platform-matrix-macos.yml`; an advisory job exists so a red macOS runner cannot block a merge, and requiring it would invert that.

`Linux - auth e2e (browser)` IS required even though its steps skip when no auth-relevant file changed: the job itself has no `if:`, so it still reports, and a skip reports success.

## Policy chosen in the payload

`enforce_admins: true` (no admin bypass), `required_approving_review_count: 0` (a PR is required, a second human is not — a PR author cannot approve their own PR, so `1` plus `enforce_admins` deadlocks a single-maintainer repo), `strict: false` (no "branch must be up to date" churn on a long CI), force-push and deletion denied, stale reviews dismissed, conversation resolution required, linear history off.

## Applying and verifying

```
scripts/apply_branch_protection.sh            # print payload + command, change nothing
scripts/apply_branch_protection.sh --apply    # PUT it (needs gh + an admin token)
gh api repos/use-less-vars/ThoughtMachine/branches/dev/protection \
  --jq '.required_status_checks.contexts[]'   # after: read back what is enforced
```

Emergency hatch: `--unprotect --apply` runs `gh api --method DELETE .../protection`, dropping reviews, required checks, `enforce_admins`, force-push and deletion protection at once. Reserve it for a required check that cannot go green (renamed job, dead runner, outage) and re-apply afterwards — with `enforce_admins: true` nobody merges past a stuck check without it.

Unverifiable from here: names and per-job conclusions can only be confirmed against a live run. Also unknown offline: whether GitHub counts a `continue-on-error` check run as passing for a required context — the second reason the macOS names stay out.
