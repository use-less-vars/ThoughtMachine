#!/usr/bin/env bash
# scripts/check_kb_staleness.sh -- content-addressed staleness gate for the
# HANDOFF router.
#
#   ./scripts/check_kb_staleness.sh --check    # gate; 0 fresh, 1 stale
#   ./scripts/check_kb_staleness.sh            # alias for --check (historical)
#   ./scripts/check_kb_staleness.sh --update   # record the current digests
#
# Env:
#   HANDOFF_FILE  router to parse (default: this repo's
#                 .thoughtmachine/knowledge/project/HANDOFF.md)
#
# WHY CONTENT AND NOT MTIME. This gate used to compare mtimes: a file "newer
# than HANDOFF.md" counted as stale. That rule is wrong in both directions on a
# real checkout -- `git clone` gives every file the same mtime, so a member
# edited between commits is never "newer" (a stale handoff passes), while a
# `git checkout` that rewrites an untouched file makes the handoff look stale
# (a false alarm). Only a digest answers the actual question: is the handoff
# describing the bytes that are on disk now?
#
# WHERE THE DIGESTS LIVE. In the router itself, one per manifest entry, at the
# end of the entry line:
#
#   1. `.thoughtmachine/knowledge/project/PRIORITIES.md` -- ... - sha256:ab12...
#
# Deliberately NOT a second lockfile: a second file is a second thing to keep in
# sync, which is exactly the drift this gate exists to catch. `--update`
# recomputes and rewrites those tokens in place, and touches nothing else.
#
# ADVISORY ENTRIES. One member is listed in the router WITHOUT a digest on
# purpose: the live work queue (PRIORITIES.md) is edited by every branch that
# touches a C-item and rewritten whenever dev is merged, so its digest is
# branch-local by definition and could never be a staleness signal. For such an
# entry the gate prints a NOTE (no digest recorded, or a digest that no longer
# matches -- `--update` refreshes the latter) and does NOT fail. The list of
# such entries is `KB_ADVISORY_ENTRIES` in lib/kb_manifest.sh; the generator's
# twin is `ADVISORY` in scripts/gen_handoff.py. A missing advisory FILE still
# fails: the bundle would not build.
#
# FAIL CLOSED. A NON-advisory entry carrying no usable digest is STALE --
# reported, not skipped. An entry that quietly lost its hash would otherwise
# pass forever. Only the advisory entries above are exempt from that rule, and
# only they.
#
# EXIT CODES -- deliberately CI-hostile to staleness:
#   0  PASS: HANDOFF.md is up to date. (nothing warned; NOTEs are not warnings)
#   1  at least one WARNING was printed
#   2  usage error (unknown argument)
# Warnings failing the build is the point: CI runs this with --check, so a stale
# handoff must break a build rather than only log.
#
# Portability: digests come from kb_sha256 (shasum -a 256, then sha256sum, then
# openssl dgst -sha256 -- hard-fail when none exists), never from `stat`; bash
# 3.2 (stock macOS), so no mapfile and no realpath.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=lib/kb_manifest.sh
. "$SCRIPT_DIR/lib/kb_manifest.sh"

usage() {
  printf 'usage: %s [--check|--update]\n' "$0" >&2
  printf '  --check   (default) exit 1 when a manifest file no longer matches the sha256 recorded in the router\n' >&2
  printf '            (advisory entries print a NOTE instead: their digest is branch-local)\n' >&2
  printf '  --update  recompute every manifest sha256 and rewrite the recorded values in place\n' >&2
  printf '            (an advisory entry that carries no token is left untouched)\n' >&2
}

MODE=check
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --check) MODE=check; shift ;;
    --update) MODE=update; shift ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage; exit 2 ;;
  esac
done

HANDOFF_FILE=${HANDOFF_FILE:-$(kb_default_handoff "$SCRIPT_DIR")}

# Parse the manifest first: kb_entry_hashes prints its own diagnostics on stderr
# and returns non-zero for a missing router, a missing section, or an empty list.
entry_list=$(kb_entry_hashes "$HANDOFF_FILE")

repo_root=$(kb_repo_root "$HANDOFF_FILE")
handoff_dir=$(cd "$(dirname "$HANDOFF_FILE")" && pwd -P)

# Two parallel arrays: entries[$i] is the manifest path, recorded[$i] the digest
# the router claims for it (may be empty -- see the fail-closed note above).
count=0
declare -a entries
declare -a recorded
# Split on the tab BY HAND: `read` with IFS=$'\t' strips the leading tab of an
# entry that recorded NO digest (tab is IFS whitespace), which would silently
# drop every hash-less entry instead of failing closed on it.
while IFS= read -r hash_line; do
  if [ -n "$hash_line" ]; then
    hash="${hash_line%%$'\t'*}"
    entry="${hash_line#*$'\t'}"
    if [ -n "$entry" ]; then
      recorded[$count]="$hash"
      entries[$count]="$entry"
      count=$((count + 1))
    fi
  fi
done <<< "$entry_list"

if [ "$count" -eq 0 ]; then
  printf 'ERROR: %s: no manifest entries parsed; nothing to check.\n' "$HANDOFF_FILE" >&2
  exit 1
