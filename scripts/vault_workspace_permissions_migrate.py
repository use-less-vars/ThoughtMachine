#!/usr/bin/env python3
"""Vault workspace permissions migration (companion to the permission migrators).

PURPOSE
-------
Every workspace lives at ``<vault_root>/workspaces/<id>/config.json``.  The
permission store reads that file's top-level ``permissions`` map as the
workspace CEILING.  A workspace whose ``config.json`` is missing, or present
but without a ``permissions`` key, has NO explicit ceiling -- and a MISSING
file makes ``permission_store.workspace_ceiling`` raise, which makes the
security gate fail CLOSED to deny-all for that workspace.

This script backfills an explicit ceiling for every workspace that lacks one,
so the (currently fail-closed) missing-/empty-config workspaces get a
deterministic, deny-by-default ceiling:

* a ``config.json`` that ALREADY has a ``permissions`` key is left completely
  untouched (bucket ``already-migrated``);
* a ``config.json`` present WITHOUT ``permissions`` gets the key added while
  every other key/value is preserved (bucket ``had-config-no-perms``);
* a workspace with NO ``config.json`` gets one created (bucket ``no-config``).

The block written is ``{"permissions": {...}}``.  When the workspace declares a
``purpose`` the preset for that purpose supplies the map
(``agent/config/workspace_purpose.py::apply_purpose_preset``); otherwise -- and
whenever the preset lookup raises -- the ALL-BANNED block is used: every key in
``security.resource_catalog.RESOURCE_CATALOG`` is ``"banned"`` except
``container``, which is the boolean ``False``.

PURPOSE SOURCE
--------------
``config.json`` is the single source of truth for a workspace's ``purpose``
(the reader mirrors ``tools/host_resource_policy.py::load_workspace_config`` /
``tools/workspace/check_system.py``: ``config_data.get("purpose")``).  No
dedicated workspace-registry ``purpose`` reader exists at HEAD, so the
``config.json`` source is the only one implemented.

MODES
-----
Default (no ``--apply``) is a DRY RUN: it prints the plan and the buckets and
writes NOTHING -- no file, no backup, no new directory, no mtime change.

``--apply`` writes.  Every existing file that is rewritten is preceded by a
byte-exact sibling backup ``config.json.bak-<YYYYmmddHHMMSS>`` (14 digits).
All planned writes are preflighted BEFORE anything is written.

VAULT-ROOT RESOLUTION ORDER (identical to the sibling migrators)
---------------------------------------------------------------
1. ``--vault-root PATH`` command-line flag
2. environment variable ``THOUGHTMACHINE_VAULT_ROOT``
3. default ``~/.thoughtmachine`` (the shared ``DEFAULT_VAULT_ROOT`` constant)

If the resolved root does not exist / is not a directory the script prints a
'VAULT ROOT ERROR' message listing every source tried and exits with code 2.

EXIT CODES
----------
0  success
2  no usable vault root
3  apply failure (e.g. a backup could not be written)
"""

import argparse
import datetime
import json
import os
import shutil
import sys

# The repo root (parent of this script's directory) must be importable so the
# canonical catalogs resolve whether this file is executed directly or loaded
# by path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.config.workspace_purpose import apply_purpose_preset
from security.resource_catalog import RESOURCE_CATALOG
from thoughtmachine.vault_repair import DEFAULT_VAULT_ROOT


# ---------------------------------------------------------------------------
# default block
# ---------------------------------------------------------------------------


def all_banned_block():
    """Return ``{"permissions": {...}}``: every catalog key 'banned' except
    ``container`` (False).  Keys and order are DERIVED from RESOURCE_CATALOG so
    the block can never drift from the canonical catalog."""
    perms = {}
    for key in RESOURCE_CATALOG:
        perms[key] = False if key == "container" else "banned"
    return {"permissions": perms}


