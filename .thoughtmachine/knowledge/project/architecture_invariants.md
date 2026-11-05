# architecture_invariants — the rules that must not be re-derived

**Status:** read-on-entry index. Created Gen 58 (stabilization sprint).
**Owner:** Infra. **Cadence:** on earning — a rule enters here only after it has
been learned the hard way (a failed run, a red-team finding, or a debugging
session that cost a turn).
**Companions:** `KB_MAP.md` (where content lives), `MANDATORY_STOP.md` (the
read-on-entry list that points here).

## What this document is

A short list of **invariants** — properties that are already true, are enforced
somewhere specific, and will silently break if a future change re-derives them
differently. Each entry names:

- the invariant, in one sentence;
- **where it is enforced** (file:symbol, so it can be read and verified);
- **what breaks** when it is violated;
- a **pointer** to the long-form document, if one exists.

## What this document is not

It is **not** a design document and it is **not** a second home for any content
class. Per `KB_MAP.md`: *one class, one home*. This file owns exactly one class
— "cross-cutting invariants that a re-derivation would break" — and it
**points** at the design docs rather than restating them. If you find yourself
wanting to write a paragraph of design here, write it in the design doc and add
a pointer instead.

---

## 1. The container permission envelope

**Invariant.** A container's isolation envelope is `(network_mode,
workspace_mode)` resolved from the *session's effective permissions capped by
the on-disk workspace ceiling*. It is **threaded** through creation, naming,
mounting and drift — it is not hardcoded anywhere, and nothing may default it
open.

**Enforced at (single source of truth):**
`security/security_gate.py::resolve_container_config` — the one place the pair
is computed:

```python
fs = eff.get("filesystem", "read")
workspace_mode = "rw" if fs in ("write", "full") else "ro"   # :1360-1361
```

Both `network_mode` and `workspace_mode` travel **with the container**, by three
independent mechanisms, and all three must move together:

| Mechanism | Where | Consequence if it drifts |
|---|---|---|
| Container **name** (pool key) | `infra/containers/executor.py::build_container_name` (:264-290) — hashes `f"{network_mode}:{workspace_mode}"` into `env_hash` | a container born `ro` can never be reused by an `rw` session. The envelope is the container's *birth certificate*; changing the envelope means a **different container**, not a reconfiguration. |
| **Mount** `read_only=` | persistent: `infra/container_manager.py`:2249-2252 `read_only=(workspace_mode != "rw")` | a wrong value here is the actual write-denial/allowance — everything upstream is only a decision about this bit. |
| **Drift refusal** | `_start_drift_decision` / `_check_exec_drift` (`infra/container_manager.py`) | drift compares **live** attributes against current policy: more permissive than policy → **REFUSE**; less permissive → **WARN**. Never silently accept a wider container. |

**Long form:** `project/design_container_envelope.md` — what the envelope *is*
(the create-time `intent_snapshot`), which axes the gate compares, and the named
gap (mem/cpu/oom are in the *storage* envelope, not the *enforcement* envelope).
Also `project/design_container_trust_contract.md` (its §"Enforcement status" is
the live statement of what is enforced; design doc and trust contract move in
the same PR).

### 1a. The four fail-closed routes to `ro`

There are **four independent ways** a session that believes it has write access
gets `workspace_mode="ro"`. Know all four before debugging "the container is
read-only":

| # | Route | Where |
|---|---|---|
| 1 | **Workspace ceiling** downgrades session `write` → `read` (the on-disk ceiling wins over the session grant) | `security/security_gate.py::apply_workspace_ceiling` (:234) |
| 2 | **Missing/unparseable capabilities** → `_FAIL_CLOSED_CAPABILITIES` (`filesystem_write=False`, no docker, no network, no git). A *absent* workspace profile is the **most restrictive** profile. | `security/security_gate.py::_FAIL_CLOSED_CAPABILITIES` (:66-71), returned by `get_workspace_capabilities` (:114-117) |
| 3 | **The resolver raised** → `_compute_config` returns `("none", "ro")` (unless `strict=True`, in which case it raises) | `infra/container_manager.py::_compute_config` (:4901+) |
| 4 | **Falsy `workspace_id`** → `_resolve_container_config_via_gate` returns `("none", "ro")` **immediately, without consulting the gate at all** | `infra/containers/executor.py::_resolve_container_config_via_gate` (:293-344) |

Route 4 is the **unifier** and the one to check first. A stale or `None`
`workspace_id` produces *both* symptoms at once — the worker loses its
permission grain **and** the mount goes `ro` — which reads like two bugs and is
one. The gate is never consulted, so every log line from the gate is silent;
the only signal is the WARN at `infra/container_manager.py`:1997-2003
("Session grants filesystem=write but gate returned workspace_mode=ro —
fail-closed").

**Corollary.** When a session that was granted `filesystem=write` gets a
read-only mount, the question is never "is the mount code wrong". It is "which
of the four routes ran". `workspace_id` first, ceiling second, capabilities
third, exception last.

---

## 2. Host access: one chokepoint, checked at the point of action

**Invariant.** Every command that leaves the sandbox for the host passes through
`thoughtmachine/host_port.py::host_run`, which re-checks authority **at the
point of action** — not at the point where a tool author remembered to check it.
Safety does not depend on tools being programmed correctly.

**Enforced at:** `thoughtmachine/host_port.py` — the only module in
`thoughtmachine/` and `tools/` permitted to `import subprocess` without a pinned
exception. Pinned by `tests/test_tool_import_declaration_match.py` (AST scan;
`PINNED_IMPORTERS` pins the literal set of importers).

The guarantee is precisely: *every site is either through the chokepoint or
enumerated in `ALLOWED_EXCEPTIONS` with a named reason* — **not** "every site is
through the port". The pinning is the win; migration is the follow-up.

### 2a. The killswitch is fail-closed and shares one config read

The operator killswitch is the workspace config key `allow_host_resources`, and
it is checked **inside the port**, in fixed order, before anything is spawned:

- only JSON `true` enables host resources (**fail-closed**: absent, `false`,
  `"true"`, `1` → refused);
- the **same** `<vault>/workspaces/<id>/config.json` read supplies the `root`
  used for cwd containment (`_workspace_config`), so the killswitch and the cwd
  root can never disagree. Do not add a second config read for either.

### 2b. The grain is a category name

`grain` is `"host_bash"` / `"mcp"` / `"network"` — a **category name, never**
`"host_bash:allow"`. The caller states the authority it believes it holds; the
port reads the actual value from the session's effective permissions and
refuses a mismatch. `ask` is never an authorized value in `AUTHORIZED_VALUES`
(the port cannot prompt). See `docs/host-port.md` §4.

**Long form — read before touching host execution:** `docs/host-port.md`
- **§1** — the claim *and its two qualifications*: CI enforces the convention in
  the **honest case only** (it cannot catch a runtime-constructed `subprocess`
  reach), and the port checks **authority, not intent**.
- **§4** — the grain is a category name; `AUTHORIZED_VALUES` is the whole
  authorization surface; `ask` is never authorized.
- **§9** — two independent audit trails (`host_port_audit.jsonl` from the port,
  `host_bash_audit.jsonl` from the tool; they are *different records written by
  different code*) and **§9.3's named exclusions** — git remote transport
  (`GitReadTool._exec_host_raw`) and container git (`_exec_container_raw`) are
  **outside** the port's killswitch/credential-strip/audit coverage. Those are
  enumerated exclusions with reasons, not oversights. Do not claim host coverage
  that these two paths do not have.

---

## 3. How to add an invariant here

1. It has already broken something (or nearly did) — this is not a place for
   design intent.
2. Name the **enforcement site** as `file:symbol` (and a line range if it helps).
   An invariant with no enforcement site is a wish.
3. Name **what breaks** when it is violated. If you cannot, it is not an
   invariant.
4. **Point**, do not duplicate. If a design doc owns it, link the doc and the
   section.
5. Add the read-on-entry step to `MANDATORY_STOP.md` if a new engineer can get
   it wrong on day one — that is what makes this file load-bearing rather than
   decorative.
