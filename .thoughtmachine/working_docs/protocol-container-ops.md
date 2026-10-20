# Protocol — container operations (the 10-step run)

**Status:** runnable protocol, not a test plan.
**Written:** Gen 46, 2026-10-20, against `dev` @ `44a638b8`.
**Companions:** `docs/ARCHITECTURE_MACHINE_MODEL.md` §1 and §3 (what runs
where; the three container types), `design_resource_grid.md` (the five facts
and their open annotations).

## How to read this

Each step names **who acts** (agent tool / operator), **what they do**, and the
**pass criteria** that must be observed before moving on. A step that produces
the right *result* with the wrong *evidence* is a fail: the point of running
this by hand is to look at what the machine reported, not only at what happened
to Docker.

Ground rules:

- **Agent tools never remove a persistent or resource container.** Removal of
  those is operator-only (interim rule, PR #127). A `refused` response is a
  **pass** for step 10, not a bug.
- **Read the tool's own JSON.** The panel is a second opinion, not the record.
- **Record the runtime revision** at the start of the run (`CheckSystem
  query='version'` → `{revision, branch, dirty}`). A run that cannot name the
  runtime that produced it is not evidence.

## Known bugs at the time of writing

Do not re-report these as new; check whether they are still true.

| Symptom | State |
|---|---|
| Panel drift banner false-positive ("older permissions" on a healthy workspace) | **Fixed in #142.** The banner now carries drift *direction*, and honest fix hints. |
| `created_by` lost on **restart** | **Reported, re-verify.** #134/#135 preserved envelope + `created_by` on *resize* and *recreate*; restart was not in that scope. Check step 6. |
| Container badge `filesystem` axis reads the `/workspace` **mount**, not the image rootfs | **Open.** The badge is truthful about the mount it describes and silent about the rest. Do not read it as "the container has no filesystem access". |
| `ContainerStatusTool` echoes the requested id rather than the resolved container id | **Open** (`fact5-container-status-id-echo`). Cross-check against `ContainerListTool` before believing an id. |
| `ttl_remaining_s` is a dead field | **Open.** TTL reclamation is exited-only; a running container is not reaped. |
| Two container counters can disagree; admit-count may include recreated records | **Open**, needs live repro (`bug/two-container-counters-disagree`). |

---

## Step 1 — Inspect before touching

**Who:** agent.
**What:** `ContainerListTool` (what exists for this workspace, with notes),
then `ContainerStatusTool <id>` for the target. Note the reported
`lifecycle_class`, `created_by`, `owner`, `grain_required_for_remove` and
`runtime_commit`.
**Pass:** the list is non-empty if a container exists; the status reports a
lifecycle class; `runtime_commit` matches the revision recorded at the start of
the run. **Fail if** the id echoed back differs from the id you asked about, or
`lifecycle_class` is `null` for a container you can see in Docker — that is the
classifier divergence (`bug/classifier-divergence`, Inv.6), not a display
wobble.

## Step 2 — Create (ephemeral)

**Who:** agent.
**What:** run any containing tool (`DockerCodeRunner`) with a trivial command
and a named `worker_name`/`interpreter` of your choice.
**Pass:** container is created and destroyed within the call; the returned
image is `agent-exec-*`; the command output is non-empty. **Fail if** the call
falls back to host execution silently — fallback must be config-chosen
(`host_fallback` in `resolve_git_execution_mode()`) and behind Gate 1, never an
accident of a failed build (machine model §7).

## Step 3 — Create (persistent)

**Who:** agent.
**What:** `ContainerStartTool` with an explicit `name` and a `note`. The note is
the coordination surface; it is written on creation and — see the caveat —
overwritten on reuse.
**Pass:** response `status` is `created`; the container appears in
`ContainerListTool` with the same note; a second `ContainerStartTool` with the
same name returns `status: reused`, **not** a second container. **Known P1:**
the note is creation-time only (`feat/container-note-on-existing`) — editing a
note on an existing container is not supported and must not be faked.

## Step 4 — Exec

**Who:** agent.
**What:** `ContainerExecTool` with a command that proves both the mount and the
cwd: `pwd && ls -a /workspace | head`.
**Pass:** cwd is `/workspace`; the listing shows the workspace files; exit code
0. **Fail if** the container is gone (`404`) — a stopped container does not
accept exec, which is a lifecycle fact, not a tool bug. Record the response's
`timed_out` flag if a timeout occurs; on timeout the container is killed.

## Step 5 — Stop

**Who:** agent.
**What:** `ContainerStopTool`.
**Pass:** response `status: stopped`; a second `ContainerStopTool` on the same
container is idempotent — `stopped` again, never an error. `missing` is also a
legitimate answer and must not be reported as failure.

## Step 6 — Restart (running) and restart (stopped)

**Who:** agent.
**What:** `ContainerRestartTool` twice — once while running, once after a stop.
**Pass, running:** record is restarted **in place**; the container id is
unchanged; `lifecycle_class` and `created_by` survive. **Pass, stopped:** either
the same record restarts in place, or the refusal is explicit and named — a
silent fresh create that loses `created_by` is a fail (see the known-bugs
table). **Observe:** envelope (`memory`, `cpu`) after restart. #134/#135 fixed
preservation for resize/recreate; restart is the case those waves did not
cover, and this step exists to find out whether it holds.

## Step 7 — Resize

**Who:** agent (or panel).
**What:** resize the persistent container to a different memory limit.
**Pass:** the new limit is live in Docker; the record still carries its envelope
and `created_by`. **Know this:** resize *is* a recreate under the hood. It is no
longer silent (that was #130/#131), but it is still a recreate — expect the
container id to change and do not treat that as loss. **Fail if** a request
below Docker's memory floor is accepted: it must be rejected *before* anything
is destroyed (the P0 of #130).

## Step 8 — Agent attempts to remove a persistent container

**Who:** agent.
**What:** `ContainerRemoveTool` against the persistent container from step 3.
**Pass:** `status: refused` with a reason naming the lifecycle class as
operator-only. The container still exists afterwards. **Fail if** the removal
succeeds, or if the refusal is an unclassified/`error` path — a refusal that
cannot name *why* is not a refusal we can rely on (`bug/classifier-divergence`).

## Step 9 — Operator removes it

**Who:** operator (or an operator-authorized surface), not the agent.
**What:** remove the persistent container through the operator path, then have
the agent call `ContainerListTool` and `ContainerStatusTool`.
**Pass:** removal is audited (`CONTAINER_DESTROY` with an actor); the list no
longer contains it; status reports `missing` with the seven description fields
`null`. **Fail if** the audit record lacks an actor (`bug/actor-not-in-destroy-audit`)
or if the list and status disagree — one of the two is lying.

## Step 10 — Close the run

**Who:** agent.
**What:** re-record `CheckSystem query='version'`; stop any container the run
left running; write down, per step, pass/fail plus the exact tool JSON that
decided it.
**Pass:** the run is reproducible from the written record by someone who was
not there — every claim traceable to a tool response and a runtime revision.
A run that ends with a cleaner Docker state than it started has achieved
nothing if the report cannot say what it observed.
