# Bug: the tool registry has no membership invariant — `_ContainerControlBase` is the class that would slip through

**Status: claim premise FALSE.** Not registered. Class *is* constructible, but unreachable via the registry.
All facts below are source-read + measured (python3, `sys.path=/workspace`).

## 1. Summary

Claim: "`_ContainerControlBase` (tools/container_control.py:119) is a base class, not a callable verb, yet registered in `TOOL_CLASSES`, so enumerators instantiate it."

Verdict: the second half does not hold. The class has no `execute`, no `tool: Literal[...]`, no `name`/`description`/`category` — it is base-only — but `tools/__init__.py:187-196` imports exactly the nine concrete tools and `:198-206` appends exactly those nine. Measured `_ContainerControlBase in TOOL_CLASSES` → `False`, `in SIMPLIFIED_TOOL_CLASSES` → `False`, `len()` each → `37`. What remains is a real design gap: `_ContainerControlBase()` **succeeds silently** (pydantic v2, no ctor guard) and its `execute()` raises a bare `NotImplementedError('')`. Nothing guards the registry against a class like this.

## 2. What is there

`_ContainerControlBase(ToolBase)` at `tools/container_control.py:119` contains only: `required_categories` ClassVar `["container:true"]` (:122), `_make_manager` (:124-162), `_respond` (:164-166). No `execute`, no `__init__`, no `name`, no `description`, no `category`, no `tool: Literal[...]`, no `@abstractmethod`. Contrast `ContainerStartTool` (:169) which declares `tool: Literal["ContainerStartTool"]` at :205.

Nine subclasses: `ContainerStartTool` :169, `ContainerExecTool` :312, `ContainerStopTool` :415, `ContainerRestartTool` :463, `ContainerStatusTool` :545, `ContainerListTool` :751, `ContainerBuildTool` :802, `ContainerLogsTool` :858, `ContainerRemoveTool` :921.

`ToolBase` is a pydantic v2 `BaseModel` (tools/base.py:36), all fields optional; `execute` at tools/base.py:298 is `def execute(self) -> str: raise NotImplementedError` — *not* `@abstractmethod`, no `ABCMeta`, no ctor guard. `name` is `ClassVar ""` (:93); `tool_name()` :95-98; `get_required_categories` (:104-120) just returns `cls.required_categories`.

Measured on the base: `issubclass(base, ToolBase)` True; `mro` = `[_ContainerControlBase, ToolBase, BaseModel, object]`; `model_fields` = the nine optional infra fields (`workspace_path`, `token_limit`, `is_docker`, `container_workspace_path`, `session_permissions`, `effective_permissions`, `agent_config`, `session_id`, `is_worker_context`); `tool_name()` → `'_ContainerControlBase'`; `required_categories` → `['container:true']`; `hasattr description`/`category` → `False`; `execute()` → `NotImplementedError('')`. Registry health: subclasses-in-registry = the nine concrete names; no member lacks its own `execute`; none inherits `ToolBase.execute`; none fails `issubclass(x, ToolBase)`.

## 3. Why it matters, per consumer

| Consumer | Site | Reads name only? |
|---|---|---|
| `iter_tool_classes`, `simplified_tool_classes`, `get_tool_class` | tools/__init__.py:88-90, 93-95, 98-103 | yes (`cls.__name__`) |
| `_update_simplified_toolset` | tools/__init__.py:41-60 | yes |
| `model_to_openai_tool` (via agent/core/agent.py:166) | tools/utils.py:95-125 | yes — `model.model_json_schema()`, `model.tool_name()` :115-118 |
| default `enabled_tools`, `get_filtered_tool_classes` | agent/config/models.py:128, 304-337 | yes |
| WS `tools_list`, tool panel | web_ui/backend/server.py:2214-2221; config_manager.py:392-402 | yes (`tool_name()`, `__doc__`) |
| `/api/tools` | web_ui/backend/server.py:3749-3788 | n/a — static `_ALL_TOOLS` list (session/tool_presets.py:21), never touches the registry |
| worker spawn validation | tools/workspace/worker_thread.py:4290-4318 (:3387-3389 name→class dict) | yes (`get_required_categories({})`) |
| **ToolExecutor** | agent/core/tool_executor.py:301-308 resolve by name; :657, :666 `tool_class(**tool_args)` | **no — the only constructing site** |

