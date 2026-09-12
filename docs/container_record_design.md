# Container Record — Design Document

**Status:** Design draft (not implemented)
**Scope:** The **Container Record** subsystem — a state-only, one-JSON-file-per-container
durable record that becomes the source of truth for what a container *is*, replacing the
name-keyed in-memory registry and the shared `container_notes.json` side file. Includes the
one-time synthesis of records for legacy containers.
**Related docs:** `docs/container_subsystem_target_architecture.md` (canonical — cited
throughout as "the target arch"), `docs/container_registry_design.md` (superseded sibling),
`docs/windows_installation_saga.md`, `docs/windows_stability_contract.md`,
`scripts/migrate_vault.py` (idempotency precedent).
**Line references:** every `file:line` anchor below was verified by direct read of the
current tree (this session). They are *current-truth anchors* — the design must keep them in
sync as it lands.

---

**Table of Contents**

1. [Record Schema](#1-record-schema)
2. [Path Convention](#2-path-convention)
3. [Lookup API](#3-lookup-api)
4. [Migration Algorithm](#4-migration-algorithm)
5. [Test Strategy](#5-test-strategy)
6. [Scope Guardrails](#6-scope-guardrails)
7. [Docker Label Contract](#7-docker-label-contract)
8. [Non-Goals](#8-non-goals)
9. [Open Questions for Main](#9-open-questions-for-main)

---

## 1. Record Schema

The schema is sourced from the target arch §2 field table. One record is **one JSON file
per container** (target arch §10). The authoritative field list is:

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `id` | string | yes | — (UUID4, generated at create) | Opaque subsystem identifier; stable across container recreate. See §2 for UUID4-vs-short-hash. |
| `lifecycle_class` | string enum | yes | — | One of the four classes in target arch §3 move5: `ephemeral`, `persistent`, `resource`, `service`. |
| `owner` | string enum | yes | — | `workspace-owned` \| `system-owned` (target arch §2). |
| `purpose` | string | yes | `""` | Human-readable reason the container exists. |
| `intent_snapshot` | object | yes | `{}` | The config the container was **created with** — drift reference (target arch §2, §6). Nested fields below. |
| `notes` | string | yes | `""` | Free-text operator notes. Replaces the shared `container_notes.json` (target arch §2, §3 move1). |
| `event_log` | array | yes | `[]` | Append-only lifecycle + drift events (target arch §2, §3 move6). Entry shape below. |
| `schema_version` | int | yes | `0` | `0` for records synthesised from live legacy state (target arch §2, §5). |
| `inferred` | bool | yes | `false` | `true` when the record was synthesised from live Docker state rather than authored (target arch §2, §5). |
| `migrated` | bool | yes | `false` | **Addition (deviation — see §1.3).** `true` when the record came from the legacy synthesis pass. |
| `created_at` | string (ISO-8601) | yes | now | **Addition (deviation — see §1.3).** Record creation time. |
| `updated_at` | string (ISO-8601) | yes | now | **Addition (deviation — see §1.3).** Last mutation time. |

### 1.1 `intent_snapshot` nested fields

`intent_snapshot` is the frozen creation config used by boot-drift comparison (target arch
§6). It records the hardening recipe actually applied, so a later `docker inspect` can be
diffed against it:

| Nested field | Type | Meaning |
|---|---|---|
| `network_mode` | string | Docker network mode at create (`none`, `bridge`, …). |
| `workspace_mode` | string | Workspace ownership mode recorded at create. |
| `mem_limit` | string | Memory limit (`512m`, `1g`, …) — cf. `RESOURCE_MEM_LIMIT` in `agent/config/defaults.py`. |
| `cpu_quota` | int | CPU quota (microseconds per period). |
| `oom_score_adj` | int | OOM score adjustment applied at create. |
| `image_hash` | string | Content hash / image id the container was created from. |
| `hardening` | object | The hardening recipe applied (capabilities dropped, `no-new-privileges`, read-only root, tmpfs mounts, non-root user, idle command). Shape mirrors the creation path in `infra/container_manager.py`. |

### 1.2 `event_log` entry fields

Per target arch §10, entries carry "a type, a timestamp, and the minimal payload the UI
needs; not full configs":

| Entry field | Type | Meaning |
|---|---|---|
| `timestamp` | string (ISO-8601) | When the event occurred. |
| `event_type` | string | Lifecycle or drift type (`created`, `started`, `stopped`, `drift_detected`, …). |
| `actor` | string | Who/what produced the event (`user`, `system`, `migration`, worker name). |
| `payload` | object | Minimal UI/decision payload — never a full config dump. |

### 1.3 Deviations from target arch §2

The target arch §2 states the field list is "exactly `id`, `lifecycle_class`, `owner`,
`purpose`, `intent_snapshot`, `notes`, `event_log`", that "`schema_version`, `inferred` and
`state` are the only additions", and that **"No other field exists."** It repeats the fence
in §10: a record "carries ONLY fields that a named move reads. A field that no move reads
does not exist."

This design's checklist-driven field list deviates in three directions, all flagged for Main
(§9):

1. **Three additions.** `migrated`, `created_at` and `updated_at` are **not** in §2, so they
   contradict "No other field exists" / §10 unless a named reader is identified. Either a
   reader must be named (e.g. `updated_at` for staleness display, `created_at` for ordering,
   `migrated` to mark synthesis provenance) or the fields must be dropped.
2. **One omission.** §2 lists `state` (the last observed state) — read by boot-drift (§6).
   The checklist omits it. **Recommendation: keep `state`.** Moved to open questions (§9).
3. **`id` redefinition.** `id` is deliberately redefined as a record-owned opaque identifier
   (UUID4 / short hash), with the Docker id carried in a separate `docker_id` field; target
   arch §2 instead defines `id` as *the Docker container id once created*. This redefinition is
   not what §2's field list contemplates — see §9 Q1 for Main to confirm.

---

## 2. Path Convention

### 2.1 Location

Records live under the resolved vault root, one directory per workspace:

```
<vault_root>/workspaces/<workspace_id>/containers/<id>.json
```

`<vault_root>` follows the existing precedence in `infra/container_manager.py`
`_resolve_vault_root`: explicit `vault_root` kwarg → env `THOUGHTMACHINE_VAULT_ROOT` →
`~/.thoughtmachine`. This keeps records in the same per-workspace directory
(`<vault_root>/workspaces/<workspace_id>/`) that already holds `config.json`,
`container_notes.json`, `Dockerfile` and `requirements.txt`, so the record is co-located
with the workspace it belongs to.

### 2.2 The `<id>` token — UUID4 (chosen)

**Decision: `<id>` is an opaque UUID4 string.**

- **Why.** Collision-free, generated without a central counter, stable across container
  recreate, and it leaks no coordinates (it is not derivable from the Docker id, name, or
  workspace). It is also the natural primary key for the label contract in §7 — the value
  must be a token that the record, not Docker, owns.
- **Rejected alternative — short hash** (e.g. `sha256(docker_id)[:12]`). Deterministic, so
  migration could re-derive an id without a log — but it *derives from an external key*, so
  it changes if the container is recreated, and it leaks the Docker id's coordinates into
  the filesystem path. Rejected; retained as an open question (§9).

### 2.3 Lock file

**Decision: a sidecar lock `<id>.json.lock` held via `fcntl.flock(..., LOCK_EX)` with a
bounded timeout; on timeout raise (fail closed).**

- The lock is acquired around every read-modify-write of `<id>.json` (§3).
- **Cross-platform caveat.** `fcntl` is POSIX-only; it does not exist on Windows. The
  project supports Windows (`docs/windows_installation_saga.md`,
  `docs/windows_stability_contract.md`), so on Windows the design degrades to the atomic
  write (`temp file + os.replace`) **alone**, which gives crash-safety but **no cross-process
  exclusion**, or an `msvcrt`/`portalocker` shim. This gap is an open question (§9).
- **Windows meaning:** without a lock, concurrent writers can lose updates; atomic replace
  bounds the damage to "last writer wins" and never a torn file.

### 2.4 Event-log location — embedded (chosen)

**Decision: the event log is the embedded `event_log` array inside `<id>.json`.**

- Target arch §2 models `event_log` as a field of the record, and §3 move6 / §6 describe the
  record as carrying the append-only log. Embedding keeps the record self-contained.
- **Tradeoff:** appending rewrites the whole file — the same class of race as the
  `container_notes.json` lost-update defect (audit probe-v2: `LOST_UPDATES=19 of 24`). The
  lock (§2.3) plus atomic replace is therefore **mandatory**, not optional.
- **Rejected alternative — sidecar `.events.jsonl`.** Append-only and cheap, but splits the
  record and invites divergence. Rejected; retained as an open question (§9). Note target
  arch §8's phrase "the append-only event log is a file" is ambiguous about which file.

### 2.5 Notes location — field (confirmed)

**Decision: `notes` is a string field inside `<id>.json`** — confirmed against target arch
§2, §3 move1 and §10. This is precisely what removes the name-keyed
`LOST_UPDATES` race: notes are now keyed by `id` and guarded by the per-record lock, not
keyed by container name in one shared file.

---

## 3. Lookup API

The surface is **Python only** (no HTTP). All functions take the resolved vault root
implicitly via the same precedence as §2.1.

```python
def create_record(
    workspace_id: str,
    lifecycle_class: str,
    owner: str,
    purpose: str = "",
    intent_snapshot: dict | None = None,
    id: str | None = None,          # caller may supply for idempotent re-materialisation
) -> Record: ...

def load_record(workspace_id: str, id: str) -> Record | None: ...

def list_records(workspace_id: str) -> list[Record]: ...

def find_by_docker_label(label_value: str) -> Record | None: ...

def update_record(workspace_id: str, id: str, **changes) -> Record: ...

def append_event(
    workspace_id: str, id: str, event_type: str, actor: str, **payload
) -> Record: ...

def delete_record(workspace_id: str, id: str) -> None: ...
```

`Record` is the parsed dict of §1 (a thin typed wrapper). Return types: readers return
`Record`, `Record | None`, or `list[Record]`; mutators return the updated `Record` (or
`None` for delete).

### 3.1 Fail-closed semantics (explicit decisions)

| Situation | Decision | Consequence |
|---|---|---|
| `load_record` / `find_by_docker_label` on a **missing** record | Return `None` (pure lookup) | Callers must handle absence explicitly; no exception on a benign miss. |
| `update_record` / `append_event` / `delete_record` on a **missing** record | Raise `RecordNotFound` | Mutations never silently create or no-op; a missing record is a bug. |
| **Corrupt JSON** on read | **Quarantine**: rename `<id>.json` → `<id>.json.corrupt-<ts>`, then raise `RecordCorrupt` | Never silently return `{}` (which would masquerade as an empty record and cause data loss). The quarantined file is evidence. |
| **Concurrent access** | Acquire the §2.3 lock; on timeout raise `RecordLocked` | Fail closed rather than interleave a read-modify-write. |

---

## 4. Migration Algorithm

The synthesis pass materialises records for **pre-existing** containers. It is **additive,
idempotent, crash-safe and read-only** (target arch §5): it never stops, restarts or mutates
a container or its labels.

**Inputs read from live Docker:** `docker inspect` over containers labelled
`thoughtmachine.workspace_id`, and (see §4.1) whatever session/type signal actually exists.

**Steps:**

1. Set up the workspace's `containers/` directory and open `migrations.log`
   (`<vault_root>/…/migrations.log`) for append + `fsync`.
2. `docker inspect` all containers carrying `thoughtmachine.workspace_id`.
3. For each container, compute its `docker_id`. **Skip if already logged** in
   `migrations.log` (§4.2) *and* the corresponding record file is present.
4. Synthesise the record: `schema_version: 0`, `inferred: true`, `migrated: true`,
   `lifecycle_class`/`owner` derived from container type (§4.1), `intent_snapshot` = the
   **partial** snapshot recoverable from inspect (missing keys left empty — never
   fabricated), `event_log` seeded with a `migrated` event.
5. **Write-ahead:** append `{docker_id, record_id, ts}` to `migrations.log` and `fsync`
   **before** writing the record file.
6. Write `<id>.json` atomically (temp + `os.replace`).
7. Continue to the next container; repeat per workspace.

### 4.1 Label reality check (deviation)

The checklist specifies reading `thoughtmachine.workspace_id`, `thoughtmachine.session_id`
and `thoughtmachine.resource`. **`thoughtmachine.session_id` is absent from the production label set**
(`thoughtmachine.workspace_id` / `thoughtmachine.worker` / `thoughtmachine.container_type` /
`thoughtmachine.resource`); it appears only as a test mock
(`tests/docker_integration/test_startup_check.py:312`). Session ownership is encoded inside
`thoughtmachine.worker` =
`<session_id or 'unknown'>:<worker_name>`, and container type is carried by
`thoughtmachine.container_type` (`free_use` / `resource`). Migration must therefore derive
session from `thoughtmachine.worker` (or leave it empty) and use `thoughtmachine.container_type`
+ `thoughtmachine.resource` to classify. Flagged (§9).

### 4.2 Idempotency — write-ahead log under UUID4

Because `id` is an opaque UUID4 (§2.2), a migration re-run cannot re-derive the id from the
container. The durable **write-ahead** entry in `migrations.log` closes this gap:

- A `docker_id` already present in the log is skipped (no duplicate record).
- A `docker_id` present in the log whose **record file is missing** (crash between step 5
  and step 6) is **re-materialised with the same `record_id` from the log** — so a crash
  never yields two records for one container.

This mirrors the version-marker precedent in `scripts/migrate_vault.py`
(`system/.vault_version`): a durable marker makes re-runs safe.

### 4.3 Edge-case behaviour

| Case | Behaviour |
|---|---|
| **Missing `workspace_id` label** | Container is not visible to the sweep; skip it (do not invent a workspace). |
| **Unknown container type** | Synthesise with the safest class (`persistent`/`workspace-owned`) and an event noting the inference; never abort the whole pass. |
| **Docker daemon down** | Abort cleanly before any write; the pass is a no-op. `migrations.log` records the failure. |
| **Partial disk write** | Prevented by atomic replace (temp + `os.replace`); a torn `<id>.json` cannot occur. |
| **Restart mid-migration** | Safe: §4.2 makes the pass resumable and duplicate-free. |

---

## 5. Test Strategy

### 5.1 Without a live Docker daemon

- **Fakes (default, in-sandbox).** A `FakeDockerClient` returning canned `docker inspect`
  payloads lets every path in §1–§4 be unit-tested with no daemon: schema round-trip, the
  fail-closed table (§3.1), lock acquisition/timeout, atomic-replace, and the full migration
  state machine (including the crash/re-materialise branch of §4.2).
- **File-system level.** Corrupt the JSON on disk to assert quarantine; hold the lock in a
  second process to assert `RecordLocked`.
- **What needs a live host.** Real label stamping, real `docker inspect` output shape, and
  the boot-drift comparison (§6 of the target arch) are verified once, manually, on a live
  host — not in the unit suite.

### 5.2 Cadence

Focused, fast tests run per commit (sandbox); **one merged gate** runs the full record +
migration suite before merge.

### 5.3 Coverage split

- **Record unit tests (this subsystem):** field validation, nested `intent_snapshot` /
  `event_log` shape, all seven API functions, the fail-closed matrix, the migration state
  machine against fakes, and idempotency/no-duplicate on re-run.
- **Deferred to the integration layer:** anything that needs the real Docker daemon,
  cross-host behaviour, and the boot-drift comparison end-to-end.

---

## 6. Scope Guardrails

The record is **state-only**. It is a durable description of what a container is — it is not
the thing that acts on containers.

- **No policy.** The record stores no permission ceiling, no Comm-Gate rule, no retention
  rule (target arch §8).
- **No execution.** The record never starts, stops, restarts or recreates anything.
- **No binary.** The record is data on disk, never an executable or a service.
- **The record computes nothing.** It has no scheduling, GC, or drift-correction logic; the
  *moves* (target arch §3) read it and decide.
- **Temptation is the signal.** Any urge to add behaviour "while we're here" (auto-recreate,
  auto-GC, live sync) means the wrong problem is being solved — those are separate moves, not
  the record.

---

## 7. Docker Label Contract

**The record owns exactly one label on the container:**

```
thoughtmachine.container_id=<id>
```

- **Nothing else.** The record does not add labels beyond this single lookup key.
- **All lookups go through the record.** Given a label value, `find_by_docker_label(label_value)`
  (§3) resolves the record; callers do not re-encode state in labels.
- **The label is a lookup key, not the source of truth.** The `<id>.json` file is the source
  of truth; the label is only a pointer to it. If the two disagree, the record wins.

> **Conflict to resolve (flagged, §9).** "Exactly one label" collides with existing sweep
> machinery that **depends on `thoughtmachine.workspace_id`**:
> `ContainerManager.cleanup_workspace()` and `sweep_exited_workspace_containers()`
> (`infra/container_manager.py` ~L1749, L1850) filter by `thoughtmachine.workspace_id`, and
> resource containers are identified by `thoughtmachine.resource`. Reducing the contract to a
> single label would break those sweeps. Either the "one label" rule is scoped to *record
> labels* (leaving infrastructure labels intact) or the sweeps must be migrated first.

---

## 8. Non-Goals

Consistent with the target arch §8 fence, the Container Record subsystem does **not**:

- Introduce a database, ORM, or event-bus library.
- Add cross-host orchestration, auto-scaling, or a Kubernetes scheduler.
- Add a plugin system or any **new container types**.
- Enforce Comm-Gate / permissions now.
- Mutate a container's config in place, or persist a permission ceiling.
- Reconcile inferred fields against intent (target arch §5 — an explicit **out-of-scope**
  future decision).
- Retire the legacy registry/manager (target arch §7 — a separate later decision).

---

## 9. Open Questions for Main

1. **UUID vs short hash** — pick UUID4 or a deterministic short hash? (Blocks §2.2 and the
   migration idempotency strategy in §4.2.) Also confirm whether `id` may be redefined as
   record-owned at all, since target arch §2 defines `id` as the Docker container id (see §1.3).
2. **`state` field** — §2 lists `state` and boot-drift (§6) reads it, but the checklist omits
   it; do we keep `state`? (Blocks the §1 schema table.)
3. **`migrated` / `created_at` / `updated_at`** — §2 says "No other field exists"; name a
   reader for each or drop them? (Blocks §1.)
4. **Event-log location** — embedded `event_log` array vs sidecar `.events.jsonl`? (Blocks
   §2.4.)
5. **Lock semantics** — is `fcntl.flock` + bounded timeout acceptable given the Windows
   fallback loses cross-process exclusion? (Blocks §2.3.)
6. **Single-label contract** — does "exactly one label" supersede the existing
   `thoughtmachine.workspace_id` sweeps, or are the two label sets reconciled first? (Blocks
   §7.)
7. **Session signal** — since `thoughtmachine.session_id` does not exist, derive session from
   `thoughtmachine.worker`, or leave it empty? (Blocks §4.1.)
