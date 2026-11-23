#!/usr/bin/env bash
# apply_branch_protection.sh -- the CLASSIC branch-protection layer, which on
# this repo is NOT the layer that decides merges. It therefore refuses to act.
#
# TWO LAYERS CARRY THE NAME "BRANCH PROTECTION" HERE
#   1. RULESET -- repos/<slug>/rulesets/<id>. This is what blocks a merge: a
#      repository ruleset (24214086, measured 2026-11-23T15:20+01:00, 13
#      required contexts) with its own bypass actors. It is the layer
#      docs/BRANCH_PROTECTION.md documents, and the only one worth reading.
#   2. CLASSIC -- repos/<slug>/branches/<branch>/protection. This is what this
#      script can build, print and PUT. On this repo it answers 404: `dev` has
#      no classic rule at all.
#   A ruleset OVERRIDES the classic layer. A successful PUT from here would
#   therefore change nothing about what blocks a merge while printing
#   "applied" -- a silent no-op dressed as enforcement. THE DEFAULT IS A
#   REFUSAL: exit 3, the layer named, and the work it did not do spelled out.
#
# WHAT THIS DOES
#   Observes the ruleset layer (or admits it cannot), then either refuses, or
#   -- when the observation says no branch ruleset governs `dev`, or with the
#   explicit --force-classic -- builds ONE fixed classic branch-protection
#   payload for `dev` and, only with --apply, `gh api` PUTs it. Dry run BY
#   DEFAULT: with no arguments it prints the payload and the exact command.
#
# WHY THE CONTEXTS ARE HARD-CODED -- AND HOW THEY ARE KEPT HONEST
#   GitHub matches a required status check by its CHECK-RUN NAME, the BARE job
#   `name:` (the job id when absent, matrix values interpolated) -- NOT
#   "<workflow> / <job>". REQUIRED_CHECKS below is the set the RULESET requires,
#   read off the ruleset (13 stored entries, 13 unique contexts), never derived
#   from the workflow files and never derived by subtracting from a count.
#   CANDIDATE_CHECKS holds the 6 names that real jobs report and the ruleset
#   does NOT require: real checks, can go red, stop no merge. Listing them here
#   is the whole point -- the previous revision declared all 18 required, which
#   was a restatement of intent dressed as enforcement.
#   Two ways a list rots: a renamed job silently un-gates the branch, and a
#   required context that no job reports parks every PR at "Expected -- waiting
#   for status to be reported" forever. `--verify-names` is the local check for
#   both directions; run it after any job rename.
#
# USAGE
#   scripts/apply_branch_protection.sh                  # observe, then dry run
#                                                       # or refuse (exit 3)
#   scripts/apply_branch_protection.sh --force-classic   # skip the refusal
#   scripts/apply_branch_protection.sh --apply          # really PUT it
#   scripts/apply_branch_protection.sh --unprotect      # dry run of the DELETE
#   scripts/apply_branch_protection.sh --unprotect --apply
#   scripts/apply_branch_protection.sh --verify-names FILE
#                                       # local: diff a check-run name dump
#                                       # against REQUIRED_CHECKS (no network)
#   scripts/apply_branch_protection.sh --help
#
# ENVIRONMENT
#   REPO_SLUG    owner/name   (default: use-less-vars/ThoughtMachine)
#   BRANCH       branch       (default: dev)
#   RULESET_IDS  a recorded observation of the RULESET layer: comma- or
#                newline-separated ruleset ids that govern BRANCH. Set but
#                EMPTY means "observed: no branch ruleset" -- the only value
#                that lets the classic path run without --force-classic.
#                Unset means "not observed": the script asks `gh` if it can,
#                and refuses otherwise. Exists so the refusal is testable
#                off-line and so a recorded read can be pasted in.
#
# EXIT CODES
#   0 dry run, verify PASS, applied      2 usage error
#   1 failure (PUT/DELETE failed, payload invalid, required name absent)
#   3 REFUSED: the layer this script speaks is not the layer that decides
#     merges, or that could not be established. Nothing was sent to GitHub.
#
# EMERGENCY HATCH -- read this before you need it
#   `--unprotect --apply` issues
#     gh api --method DELETE repos/<slug>/branches/<branch>/protection
#   and removes EVERYTHING the CLASSIC layer holds at once: required reviews,
#   required status checks, enforce_admins, force-push and deletion protection.
#   On a branch governed by a ruleset it removes nothing that matters, so it is
#   refused by the same gate as a PUT; --force-classic overrides. When a
#   required check cannot possibly go green (a renamed job, a dead runner, an
#   outage), the ruleset's own settings are the levers -- its bypass actors and
#   its `strict_required_status_checks_policy` -- not this DELETE.
#
# NOTE: scripts/*.sh is parsed by `bash -n` in the installer-contract job of
# python-version-matrix.yml -- keep this file parseable, no bashisms outside
# bash 4.
set -uo pipefail

