# Gate Audit — Generation 64

**Scope:** fail-open / fail-closed behaviour of the permission-gate surface.
**Files audited:** `security/security_gate.py`, `security/gate_helpers.py`,
`security/resource_catalog.py`, `agent/core/tool_executor.py`,
`tools/host_bash_tool.py`, `thoughtmachine/host_port.py`, `tools/cancellation.py`.
**Method:** first-hand source reads (line numbers are from the current working tree
of `gen64-gate-audit`). No execution, no dynamic probing — verdicts are static.

---

## 0. IMPORTANT CAVEAT ON CLAIM WORDING

The audit brief / claim list was **not recoverable**: the claim text was not
recorded anywhere I could find. `.thoughtmachine/working_docs/` contains tickets
only, and `.thoughtmachine/knowledge/project/PRIORITIES.md` — which **does**
exist (196 lines) — is a pending-work queue, not the audit brief. Every claim
below is therefore a **RECONSTRUCTED PARAPHRASE** of the
original wording, derived from my own record of the task. Verdicts are given
against the *paraphrase*; if the original claim was worded differently the
verdict may not transfer verbatim. Line references are authoritative — they can
be re-checked independently of the wording.

---

## A. `security/security_gate.py`

| # | Claim (RECONSTRUCTED PARAPHRASE) | Verdict | Evidence |
|---|---|---|---|
| A1 | The module defines deny-all fallback constants used when the disk/gate state cannot be trusted. | **CONFIRMED** | `_FAIL_CLOSED_CAPABILITIES` L66-71; `_DISK_FAIL_CLOSED_SESSION` L~75-90 (every category → banned); `_DISK_FAIL_CLOSED_CEILING`. |
| A2 | If the session-permission file cannot be read, the gate fails closed to deny-all rather than allowing. | **CONFIRMED — CLOSED** | `get_effective_permissions` disk read wrapped in `except Exception` at L981 → deny-all, warning emitted, `fail_closed_reason` set. |
| A3 | If the on-disk grant map cannot be coerced, the gate fails closed. | **CONFIRMED — CLOSED** | Grant coercion `except Exception` L1011 → deny-all. |
| A4 | If the workspace ceiling cannot be evaluated, the gate fails closed. | **CONFIRMED — CLOSED** | Ceiling-unusable `except Exception` L1063 → deny-all. |
| A5 | `apply_workspace_ceiling` ignores an unrecognised ceiling **resource** name. | **CONFIRMED — FAIL-OPEN (intentional)** | Ceiling L~250-400: unknown ceiling resource is logged and ignored; the session value stands. Documented as deliberate "forward-compatible" behaviour (L264-268, L364-372). |
| A6 | `apply_workspace_ceiling` ignores an unrecognised ceiling **value** (level). | **CONFIRMED — FAIL-OPEN (intentional)** | Same block, L364-372: unknown level → logged, ignored, session value stands. |
| A7 | A workspace ceiling for the `ci` capability is silently a no-op. | **CONFIRMED — DEFECT** | `_WORKSPACE_RESOURCE_MAP` omits `ci`, while `WORKSPACE_CEILING_VOCAB` in `security/resource_catalog.py` L210-217 *does* include `"ci": ("banned","read")`. An in-code comment acknowledges this as a known pre-existing defect. → **C52**. |
| A8 | Unknown permission levels inside a footprint are treated as maximum privilege by `_min_permission`. | **REFUTED (consequence-neutral)** | `_min_permission` does use `_LEVEL_MAP.get(..., 2.0)` for unknown levels, **but** footprint values are validated against `PERMISSION_SCHEMA` *before* this is reached, so an unknown level ends as `effective[cat] = False`. Net effect is closed. |
| A9 | Unknown footprint categories or levels in `check_required_categories` resolve to deny. | **CONFIRMED — CLOSED** | L1494-1671: unknown footprint category/level → `False`. |
| A10 | A requirement with no `":"` separator causes that requirement to be skipped rather than denied. | **CONFIRMED — FAIL-OPEN** | L1570-1571: bare requirement → `continue`. A tool declaring `required_categories=["host_bash"]` (no colon) would gate **nothing** for that entry. No tool currently does this, but the parse is not fail-closed. → **C50**. |
| A11 | A category absent from the effective map denies. | **CONFIRMED — CLOSED** | Absent category → `False`. |
| A12 | `allowed is None` denies. | **CONFIRMED — CLOSED** | L1576-1577. |
| A13 | `_value_satisfies` returning False denies. | **CONFIRMED — CLOSED** | Explicit deny branch. |
| A14 | An `ASK` verdict is denied whenever no interactive channel exists. | **CONFIRMED — CLOSED** | L1623: deny when `event_bus is None or is_worker_context or isinstance(event_bus, NullEventBus)`. Otherwise prompt, then `response.get("approved", False)`; timeout → deny. |
| A15 | `check_required_categories` mutates the caller's `effective` mapping in place for the worker footprint. | **CONFIRMED** | Writes at L1555 and L1564 into the caller-supplied dict. Callers must not reuse the dict. |
| A16 | Unknown resources in `check_requires_resource` deny. | **CONFIRMED — CLOSED** | L1440-1486. |
| A17 | `check_atomic_operation` denies unknown categories and never silently allows ASK. | **CONFIRMED — CLOSED** | L1386-1420: unknown category → `False`; `ASK` → deny by policy. |
| A18 | A `git:write` grant on a feature branch does not satisfy a plain `git:write` requirement. | **CONFIRMED — CLOSED** | `_git_split` L~460-494 maps `write_on_feature_branch` → `("read", "write_on_feature_branch")`, so the `write` half stays denied. Unknown level → `(False, False)`. |
| A19 | The module contains an environment or global kill switch that can disable gating. | **REFUTED** | No env/global kill switch found in `security_gate.py`. |

