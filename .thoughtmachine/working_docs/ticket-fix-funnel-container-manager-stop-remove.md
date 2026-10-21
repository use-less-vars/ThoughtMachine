# Ticket: fix/funnel-container-manager-stop-remove

**Filed:** Gen 44, 2026-10-15, from adversarial verification of
`fix/container-lifecycle-funnel` (report:
.thoughtmachine/reports/wave9a-adversarial-verify.md, Target 1 REFUTED).
**Severity:** Medium — second repro path for the field-bug symptom family.
**Status:** filed, not dispatched.

## Finding

The funnel migration (PR #115) covered the 9 sites in the plan. The verifier
found 14 additional production destroy/stop paths in the same files, un-funneled:

- U1 `ContainerManager.stop()` — `infra/container_manager.py:3155-3188`
- U2 `ContainerManager.remove()` — `:3190-3222` — public recreate API, calls U1
- U3 `ContainerManager.start()` recreate ladder — `:1555, :1652, :1774`
- U4 stale-worker cleanup — `:4533`
- U5 `ContainerRegistry.destroy_container` / `on_permission_changed` —
  `infra/container_registry.py:802-803, 808, 857-858` (self-declared "NOT wired")
- U6 `ResourceContainerManager.ensure_resource` stale removal —
  `infra/resource_container_manager.py:1397`
- U7 `ResourceContainerManager.stop()` — `:1547`
- U8 module-level resource-orphan sweep — `:2213`
- U9 `_remove_container_by_workspace` — `docker_executor.py:1285-1286`
- U10 `DockerExecutor.close()` — `:1022-1023`
- U11 `WorkerSupervisor.release_container` — `infra/workspace_lifecycle_manager.py:629-641`
- U12 `cleanup_worker_containers` — `tools/workspace/worker_container.py:199,203`;
  `tools/workspace/worker_thread.py:2080,2084`
- U13 `worker_execution.terminate_all` — `tools/workspace/worker_execution.py:296,358,403`;
  `tools/workspace/check_system.py:670`
- U14 `kill()` on exec timeout — `infra/container_manager.py:2175`,
  `infra/resource_container_manager.py:1516` (arguably out of scope)

## The residual-risk framing

Verifier Target 4: the funnel **gates only 1 of 8 migrated sites** (site 1
`verify_container_integrity`). At the other 7 it agrees with the caller
(`explicit=True` always → reap). So the funnel is largely a decision-recording
layer, not a guard.

Verifier Target 7(iii): `_ensure_container` **still destroys on mismatch**
(`explicit=True`, deliberate — pinned by `tests/trust/test_toggle_live.py`).
**A drifted workspace-owned container, destroyed on its next *use*, produces
the same symptom family as the field bug** — running container gone without
intent. This is a plausible second repro path. Not the same trigger (turn-end
verify vs next-use ensure), but the same outcome.

## Scope for the eventual fix

Candidate approach: funnel `ContainerManager.stop()` / `.remove()` (the public
API — U1/U2) with `kind="reap", explicit=True`, keeping destructive semantics.
That routes the worker-teardown family (U11/U12/U13) through the funnel and
gives one log line per removal. The recreate-ladder sites (U3) need a decision:
they are legitimate destroys on weak/denied envelopes — should they refuse
instead? That is a design call for Main.

**Not a small patch.** Requires its own design pass, its own dispatch, its own
adversarial verification. Do not fold into a follow-up fix wave.

## Acceptance

- Every production stop/remove path enumerated in this ticket routes through
  `ContainerLifecycle.reconcile` OR is documented as out-of-scope with a
  reason.
- `tests/trust/test_toggle_live.py` still green (recreate path preserved).
- One CONTAINER_LIFECYCLE event per decision across the newly migrated sites.

## MUST NOT TOUCH
- thoughtmachine/security.py::SAFE_DEFAULTS
- security/resource_catalog.py, agent/config/resource_catalog.json
- security_gate.apply_workspace_ceiling + call sites
- parked .thoughtmachine/knowledge/project/architecture_audit_2026-09.md
- scratch/_exp2.py
- agent/presenter/session_lifecycle.py

## Bearing

Arc 2 (machine self-knowledge) + Arc 3 (secure execution). The funnel is the
single-arbiter invariant Inv.6 in container-lifecycle form; completing the
coverage is the invariant made true.
