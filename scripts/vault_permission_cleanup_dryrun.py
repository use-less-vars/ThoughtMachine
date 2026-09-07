#!/usr/bin/env python3
"""Vault permission cleanup \u2014 DRY-RUN simulator v3 (scoped locations; read-only; writes nothing).

PURPOSE
-------
Scan a ThoughtMachine vault root for JSON files that carry permission
configuration and print, for each permission-bearing file, what a cleanup
pass WOULD do to its permission dict(s).  Version 3 processes ONLY the
exact permission-dict locations listed below; every other ``*.json`` file
gets a one-line skip note and no other key in any file is ever examined,
flagged or reported.  The script only reads and prints \u2014 no file is ever
opened for writing (see READ-ONLY PROOF at the end of every run).

VAULT-ROOT RESOLUTION ORDER
---------------------------
1. ``--vault-root PATH`` command-line flag
2. environment variable ``THOUGHTMACHINE_VAULT_ROOT``
3. default ``~/.thoughtmachine``

If the resolved root does not exist / is not a directory the script prints
a clear error listing every source that was tried and exits with code 2.
Invalid-JSON and unreadable files are noted and skipped (non-fatal); the
run still exits 0.

SCOPED PERMISSION-DICT LOCATIONS (the ONLY locations ever processed)
-------------------------------------------------------------------
1. workspaces/<id>/config.json                       -> role=ceiling
   Permission dict: ONLY the top-level ``permissions`` value (label
   '$.permissions').  Absent -> skipped, no fallback.
2. workspaces/<id>/sessions/<sid>/permissions.json   -> role=session
   (also workspaces/<id>/sessions/permissions.json).  The JSON ROOT
   OBJECT is the permission dict (label 'root'); there is NO wrapper
   fallback and no whole-config recognition.  Root not an object ->
   skipped.
3. workspaces/<id>/sessions/<file>.json (depth-4 modern session
   descriptor files, any name \u2014 the real vault layout, e.g. "Agent
   Session ....json") and workspaces/<id>/sessions/<sid>/session.json /
   session_metadata*.json (depth 5) -> role=session.  Dict ONLY at
   ``metadata.session_config.session_permissions`` (label
   '$.metadata.session_config.session_permissions'); absent -> skipped.
4. sessions/... (LEGACY vault layout, a sessions/ tree at the vault ROOT)
   -> role=session.  permissions.json files are treated as the ROOT
   OBJECT (rule 2); every other *.json is a session descriptor and is
   read ONLY at metadata.session_config.session_permissions (rule 3).
5. user/defaults.json                                 -> role=session
   Dict ONLY at root key ``session_permissions``; absent -> skipped.
6. agent_config.json (basename match, any depth)      -> role=agent
   Dict ONLY at root key ``session_permissions``; absent -> skipped.
Every other *.json file -> role=other -> one-line skip note; its content
is NEVER examined (no literal-key search, no whole-config-as-map).

VOCABULARIES
------------
session vocab : git, filesystem, container, network, mcp, host_bash
                (applies to every role=session and role=agent dict)
ceiling vocab : session vocab + docker, system  (valid ONLY inside a
                workspaces/<id>/config.json 'permissions' dict)
  - docker in a ceiling dict is canonical: kept silently.
  - system  in a ceiling dict is KEPT but FLAGGED for human review
    ('? system ... (ceiling resource \u2014 KEPT, review later)') \u2014 never
    silently dropped, never silently accepted.
  - any other unknown key in a ceiling dict is KEPT + logged
    ('? key ... (workspace-only \u2014 KEPT, review later)').
unknown key in a SESSION/agent dict (not in session vocab, not legacy):
  DROPPED + logged ('- key: value (unknown key \u2014 would drop)') \u2014 the
  cleanup would delete it; counts as a change.
legacy keys : git_read, git_write, execution, git_allow_worktree_commits
  -> removed from every processed dict (execution = obsolete key; the
  other three feed the git merge below).

GIT MERGE (legacy git keys -> canonical 'git')
----------------------------------------------
Precedence (rules a-d):
  a) git_write == 'write_on_feature_branch' OR
     git_allow_worktree_commits is true (folded to the
     write_on_feature_branch level)            -> 'write_on_feature_branch'
  b) 'ask' in {git_read, git_write}             -> 'ask'
  c) git_write == 'write'                       -> 'write'
  d) otherwise                                  -> 'read'
- legacy values print verbatim on their removal lines.
- existing canonical 'git' == merged            -> '. consistent'
- existing canonical 'git' differs from merged  -> '! CONFLICT: KEEPING
  existing git <existing> (merged legacy would be <merged>) \u2014 human
  verify'; the merged value is NOT written (no silent overwrite).
- no existing 'git'                             -> '+ git: <value>'
- a 'banned' string on git_read/git_write       -> '! EDGE-CASE' line;
  the merge still follows the rule chain and never swallows 'banned'
  into a grant.

OUTPUT
------
Per permission-bearing file (concise: only the permission dict changes):
  === <relpath> [role=<role>] ===
  permissions dict at <loc>  (vocab: ceiling|session)
  BEFORE: <dict, sorted keys>
  ... change lines ('-', '~', '+', '!', '.', '?') ...
  AFTER:  <dict, sorted keys>
  => WOULD CHANGE | => NO CHANGE
Files with no dict at a scoped location and role=other files print a
one-line skip note; invalid/unreadable JSON prints a non-fatal note.
Then a counter SUMMARY (files scanned / would-change / no-change /
skipped / legacy-keys-removed / unknown-keys / conflicts), a
READ-ONLY PROOF line (aggregate md5 over every scanned JSON file's raw
bytes, sorted by vault-relative path \u2014 deterministic; rerun to confirm
nothing changed on disk) and:
  Dry-run complete \u2014 no writes performed.
Deterministic: sorted directory walk, sorted key output everywhere.

EXIT CODES
----------
0 \u2014 vault root usable and scan completed (invalid/unreadable JSON non-fatal)
2 \u2014 vault root missing / not a directory (all resolution sources listed)
"""

