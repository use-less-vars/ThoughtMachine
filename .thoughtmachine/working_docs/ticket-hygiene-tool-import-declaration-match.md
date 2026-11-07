# hygiene/tool-import-declaration-match — the declaration must match the imports

**Category:** `hygiene`
**Status:** open — rule accepted, no guard written
**Owner surface:** `tools/` (all), `tests/` (one new guard file, not yet created)
**Severity:** HIGH — attack-surface class
**Filed:** 2026-10-24, Gen 49, ruled by Main Gen 39

## 1. Summary

A tool's declared permission category is a promise. Its imports are its behaviour. Nothing compares the two, and the security gate reads only the promise.

Every tool class declares `required_categories` (a `ClassVar` at `tools/base.py:81`, default empty) or overrides `get_required_categories()` (`tools/base.py:105`, whose default returns the ClassVar at `:120`). `agent/core/tool_executor.py` obtains the categories from that method and `security/security_gate.py::check_required_categories` grants or denies on them. Nothing in that path reads the module's source. A tool that declares `[]` and imports `subprocess` is granted on the strength of the declaration, and then runs a host process.

This is a different class from `hygiene/no-tool-wrapping`, and it sits above it. A wrapping tool reached host capability by invoking another tool, so a census could find it by looking for tool-shaped imports and registry lookups. A direct-reach tool needs no second tool: it imports a host primitive and uses it. The no-wrapping guard is structurally blind to this class, because it only asks whether a module under `tools/` touches another tool. A module that touches the host directly passes it clean.

Both classes are the same defect seen at different levels: a fact about reach is asserted in one place and exercised in another, and no mechanism compares them. Inv.6 — one fact, one authority — is violated by the gap, not merely by the wrapper.

## 2. Rule shape: the import must match the declaration

One-way implication. If a module imports a host-reaching primitive, its tool class must declare a category from the host-execution family. The converse is not required and must never be asserted.

Host primitives that require a host-execution declaration: `subprocess`, `os.system`, `os.popen`, `ctypes`, `pty`, `os.exec*`, `os.spawn*`.

Network primitives that require an egress declaration: `socket`, `urllib.request`, `http.client`, `requests`, `httpx`, `aiohttp`.

**The allowed-equivalence set matters, and the literal form of the rule is wrong without it.** The naive rule 'imports subprocess implies declares `execution:full`' would convict `HostBashTool`, which declares `["host_bash:allow"]` (`host_bash_tool.py:88`) and is correct — `host_bash` carries its own value grain on its own scale (`banned < ask < allow`, `security_gate.py:302-340`) and the tool additionally performs a fail-closed grain check of its own at `host_bash_tool.py:92-100`. The rule must therefore accept **any category in the host-execution family** for a host primitive, and **any category in the egress family** for a network primitive, rather than one exact string. The families are named constants at the top of the guard file, beside the primitive-to-family mapping; see section 4.

The scan direction is fixed: **if import, then declaration. Never the converse, never a biconditional.**

## 3. Indirect reach must be covered

An import scan that reads only `ast.Import` and `ast.ImportFrom` nodes is a speed bump. Each of the following reaches a host primitive without an import statement naming it, and the guard must catch each:

- `importlib.import_module("subprocess")`
- `__import__("subprocess")`
- `getattr(subprocess, "run")` reached through a module alias, and any `getattr` whose base resolves to a stdlib module
- `from importlib import import_module` followed by a call with a string literal

The tree is currently clean on this axis. `tools/` contains zero `__import__` calls, zero live `import_module` calls (the only `importlib` occurrence is a comment at `docker_code_runner.py:392`), and every `getattr` in `tools/` reads injected executor or context state or AST nodes, never a module. The guard exists to keep it that way, not to find something today. Write it now precisely because the tree is clean: a scan landed against a clean tree can be made strictly failing with no whitelist and no exempt list, and that is the only kind of guard that survives.

## 4. The taxonomy lives in the test file

The primitive-to-family mapping is not a document. It is named constants at the top of `tests/test_tool_import_declaration_match.py`, next to the code that enforces them. One place. Versioned. Reviewable in the same diff as the behaviour it constrains.

The reason is a failure this repository keeps repeating: a policy in one file, an enforcement in another, drifting silently. `hygiene/no-tool-wrapping` already shows the shape — `working_agreements.md:549` states that the rule is enforced in CI by `tests/test_tool_no_wrapping.py`, and that file does not exist. The only artefact is a parked guard at `.thoughtmachine/working_docs/tool_no_wrapping_guard.py`. The document claims an enforcement that is not there, and nothing in the system notices the claim is false. A taxonomy held in a document would reproduce that failure by construction.

Minimum structure the guard should have, mirroring the parked no-wrapping guard's better decisions:

