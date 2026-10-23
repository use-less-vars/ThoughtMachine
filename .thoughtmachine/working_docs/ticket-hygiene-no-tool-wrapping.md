# hygiene/no-tool-wrapping — tools are leaves, not nodes

**Category:** `hygiene`
**Status:** open — rule accepted, guard parked, 13 sites to burn down
**Owner surface:** `tools/` (all), `tests/` (one guard file, parked)

## 1. The rule (verbatim)

> "The tooling must be a flat, non-nested surface. No tool may invoke another tool. No wrapping. No tool symlinks. If a tool needs the capability of another tool, it uses a shared internal function, not the tool registry. Tools are leaves of the permission model, not nodes."

No exceptions.

Sequencing and looping is the agent loop's job — plan / dispatch / observe — never a tool's. A tool that paginates, retries across tools, or calls a second tool has taken over the loop; that is the agent's authority, not the tool's.

## 2. Why the whole class is banned, not just `PaginateTool`

`PaginateTool` was deleted in commit `4a4bd692`. Banning that one name would be the wrong fix, because the problem is the *shape*, not the class:

- **It re-enters the permission gate.** The outer call was authorized for tool X. The inner call is a second invocation of tool Y that the gate never saw, never attributed, and never counted.
- **It hides the real permission from the user.** The approval prompt names the wrapper; the capability actually exercised is something else. Consent to X becomes consent to Y.
- **It turns the tool registry into a call graph.** Once a tool may resolve and invoke another tool, you get composition, cycles, ordering, unbounded loops and re-entrancy — all outside the loop's control and untested.

A structural blocklist of forbidden tool names is not a gate: it is evaded by renaming, by a thin alias, or by the next tool someone adds. The ban is therefore **a rule plus a test** — the test derives its targets from the tree rather than from a fixed list of names.

## 3. The three rule letters

| letter | rule |
|---|---|
| **(a) IMPORT** | No module under `tools/` (other than the registry `tools/__init__.py`) may import another tool module or tool class — `from .x import Y`, `from tools.x import Y`, `import tools.x`, `from tools import X`. |
| **(b) REGISTRY** | No code outside `tools/__init__.py` may reference `TOOL_CLASSES` or `SIMPLIFIED_TOOL_CLASSES`. The registry is the executor's surface; it is not a dispatch table for tools. |
| **(c) DYNAMIC** | No dynamic import by string inside `tools/` — no `importlib.import_module(...)`, no `__import__(...)`. String resolution is how a static ban gets bypassed. |

## 4. Current census

`48` `.py` files scanned under `tools/`, `29` tool modules, `29` tool classes, **`13` violations** → **3 of the 4 guard tests currently fail** (the 4th is the non-vacuity self-check, which passes).

### (a) 5 sites

| site | what | verdict |
|---|---|---|
| `tools/code_modifier_utils.py:12` | `from .code_modifier import CodeModifier`; the class is instantiated in plain helper `apply_code_modifier()` and used to call the tool's private `_add_function` / `_add_method` / `_add_import` / `_add_class` / `_replace_function_body` | **GENUINE — worst site.** A util drives a tool's private methods; a tool used as an engine. |
| `tools/git_write_tool.py:9` (with `:57` `class GitWriteTool(GitReadTool)`) | module-level import of `GitReadTool` (+ helper `resolve_git_execution_mode`); the imported class is the **base class** of the tool defined in the same file | **GENUINE — tool inherits tool.** |
| `tools/workspace/worker.py:22` (`Worker` also in `__all__` @100) | pure re-export facade ("W4 facade extraction"); `~40` symbols including the tool class `Worker` re-exported from `worker_thread` | Not tool logic, but a static tool import. |
| `tools/workspace/check_system.py:119` | `from tools.git_info_tool import resolve_git_execution_mode` in a module-level `try/except ImportError` probe | a **function**, not a tool — worth moving out anyway. |
| `tools/workspace/check_system.py:881` | same helper, re-imported lazily as `resolver` inside `CheckSystem._query_capabilities` (circular-import workaround) | same; one extraction closes both. |

### (b) 7 sites — all legitimate runtime registration / dispatch

