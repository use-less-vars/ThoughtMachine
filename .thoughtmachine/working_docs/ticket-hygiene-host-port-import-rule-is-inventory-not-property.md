# Ticket: hygiene/host-port-import-rule-is-inventory-not-property

**Filed:** 2026-10-24, from the host-port implementation wave (feat/host-port)
**Category:** hygiene
**Severity:** HIGH — structural
**Status:** filed, not fixed

## Summary

The `tests/test_tool_import_declaration_match.py` guard delivered by the
host-port wave enforces a **pinned inventory** of who may import subprocess,
not the **closed rule** the brief and Main's taxonomy §4.1 asked for.

The intended rule: any file that imports a host-execution primitive must
declare an authorizing grain (`execution_full`, `host_bash`, or `mcp_connect`).
Over-declaration is fine.

The delivered rule: any file NOT on `ALLOWED_EXCEPTIONS` that imports
subprocess fails CI. That is stricter than "declaration must match" AND
weaker than it — it fails an honest new tool that declares `execution_full`
correctly, and it says nothing about tools that DECLARE `execution_full`
but DON'T import subprocess (the "over-declaration is fine" case is not
tested).

## Why this is a real defect, not cosmetic

A future tool that declares `execution:full` and imports `subprocess` to
spawn a host process is a *correct* implementation under Main's taxonomy.
Under the current guard, adding it fails CI until it's manually added to
`ALLOWED_EXCEPTIONS`. That means the guard does not codify the rule; it
codifies "nothing new may be added without a human saying so." That's an
inventory discipline, not a rule.

Conversely, a tool that declares `execution:full` and never imports
subprocess is untested — the docstring says over-declaration is fine, but
there's no test that would go green if over-declaration were present and no
test that would go red if the rule were reversed.

## What the fix looks like

Replace the inventory check with a per-file declaration check:

  for each .py in SCAN_ROOTS:
    tree = ast.parse(source)
    imports = extract_host_primitive_imports(tree)
    if imports:
      declared = extract_declared_grains(tree)  # required_categories ClassVar
      if not declared & AUTHORIZING_FOR(imports.family):
        fail(file, imports, declared)

Plus a self-test on a synthetic file: a tool declaring `execution:full` +
importing subprocess must pass; a tool importing subprocess without any
authorizing grain must fail.

The current `ALLOWED_EXCEPTIONS` list becomes either:
  (a) The set of files that will migrate in a follow-up wave — a *to-do list*
      for the port migration, tracked as `hygiene/host-port-importers-pending-migration`
  (b) Genuinely exempt files where the subprocess import doesn't count as
      host-execution (e.g. `mcp_server_connect.py` imports subprocess only
      to catch `TimeoutExpired`), which the rule's membership test must accept.

## The 7 current exceptions, classified

Per the delivered `ALLOWED_EXCEPTIONS`, with the reason each is on the list
(transcribed from the allowlist comments for verification):

1. `tools/mcp_client.py` — documented deferral (needs `host_run_async`, markers pinned). **Will migrate.**
2. `thoughtmachine/doctor.py` — dev/ops diagnostic, not agent-reachable. **Category (b) if the rule only governs agent-reachable tools. Otherwise pending.**
3. `tools/ci_read_tool.py` — shells to `gh` CLI, own grain `ci:read`. **Category (b) if `ci` is a separate family; pending if `ci:read` isn't in the host-execution family.**
4. `tools/git_info_tool.py` — drives `git` CLI, declares `git:read`. **Same question: is `git:*` in the host-execution family or a separate one?**
5. `tools/git_write_tool.py` — same, `git:write`.
6. `tools/mcp_server_connect.py` — imports subprocess only to catch `TimeoutExpired`. **Legitimate category (b). The scan's *import* detection doesn't distinguish "import for exception matching" from "import for spawning"; the rule does. The inventory approach happens to allow this file, but a rule-based approach must know the difference.**
7. `tools/workspace/worker_execution.py` — `docker exec <id> kill <pid>` fallback, container plumbing. **Category (b) if containers stay on their own path (Main Q1 confirmed). Should be authorized by a container-family grain, or the rule's scope excludes it.**

The rule cannot be a clean per-file declaration check until items 2, 3, 4, 5, 6, 7 are each resolved as either "belongs to a different family" or "must migrate."

## Not fixed here

The wave delivered an honest partial win: the port exists, two sites migrated, one deferred with markers, one CI guard that pins the current set. F-G (from the wave's own report) names the gap correctly. This ticket is the follow-up that converts the pin into a rule.

## Sibling

`hygiene/no-tool-wrapping` — same discipline: the rule is stated in doctrine, the guard must be a rule check, not an inventory. That ticket's guard is parked on the same principle.

## Filed

2026-10-24, Gen 49, from the host-port implementation wave's own F-G disclosure.
