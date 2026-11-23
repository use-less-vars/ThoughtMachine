# The second host-execution chokepoint

**Summary.** Invariant I11 (`docs/MACHINE_INVARIANTS_AND_LORE.md:107`) says host execution goes
through `host_run`, and `thoughtmachine/host_port.py` enforces identity → kill switch → grant → env →
cwd → argv in a fixed order — but a second spawner, `security/sandboxed_execution.py`, is live on the
same host for git and MCP and is reached by two production call sites that never pass through it. The
two doors are not duplicates of one control set: door A is stricter on authority (kill switch, cwd
containment, credential-shaped env, process-group kill) and door B is stricter on hermeticity
(`HOME=/dev/null`, `GIT_CONFIG_SYSTEM/GLOBAL=/dev/null`), so neither can be deleted into the other
without a change. There is no import cycle between them (measured), which makes every option below a
sequencing question rather than an architectural one. All line numbers are at `12e52a46` (`origin/dev`).

## 1. What exists today

Door A — `thoughtmachine/host_port.py:557` `host_run`, 881 lines; ordered checks documented at
`:575-587`, grain vocabulary at `:80-101` (`host_bash: allow`, `mcp: connect|full`,
`network: outbound|write`, `git: write|write_on_feature_branch`). Door B —
`security/sandboxed_execution.py:48` `SandboxedExecution`, `run()` at `:95-160`, 237 lines.

| call site | door | grant named | cwd | what the caller does with the result |
|---|---|---|---|---|
| `tools/host_bash_tool.py:318` | A | `grain="host_bash"`, `shell=True` | defaulted; check (8) pins it to the workspace root | `timed_out` → error JSON; keeps an independent six-field audit record on top |
| `tools/workspace/worktree_defaults.py:86` | A | `grain="git"` | explicit (`cwd=cwd`, the worktree) | `PermissionError` → status 1 (`:95-103`); `result.ok` → 0 |
| `tools/git_info_tool.py:1195` (`_exec_host_raw`) | B | `git:<level>`, **unset** when the effective git grant is `ask` (`:1208`, `:1222-1223`) | `cwd=str(repo_root)` (`:1237`) | returns `(returncode, stdout, stderr)`; `TimeoutExpired` escapes |
| `tools/mcp_server_connect.py:93` | B | `mcp:connect` | **none** — inherits the agent process cwd | parses stdout as the JSON-RPC `initialize` reply; `TimeoutExpired` → error string (`:106-107`) |

Four production call sites, two doors. `tools/mcp_client.py` remains the known direct-`Popen`
bypass (pinned by `tests/test_mcp_client_uses_host_port.py`, blocked on `host_run_async` at
`:854`) and is out of scope here. `infra/containers/executor.py` names neither door (grep: 0 matches
for `host_port|sandboxed_execution`).

## 2. Control by control

| control | door A (`host_run`) | door B (`SandboxedExecution.run`) |
|---|---|---|
| operator kill switch `allow_host_resources` | check (4), `:672-686`, reading `_workspace_config(workspace_id)` (`:203`) | **absent from the door**; the git caller re-implements it tool-side (`git_info_tool.py:363-402`, called at `:949`); `mcp_server_connect` has none |
| grant | scalar grain, `ask` refused by construction | optional `"category:level"` string, **skipped entirely when `None`** (`:139-140`), and `ASK` is read as denied — which is why the git caller deliberately un-sets it on an `ask` grant (`:1208`), leaving no grant check at all on that path |
| cwd containment | check (8) → `_resolve_cwd()` `:425-478`: `realpath` must equal the workspace root or sit under it, and a workspace config with no `root` **refuses** | none — `cwd` is passed straight to `subprocess.run` |
| credential-shaped env | check (7) refuses caller-supplied credential-shaped keys unless `credential_inject=True`; inherited ones are stripped from `os.environ.copy()` and their NAMES audited (`:760-775`) | none — `extra_env` is merged un-scanned (`:222-237`); with `strip_env=False` the child gets a plain `os.environ.copy()` |
| process termination | `start_new_session=True` (`:799-805`) + `_signal_group()` SIGTERM→SIGKILL after `_KILL_GRACE_SECONDS = 5.0` (`:129`, `:132`); `exit_code == -9` | `subprocess.run(..., timeout=...)` only (`:145-155`): the direct child is killed, its children are not |

Three consequences that separate the doors beyond line-by-line hygiene:

**(i) cwd.** `_validate_repo_root` (`tools/git_info_tool.py:630-649`) contains a fail-**open** branch —
`if not ws_path: return repo_root` — so when no workspace resolves the repo root the tool hands to
door B is whatever it was given, and door B has no containment to catch it. The same argument to door
A would be refused at check (8).

**(ii) grandchildren.** Door B has no session/process-group handle, so a timed-out `git fetch` (ssh
transport) or an MCP server that spawns a subprocess leaves that child running after `run()` returns.
Door A's group kill exists precisely for this.