REPO_SLUG="${REPO_SLUG:-use-less-vars/ThoughtMachine}"
BRANCH="${BRANCH:-dev}"
APPLY=0
UNPROTECT=0
FORCE_CLASSIC=0
VERIFY_NAMES_FILE=""

# `${RULESET_IDS+set}` distinguishes "set to a recorded observation" (possibly
# empty, i.e. observed-none) from "not observed at all". Referencing the bare
# name under `set -u` would kill the script when it is unset.
if [ "${RULESET_IDS+set}" = set ]; then
    RULESET_IDS_OBSERVED=1
else
    RULESET_IDS_OBSERVED=0
    RULESET_IDS=""
fi

# ---------------------------------------------------------------------------
# Required contexts: the RULESET's set, in sorted order. 13 entries.
#
# PROVENANCE -- MEASURED FROM THE RULESET, NOT DERIVED (2026-11-23T15:20+01:00):
#
#   gh api repos/<owner>/<repo>/rulesets/24214086 --jq '.rules[] |
#     select(.type=="required_status_checks") |
#     .parameters.required_status_checks[].context'
#
# 13 stored entries, 13 unique contexts: an earlier reading carried `Unit tests
# - Python 3.12` twice and no `Mutation gate (contracts)`; the duplicate is gone
# and that name is new. Sorted order carries no lost information precisely
# because nothing is duplicated -- the stored order is not asserted here.
# GitHub reports the BARE JOB NAME as the check-run name -- there is NO
# "<workflow> / " prefix. `gh pr checks` prints a grouped form that looks
# prefixed; deriving from that display invents contexts no job ever reports,
# which is exactly how an earlier hand-derived list went wrong.
#
# `Mutation gate (contracts)` HAS a producer job in this tree, and that was
# measured here rather than assumed: .github/workflows/contract-gate.yml
# declares job `mutation-gate:` (the job KEY, line 147) whose `name:` (line 148)
# is exactly this string, and its run step (line 179) is
# `python3 scripts/mutation_gate.py --timeout 300`, reading the registry in
# scripts/mutation_specs.py (both files exist on this tree). Its workflow
# triggers on `pull_request: branches: [dev]` (lines 37-38), so the context is
# reportable on a PR to `dev`. Every context in this array therefore has a
# producer, and `--verify-names` over a real check-run dump should list nothing
# under REQUIRED BUT ABSENT. See docs/BRANCH_PROTECTION.md.
#
# Every line number in this block and in the two arrays below was re-measured
# against THIS tree by searching the cited file for the quoted token -- not by
# reusing an earlier reading. contract-gate.yml had grown since that reading, so
# every anchor into it was stale: :44/:140/:141/:172/:31-32 are now
# :50/:147/:148/:179/:37-38. Anchor on the token first and the number second, or
# the citation rots silently; each number below points at the line carrying
# that job's `name:` value (or the key named), which is the fact being claimed.
# ---------------------------------------------------------------------------
REQUIRED_CHECKS=(
  "Contract gate (docs/CONTRACTS.md)"           # job contract-gate (contract-gate.yml:50)
  "Install + check-only"                        # job first-start (first_start_ci.yml:43)
  "Installer version-contract assertions"       # job installer-contract (python-version-matrix.yml:159)
  "Linux - install/startup smoke (blocking)"    # job linux-install-smoke (platform-matrix.yml:113)
  "Linux install + check-only + backend smoke"  # job linux-install (cross-platform-smoke.yml:44)
  "Mutation gate (contracts)"                   # job mutation-gate (contract-gate.yml:148; see above)
  "Ubuntu - full test suite"                    # job ubuntu-suite (platform-matrix.yml:68)
  # python-version-matrix.yml: the matrix job's name interpolates
  # matrix.python-version, so ONE job yields FOUR check-run names. Adding or
  # dropping a version in that file means editing this list too.
  "Unit tests - Python 3.11"                    # job unit-tests (python-version-matrix.yml:39)
  "Unit tests - Python 3.12"
  "Unit tests - Python 3.13"
  "Unit tests - Python 3.14"
  "macOS launcher preflight (gating)"           # job macos-launcher (cross-platform-smoke.yml:254)
  "web-ui-frontend-suite"                       # job web-ui-frontend-suite (platform-matrix.yml:501)
)

