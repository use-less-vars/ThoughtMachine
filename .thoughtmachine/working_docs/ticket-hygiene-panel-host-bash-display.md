# Ticket (hygiene): panel host_bash display may echo declared, not enforced

**Frame / Tag:** hygiene
**Status:** open (FILE ONLY -- no fix in wave 9c)
**Scope:** web UI workspace/session panel (non-agent)

## Summary

Wave 9c fixed the agent-facing `CheckSystem query='workspace_info'` so it reports
the GATE-RESOLVED `host_bash` value (via `_query_permissions().effective_permissions.host_bash`,
fail-closed to `banned`).

But web panels do NOT read `workspace_info`. `CheckSystem` is an agent-only tool.
The panels obtain `host_bash` from the session payload built in
`web_ui/backend/routes/workspace_routes.py:1010` plus the effective-permissions endpoint.

If that path echoes the CONFIG-DECLARED permission rather than the gate-resolved
value, the same "displayed != enforced" bug is present for the human-facing panel:
a session whose config allows host_bash but whose gate bans it would display `allow`.

## Fix shape

Same shape as the CheckSystem fix -- surface the gate-resolved value
(`effective_permissions.host_bash`), fail-closed to `banned`. One file
(`workspace_routes.py`) plus a test.

## Verification needed FIRST

Confirm whether `workspace_routes.py:1010` already sources the gate-resolved value.
If it does, close this ticket as invalid (no bug).

## Evidence

Raised during wave-9c independent verification (2026-10-16). See
`.thoughtmachine/working_docs/wave9c-spec.md` and the wave-9c commit.
