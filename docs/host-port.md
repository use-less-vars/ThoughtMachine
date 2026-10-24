# Host Port: the single chokepoint for host access

**Status: shipped, with one deferral and one known gap.** `thoughtmachine/host_port.py`
exists, exports `host_run`, `host_run_async`, `HostRunResult`, `HostRunHandle`,
`AUTHORIZED_VALUES` and `CREDENTIAL_SHAPED`, and is the only module in `thoughtmachine/`
and `tools/` that may import `subprocess` without a pinned exception; every other
importer in those roots is on the scan's exception list (section 5), each with a
named reason. The scan's roots are those two directories; modules in other roots
are not yet covered. `tools/host_bash_tool.py`
and the `event_log` read in `tools/workspace/check_system.py` are migrated. One stdio
spawn in `tools/mcp_client.py` is deferred on the async primitive (section 11). One real
hole -- inherited environment variables -- is described in section 7 and deliberately
not fixed here.

This document describes what the code does today. Where it disagrees with a comment in
the source, the code and this document win, and the comment is a bug.

**Ruled by:** Main Gen 39, 2026-10-24 (design pass). Implementation wave: `host_port.py`
plus the migrations in section 11.

## 1. The claim

> Safety does not depend on tools being programmed correctly; it depends on the
> execution boundary refusing at the point of action. The host port is the boundary for
> host access.

> This is the first time the machine has a single chokepoint for host access that checks
> the kill switch at the point of action. Every prior guarantee about host access
> depended on the tool being honest at the gate. This one doesn't.

The port is one function that every command leaving the sandbox for the host must pass
through, so the check happens at the point of action instead of at the point where each
tool's author remembered to write it.

Two qualifications belong here rather than in a footnote, because both are things a
reader will otherwise overestimate:

1. **CI enforces the convention in the honest case only.** `tests/test_tool_import_declaration_match.py`
   is an AST scan: any `import subprocess` outside `thoughtmachine/host_port.py` fails
   unless the module is on a pinned exception list (today: exactly one entry,
   `tools/mcp_client.py`, the deferral in section 11). It catches the four static import
   forms -- module-level, `as`-aliased, `from subprocess import ...`, and function-local.
   It cannot catch reach built at runtime (`getattr(subprocess, "run")`,
   `importlib.import_module("subprocess")`); section 12 says so plainly.
2. **The port checks authority, not intent.** A caller that legitimately holds
   `host_bash:allow` and asks for something harmful is inside the envelope. Review and
   the red team are the controls for that shape (section 12).

## 2. The signatures

```
host_run(command, *, workspace_id, session_id, grain, shell=False, timeout=60,
         cwd=None, env=None, client_type=None, effective_permissions=None,
         audit_dir=None) -> HostRunResult

host_run_async(command, *, workspace_id, session_id, grain, ...) -> HostRunHandle
```

`command` is what runs on the host, in the form that will actually be executed. It is
recorded verbatim in the audit line's `argv` field, so a pre-expansion form would produce
an audit line that does not describe what ran.

`shell=False` (the default) is **list form only**: passing a string with `shell=False` is
a `TypeError`, not a convenience, because `"echo a;b"` silently splitting on the shell
metacharacter is exactly the injection this port exists to prevent. `shell=True` accepts a
command string or an all-string sequence. This validation raises `TypeError`; it is not a
refusal and writes no audit line (section 4, step 9).

`workspace_id` identifies the workspace the call is attributed to and whose
`config.json` supplies both the kill switch and the cwd root. It must be a non-empty
string.

`session_id` is the calling session; it supplies the grants resolved in step 5 and the
attribution every audit line carries. It must be a non-empty string.

`grain` is the caller's declared host-execution grain -- a **category name**, not a
`category:value` pair (section 4). Passing it explicitly is what keeps the port from
needing to know which tool called it: the tool states the authority it believes it holds,
and the port checks that statement against the session's effective permissions.

`timeout` is in seconds and is enforced by the port, not by the caller (section 8).

`cwd` is the working directory on the host; `None` means the workspace root (section 6).
`env` is merged over the inherited environment and is the only point at which a caller can
add variables; it is the credential-injection surface, and the one place where the port is
weaker than it looks (section 7).

