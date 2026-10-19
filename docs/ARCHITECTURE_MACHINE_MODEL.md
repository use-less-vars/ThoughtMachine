# The Machine Model — what runs where, and what stops it

**Status:** first-read. Tracked, public tier.
**Audience:** anyone joining infra who will touch execution, permissions, or containers.
**Rule:** if this doc and the code disagree, the code is right. Fix this doc (§7).

The doctrine in here is scattered across `machine_model.md`,
`design_layers_and_truth_ownership.md`, and `design_resource_contract.md`. None of
those answer the plain question: *what actually runs where, and what stops it?*
This does. It is deliberately short.

## §1 — What runs where

The most common mistake is assuming "the agent runs on the host, so everything
it does runs on the host." It doesn't. There are four regions of execution.

| Component | Runs on | Is it host execution? |
|---|---|---|
| Server | Host process | N/A — it *is* the host process |
| Agent | Host process | No — it reasons and calls tools; it does not execute |
| Workers | Host process, **as threads in the server** | No |
| File tools | Host process, **in-process Python** | **No** — see below |
| `host_bash` | Host process, a real shell | **Yes** — the only one |
| Git-as-resource | Container | No |
| `DockerCodeRunner` | Container | No |

**Workers are threads.** Not processes, not subprocesses, not containers. One
server process, one shared in-memory registry. This matters: every
cross-workspace worker bug we have had is explained by it, and the fixes are
about making the registry scope correctly, not about process isolation.

**File tools are not host execution.** They are Python functions in the server's
own process, confined to the workspace root by path checks. They spawn nothing.
They reach nothing outside the workspace. Because they are not execution:

- The **kill switch does not apply** to them. With the kill switch off, file
  read/write/edit still work.
- Permission grains like `filesystem: read` bound them through the tool
  boundary, not through an OS sandbox.

**`host_bash` is the one true host-execution grain.** Every other tool either
runs in a container or is bounded in-process. `host_bash` is a real shell on the
operator's machine, as the operator's user. It is the exception the rest of this
document is organised around (§5).

## §2 — The three gates

Every permission decision is the composition of exactly three gates, in this
order. A tool checks the *result*; it must never re-derive the inputs.

### Gate 1 — Kill switch

> *"May anything run on the host at all?"*

Above all other authority. Off ⇒ nothing executes on the host. On ⇒ host
execution is *possible*, not *granted* — Gate 1 is a prerequisite, never a
permission. File tools are unaffected either way (§1). It has never failed in
testing.

### Gate 2 — Workspace ceiling

> *"What is the maximum this workspace allows, per capability?"*

A **cap** on the workspace, not a grant to any session. It can only lower what a
session may do. `effective = min(session_grant, ceiling)`.

### Gate 3 — Session grant

> *"What is this specific session allowed to do?"*

This is where the user's actual choice lives. It can narrow the ceiling. It
cannot widen it.

### Composition

```
kill switch  →  session_grant ∩ workspace_ceiling  →  tool checks effective
```

**Every authority must read from this chain.** A second authority — any code
path that decides permissions from its own state rather than from the composed
result — is a bug, not a shortcut. This is Invariant 1 in
`design_layers_and_truth_ownership.md`. One such second authority was found and
removed on 2026-10-19 (a worker-spawn path holding its own snapshot of the
permission envelope instead of re-reading the chain); see `operator_observations.md`.
Assume there are more. When you find one, remove it; do not "sync" it.

## §3 — The three container types

All containers are built from a workspace Dockerfile. They differ in lifetime,
scope, and who may remove them.

| Type | Lifetime | Scope | Created by | Example |
|---|---|---|---|---|
| **Ephemeral** | Created at request, dies on return | Single call | The tool | `DockerCodeRunner`, `agent-exec-*` |
| **Persistent** | Long-lived, survives turn boundaries | Workspace, shared across sessions | Agent or operator | `emfit-build`, `kicad-build` |
| **Resource** | Long-lived | Workspace | Operator (interim) | `tm-resource-git` |

**Ephemeral** — permissions are correct *by construction*. It is created for one
call with exactly the envelope that call needs, and destroyed after. There is no
window in which it holds stale authority.

