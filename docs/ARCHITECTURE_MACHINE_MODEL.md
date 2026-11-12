# The Machine Model — what runs where, and what stops it

**Status:** first-read. Tracked, public tier.
**Audience:** anyone joining infra who will touch execution, permissions, or containers.
**Rule:** if this doc and the code disagree, the code is right. Fix this doc (§7).

The doctrine in here is scattered across `machine_model.md`, `design_layers_and_truth_ownership.md`,
and `design_resource_contract.md`. None of those answer the plain question: *what actually runs
where, and what stops it?* This does. It is deliberately short.

## §1 — What runs where

The most common mistake is assuming "the agent runs on the host, so everything it does runs on the
host." It doesn't. There are four regions of execution.

| Component | Runs on | Is it host execution? |
|---|---|---|
| Server | Host process | N/A — it *is* the host process |
| Agent | Host process | No — it reasons and calls tools; it does not execute |
| Workers | Host process, **as threads in the server** | No |
| File tools | Host process, **in-process Python** | **No** — see below |
| `host_bash` | Host process, a real shell | **Yes** — the only one |
| Git-as-resource | Container | No |
| `DockerCodeRunner` | Container | No |

**Workers are threads.** Not processes, not subprocesses, not containers
(`WorkerThread(threading.Thread)` in `tools/workspace/worker_thread.py`): one server process, one
shared in-memory registry, keyed `(session, name, iid)` — and with no workspace id in that key, a
name lookup is global by construction. Every cross-workspace worker bug we have had is explained by
this; the fixes are about making the registry scope correctly, not about process isolation.

**File tools are not host execution.** They are Python functions in the server's own process,
confined to the workspace root by path checks. They spawn nothing and reach nothing outside the
workspace. Because they are not execution:

- The **kill switch does not apply** to them. With the kill switch off, file read/write/edit still
  work.
- Grains like `filesystem: read` bound them at the tool boundary, not through an OS sandbox.

**`host_bash` is the one true host-execution grain.** Every other tool either runs in a container or
is bounded in-process. `host_bash` is a real shell on the operator's machine, as the operator's
user. It is the exception the rest of this document is organised around (§5).

## §2 — The three gates

Every permission decision is the composition of exactly three gates, in this order. A tool checks
the *result*; it must never re-derive the inputs.

### Gate 1 — Kill switch

> *"May anything run on the host at all?"*

Above all other authority *within the workspace*. Off ⇒ nothing executes on the host. On ⇒ host
execution is *possible*, not *granted* — Gate 1 is a prerequisite, never a permission. File tools
are unaffected either way (§1). It has never failed in testing (operator observation; no automated
test covers it).

The reason it is per-workspace rather than global, in the operator's words:

> **This is per-workspace by design.** A twitter-bot workspace runs with host execution off —
> nothing it does can reach the host. A hardware-debugging workspace runs with it on — the agent
> needs direct hardware access. Different workspaces, different threat models, different settings.
> Unifying this into a single global switch would make both use cases impossible to serve at once.
>
> The security layer never fails to enforce this. Every host-execution surface checks it before
> acting.

In code it is the per-workspace `allow_host_resources` flag in `<vault>/workspaces/<id>/config.json`,
read by `tools/host_resource_policy.py` (only a JSON `true` passes; any lookup error denies) and
enforced by three host surfaces: `host_bash` (`tools/host_bash_tool.py`), git's host-side path
(`tools/git_info_tool.py`), and CI reads (`tools/ci_read_tool.py`). Scope matters: **per workspace,
not a process-global switch** — a second workspace does not inherit the first one's answer, and there
is no env var, startup flag, app setting or UI toggle for it.

### Gate 2 — Workspace ceiling

> *"What is the maximum this workspace allows, per capability?"*

A **cap** on the workspace, not a grant to any session. It can only lower what a session may do.
`effective = min(session_grant, ceiling)`.

### Gate 3 — Session grant

> *"What is this specific session allowed to do?"*

This is where the user's actual choice lives. It can narrow the ceiling. It cannot widen it.

### Composition

```
kill switch  →  session_grant ∩ workspace_ceiling  →  tool checks effective
```

