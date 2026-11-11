<!-- Design: worker capability registry -- PASS 2 OF 2, COMPLETE. This file contains front matter, the
     executive summary and Sections 1-8. Pass 1 wrote Sections 1-4; pass 2 appended Sections 5-8 and removed
     the marker they were staged under. No section is missing; a gap in coverage is a defect, not a scope
     cut. -->

---
title: Worker Capability Registry
subtitle: Narrow, auditable, spawn-frozen tool sets for worker instances
status: DRAFT -- pass 2 of 2 complete (Sections 1-8 of 8)
tier: internal design
audience: engineers who already know this codebase
branch: fix/gen67-worker-runtime-default-process
related:
  - docs/design-c44-worker-state-reattachment.md   # state/reattachment; this doc does NOT re-specify it
  - docs/design-worker-lifecycle.md
  - docs/design-worker-runtime-process.md
  - docs/design-worker-dispatch-fence.md
reads-on: tools/workspace/worker_thread.py, agent/core/tool_executor.py, agent/models/worker_definition.py
---

# Design: Worker Capability Registry

## Executive summary

A worker today gets its tools from a name list (`enabled_tools`) filtered out of one process-global
name->class map (`worker_thread.py:5117-5125`). "Enabled" is therefore a **string filter over a global
registry**, not a capability grant: the registry always contains every tool, and the only thing standing
between a buggy filter and a privileged worker is the correctness of that filter. The most privileged
setting in the system is also the easiest to type -- `tools: []` -- because an empty list is interpreted
as "every simplified tool minus a 3-name blocklist" (`worker_thread.py:3011-3016`).

This document proposes a **capability registry**: a per-spawn registry instance, built from the template's
declared tool subset and resolved to classes at spawn time, so that a tool not in the subset is *not
resolvable at all* rather than *filtered out at runtime*. Two load-bearing properties, both stated and
justified below: the set is **FROZEN AT SPAWN** (Section 4.1), and it is **INJECTED** rather than filtered
(Section 4.2). Everything about the permission system, the tool implementations, and worker state
reattachment stays exactly as it is; those are explicit non-goals (Section 1).

The second half of the problem is a *presentation* problem: the operator surface currently mixes durable
blueprints (templates/definitions) with running instances in one panel, and there is no per-instance
override surface at all (Section 2.4, Section 3). A capability registry only becomes operable once the
two-object model is separated in the UI, which is why Section 3 is in scope.

Notation used throughout: a statement citing `path:line` describes **existing** behaviour that was read
in the source. Anything marked **PROPOSED** does not exist in the tree today.

## 1. Purpose and non-goals

### 1.1 The observed pain

A worker definition carries a `tools` list (`agent/models/worker_definition.py:38-40`), and the shipped
template sets it to the empty list (`resources/worker_templates/default.json`, key `tools` = `[]`).

That empty list is not "no tools". In `_build_agent_config`:

- `worker_tools = self.definition.get("tools", [])` (`worker_thread.py:3009`)
- non-empty -> exactly those names, minus the blocklist (`:3011-3012`)
- empty -> **every** tool from `simplified_tool_classes()` minus the blocklist (`:3014-3016`)
- the result is then intersected with the parent's `enabled_tools` (`:3017-3019`) and written to
  `worker_cfg["enabled_tools"]` (`:3020`)

So the empty list is simultaneously the default, the least explicit, and the **most privileged** setting a
worker can have. Three consequences, all observable in the tree:

1. **No narrow set is expressible without enumerating class names by hand.** Giving a worker three tools
   means typing three class names into JSON with no validation against a declared intent, and no record of
   *why* those three.
2. **Privilege is invisible at the point of decision.** A reviewer reading `tools: []` in a template sees
   nothing; the actual set is whatever `simplified_tool_classes()` returns at the time the worker spawns.
   Adding a tool to the build silently widens every `tools: []` worker in the workspace.
3. **The failure mode of a filter bug is over-grant, not under-grant.** Because the map
   (`_get_tool_registry()`, `worker_thread.py:5121-5125`) always holds every tool, a forgotten filter step
   yields a *working* worker with *more* access, which no test notices.

### 1.2 What a capability registry buys

- A template states its tool set positively, and the resulting set is auditable from the template alone
  (once `tools: []` stops meaning "everything" -- Section 4.2 discusses the migration).
- Injection replaces filtering: the spawn-time registry is built from the subset, so a name outside the
  subset has no class to resolve to. Defence in depth -- a bug in any filter cannot widen the set, because
  the set is not the filter's output.
- The freeze boundary (Section 4.1) gives the property operations actually needs: **a worker cannot gain
  capability mid-run**, while it can still lose it.

### 1.3 Non-goals (explicit)

- **Not worker state reattachment.** Boot-time enumeration, re-attachment catalogues, detached-container
  exemption, queued-query durability: all specified in `docs/design-c44-worker-state-reattachment.md`
  (title `:1`; the registry-shaped gaps are already named there as **G1** "No boot-time enumeration or
  registry rebuild" `:422` and **G8** "Key mismatch between the disk catalogue and the registry" `:515`;
  recommendation `:744`). This document must be read as *additive* to C44, and where it touches instance
  identity (Section 3.3) it defers to C44-G8 rather than inventing a second key scheme.
- **Not a change to the permission system.** Categories, grains, `permission_footprint`, the per-call gate
  (`agent/core/tool_executor.py:612-648`) and its fail-closed semantics are untouched. The capability
  registry constrains *which tools exist for this instance*; the permission system constrains *what a tool
  may do when it exists*. Both remain.