**(iii) credential env.** Door B's `extra_env` is the only environment channel with no
`CREDENTIAL_SHAPED` filter on it, and the git caller feeds it a session-supplied seam:
`extra_env=merge_git_environment(self._git_environment())` (`:1243`), where
`normalize_git_environment` (`:152-189`) forwards any plain-`str` key and only `GIT_PAGER`,
`GIT_CONFIG_SYSTEM`, `GIT_SSH_COMMAND`, `GIT_DIR`, `GIT_WORK_TREE` are pinned (`:143-149`). A key such
as `AWS_SECRET_ACCESS_KEY` passes that filter and reaches the child through door B; the same key
supplied to door A is refused at check (7) (`CREDENTIAL_SHAPED`, `:118-120`). The doors disagree about
what a credential is, for the same repository operation.

## 3. Options, with measured cost

Baseline for cost figures: `98 passed, 3 skipped` across `tests/security/test_sandboxed_execution.py`,
`tests/test_host_port.py`, `tests/test_host_port_kill_switch.py`,
`tests/security/test_host_git_switch_bites.py`, `tests/security/test_mcp_server_connect.py`.
Regression surface: door A is referenced by **9 test files / 146 test functions**; door B by **19 files
/ 452 test functions** (26 files in union — `test_host_port_kill_switch.py` and
`test_tool_import_declaration_match.py` name both).

**Option 1 — collapse door B into door A (one chokepoint).** Four measured blockers: `host_run` has no
stdin parameter (signature `:557-571`; door B's `input=` carries the MCP `initialize` request), no
`strip_env`/git-pin capability (door A starts from `os.environ.copy()` and pins no git config, so
collapsing would *lose* `HOME=/dev/null` + `GIT_CONFIG_SYSTEM/GLOBAL=/dev/null` hermeticity that door B
has today), no `mcp` grain mapping from `mcp:connect` (door A's `mcp` check (6) additionally requires
`client_type == "mcp"`, which `mcp_server_connect` does not have), and no `ask`-deferral semantics.
Cost: door A gains three parameters, door B loses a code path, and the 452-function door-B surface plus
138-function `test_git_info_tool_operations.py` must be re-baselined.

**Option 2 — parity in place.** Add the four missing controls to door B (kill switch read, cwd
containment, credential regex on `extra_env`, process-group kill) and leave two doors. Cheapest to
land: one module plus two call sites, no door-A change, door-A surface untouched. Fails the same way it
fails today unless a *shared* control module (not a copy) is extracted — the current defect is exactly
two implementations of one policy drifting apart.

**Option 3 — split the primitive.** Door A owns the spawn (identity, kill switch, grant, cwd, env
filter, group kill); door B keeps only env composition (the hermetic `HOME`/`GIT_CONFIG_*` profile) and
argv shape checks, and calls door A to execute. Preserves both stricter halves, needs the `input=`
parameter on door A and a grain map for `mcp`/`git`, and keeps the ask-deferral at the tool.

## 4. Recommendation

Take Option 3: door A should own every host spawn and door B should become a hermetic-environment
profile applied *to* door A, because door B's env hermeticity is the one control door A does not have
and the one a naive collapse would silently delete.

## 5. Sequencing, risks, what is not measured

Order matters: (a) add `input=` and an env-profile hook to `host_run`, with tests pinning that the
profile cannot widen `env`; (b) route `mcp_server_connect` through door A first — it is the site with
neither a kill switch nor a grant today and the smallest test surface (5 functions); (c) route
`_exec_host_raw` last, after the `ask`-deferral and `git:<level>` mapping have a door-A equivalent.
The regressions to watch are door-B suites that assert the *stripped* environment
(`tests/test_git_auth_env.py`, `tests/test_git_environment_seam.py`) and the switch-bites suite
(`tests/security/test_host_git_switch_bites.py`), which today passes because the tool, not the door,
reads the switch.

**NOT MEASURED.** (1) Whether any live workspace runs git in host mode — `resolve_git_execution_mode`
(`tools/git_info_tool.py:212-240`) defaults to `"container"` when the catalog entry has no
`execution_mode`, and no workspace config was inspected. (2) Whether a session can actually set
`agent_config["git_environment"]` to a credential-shaped key end to end — the seam's setter was read,
the config plumbing above it was not traced. (3) Whether a session with an unresolvable workspace and
no `root` can reach `_validate_repo_root`'s fail-open branch in practice. (4) No caller in the tree
passes `strip_env=False` (all six grep hits are inside `security/sandboxed_execution.py`), so the
inherit-everything branch is unreachable from the four production sites — but it is public API and
untested as such. (5) Door B's containerized git path was not examined; only the host branch.
(6) Kill-switch exposure of `mcp_server_connect` was established by reading, not by exercising a
workspace with `allow_host_resources: false`.
