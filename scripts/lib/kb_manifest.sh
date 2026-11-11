#!/usr/bin/env bash
# scripts/lib/kb_manifest.sh -- shared manifest parsing for the KB onboarding
# scripts. This file is SOURCED, never executed:
#
#   . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/kb_manifest.sh"
#
# The manifest is section 2 of the HANDOFF router ("## 2. <any title>"). The
# heading is matched by NUMBER ONLY -- the title is prose that humans rewrite,
# so matching the words would silently break the scripts.
#
# Portability: bash 3.2 (stock macOS). No `stat`, no `readlink -f`, no
# `realpath --relative-to`, no `mapfile`; only bash builtins plus awk.
#
# Functions:
#   kb_entries <router>          one manifest entry per line, manifest order
#   kb_repo_root <router>        absolute repo root (git, then ".git" walk)
#   kb_default_handoff <dir>     default router path for a script directory
#   kb_resolve_entry <router> <entry>
#                                absolute path, or non-zero when unresolvable
#   kb_rel_path <root> <abs>     path made relative to <root> when possible
#   kb_entry_hashes <router>     "<sha256><TAB><entry>" per manifest entry
#   kb_sha256 <file>             lowercase hex sha256 of a file's bytes

# Section heading: "##" + whitespace + "2" + "." (title deliberately ignored).
KB_SECTION_RE=${KB_SECTION_RE:-'^##[[:space:]]+2[.]'}

# Numbered list item whose FIRST token is backticked, e.g.
#   1. `PRIORITIES.md` -- active work queue
# A second backticked span later in the prose is NOT part of the entry.
# The regex lives in a variable because quoting the right-hand side of =~ would
# make bash compare it literally instead of as a regex.
KB_ENTRY_RE=${KB_ENTRY_RE:-'^[[:space:]]*[0-9]+[.]?[[:space:]]+`([^`]+)`'}

KB_DEFAULT_HANDOFF_REL=${KB_DEFAULT_HANDOFF_REL:-.thoughtmachine/knowledge/project/HANDOFF.md}

# _kb_section2_body <router-file>
#   Prints the lines of section 2: everything after the "## 2." heading, up to
#   the next "## " heading. Returns 3 when no heading matched, so "absent
#   section" stays distinguishable from "empty section" -- awk's own exit status
#   would be lost to a plain command substitution.
_kb_section2_body() {
  local router="${1:-}"
  awk -v re="$KB_SECTION_RE" '
    !in_section && $0 ~ re { in_section = 1; found = 1; next }
    in_section && /^## /   { exit }
    in_section             { print }
    END                    { if (!found) exit 3 }
  ' "$router"
}

# _kb_section2_body_or_fail <router-file>
#   _kb_section2_body plus the diagnostics every caller needs, so the parsing
#   entry points below cannot drift apart in what they say about a bad router.
_kb_section2_body_or_fail() {
  local router="${1:-}" body status
  if [ -z "$router" ]; then
    printf 'ERROR: no router file given.\n' >&2
    return 1
  fi
  if [ ! -f "$router" ]; then
    printf 'ERROR: router file not found: %s\n' "$router" >&2
    return 1
  fi

  status=0
  body="$(_kb_section2_body "$router")" || status=$?
  if [ "$status" -eq 3 ]; then
    printf 'ERROR: %s: no section heading matching %s (expected a "## 2. <title>" section).\n' \
      "$router" "'^##[[:space:]]+2\[.]'" >&2
    return 1
  fi
  if [ "$status" -ne 0 ]; then
    printf 'ERROR: %s: could not read the router (awk exit %d).\n' "$router" "$status" >&2
    return 1
  fi
  printf '%s\n' "$body"
}

# kb_entries <router-file>
#   Prints one manifest entry per line (backticks stripped), in manifest order.
#   Hard-fails -- message on stderr, non-zero return -- when the router is
#   missing, when section 2 is absent, or when section 2 holds no numbered
#   backticked items: a silently empty manifest is exactly the failure mode
#   that produces a bundle which looks complete while carrying nothing.
kb_entries() {
  local router="${1:-}" body n line
  local -a entries

  # Section 2's body, with the shared "absent/unreadable router" diagnostics
  # already printed on failure.
  body="$(_kb_section2_body_or_fail "$router")" || return 1

  n=0
  while IFS= read -r line; do
    if [[ "$line" =~ $KB_ENTRY_RE ]]; then
      entries[$n]="${BASH_REMATCH[1]}"
      n=$((n + 1))
    fi
  done <<< "$body"

  if [ "$n" -eq 0 ]; then
    printf 'ERROR: %s: section 2 lists no "N. `path`" manifest entries.\n' "$router" >&2
    return 1
  fi

  printf '%s\n' "${entries[@]}"
}

# kb_entry_hashes <router-file>
#   Prints "<recorded-sha256><TAB><entry>" per manifest entry, manifest order.
#   The recorded digest is the `sha256:<64 lowercase hex>` token the entry line
#   carries; it is EMPTY when the line carries none (or carries a malformed
#   one). Callers must treat empty as STALE, never as "nothing to compare": an
#   entry that quietly lost its hash would otherwise pass the gate forever,
#   which is precisely the silent drift the digest exists to catch.
kb_entry_hashes() {
  local router="${1:-}" body n line entry hash
  body="$(_kb_section2_body_or_fail "$router")" || return 1

  n=0
  while IFS= read -r line; do
    if [[ "$line" =~ $KB_ENTRY_RE ]]; then
      entry="${BASH_REMATCH[1]}"
      hash=""
      if [[ "$line" =~ sha256:([0-9a-f]{64}) ]]; then
        hash="${BASH_REMATCH[1]}"
      fi
      printf '%s\t%s\n' "$hash" "$entry"
      n=$((n + 1))
    fi
  done <<< "$body"

  if [ "$n" -eq 0 ]; then
    printf 'ERROR: %s: section 2 lists no "N. `path`" manifest entries.\n' "$router" >&2
    return 1
  fi
}