- `_find_repo_root()` that walks parents for a directory containing both `tools/` and `tests/`, and raises if it cannot find one. The parked guard's header records that a parked-location `parents[1]` resolves to `.thoughtmachine/` and scans zero files; that failure was caught once already and must not be re-introduced.
- A non-vacuity test asserting the scan visited a plausible number of files and found a plausible number of tool classes.
- An unparsable file must fail the scan, never pass it: a syntax error is a hole in the pin, so `ast.parse` failure must raise rather than skip.
- A self-test that feeds the scanner a synthetic module which imports `subprocess` and declares `[]`, and asserts it is flagged. A guard with no test of its own detection is indistinguishable from a guard that returns nothing.
- No whitelist, no `xfail`, no `skip`.


**Four import shapes the scan must cover.** Per Main Gen 39's D6 ruling (2026-10-24), the scan is not just `ast.Import` / `ast.ImportFrom`. The four shapes:

1. **Module-level imports** — `import subprocess` at the top of a module. The common case.
2. **Function-local imports** — `import subprocess` inside a function body. The `CheckSystem` case (`tools/workspace/check_system.py:1112`). `ast.walk` finds these; a naive top-of-file scan does not.
3. **Indirect imports** — `importlib.import_module("subprocess")`, `__import__("subprocess")`, `from importlib import import_module` then call with a string literal.
4. **Dynamic-factory code** — a module that constructs tool classes at runtime via `create_model(...)` or equivalent. The scan sees the factory, not the instances. The stdio MCP case (`tools/mcp_manager.py:203`). A direct import in the factory source is the finding; the classes it builds inherit whatever the factory source declares.

**Scope limit — obfuscated access is out of scope for the AST scan.** Per Main Gen 39's D6 ruling (2026-10-24): `getattr(subprocess, "run")` reached through aliasing, `getattr(os, "system")`, and any equivalent obfuscated reach is **out of scope for this scan**. Review catches the dishonest reach; the AST scan catches the honest mistake. The test file's docstring must state this limitation explicitly — a scan that appears to catch obfuscation but doesn't is worse than one whose scope is named.

## 5. Over-declaration safety — the test docstring must say this

Because the rule is one-way, a tool that declares a host-execution category and imports no host primitive is **not** a defect. It is a tool with a reservation. The scan must never flag it, and no seat may 'fix' it.

The test docstring must contain, close to verbatim:

> This scan is one-way: if import, then declaration. Over-declaration is fine. Do not 'fix' a tool that declares execution:full and doesn't import subprocess — it is not a defect.

That paragraph is load-bearing, not courtesy. A future seat reading a failing biconditional would delete declarations to make the scan pass, and deleting a declaration makes a tool *more* permissive. The comment is what stops the fix from inverting the control.

## 6. Discovered instances

The scan below ran over all 48 `.py` files under `tools/` (29 tool modules plus helpers) on 2026-10-24. It looked for `subprocess`, `os.system`, `os.popen`, `ctypes`, `pty`, `os.exec*`, `os.spawn*`, `socket`, `urllib.request`, `http.client`, `requests`, `httpx`, `aiohttp`, and for the declaration mechanism described in section 1.

**Confirmed defective — two sites.**

1. **stdio MCP tool classes — declared `[]`, spawns a host subprocess.** `tools/mcp_client.py:11` imports `subprocess`, and `:226-230` calls `subprocess.Popen(... stdin/stdout/stderr=PIPE)` to spawn a host command taken from the vault MCP registry. Capabilities are attached by whoever builds the class: `tools/mcp_manager.py:203` creates the classes dynamically with `create_model(...)`, and `:223-228` attaches `get_required_categories` yielding `["network:outbound"]` **only for `HTTPMCPClient` and `SSEMCPClient`**. A stdio client keeps the inherited `[]` from `tools/base.py:81`. Net effect: a session holding neither `network:outbound` nor `mcp:connect` can drive a stdio MCP server process on the host. The category is keyed on client *type*, not on the reach actually exercised. Compare `tools/mcp_server_connect.py:57`, which declares `["mcp:connect"]` and routes its spawn through `security/sandboxed_execution.SandboxedExecution.run(..., required_category="mcp:connect")` — that is the correct shape, and it is in the same tree.

2. **`CheckSystem` — declared `[]`, runs a host binary and starts containers.** `tools/workspace/check_system.py:240` declares `[]`. The module imports `subprocess` function-locally at `:1112` and calls `subprocess.run(["tail", "-n", "500", file_path], ...)` at `:1115-1118` — host binary execution reachable from a tool that declares nothing. It also starts a container at `:723-739` and reads `~/.thoughtmachine/workspaces` at `:977`. The empty declaration is knowing; the comment at `:235-239` explains that no `system:read` category exists in the gate vocabulary, so declaring one would make `check_required_categories` deny every call, because unknown categories fail closed, and that access control is instead the vault query allowlist. That reasoning is sound as far as it goes and the finding is still real: the gate cannot see this tool's reach, so the category gate and the operator's kill switch are both blind to it. The mitigating fact, and the only reason this is not the headline, is that `CheckSystem` is in `_WORKER_BLOCKLIST`, so it is not worker-reachable.

