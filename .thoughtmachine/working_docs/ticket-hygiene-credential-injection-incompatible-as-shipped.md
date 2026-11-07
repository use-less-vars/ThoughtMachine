# Ticket: hygiene/credential-injection-incompatible-as-shipped

**Filed:** 2026-10-24, from the W4 proposal-gate finding
**Category:** hygiene
**Severity:** HIGH — a shipped feature cannot be used
**Status:** filed, the record stands until the design pass closes it

## Summary

W3 shipped two features that cannot be used together. This ticket exists
so no status, handoff, or PR body claims otherwise until the collision
is resolved.

**Feature 1:** `host_run`'s `credential_inject` parameter — the deliberate
bypass of the `CREDENTIAL_SHAPED` strip. Escape hatch for callers that
must deliver credentials to a host child.

**Feature 2:** `build_git_auth_env` — builder that produces the git
credential env dict. Emits `GIT_ASKPASS`, `GIT_CONFIG_COUNT`,
`GIT_CONFIG_VALUE_0`, `GIT_TERMINAL_PROMPT`, `TM_GIT_ASKPASS_SECRET`,
`GIT_CONFIG_KEY_0`.

**The incompatibility:** two of the builder's keys (`TM_GIT_ASKPASS_SECRET`,
`GIT_CONFIG_KEY_0`) are `REFUSED` by the strip. A caller using the builder
cannot pass its output to a `credential_inject=False` `host_run` call.

**The only escape:** `credential_inject=True`, which also skips the F-A
inherited-env strip. The child inherits the full parent env — the hole
the strip exists to prevent.

## Why a ticket, not a bug

Not W3's failure. W3 was briefed to deliver both features. The brief did
not name the interaction; this ticket is the interaction.

Both features are correct in isolation. They don't compose. That's a state,
and states go in the ticket ledger.

## Where it bites

Any status saying "credential injection works" today is a lie about the
git path. The honest status (per Main's ruling, verbatim in the W4 PR):

> "Credential injection plumbed for host_run callers; git path not
> migrated; regex collision blocks the migration; filed as
> hygiene/credential-regex-collision-git-askpass. W3 and the F-A strip
> are incompatible as shipped for git —
> hygiene/credential-injection-incompatible-as-shipped."

## What closes this ticket

The regex collision is resolved, AND the git path either:

(a) migrates to `host_run` and uses `credential_inject=False` with the
    askpass keys passing the strip, or
(b) stays on `SandboxedExecution` and `build_git_auth_env` is understood
    as a `SandboxedExecution`-scope utility, not a `host_run` companion.

Either path closes it. `design/host-port-git-authorization` decides which.

## Related

- `hygiene/credential-regex-collision-git-askpass` — the bug
- `design/host-port-git-authorization` — the decision that closes it

## Filed

2026-10-24, Gen 49, ruled by Main Gen 39.
