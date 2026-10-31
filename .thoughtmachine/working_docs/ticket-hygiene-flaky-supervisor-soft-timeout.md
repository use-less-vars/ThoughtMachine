# ticket: `hygiene/flaky-supervisor-soft-timeout`

**Filed:** 2026-10-31 (Gen 55 onboarding)
**Status:** open
**Severity:** low

## Symptom

`tests/test_workspace_lifecycle_manager.py::test_soft_timeout_warning_emitted_once` failed on PR
#260's CI (the macOS advisory job) but passes on `dev @ 1f275eea`. The test asserts
`sup.soft_timeout_warning_emitted is True`, which failed: the assertion line is `assert False is
True`.

## Evidence

- The test is **timing-sensitive** — it waits on a real soft-timeout interval and then asserts a
  one-shot flag was set. The flag is `False` on the failing run, i.e. the warning was never
  observed as emitted, or was emitted outside the window the test sampled.
- It failed on a branch that does not touch `test_workspace_lifecycle_manager.py` and does not touch
  the supervisor timeout code. Nothing in #260 changes the behaviour under test.
- So it is classified as a **flake, not a regression from #260**. That classification is a
  **judgment call** on the two points above, not a verified pre-existing failure: nobody has since
  reproduced the failure deliberately, and the single-emission flag has not been instrumented.
- The `dev @ 1f275eea` pass is the verdict recorded here; this ticket adds no independent local
  re-run of its own.

## Options (needs a ruling; do not implement blind)

1. **Desensitise the test to timing.** Drive the soft-timeout path from an explicit clock or a fake
   scheduler and assert on the one-shot flag without sleeping. Deterministic by construction. Cost:
   the test stops exercising the real timer wiring, so a regression in that wiring would no longer
   be caught by it.
2. **Keep real timing and retry.** Leave the test as written and allow a bounded retry (or demote it
   to advisory-only on macOS). Cost: a transient red is papered over, and if a genuine timing bug
   ever lands, the retry hides it.

Neither is safe to pick blind: option 1 trades coverage of the timer wiring for determinism, option 2
trades a known-nondeterministic verdict for stability.

## Acceptance criteria

- `test_soft_timeout_warning_emitted_once` passes deterministically on macOS CI.
- A re-run of the suite does not change its verdict.
- If option 1 is taken, the assertion no longer depends on wall-clock scheduling; if option 2, the
  retry is bounded and visible rather than an unconditional skip.

## Related

- PR #260 — the branch on which it failed, and which does not touch the code under test.
- The macOS advisory CI job.