- **Not new tools, and not a tool SDK change.** `iter_tool_classes()` / `simplified_tool_classes()` keep
  their contracts; no tool gains or loses `required_categories`.
- **Not a UI redesign beyond the two-object split.** Section 3 states what the Live Panel lists; it does
  not specify visual design.

## 2. Current truth

### 2.1 Templates

**Where they live and which one wins.** Two directories, one precedence rule: `~/.thoughtmachine/worker_templates/`
is preferred when it exists and is non-empty, otherwise the bundled `resources/worker_templates/`; with
`include_bundled=False` the loader returns `[]` instead of falling back
(`thoughtmachine/workspace_capabilities.py:478-484`). The loader chain is `_load_template_workers`
(`:463-509`, vault dir via `_user_dir()` `:46-53`, sorted `*.json` `:491`, `json.loads` `:495`,
`_validate_worker_dict` `:496`, invalid templates skipped and logged `:497-507`) and
`_validate_worker_dict` (`:32-42`), which requires
`_REQUIRED_WORKER_FIELDS = {"name","description","system_prompt","tools","permission_footprint"}`
(`:29`) plus type checks that `tools` is a list and `permission_footprint` a dict (`:38-41`).
Template dicts are merged into the workspace worker list by name, existing workspace entries winning
(`tools/workspace/worker_thread.py:5492-5515`).

Shipped templates in the tree: exactly one -- `resources/worker_templates/default.json`.

**Schema vs reality: 13 keys declared, 5 keys set.** `WorkerDefinition` declares thirteen fields --
`name` `:29`, `description` `:32`, `system_prompt` `:35`, `tools` `:38`, `permission_footprint` `:41`,
`timeout_seconds` `:48`, `max_retries` `:52`, `max_turns` `:60`, `temperature` `:64`, `max_tokens` `:68`,
`truncation_limit` `:75`, `warning_threshold_tokens` `:82`, `critical_threshold_tokens` `:90`
(`agent/models/worker_definition.py:16-90`). The shipped `default.json` sets only the five required keys
(`name`, `description`, `system_prompt`, `tools`, `permission_footprint`) and leaves the other eight unset,
because validation only requires those five (`workspace_capabilities.py:29`). Any design that assumes
templates populate the full schema is reading the model, not the data.

**Two schema fields are dead.** `max_tokens` (`agent/models/worker_definition.py:68-74`) and
`truncation_limit` (`:75-81`) are declared and never read anywhere in `agent/` or `tools/` -- grep for
`max_tokens|truncation_limit` outside `agent/logging/` and `tools/knowledge_base.py` returns only those two
declarations. This is *deliberate*: `worker_thread.py:3000-3003` documents that they live on the worker
definition schema only and are "intentionally NOT injected into the cfg (Pydantic's `extra='ignore'` would
silently drop them anyway)". The consequence to keep in mind for Section 3: a UI that offers a "max context
tokens" box is writing a value nothing reads.

### 2.2 Instances

**There is no persisted instance record.** `workers.json` is written only at bootstrap
(`thoughtmachine/workspace_capabilities.py:409-414`); no runtime writer exists in `tools/`, `web_ui/` or
`agent/` (all hits are readers: `worker_thread.py:5476`, `tools/check_system.py:773/783/804/1011`). What
exists per instance is a directory of sidecars: `WorkerThread.__init__` creates
`<workspace>/workers/<instance_label>/` (`worker_thread.py:1440-1441`) containing `context.json`
(`_context_path` `:4526-4527`, written by `_save_context` `:4861+`), `status.json` (`_write_status_file`
`:4802-4859`; keys `runtime_status, current_task, last_heartbeat, error, session_id,
current_context_tokens, max_context_tokens, started_at, last_query_at, paused_manually`) and a `generation`
sidecar (`_allocate_generation` `:4529+`).

**No pid, no container identity.** `NOT FOUND` -- no `pid` field on `WorkerThread` and none in the
`status.json` payload (`worker_thread.py:4812-4823`). The only `pid` handling in the file is event-payload
plumbing distinguishing container-exec from subprocess (`:3460-3486`). Containers are not recorded as
instance identity either: worker-owned containers are rediscovered by Docker label at teardown
(`tools/workspace/worker_container.py` helpers imported at `worker_thread.py:206-214`,
`_is_worker_owned_container` `:3620`, `_cleanup_worker_containers` `:3644`).

**Nothing re-attaches.** Live instances vanish with the process; the only on-disk residue is
`workers/<label>/{context.json,status.json,generation}`, and `GET /api/system/health` reconstructs a worker
*list* by scanning those `status.json` files (`web_ui/backend/health_routes.py:26-108`, route `:182-203`) --
read-only filesystem discovery, not reattachment. The whole subject is C44's (Section 1.3).

### 2.3 Tool injection as it actually works today

Four filters, applied in this order. Each is a *subtraction*; nothing is ever added.

1. **Definition `tools`** (`worker_thread.py:3009-3016`). Non-empty: take exactly those names. Empty: take
   every tool from `simplified_tool_classes()`. This is the "empty = everything" inversion from Section 1.1.
2. **`_WORKER_BLOCKLIST`** = `frozenset({"Worker", "CheckSystem", "KnowledgeBaseTool"})`
   (`worker_thread.py:737-741`), applied inside both branches of step 1 (`:3012`, `:3014-3016`) and again at
   the spawn boundary (step 4). Container tools are **deliberately not blocked**, only permission-gated --
   the comment at `:727-736` says so explicitly.