import argparse
import hashlib
import json
import os
import sys

# ---------------------------------------------------------------------------
# vocabularies & constants
# ---------------------------------------------------------------------------

SESSION_VOCAB = ("git", "filesystem", "container", "network", "mcp", "host_bash")
SESSION_VOCAB_SET = frozenset(SESSION_VOCAB)

# docker (canonical) and system (kept + flagged for review) are valid ONLY
# inside workspaces/<id>/config.json 'permissions' dicts.
CEILING_EXTRA = ("docker", "system")
CEILING_VOCAB = SESSION_VOCAB_SET | frozenset(CEILING_EXTRA)

# Removed from EVERY permission dict regardless of role.
LEGACY_KEYS = ("git_read", "git_write", "execution", "git_allow_worktree_commits")
LEGACY_SET = frozenset(LEGACY_KEYS)

# v3: root-map recognition / literal-key search and the role=other pass
# were removed — only the scoped locations in classify()/locate() run.


# ---------------------------------------------------------------------------
# path classification -> (role, variant)
# ---------------------------------------------------------------------------

def classify(relpath):
    """Return (role, variant) for a vault-relative path (posix separators).

    Order matters (most specific first).  v3 processes ONLY the scoped
    locations listed in the module docstring; every other ``*.json`` file
    is role='other' and gets a one-line skip note.
    """
    parts = relpath.split("/")
    n = len(parts)
    base = parts[-1]

    # 1) modern workspace ceiling: workspaces/<id>/config.json
    if n == 3 and parts[0] == "workspaces" and base == "config.json":
        return ("ceiling", "config")

    # 2) modern session trees: workspaces/<id>/sessions/...
    if n >= 4 and parts[0] == "workspaces" and parts[2] == "sessions":
        if base == "permissions.json" and n in (4, 5):
            # .../<sid>/permissions.json (depth 5) or .../permissions.json
            # (depth 4) -> the JSON ROOT OBJECT is the permission dict
            return ("session", "permissions_json")
        if n == 4:
            # depth-4 session descriptor files (any name; the real vault
            # layout, e.g. "Agent Session ....json", _meta_*.json)
            return ("session", "session_metadata")
        if n == 5 and (base == "session.json" or base.startswith("session_metadata")):
            return ("session", "session_metadata")

    # 3) legacy vault layout: sessions/ tree at the vault ROOT
    if parts[0] == "sessions":
        if base == "permissions.json":
            return ("session", "permissions_json")
        return ("session", "session_metadata")

    # 4) user/defaults.json
    if n == 2 and parts[0] == "user" and base == "defaults.json":
        return ("session", "defaults")

    # 5) agent_config.json (basename match) -> ONLY its 'session_permissions'
    if base == "agent_config.json":
        return ("agent", "agent_config")

    return ("other", "other")