**Every authority must read from this chain.** A second authority — code that decides permissions
from its own state rather than from the composed result — is a bug, not a shortcut (Invariant 1 in
`design_layers_and_truth_ownership.md`). One such authority was found and removed on 2026-10-19: a
worker-spawn path kept its own snapshot of the permission envelope (from `_restrictive_merge`) and
fed it into its own category check instead of re-reading the chain. The fourth gate is gone; the
merge helper stays, as the spawn-time mirror it is and not the runtime authority. For a worker, the composed
formula is now `effective = min(declared_footprint, live_session − workspace_ceiling)` — commit
`4e96c32c` (#137), *"Wave: worker obeys session grants (fourth gate removed)"*.

**A second authority can also be a second *entry point*.** The removed fourth gate was a copy of the
chain's output. The paginate tool (§9a) was a second *place the chain gets consulted* — and it was
consulted correctly, with the same helper, on the wrong subject: the categories of the top-level
tool, which then constructed and ran a more privileged one. Both are the same bug — the answer was
never stale, the *question* was — and both fixes are shaped the same: the gate must be evaluated on
the tool the call actually names, and the callee's authority must never be selectable by the caller's
payload.

Assume there are more. When you find one, remove it; do not "sync" it.

### Where the gates live

All three gates are workspace-scoped; only Gate 3 is additionally per session. Gate 1 is above all
other authority *within* a workspace, not above the process: there is no server-wide kill switch, and
one workspace's answer is never another's. Gate 2 is the same kind of object one level down — a
workspace-scoped authority over *capability* instead of over *reachability*. Both belong to the
workspace and are set by whoever owns it; Gate 3 is where a user's own choice lives.

Reading the three as one thing — "the permissions system" — is what makes people try to unify them.
They are three authorities at three scopes, composed in the order above.

## §3 — The three container types

All containers are built from a workspace Dockerfile. They differ in lifetime, scope, and who may
remove them.

| Type | Lifetime | Scope | Created by | Name |
|---|---|---|---|---|
| **Ephemeral** | Created at request, dies on return | Single call | The tool | `agent-exec-ephemeral-<ws>-<session>-<env>` |
| **Persistent** | Long-lived, survives turns | Workspace, shared across sessions | Agent or operator | `agent-exec-persistent-<ws>-<env>` — on the operator's workstation, e.g. `emfit-build`, `kicad-build` (operator naming; not created by this repo's code) |
| **Resource** | Long-lived | Workspace | Operator (interim) | `tm-resource-git` |

**Ephemeral** — correct *by construction*: created for one call with exactly the envelope that call
needs, then destroyed. There is no window in which it holds stale authority.

**Persistent** — created once, reused, shared by every session in the workspace. Outliving a turn,
it cannot be "correct by construction" the same way; it must obey the lifecycle contract.

**Resource** — the image bundles a specialised tool (git is the canonical one). A resource MUST obey
a gate interface, and those gates are *fine-grained*: `git` takes five values — `banned`, `ask`,
`read`, `write`, `write_on_feature_branch` — so `read`, `write` and `write_on_feature_branch` are
three different levels, not one "git" switch. `write_on_feature_branch` permits commits only on
non-protected branches. Two vocabularies meet here and must not be confused: the *level* carried in
`session_permissions`, `effective_permissions` and a workspace ceiling is `write_on_feature_branch`
(`security/resource_catalog.py:117,147,170,192`; `thoughtmachine/security.py:142`; offered in the UI
and in `agent/config/resource_catalog.json`), while the *category string* the outer gate checks is
`git:write` (`security/gate_helpers.py::_value_satisfies` ranks `write_on_feature_branch` at the
write tier; the branch restriction is enforced inside `git_write_tool.py`). **Configure the level;
expect the category.** Removal is operator-only pending the record lifecycle work.

## §4 — Why this architecture

Three alternatives were considered and rejected. Knowing *why* is the fastest way in.

| Alternative | Why it fails |
|---|---|
| **Agent on host with full permissions** | Unsafe. A single prompt injection is a full host compromise. |
| **Ask the human before each action** | Humans tire. An approval prompt that appears fifty times a day becomes allow-all. Safety that depends on sustained human attention is not safety. |
| **Put the agent in a container** | Loses fine-grained permissions. A container boundary is binary — git is either in or out. You cannot express `git: read` vs `git: write` from inside a container. |

**ThoughtMachine's answer: agent on host, execution in containers, permission at the tool boundary.**

The agent must be on the host because that is where the workspace, the vault, and the operator's
project live. Execution must be in containers because containers are the only real blast-radius
boundary. Permission must sit at the tool boundary because that is the only place where fine-grained
distinctions (`read` / `write` / `write_on_feature_branch`) are expressible. The permission layer is
the machine's strongest asset; this architecture exists to keep it where it can be precise.

## §5 — `host_bash`: the exception

`host_bash: allow` grants an uncontained shell as the operator's user. It needs *both* Gate 1 and a
`host_bash` grain of `ask`/`allow` on the session; the default is banned, and a worker context never
gets `ask` — ask means a human, and there is none.

Be precise about what the grant means:

- On a typical Linux workstation, an unsandboxed shell plus `sudo` plus membership of the `docker`
  group is **root-equivalent**. `docker run -v /:/host` defeats every container boundary here.
