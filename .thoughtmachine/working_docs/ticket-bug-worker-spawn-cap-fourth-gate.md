# Ticket: bug/worker-spawn-cap-fourth-gate

**Filed:** 2026-10-19 (Gen 46), from a field report: a worker sub-agent stayed
locked out after its session was granted `filesystem: write`.
**Severity:** HIGH — trust-layer asymmetry. The gate DENIES correctly but the
effective set is narrower than doctrine says, silently, and the UI/effective-
permissions display reports the grant that the worker cannot use.
**Status:** FIXED in tree (uncommitted at filing) — see "Fix shape". Pinned by
`tests/test_worker_permission_grant_immediate.py` (new) and
`tests/test_worker_disk_mode_inheritance.py` (rewritten premise).

## Finding

Doctrine says permissions are resolved through **three** gates on every tool
call, from disk:

    declared grant  x  workspace ceiling  x  (worker) declared footprint

The grant is the session's sidecar, re-read via
`permission_store.read_session_permissions()` on each call. The ceiling is the
workspace ceiling. The footprint is the worker definition's own declared cap
(`permission_footprint`, legacy `worker_permissions`).

**There was a fourth, undocumented gate: the spawn-time snapshot.**
`WorkerThread._build_agent_config` computed

    merged = _restrictive_merge(self._session_permissions, self._permission_footprint)
    worker_cfg["session_permissions"] = _canonicalize_session_permissions(merged)

(`tools/workspace/worker_thread.py:1677-1682`). That merged dict was frozen
into `AgentConfig.session_permissions` at spawn, and then handed to
`security_gate.check_required_categories` as the worker's
`permission_footprint` cap. So the worker's effective set was

    min(spawn-time-session-snapshot, declared footprint, live_session, ceiling)

— one term too many, and it is the only term that is not resolved from disk.

The two directions therefore behaved **asymmetrically**:

- **Revocation was immediate.** The stale snapshot was usually *wider* than the
  new disk grant, so the live read dominated and the worker lost access at
  once. This is a coincidence of direction, not a design.
- **Grant was not immediate.** After a grant the stale snapshot was
  *narrower*, so it won. The worker stayed denied until it was respawned. The
  documented workaround was "respawn the worker with `force=True`" — i.e. the
  worker, not the session, was the unit of permission refresh. Nothing in the
  doctrine says that.

Worse, a *declared* footprint narrower than the session is legitimate and must
keep capping. The bug was that an *accidental* spawn-time mirror of the
session was being treated with the same authority as a declaration.

## Root cause (historical)

