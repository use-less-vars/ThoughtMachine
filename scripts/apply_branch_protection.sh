#!/usr/bin/env bash
# apply_branch_protection.sh -- put this repo's CI gates under enforcement.
#
# WHAT THIS DOES
#   Builds ONE fixed branch-protection payload for `dev` and `gh api` PUTs it.
#   Dry run BY DEFAULT: with no arguments it prints the payload and the exact
#   command it would run. Nothing touches GitHub until --apply is passed.
#
# WHY THE CONTEXTS ARE HARD-CODED
#   GitHub matches a required status check by its CHECK-RUN NAME, which is the
#   job's `name:` (the job id when `name:` is absent), with matrix values
#   interpolated. Those names were derived by hand from the six files in
#   .github/workflows/ -- the derivation, the per-name trigger and the names
#   deliberately NOT required are documented in docs/BRANCH_PROTECTION.md.
#   Re-derive both together: a renamed job silently un-gates the branch, since
#   a required context that no job reports is either "expected" forever or
#   matched by nothing.
#
# USAGE
#   scripts/apply_branch_protection.sh                  # dry run (default)
#   scripts/apply_branch_protection.sh --apply          # really PUT it
#   scripts/apply_branch_protection.sh --unprotect      # dry run of the DELETE
#   scripts/apply_branch_protection.sh --unprotect --apply
#   scripts/apply_branch_protection.sh --help
#
# ENVIRONMENT
#   REPO_SLUG   owner/name   (default: use-less-vars/ThoughtMachine)
#   BRANCH      branch       (default: dev)
#
# EMERGENCY HATCH -- read this before you need it
#   `--unprotect --apply` issues
#     gh api --method DELETE repos/<slug>/branches/<branch>/protection
#   and removes EVERYTHING at once: required reviews, required status checks,
#   enforce_admins, force-push and deletion protection. Use it only when a
#   required check cannot possibly go green (a renamed job, a dead runner, an
#   outage) and `dev` is otherwise unmergeable. Then fix the cause and
#   re-apply this script; the dry run output is the record of what the rule is
#   supposed to be, so paste it into the incident note.
#
# NOTE: scripts/*.sh is parsed by `bash -n` in the installer-contract job of
# python-version-matrix.yml -- keep this file parseable, no bashisms outside
# bash 4.
set -uo pipefail

REPO_SLUG="${REPO_SLUG:-use-less-vars/ThoughtMachine}"
BRANCH="${BRANCH:-dev}"
APPLY=0
UNPROTECT=0

