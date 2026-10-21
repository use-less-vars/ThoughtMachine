# Ticket: hygiene/checksystem-allowlist-test-literals-drift

**Filed:** Gen 44, 2026-10-16, Wave 10a hygiene.
**Severity:** Low — hygiene / drift-trap; no runtime defect.
**Status:** filed, not dispatched.

## Defect

The live CheckSystem allowlist `resources/checksystem_allowlist.json` now holds 15
permitted queries, canonical digest
`988b35402843767eef634a7d1b6fb2057b40dd9ada72190167c689629ba020d6`
(digest = sha256 of `"\n".join(sorted(allowlist))`, per
`thoughtmachine/vault.py::verify_allowlist_integrity`). Four hardcoded **12-item**
mirrors still exist that do NOT read that resource file, so they drift silently.
None of them contains the newer entries `runtime_state`, `vault_status`, or
`version`:

- **(a)** `tests/security/test_checksystem_allowlist.py:22-23` — `ALL_ALLOWED_QUERIES`.
  Its comment falsely claims it is "the full set of allowed queries from
  resources/checksystem_allowlist.json".
- **(b)** `tests/test_check_system_vault_status.py` — `ALLOWLIST_12`, plus a
  self-consistent 12-item digest `697c3263...`.
- **(c)** `tests/tools/test_workspace_tools.py:40` — `FULL_ALLOWLIST`, comment
  "mirrors the vault allowlist".
- **(d)** `scripts/migrate_vault.py:193-201` — the legacy V2 migration writer emits
  its own 12-item list. Its sidecar `.sha256` hashes the file bytes — a DIFFERENT
  scheme from the vault verifier. Pre-existing, possibly intentional.

## Why it matters

The literals are injected so the tests pass, but a comment asserting a false
equivalence is a trap for the next reader: the test reads as a guarantee that the
allowlist is fully covered when it is not. Each future allowlist addition silently
diverges the mirrors further, and the drift is invisible.

## Recommendation

Derive each list from `resources/checksystem_allowlist.json` directly, OR add a
test asserting each literal equals the live resource set. Decide per-file whether
`scripts/migrate_vault.py`'s copy is intentional (its byte-hash scheme differs) and
document the decision either way.

## Already fixed — do not re-investigate

`docs/xray/backend_runtime.md:86` held a stale 14-item list and the old digest
`201f3e98...`. That file is GITIGNORED (`docs/*`); the on-disk copy is already
corrected to 15 items and the new digest, so it can never appear in `git status`.

**Not this ticket, but adjacent:** the *deployed* vault copy `~/.thoughtmachine/system/checksystem_allowlist.json` carries 14 entries versus the shipped 15 (no `version`) — a seed-sync gap, not a test-literal gap; tracked in `ticket-bug-checksystem-containers-denied-by-allowlist.md` (Gen 47 amendment, commit `01d1e6ae`).