# ---------------------------------------------------------------------------
# Candidate contexts: PRODUCED, MEASURED ABSENT from the ruleset. Not required.
#
# Six names that real jobs report on every PR, can go red, and stop no merge:
# they were declared required by the previous revision of this script, which is
# not the same thing as being enforced. They stay listed so that
# `--verify-names` can say "candidate, not required" instead of "unknown name",
# and so that nobody re-adds one to REQUIRED_CHECKS by copying the old list.
# Requiring one is a decision that must be made in the RULESET (this script
# cannot put it there) and recorded in docs/BRANCH_PROTECTION.md first.
# ---------------------------------------------------------------------------
CANDIDATE_CHECKS=(
  "Linux - auth e2e (browser)"                  # job linux-e2e-auth (platform-matrix.yml:180)
  "Windows - import smoke"                      # job windows-import-smoke (platform-matrix.yml:347)
  "Windows - thin pytest slice"                 # job windows-thin-pytest (platform-matrix.yml:383)
  "Windows Python 3.14 gate proof"              # job windows-python-314 (python-version-matrix.yml:235)
  "Windows venv + smoke script"                 # job windows-smoke (cross-platform-smoke.yml:124)
  "web-ui-backend-suite"                        # job web-ui-backend-suite (platform-matrix.yml:442)
)

# ---------------------------------------------------------------------------
# DELIBERATELY NOT REQUIRED, and not even candidates -- the macOS trio.
#
# platform-matrix-macos.yml's three jobs no longer carry `continue-on-error`
# (that mask was removed), so a red macOS job FAILS that workflow's run. They
# are still left out of both lists on purpose: the same job was measured red on
# PR #401 (`macOS - full test suite (advisory)` concluded failure while its run
# stayed green), and requiring it would block a merge on that runner rather
# than on the code. Requiring one is a decision for a measured green macOS run
# -- record it in docs/BRANCH_PROTECTION.md first.
#   "macOS - full test suite (advisory)"       job macos-suite (platform-matrix-macos.yml:75)
#   "macOS - install/startup smoke (advisory)" job macos-install-smoke (platform-matrix-macos.yml:133)
#   "macOS - Docker availability probe (advisory)" job macos-docker-probe (platform-matrix-macos.yml:310)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Policy knobs. One place only, so the printed dry run and the PUT payload
# cannot disagree. THE RULESET'S OWN KNOBS ARE NOT THESE: measured on
# 2026-11-23T15:20+01:00 it holds strict_required_status_checks_policy=false
# and a bypass_actors entry with bypass_mode=always, i.e. a role can merge
# without waiting. The values below describe the classic payload this script
# would send, which on this repo is a layer that decides nothing. Changing the
# real rule is a ruleset edit; this script does not attempt one.
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
apply_branch_protection.sh -- the CLASSIC branch-protection layer for `dev`,
which on this repo is not the layer that decides merges. Refuses by default.

  (no args)                observe the ruleset layer, then dry run or refuse
  --force-classic          act on the classic layer even though the ruleset
                           layer was not confirmed absent (prints a warning)
  --apply                  send the PUT to GitHub (requires a working `gh`)
  --unprotect              print the DELETE (emergency hatch); change nothing
  --unprotect --apply      DELETE the classic protection rule now  <-- destructive
  --verify-names FILE      diff FILE's newline-delimited check-run names
                           against the required list, in BOTH directions;
                           exit 0 only if every required name is present
                           (FILE is `jq -r '.check_runs[].name'` output;
                           blank lines, `#` comments and whitespace are
                           ignored). Local only: no network, no `gh`.
  --help                   this text

