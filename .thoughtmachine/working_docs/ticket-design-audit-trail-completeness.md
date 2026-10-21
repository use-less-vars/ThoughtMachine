# design/audit-trail-completeness

Operator question 2026-10-19: *"do we have the relevant logging the machine actually needs, like 'host bash commands are logged' etc?"*

**What we have today (from spare agent's audit + prior reads):**
- `event_log.jsonl` — event bus → JSONL audit trail
- `session.log` + `container.log` — lifecycle events (incl. `CONTAINER_DESTROY`, 53 entries observed 2026-10-19)
- `host_bash_audit.jsonl` — host bash tool invocations
- `config_audit.jsonl` — permission/config changes
- `agent_<session>.jsonl` — per-session code-level logging
- `redaction.py` — secret scrubbing before disk (security control)

**The question:** is every privileged operation auditable? Categories to verify:
- `host_bash` — is every invocation logged with actor + command + exit?
- container destroy — yes (verified 2026-10-19)
- credential read/use (future) — must be, per `design_credential_resource.md`
- workspace permission change — probably via `config_audit.jsonl`
- session grants change — ?

**Not urgent.** File for post-Credential. Read wave: grep every privileged op, confirm it writes an audit line, name gaps.

**Adjacent (Stage 2 from spare agent's logging audit, 2026-10-19):** the GUI Logging button is a dead control (sets backend console verbosity, no UI output surface, zero tests). Stage 1 (remove button + panel + route + 5 config functions) is safe cleanup. Stage 2 (per-session JSONL logs) is a separate decision.
