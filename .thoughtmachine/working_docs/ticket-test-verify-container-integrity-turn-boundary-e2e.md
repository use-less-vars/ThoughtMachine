# Ticket: test/verify-container-integrity-turn-boundary-e2e

**Filed:** Gen 44, 2026-10-15, from adversarial verification of
`fix/container-lifecycle-funnel` (report:
.thoughtmachine/reports/wave9a-adversarial-verify.md, Target 7 PARTIAL).
**Severity:** Low — coverage gap, not a correctness gap.
**Status:** filed, not dispatched.

## Finding

The field-repro test
(`tests/test_container_lifecycle_funnel.py::test_verify_container_integrity_field_repro_leaves_container_intact`)
proves the callee is non-destructive on a direct call. It does NOT reproduce
the field path.

Gaps:
- **(i)** Calls `verify_container_integrity` directly. Does not drive the
  turn-boundary trigger chain
  `web_ui/backend/bridge.py::_maybe_re_sync_container` → `apply_config` →
  WS `continue_session` — the path the field bug actually fired through.
- **(ii)** Its `container_exists is True` assertion reads a **static return
  field** (`docker_executor.py:557, 587`), not live liveness. The real
  evidence is `stop_calls == []` / `remove_calls == []`.
- **(iii)** Does not cover the `_ensure_container` recreate path — which still
  reaps on mismatch (see ticket fix/funnel-container-manager-stop-remove).

## What the test would need

- Drive the WS-continue path in an integration test: start session, apply a
  config change, simulate WS `continue_session` Case 2, assert the container
  was not stopped/removed.
- Assert live liveness via a mocked `container.status` attribute, not the
  static return field.

## Why filed, not blocking

The fix is correct (verifier Target 3 CONFIRMED the funnel fails closed). The
missing test is about proving it end-to-end, not about making the fix work.

## MUST NOT TOUCH
Same list as the parent ticket.

## Amendment 2026-10-20 (gen-47 run)

**Report cross-ref:** not a report finding — this amendment records a live check, not a run observation.

Confirmed 2026-10-20 on build `124477b7` that no such test exists in `tests/docker_integration/test_container_integrity.py`; the turn-boundary end-to-end coverage described above is still absent, so this ticket remains the correct artifact for the gap. No change to the gap analysis: (i) direct-call vs field path, (ii) static return field vs live liveness, (iii) `_ensure_container` recreate path uncovered.
