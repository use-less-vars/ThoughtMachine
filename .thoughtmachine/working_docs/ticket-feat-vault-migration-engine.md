# Ticket: feat/vault-migration-engine — the vault has a renamer, not a migration path

**Filed:** 2026-10-31, read-only pass over `thoughtmachine/`, `scripts/` at `docs/w4-kb-braindump`.
**Severity:** MEDIUM — no supported path from an old vault layout to the current one; the
one script that exists compares a marker against a literal and is wired to nothing.
**Status:** open; verified by read. Not exercised against a real v1 vault.

## Finding

**1. There is exactly one migration artifact and it is a one-shot script.**
`scripts/migrate_vault.py` (279 lines) — docstring, line 2: *"Vault migration script —
migrates ~/.thoughtmachine to version 2 layout."* Its idempotency check compares the marker
file to a hard-coded string (`:27-32`):

```python
version_file = vault / "system" / ".vault_version"
if version_file.exists():
    current_version = version_file.read_text().strip()
    if current_version == "2":
        print("Vault already at version 2.")
        return 0
```

It aborts on a partial migration (`:36-45`), takes a `tarfile` backup before touching
anything (`:48-51`), and returns 0/1.

**2. Nothing calls it.** `grep -rn "migrate_vault"` across `*.py/*.md/*.sh/*.json` returns
only `tests/integration/test_vault_migration.py:11` (the test imports it) and
`scripts/doctor_checks.py:375` (a docstring that says another helper *"mirrors the guarded
import in scripts/migrate_vault.py"*). No bootstrap, launcher, server, or doctor check
invokes it. An upgrading user is never told it exists.

**3. The marker means three different things.** This is the substance of the ticket:

| Notion | Where | Value |
|---|---|---|
| layout ordinal | `scripts/migrate_vault.py:31` | the literal `"2"` |
| application version | `thoughtmachine/vault.py:339-342` seeds `system/.vault_version` from `bootstrap.get_version()`; `bootstrap.py:135-137` reads it from `.version` | e.g. `1.0.x` |
| manifest integer | `agent/config/vault_drift.py:205` → `"vault_version": manifest.get("vault_version", 1)` | an `int` |

So the marker the script checks is overwritten at seed time with the **app version**, and
the script's `== "2"` test can only pass in a vault whose `.version` happens to be exactly
`"2"`. Conversely, after a successful migration, the next seed writes the app version back
over `"2"` — so the script's own notion of "already migrated" is not stable. Whether the
seed runs on every boot (`overwrite_existing` path in `vault.py`) is **unverified — needs
confirmation**; the collision of *meaning* is verified regardless.

**4. The live path is reconcile, not migrate.** What actually runs is
`thoughtmachine/vault_repair.py` (`__version__ = "0.2.0"`, `:62`): manifest-driven
`classify()` `:161`, `locate()` `:197`, `run_inspection()` `:1647`, `run_repair()` `:1552`,
surfaced as `GET /api/vault/repair/status` and an explicit apply endpoint in
`web_ui/backend/vault_repair_routes.py:45-60`. And the seeding rule states the philosophy
out loud — `thoughtmachine/vault.py:202`: *"Content-based, not version-based: a vault copy
whose content already equals ..."*. Repair can make a vault **match the current manifest**;
it cannot **transform** a document whose meaning changed between versions.

## Blast radius

- A layout change (new compartment, moved key, changed permission vocabulary) has no
  upgrade route: the options are hand-editing or a script nobody invokes.
- Repair will happily "fix" a drifted file into the current shape without ever having
  understood the old one, which is the difference between migration and overwrite.
- Every future schema decision inherits this: there is no place to write down "step 3
  rewrites X into Y", so schema changes get made as code that assumes they already
  happened.

## What the engine would have to be (scope, not design)

1. **One marker with one meaning.** A layout/schema ordinal, written by the migrator, not
   by the app-version seed. (Prerequisite: settle the three meanings above.)
2. **Ordered steps**, each with a precondition and an idempotent body, applied to reach the
   ordinal — not content-hash reconcile.
3. **Backup + dry-run + report** (the pieces exist: the tar backup at
   `scripts/migrate_vault.py:48-51`, `run_inspection`'s report shape).
4. **A caller**: install/upgrade path, and a refusal when the vault is newer than the app.
5. **Partial-failure story**: resume, or refuse-with-detail. `migrate_vault.py:36-45` today
   refuses and tells the user to restore from backup by hand.

## What is not covered

- Per-document evolution (permissions grammar, workspace records, provider config) is not
  surveyed here; only the marker plumbing and the missing entry point are established.
- The GUI repair surface (`components/VaultHealthPanel.jsx`, `VaultHealthBanner.jsx`) was
  not read, so whether it could host a migration report is **unverified — needs confirmation**.

## Dedupe

- `ticket-preflight-vault-repair-suggest-apply.md` — the inverse problem (repair suggested
  for things repair can never touch). Same family, different claim; both stay.
- `ticket-hygiene-manifest-revision.md` — manifest revision handling, adjacent plumbing.
