# vault_repair (Phase 2: read-only diagnosis + guarded repair)

`thoughtmachine.vault_repair` inspects a ThoughtMachine vault (default
`~/.thoughtmachine`, or `$THOUGHTMACHINE_VAULT_ROOT`) and reports schema
drift, permission-dict drift, seeded-file drift and unknown root files.
**Default mode is read-only diagnosis** — nothing is written. `--apply`
performs machine repair under strict guardrails (timestamped sibling backups,
`.quarantine` artifacts, permission tightening only), and seed restoration
requires `--restore-seeds --yes`. The module never raises and is stdlib-only
apart from the repo-local `agent.config.vault_drift` import (version 0.2.0),
so it can be invoked on the host:

```
python3 -m thoughtmachine.vault_repair [--vault-root PATH] [--report-json PATH]
    [--dry-run] [--apply] [--quarantine-dir PATH] [--restore-seeds] [--yes]
```

Options:

| flag | effect |
|---|---|
| `--vault-root PATH` | vault root to inspect (default: `$THOUGHTMACHINE_VAULT_ROOT` or `~/.thoughtmachine`) |
| `--report-json PATH` | write the full JSON report to `PATH` |
| `--dry-run` | force read-only mode; `--apply` is neutralised (ignored) |
| `--apply` | mutate the vault: apply machine fixes (backups + quarantine artifacts) and re-scan |
| `--quarantine-dir PATH` | directory for removed-node artifacts (default: `<vault_root>/.quarantine`) |
| `--restore-seeds` | restore missing/drifted seeded files (requires `--yes`) |
| `--yes` | acknowledge the `--restore-seeds` mutation |

## Why `<vault_root>/.quarantine` and not `logs/quarantine`

Deliberate deviation: the manifest declares a `logs/*` pattern whose entries
are `root_type: string`, so the drift checker would feed log *directories* to
the text-file check and raise `DriftAbortError`. `.quarantine` is a hidden
root directory — dot-names are skipped by the drift checker and the
unknown-file scan, and the permission deep-scan prunes it — so quarantined
artifacts are invisible to every later scan.

## Exit codes

| code | meaning |
|---|---|
| `0` | healthy — no findings |
| `1` | findings present, but no fixes performed (read-only/dry-run, or `--apply` left findings unfixed) |
| `2` | usage/argument error (argparse; e.g. unknown flag or missing value) |
| `3` | vault root missing/unreadable, or repair errors occurred |
| `4` | `--apply` performed one or more fixes |

Precedence: repair errors (`3`) override applied fixes (`4`); applied fixes
(`4`) override remaining findings; `0` is returned only for a clean vault.
`2` is raised by argparse before any vault work — a successful run never
exits 2.

## Repair guardrails (`--apply`)

- **Backups first, never deleted, never rescanned.** Every rewrite of an
  existing file is preceded by a timestamped sibling
  `<name>.bak-<YYYYmmddHHMMSS>` backup (a `-N` suffix keeps the name unique).
  Names deliberately do not end in plain `.bak`, so the drift unknown-file
  scan would flag one that leaked into manifest scope; the tool filters them
  out of every scan it performs.
- **Nothing is deleted.** Removed nodes (unknown top-level keys, drift,
  extra files) are quarantined as JSON artifacts under `.quarantine` (dir
  mode `0700`) with secret values redacted to `<redacted>` (hint keys:
  `api_key`/`apikey`/`secret`/`token`/`password`/`credential`).
- **Permission tightening only.** Legacy `git_read`/`git_write`/`execution`
  fields fold into the canonical `git` grain at *at least* their legacy
  permissiveness (merge-rank descending, breadth ascending as tie-break; an
  existing canonical `git` entry wins; never downgrades). `docker` maps to
  `container` only inside ceiling dicts. The engine-read flag
  `git_allow_worktree_commits` — folded to `git: write` at load by the
  agent/config validators (`agent/config/models.py`,
  `agent/config/session_config.py`) — is **never** auto-removed; it is
  downgraded to a `manual_review` finding.
- **Allowlist and seeds are not auto-fixed.** Tampered
  `checksystem_allowlist` entries are quarantined, never repaired; seeded
  files are restored only under `--restore-seeds --yes`.

## Missing-field backfill

Missing fields on pattern manifest entries (e.g.
`workspaces/*/config.json` `allow_host_resources`) are backfilled: the spec
is resolved via `_manifest_spec_for` (an exact manifest key match wins;
otherwise the first sorted pattern key whose segments fnmatch), and the
schema `default_value` is attached when the `missing field (repair pending)`
issue is collected. Missing vault files whose schema declares a
`safe_default` are `backfill_pending` and machine-appliable.

## CheckSystem tool capabilities

- `vault_repair_status` — read-only full dry-run report; mirrors
  `vault_status` root resolution (`thoughtmachine.vault.vault_root`, env
  `THOUGHTMACHINE_VAULT_ROOT`).

Repair is never agent-callable: the mutating apply surface was removed from
CheckSystem (dispatch entry, passthrough params and method deleted) — agents
have no apply path (`vault_repair_apply` removed). Repairs are applied via
the CLI (`python3 -m thoughtmachine.vault_repair --apply`) or through the
UI REST endpoints below — never through an agent query.

## REST endpoints (Vault Health panel)

The web UI backend exposes two operator-facing endpoints for the Vault
Health panel in the landing page. Both endpoints honour a local-origin guard:
the `Origin`/`Referer` header must match `http(s)://localhost` or
`http(s)://127.0.0.1` (any port) — anything else is refused with 403
(`origin not allowed`). Neither endpoint is reachable by agents.

### GET /api/vault/repair/status

Read-only. Runs the full `run_inspection` dry run and returns the complete
report (`run`, `summary`, `issues`, `extra_files`, `seeded_files`). Never
mutates the vault.

### POST /api/vault/repair/apply

Applies machine repairs (same guardrails as CLI `--apply`: timestamped
sibling backups, `.quarantine` artifacts, permission tightening only).
Request body:

- exactly one of `repair_ids` (list of issue ids) or `categories` (list of
  issue categories) — not both, not neither;
- `confirmed: true` — explicit operator confirmation.

| code | condition |
|---|---|
| 200 | applied; body `{report, backups_created, files_changed, quarantine_moves, errors}` (`files_changed` sorted) |
| 400 | body has both or neither of `repair_ids`/`categories` — `'exactly one of repair_ids or categories is required'` |
| 400 | `confirmed` missing or not true — `'confirmation required (confirmed must be true)'` |
| 400 | vault root missing/invalid |
| 403 | request origin is not localhost / 127.0.0.1 — `'origin not allowed'` |
| 500 | repair engine error |

## Report shape

Top-level keys: `run`, `summary`, `issues`, `extra_files`, `seeded_files`.
After `--apply`/`--restore-seeds` the report adds a `repair` block:
`{requested_apply, restore_seeds, performed[], backups[{backup, backup_of}]}`.

Full design: `working_docs/vault_repair_design.md`.
