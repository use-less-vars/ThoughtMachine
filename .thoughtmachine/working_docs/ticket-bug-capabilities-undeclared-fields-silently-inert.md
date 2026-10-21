# Ticket: bug/capabilities-undeclared-fields-silently-inert

**Filed:** Gen 44, 2026-10-15, from field agent probe.
**Severity:** HIGH — trust-layer bug; UI lies about hardening. Main ruled: sequence after T1, not hygiene.
**Status:** SHIPPED — see PR #119 (Wave HIGH).

## Finding

Several workspaces' `capabilities.json` use **8 fields the schema manifest does
not declare**:

    allowed_tools, blocked_tools, allowed_providers, allowed_file_extensions,
    allow_network, filesystem_write, allowed_workspace_dirs, extra

The vault_status report itself says these fields *"will not be validated or
repaired."* Consequence: any hardening expressed through `allowed_tools` /
`blocked_tools` / `allow_network` etc. is **silently inert**. The workspace
appears hardened; enforcement does nothing.

## Related observations in the same dump

- `allow_host_resources` missing from 14 workspace configs (fail-closed
  default; probably fine, worth confirming)
- `agent_config.json` carries undeclared `git_allow_worktree_commits`
- `providers.json` drifted from the seeded default
- unknown file `config_audit.jsonl`

## Fix shape

Either (a) the schema declares these fields and they are enforced, or
(b) the schema removes them and any workspace carrying them is a
failure-notice (not silently ignored). Currently: (c) declared-but-not
enforced — the worst of the three.

## MUST NOT TOUCH
Same list as the container tickets.
