# ThoughtMachine — Architecture Baseline

visibility: public
last updated: 2026-09-24

Purpose: the doc a new Main reads first. Carries shape, not state.
State lives in the state doc. If this doc is wrong, the next Main
operates with a wrong picture for a full session.

Tagging: every claim carries a source tag. [operator-confirmed],
[verified], [reported], [handoff-inherited], [ruling], [unknown].
Handoff prose is a lead, not a fact.

---

## 1. What ThoughtMachine is

A workspace-centric capability management platform. An operator grants
an AI agent controlled access to computer capabilities. Safety is
enforced structurally in code at chokepoints, not by prompt. The
operator's formulation: a user can put work in, walk away, and collect
good results without fear that the agent will reach something it was
not granted. The security layer is the product. Everything else is
surface that makes it legible.

## 2. The three container classes

| Class      | Lifecycle constant      | Scope     | Purpose                                                                 |
|------------|-------------------------|-----------|-------------------------------------------------------------------------|
| Ephemeral  | LIFECYCLE_EPHEMERAL     | Session   | One-shot scratch. DockerCodeRunner. Run a command, discard.             |
| Persistent | LIFECYCLE_PERSISTENT    | Workspace | The workspace's "office computers." Durable, user-configured.           |
| Resource   | LIFECYCLE_RESOURCE      | Workspace | Built on the class-2 image by default; may use an arbitrary Dockerfile; may run on host. Permission-gated. Git is the first. |

LIFECYCLE_SERVICE exists in the enum — production-unused. [reported by Infra]
LIFECYCLE_* constants in thoughtmachine/container_record/models.py.

Persistent containers: 2-6 per workspace, max_containers default 6,
overridable. Agents leave sticky notes ("currently training xy").

Resource containers: shared across all sessions in the workspace.
That is what makes the shared chip meaningful.

Resource scope is not a hard constraint that resources must sit on
top of the workspace runtime. If a resource does not need the runtime
image, it can use an arbitrary Dockerfile, or run on host if the
resource requires host execution. [operator-confirmed]

## 3. The vocabulary trap

- UI "runtime" = LIFECYCLE_PERSISTENT. There is no "runtime" lifecycle
  class. web_ui/backend/server.py says this verbatim.
- UI "resource" = LIFECYCLE_RESOURCE.
- "Office computers" / "real computers" / "workspace machines" =
  persistent.
- "Scratch" = ephemeral.
- "Session containers" — ambiguous. Ask before using.

Name the class, not the display word.

## 4. The layering

A resource container takes the class-2 image and installs a resource on
top. Git runs on the same environment the tests use — so git hooks run
tests in the environment they were written in, which means a hook that
runs tests gives the same result as the agent running tests manually.
The permission gate keeps git out of class 2: the agent cannot pip
install git on a persistent container and bypass the gate, because git
access goes through the gated resource container. The layering is the
clever part.

The layering is the intended usage, not a constraint on all resources.
A resource that does not need the workspace runtime can supply its own
Dockerfile, or run on host if host execution is required.

## 5. What lives where

- Workers — Python daemon threads inside the host process
  (tools/workspace/worker_thread.py::WorkerThread). Not containers.
  Cognitive work: think, call tools, spawn sub-tasks. Session-scoped.
- Containers — Docker. Execution work. Classes 2 and 3 workspace-
  scoped; class 1 session-scoped.
- File tools — bounded in-process Python file manipulation. Run in the
  host process. Touch files only through validate_path(). Gated by
  filesystem permissions, not the kill switch. Not a lifecycle class;
  a separate execution mode.

The container-vs-host distinction applies to subprocesses. In-process
Python calls are not containerized; there is no reason to. File tools
are the canonical case. [operator-confirmed]

machine_model.md names container / host / bounded-in-process. The
authoritative mapping to LIFECYCLE_* is confirmed as: container =
Docker execution classes (all three LIFECYCLE_*), host = the host
process generally, bounded-in-process = file tools and similar
in-process calls.

## 6. The permission gate

security/security_gate.py. Six categories: container, network,
filesystem, git, mcp, host_bash. Per-call. get_effective_permissions
merges session and workspace, applies ceiling. check_required_categories
loops the tool's declared categories; ASK denies in worker context.