---

## B. `security/gate_helpers.py` and `security/resource_catalog.py`

`gate_helpers.py` is small (4975 bytes) and was read in full.

| # | Claim (RECONSTRUCTED PARAPHRASE) | Verdict | Evidence |
|---|---|---|---|
| B1 | A boolean grant satisfies a requirement only when it is exactly `True`. | **CONFIRMED — CLOSED** | `_value_satisfies` L37-100: `allowed is True`. |
| B2 | An `ask` grant silently satisfies low-risk requirements but escalates high-risk ones. | **CONFIRMED** | `"ask"` allowed → `True` if required rank ≤ `_ASK_SILENT_MAX_RANK` (= `max(banned, read, ask)` = 2), else returns `"ASK"`. |
| B3 | If either side of a level comparison is unknown, the helper falls back to exact string equality. | **CONFIRMED — CLOSED** | L96-98: `allowed_str == required_lower`. |
| B4 | A `full` grant does **not** satisfy `host_bash:allow`. | **CONFIRMED — CLOSED (deliberate)** | `allow` is not a member of `GRANT_LEVEL_RANKS`, so the exact-string fallback applies; only a literal `"allow"` satisfies it. Note the assymmetry is a surprise for operators, but it errs on the safe side. |
| B5 | Network mode resolution fails closed for anything unrecognised. | **CONFIRMED — CLOSED** | `resolve_network_mode` L103-118: `True` / `"write"` / `"outbound"` → `"bridge"`; everything else, including unknown values, → `"none"`. |
| B6 | There is a single shared vocabulary plus separate rank tables for grants, ceilings and git breadth. | **CONFIRMED** | `security/resource_catalog.py`: `GRANT_LEVEL_RANKS` L155-166; `WORKSPACE_CEILING_LEVELS_RANKS` L180-192; `WORKSPACE_CEILING_VOCAB` L210-217; `GIT_BREADTH_RANKS` L233+ (used by the `vault_repair` `git_read`/`git_write` fold). |

---

## C. `agent/core/tool_executor.py`

