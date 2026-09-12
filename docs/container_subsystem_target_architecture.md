# Container Subsystem — Target Architecture

**Status:** Design document. Nothing here is implemented unless a section explicitly says a branch has landed.
**Companion:** `docs/container_subsystem_audit.md` is *where we are*; this document is *where we are going*.
**Scope:** Design only. No code, tests, or config changes live here; each move is executed by its own later branch (§9).

---

## 1. The problem, and why this document exists

The container subsystem works, but it grew by accretion. The audit (`docs/container_subsystem_audit.md`) records the symptoms: four independent create paths; two competing configuration sources-of-truth that disagree on the same container; an inventory check that cannot see hot-path containers; a notes file that loses concurrent writes; a container limit and GC age that exist only as scattered literals; and no single place that can answer *may this container exist, and under what configuration?* before it is created.

Every one of those was treated as a local patch. The structural cause is that there is no **model of a container** — only plumbing that happens to produce containers. This document states the model that makes that whole class of defects impossible by construction rather than by successive patching.

Two standing constraints frame everything below:

- **Fail closed.** Any value that cannot be derived is denied, never defaulted-open. This is why the audit resolves the two config sources in favour of the fail-closed one.
- **Do not lose data.** Any migration must be idempotent and non-destructive. A prior knowledge-base data-loss incident — a non-idempotent re-run that destroyed archives and permanently lost untracked files — is the durability bar this document must clear.

---

## 2. The target model

A container becomes a **first-class, durable object**: one record, one resolver, one admission gate. Everything the subsystem knows about a container lives on its record; everything that decides a container's configuration passes through one pure function; everything that decides whether a container may be created passes through one gate.

**The Container Record** (one JSON document per container, in the vault) carries a deliberately small set of fields:

| Field | Meaning |
|---|---|
| `id` | Container identity — the Docker container id once created; absent on an intent record before create |
| `lifecycle_class` | One of the four classes (§3, move 5) |
| `owner` | `workspace-owned` (free-use) or `system-owned` (resource); audit §6 |
| `purpose` | Why it exists: free-use worker, `resource:<name>`, … |
| `intent_snapshot` | The configuration the container was *created* with — the reference for drift detection (§3, move 3) |
| `notes` | Free text. Replaces the shared `container_notes.json` file (§3, move 1) |
| `event_log` | Append-only record of lifecycle and drift events (§3, move 6) |
| `schema_version` | Record-format version; `0` for synthesised legacy records (§5) |
| `inferred` | `true` when the record was synthesised from live state rather than captured at create (§5) |
| `state` | Last observed state |

The proposal's field list is exactly `id`, `lifecycle_class`, `owner`, `purpose`, `intent_snapshot`, `notes`, `event_log`; `schema_version`, `inferred` and `state` are the only additions, and each is labelled for the move that reads it. No other field exists.

Notes, ownership, drift evidence and history all live *on the container* instead of in side channels keyed by name. That single decision is what removes the notes race, the ownership ambiguity and the inventory blindness at once.

---

## 3. The six moves

The model is delivered by six moves. They are listed in the order the model is understood, not the order they are built (§9 gives the build order). The decision record sometimes refers to these components by number inconsistently (for example it cites drift-as-events as "move 6"), so the numbers here are labels, not a build order — the branch sequence in §9 is the build order.

**Move 1 — Container Record.**
Every container gets exactly one durable record (§2). Notes become a field of that record, replacing the shared, unlocked `container_notes.json` whose concurrent read-modify-write lost 19 of 24 writes in the audit's probe-v2 check (`LOST_UPDATES=19` against 24 expected). The `event_log` makes creation, teardown and drift events append-only and inspectable, replacing ad-hoc logging.