def block_for_purpose(purpose):
    """Return ``{"permissions": apply_purpose_preset(purpose)}``.

    The preset resolver returns a FLAT map; it is WRAPPED under the
    ``permissions`` key here.  May raise for an unhashable ``purpose`` -- the
    caller catches that and falls back to the all-banned block.
    """
    return {"permissions": apply_purpose_preset(purpose)}


# ---------------------------------------------------------------------------
# vault-root resolution
# ---------------------------------------------------------------------------


def resolve_vault_root(flag_root=None):
    """Return the vault root per flag > env > default, or exit(2) listing all."""
    env_val = os.environ.get("THOUGHTMACHINE_VAULT_ROOT") or None
    default_val = os.path.expanduser(DEFAULT_VAULT_ROOT)
    chosen = flag_root or env_val or default_val

    def show(source, value):
        if value is None:
            shown = "(not provided)" if source.startswith("--") else "(not set)"
            return "    %-40s %s" % (source + ":", shown)
        p = os.path.abspath(os.path.expanduser(value))
        mark = "exists" if os.path.isdir(p) else "MISSING / not a directory"
        return "    %-40s %s   [%s]" % (source + ":", p, mark)

    if not os.path.isdir(os.path.abspath(os.path.expanduser(chosen))):
        print("VAULT ROOT ERROR \u2014 no usable vault root.")
        print("  Tried sources:")
        print(show("--vault-root flag", flag_root))
        print(show("env THOUGHTMACHINE_VAULT_ROOT", env_val))
        print(show("default %s" % DEFAULT_VAULT_ROOT, default_val))
        print("  Aborting (exit code 2).")
        sys.exit(2)
    return os.path.abspath(os.path.expanduser(chosen))


# ---------------------------------------------------------------------------
# scan + plan
# ---------------------------------------------------------------------------


def _iter_workspace_configs(root):
    """Yield ``(workspace_dir, config_path)`` for every workspace directory."""
    workspaces_dir = os.path.join(root, "workspaces")
    if not os.path.isdir(workspaces_dir):
        return
    for name in sorted(os.listdir(workspaces_dir)):
        ws_dir = os.path.join(workspaces_dir, name)
        if not os.path.isdir(ws_dir):
            continue
        yield ws_dir, os.path.join(ws_dir, "config.json")