# ---------------------------------------------------------------------------
# Required contexts: INCLUDED.
#
# Every entry below belongs to a job that (a) is merge-blocking in its own
# workflow and (b) is triggered by `pull_request`, so the check runs on every
# PR head commit. Workflows that also push to dev are noted, but the
# `pull_request` trigger is the one branch protection needs.
# ---------------------------------------------------------------------------
REQUIRED_CHECKS=(
  # .github/workflows/platform-matrix.yml  (pull_request + push dev,master)
  "Ubuntu - full test suite"                    # job ubuntu-suite
  "Linux - install/startup smoke (blocking)"    # job linux-install-smoke
  "Linux - auth e2e (browser)"                  # job linux-e2e-auth
  "Windows - import smoke"                      # job windows-import-smoke
  "Windows - thin pytest slice"                 # job windows-thin-pytest
  "web-ui-backend-suite"                        # job web-ui-backend-suite
  "web-ui-frontend-suite"                       # job web-ui-frontend-suite

  # .github/workflows/python-version-matrix.yml  (pull_request + push dev,master)
  # The matrix job's name interpolates matrix.python-version, so ONE job yields
  # FOUR check-run names. Adding or dropping a version in that file means
  # editing this list too.
  "Unit tests - Python 3.11"                    # job unit-tests, matrix
  "Unit tests - Python 3.12"
  "Unit tests - Python 3.13"
  "Unit tests - Python 3.14"
  "Installer version-contract assertions"       # job installer-contract
  "Windows Python 3.14 gate proof"              # job windows-python-314

  # .github/workflows/cross-platform-smoke.yml  (pull_request + push dev,master)
  "Linux install + check-only + backend smoke"  # job linux-install
  "Windows venv + smoke script"                 # job windows-smoke
  "macOS launcher preflight (gating)"           # job macos-launcher (gating by design)

  # .github/workflows/first_start_ci.yml  (pull_request unqualified + push dev,master)
  "Install + check-only"                        # job first-start

  # -------------------------------------------------------------------------
  # DELIBERATELY NOT REQUIRED -- commented out. Uncomment only with a reason
  # recorded in docs/BRANCH_PROTECTION.md.
  #
  # PENDING EMPIRICAL NAME: contract-gate.yml DOES run on `pull_request`
  # (branches: [dev], plus workflow_dispatch), but the exact check-run name it
  # reports has not been observed on a real PR yet -- this list was derived
  # from the workflow files, not from a run. A required context that matches
  # no reported check is worse than a missing one: `dev` becomes permanently
  # unmergeable (every PR parked at "Expected -- waiting for status to be
  # reported"), and with ENFORCE_ADMINS=true there is no bypass, only
  # --unprotect.
  # FOLLOW-UP: on the next PR to dev, read the real check name off that PR
  # (gh pr checks <n>, or the Checks tab), add that exact string above, and
  # re-run this script with --apply.
  # "Contract gate (docs/CONTRACTS.md)"
  #
  # AMBIGUOUS: platform-matrix-macos.yml -- all three jobs carry
  # `continue-on-error: true`. An advisory job is not a merge gate; requiring
  # it would make a red macOS runner block a merge it was built to never block.
  # "macOS - full test suite (advisory)"
  # "macOS - install/startup smoke (advisory)"
  # "macOS - Docker availability probe (advisory)"
)

# ---------------------------------------------------------------------------
# Policy knobs. One place only, so the printed dry run and the PUT payload
# cannot disagree.
# ---------------------------------------------------------------------------
STRICT_UP_TO_DATE=false          # true = PR branch must also be up to date with dev
ENFORCE_ADMINS=true              # false = admins bypass everything (reopens the hole)
REQUIRED_APPROVALS=0             # 0 = PR required, no second human. A PR author cannot
                                 # approve their own PR, so 1 + ENFORCE_ADMINS=true
                                 # deadlocks a single-maintainer repo: raise only when a
                                 # second reviewer actually exists.
DISMISS_STALE_REVIEWS=true
CODEOWNER_REVIEWS=false
LAST_PUSH_APPROVAL=false
REQUIRED_LINEAR_HISTORY=false
REQUIRED_CONVERSATION_RESOLUTION=true
ALLOW_FORCE_PUSHES=false
ALLOW_DELETIONS=false
LOCK_BRANCH=false
ALLOW_FORK_SYNCING=false

