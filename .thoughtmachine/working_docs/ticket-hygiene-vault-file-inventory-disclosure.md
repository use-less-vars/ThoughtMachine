# Ticket: hygiene/vault-file-inventory-disclosure

**Filed:** Gen 47, 2026-10-21
**Verified at:** not recorded in the relay (pen-test ran in workspace lively-bear-27)
**Source:** gen-47 pen-test run — finding F22 relayed by the operator.
**Severity:** MEDIUM — disclosure scope, no cross-tenant leak.
**Status:** filed, not dispatched.
**Report cross-ref:** Findings F17–F22 were relayed by the operator from the gen-47 pen-test run; the report containing appendices A and B is NOT on this filesystem, so no line numbers are cited and none may be invented.

## Finding

`vault_status` returns the full file inventory for the caller's **own** workspace. Correctly scoped, so not a cross-tenant leak — but it is a complete map of the vault's file layout handed to an agent, and nothing in the agent's task requires an inventory rather than a count.

## Fix shape (not written)

Return counts, or only the files the schema declares, and leave the full inventory to the operator panel.

## Bearing

Distinct from `ticket-bug-checksystem-vault-status-issues-unscoped.md`: that one is a **scoping bug** (foreign IDs in an unscoped field), this one is a **disclosure-scope decision** (own data, more of it than the task needs). They ship together naturally — one pass over the same payload, one fixing `issues[]`, one reducing `files` to what an agent can act on. Shipping only the first leaves the payload correct-by-scope and still over-disclosing.
