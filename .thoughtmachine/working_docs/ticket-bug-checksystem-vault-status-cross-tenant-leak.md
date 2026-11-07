# Ticket: bug/checksystem-vault-status-cross-tenant-leak

**Filed:** Gen 44, 2026-10-15, from field agent probe (workspace lively-bear-27).
**Severity:** Medium — metadata disclosure, not content disclosure.
**Status:** SHIPPED — see PR #119 (Wave HIGH).

## Finding

`CheckSystem query='vault_status'` returns a **complete vault manifest** to
any session, regardless of workspace ownership. Disclosed to a probe from
workspace `lively-bear-27`:

- 20 workspace IDs (including `ws-hardening`, `ws-reuse-isolation-test`)
- ~40 session filenames from OTHER workspaces, with human-chosen titles
  (`FilmGuy`, `Intorf`, `Tetris`, `yf_tickers`, `testy`, `Schuler`,
  `Audit_engineer`, `V2.7_agent`, `V2.8_eng`, `AgentV2.9`, ...)
- per-file drift details of foreign workspaces
- log filenames: `provider_raw.jsonl`, `tool_calls_raw_debug.log`
- prompt filenames: `engineer_system_prompt.txt`, `default_system_prompt.txt`
- existence of `state/session_registry.json`, `state/workspace_registry.json`,
  `state/open_sessions.json`

**Not** file contents. It is inventory + titles + drift. But session titles
are user-authored, and title drift reveals who-was-working-on-what across
workspaces.

## Also check (broader)

Whether `provider_raw.jsonl` / `tool_calls_raw_debug.log` are reachable via
ANY query. If so, that is raw LLM payload exposure — larger class.

## Recommended fix shape

- Scope `vault_status` to the caller's own workspace.
- Redact foreign session titles and log filenames.
- Global inventory stays operator-UI-only.
- If a global diagnostic view is needed by a privileged caller, gate it
  behind an explicit operator grain.

## MUST NOT TOUCH
Same list as the container tickets.