def _dump(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def build_plan(root):
    """Scan *root* and return the plan.

    Returns ``(buckets, plan, purpose_ok, all_banned)`` where *buckets* maps the
    three bucket names to their path lists, *plan* is the ordered list of
    pending writes (``{"cfg": path, "kind": "rewrite"|"create", "doc": doc}``),
    *purpose_ok* counts workspaces migrated via a purpose preset and
    *all_banned* counts workspaces that received the all-banned block.
    """
    buckets = {
        "already-migrated": [],
        "had-config-no-perms": [],
        "no-config": [],
        "unreadable-config": [],
    }
    all_banned_paths = []
    plan = []
    purpose_ok = 0

    for ws_dir, cfg in _iter_workspace_configs(root):
        if os.path.exists(cfg):
            try:
                with open(cfg, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                # Exists but cannot be read / parsed: report it under its own
                # bucket (it receives no block and is never written) while
                # KEEPING the per-workspace diagnostic.
                buckets["unreadable-config"].append(cfg)
                print("  ! skipped unreadable config %s: %s" % (cfg, exc))
                continue
            if not isinstance(doc, dict):
                print("  ! skipped non-object config %s" % cfg)
                continue
            if "permissions" in doc:
                buckets["already-migrated"].append(cfg)
                continue
            # present, no permissions -> add the key, preserve everything else
            block, used_purpose = _resolve_block(doc.get("purpose"), cfg)
            doc["permissions"] = block["permissions"]
            buckets["had-config-no-perms"].append(cfg)
            plan.append({"cfg": cfg, "kind": "rewrite", "doc": doc})
        else:
            # absent -> create with the block (no purpose source exists)
            block, used_purpose = _resolve_block(None, cfg)
            doc = {"permissions": block["permissions"]}
            buckets["no-config"].append(cfg)
            plan.append({"cfg": cfg, "kind": "create", "doc": doc})

        if used_purpose:
            purpose_ok += 1
        else:
            all_banned_paths.append(cfg)

    return buckets, plan, all_banned_paths, purpose_ok


def _resolve_block(purpose, cfg):
    """Return ``(block, used_purpose)`` for *purpose* (or all-banned).

    ``used_purpose`` is True only when a purpose preset was applied; a lookup
    that RAISES is logged against *cfg* and falls back to all-banned.
    """
    if purpose is not None:
        try:
            return block_for_purpose(purpose), True
        except Exception as exc:  # noqa: BLE001 -- never abort on one workspace
            print(
                "  ! purpose preset lookup failed for %s: %s: %s"
                % (cfg, type(exc).__name__, exc)
            )
    return all_banned_block(), False


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def print_report(root, buckets, all_banned_paths, purpose_ok, apply_mode):
    print("Vault workspace permissions migration")
    print("vault root: %s" % root)
    print("mode: %s" % ("apply" if apply_mode else "dry-run"))
    print("")
    for name in (
        "already-migrated",
        "had-config-no-perms",
        "no-config",
        "unreadable-config",
    ):
        paths = buckets[name]
        print("%s: %d" % (name, len(paths)))
        for p in paths:
            print("  %s" % p)
    all_banned = len(all_banned_paths)
    for p in all_banned_paths:
        print("all-banned: %s" % p)
    print(
        "%d workspaces received a purpose preset; %d received all-banned."
        % (purpose_ok, all_banned)
    )


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def apply_plan(plan):
    """Write every planned file, backing up existing ones.  Returns exit code."""
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    # Preflight EVERYTHING before writing anything.
    for item in plan:
        cfg = item["cfg"]
        parent = os.path.dirname(cfg)
        if not os.path.isdir(parent):
            print("APPLY ERROR: parent directory missing: %s" % parent)
            return 3
        if item["kind"] == "rewrite":
            bak = "%s.bak-%s" % (cfg, stamp)
            if os.path.exists(bak):
                print("APPLY ERROR: backup already exists: %s" % bak)
                return 3

    for item in plan:
        cfg = item["cfg"]
        try:
            if item["kind"] == "rewrite":
                bak = "%s.bak-%s" % (cfg, stamp)
                shutil.copyfile(cfg, bak)
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(_dump(item["doc"]))
        except OSError as exc:
            print("APPLY ERROR: could not write %s: %s" % (cfg, exc))
            return 3
        if item["kind"] == "rewrite":
            print("backup: %s.bak-%s" % (cfg, stamp))

    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run(root, apply=False):
    buckets, plan, all_banned_paths, purpose_ok = build_plan(root)
    print_report(root, buckets, all_banned_paths, purpose_ok, apply)
    if not apply:
        print("Dry-run complete \u2014 no writes performed.")
        return 0
    print("")
    print("APPLY: writing %d file(s)" % len(plan))
    return apply_plan(plan)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Backfill an explicit workspace permission ceiling into every "
            "workspaces/<id>/config.json that lacks one. Default is a dry run; "
            "--apply writes (existing files are backed up first)."
        ),
        epilog=(
            "Resolution order: --vault-root flag, then env "
            "THOUGHTMACHINE_VAULT_ROOT, then ~/.thoughtmachine."
        ),
    )
    parser.add_argument(
        "--vault-root",
        metavar="PATH",
        default=None,
        help=(
            "Vault root to scan (default: env THOUGHTMACHINE_VAULT_ROOT, "
            "then ~/.thoughtmachine)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (existing files are backed up first).",
    )
    args = parser.parse_args(argv)

    root = resolve_vault_root(args.vault_root)
    return run(root, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
