# Container Aggregate Memory Envelope — Design Ticket

**Status:** Design ticket (nothing implemented). No code shipped, no behaviour changed: this
document only records an incident, states what the tree does today, and prices four options.
**Scope:** The **aggregate memory Envelope** — the missing bound on the *sum* of container
memory over a scope (a workspace, a host). It does not change the per-container memory limit,
the container count cap, the container record, or any hardening recipe.
**Driver:** operator incident report (host freeze under 9 × 1 GB containers). No implementer
assigned; this ticket exists so the gap is not re-derived from scratch.
**Related docs:** `docs/container_subsystem_target_architecture.md` (canonical — "the target
arch"), `docs/container_subsystem_audit.md` (§3 Limits, §8 Resource allocation),
`docs/container_record_design.md` (the `intent_snapshot.mem_limit` field that would *carry* a
declared limit if one is ever needed at record level, `docs/container_record_design.md:63`).
**Line references:** every `file:line` anchor below was verified by direct read of the current
tree (this session). They are *current-truth anchors* — they drift as the tree moves, and two
claims in the incident record are outside the tree and are flagged *unverified — needs runtime
check* where they are load-bearing.
**Terminology:** "memory envelope" is **not an existing term in this tree** and needs defining
before it is used. In this codebase "envelope" already means the *isolation* envelope
(`network_mode`, `workspace_mode`) — `infra/container_lifecycle.py:9-12`, `:282-311`;
`infra/container_manager.py:1466-1534` (`restored_envelope`), `:2143-2276`;
`infra/containers/executor.py:273`. "ceiling" means the per-workspace *permission* ceiling
(`agent/config/resource_catalog.py:86`, `:243`). "hardening envelope" means the
`cap_drop`/`security_opt`/`read_only`/`user` recipe (`infra/container_create.py:65`, `:259`,
`:321`). This ticket uses **Aggregate Memory Envelope** = *the maximum total container memory
that may be committed at one instant over a scope, enforced at admission.* It appears nowhere
in the tree today.

---

**Table of Contents**

1. [Incident Record](#1-incident-record)
2. [What Exists Today](#2-what-exists-today)
3. [The Gap](#3-the-gap)
4. [Options](#4-options)
5. [Recommendation](#5-recommendation)
6. [Open Questions](#6-open-questions)
7. [Not In Scope](#7-not-in-scope)
8. [Revision Log](#8-revision-log)

---

## 1. Incident Record

The incident, as reported, is reproduced verbatim — it is the only measurement this ticket has:

> 9 containers x 1 GB held at 92% swap -> host freeze

> 8 kernel OOM kills, all attributed to CONSTRAINT_MEMCG

> per-container Envelope is owned and enforced; the aggregate Envelope does not exist

Three readings matter, and they are the whole argument for this ticket:

- **The per-container limit did its job.** Every one of the nine containers was inside its
  declared limit. Nothing in the tree was misconfigured. This was not a limit that was ignored
  or a limit that leaked; it was a limit that nine times over means nothing.
- **The host, not the container, was the casualty.** The freeze happened at the host level
  (92% swap held). The kernel's OOM killer then fired eight times — one victim at a time —
  which is the kernel cleaning up *after* the host was already gone.
- **`CONSTRAINT_MEMCG` is an attribution label, not a tree symbol.** It does not occur anywhere
  in this repository (grep: no match outside `working_docs/` prose). It names the *axis* the
  kernel attributed the kills to — the memory cgroup limit that each container was living
  inside. Do not look for a constant by that name; there is none.

**Finding:** the reproduction is fully consistent with the code read in §2 — the tree can hold
nine 1 GB containers on any host of any size, because nothing in it ever compares a sum to a
capacity. *The kernel's attribution of eight kills to the memcg constraint is operator-reported
and is not reproducible from the tree — unverified, needs runtime check.*

**Disposition:** accepted as a design gap, not a bug in a line. §2.4 names the exact spot where
the missing check would sit.

---

## 2. What Exists Today

Four separate facts, in the order that matters: what a single container may take, how many
containers may exist, what the gate actually reads, and what happens when the two multiply past
the host.

### 2.1 Per-container memory limit — owned, enforced, and per-container only

Every create path declares a memory limit, and the declared value becomes a real cgroup v2
`memory.max` (audit §8 confirms live enforcement, `docs/container_subsystem_audit.md:167-191`).
The defaults are not uniform — this is the tree as it stands:

| Create path | Default `mem_limit` | Anchor |
|---|---|---|
| `ContainerManager` | `"1g"` (`cpu_quota=100000`) | `infra/container_manager.py:780-791`, stored `:802`, applied `:2110`, `:2255` |
| Shared default constant | `DEFAULT_MEM_LIMIT = "1g"` | `agent/config/defaults.py:232` (`:233` CPU) |
| `create_hardened_container` / `ContainerSpec` | `DEFAULT_MEM_LIMIT` (`"1g"`) | `infra/container_create.py:286`, applied `:425-441` |
| Registry `ContainerProfile` | `DEFAULT_MEM_LIMIT` (`"1g"`) | `infra/container_registry.py:147` |
| Resource (git) container | `"512m"` | `infra/resource_container_manager.py:929`; `RESOURCE_MEM_LIMIT` / `RESOURCE_CPU_QUOTA` `agent/config/defaults.py:132-133`; passed at `infra/container_registry.py:693` |
| `DockerExecutor` | `"1g"` | `infra/containers/executor.py:650`, `:661`, applied `:1002`, `:1059` |
| `DockerCodeRunner` | `"512m"` (`cpu_quota=50000`) | `tools/docker_code_runner.py:165-172`, applied `:400` |
| `ContainerStartTool` field | `DEFAULT_MEM_LIMIT` (`"1g"`) | `tools/container_control.py:233-236`; manager built at `:160` |

> *Forward note (v0.1.1): as-authored; per-container default now 512m (`fix/container-default-mem-limit`), literals use `DEFAULT_MEM_LIMIT`. The percentages/argument below are unaffected — they concern the unbounded aggregate, not per-container size.*

Workspace containers additionally run with `oom_score_adj=1000`
(`infra/container_manager.py:2112`, `:2257`) — they volunteer as the kernel's preferred victims,
which protects the host in principle but, as the incident shows, not before swap is exhausted.

**Finding:** the limit is genuinely owned — it is a defaulted field in every spec, threaded into
`client.containers.run(... mem_limit=...)` at `infra/container_create.py:425-441`, and enforced
by the kernel. What does not exist is any code path that *adds two of them together*.

### 2.2 Per-workspace container COUNT cap — the only aggregate bound in the tree

`DEFAULT_MAX_CONTAINERS = 6` (`agent/config/defaults.py:137`, with the comment at `:136`
pointing at `ContainerManager.max_containers`); the per-worker budget is unified with it at
`agent/config/defaults.py:44-46` (`WORKER_DEFAULT_MAX_CONTAINERS = 6`).

Resolution order is session config → workspace config → default, clamped to ≥ 1:
`infra/container_manager.py:882-898` (`_get_max_containers`), with workspace value
`self.workspace_config.get("max_containers", DEFAULT_MAX_CONTAINERS)` at `:820-825`,
per-workspace defaults `{"max_containers": ..., "disk_quota_mb": 4096}` at `:836`, and the
registry's own copy at `infra/container_registry.py:940-955`.

Enforcement exists twice, with the same shape and different wording:

- manager path: `infra/container_manager.py:1935-1945` — refuses with
  `"Workspace container limit reached (N/limit consuming container(s)); remove an existing
  container ... to free a slot"`;
- registry path: `infra/container_registry.py:533-557` — an atomic read-and-reserve slot check
  (`"{current}/{max_containers} workspace container slot(s) already in use for workspace ..."`,
  `:549-557`), the one place in the tree where the count check is *not* racy.

Both are class-filtered: non-consuming lifecycle classes occupy no slot
(`security/admission_gate.py:107-111`, `:491`).

**Finding:** the only aggregate resource bound in this tree is a **count**. It never reads a
`mem_limit`. At the default, six workspace containers at 1 GB each is a 6 GB commitment that the
code admits without asking whether the host has 6 GB.

> **Stale cross-reference:** `docs/container_subsystem_audit.md:65-83` still reports
> `DEFAULT_MAX_CONTAINERS = 4` and states "the value 4 is the only one found; 6 appears
> nowhere". The current tree says **6** (`agent/config/defaults.py:137`). The audit is stale on
> this point; correcting it is a separate docs patch (§7).

### 2.3 The admission gate — what it considers, and what it never looks at

`security/admission_gate.py` is a pure policy layer that never raises (`:1-8`), delegating the
derived configuration to `security/security_gate.resolve_container_config` (`:23-27`). Its steps
in order:

| Step | What it checks | Anchor |
|---|---|---|
| 1 | `container_type` + `workspace_id` well-formed | `:419` |
| 2 | image allowlist | `:431` |
| 3 | host disk headroom — two-tier WARN/DENY | `:444` (policy `agent/config/defaults.py:147-155`) |
| 4 | daemon reachability | `:456` |
| 5 | resolve derived config from permissions | `:464` |
| 6 | per-workspace **container count** (class-filtered, fails CLOSED) | `:483-506` |
| 7 | derived narrowing (`network_mode`, `read_only` only) | `:508-519` |

Three observations, all of them the same observation:

- The request **already carries the number**: `AdmissionRequest` has `mem_limit`, `cpu_quota`,
  `oom_score_adj` fields (`security/admission_gate.py:135-146`). No step reads them.
- The derived-narrowing dict literally sets them to `None` —
  `{"mem_limit": None, "cpu_quota": None, "oom_score_adj": None}` at `:511-513` — and the module
  docstring is explicit that this is structural, not an oversight: "*ContainerConfig exposes no
  memory / CPU / OOM fields, so the derived policy can only constrain network_mode and read_only
  here; the memory / CPU / OOM axes remain unconstrained by the resolver*" (`:31-33`).
- The **host-observation seam exists and has no memory probe**. `Probes` is a four-method
  protocol — `disk_usage(path)`, `docker_reachable()`, `workspace_container_count(workspace_id)`,
  `now()` (`:211-217`). Disk, daemon, count. Nothing that reports memory, headroom, or pressure.

There are no admission reason codes for capacity either — the closed set at `:93-105` covers
`unknown_container_type`, `image_not_allowed`, `insufficient_disk`, `daemon_unreachable`,
`container_limit_exceeded`, `narrowed_to_policy`, `internal_error` and friends, and none of them
is about memory. Any option below has to add one.

**Finding:** the gate validates *existence* (may this container type exist, in this workspace,
from this image, on a host with disk and a live daemon) and never *capacity*. It is given each
container's declared memory and asks it no question. It has no host memory probe and no code
anywhere that reads `/proc/meminfo` or swap state for a decision — the only `psutil.virtual_memory`
use in the app is logging (`agent/logging/__init__.py:601-614`).

### 2.4 What happens today when the sum exceeds host memory

Nothing. The creates are accepted, in order, until Docker says no — and Docker has no opinion
about the sum either.

The failing path, named precisely:

1. `ContainerManager.start` passes its own limit through —
   `mem_limit=mem_limit if mem_limit is not None else self.mem_limit`
   (`infra/container_manager.py:2110`), then `mem_limit=self.mem_limit` on the hardened path
   (`:2255`).
2. Admission runs and **passes** — `admit(_admission_request, probes=ClientProbes(self.client))`
   (`infra/container_manager.py:2267`), or `admit(request, ...)` → `AdmissionDenied` on the
   shared create path (`infra/container_create.py:407-409`) and the registry path
   (`infra/container_registry.py:303-305`). It passes because the only aggregate test is the
   count (§2.3 step 6).
3. The container is created — `client.containers.run(... mem_limit=spec.mem_limit,
   cpu_quota=spec.cpu_quota, oom_score_adj=..., tmpfs=..., mounts=...)`
   (`infra/container_create.py:425-441`).

**The gap is the absence of a check between step 2 (`:407`) and step 3 (`:425`).** There is no
line to fix in `:425-441`; the create is correct. On the manager path the same absence sits
between `:2267` and the `containers.run` that follows it.

What the tree does instead, when the bill arrives:

- The kernel arbitrates. Containers hit `memory.max`, reclaim runs, swap fills; once swap is at
  92% the host thrashes and the OOM killer fires one container at a time — the incident's eight
  kills. Nothing in this app is in that loop.
- **Swap is unbounded per container.** No `memswap_limit` / `memory_swap` appears anywhere in
  `infra/` (grep: no matches), so a container with `mem_limit=1g` may swap past its limit
  without a per-container cap. This is the second half of "*held at 92% swap*", and no option
  in §4 catches it unless swap is also capped. *(Fixed off this branch:*
  `infra/container_create.py:439-440` *on* `fix/container-default-mem-limit`, *commit*
  `fed55868` *—* `memswap_limit = mem_limit`*, derived from the limit, deliberately not a stored
  spec/record field.)*
- Detection is post-hoc and mis-attributing. Exit 137 raises the OOM event
  (`infra/container_manager.py:2519-2525`, `_EXEC_OOM_EVENT = "container.command_oom_killed"`),
  and the event carries the container's **declared** memory budget, not the host condition
  (`:1040-1070`, `:1083-1101`). The telemetry therefore reads "this container had 1g and was
  killed" — true, and useless for the aggregate question, because the cause was its eight
  neighbours.
- A neighbouring blind spot: configuration drift never compares `mem_limit` / `cpu_quota` /
  `oom_score_adj` (`infra/container_manager.py:4109`; the audit records the same at
  `docs/container_subsystem_audit.md:288`, citing `:1678-1704`). A container running with a
  `memory.max` different from its recorded intent is invisible to drift detection. That is not
  this ticket, but it is why a memory envelope cannot be built on drift state alone.

---

## 3. The Gap

**Finding:** the tree owns a per-container memory bound and a per-workspace container count, and
has **no representation of aggregate memory anywhere** — no symbol, no config key, no admission
step, no probe, no reason code, no record field. Formally, four artefacts are missing:

1. **a number per scope** — no `max_memory` beside `max_containers`
   (`agent/config/defaults.py:137` has no memory sibling);
2. **a measure of current commitment** — no probe alongside `disk_usage` /
   `workspace_container_count` (`security/admission_gate.py:211-217`) that reports committed or
   available memory;
3. **a comparison at admission** — no step between the count check (`:483-506`) and narrowing
   (`:508-519`) that reads `AdmissionRequest.mem_limit` (`:135-146`) against anything;
4. **a verdict** — no reason code for "this container would exceed the memory envelope"
   (closed set at `:93-105`).

The per-container limit and the count cap are both **sum-blind**: (a) bounds each term, (b)
bounds how many terms there are, and nothing bounds the total. `6 × 1g` is admissible on a host
of any size, and the code cannot tell a 64 GB host from a 4 GB one, because no live measure of
the host is ever taken. That is the entire finding, and it is worth stating in the same words the
operator used: *the aggregate Envelope does not exist.*

The target arch anticipated the surrounding machinery but not this axis. It asks for "one place
that can answer *may this container exist, and under what configuration?*"
(`docs/container_subsystem_target_architecture.md:11`) and puts admission in branch 5 as
"host disk, daemon, allowlist, limits" (`:181-194`) — memory appears only as "limits", which is
the per-container limit of §2.1, and its own guardrails forbid the machinery a full solution
would want: "*No K8s-style scheduler … no reconciliation loop, no declarative control plane*"
(`:164`), "*No auto-scaling. Container counts change on explicit decisions, not on load*"
(`:165`), "*No cross-host container orchestration. This is a single-host model*" (`:166`). Any
option below that grows a controller therefore needs the target arch amended first, not just a
new field.

---

## 4. Options

Four shapes, priced on the same five axes. All four are admission-time or ledger-time decisions;
none is implemented.

### 4.1 Option A — Per-workspace memory cap (declared sum)

**Description:** a per-workspace byte budget, sibling to `max_containers`. Admission sums the
declared `mem_limit` of the workspace's consuming containers and denies any create whose declared
limit would push the sum past the cap. Pure arithmetic over declared values: no host knowledge,
deterministic, trivially testable with fakes. At the default six-container cap this bounds a
workspace at `cap` regardless of how the six are sized.

**Hook point:** `security/admission_gate.py:483-506` — a step 6b immediately after the count check
(`:506`), before derived narrowing (`:508`). Inputs: `AdmissionRequest.mem_limit` (`:135-146`) and
one new `Probes` method next to `workspace_container_count` (`:211-217`) returning the declared
sum, ideally as an atomic reserve following the registry precedent at
`infra/container_registry.py:533-557`. New config key plumbed exactly like `max_containers`
(`infra/container_manager.py:820-825`, `:882-898`; `infra/container_registry.py:940-955`), new
reason code in the closed set at `:93-105`.

**Cost:** low. One config key, one probe, one step, one reason code, no daemon-wide enumeration.
Fits the existing "fail closed when unobservable" pattern (`:494-500`) verbatim.

**What it fails to catch:** anything cross-workspace. Three workspaces with a 6 GB cap each on a
6 GB host still admit 18 GB; the incident's nine containers look like a single workspace only
*unverified — needs runtime check* (see §6). It also inherits the overshoot window unless the
sum-and-reserve is atomic, and it bounds *declared* values, not residency (page cache, the docker
daemon itself, and the app are outside).

**Bypass route:** edit `max_memory` in workspace `config.json` upward — a legitimate, silent,
one-line bypass.

**Not a bypass route (corrected v0.1.2):** `tools/docker_code_runner.py` does not create
containers itself. `:394-402` is a `ContainerManager(` *constructor* call — `mem_limit=` at
`:400` is a ctor kwarg, and the file contains no `containers.run(` at all — and the create runs
through `manager.start(...)` (`:411-417`) → `infra/container_manager.py:2068` →
`_fresh_start()` (`:2129`), which builds a full `AdmissionRequest` (`:2245-2266`) and calls
`admit(_admission_request, probes=ClientProbes(self.client))` at `:2267`, returning an error
dict on `Deny` (`:2268-2269`). Admission therefore runs on that path, and a step 6b added here
is enforced for `DockerCodeRunner` unchanged. The earlier "never calls `admit`" reading was a
grep artefact: the file imports no admission symbol because it never reaches the create
primitive at all.

**The real defect next door — a dead admission branch:** the gate is duplicated. Every
production path admits *before* the primitive — `infra/container_manager.py:2267`,
`infra/container_registry.py:303`, `infra/containers/executor.py:994`,
`infra/resource_container_manager.py:1127` — and then calls `create_hardened_container(...)`
with `admission=None` (`infra/container_manager.py:2114-2116`,
`infra/container_registry.py:339`, `infra/resource_container_manager.py:1210-1212`,
`infra/containers/executor.py:1063-1065`). The primitive's own gate at
`infra/container_create.py:407` therefore sits behind `if admission is not None:` (`:387`) and
is unreachable: `AdmissionContext(` is constructed **nowhere in production** — only in
`tests/test_container_create_spec.py:159`, `:294`, `:314`. One fact, four sites. Disposition:
wire `AdmissionContext` through those four callers (one gate in one place, which is what the
target arch asks for) or delete the branch. Keeping both is what produced the misreading
above.

### 4.2 Option B — Per-host memory cap (declared sum, all workspaces)

**Description:** one host-wide byte budget; admission sums the declared limits of **all** live
containers the app created on this host, regardless of workspace, and denies when the new one
would exceed it. Structurally the same arithmetic as A, scoped to the thing that actually froze.

**Hook point:** a host-scoped check belongs with the other host preconditions, so a step between
daemon reachability (`security/admission_gate.py:456`) and count (`:483`), or a new step 6c; the
`Probes` protocol (`:211-217`) gains `host_committed_memory()`, which cannot be served by
`workspace_container_count` and needs a daemon-wide enumeration of app-labelled containers. The
budget default belongs beside the disk policy defaults (`agent/config/defaults.py:147-155`) or
next to `DEFAULT_MAX_CONTAINERS` (`:137`) — and needs a *value*, which is the hard part on a tree
that has never known the host's size.

**Cost:** medium. Requires enumerating containers outside the current workspace (new probe
surface, new failure modes), a default-fraction decision, and a fail-closed policy when the
daemon is briefly unobservable — which, following `:494-500`, means denying *all* creates during
a daemon hiccup. Needs the same new reason code.

**What it fails to catch:** still declared, never actual — and swap is unbounded per container
(no `memswap_limit` anywhere in `infra/`, since fixed — see §2.4), so a host cap over declared
RAM does not bound the swap pool that the incident actually exhausted. It is also a static
number: a cap chosen when the host was idle is wrong when the host is busy, and the wrongness is
invisible because nothing measures.

**Bypass route:** the single host-wide number is easy to raise, and it has no per-workspace
legibility — an operator cannot tell whose containers ate the budget. (`DockerCodeRunner` adds
no bypass of its own: it admits through `_fresh_start` — §4.1, corrected v0.1.2.)

### 4.3 Option C — Admission driven by live host pressure *(operator's lean)*

**Description:** do not model capacity — measure it. A new admission step reads the host's
current memory headroom (MemAvailable and swap free, or host-level cgroup accounting) and denies
a create whose declared limit would breach a reserve floor. No new per-scope number is invented;
the input is the host's own state at the moment of the decision. Reuses the existing two-tier
WARN/DENY discipline from the disk check: a WARN tier that attaches an advisory note, a DENY tier
that refuses.

**Hook point:** `security/admission_gate.py:444` is the exact precedent — the disk headroom step
reads `probes.disk_usage(...)` and applies `ADMISSION_DISK_MAX_USED_PCT` /
`ADMISSION_DISK_MIN_FREE_BYTES` / the WARN pair (`agent/config/defaults.py:147-155`). The memory
step is its twin: add `host_memory_headroom()` to `Probes` (`security/admission_gate.py:211-217`),
read it in a new step adjacent to `:444`, tier it with new `ADMISSION_MEM_*` constants beside
`:147-155`, and add the reason code to `:93-105`. The probe's implementation is the only new
reading of `/proc/meminfo` (or a `psutil.virtual_memory`/`swap_memory` call, used today only at
`agent/logging/__init__.py:601-614`).

**Cost:** low-medium code, high operational variance. The probe must honour the gate's
never-raises contract (`:1-8`): any probe failure is a WARN or a fail-closed DENY with its own
reason code, never an exception — otherwise a missing or unusual `/proc` breaks *every* create.
Tests stop being pure arithmetic and depend on injected probe values; the `Probes` protocol is
already built for exactly that (`:211-217`), so the testing story exists.

**What it fails to catch:** everything that happens *after* admission. This is a point-in-time
check at the moment of create; nine containers admitted into a healthy host, each later growing
to its full 1 GB, reproduce the incident exactly. It answers "is there room right now", not "may
this workspace ever commit another gigabyte". It also does not bound swap growth (again: no
`memswap_limit`, since fixed — see §2.4), and it makes an identical create succeed or fail
depending on unrelated neighbours' page cache — deterministic-looking but not deterministic.

**Bypass route:** anything that creates containers without passing the gate — today only
`live_smoke_docker.py:233` and `:244`, a standalone host-runnable smoke script that calls
`client.containers.run(...)` directly, passes no `mem_limit`, is imported by nothing and is not
a security boundary (the `DockerCodeRunner` site named here before v0.1.2 does admit — §4.1);
and, more subtly, timing: a create that lands while the host is momentarily quiet is admitted,
and the pressure arrives afterwards.

### 4.4 Option D — Scheduler against a capacity model

**Description:** the "proper" answer. A capacity ledger owns the host budget, allocates declared
limits to container records, and reconciles desired against actual — i.e. a scheduler, which is
also what the audit's "*no single place that can answer may this container exist*"
(`docs/container_subsystem_target_architecture.md:11`) ultimately gestures at. Container record
`intent_snapshot.mem_limit` (`docs/container_record_design.md:63`) becomes the ledger's per-entry
declared value; admission becomes a query against the model.

**Hook point:** not a step — a subsystem. It would sit at the admission entry
(`security/admission_gate.py`, the "one admission gate" of
`docs/container_subsystem_target_architecture.md:22-24`) backed by a capacity record and a
reconcile loop, replacing step 6 (`:483-506`) with a model query and adding a drift/true-up path
(currently absent for memory: `infra/container_manager.py:4109`).

**Cost:** high, and partly *political*: it contradicts three explicit non-goals — "no K8s-style
scheduler … no reconciliation loop, no declarative control plane"
(`docs/container_subsystem_target_architecture.md:164`), "no auto-scaling … not on load" (`:165`),
"single-host model" (`:166`). Adopting D means amending the target arch, not adding a field —
and it needs a reconciliation loop on a tree whose fail-closed, never-raises admission gate
(`security/admission_gate.py:1-8`) was deliberately built to avoid exactly that machinery.

**What it fails to catch:** model drift. A ledger is only as true as its last reconcile, so
between reconciles the model and the kernel disagree, and the kernel still wins. It also does not
bound swap unless swap is modelled, and it inherits option C's fundamental limit — a committed
allocation is not a resident byte, so the freeze remains reachable by legitimate growth.

**Bypass route:** containers created outside the ledger (docker CLI, other tooling, another
checkout) are invisible until reconcile, and the ledger will happily plan against capacity that
already has an unrecorded occupant.

---

## 5. Recommendation

**Recommend Option C — admission driven by live host pressure** — as the operator's lean, and for
three reasons that are about the incident rather than about elegance:

- **It measures the thing that failed.** The casualty was the host, not a workspace budget.
  C is the only option whose input is the host's actual state; A and B both bound numbers the
  tree invented and cannot check. A workspace cap would not have prevented nine containers from
  a *different* workspace doing the same thing.
- **It is the smallest surface that works.** One probe on an existing seam
  (`security/admission_gate.py:211-217`), one step beside the disk check (`:444`), one two-tier
  policy block mirroring `agent/config/defaults.py:147-155`, one reason code (`:93-105`). No new
  controller, no ledger, no config key that has to be guessed and then maintained.
- **It stays inside the target arch's non-goals.** A measure-and-deny check is not a scheduler
  and not autoscaling; it leaves `docs/container_subsystem_target_architecture.md:164-166`
  intact, where option D would require rewriting them.

State it plainly, because this is the crux and it should not be softened:

> **(a) the per-container limit and (b) the container count cap are both sum-blind.** (a) bounds
> each term; (b) bounds how many terms there are — six, `agent/config/defaults.py:137`. Nothing
> bounds the sum, and nothing in the tree can observe the host it is standing on: admission's
> only probes are disk, daemon, count (`security/admission_gate.py:211-217`). `6 × 1g` is
> admissible today on a 4 GB host and on a 64 GB host with equal confidence. Only a live measure
> can close that, which is why (A) and (B) cannot be the answer on their own no matter how their
> constants are chosen.

**Disposition:** recommend C now; treat B as the natural second layer *only* if a static
commitment bound is also wanted for legibility, and explicitly not as a substitute for C. Three
follow-ups should be opened as separate tickets rather than folded in here, because each is a
different failure: (i) `memswap_limit` was unset on every create path (§2.4), so swap — the axis
the incident actually exhausted — is unbounded per container and no option above catches it
(fixed off this branch: `infra/container_create.py:439-440` on
`fix/container-default-mem-limit`, commit `fed55868` — `memswap_limit = mem_limit`, zero swap
headroom, deliberately: with `oom_score_adj=1000` the container is already the designated OOM
victim, so it dies fast and observably (exit 137 / `container.command_oom_killed`) instead of
the host thrashing);
(ii) the create primitive's admission branch is dead plumbing — `admission=None` at all four
`create_hardened_container` call sites, so `infra/container_create.py:407` is unreachable (§4.1,
corrected v0.1.2: `DockerCodeRunner` is not the ungated path it was recorded as); (iii) drift
never compares `mem_limit` (`infra/container_manager.py:4109`),
so a memory envelope cannot safely be built on drift state.

---

## 6. Open Questions

1. **What is the envelope's unit — declared limit or resident memory?** Declared is cheap, stable
   and racy-truthful about intent; resident (`memory.current` summed) is what freezes the host but
   requires polling, and admission is a request-time decision. C can only plausibly use headroom
   (a third thing); A/B use declared. This choice must be made before any option is implemented.
2. **Is unbounded per-container swap in scope?** No `memswap_limit` exists on any create path
   (§2.4 — since fixed there). If swap is out of scope, C is defeatable by exactly the mechanism
   in the incident.
3. **Where does the reserve threshold live, and who commits to the number?** Disk has
   `ADMISSION_DISK_*` constants (`agent/config/defaults.py:147-155`) chosen by someone; memory
   needs the same, and the value matters more than the machinery.
4. **Fail-open or fail-closed when the memory probe is unreadable?** Precedents point both ways:
   the count check fails CLOSED (`security/admission_gate.py:494-500`), the disk check has a WARN
   tier. Fail-closed on a probe that can be absent in containers would deny every create.
5. **Did the incident's nine containers span one workspace or several?** Determines whether A
   would have prevented it at all. *unverified — needs runtime check.*
6. **Does the envelope apply to resource containers (512m) and `DockerCodeRunner` (512m)?** They
   are containers and both admit — resource containers at
   `infra/resource_container_manager.py:1127`, `DockerCodeRunner` via `_fresh_start`
   (`infra/container_manager.py:2267`). The only ungated creates in the tree are the two direct
   `containers.run` calls in `live_smoke_docker.py` (§4.3). Corrected v0.1.2 — this entry
   previously said `DockerCodeRunner` "does not admit".
7. **Refuse or wait?** Every existing denial is an immediate refusal (`AdmissionDenied`, e.g.
   `infra/container_create.py:409`). A queue-with-timeout is a much larger design and would grow
   the controller the target arch forbids.
8. **Do host co-tenants count?** The app itself, the docker daemon, page cache, and any
   non-container process share the host. C sees them through MemAvailable by construction; A and
   B have no way to represent them.
9. **Should `CONSTRAINT_MEMCG` be adopted as a term in reason codes?** It is the kernel's
   attribution vocabulary, not this tree's (§1). Recommendation: no — use the tree's own
   `REASON_*` style (`security/admission_gate.py:93-105`) and cite the kernel label in prose.

---

## 7. Not In Scope

- **Any implementation.** No code, no config key, no probe, no reason code. This ticket is a
  record and a price list.
- **Cross-host or fleet capacity.** Single-host model by design
  (`docs/container_subsystem_target_architecture.md:166`).
- **A scheduler, a reconciliation loop, or a declarative control plane** (`:164`), and
  **autoscaling** (`:165`) — option D is documented as an option, not endorsed.
- **The aggregate CPU envelope.** Same shape, different axis
  (`RESOURCE_CPU_QUOTA` / `cpu_quota` at `agent/config/defaults.py:133`, `:233`). Deserves a
  parallel ticket; nothing here transfers without re-derivation.
- **Disk aggregation.** Already covered by the headroom check (`security/admission_gate.py:444`)
  and flagged out of scope by the audit (`docs/container_subsystem_audit.md:187`).
- **The drift blind spot for memory.** `infra/container_manager.py:4109` never compares
  `mem_limit`; recorded here so it is not lost, fixed elsewhere.
- **The stale audit numbers.** `docs/container_subsystem_audit.md:65-83` says
  `DEFAULT_MAX_CONTAINERS = 4`; the tree says 6 (`agent/config/defaults.py:137`). Separate docs
  patch.
- **The dead admission branch in the create primitive** (§4.1, corrected v0.1.2 — the
  `DockerCodeRunner` "never admits" claim it replaces was wrong) and **`memswap_limit`** (fixed
  off this branch — see §2.4) — both named as follow-ups in §5; the admission branch is still
  open.
- **Per-container limits and the hardening recipe.** Unchanged; that part of the envelope works.

---

## 8. Revision Log

- **v0.1** (2026-10-21) — Initial ticket. Incident recorded verbatim; current behaviour anchored
  to the tree (all `file:line` verified by direct read, this session); four options priced;
  recommendation recorded as C with (A)/(B) explicitly rejected as sum-blind on their own. No
  code changed.
- **v0.1.1** (2026-10-21) — Forward note in 2.1: per-container default lowered to 512m under
  separate change `fix/container-default-mem-limit`; no argument changed.
- **v0.1.2** (2026-10-21) — §4.1/§4.2/§4.3/§5(ii)/§6(6)/§7: the "`tools/docker_code_runner.py`
  creates containers and never calls `admit`" claim is refuted by direct read — `:394-402` is a
  `ContainerManager(` ctor call (`mem_limit=` at `:400` is a ctor kwarg, no `containers.run(` in
  the file) and the create admits at `infra/container_manager.py:2267` — so it is replaced, in
  place, with the defect that is real: the primitive's admission branch
  (`infra/container_create.py:387-409`) is dead plumbing (`admission=None` at all four callers;
  `AdmissionContext(` constructed only in tests), and the only creates outside the gate are
  `live_smoke_docker.py:233`/`:244` (dev smoke script). No option, price or recommendation
  changed; §2.1's `:400` row left as written (it names the same ctor kwarg).