# kb_repo_root <router-file>
#   Absolute repo root for a router file: $KB_REPO_ROOT when set, else
#   `git rev-parse --show-toplevel`, else the nearest ancestor holding a .git
#   entry, else the router's own directory. The .git walk keeps the rule the
#   same on hosts where git is not on PATH (and makes it testable hermetically).
kb_repo_root() {
  local router="${1:-}" dir root probe
  dir="$(cd "$(dirname "$router")" && pwd -P)"

  if [ -n "${KB_REPO_ROOT:-}" ]; then
    root="$KB_REPO_ROOT"
  elif command -v git >/dev/null 2>&1; then
    root="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)"
  fi

  if [ -z "${root:-}" ]; then
    probe="$dir"
    while [ -n "$probe" ] && [ "$probe" != "/" ]; do
      if [ -e "$probe/.git" ]; then
        root="$probe"
        break
      fi
      probe="$(dirname "$probe")"
    done
  fi

  if [ -z "${root:-}" ]; then
    root="$dir"
  fi
  (cd "$root" && pwd -P)
}

# kb_default_handoff <script-dir>
#   Default router path: resolved against the git repo root when that can be
#   determined portably (git, then a .git ancestor of the script directory),
#   else left relative to the current directory.
kb_default_handoff() {
  local script_dir="${1:-}" root=""

  if [ -n "${KB_REPO_ROOT:-}" ]; then
    root="$KB_REPO_ROOT"
  elif command -v git >/dev/null 2>&1; then
    root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || true)"
  fi
  if [ -z "$root" ] && [ -e "$script_dir/../.git" ]; then
    root="$(cd "$script_dir/.." && pwd -P)"
  fi

  if [ -n "$root" ] && [ -f "$root/$KB_DEFAULT_HANDOFF_REL" ]; then
    printf '%s/%s\n' "$root" "$KB_DEFAULT_HANDOFF_REL"
  else
    printf '%s\n' "$KB_DEFAULT_HANDOFF_REL"
  fi
}

# kb_resolve_entry <router-file> <entry>
#   Manifest paths are relative to the router's directory; a miss there falls
#   back to the repo root. Prints the absolute path, returns non-zero when it
#   exists in neither place.
kb_resolve_entry() {
  local router="${1:-}" entry="${2:-}" dir root candidate
  dir="$(cd "$(dirname "$router")" && pwd -P)"
  root="$(kb_repo_root "$router")"

  candidate="$dir/$entry"
  if [ -f "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  candidate="$root/$entry"
  if [ -f "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

# kb_rel_path <root> <abs-path>
#   <abs-path> relative to <root> when it lives underneath it, else unchanged.
kb_rel_path() {
  local root="${1:-}" path="${2:-}"
  case "$path" in
    "$root"/*) printf '%s\n' "${path#"$root"/}" ;;
    *)         printf '%s\n' "$path" ;;
  esac
}

# kb_sha256 <file>
#   Prints the lowercase hex sha256 digest of <file>'s bytes.
#   The tool is DISCOVERED, never assumed: `shasum -a 256` ships with stock
#   macOS, `sha256sum` with GNU coreutils, `openssl dgst -sha256` with Git for
#   Windows (which has neither of the other two). Probing all three is what lets
#   one script serve every supported host without reaching for `stat`. When none
#   is present we HARD-FAIL: silently skipping the comparison would turn a
#   fail-closed gate into a no-op, which is worse than a loud refusal.
kb_sha256() {
  local file="${1:-}" digest
  if [ -z "$file" ]; then
    printf 'ERROR: kb_sha256: no file given.\n' >&2
    return 1
  fi
  if [ ! -f "$file" ]; then
    printf 'ERROR: kb_sha256: not a regular file: %s\n' "$file" >&2
    return 1
  fi

  if command -v shasum >/dev/null 2>&1; then
    digest=$(shasum -a 256 "$file" | awk '{ print $1 }')
  elif command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum "$file" | awk '{ print $1 }')
  elif command -v openssl >/dev/null 2>&1; then
    # Third probe: Git Bash on Windows ships openssl but neither shasum nor
    # sha256sum. Both OpenSSL 1.1 ("SHA256(f)= <hex>") and 3.x
    # ("SHA2-256(f)= <hex>") put the digest LAST, so $NF is the hex field.
    digest=$(openssl dgst -sha256 "$file" | awk '{ print $NF }')
  else
    printf 'ERROR: kb_sha256: none of "shasum -a 256", "sha256sum" or "openssl dgst -sha256" is on PATH; cannot compute a checksum. Install one of them (shasum ships with macOS, sha256sum comes with GNU coreutils, openssl with Git for Windows).\n' >&2
    return 1
  fi

  # A tool that exits 0 but emits something short of a full digest is broken,
  # and a short string would compare unequal to every recorded value -- i.e. a
  # permanent, unexplained STALE. Fail loudly here instead.
  if [ "${#digest}" -ne 64 ]; then
    printf 'ERROR: kb_sha256: could not compute a 64-char digest for %s (got %d chars).\n' \
      "$file" "${#digest}" >&2
    return 1
  fi
  printf '%s\n' "$digest"
}