**Examined and found sound — no action.**

- `HostBashTool` (`tools/host_bash_tool.py:47` imports `subprocess`; `:303` calls `subprocess.run(cmd, shell=True, ...)`) declares `["host_bash:allow"]` at `:88`. Correctly declared, with an in-tool fail-closed grain check at `:92-100` reading only executor-injected effective permissions. This is the reference implementation.
- `GitReadTool` (`tools/git_info_tool.py:5` imports `subprocess`) declares `["git:read"]` via an override at `:279-286`. Every operation including `remote` is `git:read`, and `remote` runs `git remote -v` without contacting a remote, so there is no egress to declare.
- `GitWriteTool` (`tools/git_write_tool.py:5` imports `subprocess`) declares `["git:write", "filesystem:write"]` and adds `network:outbound` for `clone` (`:115-126`). Correct.
- `MCPServerConnect` (`:26` imports `subprocess`) uses it only to catch `subprocess.TimeoutExpired` at `:106`; the actual spawn goes through `SandboxedExecution` with `mcp:connect`. Correct.

**Judgement calls — recorded, not filed.**

- `CiReadTool` performs real outbound HTTPS (`urllib.request.urlopen` at `tools/ci_read_tool.py:93`) and declares `["ci:read"]` (`:176-183`), with no `network:outbound`. This is deliberate and documented at `:33-37` and `:168-173`. It is safe only because the `ci` domain defaults to `banned` (`security_gate.py:96`). The consequence for auditors: `ci` is a second egress axis beside `network:outbound`, and any 'who can reach the network' review that greps for `network:outbound` alone will miss it.
- `tools/workspace/worker_execution.py:29` imports `subprocess` and shells `docker exec <container> kill <pid>` at `:384-388`, while `Worker` declares `[]` (`tools/workspace/worker_thread.py:3444`, with a comment at `:3442-3443` recording that worker spawning was deliberately decoupled from session permissions on 2026-08-03). The reach is delegated rather than declared. Not a tool-module import violation, but 'Worker declares nothing' is not the same claim as 'Worker reaches nothing', and the distinction belongs on the record.

**Also on the record.** `security_architecture.md:376`, FINDING 10 of the 2026-06-09 audit, is titled 'Tool `required_categories` Declarations Are Correct'. It predates `HostBashTool`, `CiReadTool`, `MCPServerConnect` and the `check_system` host-`tail` path, and section 6 above shows it is no longer true. That finding should be marked superseded rather than trusted.

## 7. Blast radius

A tool that reaches host outside its declared envelope defeats the category gate at the only place it is enforced. The operator's kill switch is a control over host-side tool execution; a tool whose reach the gate cannot see is a tool the kill switch cannot stop. That is the failure the machine exists to prevent, stated in `security_architecture.md:9-32`, and it is why this is filed HIGH rather than as hygiene trivia.

Severity HIGH, attack surface, not currently exercised. Three of the four boundaries named in `security_architecture.md:13-20` are intact: the container boundary, the permission layer and the envelope. The defect is entirely in the fourth, tool-call dispatch.

## 8. Sibling

`hygiene/no-tool-wrapping` — ticket at `.thoughtmachine/working_docs/ticket-hygiene-no-tool-wrapping.md`, guard parked at `.thoughtmachine/working_docs/tool_no_wrapping_guard.py` (335 lines), rule at `working_agreements.md:543-557`.

Two corrections to how that sibling is usually described, both verified on 2026-10-24:

- **The guard is not in CI.** `tests/test_tool_no_wrapping.py` does not exist. A repo-wide search for the rule's identifiers in `*.py` returns only `tools/__init__.py` and the parked guard. `working_agreements.md:549` asserts CI enforcement by a file that is absent, which is itself an honesty-contract item: the document describes a control that is not running.
- **The parked guard cannot catch this class even when activated.** Its three letters are: import another tool, reference the registry, dynamic import by string. None of them reads the host primitives this ticket is about. The two guards are complements; activating one does not cover the other.

**`PaginateTool` was deleted in PR #184, not #186.** `09a06495` is the merge of `fix/delete-paginate-tool`; #186 is `b519532d`, `docs/gen48-paginate-residuals`. Any document attributing the deletion to #186 should be corrected on its next touch.

## 9. Not in scope

No fixes here. The stdio MCP declaration and the `CheckSystem` declaration are recorded and should be repaired under their own change, with tests, because both are behaviour changes in the security path. This ticket delivers a guard against the class plus a census of the instances.

## 10. Filed

2026-10-24, Gen 49, ruled by Main Gen 39.