Env: REPO_SLUG (default use-less-vars/ThoughtMachine), BRANCH (default dev),
     RULESET_IDS (recorded ruleset ids that govern BRANCH; set-but-empty means
     "observed: none").

Exit 3 = REFUSED: a repository ruleset governs the branch, or that could not
be established. Rulesets override classic protection, so a PUT here would
print "applied" about a layer that decides nothing. REQUIRED_CHECKS mirrors
the RULESET's 13 contexts; it is not the rule. Read
docs/BRANCH_PROTECTION.md before adding, renaming or removing a job.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --unprotect) UNPROTECT=1 ;;
        --force-classic) FORCE_CLASSIC=1 ;;
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
#                           "Expected -- waiting for status to be reported".
#                           -> FAILS (exit 1). Measured on this tree all 13
#                              required contexts have a producer job, so this
#                              list should come back empty; a name in it means a
#                              renamed job, a deleted job, or a workflow that did
#                              not run.
#   candidate, present      one of CANDIDATE_CHECKS: produced, measured not
#                           required. Informational -- these are MEANT to land
#                           here.
#   present, not declared   a check nobody has classified yet, typically a new
#                           job. Informational; add it to REQUIRED_CHECKS if the
#                           ruleset requires it, to CANDIDATE_CHECKS if not.
#
# Exit: 0 = every required name present; 1 = a required name is absent;
# 2 = usage error (file missing or unreadable). No network, no `gh` call, and
# no ruleset observation: this mode reports names, not layers.
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

    candidates=0
    printf '\nCANDIDATE, PRESENT -- produced, measured NOT required. Informational:\n'
    while IFS= read -r ctx; do
        [ -n "$ctx" ] || continue
        for cand in "${CANDIDATE_CHECKS[@]}"; do
            if [ "$cand" = "$ctx" ]; then
                printf '  NOT REQUIRED  %s\n' "$ctx"
                candidates=$((candidates + 1))
                break
            fi
        done
    done <<< "$observed"
    if [ "$candidates" -eq 0 ]; then
        printf '  (none of the %s candidates reported)\n' "${#CANDIDATE_CHECKS[@]}"
    fi

    extra=0
    printf '\nPRESENT BUT UNDECLARED -- not a gate, and not classified yet:\n'
    while IFS= read -r ctx; do
        [ -n "$ctx" ] || continue
        declared=0
        for req in "${REQUIRED_CHECKS[@]}" "${CANDIDATE_CHECKS[@]}"; do
            if [ "$req" = "$ctx" ]; then
                declared=1
                break
            fi
        done
        if [ "$declared" -eq 0 ]; then
            printf '  NOT GATED  %s\n' "$ctx"
            extra=$((extra + 1))
        fi
    done <<< "$observed"
    if [ "$extra" -eq 0 ]; then
        printf '  (none)\n'
    fi

    if [ "$missing" -ne 0 ]; then
        printf '\nFAIL: %s required name(s) absent. A required context that no job\n' "$missing"
        printf 'reports parks every PR at "Expected" -- fix the job name or the ruleset.\n'
        exit 1
    fi
    printf '\nPASS: all %s required name(s) reported; %s candidate(s) not required, %s undeclared.\n' \
        "${#REQUIRED_CHECKS[@]}" "$candidates" "$extra"
    exit 0
