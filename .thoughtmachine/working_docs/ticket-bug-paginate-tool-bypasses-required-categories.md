# Ticket: bug/paginate-tool-bypasses-required-categories

**Filed:** 2026-10-23 (post-#179 red-team re-run, client-claim pass).
**Verified at:** dev @ 5abd2e47. Every claim below is a first-hand read of dev, not inherited.
**Severity:** **CRITICAL** — the permission model is advisory for any tool reachable by class name.
**Status:** filed, not dispatched. No fix on dev. No existing ticket owns this (dedupe below).

## One sentence

`PaginateTool` declares `required_categories = ["filesystem:read"]` and then instantiates **any** registered tool class by agent-supplied name and calls `.execute()` — so the security gate evaluates `filesystem:read` instead of the target's own categories, and `filesystem=read` is the default grant.

## Defect 1 — the target's `required_categories` is never evaluated

| Step | Evidence |
|---|---|
| Gate checks only the TOP-LEVEL tool's categories | `agent/core/tool_executor.py:419` `required_categories = tool_class.get_required_categories(arguments)` → `:468-469` `check_required_categories(...)` |
| PaginateTool's declaration is `filesystem:read` — the only `required_categories` in the file | `tools/paginate_tool.py:12` |
| Target resolved from **all** registered classes by agent string, no allowlist | `tools/paginate_tool.py:98` `from tools import TOOL_CLASSES`; `:102-105` `for cls in TOOL_CLASSES: if cls.__name__ == self.target_tool_name` |
| Target instantiated and run, no re-gate, no category check | `tools/paginate_tool.py:115-116` `tool_instance = tool_class(**params)` / `return tool_instance.execute()` |
| PaginateTool is agent-callable | `session/tool_presets.py:66` (`_ALL_TOOLS`), `:101` (`AGENT_TOOLS`) |

Consequence, using `ContainerRemoveTool` as the worked example: it inherits `required_categories = ["container:true"]` (`tools/container_control.py:122`, class at `:921`, no override). Under the **default** profile `container=False` a direct call is correctly DENIED at the gate. Through paginate, `container:true` is never evaluated.

The reachable set is not just containers. Anything in `TOOL_CLASSES` is in scope: `host_bash` (`host_bash=banned` by default), `git_write`, `ContainerExecTool` (`container:true` + `filesystem:write`), `DockerCodeRunner`, the file-writing tools. One gate-free path defeats the whole category model.

## Defect 2 — `workspace_id` escapes the context-allowlist

`_CONTEXT_FIELDS` (`tools/paginate_tool.py:55-67`) names the fields that are injected authoritatively and that a caller may **not** forge: `workspace_path, token_limit, is_docker, container_workspace_path, session_permissions, effective_permissions, agent_config, session_id, is_worker_context, worker_name`.

`workspace_id` is **not** in that list. `_build_params` drops caller keys only when they collide with `_CONTEXT_FIELDS` (`:80-82`), so a caller-supplied `workspace_id` inside `tool_params` is forwarded verbatim to the target class.

Runtime observation (real classes, real `_build_params`, nothing written to the repo):
```
PARAMS_PASSED_TO_CONTAINERREMOVETOOL: {'container_id_or_name': 'x', 'workspace_id': 'victim-ws', ...}
INSTANCE.workspace_id = 'victim-ws'
```
Downstream effect: `ContainerRemoveTool.execute` passes it to `_make_manager(self.workspace_id)` (`tools/container_control.py:972`), which accepts it verbatim (`:145` `resolved_workspace_id = workspace_id`; the registry fallback at `:146-151` runs only when falsy). `manager.workspace_id` then selects `<vault_root>/workspaces/<id>/` for config reads and note sidecars (`infra/container_manager.py:838`, `:956`), drives record reaping (`_reap_bound_record`), and is stamped into the emitted removal event (audit mis-attribution). The workspace **path** stays safe (`_resolve_registry_workspace()`), so this is an identity/attribution defect, not a filesystem-containment one.

## Reachable from a worker, and it defeats the worker footprint

- `filesystem` defaults to `'read'` on the pydantic model (`thoughtmachine/security.py:143-147`) and in the dict fallback (`agent/config/defaults.py:262-270`, `:265`); `container` defaults to `False` in both.
- `PaginateTool` is **not** in `_WORKER_BLOCKLIST` (`tools/workspace/worker_thread.py:634-638` = {Worker, CheckSystem, KnowledgeBaseTool}) and is in `SIMPLIFIED_TOOL_CLASSES` (`tools/__init__.py:225-226`), so it is in the default worker tool set (`worker_thread.py:1728`).
- The spawn-time category validation (`worker_thread.py:4290-4317`) checks `cls.get_required_categories({})` of the **named** tool (`:4296`) — i.e. PaginateTool's own `filesystem:read`, never the wrapped tool's.

⇒ A session with `container=False` and `host_bash=banned` — the stock profile — can execute container and host-bash tools. The only residual limit is the parent's `enabled_tools` intersection (`worker_thread.py:1731`); no permission setting removes the path.

## Not affected (checked, so this ticket is not over-claimed)

- **Direct dispatch** of `ContainerRemoveTool` is **not** exploitable via a forged `workspace_id`: `agent/core/tool_executor.py:561` overwrites `tool_args['workspace_id'] = ws_id` from the session/registry. The paginate path is the only way an agent value survives.
- `workspace_path` forgery through `tool_params` is already closed by `_CONTEXT_FIELDS` + the authoritative injection loop (`tools/paginate_tool.py:83-90`).

## Prior art — this is a partial fix that stopped at containment

`8aa17e0d "fix(security): contain WorkingDocument doc_id and stop PaginateTool dropping tool context"` (PR #175, ancestor of dev) added `_CONTEXT_FIELDS` **including** `workspace_path` and **excluding** `workspace_id`, and left the category question untouched. The lesson to record: the context list was treated as a containment list, so identity fields that are not paths fell through it, and "which categories does the target need" was never asked.

## Test coverage today

`tests/tools/test_paginate_tool_context.py` is the only file mentioning PaginateTool (web_ui: none). It asserts context propagation (`:116-127`) and contains `required_categories` **zero** times. There is no test asserting a `filesystem:read`-only caller cannot paginate a `container:true` tool. Any fix must add one; nothing needs updating.

## Fix direction (for the implementer, not prescriptive)

1. **Re-gate the target.** Before `tool_class(**params)`, resolve the target's `get_required_categories(params)` and run the same `check_required_categories` the executor uses, against the caller's effective permissions — which PaginateTool already receives (`_CONTEXT_FIELDS` includes `effective_permissions`). Fail closed when the target cannot be classified.
2. **Restrict the target set.** An explicit paginatable-tool allowlist is the cheaper, narrower option and may be the right first move: pagination is only meaningful for list-returning tools.
3. **Add `workspace_id` to `_CONTEXT_FIELDS`** so identity is injected authoritatively like `workspace_path` — a one-line change that closes Defect 2 on its own.
4. Consider whether a meta-tool should hold any non-read category at all; the deeper issue is that a dispatcher that can name any class is a second, ungated entry point to the whole tool surface.

## Dedupe (checked, honest)

- No `ticket-*.md` matches `paginate|meta.?tool|gate.?bypass|required_categor|permission.?bypass|privilege` by name.
- No `ticket-*.md` contains `PaginateTool`.
- `required_categories` appears in three tickets — `ticket-hygiene-prompt-asserted-guards-audit.md`, `ticket-bug-host-bash-double-prompt-on-ask.md`, `ticket-bug-worker-spawn-cap-fourth-gate.md` — all disjoint (host_bash prompt double-approval, worker spawn-cap category checking, prompt-claim auditing).
- **Unclaimed. This is new.**

## Relation to the isolation matrix

This is orthogonal to `design/workspace-isolation-matrix` (which is about HTTP/WS route scoping). That matrix assumes the permission gate is the enforcement boundary *behind* the routes; this ticket shows the gate can be stepped around from inside a session, so route-level scoping fixes do not compose with it. Sequence matters: fix this before relying on category-based route gating.

## Filed

Filed 2026-10-23 by the main agent. Base: dev @ 5abd2e47. Evidence gathered first-hand (file reads + one runtime instantiation probe); no end-to-end pytest repro, stated plainly rather than implied.

## Residuals — added post-#182 (verified at dev @ 9ef9cfd0)

The body above is unchanged: it was read first-hand at `5abd2e47` and its claims still hold at `9ef9cfd0` except where noted here. PR #182 (`0c125856`) closed Defect 2 by *refusing to forward* `workspace_id` rather than by *injecting* it, which leaves two residuals.

### Residual A — `workspace_id` is dropped, not injected; the wrapped tool sees `None`

`tools/paginate_tool.py:53` adds a blocklist:

```
48:     # Fields the executor owns and that a paginated call can therefore never
49:     # legitimately supply.  ``workspace_id`` is overwritten by the executor on
50:     # the direct path (agent/core/tool_executor.py) *after* the model is
51:     # built, so a caller may never provide it; a paginated call cannot stand
52:     # in for the executor, so the field is dropped instead of forwarded.
53:     _NEVER_FORWARD: ClassVar[tuple] = ("workspace_id",)
```

`_build_params` strips it at `:117-122` (`dropped = self._CONTEXT_FIELDS + self._NEVER_FORWARD`). `workspace_id` remains **absent from `_CONTEXT_FIELDS`** (`:87-98`), and the authoritative-injection loop iterates `_CONTEXT_FIELDS` only (`:123-128`). Three independent paths therefore guarantee the wrapped tool is constructed without the field, so **the pydantic default applies: `None`, not `""`.** (The brief that prompted this residual said `""`; the code says `None`. Same falsiness, different evidence — and `None` is the honest quote.)

The consequence is **per-tool, not uniform**, which is the part worth keeping:

| Wrapped tool | Behaviour on the paginated path | Evidence |
|---|---|---|
| `CiReadTool` | **fails closed, hard** — `workspace_allows_host_resources(None)` → `False` → `ValueError`; a paginated CI read loses remote-origin provenance outright | `tools/ci_read_tool.py:245-258`; `tools/host_resource_policy.py:72-73` (`if not workspace_id: return False`) |
| `ContainerRemoveTool` | OK — derives from `workspace_path`, which paginate *does* forward | `tools/container_control.py:974` → `:147-153` |
| `GitReadTool` | mostly self-heals via the registry — **but fabricates `"default"`** for `ResourceContainerManager` when empty, contradicting its own comment *"Never fabricate a 'default' workspace id"* | `tools/git_info_tool.py:1236-1243`, `:1262-1270` |
| `MCPServerConnect` | **silently unattributed** — no truthiness guard; the empty id flows into `SandboxedExecution` | `tools/mcp_server_connect.py:65-68`, `:93-97` |
| `GitWriteTool` | fails closed: `if not workspace_id: return (None, None)` | `tools/git_write_tool.py:731-735` |
| `Glob` / `DirectoryTree` / `FileSearch` / `FileEditor` / `FilePreview` / `FileMover` / `ReadFile` / `ToolBase` | no `workspace_id` field at all — unaffected either way | six files searched, zero matches |

So fix direction item 3 is still the right fix, and it should be read as an *injection* fix, not a forwarding fix: dropping the field is a denial, and at least one of those denials (`ci_read`) is a silent loss of provenance that nobody would think to look for. `_NEVER_FORWARD` should shrink to empty once `workspace_id` is treated the way `workspace_path` already is.

Two things found while checking the above. Neither is this ticket; both are recorded so they are not lost:

- **Doc defect.** The comment at `:49-52` says the executor overwrites `workspace_id` "after the model is built". It does the opposite: `agent/core/tool_executor.py:561` sets `tool_args['workspace_id'] = ws_id` **before** `tool_class(**tool_args)` at `:657`, and no post-construction overwrite exists anywhere. The constant behaves correctly; its stated rationale is wrong. A reader who checks the claim will conclude the blocklist is unnecessary.
- **`MCPServerConnect` treats an absent `workspace_id` as "no scope"** (`:93-97`) — exactly the class of defect this residual was asked to surface, and the same shape as `bug/session-workspace-attribution-unresolved`: absence rendered as silence rather than refusal.

### Residual B — the re-gate borrows a ceiling it never computes, and `ci` is missing from the map

1. **`check_required_categories` does not apply a workspace ceiling.** `apply_workspace_ceiling` has exactly one non-test call site in the tree: `security/security_gate.py:435`, `return apply_workspace_ceiling(ceiling_map, session_grants)` inside `def cap(...)` (`:416-435`). That funnel is reached only from `get_effective_permissions` (`:1030-1032`), which the executor calls at `agent/core/tool_executor.py:449-455` and injects at `:560`. `check_required_categories` (`:1483+`) merely reads the `effective` dict it is handed (`:1558+`) and mentions a ceiling only when formatting denial text (`:1593-1601`). PaginateTool's re-gate (`tools/paginate_tool.py:198-216`) is therefore capped **only because the dict it validates was capped upstream**. There is no second line of defence: hand it an un-capped `effective` and the re-gate goes silently un-capped too. That is Defect 1 with a seatbelt, not a fix.
2. **`ci` is absent from `_WORKSPACE_RESOURCE_MAP`.** The map (`security/security_gate.py:201-210`) carries `filesystem, docker, container, host_bash, git, network, mcp, credential` — no `ci` — while `_WORKSPACE_CEILING_KEYS` is derived from `RESOURCE_CATALOG`, which does contain `ci` (`security/resource_catalog.py:123`; `WORKSPACE_CEILING_VOCAB = {"ci": ("banned", "read")}` at `:200`). A `ci:` ceiling thus passes the known-key check at `:289` and dies at `:297-299`: `key = _WORKSPACE_RESOURCE_MAP.get("ci")` → `None` → `continue`. It does not even reach the fail-open warning at `:292-295`, because that branch is for resources absent from the catalog. A workspace `ci: banned` ceiling is silently inert; the only control actually gating `CiReadTool` is the separate `allow_host_resources` flag (`tools/host_resource_policy.py`).

**Consequence for this ticket:** the paginate path cannot diverge from a `ci` fix. The drop happens *inside* `apply_workspace_ceiling`, upstream of both the executor and the re-gate, and both consume the same already-capped dict — so adding `"ci": "ci"` to the map corrects direct and paginated calls in one edit, with no paginate-side change and no divergence to document. The inverse risk is the one to note: because the re-gate computes nothing, any future change to the ceiling funnel reaches the paginate path by data flow alone, unobserved and untested.

**Premise correction on the `ci` defect.** It is **not filed.** The only recorded ruling is `"Known-harmless: security_gate._WORKSPACE_CEILING_KEYS auto-gains ci while _WORKSPACE_RESOURCE_MAP has none -> ceiling no-ops for ci; record, do not fix."` — `.thoughtmachine/working_docs/a5ef12e56abd.json:7` (`brief_turn2`, 2026-10-14, branch `feat/agent-ci-monitor-read-only`). Zero `ticket-*.md` files mention `_WORKSPACE_RESOURCE_MAP` or the `ci` grain; the nearest neighbours by shape are `bug/capabilities-undeclared-fields-silently-inert` (shipped, different subject) and this ticket. The "do not fix" ruling is defensible only while nothing can set the key — the frontend vocabulary has no `ci` entry, so a workspace map carrying `ci:` must be hand-written to disk. If that reachability argument is still accepted this entry is documentation; if not, it wants its own ticket with the `credential` precedent copied whole (`security_gate.py:209` plus `tests/test_credential_resource_grain.py:277-292`, `test_credential_workspace_ceiling_is_not_inert`). Recorded here so that `test_ci_workspace_ceiling_is_not_inert` is not written for the first time by someone who assumed the ceiling already worked.
