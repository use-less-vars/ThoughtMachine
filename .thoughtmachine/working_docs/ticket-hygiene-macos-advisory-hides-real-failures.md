# Ticket: hygiene/macos-advisory-hides-real-failures

**Filed:** Gen 47, after wave-4 PR 4 (#162) merged; `dev` = `d60ffc92`.
**Verified at:** three consecutive merged PRs — #157, #159, #161 — each reported "macOS - full test suite (advisory): fail". Two different failing tests were named in those logs.
**Source:** operator relay of the CI logs for those three PRs.
**Severity:** MEDIUM — process, not product. The cost is that a real, deterministic failure passed three merges filed under "the known flake".
**Status:** filed, not dispatched.

## Defect

Three merged PRs reported the same job failing. Each was read as the known flake `tests/test_workspace_lifecycle_manager.py::test_soft_timeout_warning_emitted_once`. That reading was correct once and wrong twice:

- PR #157's macOS failure: the timing flake, genuinely.
- PR #161's macOS failure: `tests/integration/test_apply_config_coverage.py`, `/tmp` versus `/private/tmp` — a deterministic consequence of PR 2's realpath ingress, filed as `ticket-bug-macos-tmp-path-tests-assume-symlink-free.md`. Not a flake.

Two different tests, one assumption, three merges.

## Mechanism — why an honest reader still got it wrong

1. The job is named "advisory", which the queue reads as "not a gate" and therefore as "not a reading task".
2. "Known flake" was carried as a **category, not a name**. A category has no membership test, so any failure can be filed under it without contradiction.
3. The macOS log is long and red, and the second failure sits a few lines under the first — a reader who found the expected name stopped looking.

Net: declared-but-not-enforced at the CI gate. This is the same pattern the audit corpus tracks in the machinery (`displayed ≠ enforced`), one layer up — in the process that decides what a red job means rather than in the code that produces it. "Advisory" means "not gating", not "not reading".

## Fix options

**(a) Named-flake allowlist in CI config**, listing `test_soft_timeout_warning_emitted_once` explicitly, so any *other* macOS failure is by construction real and unexplained. A name list has a membership test; a category does not.

**(b) Log scan at merge time** — pull test names out of the failed job's log and refuse to merge on "known flake" if the name is not on the list. Weaker than (a) because it needs the log to be machine-read every time, but it needs no CI config change.

**(c) Promote macOS to blocking** once the path test is fixed, so the platform is gated rather than merely observed.

**Recommendation: (a) + (c).** (a) is cheap and makes the residual signal trustworthy; (c) is only safe after (a) — promoting a job to blocking while it still carries an unnamed flake teaches the team to bypass the gate, which is the failure mode (a) exists to prevent.

## Bearing

MEDIUM, and it compounds. The reason one macOS failure survived three merges is the same reason any future real macOS regression will. Fix before the platform is promoted to blocking, not after.