- It **bypasses `network: banned`**. A shell makes its own network calls; the grain never sees them.
- It **bypasses `filesystem: read`**. Same reason.
- It bypasses **every** grain: a shell does not consult the permission layer. There is no hook that
  a `curl` goes through.
- It does not run in the workspace. The execute path is `subprocess.run(cmd, shell=True, ...)` with
  **no `cwd=`**, so the shell inherits the *server process's* working directory — whatever directory
  the server was started from, which may be nowhere near the workspace.

**The core promise — "a user can let it run unsupervised without fear" — does not survive
`host_bash: allow` silently.** Every other grain in this document is compatible with that promise.
This one is not, and pretending otherwise is the failure mode.

What it needs before it can be considered usable:

1. Its own tier, separate from the ordinary permission grains.
2. An explicit acknowledgment gate — the operator states, per workspace, that they know the risk.
3. A red banner wherever the grant is displayed, so it can never look ordinary.

Design pass: `design/host-bash-trust-model` (V3 Prep; no file on disk yet — this paragraph is the
current statement of it).

## §6 — What is *not* the machine

These assumptions produce bad reasoning about safety. Each names a real boundary, just not the
boundary you might think.

- **The vault is memory, not a security boundary.** It stores configuration, knowledge, and session
  state. It is not a sandbox and enforces nothing.
- **The knowledge base is working memory, not state.** Losing it loses notes, not truth.
- **`network: banned` and `filesystem: read` bind gate-aware tools only.** They do not bind a shell.
- **If a tool shells out, the grain restrictions do not apply unless the tool itself checks.** By
  design, not oversight: the permission layer governs the tool boundary, and everything below it is
  the tool's business. It is precisely why `host_bash` is its own tier rather than a flag on some
  other tool.

## §7 — Agent Runtime vs. Bootstrapping

The machine has one home and one front door, and confusing them is the most common way a session
goes wrong.

1. **The Workspace Image is the runtime.** The image built for this workspace — tagged
   `agent-executor-<sha256(workspace_path)[:16]>`, so two workspaces never share one tag like
   `agent-executor:latest` — is where work happens: every tool call that executes code lands inside a
   container built from it. Treat it as your home:
   interpreters, linters, CLI tools and the test suite live there. If a command works in the image
   and nowhere else, that is correct, not a limitation to route around.
2. **The Vault Dockerfile is the blueprint.** The image is not configured by hand. It is declared in
   the vault-managed Dockerfile and built from it, and when the runtime lacks a tool the fix is to
   edit that Dockerfile and rebuild — not to install the tool on the host. A host install changes a
   machine nobody can reproduce; a Dockerfile edit changes the image everyone gets.
3. **Host Bash is bootstrapping, and only bootstrapping.** The `host_bash` tier (§5) exists for the
   one job that cannot happen inside the container: building the container. Use it to run
   `docker build`, then turn it off and go back inside. It is not a second runtime, not a
   convenience shell, and never the place to "just quickly" install something. Correction: you do not
   need `host_bash` even for this — `ContainerBuildTool` builds the workspace image from the vault
   Dockerfile via `ContainerManager.build_image()` under the container grant; `host_bash` is only
   the escape hatch for what that tool cannot express.

The test is simple: **if the work can happen in the image, it belongs in the image.** Host Bash is
how you make the image better; it is not where you live.

## §8 — For the incoming engineer

**Code wins over this doc.** This describes intent, and intent has been wrong before — the doc is
not the specification, the code is. When they disagree: read the code, then fix this doc so the next
person is not misled.

Three habits that pay for themselves:

- **Found a second authority? Remove it** — don't synchronise it (§2).
- **Does a tool shell out? Ask what enforces the grain.** If the answer is "the tool checks", verify
  that it does.
- **Name the path before you believe a behaviour claim.** Git in this system has three commit
  paths with three behaviours — container (workspace `.githooks` fires), host (hooks neutralized by
  construction), bare shell (the worktree gitdir's hook resolves; `/workspace/.git` is a gitfile into
  the host worktree). "The hook runs" and "the hook is dormant" are both true statements about this
  repo, about different paths (§9b).
- **Never infer execution class from failure.** There is no *silent* container→host fallback: a
  container-path failure raises a loud error (`docs/container_p0_bug4_git_fallback.md`). Host mode
  exists, but it is chosen deliberately by config (`host_fallback` in
  `resolve_git_execution_mode()`) and still sits behind Gate 1. Turning a container failure into a
  quiet host run is a safety regression sold as resilience.

## §9 — History: two bypasses that changed the design

Two incidents, kept because the *shape* of each fix is reusable. Neither was solved by tightening a
check; both ended as a rule about where authority may live. Both were found by asking one question,
the second habit in §8: **what actually enforces this?**

