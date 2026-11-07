# Ticket - Phase 7 Forensics (post-incident)

**Status:** partial - section 1 is blocked on operator data; sections 2-5 are complete.
**Branch:** `audit/post-incident-forensics`, based on `d28d8faa` (merge of PR #306).
**Scope:** this ticket *documents observed truth only*. No code or config was changed. Where the evidence contradicts existing docs, the docs are named and left alone.

Provenance legend - every claim below carries one of these markers:

- **[verified-agent]** - read directly from source in this session.
- **[worker-reported]** - observed by a worker sub-agent, with raw command output quoted in its transcript; *not* independently re-executed by the authoring agent.
- **[awaiting-operator]** - no agent has access to the data; nothing has been inferred.

---

## 1. Vault / registry analysis - AWAITING OPERATOR OUTPUT

**[awaiting-operator]** - this section cannot be produced by an agent.

`~/.thoughtmachine/` is outside every reachable tree. Two independent reasons:

1. On the host, every file tool resolves paths and rejects anything outside the bound workspace (`_validate_repo_root`, `tools/git_info_tool.py:~547-559`).
2. The vault is not mounted into containers at all - see 2.5.

**Result: NOT COLLECTED. Do not infer orphan counts from the absence of evidence.**

The operator should paste the output of the vault/registry consistency script. The four checks it must answer:

| # | Check | Failure mode it detects |
|---|---|---|
| 1 | Registry row with no session sidecar file | orphaned registry entry |
| 2 | Sidecar file with no registry row | orphaned sidecar / lost session |
| 3 | Active session with a missing sidecar | silent session loss on restart |
| 4 | Workspace directory on disk absent from registry | the `lost EE workspace` |

---

## 2. Container isolation / cross-talk measurements

**Method limitation, stated up front.** The worker had no worker-spawn tool in its own toolset, so a genuine worker-vs-worker isolation test was impossible. It used the strongest available proxy: two independently created containers plus host-side file tools. That proxy is sound for the question asked, because a worker container is created through the same code path as any other container - but it is a proxy, and it is labelled as such.

Second limitation: the container-side measurements below are **[worker-reported]**. The authoring agent did not re-execute them.

### 2.1 Static posture [worker-reported]

Non-root `uid=1000(agent)`; cwd `/workspace`; `.dockerenv` present; container rootfs overlay is read-only.

### 2.2 Mount topology [worker-reported]

From `/proc/mounts` inside the container:

```
/dev/nvme0n1p5 /workspace      ext4 rw,relatime 0 0
/dev/nvme0n1p5 /workspace/.git ext4 ro,relatime 0 0
```

`/workspace` is a **read-write bind mount of the host ext4 partition**; `.git` is re-mounted **read-only** on top of it.

Also observed:

- `/home/agent` is a **tmpfs, 256 MB** - ephemeral, except `/home/agent/.local`, which is bound read-write from host disk.
- `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` are read-only host bind mounts.

### 2.3 Bidirectional host-to-container file visibility [worker-reported]

| Direction | Test | Observed |
|---|---|---|
| host to container | host wrote `TOKEN_HOST_A7F3K9QX` into the workspace; container read it | read back verbatim |
| container to host | container wrote `TOKEN_C`; host read it | `TOKEN_C` plus newline, size 8 |

Both sides see the same inode (`stat`: inode 7733434, dev 66309; `df`: `/dev/nvme0n1p5 368G ... /workspace`). This is a bind mount, **not** a copy - writes are shared, immediately.

### 2.4 Cross-talk between independent containers [worker-reported]

Two independently created containers shared one `/workspace`. A token written host-side was read by a **different** container (whose `hostname` was `3342b4f4418c`), which also saw the first container's other files.

**Finding: there is no per-worker filesystem partitioning.** Any two workers attached to the same workspace can read and modify each other's files, and both are anchored at the same host directory.

### 2.5 Vault reachability from containers [worker-reported]

- `/home/jojo` does not exist inside the container.
- `/home` contains only `agent`.
- `/root` is Permission denied.
- `/home/agent/.thoughtmachine` does not exist.

**Finding: the vault is not mounted into containers.** Network posture: `hostname -I` is empty and `/etc/hosts` is loopback-only.

### 2.6 An asymmetry that deserves its own ticket

A worker's *file tools* execute on the **host**, at `/home/jojo/PycharmProjects/ThoughtMachine-dev`, not inside the container. This was demonstrated accidentally: a `DirectoryCreator` call on `/workspace/...` was refused as `outside workspace /home/jojo/PycharmProjects/ThoughtMachine-dev`.

That refusal proves two things at once:

1. `/workspace` and the host repo path denote the **same directory** (consistent with 2.2).
2. File tools **bypass the container entirely** - so the read-only `.git` overlay of 2.2 does not constrain them.

### 2.7 Cleanup debt

The second test container, `tm-crosstalk-b` (`3342b4f4418c`), was stopped but **could not be removed** - the agent was told *Persistent containers can only be removed by the operator*.

**Operator action required.** The working tree itself is clean (verified: empty `git status`).

---

## 3. Host-port bypass audit: `GitReadTool._exec_host_raw`

Question asked: can `_exec_host_raw` be driven to execute arbitrary commands on the host?

### 3.1 Reachability [verified-agent]

In `tools/git_info_tool.py:853-869`, host execution is entered only under `if not self._use_container_mode():`, and is gated by the kill switch `_host_execution_denied_reason()`, which raises when denied. Container mode has **no silent fallback** - an unavailable or degraded container raises loudly rather than quietly running on the host. Host mode is reachable only when the resource catalog declares `host` or when no registry workspace id exists.

### 3.2 Argument construction [verified-agent]

```python
executor.run(
    ["git"] + identity_args + hardened_args + args,
    cwd=str(repo_root),
    timeout=timeout,
    required_category=...,
    extra_env=merge_git_environment(...),
)
```

**List form only. No shell string is ever assembled,** so no agent-supplied input can ever become `argv[0]`.

### 3.3 `SandboxedExecution.run()` internals [verified-agent]

`security/sandboxed_execution.py` runs five ordered steps:

1. `_check_permission(required_category)` - fail-closed.
2. `_reject_shell_metachars(command)` when `allow_shell` is false.
3. `_build_env`.
4. Audit log - records the command *name* only, never the arguments.
5. `subprocess.run(command, cwd=..., env=..., capture_output=True, text=True, input=...)` - **no `shell` argument is passed at all**, with an explicit *never shell=True* comment.

The metacharacter denylist is `&&`, `||`, `;`, `|`, a backtick, and `$(`; a hit raises `ValueError("Shell metacharacters are not allowed in arguments: ...")`.

### 3.4 Working-directory containment [verified-agent]

`_validate_repo_root` resolves the path and requires `repo_root.relative_to(ws_abs)`, otherwise it raises `ValueError("Git repository ... is outside the workspace ...")`. It is called before and after `_git_repo_root()` re-points the root, and again in `_run_git`. `_normalise_working_dir` maps the container mount `/workspace` back through `_from_container_path`, and rejects any `..` segment; lookalikes such as `/workspaceX` fall through to the normal validator.

### 3.5 Verdict

| Question | Answer |
|---|---|
| Arbitrary host command execution? | **No.** Fixed `git` argv prefix, `shell=True` never used, metacharacters rejected, binary resolved from a pinned PATH. |
| Arbitrary working directory on the host? | **No** - bounded to realpath-contained subdirectories of the bound workspace. |
| Anything left at all? | **Yes, exactly one thing: the `git_environment` seam (section 4).** |

### 3.6 This is already a known exclusion, now with a bounded blast radius [verified-agent]

`docs/host-port.md`, section 9.3 *Named exclusions* (heading near line 333), already records that `GitReadTool._exec_host_raw` runs git through `SandboxedExecution` rather than `host_run`, and therefore *the port's credential strip, kill switch and audit do not govern it*. `_exec_container_raw` is excluded the same way and filed as `hygiene/git-exec-container-raw-not-port-audited`. Section 7 of the same doc restates it.

**What this audit adds:** the exclusion is real, but the blast radius is bounded to what section 3.5 measures. It is not an open RCE.

---

## 4. Open item - the `git_environment` seam can override git's environment hardening

[verified-agent] In `security/sandboxed_execution.py` the hardened environment is built in this order:

1. `_STRIPPED_ENV` - `HOME=/dev/null` and a pinned `PATH`.
2. `_GIT_ENV` - `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`; applied only when `strip_env` is set **and** `command[0] == "git"`.
3. **`env.update(extra_env)`** - and `extra_env` is whatever `merge_git_environment(...)` returned.

So an `extra_env` key silently wins over both `_STRIPPED_ENV` and `_GIT_ENV`.

| Variable | Can the seam override it? |
|---|---|
| `GIT_CONFIG_NOSYSTEM` | **yes** |
| `GIT_CONFIG_GLOBAL` | **yes** |
| `GIT_DIR` | **yes** |
| `GIT_WORK_TREE` | **yes** |
| `GIT_SSH_COMMAND` | **yes** |
| `GIT_PAGER` | no - re-pinned |
| `GIT_CONFIG_SYSTEM` | no - re-pinned |

The two survivors are re-pinned because `GIT_ENV_PINS = {"GIT_PAGER": "cat", "GIT_CONFIG_SYSTEM": "/dev/null"}` is merged **last** inside `merge_git_environment`.

**Why this is not a live exploit:** the seam's source is `agent_config["git_environment"]`, fed in through `set_git_environment`. That is an operator / session-config channel. An agent cannot set it from a tool call. This is a **trust-boundary hygiene defect, not a working bypass.**

**Why it still deserves a ticket:** `GIT_SSH_COMMAND` is not pinned. If the seam ever became reachable from a less-trusted layer, `GIT_SSH_COMMAND` turns into arbitrary command execution the moment any remote-transport verb runs in host mode - precisely the capability section 3 otherwise rules out. Two cheap remediations, either is sufficient:

- add `GIT_SSH_COMMAND`, `GIT_DIR` and `GIT_WORK_TREE` to `GIT_ENV_PINS`; or
- apply `extra_env` **before** `_GIT_ENV`, so the hardening always wins.

---

## 5. Stale documentation - probable origin of the `phantom hook` theory

[verified-agent, via verbatim worker quote] Two docs describe a **279-line `.githooks/pre-commit`** as an active security control:

- `docs/git_resource_execution_audit.md:43-44`
- `docs/resource_execution_contract.md:83`

**That file no longer exists.** The controls that actually exist today are the argv `-c` pins plus a fail-closed *refusal* when repository hooks are configured (`tools/git_info_tool.py:972-987`, the `repository hooks are configured (.githooks/pre-commit)` error).

Consequence: anyone reasoning from these docs concludes a 279-line hook is still doing security work in the git path. It is not. The docs need correcting - **deliberately not done in this ticket**, which is document-only.

Supporting detail, quoted but **not** independently re-executed [worker-reported]:

- argv `-c` pins at `git_info_tool.py:993-1008` - `core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`, `diff.external=`, `core.fsmonitor=`, `filter.clean=`, `filter.smudge=`, `diff.textconv=`, `credential.helper=`.
- `--no-verify` on commits at `git_info_tool.py:1011-1012`.
- `agent/config/resource_catalog.json:12` declares `"execution_mode": "container"`.
- clone-URL scheme allowlisting at `git_write_tool.py:526-535`.

---

## 6. Tooling gaps encountered while doing this work

Recorded because they cost real time and several are silent.

| # | Gap | Impact |
|---|---|---|
| 1 | `git_read show_file` ignores a large `line_start..line_end` range and returns the whole file from line 1, truncating near 10k tokens | three parallel reads returned identical opening lines; exhausted the entire context window. Workaround: never request more than ~60 lines per call. |
| 2 | `git` is absent from `DockerCodeRunner` (exit 127); `host_bash` is banned | no way to run git from inside the container |
| 3 | `/tmp` does not persist between tool calls | scratch files must live in the workspace |
| 4 | `Worker spawn` / `query` require an explicit `worker_name` | silent failure otherwise |
| 5 | `branch_create` does not move HEAD; a `checkout` is required afterwards | easy to write commits to the wrong branch |
| 6 | No fetch verb, and push is blocked by `network:banned` | the agent cannot verify its own work landed. Merges appear in the local DB only because the operator pushes. |
| 7 | `FileSearchTool` under-reports headings (1 of 13 in `status.md`) | structural searches are unreliable |
| 8 | `WorkingDocument create` ignores a supplied `doc_id` | cannot produce a required filename through the tool; the file must be written directly |
| 9 | Workers have no Worker tool | worker-vs-worker isolation cannot be tested directly (see section 2) |
| 10 | Agents cannot remove their own persistent containers | every isolation experiment leaves operator cleanup debt (see 2.7) |
| 11 | A `meta` dict nested inside the `context` argument of `Worker spawn` is **not delivered** to the worker | a 6 KB payload was silently dropped and the worker stopped asking for it. Pass bulk text in the `query` string instead. |
| 12 | At the token ceiling, all tools except `Respond` and the summariser are revoked | work stops mid-verification; see gap 1 for the usual trigger |

One methodological note that is not a tool gap but matters for anyone reading worker output: a worker's *prose* was unreliable in aggregate, while its **verbatim line quotes were accurate every time**. Brief workers to QUOTE, never to CONFIRM.

---

## 7. Operator actions outstanding

1. Paste the vault/registry script output so section 1 can be completed.
2. Remove the leftover container `tm-crosstalk-b` (`3342b4f4418c`).
3. Decide on the `git_environment` seam mitigation in section 4 (pin `GIT_SSH_COMMAND`, or reorder `extra_env`).
4. Correct `docs/git_resource_execution_audit.md:43-44` and `docs/resource_execution_contract.md:83` per section 5.