# ---------------------------------------------------------------------------
# dict location (v3: NO literal-key search, NO whole-config recognition)
# ---------------------------------------------------------------------------


def locate(role, variant, doc):
    """Return [(label, permission_dict, vocab)] per the v3 role rules.

    Only the scoped locations from the module docstring are examined.
    An explicitly-present EMPTY dict still counts as a located permission
    dict (nothing to clean -> NO CHANGE); a non-dict at the scoped path
    yields no location (skip note).
    """
    if role == "ceiling":  # workspaces/<id>/config.json
        if isinstance(doc, dict) and isinstance(doc.get("permissions"), dict):
            return [("$.permissions", doc["permissions"], CEILING_VOCAB)]
        return []
    if role == "session":
        if variant == "permissions_json":
            # the JSON ROOT OBJECT is the permission dict (no wrapper
            # fallback, no whole-config recognition)
            if isinstance(doc, dict):
                return [("root", doc, SESSION_VOCAB_SET)]
            return []
        if variant == "session_metadata":
            try:
                sp = doc["metadata"]["session_config"]["session_permissions"]
            except (TypeError, KeyError):
                sp = None
            if isinstance(sp, dict):
                return [("$.metadata.session_config.session_permissions", sp,
                         SESSION_VOCAB_SET)]
            return []
        if variant == "defaults":  # user/defaults.json
            if isinstance(doc, dict) and isinstance(doc.get("session_permissions"), dict):
                return [("$.session_permissions", doc["session_permissions"],
                         SESSION_VOCAB_SET)]
            return []
    if role == "agent":  # agent_config.json: ONLY root 'session_permissions'
        if isinstance(doc, dict) and isinstance(doc.get("session_permissions"), dict):
            return [("$.session_permissions", doc["session_permissions"],
                     SESSION_VOCAB_SET)]
        return []
    return []  # role == 'other': never examined


# ---------------------------------------------------------------------------
# git merge + per-dict simulation
# ---------------------------------------------------------------------------

def _merge_git(perm_dict):
    """git_read/git_write/git_allow_worktree_commits -> canonical 'git'.

    Triggered ONLY when git_read or git_write is present OR when
    git_allow_worktree_commits is True (a bare git_allow_worktree_commits
    False never triggers a merge).  Rule chain (returns (value, rule
    letter), or (None, None) when untriggered):
      a) git_write == 'write_on_feature_branch' OR
         git_allow_worktree_commits is True  -> 'write_on_feature_branch'
      b) 'ask' in {git_read, git_write}       -> 'ask'
      c) git_write == 'write'                 -> 'write'
      d) otherwise                            -> 'read'
    Only string values participate in the ask/write comparisons; anything
    else falls through to rule d ('read').
    """
    gr = perm_dict.get("git_read")
    gw = perm_dict.get("git_write")
    gaw = perm_dict.get("git_allow_worktree_commits")
    if gr is None and gw is None and gaw is not True:
        return None, None
    if gaw is True or (isinstance(gw, str) and gw == "write_on_feature_branch"):
        return "write_on_feature_branch", "a"
    present_vals = [v for v in (gr, gw) if isinstance(v, str)]
    if "ask" in present_vals:
        return "ask", "b"
    if isinstance(gw, str) and gw == "write":
        return "write", "c"
    return "read", "d"