| # | Claim (RECONSTRUCTED PARAPHRASE) | Verdict | Evidence |
|---|---|---|---|
| C-1 | Tool arguments come straight from the model's input dict. | **CONFIRMED** | L459 `tool_args = arguments.copy()`; L795 `tool_instance = tool_class(**tool_args)`. |
| C-2 | Workspace identity has a defined precedence chain ending in a path-derived resolution. | **CONFIRMED** | L543-555: explicit arg → store `load_session_workspace_id` → `_resolve_workspace_id_from_session` → `resolve_workspace_id(workspace_path)`. |
| C-3 | If a workspace path is set but no workspace id can be resolved and the tool is gated, the call is denied. | **CONFIRMED — CLOSED** | L566-572: fail-closed deny. |
| C-4 | Effective permissions are recomputed per tool call, scoped to the resolved workspace. | **CONFIRMED** | L588 `get_effective_permissions(session_perms_obj, caps, session_id, workspace_id=ws_id)`. |
| C-5 | When no workspace id exists, the disk fail-closed session is used. | **CONFIRMED — CLOSED** | L602-604 → `get_effective_permissions(_DISK_FAIL_CLOSED_SESSION, caps)`. |
| C-6 | The category check receives the model's raw arguments as context. | **CONFIRMED** | L606-620 passes `tool_args=arguments`, plus `event_bus=self._event_bus or global_event_bus` and `is_worker_context=...`. |
| C-7 | A failed category check denies; resource checks run separately and also deny. | **CONFIRMED** | L663-664 deny on `not ok`; L671-678 `check_requires_resource`. |
| C-8 | The gate injects the authoritative effective-permission map into the tool arguments. | **CONFIRMED** | L697-701 (gate path) overwrites **both** `tool_args['effective_permissions'] = effective` and `tool_args['workspace_id'] = ws_id`. |
| C-9 | In the non-gate branch an LLM-supplied `workspace_id` / `effective_permissions` argument can reach the tool model unmodified. | **CONFIRMED — RESIDUAL RISK** | L700-701 vs the `else` branch: the else branch sets `effective_permissions = {}` but does **not** set `workspace_id`. So when `GATE_AVAILABLE` is False or `session_perms_obj` is None, a model-supplied `workspace_id` survives into any tool model that declares that field. → **C54**. |
| C-10 | Injected `agent_config` carries no `workspace_id`. | **CONFIRMED** | L705-720: the injected dict has no `workspace_id` key. This matters for D3/C53. |
| C-11 | Worker-context flag is injected from the executor's own state, not from model input. | **CONFIRMED — CLOSED** | L742. |
| C-12 | Permission-widening telemetry can never break a tool call. | **CONFIRMED** | L839-897 `_record_permission_widening` entirely inside `try/except Exception: pass` — good for availability, but it is also *silently* lossy as observability. |

---

## D. `tools/host_bash_tool.py`