3. **Intersection with the parent's `enabled_tools`** (`:3017-3019`, guarded by
   `if parent_enabled_tools is not None`). A worker can never exceed its parent's tool set.
4. **Enforcement at spawn.** Name -> class resolution walks the list again: blocklisted names are skipped
   with `"<name> (excluded for worker safety)"` (`:6335-6337`); unknown names and permission failures
   accumulate in `missing_tools` (`:6333-6341`, `:6362-6365`). Per-tool permission validation calls
   `cls.get_required_categories({})` (`:6344`) and then `check_required_categories(...)` with
   `spawn_effective = _restrictive_merge(session_perms, worker_perms)` (`:6332`) and
   `worker_perms = definition.get("permission_footprint") or definition.get("worker_permissions", {})`
   (`:6312`); a footprint naming a category the session does not hold fails closed via
   `denied_categories = sorted(set(worker_perms) - set(session_perms))` (`:6319-6329`).

The resolved map is then handed to the thread as `tool_classes` (`:6309`, filled `:6366`, passed
`:6420-6433`, handoff at `:6425`); with an empty `tools` list the dict stays `{}`, `None` is passed, and
`self._tool_classes` becomes `{}` (`:1454`) -- in that case availability comes from `enabled_tools`, not
from the class map. Resolution goes through one global registry: `_resolve_tool_class` (`:5786-5796`) ->
`_get_tool_registry()` (`:5122-5125`) -> `_build_tool_registry()` = `{cls.__name__: cls for cls in
iter_tool_classes()}` (`:5117-5119`), cached in the module-global `_TOOL_REGISTRY` (`:5121`). There is no
per-worker registry, and no class named `ToolRegistry` exists anywhere in `tools/` or `agent/`.

Per call, the gate runs again: `ToolExecutor._execute_single_tool` (`agent/core/tool_executor.py:440`)
calls `check_required_categories` (`:612-648`) with
`permission_footprint=... if self._is_worker_context else None` (`:642-646`). Tools execute in-process --
`tool_class(**tool_args)` then `.execute()` (`:801`, `:810`, `:821`) -- so "which tools exist" is decided
entirely by Python objects reachable from this interpreter, which is what makes injection (Section 4.2)
possible at all.

### 2.4 The panels

Three backend surfaces, distinct and easily confused:

- **Runtime instances**: `_collect_workers_from_fs()` (`web_ui/backend/health_routes.py:26-108`) scans
  `<workspaces>/*/workers/**/status.json`, handling session-scoped
  `workers/<session_id>/<name>/status.json` (`:66-77`) and legacy `workers/<name>/status.json` (`:89-96`),
  served by `GET /api/system/health` (`:182-203`).
- **Templates**: `@router.get("/templates")` -> `get_worker_templates()` (`web_ui/backend/workspace_routes.py:223-224`),
  user dir first, bundled fallback (`:227-244`).
- **Definitions**: `get_workers` (`:589`), with `_worker_instance_key` (`:560`) and `_worker_active_entry`
  (`:719`) joining definitions to live instances.

The UI consumes all three from one component: `WorkerManagementPanel.jsx` fetches
`/api/workspace/${ws}/workers/active` (`:810`, comment at `:749` names it "runtime instances"; `:895` notes
it "carries runtime state only"), `/api/workspace/templates` (`:855`), `/api/workspace/${ws}/config_tiers`
(`:882`), `/api/workspace/${ws}/workers` (`:908`, `:1102`), and the per-worker verbs `stop` `:939`,
`stop_all` `:978`, `pause` `:1006`, `resume` `:1034`, `kill` `:1063`/`:1169`, definition GET/PUT
`:1118`/`:1124`, config apply `:1140`, create `:1225-1229`.

**No per-instance override UI exists today.** `WorkerFormModal` (`:507`) edits the *definition*: its state
seeds from the definition (`:519-522`) and its payload posts `max_tokens`, `warning_threshold_tokens`,
`max_turns`, `temperature` (`:568-571`). The nearest thing to an instance-scoped lever is the W9 config
patch: `workerConfigPatch(definition, configTiers)` (`:195`) keeps only fields the backend's tier map
recognises, `patchNeedsRespawn` (`:206`) flags fields tiered `'requires_respawn'`, and
`workerRespawnPlan` (`:218`, decision `:236`) hot-swaps or forces a respawn. The backend answers with the
runtime's own `config_ack` (`web_ui/backend/workspace_routes.py:1638`, `:1651-1653`: `hot_swap` = applies at
the next turn boundary, otherwise `requires_respawn`), and the tier map itself is derived, not hardcoded
(`GET /{ws_id}/config_tiers` `:1759-1784` -> `agent.core.agent.config_field_tiers`, `:1769`). There is no
instance-scoped override anywhere in that chain, and nowhere to persist one (Section 2.2).

## 3. Templates vs instances: the two-object model

### 3.1 Definitions

A **template** (a.k.a. definition) is a durable blueprint: a dict in `workers.json` or a file under
`~/.thoughtmachine/worker_templates/`. It has no runtime state, no session, no containers, no lifetime; it
is edited by hand or by the form, and it is what a spawn reads (Section 2.1, Section 2.3 step 1).

An **instance** is one running `WorkerThread`: its own session, workspace, runtime mode (thread or process),
generation epoch, sidecar directory, and -- possibly -- containers. It is created *from* a definition
(`Worker._action_spawn` looks the dict up by name and passes it as `definition`, `worker_thread.py:5905-...`,
stored `:1433`) and is never created from a template file directly; templates only seed/merge into
`workers.json` (Section 2.1).

### 3.2 What the Live Panel lists