`_restrictive_merge` predates disk-authoritative permission mode. Back when the
session's `AgentConfig.session_permissions` **was** the only authority in the
process, merging session x footprint at spawn and using the result as the
worker's permissions was correct: there was nothing to re-read. When grants
moved to the sidecar with per-call re-reads, the session's mirror became a
cache and the merge call site was never revisited. It kept its old meaning
("here is the worker's permissions") while acquiring a new one ("here is the
cap on the live resolution"), and the two disagree the moment a grant lands.

## Impact

- A granted worker stays locked out until respawn. Silent: the tool simply
  denies with a footprint-shaped message that names a permission the session
  does hold.
- Any display that shows a worker's effective permissions from its
  `AgentConfig` lies for the worker's whole lifetime. Compare
  `hygiene/panel-host-bash-display` — same "displayed != enforced" family, but
  the reverse direction (here the display is wider than what is enforced).
- Auditability: a widening that the operator intended left no trace, so
  "why is this worker still denied?" had no answer on disk.

## Fix shape

Separate the two facts that were collapsed into one dict:

1. **The declared cap is its own field.**
   `agent/config/models.py:136-148` — `permission_footprint: Dict[str, Any]`,
   empty = "no declared cap". Its docstring states the rule:
   `effective = min(permission_footprint, live_session - workspace_ceiling)`.
2. **The spawn-time merge still happens, but is only a mirror.**
   `tools/workspace/worker_thread.py:757-768` (`_restrictive_merge`
   docstring: "never treat this function's result as the authority"),
   `:891` (read the declared footprint from the definition, legacy alias
   included), `:1684-1692` (`worker_cfg["permission_footprint"]` passed
   unmerged and un-canonicalized — explicitly *not* folded into
   `session_permissions`). The mirror itself (`:1677-1682`) is untouched: it
   is still what the worker's `AgentConfig` is built with, and it is still
   what a non-gate consumer sees.
3. **The gate gets the declared cap, worker-only.**
   `agent/core/tool_executor.py:488-515` —
   `permission_footprint = getattr(self.config, 'permission_footprint', None) or None`
   for workers, `None` for the main agent. The session's live grant is now
   passed as `session_permissions` (`:532-544`) so the gate resolves
   grant x ceiling itself, from disk, per call.
4. **Widening is audited.**
   `agent/core/tool_executor.py:78-101` (`_permission_rank`), `:188-191`
   and `:473-475` (last-observation record, worker context only),
   `:659-718` (`_record_permission_widening` -> `worker_permission_widened`
   with `worker_id, session_id, category, old_value, new_value, actor`).
   Only categories observed in both calls with a strictly higher rank are
   reported; the first observation is a baseline, not a widening.
5. **The doctrine is written down.** `thoughtmachine/permission_store.py`
   "Live (post-spawn) effect": a write there is the only grant authority, no
   respawn and no in-process refresh — for worker sub-agents too.

Principle to hold the line on: **the declared footprint caps; the spawn-time
snapshot never does.** Nothing else may narrow a live worker below its session
minus ceiling.

## Acceptance tests

Pinned in `tests/test_worker_permission_grant_immediate.py` (self-contained;
seeds a sidecar + workspace ceiling in `tmp_path`, reuses one executor across
calls):

- **A** `test_grant_is_immediate_on_the_same_executor` — seed `read`, denied;
  rewrite the sidecar to `write`; same executor instance now allows. Asserts
  `live_executor is executor` and that `config.session_permissions` still says
  `read` (the stale mirror really is stale).
- **B** `test_revocation_is_immediate_on_the_same_executor` — the other
  direction, unchanged from before the fix.
- **C** `TestDeclaredFootprintCap` — a declared footprint still caps a wider
  live grant; it cannot grant beyond the session; it survives a live grant;
  the main agent has no footprint cap.
- **D** `TestWideningAudit` — baseline is not a widening; one grant emits
  exactly one `worker_permission_widened` with the six-key payload and a third
  call adds none; narrowing is never reported; main-agent widening is not
  audited.
- **E** `TestCeilingStillWins` — a session grant above the workspace ceiling is
  denied for worker and main alike (the ceiling hard-deny message differs from
  the footprint-deny message: "The session exceeds the ceiling"), and a ceiling
  of `read` caps a session `write` for a worker.

In `tests/test_worker_disk_mode_inheritance.py` the fourth-gate premise is
rewritten: the old case that asserted "the worker's stale snapshot denies the
wider disk grant" is replaced by
`test_stale_snapshot_does_not_cap_live_disk_grant` (now allowed) plus
`test_worker_declared_footprint_caps_live_disk_grant` (still denied, via the
declared field).

## Residuals (not fixed here)

1. **Boolean non-container values.** `SessionPermissions` only coerces the
   boolean channels (e.g. `network`), while `permission_footprint` is a raw
   `Dict[str, Any]`. A footprint written in legacy boolean form
   (`{"filesystem": true}`) is not normalized the way the session profile is;
   what the gate does with it depends on the level vocabulary, not on the
   footprint's own shape. Needs one canonicalization at the footprint boundary
   before any legacy worker definition is trusted.
2. **Legacy worker definitions.** A definition still carrying a
   pre-canonical grain (`git_write`, `container`, `host_bash`) reaches the
   gate's unknown-level branch, which is fail-closed. Correct direction, poor
   diagnosis: the error names the category, not "your worker definition uses a
   retired vocabulary". `_canonicalize_session_permissions` runs on the mirror
   (`:1682`) but the declared footprint is passed un-canonicalized on purpose
   (`:1684-1692`); that asymmetry deserves a fail-closed warning.

## Related

- `design/start-unification` — same underlying question at container level:
  which spawn-time facts are authority and which are cache.
- `hygiene/panel-host-bash-display` — displayed != enforced, human panel. This
  ticket is the worker-side instance of the same family.
- `bug/no-workspace-session-permissive-defaults-security-review` — the other
  end of the same axis (what a session resolves to when nothing pins it).
- `design/audit-trail-completeness` — `worker_permission_widened` is a new
  audit event; it belongs in that inventory.

## MUST NOT TOUCH

- `thoughtmachine/security.py::SAFE_DEFAULTS`
- `security/resource_catalog.py`, `agent/config/resource_catalog.json`
- `security_gate.apply_workspace_ceiling` + call sites
- parked `.thoughtmachine/knowledge/project/architecture_audit_2026-09.md`
- `scratch/_exp2.py`
- The session-side `_restrictive_merge` result at
  `worker_thread.py:1677-1682` — it is deliberately still a mirror. Deleting
  it "because it is stale" would break every consumer that reads
  `AgentConfig.session_permissions` instead of the gate.
