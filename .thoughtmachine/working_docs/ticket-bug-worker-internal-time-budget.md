# bug/worker-internal-time-budget

**Observed 2026-10-19.** The worker has an internal time budget (~80s in the
test agent's mode, ~134s in the EE agent's) that fires *independently* of the
caller-supplied `timeout_seconds`. The caller passed `timeout_seconds: 300` and
the worker was stopped at 73.6s (warning) / ~80-134s (hard).

**Impact:** the parameter is silently ignored. An agent doing legitimate work
gets cut off with no diagnostic to the operator. The EE agent lost 71 tool
calls of work; nothing on disk.

**Read first:**
1. Grep `internal.*budget|worker.*budget|WARN.*budget|soft.*budget|hard.*budget`
   in `agent/core/worker_context.py`, `tools/workspace/worker_thread.py`,
   `agent/config/*.py`.
2. Find where `timeout_seconds` is accepted and confirm it doesn't propagate to
   the internal budget.
3. Is the budget mode-dependent? (engineer vs audit vs agent)
4. The 73.6s warning is the soft boundary — is the hard boundary 80s? 134s?
   Where is the ratio defined?

**Fix shape (design-before-implement):**
- (a) `timeout_seconds` propagates to the internal budget. Caller wins.
- (b) Internal budget is a floor: `effective = max(caller_timeout, internal_budget)`.
- (c) Internal budget becomes configurable per-workspace, defaults to a
  documented value (60s?), override via workspace config.

**Important:** whichever shape, the soft-warning threshold should give enough
headroom for the worker to *commit and report* before the hard stop. Currently
73.6s warning / 80-134s hard — the worker did the right thing and stopped to
report, but "commit and report" needs to be the explicit instruction at the
soft boundary.

**Related:** cross-workspace naming (separate ticket), `context.json` null
fields (separate ticket).

**Filed by:** Gen 46, 2026-10-19, from EE + test agent reports.