| site | what |
|---|---|
| `tools/mcp_manager.py:360,364,365` | `from . import TOOL_CLASSES`, `if cls not in TOOL_CLASSES:`, `TOOL_CLASSES.append(cls)` inside the nested `_do_register()` of `register_mcp_tools()` — background MCP tool registration |
| `tools/workspace/worker_thread.py:1726,1727` | `from tools import SIMPLIFIED_TOOL_CLASSES` + name list-comp in `WorkerThread._build_agent_config` — worker's default tool set |
| `tools/workspace/worker_thread.py:3388,3389` | `from tools import TOOL_CLASSES` + `{cls.__name__: cls for cls in TOOL_CLASSES}` in `_build_tool_registry()` — registry construction |

These are the executor doing its job: no tool semantics, no tool invoking a tool. They are violations of the *letter* and must still be cleared, by giving them an accessor API (feed-forward, §7) rather than by narrowing the rule.

### (c) 1 site

| site | what | verdict |
|---|---|---|
| `tools/workspace/check_system.py:436` | `__import__('dataclasses').fields(WorkspaceCapabilities)` inside `CheckSystem._query_permissions` | **collateral** — stdlib, not a tool. |

## 5. Detection defect on record

`class GitWriteTool(GitReadTool)` is **neither** a tool module **nor** a direct `ToolBase` subclass. Verified at park time: `"tools.git_write_tool" in tool_modules → False`, `"GitWriteTool" in tool_classes → False`. So any scoping phrased as *"a file that defines a `ToolBase` subclass"* silently exempts the **second-worst** site in the census.

This is recorded here as a known limitation, not as licence to re-scope. **Do not re-scope the rule to make sites disappear.** If tool-ness detection is changed, it must be made *transitive* (a class whose base resolves to a tool class is itself a tool class), which strictly adds sites — never subtracts.

## 6. Enforcement design

- **Pure `ast` static analysis.** Nothing imports or executes the application; the check reads source, walks the tree, and reports.
- **Targets derived from the tree**, not from a hand-maintained list: tool modules/classes are discovered, so a new tool is covered on the commit that adds it.
- **No whitelist, no allow-list, no `xfail`, no `skip`.** A site is either clean or it is a violation. (This is why the guard is parked rather than landed-red with exemptions.)
- **Non-vacuity self-check.** A synthetic `PaginateTool`-shaped source is fed through the *same* scanner and must trip all three letters; otherwise the rule can rot into a green no-op.
- **Failure output** is `path:line: [letter] detail || source line`, under a header carrying the count and the scan scope, so a failing run tells you exactly what to fix.

## 7. Burn-down plan (ordered)

1. **(b)** Add accessor API in `tools/__init__.py` — `register_tool_class()`, `iter_tool_classes()`, `simplified_tool_classes()` — and migrate the 7 call sites in `mcp_manager.py` and `worker_thread.py` off the raw registry names. *Small; removes 7 of 13.*
2. **(c)** `import dataclasses` at module top in `tools/workspace/check_system.py`; use `dataclasses.fields(...)`. *Trivial; removes 1.*
3. **(a)** Three separate fixes:
   - `tools/workspace/worker.py` — lazy module-level `__getattr__` re-export so the facade keeps working without a static tool import;
   - `tools/code_modifier_utils.py` — extract the CST operation bodies into a non-tool core module (e.g. `tools/code_modifier_core.py`) that both the tool and the helper call;
   - `tools/git_write_tool.py` — extract the shared git plumbing (`resolve_git_execution_mode` + common runners) into a non-tool module (e.g. `tools/git_execution.py`) so `GitWriteTool` stops inheriting `GitReadTool`; this also removes `check_system.py:119/881`'s reason to reach into a tool module.
4. **Activate the guard and land it green in the same PR** — a parked guard that never activates is just a comment.

## 8. Parked guard source

`.thoughtmachine/working_docs/tool_no_wrapping_guard.py` — the full, unweakened guard implementation with a header explaining why it is parked. Activation steps are in that header: move back to `tests/test_tool_no_wrapping.py`, drop the header, confirm `python -m pytest -q tests/test_tool_no_wrapping.py` is green.