- **It lists INSTANCES**: one row per running worker. That is what an operator acts on -- the row is the
  thing that has a `stop`, a `pause`, a live token count and a `generation`. Source:
  `/api/workspace/{ws}/workers/active` (`WorkerManagementPanel.jsx:810`), joined against the in-memory
  registry via `_worker_active_entry` (`workspace_routes.py:719`).
- **It must not list templates.** A template is a blueprint; it belongs on the blueprint/create surface
  (today `WorkerBlueprintPanel.jsx` plus the template fetch at `:855`). Listing blueprints beside live rows
  is what forces the current panel to render two different objects with one visual grammar, and it is why
  "stop" has to be disabled conditionally rather than being a property of the row's type.
- **A definition with zero instances is not a live worker** and must not get a status pill. Its correct
  representation is "0 running" on the blueprint row, not a greyed-out instance.

**PROPOSED**: the Live Panel's row set is defined as *the set of keys returned by the active-instances
registry*, full stop -- no synthesis of rows for definitions, and no fallback to the filesystem scan except
when explicitly labelled "found on disk, not running" (which is exactly the C44 boot-catalogue case,
`docs/design-c44-worker-state-reattachment.md:422`).

### 3.3 What identifies an instance today

There is no instance id in the durable record, so identity is the *composition of the sidecar path*:
`<workspace>/workers/<session_id>/<name>/status.json` for session-scoped instances, or the legacy
`<workspace>/workers/<name>/status.json` when there is no session
(`web_ui/backend/health_routes.py:66-77`, `:89-96`). That is: **workspace_path + session_id +
instance_label**, where `instance_label` is the worker name (plus any collision suffix the runtime applied
when building the directory, `worker_thread.py:1440-1441`).

In memory the identity is richer but not durable: `instance_id` (`:1429`), `worker_name` (`:1432`),
`session_id` (`:1435`), `workspace_id` (`:1436`), thread name `worker-<instance_label>` (`:1431`), and the
`_generation` epoch (`:1451`); the registry key is `(session_id|_NO_SESSION_KEY, worker_name, instance_id)`
(`tools/workspace/worker_registry.py`, aliases `worker_thread.py:754-758`, `_NO_SESSION_KEY` `:86`).

**PROPOSED**: persist that composite as the instance record -- `(workspace_id, session_id, instance_label,
generation)` written once at spawn into `status.json` alongside the existing keys, with `instance_label`
stable across restarts and `generation` monotonic, so that a boot-time scan can rebuild the same keys the
registry would have used. This is the *same* key scheme C44-G8 already asks for
(`docs/design-c44-worker-state-reattachment.md:515`); it is stated here only so the capability registry does
not invent a competing one, and the persistence mechanism itself (who writes, when, atomicity) is C44's
problem, not this document's.

### 3.4 The two objects side by side

| Object | Where stored | Lifetime | Who may edit | What it controls |
|---|---|---|---|---|
| Template (definition) | `workers.json` in the workspace, or a file in `~/.thoughtmachine/worker_templates/` / bundled `resources/worker_templates/` (`workspace_capabilities.py:463-509`, precedence `:478-484`, merge `worker_thread.py:5492-5515`) | forever; survives restarts and clones | the operator, via the form (`WorkerManagementPanel.jsx:507`) or by hand | what the *next* spawn gets: prompt, tools, footprint, temperature, turns (Section 2.3 step 1) |
| Instance | memory + sidecar dir `<workspace>/workers/<instance_label>/{context.json,status.json,generation}` (`worker_thread.py:1440-1441`, `:4526-4527`, `:4802-4859`, `:4529+`); no durable record (`workspace_capabilities.py:409-414`) | from spawn to stop/process death (Section 2.2) | the operator, only via verbs (`stop`/`pause`/`resume`/`kill`) plus the W9 config patch (`:1140`) | what this running worker actually does right now: its `enabled_tools` snapshot, its footprint snapshot, its session permissions as read live at the gate (`agent/core/tool_executor.py:593-599`) |

## 4. The capability set: tool subset, FROZEN at spawn, injected via a secure ToolRegistry

### 4.1 FROZEN AT SPAWN

**The rule.** The tool set is resolved once, at spawn, and injected. A later edit to the template affects
only the next spawn. There is no live re-scoping, no dynamic widening, and no "apply now" for the tool set.

**Why this is not a new idea -- the code already has this exact split, and already relies on it.** The
permission footprint is canonicalized once at construction (`worker_thread.py:1461-1463`) and thereafter
only read (`:3054` restricted merge with session permissions, `:3071` copied into the child config). The
permission *gate*, by contrast, re-reads the on-disk session record on every call
(`agent/core/tool_executor.py:593-599`, `get_effective_permissions(..., session_id=..., workspace_id=...)`),
capped by that frozen footprint (`:642-646`). The observable asymmetry is deliberate and worth preserving
verbatim: **a worker can lose access mid-run (revocation propagates immediately) but cannot gain it without
a respawn.**

The capability registry adopts the same asymmetry for a stronger reason: a tool set that can grow at runtime
is a tool set whose audit is only valid at an instant nobody recorded. Freeze makes the template the single
readable answer to "what could this worker do?", which is the entire point of Section 1.2.

**What freeze costs, stated honestly.** A template edit needs a respawn to take effect, so the operation is
"edit, then respawn" and the UI must say so rather than pretend. The mechanism for that already exists for
config: the W9 tier map classifies fields as `hot_swap` or `requires_respawn`
(`web_ui/backend/workspace_routes.py:1759-1784`, `:1651-1653`). **PROPOSED**: the tool subset is registered
in that same tier map as `requires_respawn`, so no new user-facing concept is introduced -- the panel's
existing `workerRespawnPlan` (`WorkerManagementPanel.jsx:218`) already knows how to react to that tier.