| # | Claim (RECONSTRUCTED PARAPHRASE) | Verdict | Evidence |
|---|---|---|---|
| D1 | The tool requires the `host_bash:allow` category. | **CONFIRMED** | `required_categories = ["host_bash:allow"]`. |
| D2 | Its docstring line references are stale. | **CONFIRMED — DOC DRIFT** | Docstring cites `security_gate.py:1483-1485`; the actual bare-requirement code is at L1570-1571. → **C55**. |
| D3 | The tool resolves its own workspace id instead of using the injected one. | **CONFIRMED — DIVERGENCE RISK** | `ws_id = self._workspace_id()` prefers `agent_config['workspace_id']` (absent per C-10), then `SessionRegistry`; it **never** reads the injected `self.workspace_id`. The gate's decision (C-8, gate path) and the tool's host-port identity can therefore be computed from different workspace ids. → **C53**. |
| D4 | A missing grain is treated as banned, and a grain that is neither `ask` nor `allow` is denied. | **CONFIRMED — CLOSED** | `grain = (self.effective_permissions or {}).get("host_bash")`; `None` → `"banned"`; not in `("ask","allow")` → deny. |
| D5 | `ask` in a worker context denies without prompting. | **CONFIRMED — CLOSED** | Explicit branch; no prompt issued for workers. |
| D6 | Timeouts are environment-tunable. | **CONFIRMED** | `HOST_BASH_TIMEOUT` via `int()`, `HOST_BASH_APPROVAL_TIMEOUT` via `float()`. |
| D7 | Malformed timeout env values raise rather than silently defaulting, and env can *extend* timeouts without bound. | **CONFIRMED — DEFECT (minor)** | `int()` / `float()` raise `ValueError` on garbage; nothing clamps an oversized value. → **C56**. |
| D8 | Audit logging never blocks command execution. | **CONFIRMED (by design)** | `_audit_log` catches `except Exception`. This is availability-correct but means audit loss is silent. |
| D9 | Argument redaction is best-effort only. | **CONFIRMED** | `_redact_command` masks `os.environ` values of length ≥ 6; anything shorter, or not present in the environment, is logged verbatim. |
| D10 | Approval transport failures deny rather than allow. | **CONFIRMED — CLOSED** | `_request_approval`: publish failure → `"denied"`; `queue.Empty` → `"timeout"` (both deny). |

---

## E. `thoughtmachine/host_port.py`

| # | Claim (RECONSTRUCTED PARAPHRASE) | Verdict | Evidence |
|---|---|---|---|
| E1 | `host_run` performs its checks in a fixed, documented order. | **CONFIRMED** | Order: grain → ids → `_check_identity` → `config['allow_host_resources'] is True` → grant map → mcp `client_type` → credential env → cwd → argv. |
| E2 | The audit record is written **before** the process is spawned and is not re-checked afterwards. | **CONFIRMED** | Pre-audit before `Popen`; no post-spawn re-validation. |
| E3 | Audit write failures never raise and never block the caller. | **CONFIRMED (by design)** | `_write_audit` swallows all exceptions. |
| E4 | `_check_identity` allows the call when no identity record exists but a permissions mapping was supplied. | **CONFIRMED — FAIL-OPEN** | `_check_identity`: `record is None` **and** `effective_permissions is not None` → `return` (allowed). Combined with E5 this means a caller-supplied mapping is self-certifying. → **C58**. |
| E5 | `_resolve_permissions` trusts the caller's mapping without validation. | **CONFIRMED** | `dict(effective_permissions)` — no schema check, no rank check. |
| E6 | Only the literal `"allow"` is authorised for `host_bash`. | **CONFIRMED — CLOSED** | `AUTHORIZED_VALUES["host_bash"] == {"allow"}`. |
| E7 | `host_bash_tool.py` is the only production caller of the host-port chokepoint. | **CONFIRMED** | Sole production call site: `tools/host_bash_tool.py` L318-327. |
| E8 | MCP stdio commands bypass the chokepoint. | **CONFIRMED — BYPASS** | `tools/mcp_client.py` still spawns raw `subprocess` for MCP stdio; migration to `host_port` is pending. Any MCP stdio server therefore runs outside the audited chokepoint. → **C57**. |

---

## F. `tools/cancellation.py`

| # | Claim (RECONSTRUCTED PARAPHRASE) | Verdict | Evidence |
|---|---|---|---|
| F1 | Cancellation state is purely per-thread and carries no permission semantics. | **CONFIRMED** | `threading.local` + `Event`; `is_interrupted()` = `event.is_set()`, `cancel_reason()` = `tls.reason`. No permission logic anywhere in the module. |
| F2 | Every consumer uses cancellation for control flow only, never as a grant. | **CONFIRMED** | `infra/container_manager.py` L2591-2641; `infra/resource_container_manager.py` L1611-1668 (wait loops → interrupted payload); `tools/workspace/worker_thread.py` L1802-1810 and L7196-7201; `agent/core/tool_executor.py` `is_interrupted_tool_entry` L164 (+ `agent.py` L2204). All are early-exit / abort paths. |
| F3 | Cancellation cannot be used to *obtain* a permission. | **CONFIRMED** | No grant path references cancellation state. |