`client_type` is only consulted for `grain="mcp"` (step 6). `effective_permissions` is the
test/executor injection point that lets a caller supply its own authority map instead of
having it resolved from the vault. `audit_dir` overrides where the audit line lands
(section 9).

`HostRunResult` carries `exit_code`, `stdout`, `stderr`, `duration_ms`, `timed_out`,
`argv`, `grain`, plus an `ok` property that is true only for `exit_code == 0 and not
timed_out` -- a timed-out run reports `exit_code == -9` and must never be read as success.

`host_run_async` and `HostRunHandle` exist as signature-only stubs that raise
`NotImplementedError` (section 11). They reserve the name of the async twin so nothing can
grow a second path to the host while claiming to be "the async one".

## 3. What `host_run` checks, in order

The order is the implementation, not a description of it: each step is only safe given the
ones before it, and a version that performs them in another order is a different design.
Everything before step 9 is a refusal or a raise, never a silent widening.

1. **Grain must be a known category.** `grain not in AUTHORIZED_VALUES` -> refuse
   ("unknown permission grain ..."). This stops a caller naming a category the port has no
   value scale for -- there is no default grant and no fallthrough.
2. **Identity must exist.** `workspace_id` then `session_id` must be non-empty strings ->
   otherwise refuse ("host execution has no authority without a workspace/session").
3. **The session must belong to this workspace.** `_check_identity` asks the session
   registry for `session_id` and refuses when the row names a different workspace. An
   *unknown* session is acceptable only when the caller injected `effective_permissions`
   (an id-less/test caller carrying its own authority); otherwise the port has nothing to
   resolve grants from and fails closed.
4. **The operator's kill switch must be on.** `_workspace_config(workspace_id)` reads
   `<vault>/workspaces/<id>/config.json`, and `config.get("allow_host_resources") is not
   True` -> refuse, naming that path. `is not True` is deliberate: a missing file, a
   string `"true"`, or any other truthy value is off. This is the check no tool-level gate
   can make on the operator's behalf, and it is why the port exists.
5. **The session must actually hold the declared grain.** `_resolve_permissions` returns
   the effective map -- injected `effective_permissions` win, otherwise the grants are
   resolved from the vault for `(workspace_id, session_id)` through the security gate's
   disk mode, and **any** failure there is a refusal, never a default. The grant for
   `grain` must then be a string present in `AUTHORIZED_VALUES[grain]` (section 4). The
   `host_bash` refusal carries an extra sentence: the port cannot prompt -- no event bus,
   no queue, no interactive user -- so a session `ask` grant has nowhere to go and can
   never authorise a host command.
6. **`mcp` calls must come from an MCP client.** `grain="mcp"` with `client_type != "mcp"`
   -> refuse. Otherwise `mcp:connect` would be a general host-execution grain.
7. **No credential-shaped key in caller-supplied `env`.** Any key matching
   `CREDENTIAL_SHAPED` (`TOKEN|SECRET|PASSWORD|KEY|PAT|CREDENTIAL`, case-insensitive) ->
   refuse, with a message pointing at `credential_inject=True` (W3). See section 7 for
   what this does *not* cover.
8. **cwd must be inside the workspace root.** `_resolve_cwd` returns the workspace root
   for `cwd=None`, and for an explicit `cwd` resolves symlinks and requires the result to
   equal the root or sit beneath it; otherwise refuse (section 6).
9. **argv must be well-formed.** `_build_argv` normalises the command and raises
   `TypeError` on an insane `shell`/`command` pair.

Refusals raise `PermissionError` with the reason as the message, and write one audit line
each (section 9). They do not return a typed result -- the earlier design sketch in this
document proposed one, and the shipped code chose an exception so that a refusal cannot be
mistaken for a result by a caller that forgets to check. A permitted call proceeds to
audit-PRE, spawn, audit-POST.

## 4. The grain is a category name

`AUTHORIZED_VALUES` is the whole authorization surface, and it is three rows:

| grain | values the port accepts | catalog levels |
|---|---|---|
| `host_bash` | `allow` | `banned / ask / allow` |
| `mcp` | `connect`, `full` | `banned / connect / full` |
| `network` | `outbound`, `write` | `banned / ask / write / outbound` |

