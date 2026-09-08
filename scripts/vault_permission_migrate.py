#!/usr/bin/env python3
"""Vault permission cleanup — MIGRATE (apply) tool, sibling of the v3 dry-run simulator.

PURPOSE
-------
The dry-run simulator (scripts/vault_permission_cleanup_dryrun.py) prints what
a cleanup pass WOULD do to every scoped permission-dict location in a vault.
This script is its write-side sibling: it runs the EXACT same analysis and can
persist the simulated edits to disk (--apply).  All analysis logic is imported
from the reference module (single source of truth — no logic is duplicated
here): the vocab/legacy constants, classify(), locate(), simulate() and
_merge_git() are reused verbatim, so the migration can never drift from what
the dry-run reports.

MODES
-----
Default (no --apply): byte-identical dry run.  The reference module's own
run()/resolve_vault_root() code paths are called directly, so stdout is
byte-identical to running scripts/vault_permission_cleanup_dryrun.py against
the same vault root — including the per-file plan, the SUMMARY block, the
READ-ONLY PROOF aggregate md5 and the "Dry-run complete — no writes
performed." line.

--apply: writes the cleanup.
  1. Prints a prominent warning that the engine must be stopped.
  2. Analyses the whole vault root with the reference rules.
  3. For every file the analysis reports as would-change, BEFORE any write:
     refuses with an error if <file>.bak already exists (unless --force-bak).
  4. For each such file, in deterministic walk order:
       - writes <file>.bak containing the exact original bytes;
       - prints the per-file edit lines mirroring the reference labels
         ('=== rel [role=r] ===', 'permissions dict at LABEL  (vocab: ...)',
         'BEFORE: ...', the reference simulate() change lines, 'AFTER:  ...')
         plus a 'backup: <path>' line and '=> WOULD CHANGE (applied)';
       - persists the after-state: the located permission dict object is
         mutated in place (clear + update with the reference simulate()
         result) so nested paths survive, then the whole document is written
         with json.dumps(indent=2, ensure_ascii=False) plus a trailing
         newline.  All other document content and key order are preserved
         (json.load keeps insertion order).
  5. POST-APPLY VERIFICATION: re-runs the full analysis over the root and
     requires 0 files that WOULD change remain; otherwise prints the
     offending files and exits 3.

SAFETY
------
Never writes outside the resolved vault root: every edited path is produced
by os.walk(root) over the resolved root and backups are path + '.bak'.

VAULT-ROOT RESOLUTION ORDER (identical to the reference)
--------------------------------------------------------
1. --vault-root PATH command-line flag
2. environment variable THOUGHTMACHINE_VAULT_ROOT
3. default ~/.thoughtmachine

If the resolved root does not exist / is not a directory the reference's own
resolve_vault_root() prints the exact 'VAULT ROOT ERROR ... Aborting (exit
code 2).' message listing every source tried and exits with code 2.

EXIT CODES
----------
0 — success (dry run completed, or --apply completed and the post-apply
    verification found 0 files that would change)
2 — vault root missing / not a directory (identical message to the reference)
3 — apply failed: an existing <file>.bak was refused (no --force-bak), a
    backup could not be written, or the post-apply re-analysis still found
    files that WOULD change
"""

import argparse
import importlib.util
import json
import os
import shutil
import sys

_REF_NAME = "vault_permission_cleanup_dryrun"
_REF_FILE = "vault_permission_cleanup_dryrun.py"


def _load_reference():
    """Load the dry-run simulator module from the same directory.

    scripts/ has no __init__.py, so the module is loaded explicitly by path;
    the module's top level is side-effect free (constants + function defs).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, _REF_FILE)
    spec = importlib.util.spec_from_file_location(_REF_NAME, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Single source of truth: every analysis constant/rule comes from the
# reference module.  No duplicated analysis logic below.
ref = _load_reference()


def _fresh_counters():
    """A stats dict shaped exactly like the reference run()'s, so that the
    reference simulate() can increment counters without KeyError."""
    return {
        "scanned": 0,
        "invalid": 0,
        "invalid_files": [],
        "perm_files": 0,
        "dicts": 0,
        "changed_files": 0,
        "clean_files": 0,
        "skipped_files": [],
        "legacy": {k: 0 for k in ref.LEGACY_KEYS},
        "legacy_total": 0,
        "merges": 0,
        "unknown": 0,
        "edge": 0,
        "conflicts": 0,
    }


def _scan(root):
    """Analyse every ``*.json`` file under ``root`` with the reference rules.

    Read-only: never mutates documents and never writes to disk.  Yields one
    record per JSON file:
      - record['full'] / record['rel'] / record['role']
      - record['note']          -> file was unreadable / invalid JSON /
                                    skipped (no permission dict)
      - record['doc'] / record['locs'] / record['would_change'] otherwise;
        each loc carries label, perm_dict (live object inside doc), vocab,
        lines (reference simulate() change lines) and after (reference
        after-state dict).
    Walk order is identical to the reference (sorted dirs and files).
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            role, variant = ref.classify(rel)
            rec = {"full": full, "rel": rel, "role": role}
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as exc:
                rec["note"] = "unreadable \u2014 skipped (non-fatal: %s)" % exc
                yield rec
                continue
            try:
                doc = json.loads(text)
            except ValueError as exc:
                rec["note"] = "invalid JSON \u2014 skipped (non-fatal: %s)" % exc
                yield rec
                continue
            locs = ref.locate(role, variant, doc)
            if not locs:
                if role == "other":
                    rec["note"] = ("not a scoped permission-dict location "
                                   "\u2014 skipped")
                else:
                    rec["note"] = ("no permission dict at scoped location "
                                   "\u2014 skipped")
                yield rec
                continue
            rec["doc"] = doc
            rec["locs"] = []
            for label, perm_dict, vocab in locs:
                lines = []
                after, changed = ref.simulate(perm_dict, vocab, lines,
                                              _fresh_counters())
                rec["locs"].append({
                    "label": label,
                    "perm_dict": perm_dict,
                    "vocab": vocab,
                    "lines": lines,
                    "after": after,
                    "changed": changed,
                })
            rec["would_change"] = any(x["changed"] for x in rec["locs"])
            yield rec