### 4.2 Injected via a secure ToolRegistry

**Today.** `_get_tool_registry()` (`worker_thread.py:5117-5125`) is one process-global
`{name: class}` map, populated from `iter_tool_classes()` and memoized in `_TOOL_REGISTRY` (`:5121`).
`enabled_tools` is a *name list*; availability is a membership test against a map that always holds
everything. "Enabled" is a filter output, not a grant. The practical consequence: any code path that skips
or mis-orders one of the four filters in Section 2.3 produces a worker that works fine with more access,
and nothing fails.

**PROPOSED -- the registry becomes per-spawn and injective.** At spawn, build a registry *instance* for this
worker from the template's declared subset: resolve each declared name to a class, and hand the worker that
map (and only that map). Resolution then has no second source: a name not in the subset is not resolvable at
all, because there is no global map in the lookup path. This is **defence in depth**, and it is specifically
defence against the failure mode above: a filter bug can no longer widen the set, because the set is not the
filter's output -- the registry *is* the set.

Three constraints on the proposal:

- **Additive to the blocklist, never a replacement.** `_WORKER_BLOCKLIST`
  (`worker_thread.py:737-741`) stays, applied before the subset is built, and the spawn-boundary check
  (`:6335-6337`) stays as-is. The blocklist is a safety net for names that must never be constructible by a
  sub-agent regardless of template intent; the registry is a scoping mechanism. Removing the net because the
  registry exists would trade one guarantee for another.
- **Truncated build -> loud failure at spawn.** If the template names a tool the build no longer ships, the
  spawn **fails**, naming the missing tools. Today the equivalent path is soft: unknown names and
  permission failures accumulate in `missing_tools` and the worker is constructed with whatever survived
  (`:6333-6341`, `:6362-6365`), which is the right behaviour for *permission* refusals (the operator may
  legitimately have less grant than the template assumed) and **PROPOSED** to be the wrong behaviour for
  *nonexistent* tools (a typo or a deleted tool must not silently produce a narrower worker, because the
  narrowing looks like success). Splitting those two cases is the change.
- **Subset-of-parent remains an invariant.** The intersection at `:3017-3019` is not weakened or removed;
  the per-spawn registry is built from the *post-intersection* set. A worker still cannot exceed its parent,
  and a parent narrowing at spawn time still narrows its children.

**PROPOSED -- migration for `tools: []`.** The empty list keeps its current meaning ("all simplified tools
minus the blocklist") for exactly one release, but is **reported**, not silently honoured: at spawn, an
empty `tools` list produces a warning that names the resolved set size, and the panel marks the definition
as "implicit / unrestricted". Flipping empty to mean "nothing" in a single change would silently disarm
every existing `tools: []` worker -- the shipped default template among them
(`resources/worker_templates/default.json`) -- which is the opposite of the auditability this document is
for.

## 5. Reattachment across restart, and the no-cross-workspace-leak requirement

Reattachment is specified in `docs/design-c44-worker-state-reattachment.md`; this section does not restate it.
It asks the one question C44 does not: what happens to a *frozen capability set* when the process that froze it
dies, and what stops a reattached instance from being bound into the wrong workspace.

### 5.1 What is restored today, and what is not

A worker instance's durable footprint is the on-disk triple under `workers/<session_id>/<name>/` --
`context.json`, `status.json`, and the generation counter. Enumerators read `status.json` per directory
(`web_ui/backend/health_routes.py:59-108`, session-scoped branch `:66-77`, legacy by-name branch `:89-96`) and
that is the whole of the recovery story: it recovers *records*, never *runtimes*. Nothing walks `workers/*` at
boot and rebuilds a handle or a registry -- C44-G1, `docs/design-c44-worker-state-reattachment.md:422`. So a
capability registry that is rebuilt only at spawn has no boot path at all today; it either gains one or
reattachment cannot restore capability state.

The key shape is already inconsistent, and the registry must not inherit the wrong half. The in-memory worker
manager keys on `(session_id, worker_name, instance_id)` (quoted at
`docs/design-c44-worker-state-reattachment.md:517-518`); the UI catalogue keys on the bare directory name
(C44-G8, `:515-519`). A registry built on the catalogue's key inherits G8 before it has written a line of its
own logic.

### 5.2 The registry-specific question: what happens to the frozen set

Two candidates:

- **(a) Re-resolve the subset from the named template on reattach.** Cheap: no new persistence, the template
  stays the single source. Wrong: "frozen at spawn" becomes true only for the lifetime of a process. The window
  in which the template can change is exactly the window the right panel exists to exploit (Section 6), so the
  instance comes back with a different capability set than the one the operator audited -- no event, no log, no
  operator action. Worse, the drift is one-directional in practice: an operator adding a tool to the template
  widens *every* reattached instance at once. And if the template was deleted while the instance was down, (a)
  has no set at all: it must either fail the reattach (discarding state the disk triple preserved) or fall back
  to `tools: []`, which resolves to *everything minus the blocklist*
  (`tools/workspace/worker_thread.py:3011-3016`) -- the most privileged set in the system, produced by the least
  deliberate path.
- **(b) Persist the RESOLVED tool list alongside the instance.** Costs a durable record. Every other property is
  better: the word "frozen" is literally true across a process boundary; a reattach can be diffed against the
  template and against the previous instance generation; a deleted or edited template has zero effect on a
  running or stopped instance, matching the spawn-snapshot semantics of Section 4.

**Decision: (b).** The rationale reduces to one sentence -- under (a) a template edit is a silent, retroactive
capability change on instances that are not running, and under (b) it is not. A designed-in auditability
mechanism that can be bypassed by editing a different file while the process is down is not a mechanism.

**PROPOSED -- what the record contains and what it does not.** The persisted record holds the resolved tool
*names* (post-intersection, post-blocklist), the resolution timestamp, the definition name and version it came
from, and a digest of the parent's then-effective set. It does **not** hold tool classes or instances: a
reattached instance re-resolves names to live objects, because a class object is not durable state. Therefore a
name in the record that the current build no longer ships is a reattach *failure*, not a quiet narrowing --
the same loud-failure rule Section 4 applies at spawn, applied at reattach. Silent narrowing at reattach would
recreate exactly the hole the record exists to close.

### 5.3 Reattachment must not cross a workspace or a session

The failure mode is concrete and not exotic. A registry keyed on the worker NAME alone will bind the wrong
instance the first time two workspaces both contain a worker called the same thing -- which is normal, not
adversarial: names come from the operator or from the template (`resources/worker_templates/default.json`), so
collisions across workspaces are expected. Under a name-keyed registry, a caller in workspace A asking for
`accounting-agent` can be handed workspace B's instance: its config becomes patchable (the config route resolves
by name, `web_ui/backend/workspace_routes.py:1638`), its context readable, its containers cleanup-eligible, its
capabilities inherited by anything it spawns.