def simulate(perm_dict, vocab, lines, st):
    """Compute what a cleanup pass WOULD do to ONE permission dict.

    Read-only: operates on a copy.  Appends change lines to ``lines``,
    accumulates counters into ``st`` and returns (after_dict, changed).

    v3 semantics:
    - legacy keys are always removed (a change);
    - in a SESSION/agent dict, any key outside the session vocab (and not
      legacy) is DROPPED and logged ('- k: v (unknown key — would drop)')
      — a change;
    - in a CEILING dict, docker is canonical (silent), 'system' is KEPT
      but FLAGGED ('? system: v (ceiling resource — KEPT, review
      later)'), and any other unknown key is KEPT + logged ('? k: v
      (workspace-only — KEPT, review later)') — neither is a change.
    """
    keys = sorted(perm_dict)
    after = dict(perm_dict)
    ceiling = vocab is CEILING_VOCAB

    # banned edge-case detection on git_read/git_write string values
    edge = False
    for k in ("git_read", "git_write"):
        v = perm_dict.get(k)
        if isinstance(v, str) and "banned" in v:
            edge = True

    # --- legacy key removal (regardless of role) ---------------------------
    for k in keys:
        if k not in LEGACY_SET:
            continue
        v = perm_dict[k]
        hint = "obsolete key" if k == "execution" else "feeds git merge"
        lines.append("- %s: %s (legacy removed — %s)" % (k, json.dumps(v), hint))
        del after[k]
        st["legacy"][k] += 1
        st["legacy_total"] += 1

    # --- conservative git merge --------------------------------------------
    merged, rule = _merge_git(perm_dict)
    if merged is not None:
        st["merges"] += 1
        present = [k for k in ("git_read", "git_write",
                               "git_allow_worktree_commits") if k in perm_dict]
        src = " + ".join("%s %s" % (k, json.dumps(perm_dict[k])) for k in present)
        lines.append("~ merge rule %s: %s -> git" % (rule, src))
        if "git" in after:
            if after["git"] == merged:
                lines.append(". consistent")
            else:
                lines.append("! CONFLICT: KEEPING existing git %s (merged "
                             "legacy would be %s) — human verify"
                             % (json.dumps(after["git"]), json.dumps(merged)))
                st["conflicts"] += 1
        else:
            after["git"] = merged
            lines.append("+ git: %s (added by merge)" % json.dumps(merged))

    # --- banned edge-case report -------------------------------------------
    if edge:
        lines.append("! EDGE-CASE: \"banned\" value on git_read/git_write — "
                     "merged per rules (NOT swallowed); human review needed")
        st["edge"] += 1

    # --- unknown / extra keys (vocab-dependent) ----------------------------
    for k in keys:
        if k in LEGACY_SET:
            continue
        v = perm_dict[k]
        if ceiling:
            if k in SESSION_VOCAB_SET or k == "docker":
                continue  # canonical session/ceiling keys: silent
            if k == "system":
                lines.append("? system: %s (ceiling resource — KEPT, review later)"
                             % json.dumps(v))
            else:
                lines.append("? %s: %s (workspace-only — KEPT, review later)"
                             % (k, json.dumps(v)))
            st["unknown"] += 1
        else:
            if k in vocab:  # SESSION_VOCAB_SET: canonical, silent
                continue
            lines.append("- %s: %s (unknown key — would drop)"
                         % (k, json.dumps(v)))
            del after[k]
            st["unknown"] += 1

    return after, after != perm_dict


# ---------------------------------------------------------------------------
# main driver
# ---------------------------------------------------------------------------

def _vocab_name(vocab):
    return "ceiling" if vocab is CEILING_VOCAB else "session"