So the blast radius of a future mis-registration is one site: `ToolExecutor` would build the instance (succeeds) and then fail at `execute()` with an empty `NotImplementedError` message. Every other consumer would emit the base as a tool named `_ContainerControlBase` with no schema.

## 4. Severity: **LOW**

Derived from evidence, not assumed: the defect is absent today; even if registered, instantiation succeeds silently and `execute()` dies with a bare `NotImplementedError` — no security or user-visible effect; the inherited `required_categories` is the same `container:true` the real tools use, so no permission bypass; the guard costs ~8 lines of test.

## 5. Fix shape

No removal step applies: the base is not in `TOOL_CLASSES` (:25), not in the import list (:187-196), not appended (:198-206); `tools/container_control.py` has no `__all__` and no bulk list. If a future import/append line for it appears, delete that line and keep the class importable — it is the base of all nine tools (e.g. :169). Do **not** make it abstract (the nine subclasses rely on plain inheritance; `ToolBase` deliberately has no `ABCMeta`).

Add two tests to the existing `tests/test_tool_registry.py` (already holds registration tests, e.g. `test_git_read_registered` :49-51): `test_container_control_base_not_registered` and `test_every_registered_tool_class_overrides_execute`.

## 6. How to prove fixed

```python
from tools import TOOL_CLASSES, SIMPLIFIED_TOOL_CLASSES, get_tool_class
from tools.base import ToolBase
from tools.container_control import _ContainerControlBase

assert _ContainerControlBase not in TOOL_CLASSES
assert _ContainerControlBase not in SIMPLIFIED_TOOL_CLASSES
assert get_tool_class("_ContainerControlBase") is None
assert {c.__name__ for c in TOOL_CLASSES if issubclass(c, _ContainerControlBase)} == {
    "ContainerStartTool", "ContainerExecTool", "ContainerStopTool", "ContainerRestartTool",
    "ContainerStatusTool", "ContainerListTool", "ContainerBuildTool", "ContainerLogsTool",
    "ContainerRemoveTool"}
assert [c.__name__ for c in TOOL_CLASSES if getattr(c, "execute", None) is ToolBase.execute] == []
```

Both tests pass today — they are guards, not red tests.

## 7. Cross-refs and ownership

`ticket-hygiene-no-tool-wrapping.md` owns rule letter (b) — "no code outside `tools/__init__.py` may reference `TOOL_CLASSES`/`SIMPLIFIED_TOOL_CLASSES`" (§3 there, cited at tools/__init__.py:70-74). That governs *readers*; this ticket guards *contents* — complementary, no overlap. Grep of `working_docs/*.md` for `_ContainerControlBase` hits only `wave-container-sprint-close.md:59` (mem/cpu sizing in `_make_manager`), so no other ticket owns this.

Correction: `ticket-bug-paginate-tool-bypasses-required-categories.md` cites `tools/paginate_tool.py:98/102-105/115-116`, but that file no longer exists (removed in commit `4a4bd692`, per hygiene ticket §2; `tools/*paginate*` globs empty). Hence `ToolExecutor` is the *only* constructing consumer.

## 8. Draft vs source

Draft asserted registration; source disproves it (`tools/__init__.py:187-196` + `:198-206`, plus measured membership). The class is constructible but unreachable, and `execute()` raises a bare `NotImplementedError`, so the draft's "remove from `TOOL_CLASSES`" is an empty diff. Re-verify with: `python3 -c` importing `TOOL_CLASSES`, `SIMPLIFIED_TOOL_CLASSES`, `_ContainerControlBase`; print `len()` of each, membership, `tool_name()`, `required_categories`, `hasattr(..., "description")`, then call `execute()`.

Unverified: `37/37` is import-time state — MCP tools register later (`tools/mcp_manager.py:356-366` via `register_tool_class`; `create_tool_class` :163), so the counts grow and whether MCP-supplied classes define `execute` was not measured; the guard test above would catch it. Pre-existing, unrelated: pydantic warns "field name `name` shadows an attribute in parent" at `tools/container_control.py:169`.
