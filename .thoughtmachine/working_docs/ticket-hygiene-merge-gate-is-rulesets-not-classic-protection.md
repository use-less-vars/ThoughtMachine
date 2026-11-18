# Ticket: hygiene/merge-gate-is-rulesets-not-classic-protection

**Filed:** 2026-11-18, same day as the measurement this ticket is built from (the C93
branch-protection pass), at `dev` tip `26791d6b`. Every measured number below is from that pass.
**Severity:** HIGH — merge-blocking, and the in-flight "fix" (PR #401) would manufacture a **third**
parallel gate instead of removing the one that exists.
**Status:** filed, not dispatched. Read "Why PR #401 does not fix it" before touching
`scripts/apply_branch_protection.sh` or `docs/BRANCH_PROTECTION.md` (both live on branch
`c93-branch-protection`; neither is on `dev`).
**Evidence tags:** **[M]** = measured in that pass (API output, verbatim). **[D]** = derived by
set/vocabulary comparison of two measured lists. **[?]** = not established; listed under
"Open questions".

## Symptom

PRs cannot be merged into `dev` while every gating check on the PR is green, and the error names a
policy that the obvious inspection tool says does not exist:

- `gh pr merge` on a fully green PR → `the base branch policy prohibits the merge`. **[M]**
- `gh api repos/use-less-vars/ThoughtMachine/branches/dev/protection` → HTTP **404**,
  body `Branch not protected`. **[M]** (this is the slug the script itself defaults to,
  `REPO_SLUG="${REPO_SLUG:-use-less-vars/ThoughtMachine}"`)

The repo's own answer to "what enforces `dev`?" — `scripts/apply_branch_protection.sh` plus
`docs/BRANCH_PROTECTION.md` — is written entirely against the endpoint that answers 404. So the
documented remedy cannot reach the policy that is actually blocking merges, and the documented
emergency hatch is a no-op.

## Measured evidence

The gate is **two GitHub repository rulesets**, both `target: branch`, both `enforcement: active`,
both matching the same two refs:

| ruleset id | name | rule types | conditions.ref_name.include |
| --- | --- | --- | --- |
| `24214086` | `dev-1` | `deletion`, `non_fast_forward`, `required_status_checks` | `refs/heads/dev`, `refs/heads/master` |
| `24214087` | `dev-2` | `required_status_checks` (only) | `refs/heads/dev`, `refs/heads/master` |

Required status-check contexts, **verbatim**:

`dev-1` (10 entries, one duplicated):
`Ubuntu - full test suite`, `Linux - install/startup smoke (blocking)`,
`Linux install + check-only + backend smoke`, `macOS launcher preflight (gating)`,
`Install + check-only`, `Unit tests - Python 3.11`, `Unit tests - Python 3.12`,
`Unit tests - Python 3.12` *(duplicate)*, `Unit tests - Python 3.13`,
`Installer version-contract assertions`

`dev-2` (10 entries, no duplicate, 3.14 present):
the same list **without** the duplicate and **with** `Unit tests - Python 3.14` added.

GitHub enforces the **union** of all matching rulesets → effective required set = **10 distinct
names**: `Ubuntu - full test suite`, `Linux - install/startup smoke (blocking)`,
`Linux install + check-only + backend smoke`, `macOS launcher preflight (gating)`,
`Install + check-only`, `Unit tests - Python 3.11`, `Unit tests - Python 3.12`,
`Unit tests - Python 3.13`, `Unit tests - Python 3.14`,
`Installer version-contract assertions`. **[M]/[D]**

Classic branch protection: **off** (the 404 above). **[M]**

PR #401 reported **21** check-runs: **18** gating, **3** advisory (`continue-on-error: true`) —
`macOS - full test suite (advisory)`, `macOS - install/startup smoke (advisory)`,
`macOS - Docker availability probe (advisory)`. All three are correctly absent from both rulesets.
**[M]**

## Root cause

Two overlapping, individually stale rulesets own one fact. Neither is authoritative, nothing in the
tree says which one is, and the enforced set is their union — so the gate is correct only by
accident:

- `dev-1` lists `Unit tests - Python 3.12` twice and has **no** `Unit tests - Python 3.14`.
  `dev-2` has 3.14 and no duplicate. Neither list alone is the intended set; the union is.
- Neither ruleset requires **8** checks that `docs/BRANCH_PROTECTION.md` and the script's
  `REQUIRED_CHECKS` call required: `Contract gate (docs/CONTRACTS.md)`, `Linux - auth e2e (browser)`,
  `web-ui-backend-suite`, `web-ui-frontend-suite`, `Windows - import smoke`,
  `Windows Python 3.14 gate proof`, `Windows - thin pytest slice`, `Windows venv + smoke script`.
  **[D]** (set difference of the two measured lists; the 18-name list is reproduced under
  "The intended list, verbatim"). So the *enforced* gate today is **weaker** than the documented
  intent while being *unfixable* from the file that documents it.
- Classic branch protection — the only surface the repo's tooling knows — is not in use at all.

What is **not** established: which term actually produces `the base branch policy prohibits the
merge` for PR #401. The union (10 names) is a subset of the 18 green gating check-runs, so a
name-match block is not self-evident; the plausible remaining terms are
`strict_required_status_checks_policy` on either ruleset (branch must be up to date), a
review/conversation rule inside a ruleset, or a required name failing at the moment of the attempt.
All **[?]** — see "Open questions" for the probes. The ticket does not need that answer to be
correct about the wrong-API problem, and must not pretend to have it.

## Why PR #401 does not fix it

PR #401 adds `scripts/apply_branch_protection.sh` (mode 755) and `docs/BRANCH_PROTECTION.md` on
branch `c93-branch-protection` — both good documents about the wrong object.

1. **Wrong endpoint.** The script's whole effect is
   `gh api --method PUT repos/${REPO_SLUG}/branches/${BRANCH}/protection --input -`. That is the
   *classic* branch-protection endpoint. On this repo it 404s on GET, i.e. no classic protection
   exists; a successful PUT would **create** one rather than correct the rulesets. **[M]**
2. **A third parallel gate, not a replacement.** Classic protection and rulesets are both
   enforced; their requirements union. Applying the script would leave both stale rulesets in place
   and *add* the script's full 18-name list on top — newly requiring the 8 checks the rulesets do
   not require. That is strictly more policy than the operator asked for, produced by a tool whose
   stated purpose is "put the gates under enforcement".
3. **The names are right, the authority is wrong.** The 18 names were measured off the check-runs
   API (not derived from the workflow `name:` display) — that part is correct and worth keeping.
   What they lack is an owner: they are not the list anything enforces today.
4. **The emergency hatch is a no-op.** `--unprotect --apply` issues
   `DELETE .../branches/<branch>/protection`, which on this repo deletes nothing (404) and leaves
   the real gate intact — precisely the situation the hatch exists for ("a required check cannot go
   green and `dev` is unmergeable").
5. **The read-back step is also 404.** `docs/BRANCH_PROTECTION.md` ends with
   `gh api .../branches/dev/protection --jq '.required_status_checks.contexts[]'` as the way to see
   what is enforced. It shows nothing here.
6. Neither file mentions rulesets. A future reader concludes `dev` is unprotected and "fixes" that
   by adding a gate.

## The intended list, verbatim (from `scripts/apply_branch_protection.sh`, `REQUIRED_CHECKS`)

Kept here so this ticket is self-contained; 18 names, measured order:

```
Contract gate (docs/CONTRACTS.md)            # job contract-gate (contract-gate.yml)
Install + check-only                         # job first-start (first_start_ci.yml)
Installer version-contract assertions        # job installer-contract (python-version-matrix.yml)
Linux - auth e2e (browser)                   # job linux-e2e-auth (platform-matrix.yml)
Linux install + check-only + backend smoke   # job linux-install (cross-platform-smoke.yml)
Linux - install/startup smoke (blocking)     # job linux-install-smoke (platform-matrix.yml)
macOS launcher preflight (gating)            # job macos-launcher (cross-platform-smoke.yml)
Ubuntu - full test suite                     # job ubuntu-suite (platform-matrix.yml)
Unit tests - Python 3.11                     # job unit-tests (python-version-matrix.yml)
Unit tests - Python 3.12                     #   (matrix.python-version)
Unit tests - Python 3.13
Unit tests - Python 3.14
web-ui-backend-suite                         # job web-ui-backend-suite (platform-matrix.yml)
web-ui-frontend-suite                        # job web-ui-frontend-suite (platform-matrix.yml)
Windows - import smoke                       # job windows-import-smoke (platform-matrix.yml)
Windows Python 3.14 gate proof               # job windows-python-314 (python-version-matrix.yml)
Windows - thin pytest slice                  # job windows-thin-pytest (platform-matrix.yml)
Windows venv + smoke script                  # job windows-smoke (cross-platform-smoke.yml)
```

The three `(advisory)` macOS names are deliberately excluded there, and correctly excluded from both
rulesets. Keep that.

## Remediation plan

Order matters: capture before mutating.

1. **Capture, don't guess.** `gh api repos/<slug>/rulesets/24214086 > /tmp/dev-1.json` and the same
   for `24214087`. The current gate exists only as remote state; there is no copy in the tree.
2. **One authoritative list.** Move the 18 names into a single data file owned by the script —
   one name per line, `#` comments allowed, e.g. `scripts/ci/required_checks.txt` — instead of a
   bash array with inline comments. One fact, one owner; the docs table becomes generated prose at
   best, not a second source.
3. **Rewrite the script against the Rulesets API.** `GET/POST/PUT repos/<slug>/rulesets`
   (+ `/rulesets/{id}`), `target: "branch"`, `enforcement: "active"`, `bypass_actors: []`,
   `conditions.ref_name.include: ["refs/heads/dev"]`,
   `rules: [{type:"deletion"},{type:"non_fast_forward"},
   {type:"required_status_checks", parameters:{strict_required_status_checks_policy: <ruling>,
   required_status_checks:[{context:"<name>"}, ...]}}]`. Keep the current shape otherwise:
   **dry run by default**, `--apply` to send, payload printed so the dry run is the record.
4. **Idempotence by name, not by id.** Look the ruleset up by `name` (e.g. `dev`), create if absent,
   `PUT` if present; never hard-code `24214086`/`24214087`. Second run must be a no-op.
5. **Consolidate.** Create/update the one ruleset to the intended set **first**, then delete the
   stale one(s) — never the reverse (that window leaves `dev` unprotected).
6. **Verify mode.** `--verify` re-reads the live rulesets and diffs their required contexts against
   the data file **in both directions** (missing → blockable-but-not-enforced; extra → enforced-but-
   not-intended, which is how the `3.12` duplicate and the missing `3.14` hid for so long). Keep the
   existing local `--verify-names FILE` idea for the "does every required name actually report on a
   PR?" direction.
7. **Docs.** Rewrite `docs/BRANCH_PROTECTION.md` around rulesets: the two live ids/names as history,
   the single intended ruleset as the rule, and the 404 as the reason the old text was wrong.

## Acceptance criteria

- `gh api repos/<slug>/rulesets` shows **exactly one** active ruleset whose
  `conditions.ref_name.include` contains `refs/heads/dev`. (The `master` question below is a ruling,
  not a blocker for this criterion as written.)
- That ruleset's required-context **set equals** the data file: no missing name, no extra name, no
  duplicates.
- None of the three `(advisory)` names is required.
- Running the rewritten script twice: the second run reports no change and issues no mutating call.
- `--verify` catches a **deliberately misspelled** name (demonstrated by editing one line and seeing
  it reported, not asserted in prose).
- No file in the tree still calls `repos/<slug>/branches/<branch>/protection` as a way to change or
  read the gate.

## Anti-goals / do not

- **Do not run `scripts/apply_branch_protection.sh --apply` (or `--unprotect --apply`) against this
  repo** until it speaks the Rulesets API. The PUT either 404s or manufactures a third gate; the
  DELETE is a silent no-op that will read as "protection removed" while `dev` stays blocked.
- **Do not delete either ruleset before capturing its exact payload.** `24214086` /
  `24214087` are the only place the current gate is written down.
- **Do not "fix" the drift by editing workflow job names** to match the rulesets. The 10-name union
  is the stale side; the 18-name list is the measured, intended side.
- Do not change `refs/heads/master` policy as a drive-by — both live rulesets cover it (see below).
- Do not add a check to the required set to "see if it helps" — an unrequired-but-required name
  parks every PR at `Expected -- waiting for status to be reported`.

## This is not

- Not a CI-flakiness ticket. The check-runs on PR #401 are green; this is about which of them the
  branch policy listens to.
- Not a claim that the 18 names are wrong. They were measured; the ticket defends keeping them.
- Not a claim about *which* live ruleset is "the real one" — both are, that is the defect.

## Open questions (need a ruling; do not implement blind)

1. **`strict_required_status_checks_policy` on both rulesets — UNKNOWN.** Not read in the pass.
   It decides whether "PR branch up to date with `dev`" is also enforced, and it is the leading
   candidate for the actual blocking term (see Root cause). Probe:
   `gh api repos/<slug>/rulesets/24214086 --jq '.rules[] | select(.type=="required_status_checks").parameters.strict_required_status_checks_policy'`
   and the same for `24214087`.
2. **Should `refs/heads/master` be gated at all?** Both rulesets include it; nothing in the tree
   documents why, and the script targets `dev` only (`BRANCH="${BRANCH:-dev}"`). Whatever is
   decided must be decided once, for the consolidated ruleset.
3. **Should `Contract gate (docs/CONTRACTS.md)` be required?** It is in the intended 18, runs green
   on PR #401 in ~40 s, and its workflow triggers on `pull_request` only (no `push:`), so a direct
   push to `dev` never runs it. Requiring it is consistent with the rest of the list; the alternative
   is to drop it from the list deliberately rather than by omission (which is what the rulesets
   currently do).
4. **Which term actually blocks PR #401 — UNKNOWN.** Probe once the strict flags are known:
   `gh pr view 401 --json mergeStateStatus,statusCheckRollup` and, if it is still blocked with a
   green rollup, re-read both rulesets' full `rules` array (reviews, conversation resolution).
