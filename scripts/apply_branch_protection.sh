#!/usr/bin/env bash
# apply_branch_protection.sh -- put this repo's CI gates under enforcement.
#
# WHAT THIS DOES
#   Builds ONE fixed branch-protection payload for `dev` and `gh api` PUTs it.
#   Dry run BY DEFAULT: with no arguments it prints the payload and the exact
#   command it would run. Nothing touches GitHub until --apply is passed.
#
# WHY THE CONTEXTS ARE HARD-CODED -- AND HOW THEY ARE KEPT HONEST
#   GitHub matches a required status check by its CHECK-RUN NAME, which is the
#   BARE job `name:` (the job id when `name:` is absent, matrix values
#   interpolated) -- NOT "<workflow> / <job>". The 18 names below were
#   MEASURED off the check-runs API on PR #401, not derived from the workflow
#   files; the provenance, the per-name trigger and the names deliberately NOT
#   required are documented in docs/BRANCH_PROTECTION.md.
#   Two ways this list rots: a renamed job silently un-gates the branch, and a
#   required context that no job reports parks every PR at "Expected -- waiting
#   for status to be reported" forever. `--verify-names` is the local check for
#   both directions; run it after any job rename.
#
# USAGE
#   scripts/apply_branch_protection.sh                  # dry run (default)
#   scripts/apply_branch_protection.sh --apply          # really PUT it
#   scripts/apply_branch_protection.sh --unprotect      # dry run of the DELETE
#   scripts/apply_branch_protection.sh --unprotect --apply
#   scripts/apply_branch_protection.sh --verify-names FILE
#                                       # local: diff a check-run name dump
#                                       # against REQUIRED_CHECKS (no network)
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
VERIFY_NAMES_FILE=""

