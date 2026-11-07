# Ticket: the non-strict xfail never XPASSes, so it never pinned the refusal fix

Status: open

## Problem

The suite carries a non-strict `xfail` intended to pin the refusal→`success` path, but it has
never XPASSed. Every acceptance gate reports `5 xfailed, 0 xpassed` both before and after the
refusal fix landed (`c37943a1`, merged via #237). A non-strict `xfail` that cannot flip is not a
regression pin: it stays green whether the behaviour is fixed or broken, so the one behaviour the
fix exists to guarantee — a refused or never-executed call is emitted with `success: False` — is
still untested.

## Evidence

- Gate tallies before the fix and after the fix are identical: `5 xfailed, 0 xpassed`
  (sandbox baseline `5594 passed, 66 skipped, 5 xfailed, 12 subtests`; host baseline
  `5642, 16, 5, 12`).
- The fix merged in `c37943a1` (#237) without moving any test from xfail to xpass.
- The production code the test is supposed to cover now exists:
  `agent/core/tool_executor.py:278`, `:295`, `:314`, `:349-350`, `:397-399`, `:419`, `:439`,
  `:537`, `:551`, `:642`, `:708-710` (refusal/failure paths carry `success: False`), and
  `agent/core/agent.py:1651-1665` (the loop reads that verdict before sniffing prose).

## Suggested fix

Replace the phantom xfail with a real regression test that:

1. drives a denial through `execute_tool_calls` (a gate denial, not a mocked result), and
2. asserts the emitted entry's `success is False` — and after the loop, that the `tool_call` and
   `tool_result` events carry `success: False` as well.

Add a companion assertion that `agent/presenter/event_processor.py` does not default a denial to
`True`: a payload with an explicit `success: False` (and one with the key absent) must not be
rewritten into a success by the presenter.
