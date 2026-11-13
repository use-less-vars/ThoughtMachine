# CI_PROCESS.md — what CI actually runs, and what this repo cannot see

**What this file is.** Which workflows exist, which jobs gate a merge, which are advisory, how a collection floor stops
a suite passing vacuously, how to verify locally, and which CI facts are github.com state invisible from this tree.
Read `MANDATORY_STOP.md` first.

**How to update it.** Edit in place; do not regenerate. When a workflow changes, change the citation rather than the
summary: which manifest entries carry a recorded digest is a LIST, and the list is the count — `HASHED_MEMBERS` at
`tests/test_onboarding_bundle_contract.py:62`. The prose above that list once asserted a different number than the list
held; it now points at the list instead. History goes to `machine_history_and_vision.md`.

**Sourcing rule.** Every repo-side fact below is a `file:line` citation the reader can open. Every GitHub-side fact —
branch protection, rulesets, bypass allowlists — is marked SERVER-SIDE / NOT WITNESSED HERE: it is state on github.com,
not bytes in this checkout, so nothing read here can prove it either way. Where two KB entries disagree this file
quotes both and says the question is open. **Siblings:** `MANDATORY_STOP.md` (read first), `ORCHESTRATOR_GUIDE.md`,
`architecture_invariants.md`, `docs/ARCHITECTURE_MACHINE_MODEL.md`, `machine_history_and_vision.md`.

---

## 1. What actually runs

`.github/workflows/` holds exactly four files — `cross-platform-smoke.yml`, `first_start_ci.yml`, `platform-matrix.yml`,
`python-version-matrix.yml` — and all four fire on `push:` to `dev`/`master` and on every `pull_request`
(`cross-platform-smoke.yml:28-31`, `first_start_ci.yml:32-35`, `platform-matrix.yml:34-37`,
`python-version-matrix.yml:27-30`). None configures a ruleset, edits branch protection or merges a PR.

**cross-platform-smoke.yml — install and boot smoke on three OSes.** `linux-install` (`:43-45`) runs `bash install.sh`
(`:68`), asserts `--check-only` non-vacuously with `--expect-docker` (`:84`), then boots the backend and polls
`GET /api/health` for HTTP 200 (`:97-114`). `windows-smoke` (`:123-125`) runs
`powershell -ExecutionPolicy Bypass -File scripts\smoke_windows.ps1` (`:231`) — that flag and
`scripts/smoke_windows.ps1:8` are the only "bypass" matches in `.github/` or `scripts/`, a PowerShell flag and not a
merge-gate bypass. `macos-launcher` (`:252-254`) is gating but asserts the launcher class only; the full
`--check-only` run is omitted on purpose as flake-risk (`:247-250`).

**first_start_ci.yml — the installer on a pristine Debian 13 container.** One job (`:42-45`, `container: debian:13`)
pre-creates `$HOME/.thoughtmachine` because check [6/8] is fatal in every mode (`:64-67`), runs `bash install.sh`
(`:72`), then `--check-only` under `pipefail` asserted by `--expect-no-docker` (`:88-91`). Docker is absent BY
CONTRACT: no socket volume, and the observed outcome is a [2/8] WARNING with `reason: lib_missing` (`:12-25`) — the
one green check here that states a Docker limitation instead of hiding it.

**platform-matrix.yml — the suite and the browser gates.** Ten jobs. Blocking: `ubuntu-suite` (full `-m "not docker
and not e2e"`), `linux-install-smoke`, `linux-e2e-auth` (Playwright auth suites, `--run-e2e` at `:310`),
`windows-thin-pytest`, `web-ui-frontend-suite` (`npm ci` at `:522`). Advisory (`continue-on-error`): `macos-suite`
(`:413`), `web-ui-backend-suite` (`:452`), `macos-install-smoke` (`:565`), `macos-docker-probe` (`:687`). Every pytest
job asserts a collection FLOOR via `scripts/ci_assert_collected.py` — 3827 (`:77`), 1 (`:116`), 3 (`:294`), 77 (`:391`),
125 (`:474`) — and `:15-17` gives the reason: a suite that collects zero tests must not report green. `:121` is the
mirror trap: without `--run-e2e` the e2e items are skipped and the job passes having run nothing.