A `grain` is a **key of this mapping** -- a resource in `security/resource_catalog.py`
(`host_bash`, `mcp`, `network`, ...) -- never a `"category:value"` pair. The value comes
from the session's effective permissions; the caller only names the category. Two
consequences worth stating:

- `network` deliberately has no `full` level in the catalog, which is why the port does not
  list one. A caller-written `"network:full"` string would be refused at step 1 anyway,
  because it is not a key of `AUTHORIZED_VALUES`.
- Because the port cannot prompt, `ask` is never an authorized value for any category here.
  In particular `host_bash` accepts only `allow`; a session sitting at `ask` is refused,
  not escalated and not prompted.

## 5. What the port does not do about the taxonomy

The primitive-to-family mapping does not appear in this document, and its absence is the
design. It lives as named constants in `tests/test_tool_import_declaration_match.py`, next
to the scan that enforces them: one place, versioned, reviewable in the same diff as the
behaviour it constrains. A mapping restated here would be a second authority from the
moment it was written, and the scan's constants would drift from it silently.

The scan's rules, as pinned today:

1. `thoughtmachine/host_port.py` may import `subprocess` unconditionally; it is the
   positive control proving the scan actually finds imports.
2. Every other importer must be on `ALLOWED_EXCEPTIONS` with its reason, and that list is
   pinned by exact membership -- no duplicates, and no entry whose module no longer exists
   on disk (a stale allowlist is how these guards rot).
3. `tools/mcp_client.py` is on the list *only* as the documented deferral: it must still
   carry its deferral markers (`host_run_async`, `HostRunHandle`, `NotImplementedError`),
   so the exception cannot outlive its explanation.
4. `tools/host_bash_tool.py` and `tools/workspace/check_system.py` -- the two modules
   migrated in this wave -- must import no `subprocess` at all.

The scan's roots are `thoughtmachine/` and `tools/`; modules in other roots are outside
it, and `import subprocess` exists today in `tests/`, `scripts/`, `infra/`, `security/`,
`web_ui/`, `mcp_examples/` and the project root, so the gap is named rather than implied.

That last rule is the reason the migration is checkable rather than merely claimed: the
scan fails if a migrated module regrows the import.

## 6. cwd containment

`cwd` defaults to the workspace root, taken from the `root` key of
`<vault>/workspaces/<id>/config.json` and passed through `os.path.realpath`. One config
read serves both the kill switch and the root, so the two can never disagree.

- No usable `root` -> refuse. Not "fall back to the process cwd": a workspace that cannot
  say where it is gets no host execution.
- Explicit `cwd` -> `os.path.realpath(cwd)` must equal the root or start with
  `root + os.sep`. Symlinks are therefore resolved *before* the comparison, so a symlink
  planted inside the workspace cannot be used to reach outside it.
- Anything else -> refuse, naming both the requested path and its resolution.

**Behaviour change worth knowing:** before the port, `host_bash` ran in whatever directory
the agent process happened to inherit. Now it runs at the workspace root unless the caller
asks for a path inside it. A command that relied on the inherited cwd will now fail or, if
it is lucky, produce output relative to a different directory.

## 7. Environment and credentials

Credentials must reach a host child through `env`, never through `argv`: arguments are
visible in `ps`, in the audit line, and in whatever the child logs. The port is the only
code that constructs the child environment, so the rule can be asserted there once for
every caller.

What is enforced today, exactly:

- A **caller-supplied** `env` key matching `CREDENTIAL_SHAPED` is refused (step 7). No tool
  can hand a secret-value-shaped variable to a host child through `env=` and have it look
  routine.
- The child environment is `os.environ.copy()` merged with the caller's `env`. Keys
  **inherited from the parent environment are forwarded as-is and are not scanned**. If the
  agent process itself is running with `GITHUB_TOKEN` set, the host child sees it.

That second point is a real, named hole and it is not closed here. `CREDENTIAL_SHAPED` is a
filter on what the *caller* adds, not a filter on what the port *forwards*. Closing it
means the real `credential_inject` path (W3) -- an allowlist of variables a host child may
receive, rather than a name-shaped regex over the ones it may not -- and that is a
different piece of work. Anyone reading the constant as "the port never forwards
credentials" is reading a claim the code does not make.

