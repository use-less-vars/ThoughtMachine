#!/usr/bin/env bash
# scripts/check_kb_staleness.sh -- report when the HANDOFF router has gone stale.
#
#   ./scripts/check_kb_staleness.sh        # no arguments
#
# Env:
#   HANDOFF_FILE  router to parse (default: this repo's
#                 .thoughtmachine/knowledge/project/HANDOFF.md)
#
# A referenced file is STALE when it is strictly newer than the router (equal
# mtimes count as fresh) or when it is missing entirely; both print a WARNING.
#
# EXIT CODES -- deliberately CI-hostile to staleness:
#   0  PASS: HANDOFF.md is up to date. (nothing warned)
#   1  at least one WARNING was printed
# Warnings failing the build is the point: the operator intends to gate CI on
# this script, so a stale handoff must break a build rather than only log.
#
# Portability: freshness uses bash's `[[ -nt ]]` builtin, never `stat -c %Y`
# (GNU-only, wrong on macOS).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=lib/kb_manifest.sh
. "$SCRIPT_DIR/lib/kb_manifest.sh"

HANDOFF_FILE=${HANDOFF_FILE:-$(kb_default_handoff "$SCRIPT_DIR")}

# Parse the manifest first: kb_entries prints its own diagnostics on stderr and
# returns non-zero for a missing router, a missing section, or an empty list.
entry_list=$(kb_entries "$HANDOFF_FILE")

repo_root=$(kb_repo_root "$HANDOFF_FILE")
handoff_dir=$(cd "$(dirname "$HANDOFF_FILE")" && pwd -P)

count=0
declare -a entries
while IFS= read -r entry; do
  if [ -n "$entry" ]; then
    entries[$count]="$entry"
    count=$((count + 1))
  fi
done <<< "$entry_list"

if [ "$count" -eq 0 ]; then
  printf 'ERROR: %s: no manifest entries parsed; nothing to check.\n' "$HANDOFF_FILE" >&2
  exit 1
fi

stale=0
index=0
while [ "$index" -lt "$count" ]; do
  entry="${entries[$index]}"
  if ! resolved=$(kb_resolve_entry "$HANDOFF_FILE" "$entry"); then
    # The file is absent, so report the path where it was expected first.
    printf 'WARNING: %s is missing. The handoff may be stale.\n' \
      "$(kb_rel_path "$repo_root" "$handoff_dir/$entry")"
    stale=1
  elif [[ "$resolved" -nt "$HANDOFF_FILE" ]]; then
    printf 'WARNING: %s is newer than HANDOFF.md. The handoff may be stale.\n' \
      "$(kb_rel_path "$repo_root" "$resolved")"
    stale=1
  fi
  index=$((index + 1))
done

if [ "$stale" -eq 0 ]; then
  printf 'PASS: HANDOFF.md is up to date.\n'
  exit 0
fi
exit 1
