# Host Port: the single chokepoint for host access

**Status:** design pass -- document only. This is **not** a gate, it **blocks nothing**, and it changes no code. No `host_port.py` exists in the tree and nothing imports one. The spec is written so that the implementation wave has a fixed target, and so that a reviewer can reject the implementation by comparing it against this document rather than against a memory of it.

**Ruled by:** Main Gen 39, 2026-10-24. Measured against `tools/host_bash_tool.py`, which already does at the tool level what the port will do once, at the boundary.

## 1. The claim

> Safety does not depend on tools being programmed correctly; it depends on the execution boundary refusing at the point of action. The host port is the boundary for host access. CI enforces the convention in the honest case; review and the red team catch the dishonest one. That is the strongest honest claim, and it is what the machine actually promises.

> This is the first time the machine has a single chokepoint for host access that checks the kill switch at the point of action. Every prior guarantee about host access depended on the tool being honest at the gate. This one doesn't.

The host port is the single chokepoint for host access: one function that every command leaving the sandbox for the host must pass through, so that the check happens at the point of action instead of at the point where each tool's author remembered to write it.

One qualification belongs here rather than in a footnote. The first paragraph above is prospective about CI. Today no test enforces the import-to-declaration convention: `tests/test_tool_import_declaration_match.py` does not exist, and the only artefact resembling a guard is parked and unactivated at `.thoughtmachine/working_docs/tool_no_wrapping_guard.py`. The CI enforcement that the claim names is a deliverable of the implementation wave, not a property the tree has now. `working_agreements.md:549` carried the opposite impression until 2026-10-24; the correction is `ce127e03` (`docs(doctrine): the no-tool-wrapping rule is not CI-enforced`). That correction landed 2026-10-24 as PR #194; line 549 now reads as the corrected claim.

## 2. The function signature

```
host_run(command, *, workspace_id, session_id, grain,
         timeout=60, cwd=None, env=None) -> HostRunResult

host_run_async(command, *, workspace_id, session_id, grain,
               timeout=60, cwd=None, env=None) -> HostRunHandle
```

`command` is what runs on the host, in the form that will actually be executed. It is recorded verbatim in the audit line's `argv` field, so a pre-expansion form would produce an audit line that does not describe what ran.

`workspace_id` identifies the workspace the call is attributed to and whose defaults apply. It is the same value that `workspace_capabilities._workspace_dir` validates, and the port inherits that validation rather than repeating it.

`session_id` is the calling session. It supplies the effective permissions the port checks in step (a) and the attribution the audit line carries. A call without a session cannot be attributed, and mirrors `check_system.py:1098-1105`, where a missing session scope fails closed rather than widening to the global view.

`grain` is the caller's declared host-execution grain for this call. Passing it explicitly is what keeps the port from needing to know which tool called it: the tool states the authority it believes it holds, and the port checks that statement against the session's effective permissions.

`timeout` is in seconds and is enforced by the port, not by the caller. Section 8 gives the signal sequence. A caller-supplied `subprocess.run(timeout=...)` is exactly the arrangement this parameter replaces.

`cwd` is the working directory on the host; `None` means the workspace root. `env` is the environment handed to the child and is the credential injection point described in section 7; `None` means inherit.

`host_run_async` is the same primitive, not a second one: same arguments, same check order, same audit line, returning a handle instead of a completed result. Its implementation is not part of this wave, because the shape of the handle belongs to the worker job primitive and is decided in that design pass. Naming it here fixes that it is the same choke point, so nothing can grow a second path to the host while claiming to be "the async one".

## 3. What host_run checks, in order

The order is the spec. A future implementation that performs these checks in another order is a different design, not a variation, because each step is only safe given the ones before it.

1. **Grain banned in effective permissions -> refuse.** The `grain` argument is resolved against the session's effective permissions, and a value that resolves to `banned` stops the call before any side effect. `tools/host_bash_tool.py:92-105` is the existing precedent: it reads only executor-injected permissions and defaults to `banned` when the key is absent.