## 8. Timeout semantics

The timeout is owned by the port. `subprocess.Popen(..., start_new_session=True)` puts the
child in its own process group; on timeout the port sends `SIGTERM` to that **group**, waits
`_KILL_GRACE_SECONDS` (5.0s), and sends `SIGKILL` to the group if it is still alive. A
timed-out call reports `exit_code == TIMEOUT_EXIT_CODE` (`-9`), `timed_out=True` and
`duration_ms` equal to the timeout in milliseconds.

The grace window exists because a well-behaved child that is told to stop should be allowed
to flush and exit; the `SIGKILL` exists because a child that ignores `SIGTERM` must not be
able to extend its own lifetime by refusing. Killing the group rather than the pid matters
because for a host command the process that matters is usually the one the shell started,
not the shell itself -- `subprocess.run(timeout=...)` alone would leave it running.

## 9. The audit trail

There are **two independent trails**, and they are not the same file:

### 9.1 `host_port_audit.jsonl` -- the port's own

Written by `_write_audit`, one JSON object per line, appended, best-effort by contract
(auditing failures never fail a call). Location resolution order: the `audit_dir`
argument, then `HOST_PORT_AUDIT_DIR`, then the log root from `agent._log_root.get_log_root()`.
If none resolves, nothing is written -- the port does not silently write to cwd.

Three record shapes:

```json
{"timestamp": "...", "event": "pre", "workspace_id": "ws-1", "session_id": "sess-1",
 "grain": "host_bash", "argv": ["ls"], "shell": false, "timeout": 60,
 "cwd": "/path/to/workspace", "decision": "allow"}
```

```json
{"timestamp": "...", "event": "post", "workspace_id": "ws-1", "session_id": "sess-1",
 "grain": "host_bash", "argv": ["ls"], "exit_code": 0, "duration_ms": 12}
```

```json
{"timestamp": "...", "event": "refused", "workspace_id": "ws-1", "session_id": "sess-1",
 "grain": "host_bash", "decision": "deny", "reason": "...", "argv": ["ls"]}
```

A permitted call therefore leaves **two** lines (PRE before the child exists, POST after it
returns); a refused call leaves **one**. The split is what makes a hung or killed call
traceable: the PRE line names the command, the workspace, the session, the grain, the shell
mode, the timeout and the resolved cwd before anything ran, and the POST line is a note
added to a record that already exists. On a refusal, `argv` is present only when the port
could build a best-effort preview; the record always carries `decision: "deny"` and
`reason`, which is the field to read when reconstructing why a call was stopped.

### 9.2 `host_bash_audit.jsonl` -- the tool's own

`HostBashTool` keeps its own six-field record: `timestamp`, `workspace_id`, `session_id`,
`command` (env values of length >= 6 redacted), `outcome` (`allow` / `deny` / `timeout` /
`allow` with an error note), `reason`. It writes `host_bash_audit.jsonl` in the same
directory the port uses when `audit_log_path` is supplied, so an operator sees both files
side by side -- but they are separate records written by separate code. The tool's line
says what the tool decided; the port's lines say what the boundary permitted.

## 10. Error surfaces

- Refusal: `PermissionError`, message = reason, one `refused` audit line.
- Malformed `command`/`shell` pair: `TypeError` from `_build_argv`, no audit line (nothing
  was permitted or refused -- the call was never well-formed).
- Timeout: not an exception. A result with `exit_code == -9` and `timed_out=True`.
- Non-zero exit: not an exception. A result with `ok == False`.

## 11. Honest status: what is migrated, what is not

**Migrated and pinned by tests.**

| Site | What changed |
|---|---|
| `tools/host_bash_tool.py` | Calls `host_run(...)` instead of spawning directly; declares `host_bash:allow`; keeps its own six-field audit line on top of the port's. |
| `tools/workspace/check_system.py` (`event_log`) | The `tail -n 500` subprocess is gone; the event log is read in-process from `EventLogger.instance().file_path`, still fail-closed without a session scope and still filtered to attributable records. |

**Migrated in part.**