usage() {
    cat <<'USAGE'
apply_branch_protection.sh -- dry-run-by-default branch protection for `dev`.

  (no args)                print the payload + the command; change nothing
  --apply                  send the PUT to GitHub (requires a working `gh`)
  --unprotect              print the DELETE (emergency hatch); change nothing
  --unprotect --apply      DELETE the protection rule now  <-- destructive
  --help                   this text

Env: REPO_SLUG (default use-less-vars/ThoughtMachine), BRANCH (default dev).

The required check names are derived by hand from .github/workflows/*.yml --
read docs/BRANCH_PROTECTION.md before adding, renaming or removing a job.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --unprotect) UNPROTECT=1 ;;
        --help|-h) usage; exit 0 ;;
        *)
            printf 'unknown argument: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

api_url="repos/${REPO_SLUG}/branches/${BRANCH}/protection"

build_payload() {
    # Hand-rolled JSON: no python/jq dependency, and each context stays on its
    # own line so a diff of this script is a diff of the rule.
    printf '{\n'
    printf '  "required_status_checks": {\n'
    printf '    "strict": %s,\n' "$STRICT_UP_TO_DATE"
    printf '    "contexts": [\n'
    local first=1 ctx
    for ctx in "${REQUIRED_CHECKS[@]}"; do
        if [ "$first" -eq 1 ]; then
            first=0
        else
            printf ',\n'
        fi
        printf '      "%s"' "$ctx"
    done
    printf '\n    ]\n'
    printf '  },\n'
    printf '  "enforce_admins": %s,\n' "$ENFORCE_ADMINS"
    printf '  "required_pull_request_reviews": {\n'
    printf '    "dismiss_stale_reviews": %s,\n' "$DISMISS_STALE_REVIEWS"
    printf '    "require_code_owner_reviews": %s,\n' "$CODEOWNER_REVIEWS"
    printf '    "required_approving_review_count": %s,\n' "$REQUIRED_APPROVALS"
    printf '    "require_last_push_approval": %s\n' "$LAST_PUSH_APPROVAL"
    printf '  },\n'
    printf '  "restrictions": null,\n'
    printf '  "required_linear_history": %s,\n' "$REQUIRED_LINEAR_HISTORY"
    printf '  "required_conversation_resolution": %s,\n' "$REQUIRED_CONVERSATION_RESOLUTION"
    printf '  "allow_force_pushes": %s,\n' "$ALLOW_FORCE_PUSHES"
    printf '  "allow_deletions": %s,\n' "$ALLOW_DELETIONS"
    printf '  "lock_branch": %s,\n' "$LOCK_BRANCH"
    printf '  "allow_fork_syncing": %s\n' "$ALLOW_FORK_SYNCING"
    printf '}\n'
}

validate_json() {
    # Soft check: only when a python3 is around. A payload typo must not reach
    # GitHub as a half-applied rule.
    command -v python3 >/dev/null 2>&1 || return 0
    printf '%s' "$1" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
        || { echo "ERROR: generated payload is not valid JSON" >&2; exit 1; }
}

PAYLOAD="$(build_payload)"
validate_json "$PAYLOAD"

if [ "$UNPROTECT" -eq 1 ]; then
    printf '== EMERGENCY HATCH: remove ALL protection from %s in %s ==\n' "$BRANCH" "$REPO_SLUG"
    printf '   This drops required reviews, required checks, enforce_admins,\n'
    printf '   force-push protection and deletion protection in one call.\n'
    printf '   Fix the cause and re-run this script afterwards.\n\n'
    printf 'gh api --method DELETE "%s"\n' "$api_url"
    if [ "$APPLY" -ne 1 ]; then
        printf '\n(dry run -- nothing was deleted; add --apply to execute)\n'
        exit 0
    fi
    command -v gh >/dev/null 2>&1 || { echo "ERROR: --apply needs the gh CLI on PATH" >&2; exit 1; }
    printf '\n>>> executing DELETE\n'
    gh api --method DELETE "$api_url" || exit 1
    printf 'protection removed from %s\n' "$BRANCH"
    exit 0
fi

printf '== branch protection for %s in %s (dry run unless --apply) ==\n' "$BRANCH" "$REPO_SLUG"
printf '== %s required context(s) ==\n\n' "${#REQUIRED_CHECKS[@]}"
printf '%s\n\n' "$PAYLOAD"
printf 'gh api --method PUT "%s" --input - <<<payload\n' "$api_url"

if [ "$APPLY" -ne 1 ]; then
    printf '\n(dry run -- nothing was sent; add --apply to execute)\n'
    exit 0
fi

command -v gh >/dev/null 2>&1 || { echo "ERROR: --apply needs the gh CLI on PATH" >&2; exit 1; }

printf '\n>>> executing PUT\n'
printf '%s' "$PAYLOAD" | gh api --method PUT "$api_url" --input - || exit 1

printf '\napplied. verify with:\n'
printf '  gh api "%s" --jq ".required_status_checks.contexts[]"\n' "$api_url"