The only place in the tree where the three identity components are composed into a single record today is the
status reader: `worker_name` (`web_ui/backend/health_routes.py:78`), `session_id` (`:79`), `workspace_path`
(`:80`) -- and the legacy branch mirrors it at `:97-99`. The disk layout encodes the same thing
(`workers/<session_id>/<name>/`, `:66`). That record shape is the model to copy; a route is not an enforcement
mechanism, which is why the invariant is stated as an API constraint rather than a convention.

**Invariant: identity = workspace + session + instance label; a name alone is never a key.** Operationally:
every registry operation takes the full tuple; a name-only lookup is not expressible in the API surface, so
callers cannot accidentally rely on it; a cross-workspace reattach returns not-found rather than the best match;
and a resolution that finds candidates in more than one workspace is an error, never a pick-first.

---

## 6. Per-instance configuration overrides from the right panel, without mutating the template

### 6.1 What exists: three layers, none of them a durable instance override

1. **The stored definition.** `PUT .../workers/{name}` writes it, and it is what a respawn reads -- the config
   route says so explicitly (`web_ui/backend/workspace_routes.py:1664-1666`).
2. **The live config push (W9).** `POST /{ws_id}/workers/{name}/config` full route at
   `web_ui/backend/workspace_routes.py:1638`. It accepts an optional `instance_id` query parameter
   (`:1644-1647`), resolves the runtime handle for that exact instance (`:1686`), and pushes the patch into the
   live runtime (`:1706`). The response is the runtime's own `config_ack` -- `ok` plus `tier` -- never an
   optimistic "applied" (`:1651-1656`), and `applied_live` is true only when the ack says `ok` *and* `hot_swap`
   (`:1749`). No live handle is answered honestly as `409` + `requires_respawn` (`:1687-1703`).
3. **The tier map.** `GET /{ws_id}/config_tiers` (`:1759-1785`) returns every `AgentConfig` field's tier, derived
   by `agent.core.agent.config_field_tiers` (`:1769`), the same authority `Agent._can_hot_swap` delegates to
   (`:1763-1767`), so a UI badge cannot drift from the verdict the process acts on. `hot_swap` means the change
   lands at the next turn boundary; anything else means `requires_respawn`.

The frontend does not hardcode a field list: `workerConfigPatch` intersects the stored definition with the tier
map (`web_ui/frontend/src/components/WorkerManagementPanel.jsx:195-201`), `patchNeedsRespawn` answers whether the
patch forces a respawn (`:206-210`), and `workerRespawnPlan` couples label, disabled state, and kill-after-apply
into one decision (`:218`).

State it plainly: **this path edits the DEFINITION, and the instance-addressed push is in-memory only.** The
`instance_id` parameter proves the addressing exists (`:1644-1647`, `:1686`); nothing durable is written under it,
so the push dies with the process and a respawn comes back on the old values. There is no instance-scoped
override that survives a restart, and there is no per-instance capability override of any kind.

### 6.2 **PROPOSED** -- an instance override layer

**PROPOSED** A fourth layer, resolved at spawn and addressable per instance:

    template (stored definition) -> instance override (persisted) -> runtime ack (live, ephemeral) -> effective

Rules that are not negotiable if the layer is to be worth building:

- **Never writes back to the template.** The override is persisted per instance under the Section 5.3 identity
  tuple, next to `context.json`/`status.json`, never in the definition store and never in
  `resources/worker_templates/*.json`. A test asserts the template bytes are unchanged after an override save
  (Section 8.2).
- **Which fields:** those the tier map already classifies (`:1759-1785`), plus `tools` and `permission_footprint`.
  The latter two are *not* in the tier map -- they are definition fields (`agent/models/worker_definition.py:38-40`)
  consumed at spawn (`tools/workspace/worker_thread.py:3017-3020`, footprint read at `:3054`/`:3071`) -- so the
  override layer must declare their tier itself (always `requires_respawn`) instead of pretending the map covers
  them. An override UI built only from `config_tiers` structurally cannot express a capability override.
