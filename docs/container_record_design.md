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
9. [Questions Resolved](#9-questions-resolved)

---

## 1. Record Schema

The schema is sourced from the target arch §2 field table. One record is **one JSON file
per container** (target arch §10). The authoritative field list is:

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `id` | string | yes | — (UUID4, generated at create) | **Record-owned** opaque identifier; stable across container recreate. See §2.2. |
| `docker_id` | string | yes | `""` | Current Docker container id; empty on an intent record before create. |
| `lifecycle_class` | string enum | yes | — | One of the four classes in target arch §3 move5: `ephemeral`, `persistent`, `resource`, `service`. |
| `owner` | string enum | yes | — | `workspace-owned` \| `system-owned` (target arch §2). |
| `purpose` | string | yes | `""` | Human-readable reason the container exists. |
| `intent_snapshot` | object | yes | `{}` | The config the container was **created with** — drift reference (target arch §2, §6). Nested fields below. |
| `notes` | string | yes | `""` | Free-text operator notes. Replaces the shared `container_notes.json` (target arch §2, §3 move1). |
| `event_log` | array | yes | `[]` | Append-only lifecycle + drift events (target arch §2, §3 move6). Entry shape below. |
| `schema_version` | int | yes | `0` | `0` for records synthesised from live legacy state (target arch §2, §5). |
| `inferred` | bool | yes | `false` | `true` when the record was synthesised from live Docker state rather than authored (target arch §2, §5). |
| `state` | string | yes | `""` | Last observed container state; read by boot-drift (target arch §6). |
| `created_at` | string (ISO-8601) | yes | now | Record creation time; read by UI ordering in `list_records()` (§1.3). |
| `updated_at` | string (ISO-8601) | yes | now | Last mutation time; read by staleness display in drift events (§1.3). |

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

**Embedded with a size ceiling.** The log stays embedded in `<id>.json` while it is below
**256 KiB**; once an append would push the file past that threshold, the log moves to a
sidecar `<id>.events.jsonl` (see §2.4). This keeps the common case self-contained without
letting the record grow without bound.

### 1.3 Field list, with readers

Every field has a named reader; nothing is carried without one (target arch §10). This is the
settled field list — target arch §2's "No other field exists" and §10's "A field that no move
reads does not exist" are satisfied:

| Field | Reader |
|---|---|
| `id` | every function (§3) |
| `docker_id` | create, start, `find_by_docker_label` (§3) |
| `lifecycle_class` | restart policy, admission gate, GC (target arch §3 moves 4–5) |
| `owner` | UI display, agent ownership |
| `purpose` | UI display |
| `intent_snapshot` | drift check (target arch §6) |
| `notes` | UI, agent handoff |
| `event_log` | UI, audit |
| `schema_version` | migration, integrity |
| `inferred` | integrity (provenance), migration |
| `state` | boot-drift (target arch §6) |
| `created_at` | UI ordering (`list_records`) |
| `updated_at` | UI staleness (drift events) |

`id` is record-owned and opaque (UUID4, §2.2); the Docker container id lives in the separate
`docker_id` field. Synthesis provenance is fully expressed by `schema_version >= 1` together
with `inferred`; no separate flag is carried (see §9 Q3).

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

### 2.2 The `<id>` token — record-owned UUID4 (settled)

**Decision: `<id>` is a record-owned, opaque UUID4 string.**

- **Why.** The record owns its own identity: no coordination or collision key is required, the
  id is generated without a central counter, it stays stable across container recreate, and it
  leaks no coordinates (not derivable from the Docker id, name, or workspace). It is also the
  natural primary key for the label contract in §7 — the value must be a token the record, not
  Docker, owns. The Docker container id is carried separately in `docker_id` (§1).
- **Rejected alternative — short hash** (e.g. `sha256(docker_id)[:12]`). Deterministic, so
  migration could re-derive an id without a log — but it *derives from an external key*, so
  it changes if the container is recreated, and it leaks the Docker id's coordinates into
  the filesystem path. Rejected (settled).

### 2.3 Lock file (settled)

**Decision: a sidecar lock `<id>.json.lock` held via `fcntl.flock(..., LOCK_EX)` with a
bounded timeout; on timeout raise (fail closed).**

- The lock is acquired around every read-modify-write of `<id>.json` (§3).
- **Windows is best-effort, not a target.** `fcntl` is POSIX-only. Windows is explicitly
  **not** a target platform for the record subsystem, so on Windows the design degrades to
  the atomic write (`temp file + os.replace`) **alone** — crash-safety but **no cross-process
  exclusion**. This is documented as best-effort, not a supported guarantee.
- **Windows meaning:** without a lock, concurrent writers can lose updates; atomic replace
  bounds the damage to "last writer wins" and never a torn file.

### 2.4 Event-log location — embedded, with a 256 KiB ceiling (settled)

**Decision: the event log is the embedded `event_log` array inside `<id>.json`, until it
exceeds 256 KiB; above that threshold it moves to a sidecar `<id>.events.jsonl`.**

- Target arch §2 models `event_log` as a field of the record, and §3 move6 / §6 describe the
  record as carrying the append-only log. Embedding keeps the record self-contained.
- **Threshold.** While the log is below **256 KiB** it stays embedded (§1.2). Once an append
  would push the file past 256 KiB, the log moves to a sidecar `<id>.events.jsonl` and the
  embedded `event_log` becomes a pointer to it. This bounds the record file size without
  splitting the common (small) case.
- **Tradeoff:** appending rewrites the whole file — the same class of race as the
  `container_notes.json` lost-update defect (audit probe-v2: `LOST_UPDATES=19 of 24`). The
  lock (§2.3) plus atomic replace is therefore **mandatory**, not optional.
- **Rejected alternative — always-sidecar `.events.jsonl`.** Append-only and cheap, but it
  splits the record for the common case and invites divergence. Rejected (settled); the
  sidecar is used only above the 256 KiB ceiling.

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
4. Synthesise the record: `schema_version: 0`, `inferred: true`,
   `lifecycle_class`/`owner` derived from container type (§4.1), `intent_snapshot` = the
   **partial** snapshot recoverable from inspect (missing keys left empty — never
   fabricated), `event_log` seeded with a `synthesised` event.
5. **Write-ahead:** append `{docker_id, record_id, ts}` to `migrations.log` and `fsync`
   **before** writing the record file.
6. Write `<id>.json` atomically (temp + `os.replace`).
7. Continue to the next container; repeat per workspace.

### 4.1 Label reality check (settled)

The production Docker label set is `thoughtmachine.workspace_id` / `thoughtmachine.worker` /
`thoughtmachine.container_type` / `thoughtmachine.resource`. `thoughtmachine.session_id` is
**not** a production Docker label: it appears only as a test mock
(`tests/docker_integration/test_startup_check.py:312`) and as an unrelated environment
variable (`infra/container_env.py:18`). Migration therefore derives session from
`thoughtmachine.worker` when present — session owner is encoded there as
`thoughtmachine.worker = <session_id or 'unknown'>:<worker_name>` — and leaves it empty when
the label is absent; it never assumes `thoughtmachine.session_id` exists. Container type is
carried by `thoughtmachine.container_type` (`free_use` / `resource`), and resource
containers are identified by `thoughtmachine.resource`.

### 4.2 Idempotency — write-ahead log under UUID4

Because `id` is a record-owned opaque UUID4 (§2.2), a migration re-run cannot re-derive the
id from the container. The durable **write-ahead** entry in `migrations.log` closes this gap:

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

**The record owns exactly one *record* label on the container:**

```
thoughtmachine.container_id=<id>
```

- **One record-owned label.** The *record* system adds exactly this single lookup key
  (`thoughtmachine.container_id`, value = the record's `id`). It adds no other record label.
- **Existing infrastructure labels remain (dual-label transition).** The infra labels current
  readers depend on — `thoughtmachine.workspace_id`, `thoughtmachine.resource`,
  `thoughtmachine.container_type` — stay on the container during the transition. They are
  consumed today by `ContainerManager.cleanup_workspace()` (`infra/container_manager.py`
  label filter ~L1748–1750) and `sweep_exited_workspace_containers()` (`_WORKSPACE_LABEL`
  defined ~L1833 and applied ~L1900, skipping `_RESOURCE_LABEL` ~L1859), and are defined in
  `agent/config/defaults.py:118` (`RESOURCE_LABEL = "thoughtmachine.resource"`) and `:120`
  (`WORKSPACE_ID_LABEL = "thoughtmachine.workspace_id"`). The record label and the infra
  labels coexist; nothing breaks.
- **All lookups go through the record.** Given a label value, `find_by_docker_label(label_value)`
  (§3) resolves the record; callers do not re-encode state in labels.
- **The label is a lookup key, not the source of truth.** The `<id>.json` file is the source
  of truth; the label is only a pointer to it. If the two disagree, the record wins.

**Retirement criteria for the infrastructure labels.** The infra labels may be dropped only
once **all three** hold:

1. Every container has a record with `schema_version >= 1` **and** `inferred: false` (natively captured, not synthesised).
2. **Both** sweeps — `cleanup_workspace()` and `sweep_exited_workspace_containers()` — read
   the record instead of the label, verified by a test.
3. Four weeks of production with no orphan found by a record-only sweep.

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

## 9. Questions Resolved

Each question below was open at draft time. All seven are now **resolved**; the list is kept
for history.

1. **UUID vs short hash** — **RESOLVED: UUID4.** `id` is a record-owned opaque UUID4 string
   (§2.2); the Docker container id lives in the separate `docker_id` field (§1). See §1.3 /
   §2.2.
2. **`state` field** — **RESOLVED: keep `state`.** It is in the §1 field list; its reader is
   boot-drift (target arch §6). See §1.3.
3. **`migrated` / `created_at` / `updated_at`** — **RESOLVED: drop `migrated`; keep
   `created_at` and `updated_at`.** `migrated` was redundant with `schema_version >= 1` +
   `inferred`; `created_at` is read by UI ordering in `list_records()` and `updated_at` by
   staleness display in drift events. See §1.3.
4. **Event-log location** — **RESOLVED: embedded `event_log`, with a 256 KiB ceiling.** Above
   256 KiB the log moves to a sidecar `<id>.events.jsonl`. See §1.2 / §2.4.
5. **Lock semantics** — **RESOLVED: accept `fcntl.flock` with a bounded timeout.** Windows is
   best-effort and explicitly not a target platform. See §2.3.
6. **Single-label contract** — **RESOLVED: the "one label" rule is scoped to record-owned
   labels only.** Infra labels stay during the transition; retirement criteria are in §7. See
   §7.
7. **Session signal** — **RESOLVED: derive session from `thoughtmachine.worker` when present;
   empty otherwise.** `thoughtmachine.session_id` is not a production label. See §4.1.