_ENGINE_WARNING = (
    "WARNING: --apply mode \u2014 this run WILL WRITE to the vault.\n"
    "         STOP the ThoughtMachine engine (poller / workers) before running:\n"
    "         the vault must be quiescent while permissions are migrated."
)


def _apply(root, force_bak):
    """Persist the simulated cleanup for every would-change file."""
    print(_ENGINE_WARNING)
    print()
    print("Vault root: %s" % root)
    print()

    edits = [rec for rec in _scan(root) if rec.get("would_change")]

    # Preflight: refuse to overwrite an existing backup unless forced.
    # Checked for ALL planned files before any write happens.
    conflicts = []
    for rec in edits:
        bak = rec["full"] + ".bak"
        if os.path.exists(bak) and not force_bak:
            conflicts.append(bak)
    if conflicts:
        print("ERROR: refusing to overwrite existing backup file(s):")
        for bak in conflicts:
            print("  - %s" % bak)
        print("  pass --force-bak to overwrite.  Aborting before any write "
              "(exit code 3).")
        return 3

    migrated = 0
    for rec in edits:
        full = rec["full"]
        bak = full + ".bak"
        try:
            shutil.copyfile(full, bak)  # byte-exact copy of the original
        except OSError as exc:
            print("ERROR: could not create backup %s (%s) \u2014 aborting "
                  "(exit code 3)." % (bak, exc))
            return 3

        # Per-file edit lines mirroring the reference labels, printed before
        # the in-place mutation so BEFORE shows the original content.
        print("=== %s [role=%s] ===" % (rec["rel"], rec["role"]))
        for loc in rec["locs"]:
            print("permissions dict at %s  (vocab: %s)"
                  % (loc["label"], ref._vocab_name(loc["vocab"])))
            print("BEFORE: " + json.dumps(loc["perm_dict"], sort_keys=True))
            for line in loc["lines"]:
                print(line)
            print("AFTER:  " + json.dumps(loc["after"], sort_keys=True))
        print("backup: %s" % bak)
        print("=> WOULD CHANGE (applied)")
        print()

        # Persist the after-state: mutate the located dict object in place
        # (clear + update with the reference simulate() result) so nested
        # paths survive, then dump the whole document.  json.load preserves
        # key order; json.dump keeps it; untouched content is preserved.
        for loc in rec["locs"]:
            if loc["changed"]:
                perm_dict = loc["perm_dict"]
                perm_dict.clear()
                perm_dict.update(loc["after"])
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec["doc"], indent=2, ensure_ascii=False)
                     + "\n")
        migrated += 1

    # POST-APPLY VERIFICATION: re-run the full analysis over the root and
    # require 0 would-change files remain.
    remaining = [rec for rec in _scan(root) if rec.get("would_change")]
    if remaining:
        print("ERROR: post-apply verification failed \u2014 %d file(s) still "
              "WOULD change:" % len(remaining))
        for rec in remaining:
            print("  - %s" % rec["rel"])
        print("  Aborting (exit code 3).")
        return 3
    print("POST-APPLY VERIFICATION: full re-analysis reports 0 files that "
          "WOULD change.")
    print("Migration complete \u2014 %d file(s) migrated; original bytes saved "
          "as <file>.json.bak." % migrated)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Vault permission cleanup \u2014 MIGRATE (apply sibling of "
                    "the v3 dry-run simulator).  Default: dry run with "
                    "byte-identical output to "
                    "scripts/vault_permission_cleanup_dryrun.py.  Pass --apply "
                    "to write the simulated cleanup to disk.",
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cleanup: write <file>.json.bak backups and persist "
             "the simulated edits.  Default is a read-only dry run.",
    )
    parser.add_argument(
        "--force-bak",
        action="store_true",
        help="Overwrite an existing <file>.json.bak instead of refusing "
             "(only used with --apply).",
    )
    args = parser.parse_args(argv)

    # Exact reference resolution: prints the exact VAULT ROOT ERROR message
    # (listing every source tried) and sys.exit(2) for a non-directory root.
    root = ref.resolve_vault_root(args.vault_root)
    if not args.apply:
        ref.run(root)  # byte-identical dry-run report (read-only)
        return 0
    return _apply(root, args.force_bak)


if __name__ == "__main__":
    sys.exit(main())
