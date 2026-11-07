# Ticket: bug/checksystem-containers-denied-by-allowlist

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** sub-finding of bug/container-turn-start-reap-kills-running
**Status:** filed, not dispatched.

## Defect

CheckSystem query='containers' is denied by the workspace allowlist. The agent cannot inspect container state — the exact diagnostic needed for the container feature. Inv.5 violation: agent cannot inspect the mechanism that refuses it. May fold into the broader Inv.5 work; placement is Infra's call. Surfaced by bug/container-turn-start-reap-kills-running.

## Bearing

Filed alongside the primary ticket. Not to be dispatched until the Wave 9
read-first (turn-start reap) returns — the primary read may reshape these.

## Amendment 2026-10-20 (gen-47 run)

**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F9 (line 368); run evidence §Step 9 (line 273), §Step 10 (line 306).

The split is unguessable by construction, not merely by accident. `CheckSystem query='runtime_state'` reports `"allowlist_count": 14` but never enumerates the members — it publishes the *cardinality* of the policy and not the policy — while `event_log` is allowed and `version` and `containers` are denied (live-verified 2026-10-20 on `124477b7`: `version` → `"Query \"version\" not allowed by system configuration"`, `status: denied`).

Fix shape: the denial should enumerate what IS allowed, and/or `runtime_state` should expose the list rather than its length. Otherwise every future protocol step rediscovers this from scratch by trial, and the operator has no way to answer "is query X permitted?" without running it.

## Amendment Gen 47 (2026-10-21, commit `01d1e6ae`) — `version` and `containers` are now always-allowed

**Shipped.** `version` and `containers` are permitted regardless of the workspace allowlist: `SAFE_QUERIES = frozenset({"version", "containers"})` at `tools/workspace/check_system.py:45`, short-circuited at the top of `_check_path_allowed` (`:274-276`) before the vault allowlist is consulted. Both are read-only facts about the caller's own machine/workspace — its revision, its own container list.

**Why hardcode the bypass rather than edit the allowlist:** the tool schema advertises both queries unconditionally (`tools/workspace/check_system.py:247` for `version`, `:248` for `containers`), so a runtime denial is displayed != enforced. The schema is the contract; the gate now matches it.

**Root cause was vault drift, not the seed.** The shipped `resources/checksystem_allowlist.json` lists **15** queries and *does* include `version`; the deployed `~/.thoughtmachine/system/checksystem_allowlist.json` carries **14** and does not — which is why the live agent was denied on 2026-10-20. Seeded once, never updated: same class as the global-KB `.version` sync gap. The short-circuit makes the denial impossible regardless of which copy is live, but the drift itself is unfixed and will bite the *other* queries.

**Second reachable failure mode, now covered for these two only.** `thoughtmachine/vault.py:411 get_checksystem_allowlist()` returns an **empty list** — not `None` — when the vault file is missing, unreadable, or fails its sha256 integrity check (docstring `:420-421`), and an empty-but-present allowlist previously denied **every** query. Only the safe set is immune now; the general hard-deny edge case remains open.

**The enumeration finding stands.** For every query outside the safe set the split is still unguessable: `runtime_state` publishes `allowlist_count` and not the members, and the denial names nothing. This amendment closes the user-visible pain, not F9's broader point.

**Report-only — a decision, not a patch.** `vault_repair_status` is advertised in the schema (`:254`) and implemented (`:369`) but absent from the shipped 15, so it is denied in every workspace by default: same displayed != enforced class. It is a read-only dry-run but inspects vault-wide state, so whether it belongs in the safe set is an operator call. It has not been added to any code.

**The agent can now query its own commit:** `CheckSystem(query="version")` returns `revision` / `branch` / `dirty` — with the caveat that all three come from a single import-time snapshot in `web_ui.backend.server` (`check_system.py:200-218`), so a failed snapshot yields `"unknown"` / `False` rather than a live git read. The permission is unconditional; the accuracy is snapshot-dependent.