### a) The paginate laundering hop (removed Gen 48)

The gate is evaluated on the **top-level tool's declared categories**
(`agent/core/tool_executor.py:419` → `:468-469`). A helper tool existed to paginate large results
and declared only `filesystem: read`. It also accepted a tool *name* and instantiated that class
from `tools.TOOL_CLASSES`, then ran it. So a stock filesystem-only profile could execute
`ContainerRemoveTool` and `HostBashTool`: the gate asked about the tool the model named, and a
different tool did the work. Because it forwarded a context dict that omitted `workspace_id`, the
inner call could also run under a caller-chosen workspace identity — a containment defect *and* an
attribution defect, and reachable from workers.

Filed 2026-10-23 as
`ticket-bug-paginate-tool-bypasses-required-categories.md`. Three attempts, in order:

1. `8aa17e0d` — contain the context fields (`_CONTEXT_FIELDS`). Failed as a *class* of fix, not as
   code: the list was written as a list of **paths**, so an identity field that is not a path
   (`workspace_id`) was never in it. A containment list is not an identity list.
2. `0c125856` — re-gate inside `execute()`: call `get_required_categories(params)` on the target
   class and run it back through the same `check_required_categories` the executor uses, before
   instantiation, fail-closed if the gate cannot be imported. Plus `_NEVER_FORWARD =
   ('workspace_id',)` and `_NEVER_PAGINATE`, a structural blocklist — needed because several of the
   blocked tools declare *no* categories at all, so no categories gate can see them. Residuals were
   recorded honestly: the re-gate validated the already-capped `effective` dict and so applied no
   ceiling of its own ("Defect 1 with a seatbelt"), and dropping `workspace_id` gives the inner tool
   `None` — which fails closed in some tools, fabricates `"default"` in others, and is silently
   unattributed in one.
3. `4a4bd692` — delete it. 223 lines gone; 18 tests retired with it, not silently. Verbatim
   rationale: *"a safe wrapper is still a wrapper and the class survives wherever the pattern is
   copied."* The registry entry is now a pointer comment naming the class rule
   (`hygiene/no-tool-wrapping`).

**What the design now says.** Authority is evaluated on the tool the call names, and a tool's
declared categories must describe the maximum reach of that call. A wrapper is therefore not a tool
with narrow permissions — it is a permission *laundering* device, and the safe version of it does not
exist. The residual defects in step 2 are the argument for step 3: every one of them is a case where
the wrapper had to *guess* an identity or a ceiling it had no standing to know.

### b) Git hooks: three commit paths, three behaviours

Same code, same repository, three answers to "do hooks run?" — because the answer is a property of
the path, not of the repo:

| Path | What happens | Where |
|---|---|---|
| Container | `-c core.hooksPath=/workspace/.githooks` (absolute, container-mapped); **no** `--no-verify`. The container is the boundary, so hooks are confined to the workspace dir and allowed to run. | `tools/git_info_tool.py` (container commit path) |
| Host | Hooks neutralized by construction on *every* invocation: `core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`, `diff.external=`, `core.fsmonitor=`, `filter.clean=`, `filter.smudge=`; `--no-verify` on commit. | `tools/git_info_tool.py` (host argv builder) |
| Bare shell | Whatever `core.hooksPath` resolves to where you stand. `/workspace/.git` is an ~84-byte gitfile pointing into the host worktree gitdir, so the hook that fires is the host's. | not a tool path — a human or a stray subprocess |

That third row is what makes the table load-bearing: `.githooks/pre-commit` was **deleted** in
`85035605` because `core.hooksPath` read as *unset* from a bare shell — judged dormant — while the
container path sets it explicitly and the hook was live. The lesson is one sentence and it is the
reason this section exists: **one shell's default is not a fact about the machine.**

The design change that came out of it: host mode no longer *silently* bypasses. When
`<workspace>/.githooks/pre-commit` exists and the execution mode is host, the commit is **refused** —
"refusing commit: repository hooks are configured (.githooks/pre-commit) but host execution mode
neutralizes them … Use container mode, or remove the configured hooks to accept the bypass." The rule
behind it (design §E3): *fail closed only when hooks are configured-but-unenforceable*; silence in the
face of unknowability is the bug, not the fix. The vault-hook mechanism is gone with it — hooks are
now either the workspace-local `.githooks` dir (container) or fully neutralized (host), nothing else
(`tests/security/test_git_hardening.py`, which also asserts the container argv exactly and that
`--no-verify` is absent from it).

One operational corollary, because it is the trap: a tool commit can **time out while the hook is
still running** — paths staged, HEAD not advanced, hook killed. A timed-out commit is therefore weak
evidence that the suite passed, and never a reason to reach for `--no-verify`.

The machine gets more honest one wave at a time. That is the whole method.