**Persistent** — created once, reused, shared by every session in the workspace.
Because it outlives a turn, its permissions cannot be "correct by construction"
in the same way; it must obey the lifecycle contract.

**Resource** — the Dockerfile includes a specialised tool (git being the
canonical one). A resource MUST obey a gate interface, and those gates are
*fine-grained*: `git: read`, `git: write`, `git: write_on_feature_branch` are
three different grains, not one "git" switch. Removal is operator-only pending
the record lifecycle work.

## §4 — Why this architecture

Three alternatives were considered and rejected. Understanding *why* they were
rejected is the fastest way to understand the design.

| Alternative | Why it fails |
|---|---|
| **Agent on host with full permissions** | Unsafe. A single prompt injection is a full host compromise. |
| **Ask the human before each action** | Humans tire. An approval prompt that appears fifty times a day becomes allow-all. Safety that depends on sustained human attention is not safety. |
| **Put the agent in a container** | Loses fine-grained permissions. A container boundary is binary — git is either in or out. You cannot express `git: read` vs `git: write` from inside a container. |

**ThoughtMachine's answer: agent on host, execution in containers, permission at
the tool boundary.**

The agent must be on the host because that is where the workspace, the vault,
and the operator's project live. Execution must be in containers because
containers are the only real blast-radius boundary. Permission must sit at the
tool boundary because that is the only place where fine-grained distinctions
(`read` / `write` / `write_on_feature_branch`) are expressible.

The permission layer is the machine's strongest asset. This architecture exists
to keep it in the one place where it can be precise.

## §5 — `host_bash`: the exception

`host_bash: allow` grants an uncontained shell as the operator's user.

Be precise about what that means:

- On a typical Linux workstation, an unsandboxed shell plus `sudo` plus
  membership of the `docker` group is **root-equivalent**. `docker run -v
  /:/host` defeats every container boundary in this document.
- It **bypasses `network: banned`**. A shell makes its own network calls; the
  grain never sees them.
- It **bypasses `filesystem: read`**. Same reason.
- It bypasses **every** grain, because a shell does not consult the permission
  layer. There is no hook that a `curl` goes through.
- It currently runs in the operator's working directory, not the workspace.
  That is a bug in its own right, filed separately.

**The core promise — "a user can let it run unsupervised without fear" — does
not survive `host_bash: allow` silently.** Every other grain in this document is
compatible with that promise. This one is not, and pretending otherwise is the
failure mode.

What it needs, before it can be considered usable:

1. Its own tier, separate from the ordinary permission grains.
2. An explicit acknowledgment gate — the operator states, per workspace, that
   they understand the blast radius.
3. A red banner wherever the grant is displayed, so it can never look ordinary.

Design pass: `design/host-bash-trust-model`. Scope: V3 Prep.

## §6 — What is *not* the machine

These are the assumptions that produce bad reasoning about safety. Each of them
is a real boundary, but not the boundary you might think.

- **The vault is memory, not a security boundary.** It stores configuration,
  knowledge, and session state. It is not a sandbox and does not enforce
  anything.
- **The knowledge base is working memory, not state.** Losing it loses notes,
  not truth.
- **`network: banned` binds gate-aware tools only.** It does not bind a shell.
- **`filesystem: read` binds gate-aware tools only.** It does not bind a shell.
- **If a tool shells out, the grain restrictions do not apply unless the tool
  itself checks.** This is by design, not an oversight: the permission layer
  governs the tool boundary, and everything below the boundary is the tool's
  business. It is precisely why `host_bash` is its own tier and not a flag on
  some other tool.

## §7 — For the incoming engineer

**Code wins over this doc.** This describes intent. Intent has been wrong
before — the doc is not the specification, the code is. When they disagree: read
the code, then fix this doc so the next person is not misled.

Three habits that pay for themselves:

- **Found a second authority? Remove it** — don't synchronise it (§2).
- **Does a tool shell out? Ask what enforces the grain.** If the answer is "the
  tool checks", verify that it does.
- **Never infer execution class from failure.** No "try container, fall back to
  host" path exists; adding one is a safety regression sold as resilience.

The machine gets more honest one wave at a time. That is the whole method.
