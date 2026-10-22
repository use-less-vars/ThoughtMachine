# CI and Branch Process

> **Branches push to origin before merge. Merge to `dev` only after CI is green on the pushed branch.**

This rule closes an enforcement gap: a branch can be merged locally without ever
being pushed, so CI never runs on it and the merge to `dev` lands unverified.
Pushing the branch first guarantees that the exact revision being merged has
already run and passed CI on origin.

## What runs on a PR

There are four workflows, all in `.github/workflows/`. Every one of them triggers
on `pull_request` (all PRs, any base branch) and on `push` limited to `dev` and
`master`:

| Workflow | Jobs | Notes |
| --- | --- | --- |
| `cross-platform-smoke.yml` | `linux-install`, `windows-smoke`, `macos-launcher` | all gating |
| `platform-matrix.yml` | `ubuntu-suite`, `linux-install-smoke`, `windows-import-smoke`, `windows-thin-pytest`, `macos-suite`, `macos-install-smoke`, `macos-docker-probe`, `web-ui-backend-suite` | 4 gating, 4 advisory |
| `python-version-matrix.yml` | `unit-tests` (Python 3.11–3.14), `installer-contract`, `windows-python-314` | all gating |
| `first_start_ci.yml` | `first-start` | gating |

So a PR on any branch is covered by all four. A **push** to any branch other than
`dev` or `master` runs nothing at all — which is the reason for the push-then-merge
rule above: an unpushed branch has no CI result, and a locally-merged branch never
gets one. Each workflow also sets `concurrency: group: <workflow>-${{ github.ref }}`
with `cancel-in-progress: true`, so a new push supersedes the previous run on the
same ref.

## Which jobs can fail a merge

"Cannot fail a merge" below has two distinct meanings, and they are worth keeping
apart:

- **Advisory job**: `continue-on-error: true` in the workflow file. Its failure is
  recorded but does not fail the workflow run.
- **Required status check**: GitHub branch protection. That configuration lives in
  the repository's GitHub settings, not in the tree, so **which checks are actually
  required cannot be determined from this repository**. What follows is only what
  the YAML enforces.

Gating (a failure fails its own workflow run):

- `cross-platform-smoke.yml` — `linux-install`, `windows-smoke`, `macos-launcher`.
- `platform-matrix.yml` — `ubuntu-suite`, `linux-install-smoke`,
  `windows-import-smoke`, `windows-thin-pytest`.
- `python-version-matrix.yml` — `unit-tests`, `installer-contract`,
  `windows-python-314`.
- `first_start_ci.yml` — `first-start`.

Advisory (`continue-on-error: true`, never fails its workflow):

- `platform-matrix.yml` — `macos-suite`, `macos-install-smoke`, `macos-docker-probe`.
- `platform-matrix.yml` — `web-ui-backend-suite` is likewise advisory, for an
  unrelated reason: `web_ui/backend/tests/` is outside the main `testpaths`, so it
  is collected and run as a separate advisory suite.

## What each job proves, and what it does not

Test-count claims are guarded: every pytest job in `platform-matrix.yml` first runs
`scripts/ci_assert_collected.py --expected <N>`, which fails the job if the
collection count drops (an empty selection cannot pass silently). The
`python-version-matrix.yml` unit lane has no such floor.

- **`ubuntu-suite`** — the authoritative suite: the full test tree minus the
  `docker` and `e2e` markers. Proves the Docker-free, host-only test suite on Linux.
  Does not prove anything Docker-gated, and does not prove the installer or startup.
- **`linux-install-smoke`** — the only job that walks the whole path: real
  `install.sh`, real `start_thoughtmachine.sh` on free ports, then one live turn
  through the running backend. Proves clone → install → start → answer, on Linux
  only.
- **`linux-install`** (cross-platform-smoke) — real `install.sh`, the full
  `--check-only` preflight (checked non-vacuous by
  `scripts/assert_check_only_not_vacuous.sh --expect-docker`), then the backend is
  started and `/api/health` must return 200. Proves installer + preflight + a served
  health endpoint on Linux. Does not prove the frontend, the test suite, or any
  other platform — `install.sh` only has to exit 0.
- **`windows-smoke`** — runs `install_thoughtmachine.bat` under real `cmd.exe` up to
  the `--- Node.js ---` banner (deliberately killed there), then venv + imports +
  `scripts/smoke_windows.ps1`, which polls `/api/health`. Proves the batch file's
  Python section parses and executes and the backend boots on Windows. Does not
  prove the batch file completes, and runs no pytest.
- **`windows-import-smoke`** / **`windows-thin-pytest`** — import of
  `web_ui.backend.server` only, and a thin Docker-free slice of four test files.
  Neither is the suite.
- **`macos-launcher`** — the only gating macOS job. Three assertions: the port check
  uses the `lsof` fallback and reports a real verdict; `--check-tools` marks `ss`
  critical and the launcher prints its Darwin skip marker; `--check-docker` reports
  a usable Docker or a named reason. Proves the launcher code paths. **Does not
  prove the application starts** — there is no uvicorn, no `/api/health`. That
  assertion is omitted deliberately (its preflight depends on venv/node/vault/locale
  state that cannot be proven deterministically), and the macOS Docker substrate
  (Docker Desktop + VirtioFS) is left to the advisory probe.
- **`macos-suite`** — the same suite as `ubuntu-suite`, advisory. Its result is
  recorded and readable, but it cannot fail a merge.
- **`macos-install-smoke`** — advisory install/start attempts that bypass the
  launcher and call `uvicorn` directly; the installer failure path is swallowed
  (`INSTALLER_EXIT=... (non-fatal: advisory job)`) and the health probe only logs
  `HEALTH_UNREACHABLE` rather than exiting non-zero. Treat a green run here as
  "nothing crashed hard", not as evidence that macOS startup works.
- **`macos-docker-probe`** — advisory: does the Docker CLI and Docker Desktop exist,
  does `docker info` answer, do container `--user` and `git` behave. Its own comment
  states what it cannot prove: macOS bind-mount host-ownership under VirtioFS/gRPC-FUSE
  is not native-Linux uid mapping.
- **`unit-tests`** (python-version-matrix) — the Linux suite on Python 3.11, 3.12,
  3.13 and 3.14, `fail-fast: false` so every version reports.
- **`installer-contract`** — static assertions about the installers themselves:
  shell syntax, that `install_thoughtmachine.sh` probes 3.14 first and pins
  `Python.Python.3.14`, that the `.bat` treats 3.14 as a floor rather than a ceiling,
  and a CRLF guard on the batch files. Proves the version contract, not that the
  installers run.
- **`first-start`** — real install + full `--check-only` on a pristine
  `debian:13` container. Docker is absent by contract there
  (`--expect-no-docker`), so this proves the installer and preflight on a clean
  Debian with no Docker; it proves no Docker-usable path and does not start the app.

## Gaps worth naming

- macOS is gated by one job, and that job cannot see whether the app starts. A macOS
  start-up regression has no RED to land on.
- Windows has no test-suite coverage — an import check and four test files.
- No workflow step builds or tests the frontend directly: Vite/npm appear nowhere in
  the four workflow files, so whatever `install.sh` does with Node is unasserted
  beyond "the installer exited 0".
- Docker-marked and `e2e`-marked tests are excluded from every suite job except
  `linux-install-smoke`, which runs one of them on Linux only.