---

## 1. Consolidated fail-open register

Items marked **intentional** are documented in-code as design decisions and are
recorded here for completeness rather than as defects to fix.

| ID | Location | Behaviour | Class |
|---|---|---|---|
| FO-1 | `security_gate.apply_workspace_ceiling` L~250-400 | Unknown ceiling **resource** → ignored, session value stands | Intentional (forward-compat) |
| FO-2 | `security_gate.apply_workspace_ceiling` L364-372 | Unknown ceiling **value** → ignored, session value stands | Intentional (forward-compat) |
| FO-3 | `security_gate.check_required_categories` L1570-1571 | Bare requirement (no `":"`) → `continue`, gates nothing | **Latent defect → C50** |
| FO-4 | `_WORKSPACE_RESOURCE_MAP` | `ci` ceiling is a silent no-op | **Defect → C52** |
| FO-5 | `agent/core/tool_executor.py` else-branch L700-701 | Model-supplied `workspace_id` survives when gating is off | **Residual risk → C54** |
| FO-6 | `tools/host_bash_tool.py` `_workspace_id()` | Ignores injected `self.workspace_id` | **Divergence risk → C53** |
| FO-7 | `thoughtmachine/host_port.py` `_check_identity` | Caller-supplied `effective_permissions` self-certifies when no record exists | **Fail-open → C58** |
| FO-8 | `tools/mcp_client.py` stdio | Raw `subprocess`, bypasses `host_port` | **Bypass → C57** |
| FO-9 | `tools/host_bash_tool.py` timeouts | Env-tunable, unbounded, malformed value raises | **Minor → C56** |
| FO-10 | `tools/host_bash_tool.py` docstring | Stale `security_gate.py:1483-1485` | **Doc drift → C55** |

---

## 2. New findings — proposed tickets C50–C58

Slot numbering continues from the **post-merge** register, not from the C36
currently at the tail of this worktree's `PRIORITIES.md`. Two things land ahead
of this document: **C38–C44** are added to `PRIORITIES.md` by a separate branch
that merges first, and **C45–C49** are the in-flight tickets. C50–C58 therefore
begins at the first slot free in the merged queue, and **C49 must stay free**
for the pending operator item. Do not renumber these findings down into
C37–C49.

### C50 — Bare requirement entries in `required_categories` gate nothing
`security/security_gate.py` L1570-1571: a requirement string with no `":"` hits
`continue`. A typo in a tool's `required_categories` (e.g. `"host_bash"` instead
of `"host_bash:allow"`) silently disables that requirement. No current tool
declares a bare requirement, so this is latent, not active.
**Fix:** treat a malformed requirement as deny (or raise at class-decoration
time). Severity: medium.

### C51 — `apply_workspace_ceiling` fails open on unknown resource/value
L~250-400 and L364-372. Unknown ceiling resources and unknown ceiling levels are
logged and ignored, so the session's own (potentially wider) value survives.
Documented as intentional forward-compatibility. **Recommendation:** keep the
forward-compat behaviour for *unknown* names but emit a structured warning that
surfaces in the permission-widening telemetry, so an operator can see that a
ceiling failed to apply. Severity: low (by design), observability gap.

### C52 — `ci` workspace ceiling is a silent no-op
`_WORKSPACE_RESOURCE_MAP` has no `ci` entry although
`security/resource_catalog.py` `WORKSPACE_CEILING_VOCAB` L210-217 declares
`"ci": ("banned", "read")`. Setting a `ci` ceiling therefore validates as legal
and then does nothing. In-code comment already flags it as a known pre-existing
defect. **Fix:** add `ci` to `_WORKSPACE_RESOURCE_MAP`. Severity: medium.

