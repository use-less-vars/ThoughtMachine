# vault_repair (Phase 1: read-only vault inspection)

`thoughtmachine.vault_repair` is a **read-only vault integrity scanner** — the
first phase of the vault-repair program. It inspects the vault at
`--vault-root` against the real schema manifest
(`agent/config/schema_manifest.json`), compares seeded files against the
bundled resources, and checks permission-shaped JSON for legacy/unknown keys.

- **Phase-1 scope is read-only**: the tool never writes, applies, restores, or
  quarantines anything (`dry_run` is always true in reports). No vault file is
  modified.
- Full design: `working_docs/vault_repair_design.md`.
- Roadmap: later phases add `apply` (safe repairs), `restore` (seeded files)
  and `quarantine` of unknown files.

Usage:

```
python3 -m thoughtmachine.vault_repair --vault-root PATH [--report-json out.json]
```

Exit codes: `0` healthy, `1` findings reported, `2` usage error, `3` vault
root missing/invalid.