2. **Kill switch off -> refuse.** A call that survives step (a) is still refused when the operator's kill switch is off. This is the check that no tool-level gate can make on the operator's behalf, and it is the reason the port exists: it is the first place where the kill switch is consulted at the point of action rather than at spawn or construction time.

3. **Audit line written.** The record is opened before the child exists, so a call that hangs, or that is killed by the port's own timeout, still leaves a trace of what was asked for and under whose authority. Section 6 gives the shape.

4. **Subprocess spawned, timeout enforced by the port.** Only now does anything run. The timeout is owned here, by the signal sequence in section 8, not by a `timeout=` keyword handed to a library call and not by the caller.

Refusals at steps (a) and (b) return a typed result naming their reason, not an exception. The reason must be traceable to the check that produced it (Inv.3), because an untraceable refusal is indistinguishable from a bug. The async form is the one exception: a refusal there surfaces through the handle, since the caller has no return value to inspect.

## 4. The taxonomy, and why it is not printed here

The primitive-to-family mapping does not appear in this document, and its absence is the design, not an omission. The ticket that commissions this work states the rule in its own words:

> The primitive-to-family mapping is not a document. It is named constants at the top of `tests/test_tool_import_declaration_match.py`, next to the code that enforces them. One place. Versioned. Reviewable in the same diff as the behaviour it constrains.

The reason is on disk as a worked example. `working_agreements.md:549` asserted that the no-tool-wrapping rule was enforced in CI by `tests/test_tool_no_wrapping.py`, a file that does not exist, and nobody noticed for a day; the correction is `ce127e03` (section 1). A mapping restated in a design document would be a second authority from the moment it was written, and the scan's constants would drift from it silently. So this document names the shape of the enforcement and refuses to hold a copy of its data.

The scan must cover four import shapes. They are requirements on the test, and naming them here is legitimate because they are structural, not a copy of the mapping:

1. **Module-level** -- `import subprocess` at the top of a module. `tools/host_bash_tool.py:47` and `tools/mcp_client.py:11` are the live examples.

2. **Function-local** -- an import inside a method body, invisible to a scan that only walks module-level statements. `tools/workspace/check_system.py:1112` is the live example, and it is also one of the two sites in section 5.

3. **Indirect** -- reach that never names the primitive in an `import` statement: `importlib.import_module("subprocess")`, `__import__(...)`.

4. **Dynamic-factory code** -- a class built by a factory, so the declaration lives at the factory rather than on the class. `tools/mcp_manager.py:203` (`create_model(...)`) is the live example.

Obfuscated access is out of scope for the AST scan. `getattr(subprocess, "run")`-style reach defeats static analysis, and pretending otherwise would inflate the guarantee; review and the red team are the controls for that shape, and section 9 restates the limit so no one reads the scan as covering it.

## 5. The migration list

Two sites are confirmed defective. The list is two rows because the ticket's census found two; it is not a summary of everything the port touches.

| Site | Declares | Reach | Net effect |
|---|---|---|---|
| stdio MCP tool classes (`tools/mcp_client.py`) | inherits `[]` from `tools/base.py:81` | `import subprocess` at `:11`, `subprocess.Popen` at `:226-234`; capability attached by the dynamic factory at `tools/mcp_manager.py:203`, which sets `network:outbound` only for `HTTPMCPClient` and `SSEMCPClient` (`:223-228`) | a session holding neither `network:outbound` nor `mcp:connect` can drive a stdio MCP server process on the host |
| `CheckSystem` (`tools/workspace/check_system.py`) | `[]` at `:240`, knowingly (comment `:233-239`) | `import subprocess` at `:1112`, `tail -n 500` at `:1115-1118`; also starts a container at `:723-739` and reads `~/.thoughtmachine/workspaces` at `:977` | the gate cannot see this tool's reach; today it is mitigated only by membership in `_WORKER_BLOCKLIST` |