Filesystem and network reach Docker config (network -> network_mode,
filesystem -> workspace_mode) via resolve_container_config. Everything
else is per-call only.

Exception: tools/host_bash_tool.py declares required_categories: [] and
self-gates (5 layers, host subprocess.run(shell=True), fail-closed).
Confirmed this session.

Pattern to watch: any tool with empty required_categories that does not
self-gate is an ungated path. The exhaustive list is not yet
established. Noted for the MCP sprint and future tool registration.

## 7. The container record system

thoughtmachine/container_record/. Every long-lived container carries a
durable record so the system can detect drift, reap orphans, and
surface state. The record is the home for intent_snapshot (network_mode,
workspace_mode, mem_limit, cpu_quota, image_ref, hardening,
restart_policy, user) and, since D.1, permissions.

- Record model in models.py. SCHEMA_VERSION_CURRENT = 5.
- RECORD_LABEL_KEY = "thoughtmachine.container_id" in api.py — the one
  Docker label the record system adds.
- record_creation(...) in hook.py wraps create; writes a "creating"
  stub before containers.run; record.attach(container) binds docker_id.
- storage.py writes atomic JSON to
  <vault>/workspaces/<ws>/containers/<id>.json.
- drift.py::classify_drift compares intent_snapshot against live
  container attrs. Axes: identity, policy (network_mode,
  workspace_mode), runtime, image, hardening, restart_policy, and
  (D.1) permission (CLASS_PERMISSION).
- emit_drift_findings dedupes by signature.
- state is a free-form string, not an enum. STATE_CREATING is the only
  constant today. Adding STATE_RUNNING as a constant — not a magic
  string — is part of the record-state-advance work.
- Schema-4 read does not auto-upgrade. Record.from_dict reads version
  verbatim. Upgrade requires an explicit upgrade_record_schema call.
  Old records stay old until upgraded.

If the record lies, the panel lies.

Architectural fragility (report-only, safe today): record_creation is
opened by five create paths; a second writer family (attach_container
reuse/heal, append_event from drift and permission sites,
write_record_file) writes to the same store. record_lock serializes
writes but does not coordinate. The writers are individually correct
today; the shape is fragile by design.

## 8. The chokepoints

- security/security_gate.py — per-call gate, get_effective_permissions,
  check_required_categories, resolve_container_config
- validate_path() — file tools
- Container create paths — hook.record_creation,
  _create_resource_container, ContainerManager._fresh_start,
  _heal_missing
- Git argv construction — hardened_args on the host path. Known
  asymmetry with the container path, named as a held item.
- No docker socket. No vault mount. Resource containers: cap_drop=ALL,
  no-new-privileges, read-only rootfs, network none unless granted.
- RECORD_LABEL_KEY — the one Docker label the record system adds.

The habit: every brief cites by symbol, never line number. Every
premise is verified before RED. Handoff-inherited premises are wrong
more often than right.

## 9. The trust hierarchy

Operator-confirmed > running code > code read today > git history >
tests > CI > Main's rulings > KB docs > handoff prose > memory.

Operator-confirmed binds even if code disagrees. The code is wrong, and
there is a named item to fix it. Push back when the operator is wrong;
that is the job.

Cite the file, not the framing. Where KB and git disagree, git wins.
Where docs and code disagree, code wins.

## 10. Reading list

1. This doc.
2. machine_model.md — three classes, permission/execution model.
3. working_agreements.md — the rules. Pre-existing artifact; do not
   tidy.
4. v3_vision_full.md — the six threads.
5. structural_debt_register.md — M1-M8.
6. .thoughtmachine/knowledge/project/d1_permission_lifecycle_recon.md —
   D.1 input.
7. personal/lessons_learned.md — section-read: premise-failure entries,
   container-vs-host gap.
8. The Gen 30 journey document — the arc map.
9. C.1 and C.2 strand close reports in reports/.

Where things live: docs/ product docs (public-safe). .thoughtmachine/
knowledge/ working docs (private). reports/ dated log, gitignored.

Pre-V3 checklist: (1) set up new sysprompts; (2) triage each KB doc —
public-safe into docs/, private into the vault, agent-accessibility
configured per doc.