### C53 — `host_bash_tool._workspace_id()` ignores the injected workspace id
`tools/host_bash_tool.py`: `ws_id = self._workspace_id()` prefers
`agent_config['workspace_id']` — a key that the executor never injects
(`tool_executor.py` L705-720) — then falls back to `SessionRegistry`; it never
reads the injected `self.workspace_id`. The gate resolves the policy workspace
through a four-step precedence chain (L543-555) which can legitimately differ
from `SessionRegistry`. Result: the gate can authorise against workspace *X*
while `host_port` records/enforces identity for workspace *Y*.
**Fix:** prefer the injected `self.workspace_id`, falling back to the current
chain. Severity: medium-high (policy/audit identity divergence).

### C54 — Non-gate branch leaves an LLM-supplied `workspace_id` intact
`agent/core/tool_executor.py` L697-701 vs the `else` arm: the gate path
overwrites both `effective_permissions` and `workspace_id`; the non-gate path
sets `effective_permissions = {}` but does **not** neutralise `workspace_id`.
When `GATE_AVAILABLE` is False or `session_perms_obj` is None, a model-supplied
`workspace_id` argument reaches any tool model that declares that field.
**Fix:** unconditionally strip/overwrite `workspace_id` (and
`effective_permissions`) in `tool_args`, or refuse to run gated tools at all in
that branch. Severity: medium (only reachable with the gate unavailable).

### C55 — Stale line references in `host_bash_tool` docstring
Docstring cites `security_gate.py:1483-1485`; the logic now lives at
L1570-1571. **Fix:** reference the symbol (`check_required_categories`
bare-requirement branch) rather than line numbers. Severity: trivial.

### C56 — Environment-tunable host_bash timeouts
`HOST_BASH_TIMEOUT` via `int()` and `HOST_BASH_APPROVAL_TIMEOUT` via `float()`.
Malformed values raise `ValueError` out of configuration parsing, and nothing
clamps an oversized value — an environment variable can extend both the command
and the approval timeout without bound.
**Fix:** parse defensively with a documented default, and clamp to a sane
maximum. Severity: low.

### C57 — MCP stdio bypasses the `host_port` chokepoint
`tools/mcp_client.py` still spawns MCP stdio servers with raw `subprocess`,
skipping `host_run`'s grain check, identity check, `allow_host_resources` gate
and pre-audit. Migration to `host_port` is marked pending in the source.
Severity: medium-high (an entire process-spawn path is outside the audited
chokepoint).

### C58 — `host_port._check_identity` trusts caller-supplied permissions
`_check_identity` returns (allows) when `record is None` and
`effective_permissions is not None`; `_resolve_permissions` then does
`dict(effective_permissions)` with no schema or rank validation. Any in-process
caller that passes a mapping self-certifies. Today the only production caller is
`tools/host_bash_tool.py` L318-327, which derives the mapping from the gate — so
this is not currently exploitable from model input, but the chokepoint's
guarantee is weaker than it appears and would break the moment a second caller
is added (cf. C57).
**Fix:** require a validated/authoritative permissions object (or a signed
identity record) rather than an arbitrary mapping. Severity: medium.

---

## 3. Verification limits

- **Claim wording is reconstructed.** The original brief/claim text could not be
  located in this worktree. Section verdicts apply to the paraphrases in the
  tables, not to verbatim original wording.
- **Static analysis only.** No execution, no adversarial probing, no attempt to
  actually trigger the fail-open paths at runtime.
- **Caller enumeration.** The "only production caller" claim for `host_port`
  (E7) is based on a repository-wide search at audit time; new or generated
  callers outside the tracked tree would not be covered.
- **Line numbers** refer to this worktree's `gen64-gate-audit` branch and will
  drift with edits; symbols were preferred where possible.
- **`_min_permission`**: the raw default is `2.0` (maximum), which *looks*
  fail-open but is neutralised by earlier `PERMISSION_SCHEMA` validation. Anyone
  refactoring that validation away would reintroduce a real fail-open hole.