HostBashTool is the reference implementation -- it declares `host_bash:allow` at `tools/host_bash_tool.py:88` and does its own fail-closed grain check. The two sites above should look like it after migration.

The ticket's remaining buckets are not migration work and are not omissions from this table: four tools were examined and found sound, and two judgement calls were recorded rather than filed. The scan will find more once it covers the four shapes in section 4; those become follow-up tickets filed in the implementation PR, not blockers on this wave.

## 6. The audit line

Every call that reaches step (d) leaves one record, written as JSON:

```json
{
  "timestamp": "2026-10-24T12:00:00.000000Z",
  "workspace_id": "ws-1",
  "session_id": "sess-...",
  "grain": "ask",
  "argv": ["docker", "ps"],
  "exit_code": 0,
  "duration_ms": 12
}
```

`timestamp`, `workspace_id`, `session_id`, `grain` and `argv` are known at spawn time and are written then. `exit_code` and `duration_ms` are filled on return.

The split is deliberate. Because the line is written before the child is spawned, a call that hangs forever is still traceable: there is a record naming the command, the workspace, the session and the grain it ran under, which is precisely the case where a post-hoc record would be missing. The completion fields are then a note added to a record that already exists, rather than the only evidence that the call happened.

## 7. The credential injection point

Credentials reach a host child through `env=`, never through `argv`. Command arguments are visible in `ps`, in the audit line, and in whatever the child logs; an environment variable is not.

This is the point at which "never argv" stops being a convention that each tool is trusted to follow and becomes structurally enforceable in one place. The port is the only code that constructs the child environment, so the rule can be asserted there once, for every caller, and the number of places a reviewer has to check drops to one.

W3 and W4 implement the resolution; nothing about credential fetch, scoping or redaction is settled here. This wave names the parameter and fixes its position in the signature, so that the implementation cannot later be shaped into an argv-passing design without contradicting a written spec.

## 8. The timeout semantics

The port sends `SIGTERM`, and if the child is still alive after a one-second grace window it sends `SIGKILL`. The grace window exists because a well-behaved child that is told to stop should be allowed to flush and exit, and the `SIGKILL` exists because a child that ignores `SIGTERM` must not be able to extend its own lifetime by refusing.

The port owns that sequence. Relying on `subprocess.run(timeout=...)` alone would kill the direct child and leave its descendants running, which for a host command is the interesting case rather than the edge case: the process that matters is often the one the shell started.

This is the substrate for the worker job primitive. A job needs to be cancellable, and cancellation that leaves orphans on the host is not cancellation; the job primitive design pass builds on this signal sequence rather than inventing a second one.

## 9. What the port does not cover

Two exclusions, stated so that the next seat does not claim more than the port delivers.

**Obfuscated access.** Reach constructed at runtime -- `getattr(subprocess, "run")`, a primitive fetched from a string -- defeats the AST scan, and a tool that wraps such a call after migration is not caught by the port either. The controls are review and the red team. This is a real hole and it is named as one.

**A tool with legitimate `execution:full` used maliciously.** The port checks a grain it is told; a caller that legitimately holds `execution:full` and then asks for something harmful is inside the envelope, and no boundary check at this layer distinguishes it from a harmless call by the same caller. The controls are review and the red team.

Both are outside the port because the port is a boundary on authority, not a judge of intent. Stating them is the difference between a guarantee and a claim of omniscience: a reader who knows the two holes can reason about what the port is worth, and a reader who does not will overestimate it.

## 10. What this wave does not do

- No `host_port.py`. The spec is the deliverable.
- No CI test. The import-to-declaration scan is a separate ticket and a separate wave.
- No migration of the two sites in section 5.
- No doctrine amendment.
- No `host_run_async` implementation. The job primitive owns its shape.
- No credential resolution. Section 7 names the parameter and stops there.
- No activation of the parked guard at `.thoughtmachine/working_docs/tool_no_wrapping_guard.py`.

## 11. Filed

2026-10-24, Gen 49, ruled by Main Gen 39. Taxonomy pending Main's confirmation before the implementation brief is written.