# ---------------------------------------------------------------------------
# Required contexts: INCLUDED. 18 names, in the measured order.
#
# PROVENANCE -- MEASURED, NOT DERIVED (C93, 2026-11-18). These names were read
# off the check-runs API for PR #401:
#
#   gh api repos/<owner>/<repo>/commits/<PR #401 head sha>/check-runs \
#     --jq '.check_runs[].name'
#
# GitHub reports the BARE JOB NAME (`jobs.<id>.name:`) as the check-run name --
# there is NO "<workflow> / " prefix. `gh pr checks` prints a grouped form that
# looks prefixed; deriving from that display invents contexts no job ever
# reports, which is exactly how the previous hand-derived list went wrong.
#
# Every entry below belongs to a job that (a) is merge-blocking in its own
# workflow (no `continue-on-error`) and (b) is triggered by `pull_request`, so
# the check runs on every PR head commit. Workflows that also push to dev are
# noted, but the `pull_request` trigger is the one branch protection needs.
# ---------------------------------------------------------------------------
REQUIRED_CHECKS=(
  "Contract gate (docs/CONTRACTS.md)"           # job contract-gate (contract-gate.yml)
  "Install + check-only"                        # job first-start (first_start_ci.yml)
  "Installer version-contract assertions"       # job installer-contract (python-version-matrix.yml)
  "Linux - auth e2e (browser)"                  # job linux-e2e-auth (platform-matrix.yml)
  "Linux install + check-only + backend smoke"  # job linux-install (cross-platform-smoke.yml)
  "Linux - install/startup smoke (blocking)"    # job linux-install-smoke (platform-matrix.yml)
  "macOS launcher preflight (gating)"           # job macos-launcher (cross-platform-smoke.yml)
  "Ubuntu - full test suite"                    # job ubuntu-suite (platform-matrix.yml)
  # python-version-matrix.yml: the matrix job's name interpolates
  # matrix.python-version, so ONE job yields FOUR check-run names. Adding or
  # dropping a version in that file means editing this list too.
  "Unit tests - Python 3.11"                    # job unit-tests (python-version-matrix.yml)
  "Unit tests - Python 3.12"
  "Unit tests - Python 3.13"
  "Unit tests - Python 3.14"
  "web-ui-backend-suite"                        # job web-ui-backend-suite (platform-matrix.yml)
  "web-ui-frontend-suite"                       # job web-ui-frontend-suite (platform-matrix.yml)
  "Windows - import smoke"                      # job windows-import-smoke (platform-matrix.yml)
  "Windows Python 3.14 gate proof"              # job windows-python-314 (python-version-matrix.yml)
  "Windows - thin pytest slice"                 # job windows-thin-pytest (platform-matrix.yml)
  "Windows venv + smoke script"                 # job windows-smoke (cross-platform-smoke.yml)

  # -------------------------------------------------------------------------
  # DELIBERATELY NOT REQUIRED -- commented out. Do not uncomment without a
  # reason recorded in docs/BRANCH_PROTECTION.md.
  #
  # ADVISORY, and still visible: platform-matrix-macos.yml's three jobs all
  # carry `continue-on-error: true`, so they cannot fail their workflow run --
  # but they DO still report a check run, and it can conclude "failure"
  # (measured on PR #401: `macOS - full test suite (advisory)` concluded
  # failure while its run stayed green). Requiring one would either park `dev`
  # at "Expected" or block a merge on the red macOS runner it was built to
  # never block.
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
  --verify-names FILE      diff FILE's newline-delimited check-run names
                           against the required list, in BOTH directions;
                           exit 0 only if every required name is present
                           (FILE is `jq -r '.check_runs[].name'` output;
                           blank lines, `#` comments and whitespace are
                           ignored). Local only: no network, no `gh`.
  --help                   this text

Env: REPO_SLUG (default use-less-vars/ThoughtMachine), BRANCH (default dev).

The required check names are MEASURED from the check-runs API, not derived
from .github/workflows/*.yml -- read docs/BRANCH_PROTECTION.md before adding,
renaming or removing a job, and re-check the live list with --verify-names.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --unprotect) UNPROTECT=1 ;;
        --verify-names)
            if [ "$#" -lt 2 ]; then
                printf 'ERROR: --verify-names needs a FILE argument\n\n' >&2
                usage >&2
                exit 2
            fi
            VERIFY_NAMES_FILE="$2"
            shift
            ;;
        --help|-h) usage; exit 0 ;;
        *)
            printf 'unknown argument: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# --verify-names: the local half of the re-derivation above.
#
# Reads newline-delimited check-run names -- exactly the output of
#
#   gh api repos/<slug>/commits/<sha>/check-runs --jq '.check_runs[].name'
#
# trims surrounding whitespace, drops blank lines and `#` comments, and diffs
# the result against REQUIRED_CHECKS in BOTH directions, because the two
# directions mean different things:
#
#   required but absent     the rule can never be satisfied: every PR sits at
#                           "Expected -- waiting for status to be reported" and,
#                           with ENFORCE_ADMINS=true, only --unprotect helps.
#                           -> FAILS (exit 1).
#   present, not required   a check runs that branch protection does not gate
#                           on. Informational: the three macOS advisory names
#                           are MEANT to land here, and so does every new job.
#
# Exit: 0 = every required name present; 1 = a required name is absent;
# 2 = usage error (file missing or unreadable). No network, no `gh` call.
# ---------------------------------------------------------------------------
if [ -n "$VERIFY_NAMES_FILE" ]; then
    if [ ! -f "$VERIFY_NAMES_FILE" ]; then
        printf 'ERROR: --verify-names: not a file: %s\n' "$VERIFY_NAMES_FILE" >&2
        exit 2
    fi
    if [ ! -r "$VERIFY_NAMES_FILE" ]; then
        printf 'ERROR: --verify-names: not readable: %s\n' "$VERIFY_NAMES_FILE" >&2
        exit 2
    fi
    # The normalised set lives in a shell variable, not a temp file: it is read
    # twice and never shared, and a temp file adds a failure mode (a read-only
    # TMPDIR or /var/tmp turns a name check into a hard error) for no gain.
    # Trailing `|| true`: grep exits 1 when it filters EVERYTHING, which here
    # only means "the dump listed no names" -- reported as every name absent.
    # awk de-duplicates while keeping first-seen order (a job with several
    # completed runs appears once per run in the check-runs API response).
    observed="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$VERIFY_NAMES_FILE" \
        | grep -v '^#' | grep -v '^$' | awk '!seen[$0]++' || true)"

    printf '== --verify-names: %s against the %s required context(s) ==\n\n' \
        "$VERIFY_NAMES_FILE" "${#REQUIRED_CHECKS[@]}"

    missing=0
    printf 'REQUIRED BUT ABSENT -- unmergeable while anything is listed here:\n'
    for ctx in "${REQUIRED_CHECKS[@]}"; do
        if ! grep -qxF -- "$ctx" <<< "$observed"; then
            printf '  ABSENT     %s\n' "$ctx"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -eq 0 ]; then
        printf '  (none: every required name is present)\n'
    fi

    extra=0
    printf '\nPRESENT BUT NOT REQUIRED -- informational, not a gate:\n'
    while IFS= read -r ctx; do
        [ -n "$ctx" ] || continue
        is_required=0
        for req in "${REQUIRED_CHECKS[@]}"; do
            if [ "$req" = "$ctx" ]; then
                is_required=1
                break
            fi
        done
        if [ "$is_required" -eq 0 ]; then
            printf '  NOT GATED  %s\n' "$ctx"
            extra=$((extra + 1))
        fi
    done <<< "$observed"
    if [ "$extra" -eq 0 ]; then
        printf '  (none)\n'
    fi

    if [ "$missing" -ne 0 ]; then
        printf '\nFAIL: %s required name(s) absent. A required context that no job\n' "$missing"
        printf 'reports parks every PR at "Expected" -- fix the job name or this list.\n'
        exit 1
    fi
    printf '\nPASS: all %s required name(s) reported; %s observed name(s) not gated.\n' \
        "${#REQUIRED_CHECKS[@]}" "$extra"
    exit 0
fi

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
