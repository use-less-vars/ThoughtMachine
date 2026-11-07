# hygiene/vault-writer-audit

Operator: *"I am not sure who is writing to vault (or to codebase) and maybe silently pollutes the disk. We had this problem in the past, stuff silently grew 1kB at a time."*

**Read wave:** enumerate every write to `~/.thoughtmachine/`. Grep patterns:
- `open(..., "w")` / `open(..., "a")` with paths under vault
- `Path.home() / ".thoughtmachine"` / `_log_root()` / `vault_root`
- `write_text`, `write_bytes`, `json.dump`

**Output:** list of writers × file paths × write frequency. Flag:
- any writer that doesn't rotate or cap
- any writer that fires on every turn (would grow silently)
- any writer outside expected lifecycle

**Then:** decide retention policy per file class. Long-lived evidence (audit, lifecycle) gets retention; per-turn scratch gets rotation.

Not urgent. File for post-Credential cleanup wave.
