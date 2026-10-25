# Ticket: bug/domain-allowlist-inert-control

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit).
**Severity:** MEDIUM
**Status:** open; verified by read at `24012a89` — census and `network_mode` derivation read in source; not exercised at runtime — `UNRUN: no container started, no egress observed`.

## Finding

`domain_allowlist.json` reads like a network egress control. It is not one. Nothing consumes the file to restrict anything.

Census: `grep -rn "domain_allowlist" --include=*.py --include=*.json --include=*.md .` returns roughly 120 hits. Every one falls into five classes, and none is an enforcement consumer:

- **Declaration / schema.** `agent/config/schema_manifest.json:662,678,1020-1021` describes `workspaces/*/domain_allowlist.json` as "Per-workspace allowed network domains (JSON array of domain strings…)".
- **Seeding.** `thoughtmachine/workspace_capabilities.py:365-369` writes `[]` if the file is absent; `:312,525,535` list it among created files.
- **CRUD.** `web_ui/backend/workspace_routes.py:376-386` (`get_domain_allowlist`) and `:391-403` (`put_domain_allowlist`) read and write the file and return `{"domains": [...]}`. Storage only.
- **Read-only surfacing.** `tools/workspace/check_system.py:552-565,620` loads the key into a workspace-info dict.
- **Tests and report prose.** Fixtures assert the file seeds as `[]` and round-trips through the API (`tests/workspace/test_bootstrap.py:83-86,117-136`, `tests/workspace/test_api.py:73-102`, `tests/test_workspace_info.py:30-47`, `tests/test_vault_schema_runtime_truth.py:42`).

**Zero enforcement consumers were found.** The string does not appear in `infra/` or `security/` at all, which the container subsystem audit already records at `docs/container_subsystem_audit.md:203`: "`domain_allowlist` appears nowhere in `infra/` (0/7) or `security/` (0/5) … but **never enforced** in any container-run or egress path."

Where egress is actually decided: `network_mode` is resolved in `security/security_gate.py`, `resolve_container_config` (~:1188, signature `(permissions, capabilities, lifecycle_class, *, …)`), from the workspace's **permissions plus capabilities**. No parameter carries a domain list.

The finding is therefore a deceptive control, not a missing one. An operator reading the file — or the endpoint, or the UI that fronts it — believes egress is limited to a declared domain set. The container is configured from permissions and capabilities, and the file changes nothing. The gap between what the file looks like and what it does is itself the risk, because it suppresses the question.

`reports/2026-10-10_02-52-52_Two_Weeks_of_Making_the_Invisible_Visible___Silent.md:67` already states: "`domain_allowlist` is **inert metadata** — no create-path consumer; an empty allowlist neither forces `none`, nor blocks DNS, nor forms a layer. It looks like a control and is not one." Its summary table repeats the disposition at `:310`: "It reads like a control. Either wire it or delete it."

**Adjacent to F5, not one layer below it.** F5 is about the ceiling that *drives* `network_mode`. This file sits nowhere in that chain: not a weaker form of the ceiling, not a downstream filter. It is a parallel artifact resembling a control. The file has no position in the chain at all.

## Bearing

The deceptive surface is broad for a file that does nothing: a GET/PUT API, a frontend editor, a schema entry, a seeded default, a `check_system` field, audit prose beside real controls. Each is evidence an operator would reasonably read as "this is wired".

## Blast radius

Belief, not behaviour. No runtime path consults the file, so no container's configuration changes. What changes is what an operator thinks the configuration is: egress assumed bounded by an empty-by-default list enforced by nothing.

## What this is not

- Not a claim of unrestricted egress. Egress policy is whatever permissions plus capabilities produce; whether that is adequate is a separate question and it is F5's subject, not this ticket's.
- Not an exploit. Writing the file grants nothing and blocks nothing.
- Not a missing validation a client can bypass — there is nothing to bypass.
- `UNRUN`. No container was started and no egress was observed. The inertness conclusion is static: zero consumers in source.

## Fix shape

Either wire it into the egress decision so it is load-bearing, or delete or rename it so it stops reading like a control, with a comment pointing at where `network_mode` is genuinely resolved. Leaving it as-is is not acceptable.