def run(root):
    """Scan ``root`` and print the dry-run report.  Never writes anything."""
    st = {
        "scanned": 0,
        "invalid": 0,
        "invalid_files": [],
        "perm_files": 0,
        "dicts": 0,
        "changed_files": 0,
        "clean_files": 0,
        "skipped_files": [],
        "legacy": {k: 0 for k in LEGACY_KEYS},
        "legacy_total": 0,
        "merges": 0,
        "unknown": 0,
        "edge": 0,
        "conflicts": 0,
    }
    digests = {}

    print("Vault root: %s" % root)
    print()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st["scanned"] += 1
            role, variant = classify(rel)

            try:
                with open(full, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as exc:
                st["invalid"] += 1
                st["invalid_files"].append(rel)
                print("=== %s [role=%s] ===" % (rel, role))
                print("unreadable — skipped (non-fatal: %s)" % exc)
                print()
                continue
            digests[rel] = hashlib.md5(text.encode("utf-8")).hexdigest()
            try:
                doc = json.loads(text)
            except ValueError as exc:
                st["invalid"] += 1
                st["invalid_files"].append(rel)
                print("=== %s [role=%s] ===" % (rel, role))
                print("invalid JSON — skipped (non-fatal: %s)" % exc)
                print()
                continue

            locs = locate(role, variant, doc)
            print("=== %s [role=%s] ===" % (rel, role))
            if not locs:
                st["skipped_files"].append(rel)
                if role == "other":
                    print("not a scoped permission-dict location — skipped")
                else:
                    print("no permission dict at scoped location — skipped")
                print()
                continue

            st["perm_files"] += 1
            file_changed = False
            for label, perm_dict, vocab in locs:
                st["dicts"] += 1
                lines = []
                after, changed = simulate(perm_dict, vocab, lines, st)
                file_changed = file_changed or changed
                print("permissions dict at %s  (vocab: %s)" % (label, _vocab_name(vocab)))
                print("BEFORE: " + json.dumps(perm_dict, sort_keys=True))
                for line in lines:
                    print(line)
                print("AFTER:  " + json.dumps(after, sort_keys=True))
            if file_changed:
                st["changed_files"] += 1
                print("=> WOULD CHANGE")
            else:
                st["clean_files"] += 1
                print("=> NO CHANGE")
            print()

    _print_summary(st)
    print()

    # READ-ONLY PROOF: aggregate md5 over every scanned JSON file's raw bytes
    # (sorted by vault-relative path) — deterministic; rerun and compare to
    # confirm the scan never modified anything on disk.
    agg = hashlib.md5()
    for rel in sorted(digests):
        agg.update(rel.encode("utf-8"))
        agg.update(b"\x00")
        agg.update(digests[rel].encode("ascii"))
        agg.update(b"\n")
    print("READ-ONLY PROOF: aggregate md5 over %d scanned json files "
          "(sorted by relpath) = %s" % (len(digests), agg.hexdigest()))
    print("Dry-run complete — no writes performed.")


def _print_summary(st):
    print("SUMMARY")
    print("-------")
    print("json files scanned: %d" % st["scanned"])
    print("invalid/unreadable JSON files: %d" % st["invalid"])
    for name in st["invalid_files"]:
        print("  - %s" % name)
    print("permission-bearing files: %d" % st["perm_files"])
    print("permission dicts located: %d" % st["dicts"])
    print("files that WOULD change: %d" % st["changed_files"])
    print("files NO CHANGE: %d" % st["clean_files"])
    print("files skipped: %d" % len(st["skipped_files"]))
    for name in st["skipped_files"]:
        print("  - %s" % name)
    print("legacy keys removed:")
    for k in LEGACY_KEYS:
        print("  %s: %d" % (k, st["legacy"][k]))
    print("  total: %d" % st["legacy_total"])
    print("git merges (git_read/git_write/git_allow_worktree_commits -> git): %d"
          % st["merges"])
    print("unknown keys (kept-flagged / dropped): %d" % st["unknown"])
    print("edge cases (banned): %d" % st["edge"])
    print("conflicts (existing git vs merged): %d" % st["conflicts"])


def resolve_vault_root(flag_root):
    """Return the vault root per flag > env > default, or exit(2) listing all."""
    env_val = os.environ.get("THOUGHTMACHINE_VAULT_ROOT") or None
    default_val = os.path.expanduser("~/.thoughtmachine")
    chosen = flag_root or env_val or default_val

    def show(source, value):
        if value is None:
            shown = "(not provided)" if source.startswith("--") else "(not set)"
            return "    %-40s %s" % (source + ":", shown)
        p = os.path.abspath(os.path.expanduser(value))
        mark = "exists" if os.path.isdir(p) else "MISSING / not a directory"
        return "    %-40s %s   [%s]" % (source + ":", p, mark)

    if not os.path.isdir(os.path.abspath(os.path.expanduser(chosen))):
        print("VAULT ROOT ERROR — no usable vault root.")
        print("  Tried sources:")
        print(show("--vault-root flag", flag_root))
        print(show("env THOUGHTMACHINE_VAULT_ROOT", env_val))
        print(show("default ~/.thoughtmachine", default_val))
        print("  Aborting (exit code 2).")
        sys.exit(2)
    return os.path.abspath(os.path.expanduser(chosen))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Vault permission cleanup DRY-RUN (v3, scoped-locations). "
                    "Read-only: prints what a cleanup pass WOULD do; never writes.",
        epilog="Resolution order: --vault-root flag, then env "
               "THOUGHTMACHINE_VAULT_ROOT, then ~/.thoughtmachine.",
    )
    parser.add_argument(
        "--vault-root",
        metavar="PATH",
        default=None,
        help="Vault root to scan (default: env THOUGHTMACHINE_VAULT_ROOT, "
             "then ~/.thoughtmachine).",
    )
    args = parser.parse_args(argv)

    root = resolve_vault_root(args.vault_root)
    run(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
