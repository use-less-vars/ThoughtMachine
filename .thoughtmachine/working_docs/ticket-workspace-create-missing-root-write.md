# Ticket: workspace creation never writes `cfg["root"]`, so API-created workspaces stay host-bash-dead

Status: open

## Problem

D3: `POST /api/workspace` → `create_workspace` / `ensure_workspace_dirs` never write `cfg["root"]`
into the workspace config. A workspace created through the API therefore has no `root`, and
everything that resolves the workspace root from config treats it as unresolvable — most visibly
`host_bash`, which cannot run in such a workspace. The recent schema-manifest change only silenced
the drift *warning* for the missing key; it did not add the write, so the underlying defect is
unchanged and now also reports less loudly.

## Evidence

- `create_workspace` / `ensure_workspace_dirs` in `thoughtmachine/workspace_capabilities.py` create
  the directory tree but never persist a `root` entry.
- The schema-manifest fix addressed the warning surface only; the write path is still missing.
- `root` is one of the engine-read keys, so `vault_repair` will not add it either: a missing `root`
  is a manual-review item, not something the repair tool fills in.

## Suggested fix

Write `cfg["root"]` at creation time, in the same place the other derived fields are persisted, and
cover it with a test that creates a workspace through the API and asserts (a) `root` is present in
the persisted config and (b) a `host_bash` call in that workspace is not refused for want of a
root. Do not paper over this by making `vault_repair --apply` insert engine-read keys.