fi

if [ "$MODE" = "update" ]; then
  # Resolve and hash EVERY entry before writing anything: a half-rewritten
  # router is worse than a stale one, so one missing file aborts the whole run.
  declare -a fresh
  index=0
  while [ "$index" -lt "$count" ]; do
    entry="${entries[$index]}"
    if ! resolved=$(kb_resolve_entry "$HANDOFF_FILE" "$entry"); then
      printf 'ERROR: %s: referenced file "%s" is missing (looked next to the router and at the repo root); refusing to rewrite a partial set of digests.\n' \
        "$HANDOFF_FILE" "$entry" >&2
      exit 1
    fi
    fresh[$index]=$(kb_sha256 "$resolved")
    index=$((index + 1))
  done

  tmp="$HANDOFF_FILE.tmp.$$"
  trap 'rm -f "$tmp"' EXIT HUP INT TERM

  # Rewrite line by line so the rest of the router is preserved byte for byte:
  # only section-2 entry lines change, and only their trailing digest token. That
  # is the difference between --update and regenerating the file.
  #
  # An ADVISORY entry that carries NO token is emitted untouched: the generator
  # deliberately records none for it (see ADVISORY in scripts/gen_handoff.py),
  # so appending one here would make --update disagree with a regeneration and
  # hand-write state the generator would immediately drop. An advisory entry
  # that DOES carry a token (e.g. a router from before the exemption) is
  # rewritten like any other, so --update still clears a stale advisory digest.
  in_section=0
  index=0
  written=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$in_section" -eq 0 ] && [[ "$line" =~ $KB_SECTION_RE ]]; then
      in_section=1
    elif [ "$in_section" -eq 1 ] && [[ "$line" == "## "* ]]; then
      in_section=0
    elif [ "$in_section" -eq 1 ] && [[ "$line" =~ $KB_ENTRY_RE ]]; then
      base="$line"
      has_token=0
      # Drop an existing digest (well formed or not) before appending the new
      # one, so repeated --update runs cannot stack "sha256:" tokens.
      if [[ "$base" =~ [[:space:]]+-[[:space:]]+sha256:[^[:space:]]+$ ]]; then
        base="${base%${BASH_REMATCH[0]}}"
        has_token=1
      fi
      if [ "$has_token" -eq 0 ] && kb_entry_is_advisory "${entries[$index]}"; then
        printf '%s\n' "$line"
        index=$((index + 1))
        continue
      fi
      printf '%s - sha256:%s\n' "$base" "${fresh[$index]}"
      written=$((written + 1))
      index=$((index + 1))
      continue
    fi
    printf '%s\n' "$line"
  done < "$HANDOFF_FILE" > "$tmp"

  if [ "$index" -ne "$count" ]; then
    printf 'ERROR: %s: rewrote %d of %d manifest entries; refusing to replace the router.\n' \
      "$HANDOFF_FILE" "$index" "$count" >&2
    exit 1
  fi

  mv "$tmp" "$HANDOFF_FILE"
  printf 'Updated %d recorded sha256 values in %s.\n' "$written" "$HANDOFF_FILE"
  exit 0
fi

stale=0
index=0
while [ "$index" -lt "$count" ]; do
  entry="${entries[$index]}"
  want="${recorded[$index]}"
  if ! resolved=$(kb_resolve_entry "$HANDOFF_FILE" "$entry"); then
    # The file is absent, so report the path where it was expected first. This
    # is a FAILURE for advisory entries too: the bundle would not build.
    printf 'WARNING: %s is missing. The handoff may be stale.\n' \
      "$(kb_rel_path "$repo_root" "$handoff_dir/$entry")"
    stale=1
  elif [ -z "$want" ]; then
    if kb_entry_is_advisory "$entry"; then
      printf 'NOTE: %s carries no recorded sha256 because it is an advisory entry that changes on every branch; not a staleness failure.\n' \
        "$(kb_rel_path "$repo_root" "$resolved")"
    else
      printf 'WARNING: %s has no sha256 recorded, run --update. The handoff may be stale.\n' \
        "$(kb_rel_path "$repo_root" "$resolved")"
      stale=1
    fi
  else
    have=$(kb_sha256 "$resolved")
    if [ "$have" != "$want" ]; then
      if kb_entry_is_advisory "$entry"; then
        printf 'NOTE: %s changed since HANDOFF.md recorded sha256:%s (now sha256:%s); it is an advisory entry that changes on every branch, so the mismatch is expected -- run --update to refresh the recorded value.\n' \
          "$(kb_rel_path "$repo_root" "$resolved")" "$want" "$have"
      else
        printf 'WARNING: %s changed since HANDOFF.md recorded sha256:%s (now sha256:%s). The handoff may be stale.\n' \
          "$(kb_rel_path "$repo_root" "$resolved")" "$want" "$have"
        stale=1
      fi
    fi
  fi
  index=$((index + 1))
done

if [ "$stale" -eq 0 ]; then
  printf 'PASS: HANDOFF.md is up to date.\n'
  exit 0
fi
exit 1