fi

# ---------------------------------------------------------------------------
# The observation this script could not make before: does a RULESET govern the
# branch? Without it the "applied" message was about the wrong layer.
#
# Prints one ruleset id per line (nothing = none found) and returns:
#   0  observed      1  no `gh` on PATH      2  the gh read failed
# Any branch-targeting ruleset counts as governing. Checking whether its ref
# conditions actually cover BRANCH needs a second read this script does not do;
# over-refusing is the safe direction, and says so in the message.
# ---------------------------------------------------------------------------
observe_governing_rulesets() {
    if [ "$RULESET_IDS_OBSERVED" -eq 1 ]; then
        printf '%s\n' "$RULESET_IDS" | tr ',' '\n' \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$'
        return 0
    fi
    if ! command -v gh >/dev/null 2>&1; then
        return 1
    fi
    ids="$(gh api "repos/${REPO_SLUG}/rulesets" \
        --jq '.[] | select(.target=="branch") | .id' 2>/dev/null)" || return 2
    printf '%s\n' "$ids" | grep -v '^$'
    return 0
}

refuse_governed() {
    printf '\nREFUSING: refs/heads/%s is governed by ruleset %s; this script applies\n' \
        "$BRANCH" "$1" >&2
    printf 'CLASSIC branch protection, which is not the control that decides merges on\n' >&2
    printf 'this repository (the classic endpoint answers 404 here).\n\n' >&2
    printf 'What this run did NOT do:\n' >&2
    printf '  - set required status checks (%s contexts) on the classic endpoint\n' \
        "${#REQUIRED_CHECKS[@]}" >&2
    printf '  - set enforce_admins, required reviews, force-push or deletion protection\n' >&2
    printf '  - read or write the ruleset'"'"'s own settings (strict, bypass actors)\n\n' >&2
    printf 'Nothing was sent to GitHub.\n' >&2
    printf 'This script cannot change what blocks a merge. Read or edit the ruleset instead:\n' >&2
    printf '  gh api repos/%s/rulesets/%s --jq ".rules[]\n' "$REPO_SLUG" "$1" >&2
    printf '    | select(.type=="required_status_checks")\n' >&2
    printf '    | .parameters.required_status_checks[].context"\n\n' >&2
    printf 'If you have read the ruleset layer yourself and it does NOT govern %s,\n' "$BRANCH" >&2
    printf 're-run with --force-classic to act on the classic layer anyway.\n' >&2
    exit 3
}

refuse_unobserved() {
    printf '\nREFUSING: this run could not observe the RULESET layer for refs/heads/%s\n' "$BRANCH" >&2
    printf '(reason: %s), and a ruleset OVERRIDES the classic layer this script applies.\n' "$1" >&2
    printf 'Assuming "ungoverned" would let this script print "applied" about a layer\n' >&2
    printf 'that decides nothing -- the silent no-op it exists to avoid.\n\n' >&2
    printf 'The layer this run could NOT observe: repos/%s/rulesets\n' "$REPO_SLUG" >&2
    printf 'The layer this script speaks:          repos/%s/branches/%s/protection\n\n' \
        "$REPO_SLUG" "$BRANCH" >&2
    printf 'What this run did NOT do:\n' >&2
    printf '  - set required status checks (%s contexts) on the classic endpoint\n' \
        "${#REQUIRED_CHECKS[@]}" >&2
    printf '  - set enforce_admins, required reviews, force-push or deletion protection\n' >&2
    printf '  - observe the ruleset, so "no ruleset governs this branch" is UNPROVEN\n\n' >&2
    printf 'Nothing was sent to GitHub.\n' >&2
    printf 'Options: install `gh` (and a token that can read rulesets) and re-run;\n' >&2
    printf 'record an observation with RULESET_IDS=<id[,id]> (empty = observed none);\n' >&2
    printf 'or, having read the ruleset layer yourself, re-run with --force-classic.\n' >&2
    exit 3
}