- **A `requires_respawn`-tier field is never hot-applied.** The write either rejects it (400, naming the field and
  its tier) or queues it as next-spawn (200 with `tier: requires_respawn`, `applied_live: false`). One policy per
  field, shown in the panel. A silent accept is the failure mode this whole document is against.
- **Only the runtime ack may claim "live".** The override layer must never synthesize `applied_live: true`;
  `:1749` remains the single place that bit is set.
- **Precedence is precedence, not a blind dict merge.** Absent = inherit from the layer below; explicitly null =
  unset at this layer. If those two are indistinguishable, "revert to template" cannot be implemented honestly.

**PROPOSED** panel affordances that follow: an instance picker (the panel today reads the definition -- e.g.
`worker?.max_tokens` at `WorkerManagementPanel.jsx:519`), a two-column diff of template value vs override value
per field, a "revert to template" that deletes the override rather than writing the template's value into it, and
the tier badge from the same map the backend uses.

### 6.3 The token-limit trap

This is the sharpest defect in the panel and it deserves to be stated without hedging.

The operator sees a field labelled as a maximum context-token limit. It initialises from `worker?.max_tokens`
(`web_ui/frontend/src/components/WorkerManagementPanel.jsx:519`) and posts `payload.max_tokens` (`:568`). The
stored definition model does declare `max_tokens` and `truncation_limit` (`agent/models/worker_definition.py:68-81`)
-- and those fields are **dead**: `_build_agent_config` documents their deliberate non-injection at
`tools/workspace/worker_thread.py:3000-3003`, because `AgentConfig` has no such attributes to set. So typing a
number into that box changes a value in a JSON file and enforces nothing. `max_context_tokens` also appears in
the status record the UI reads (`web_ui/backend/health_routes.py:86`), which makes the box look wired to a live
measurement while the control path is absent.

What *does* reach the runtime: `warning_threshold_tokens` and `critical_threshold_tokens`
(`agent/models/worker_definition.py:82-90`), which map onto `token_monitor_warning_threshold` /
`token_monitor_critical_threshold` on `AgentConfig`. So "warn earlier" is real today and "cap the context" is not.
A per-instance *threshold* override is buildable on the existing injection path; a per-instance *limit* override
has nowhere to land.

**PROPOSED -- what a real per-instance token-limit override requires**, in order: (1) the limit fields must exist
on `AgentConfig` with a tier of their own -- a context cap cannot take effect mid-turn, so `requires_respawn`,
not `hot_swap`; (2) the "deliberately not injected" block at `:3000-3003` must be replaced by a documented
mapping, with the reason for the original omission re-examined rather than deleted; (3) the panel field must be
repointed at the new key, and relabelled if "max context" and "max output" are different limits, because one box
labelled as the former currently writes a name that reads like the latter; (4) someone must decide what the limit
*does* on breach -- truncate the context, refuse the turn, or spawn a successor -- because a limit with no defined
breach behaviour is a number, not a control.

---

## 7. Container access and the Docker ruling

> **Ruling.** The absence of a `docker` binary inside a worker container is BY DESIGN. A worker never talks to the
> Docker daemon directly; code execution is proxied by the `DockerCodeRunner` tool, which runs on the host side and
> drives the resource container through `ContainerManager`. Container isolation must therefore be proven VIA THE
> TOOL PROXY. A probe that looks for a docker binary inside the worker container measures the design, not a gap,
> and a "fix" that ships the binary to satisfy the probe removes the boundary it was testing.

### 7.1 How a worker reaches a container today

`DockerCodeRunner` is a tool class declaring two required categories,
`["filesystem:write", "container:true"]` (`tools/docker_code_runner.py:60-61`). It imports the docker SDK at module
scope with a soft-fail fallback (`:41-50`) and `ContainerManager` from `infra.container_manager` (`:52-55`) -- both
imports resolve in the *host* process, which is where the daemon socket lives. The tool is dispatched like any
other tool in-process by the executor (`agent/core/tool_executor.py:801-821`), and the category gate runs before
it (`:612-648`) against the worker's canonicalised permission footprint (`:642-646`). Both halves of the
capability -- daemon access and authorisation -- exist on the host side of the boundary; inside the worker
container neither exists, and neither is needed.

### 7.2 Registry consequence: a category, reached through a proxy, not a tool name

Three things follow, and all three are about not creating a second authority for one question:

1. **Narrowing `tools` must not be assumed to remove the ability to run code.** Removing `DockerCodeRunner` from
   the resolved set removes *that* tool; whether code can still be executed depends on which other tools survived
   and which categories the footprint grants. The category gate is what removes the ability; the registry decides
   what is available to be gated.
2. **The registry composes with the category gate and never duplicates it.** The registry answers *which tools
   exist for this instance*; `required_categories` plus the footprint answer *whether a chosen tool may run*
   (`agent/core/tool_executor.py:612-648`). Encoding `container:true` into registry data would put the same
   question in two places and guarantee they eventually disagree.
3. **Both narrowing steps must appear in the audit record.** An operator who narrows `tools` and sees the
   container category still granted must be able to read *why* -- spawn-time subset resolution (Section 4) and the
   per-call category check are different mechanisms and the record has to show both, or narrowing looks stronger
   than it is.

### 7.3 Test consequence

Every isolation claim in Section 8.2 is made **through the tool proxy**: call the tool and assert the refusal, not
a shell probe for a binary. Concretely, with `container:true` absent from the footprint, invoking
`DockerCodeRunner` must be refused by the gate at `agent/core/tool_executor.py:612-648`; with the tool removed
from the subset, it must be unavailable regardless of footprint. A worker-side `docker ps` returning
command-not-found is the expected output of a correct design and must never be written into a test as a failure.