**Move 2 — One pure, fail-closed `resolve_container_config()`.**
The two sources-of-truth collapse into a single pure function, `resolve_container_config()`, which derives a container's full intended configuration from a spec plus effective permissions. Pure means no Docker calls and no I/O, so it is unit-testable and identical on every call site. Fail-closed means `caps=None` is *never* permissive — the audit's `## Decisions Recorded` index states this as decision 4: "The two SSOTs collapse to one fail-closed config function — SSOT #2 wins, `caps=None` never permissive". The fail-closed helper wins; the permissive helper is deleted.

**Move 3 — Drift as events.**
The `intent_snapshot` captured at create (§2) is the reference. Later divergence — in policy, runtime, image, or hardening — is detected and recorded as a **discrete event** on the record's `event_log`. *None of these events stops the container.* The user decides. This is the concrete meaning of "no silent kills" (audit §4): the subsystem never mutates or destroys a running container behind the user's back.

**Move 4 — The admission gate.**
One choke point, `admit(spec) -> Allow | Deny | Transform`, sits before anything is created. It checks: host disk headroom (the audit's probe-v2 evidence records host `/` as 94 % used / ~21 GiB free, with no precondition anywhere in `infra/` or `tools/`); Docker daemon reachable (`ContainerManager.__init__` today hard-requires a live daemon); the allowlist; the per-workspace container limit (6, user-configurable; audit §3); capability derivation; and lifecycle-class rules. `Deny` is loud and structured; `Transform` may only *narrow* a request, never widen it.

**Move 5 — Four lifecycle classes.**
Four classes, each with fixed restart policy, ownership default, and GC rule. A container's class is declared, not inferred from labels or name prefixes:

- `ephemeral` — session-scoped; restart `no`; torn down with its session.
- `persistent` — long-lived, workspace-owned; restart `unless-stopped`; 24 h idle GC (audit §5).
- `resource` — system-owned (e.g. `git`); restart `unless-stopped`; never swept by workspace GC.
- `service` — reserved for future long-lived shared containers; not created today.

**Move 6 — The append-only event log.**
Every record carries an ordered, append-only `event_log`. The record is *current state*; the log is *what happened*. Lifecycle transitions and drift events are appended, never rewritten, so the history of a container stays durable and inspectable independently of the state it ended in.

```
  request ──▶ admit(spec) ──▶ Allow │ Deny │ Transform
                                │ Allow
                                ▼
                    resolve_container_config()      (pure, fail-closed)
                                │
                                ▼
                     Container Record  (vault JSON)
                     id · class · owner · purpose
                     intent_snapshot · notes · event_log
                                │
                                ▼
                          docker create / start
```

---

## 4. Machines, not plumbing

Containers are workspace machines — the runtime for real work — not internal agent tooling. The four properties below are the acceptance test for the whole model. If a container is not all four, it is still plumbing.

- **VISIBLE.** Every container has a record and an append-only event log. There is no path that creates a container without a record, and no name-matching guesswork: the audit confirmed (CONFIRMED live by probe v2) that the integrity check was blind to hot-path containers because it stripped the session suffix from the name. The record removes the guess entirely.
- **CONTROLLABLE.** Start, stop, teardown and recreate are owner decisions expressed as events, governed by the lifecycle class. Configuration is never mutated in place; a change is a new intent, not a silent edit.
- **COMMUNICABLE.** A container can be addressed and annotated: `set_note` is exposed as an agent tool (audit §6), and the record is the place a note lives. Outbound network policy (the Comm Gate, `domain_allowlist.json`) is *not yet enforced* and its UI carries that label (audit §11) — the record is the surface where enforcement will later attach.
- **OWNABLE.** Ownership is explicit — free-use containers are workspace-owned; resource containers are system-owned (audit §6). Rename, claim, handoff and release are Phase-3 items, but the record field exists now so those operations have somewhere to attach.

---

## 5. Migration path

The move from today's state to the model is additive and idempotent, never a destructive rewrite.

1. **Synthesise legacy records.** For every existing container, derive a record from a live `docker inspect`. These records are **lower-trust by construction**: they carry `inferred: true`, `schema_version: 0`, and a **partial** intent snapshot. We know what a container *is* now; we do not know what it was *created with*. The snapshot is the best available approximation, not the original intent, and drift detection must treat it as such.
2. **Read only, never mutate.** Migration *reads* Docker state. It does not stop, restart, or mutate the labels of any running container. It creates records *alongside* the existing containers; running containers that are mid-job are left alone.
3. **Backfill only what is known.** Fields that cannot be derived from live state (e.g. `notes`) are left empty rather than guessed. A missing field is empty, never fabricated.
4. **Idempotent.** The synthesis is crash-resume safe — interrupted and re-run, it produces the same records and never duplicates them.
5. **Observable.** The run appends to an append-only `migrations.log` written in the vault. This is the same *class* of durability-critical data as the prior knowledge-base data-loss incident: it must be durable and never best-effort.

Reconciliation between these synthesised records and observed reality — deciding which inferred fields to trust and correcting them — is a **separate, future decision** and is explicitly out of scope for this workstream.

---

## 6. Restart policy and boot drift

The shipped subsystem has no restart policy anywhere; Docker's default is `restart=no`, so after a host reboot every container stays stopped and is re-created on demand. The target makes this explicit per lifecycle class (§3, move 5): `unless-stopped` for `persistent` and `resource`, `no` for `ephemeral`.

**The boot drift check is fast, and it runs first.** At boot, before the UI comes up, each record's `intent_snapshot` is compared against its live configuration. The check must stay under one second per container — ideally batched — so it never becomes a reason to delay startup. A container whose snapshot no longer matches is left STOPPED and a drift event is recorded.

**Boot drift is visible in the UI on first load as a card, not a log line.** The user sees it where they act on it, in plain language, e.g. *"Three containers were stopped after reboot due to drift"*, with the affected containers listed and an accept-or-recreate choice. The notification mechanism itself is not yet designed; the requirement here is that the boot-drift result is reported to the user as a first-class card rather than buried in logs.

**This is deliberate and approved.** A container that is stopped at boot because of drift is *not* a "silent kill". "No silent kills" governs *mutating a running container* — the subsystem must never change or destroy something that is already running behind the user's back. A container that failed its drift check and therefore never started was never running to be killed; leaving it stopped is the honest, visible outcome. The rule also does not forbid starting a container that *does* still match its record: a matching container may be silently restarted.

```
  boot ──▶ for each record:  intent_snapshot  vs  live config
                │ match                        │ drift
                ▼                              ▼
        auto-start if                   record drift event
        unless-stopped                  leave STOPPED
                                        show boot-drift card ──▶ user decides
                                                                 accept / recreate
```

---

## 7. Registry transition

The Container Registry becomes the system of record for inventory without a flag-day cutover.

- **Dual-write.** During the transition the registry *writes* to the Container Record while the legacy manager keeps reading its own state. The two coexist so no reader breaks mid-flight; the record is populated for everyone even before every reader has moved.
- **Retiring the legacy manager is a separate, later decision.** This document does *not* commit to retiring it. That decision waits until the Container Record has proven itself in production.
- **Incremental UI growth.** The UI adopts the record read path screen by screen, and the growth is ordered by the branch sequence (§9). Once `feat/container-record` merges, the GUI can build the container inventory panel against the record API. Drift display is added when `feat/drift-events` lands. Limits display is added when `feat/admission-gate` supersedes the current limit code. This is incremental UI growth, not a big reveal; until a screen moves it shows the legacy view, and any divergence between the two is surfaced rather than hidden.

---

## 8. What we are explicitly NOT doing

Scope is as load-bearing as scope-creep avoidance. Explicitly out of scope — each named so it is not re-litigated:

- **No database.** The Container Record is vault JSON files, not a database.
- **No ORM.** Records are read and written directly; there is no object-relational layer.
- **No event-bus library.** The append-only event log is a file, not a message bus.
- **No K8s-style scheduler.** No pod model, no reconciliation loop, no declarative control plane.
- **No auto-scaling.** Container counts change on explicit decisions, not on load.
- **No cross-host container orchestration.** This is a single-host model.
- **No plugin system for the Container Record.** The record schema is fixed; behaviour is not pluggable.
- **No new container types.** The dead `mcp` / `proxy` taxonomy entries stay dead; no new class is added until a real workload needs it.
- **No Comm-Gate enforcement now.** `domain_allowlist.json` remains unenforced; its UI carries a "not yet enforced" label (audit §11). Enforcement is a parked item, not part of these moves.
- **No in-place config mutation.** Changing a running container's configuration is not a supported operation; a change is a new intent (§3, move 3).
- **No persisting the permission ceiling into stored state.** Raw grants are stored; the ceiling is applied at enforcement/read time only, as today.

Bounded scope is a feature, and this section exists so it is not re-litigated.

---

## 9. Branch sequence

Execution order. Each item is one branch; they are processed sequentially, never in parallel, because the early code branches all touch the same container-manager module.

| # | Branch | Delivers | Status |
|---|---|---|---|
| 1 | `fix/container-limit-and-gc` | limit 6 per-workspace, GC 24 h, per-workspace axis wired | **DONE** |
| 2 | `docs/container-subsystem-audit` + `docs/container-target-architecture` | the audit revision and this target-architecture doc | **DONE** |
| 3 | `feat/container-record` | the Container Record + legacy-record synthesis | planned |
| 4 | `fix/container-config-ssot` | one fail-closed `resolve_container_config()` (move 2) | planned |
| 5 | `feat/admission-gate` | `admit(spec)` — host disk, daemon, allowlist, limits (move 4) | planned |
| 6 | `feat/drift-events` | drift detection as recorded events (move 3) | planned |
| 7 | `fix/notes-as-record-field` | notes become a record field; the race is removed (move 1) | planned |
| 8 | `refactor/lifecycle-classes` | the four classes drive restart policy, ownership, GC (move 5) | planned |
| 9 | `feat/restart-policy` | `unless-stopped` / `no` per class + boot drift handling (§6) | planned |
| 10 | `feat/registry-transition` | dual-write, retire legacy reads, incremental UI (§7) | planned |

Items 1 and 2 are complete. Item 1 (`fix/container-limit-and-gc`) landed the limit/GC fix; item 2 is the documentation pair — the audit revision on `docs/container-subsystem-audit` and this document on `docs/container-target-architecture`. Items 3–10 are planned: the branches do not exist yet, which is expected, not a defect.

---

## 10. Scope guardrails — the Container Record stays MINIMAL

The Container Record is deliberately the smallest thing that can be durable state: **one JSON file per container, fixed schema, state only.** It does not compute policy, it does not execute lifecycle, and it does not store binary data. It records; other components decide and act. If a proposed change wants the record to *do* something, that is the signal that we are solving the wrong problem at the wrong layer.

The guardrail is a rule, not a preference:

> The record carries **only** fields that a named move reads. A field that no move reads does not exist.

Applied concretely:

- The fields in §2 are the whole set. There is no metadata bag, no free-form `extra`, no denormalised copies of Docker state.
- `intent_snapshot` holds exactly what drift detection compares — no more.
- `event_log` entries carry a type, a timestamp, and the minimal payload the UI needs; not full configs.
- `notes` is a string. It is not a message thread, not a tagged structure, not a notification centre.
- New fields are added by bumping `schema_version` and stating which move reads them. A field with no reader is a defect.
- Ownership, rename/claim/handoff/release (Phase-3 items) attach to existing fields; they do not justify new ones.

If a proposed change wants a field that no move reads, the answer is no — either it belongs on a different object, or the move that needs it does not exist yet.