**python-version-matrix.yml — the Python floor and the installer contract.** `unit-tests` runs the suite on 3.11–3.14
(`:44`). `installer-contract` runs `bash -n` over every shell script (`:166`) and is the ONE CI step that gates this
documentation bundle — `./scripts/check_kb_staleness.sh --check` (`:171-172`) — plus grep assertions that both
installers stay floor-only and all five `.bat` files keep CRLF (`:232`). `windows-python-314` (`:234-236`) proves on a
real Windows runner that the 3.11+ floor accepts 3.14 and the old 3.11-3.13 ceiling would have rejected it.

Together these prove installers install, the backend boots and answers `/api/health`, suites collect above a floor
and pass, and the handoff bundle is not stale. Nothing here proves who may merge.

---

## 2. What the repo cannot see

Branch protection, rulesets and bypass allowlists are github.com settings; nothing in `.github/` yields them.
Grepping `admin|ruleset|bypass` across `.github/` and `scripts/` matches only PowerShell's `-ExecutionPolicy Bypass`
and a comment in `scripts/reap_container_record.py:138`; any gate was configured on the host. The KB contradicts itself:

- `project/operator_observations.md:314` (2026-10-03, PR #57): "**Edge 8 stands**; the ruleset + required checks held
  (13 rows ran, 13 succeeded). Do NOT conflate. Do NOT reopen Edge 8."
- `project/working_agreements.md:127`: `gh pr checks <PR> --required --watch` "returns 'no required checks reported'" —
  branch protection has no required checks configured, so every check is advisory and `--required` gates on the empty
  set; recommendation at `:142-144` to configure branch protection on `dev`/`master` with a non-advisory set.

Both cannot hold of one repository at one time; both may hold at different times, or one was generalised from
another repo — **the truth is unknown** until someone runs the three commands below, and this file does not guess:

```
gh api repos/:owner/:repo/rulesets
gh pr checks <PR> --required
gh api repos/:owner/:repo/branches/dev/protection
```

The first settles whether a ruleset exists and who sits in `bypass_actors`; the second whether the platform gates
anything today; the third returns `required_status_checks`. A 404 is evidence — no protection — not a retry prompt.

**OPERATOR-SUPPLIED / UNVERIFIED FROM REPO.** "Two rulesets" and an "`--admin` bypass list" are a report, not a
citation; nothing here confirms the count or the membership. `gh api .../rulesets` output showing two entries, each
with `bypass_actors`, dated and attributed, would promote them to fact; until then it stays a report.

---

## 3. The `--admin` hazard: a success line is not a state change

- `personal/bugs_and_fixes.md:1412` — PRs #331 (C30) and #332 (handoff) were reported merged with `gh pr merge --admin`;
  local `dev` **and** `origin/dev` both read `232a37d1`, whose log contains only "Merge pull request #331".
  `merge_base --is-ancestor 596419cd dev` → true; `6fbd99d5 dev` → not an ancestor.
- `:1536` — `gh pr merge 336 --admin --merge` for the Tier 0 branch "**never landed**"; that `dev` (`0ff6ae6c`) holds
  only PR #337.

The lesson is not that `--admin` is broken. A tool's success report describes the tool's exit, not the repository's
state — and `--admin` lets a merge step past a gate, so success-without-movement is its expected shape:

```
git_read merge_base    a=<sha-you-believe-landed>    b=dev    is_ancestor=true
```

A "merged" claim stays a claim until that returns true, and a branch cut from a `dev` lacking the work guarantees a
conflict or a lost change (the agent cannot rebase, so a stale base is unrecoverable from this side — `:1413`).

**Same failure class, another subsystem.** `c33a61e1` ("stop the resume route claiming 'resumed' when nothing
revived"): the route scanned an in-process registry, matched zero threads, and fell through to the same `200 {"status":
"resumed"}` body as a genuine revive. The fix is not a 404 — resume legitimately travels over a cross-process command
channel — it answers `200 {"status": "queued", "resumed_command_written": true, "in_process": false}`. When a path can
succeed without doing what its name says, the response must distinguish the two: every reader will believe the body.

---

## 4. C60 was not a CI problem

`PRIORITIES.md:282` records it closed: "**C60: Test Isolation Failures.** **FIXED** in `35c11fbc`". Cause, per
`personal/bugs_and_fixes.md:1841`: four fixtures assigned the class attribute `ProviderFactory._providers` directly
with no teardown, killing the factory's lazy population for the rest of the process, so later tests saw
`ProviderNotFoundError: ... Available: ['echotool']`. They are now `monkeypatch.setattr`-based with an autouse
`_restore_provider_registry` guard in `tests/conftest.py`, and the reproducing pair went 8 failed / 35 passed → 43
passed (`PRIORITIES.md:282-288`). Not infrastructure: order-dependence is a property of the tests, fixed in the tests.

**The durable rule, stated separately because it outlives C60.** A flaky test is a test bug: fix it, or quarantine it
explicitly and visibly — tracked C-item, named owner — never route around a gate to get green. A gate you routinely
bypass stops reporting anything: the next failure looks like noise. Hence the floors (`platform-matrix.yml:15-17`) and
the pedantic `--expect-no-docker` assertion (`first_start_ci.yml:91`): no check may pass by doing nothing.

---

## 5. Local verification before you push

Three commands cover what a docs change can break; outputs are what this generation saw in a worker container
(Python 3.12.15), not what the docs claim:

```
$ bash scripts/build_onboarding.sh
Bundle built: 170442 bytes.
$ python3 -m pytest tests/test_onboarding_bundle_contract.py tests/test_onboarding_scripts.py -q
24 passed, 25 warnings in 2.97s
$ bash scripts/check_kb_staleness.sh --check
NOTE: PRIORITIES.md carries no recorded sha256 (advisory entry); not a staleness failure.
PASS: HANDOFF.md is up to date.
```

**The NOTE is not a failure.** The gate prints it for the advisory entry on every run by design: that file is edited by
every branch touching a C-item, so its digest is branch-local and cannot signal staleness (script header, "ADVISORY
ENTRIES"). Only `WARNING:` lines fail, and only they set exit 1. CI runs the same script with `--check`
(`python-version-matrix.yml:171-172`), which is what makes a green local run worth anything. `--update` is the fix when
a manifest file legitimately changed and rewrites only the trailing `sha256:` tokens. The bundle is built from the
router `project/HANDOFF.md`, so a new member means editing that router and `scripts/gen_handoff.py:49-58` together.

---

## 6. If you are the next engineer starting here

- **Trust the files over the prose.** A workflow is executable truth; a summary of it, including this one, is a claim.
  Open `.github/workflows/<file>` before asserting what runs.
- **A contradicted KB entry is a bug to file, not a puzzle to resolve privately.** §2 is a live defect: cite both
  lines and let whoever has `gh` settle it; silently preferring one half inherits a confident wrong answer.
- **Verify merges yourself** — `is_ancestor` on the sha you believe landed, before cutting from `dev` (§3). Never act
  on a merge success line, including your own.
- **Never bypass a gate to go green.** Fix or quarantine the flake (§4); advisory is a status assigned deliberately,
  and a red blocking job is information.
- **The bundled docs are the contract.** `tests/test_onboarding_bundle_contract.py` pins the checked-in router against
  a fresh generation: set equality (`:349`), one advisory member (`:283`), no digest on it (`:286-288`).
- **A new KB markdown file needs its `.gitignore` negation** or it never shows in `git status`: `.gitignore:230` ignores
  `knowledge/project/**/*.md` wholesale and the `!` negations sit at `:282-285`.