---

## 8. Open questions, tickets, test strategy

### 8.1 Tickets

1. **Dead schema.** `max_tokens`/`truncation_limit` are declared (`agent/models/worker_definition.py:68-81`) and
   never injected (`tools/workspace/worker_thread.py:3000-3003`). Delete or wire; leaving them is a UI that lies.
2. **UI posts a dead field.** `payload.max_tokens` (`WorkerManagementPanel.jsx:568`) labelled as a context limit --
   same ticket as 1, and the relabel cannot land before the field means something (Section 6.3).
3. **No persisted instance record.** `pid` is plumbed (`tools/workspace/worker_thread.py:3460-3486`) but not
   durable, and container ownership is *derived* per call (`tools/workspace/worker_container.py:3620`, cleanup
   `:3644`) rather than stored. This blocks Section 5.2(b) and any honest reattach.
4. **`missing_tools` is log-only at spawn.** The list is accumulated at `worker_thread.py:6336`, `:6340`, `:6362`;
   the sole consumer is a warning emitted *after* `thread.start()` -- `:6622-6626`,
   `logger.warning("Worker '%s' spawned but tool(s) not found: %s", ...)`. It is not in any return value or status
   record; the spawn result carries the worker's own output (`:6644-6660`). **Settled:** narrowing at spawn is
   invisible to the caller, so Section 4.2's "silently" is accurate for nonexistent tools as well, and Section
   4.3's "fails loudly" is a change to make, not a description of current behaviour.
5. **No boot-time registry rebuild.** C44-G1 (`docs/design-c44-worker-state-reattachment.md:422`).
6. **Catalogue/registry key mismatch.** C44-G8 (`:515-519`); the capability registry must key on the tuple
   (Section 5.3), not the directory name.
7. **`tools: []` is the most privileged setting** -- "everything minus the blocklist"
   (`worker_thread.py:3011-3016`) -- and it is what the shipped default template carries
   (`resources/worker_templates/default.json`).
8. **Instance addressing exists without instance storage.** The config route already accepts `instance_id`
   (`workspace_routes.py:1644-1647`) and resolves per instance (`:1686`); nothing persists under that key across a
   respawn (Section 6.1).
9. **Capability overrides are invisible to the tier map.** Definition-only fields
   (`agent/models/worker_definition.py:38-40`) are correctly never hot-pushed (`WorkerManagementPanel.jsx:195-201`
   intersects), but they are absent from `config_tiers` (`workspace_routes.py:1759-1785`), so an override UI built
   from the map alone cannot express them (Section 6.2).

### 8.2 Test strategy

Each test asserts on an observable, not on an implementation detail:

1. `test_subset_frozen_at_spawn` -- spawn from a template, edit the template's `tools`, assert the running
   instance's resolved set is unchanged and that the next spawn differs.
2. `test_subset_never_wider_than_parent` -- narrow the parent at spawn; the child's resolved set is a subset of the
   parent's (`worker_thread.py:3017-3019`).
3. `test_spawn_fails_loudly_on_missing_tool` -- template names a tool the build does not ship; spawn must
   surface an error naming it. This test fails against today's warn-and-continue (`:6622-6626`) by design; it is
   the regression test for ticket 4.
4. `test_blocklisted_tool_never_constructible` -- a name in `_WORKER_BLOCKLIST` (`:737-741`) is skipped and the
   reason is recorded (`:6335-6337`), for every definition, including `tools: []`.
5. `test_reattach_restores_the_same_frozen_set` -- snapshot the resolved names, kill the process, reattach, compare
   exactly. Under Section 5.2 candidate (a) this test fails whenever the template changed while the process was
   down; that is its purpose.
6. `test_reattach_cannot_cross_workspace` -- two workspaces, one identically named worker each; a reattach in
   workspace A never resolves B's instance, and no API surface accepts a name-only lookup (Section 5.3).
7. `test_instance_override_does_not_mutate_template` -- save an override; assert the template file's bytes and the
   stored definition are unchanged, and that the next spawn sees the override.
8. `test_requires_respawn_override_never_hot_applied` -- an override on a `requires_respawn`-tier field returns
   `applied_live: false` (or 400) and the live config is unchanged; `workspace_routes.py:1749` stays the only
   place `applied_live` can become true.
9. `test_container_access_denied_through_proxy` -- with `container:true` removed from the footprint, invoking
   `DockerCodeRunner` is refused at `agent/core/tool_executor.py:612-648`. Assert the refusal, not the absence of a
   binary (Section 7.3).
10. `test_container_runner_absent_from_subset` -- with `DockerCodeRunner` removed from the resolved set, the tool is
    unavailable even with a full footprint.

### 8.3 Open questions for the engineers who implement this

1. Does an instance override outlive its template? If the template is deleted, does an instance with an override
   survive, and against what base does it resolve?
2. Build upgrades vs frozen sets: when a release ships a new tool, may a reattached instance gain it? (Recommended:
   no -- a capability appearing across a restart is an escalation nobody approved.)
3. May an instance override *widen* `tools` beyond the template? Widening the parent-child relation is a clamp at
   spawn (`:3017-3019`); widening an instance relative to its own template has no rule yet.
4. `requires_respawn` override policy: reject or queue -- one policy for all fields, or per field?
5. Where does the resolved set live: inside `status.json`, which every enumerator already reads
   (`health_routes.py:66-96`), or a sibling file that costs no enumerator change but adds a second thing to keep
   in sync?