governing="$(observe_governing_rulesets)"
observe_rc=$?

if [ "$FORCE_CLASSIC" -ne 1 ]; then
    if [ "$observe_rc" -eq 0 ] && [ -n "$governing" ]; then
        refuse_governed "$(printf '%s' "$governing" | tr '\n' ',' \
            | sed -e 's/,$//' -e 's/,/, /g')"
    fi
    if [ "$observe_rc" -eq 1 ]; then
        refuse_unobserved 'no gh on PATH'
    fi
    if [ "$observe_rc" -eq 2 ]; then
        refuse_unobserved 'the gh read of the rulesets list failed'
    fi
fi

if [ "$FORCE_CLASSIC" -eq 1 ]; then
    if [ "$RULESET_IDS_OBSERVED" -eq 1 ]; then
        layer="RULESET_IDS='${RULESET_IDS}'"
    elif [ "$observe_rc" -eq 0 ]; then
        layer="gh listed: $(printf '%s' "${governing:-none}" | tr '\n' ' ')"
    else
        layer="unobserved (gh rc=${observe_rc})"
    fi
    printf '== WARNING --force-classic: the RULESET layer was not confirmed absent ==\n' >&2
    printf '   observation: %s\n' "$layer" >&2
    printf '   acting on the CLASSIC layer only; it does not decide merges here.\n\n' >&2
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
    printf '== EMERGENCY HATCH: remove the CLASSIC rule from %s in %s ==\n' "$BRANCH" "$REPO_SLUG"
    printf '   This drops required reviews, required checks, enforce_admins,\n'
    printf '   force-push protection and deletion protection in one call -- for the\n'
    printf '   classic layer only. Committed bypass actors on a governing ruleset are\n'
    printf '   NOT removed by it.\n\n'
    printf 'gh api --method DELETE "%s"\n' "$api_url"
    if [ "$APPLY" -ne 1 ]; then
        printf '\n(dry run -- nothing was deleted; add --apply to execute)\n'
        exit 0
    fi
    command -v gh >/dev/null 2>&1 || { echo "ERROR: --apply needs the gh CLI on PATH" >&2; exit 1; }
    printf '\n>>> executing DELETE\n'
    gh api --method DELETE "$api_url" || exit 1
    printf 'classic protection removed from %s\n' "$BRANCH"
    exit 0
fi

printf '== CLASSIC branch protection for %s in %s (dry run unless --apply) ==\n' "$BRANCH" "$REPO_SLUG"
printf '== %s required context(s), %s candidate(s) NOT required ==\n\n' \
    "${#REQUIRED_CHECKS[@]}" "${#CANDIDATE_CHECKS[@]}"
printf '%s\n\n' "$PAYLOAD"
printf 'gh api --method PUT "%s" --input - <<<payload\n' "$api_url"

if [ "$APPLY" -ne 1 ]; then
    printf '\n(dry run -- nothing was sent; add --apply to execute)\n'
    exit 0
fi

command -v gh >/dev/null 2>&1 || { echo "ERROR: --apply needs the gh CLI on PATH" >&2; exit 1; }

printf '\n>>> executing PUT\n'
printf '%s' "$PAYLOAD" | gh api --method PUT "$api_url" --input - || exit 1

printf '\napplied to the CLASSIC layer. verify with:\n'
printf '  gh api "%s" --jq ".required_status_checks.contexts[]"\n' "$api_url"
printf 'and read what actually gates %s with:\n' "$BRANCH"
printf '  gh api repos/%s/rulesets --jq ".[] | select(.target=="branch") | .id"\n' "$REPO_SLUG"