| Site | What is done | What is missing |
|---|---|---|
| MCP tool permission gating (`tools/mcp_manager.create_tool_class`) | stdio server classes declare `mcp:connect`; HTTP/SSE classes declare `network:outbound`. A session holding neither can no longer drive a stdio MCP server. | -- |
| MCP stdio spawn (`tools/mcp_client.StdioMCPClient.start`) | Nothing. It still calls `subprocess.Popen(...)` directly and therefore still bypasses the port. | The migration is blocked on `host_run_async` / `HostRunHandle`, which are stubs. An MCP stdio child is long-lived (stdin/stdout stay open for the session), so the sync `host_run` cannot express it. |

The deferral is documented in a comment at the Popen site and pinned twice: the import scan
requires the deferral markers to stay while the import stays, and
`tests/test_mcp_client_uses_host_port.py` pins both the markers and the continued presence
of the `subprocess` import and the `Popen` call. The effect today is that the one unaudited
host launch left in the tree is a known, listed, tested exception -- not an oversight.

**Behaviour changes introduced by the migration.**

- `host_bash` runs at the workspace root, not in the agent's inherited cwd (section 6).
- A call with no resolvable identity, or with the workspace kill switch not exactly `true`,
  is refused. Tools that previously spawned first and checked later (or not at all) now see
  `PermissionError` before any side effect.
- A malformed `command`/`shell` pair raises `TypeError` instead of being passed to `Popen`.

**Known gaps, not fixed in this wave.**

1. Inherited environment variables are forwarded unscanned (section 7). Closing it is W3's
   `credential_inject`.
2. `host_run_async` and every `HostRunHandle` method raise `NotImplementedError`.
3. Runtime-obfuscated reach defeats the import scan (section 12).

## 12. What the port does not cover

Two exclusions, stated so the next seat does not claim more than the port delivers.

**Obfuscated access.** Reach constructed at runtime -- `getattr(subprocess, "run")`, a
primitive fetched from a string, `importlib.import_module("subprocess")` -- defeats the AST
scan, and a tool that wraps such a call after migration is not caught by the port either.
The controls are review and the red team. This is a real hole and it is named as one.

**A tool with legitimate `host_bash:allow` used maliciously.** The port checks a grain it is
told; a caller that legitimately holds that grain and then asks for something harmful is
inside the envelope, and no boundary check at this layer distinguishes it from a harmless
call by the same caller. The controls are review and the red team.

Both are outside the port because the port is a boundary on authority, not a judge of
intent.

## 13. Guidance for the next module that needs the host

1. **Call `host_run`, do not shell out.** If you are writing `import subprocess`, you are
   writing the code the import scan fails on.
2. **Pass the grain as a category name** -- `"host_bash"`, `"mcp"`, `"network"` -- never
   `"host_bash:allow"`. The value is not yours to assert; the port reads it from the
   session's effective permissions and only checks that it is in the scale for the category
   you named.
3. **Pass real `workspace_id` and `session_id`.** They are the attribution on every audit
   line and the key the grants are resolved from. If you have no session, you have no host
   authority -- that is the design, not a bug to work around with `effective_permissions`
   outside a test.
4. **Do not assume the inherited cwd.** Ask for `cwd=` explicitly if you need a directory,
   and expect the workspace root if you do not.
5. **Do not assume the inherited environment is safe.** The port refuses credential-shaped
   keys *you* pass and forwards everything it inherited; if a secret is in your own
   environment, the child sees it. Pass nothing sensitive through `env` until W3's
   `credential_inject` lands.
6. **A long-lived child is the async gap.** `host_run` is synchronous: it waits, enforces
   the timeout and returns. A child whose pipes stay open for the session (an MCP stdio
   server, an interactive process) needs `host_run_async` + `HostRunHandle`, which do not
   work yet.
7. **Do not copy the `mcp_client` deferral pattern.** A comment explaining why a direct
   spawn is temporarily acceptable, plus two tests pinning the comment, is what the
   exception list looks like when it is honest -- it is not a template for adding the
   second exception. If your call blocks on the async primitive, say so in the wave that
   owns the async primitive rather than spawning directly.

## 14. Filed

2026-10-24, Gen 49, ruled by Main Gen 39. Taxonomy confirmed (Main, 2026-10-24).
`host_port.py` and the two migrations shipped after the design pass; `host_run_async`, the
stdio MCP spawn and `credential_inject` remain W3 work.
