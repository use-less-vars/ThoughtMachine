# Ticket: hygiene/macos-advisory-check-triage

**Filed:** 2026-10-26, from Gen 51's session close, after `macOS - full test suite (advisory)` failed on PR 206 and PR 207 while every other platform check passed.
**Severity:** LOW (advisory, non-blocking) — class-level: a check that is always red trains reviewers to ignore red.
**Status:** filed; triage deferred to the delivery wave.

## Finding

`macOS - full test suite (advisory)` is an advisory check on every PR. It has failed on both PR 206 (docs-only, KB close) and PR 207 (auth-layer D3) — the two most recent PRs of this session. It failed on PR 206 despite that PR touching only `.thoughtmachine/knowledge/` files and nothing under `tests/` or `web_ui/` — the failure is structurally independent of the PR diff.

Failing-check duration is consistently ~6–7 minutes, matching the full-suite runtime on other platforms. The check runs the corpus and fails somewhere inside it; it is not failing to start.

## Bearing

A red check that is not required-by-branch-protection is a **red that is read but not acted on**. Over repeated PRs the reviewer habituates to `UNSTABLE` as the expected state, and the signal value of any advisory check on this platform approaches zero. That is `verification-pins-wrong-contract` at the CI layer: the check is a fact owner that owns the wrong fact.

The specific question to triage: **was this check ever green on this repository?**
- **Never green** — aspirational check that was never made real. Demote honestly (remove from required-options, rename to indicate non-operational) or delete.
- **Sometimes green** — flake. Quarantine (retry-3) or scope (run a subset). Shape depends on whether the flake is timing- or platform-sensitive.
- **Previously green, now not** — real macOS regression. Delivery wave (macOS/Windows install) is the natural owner, since that wave needs macOS working anyway.

## Blast radius

Advisory only — no merge is blocked by this check. But: the CI status surface currently shows "17/18 checks green, 1 red" for every PR, and the reviewer has no way to distinguish "red for a real reason" from "red because this check has been red since before the PR." That ambiguity is the cost.

## What this is not

- Not a blocker for any current wave.
- Not a cause of any other failure — the failing tests inside the macOS check are platform-shape, not Linux reproducibility failures.
- Not the same class as `test_providers_crud.py:79` — that is a test pinning the wrong contract; this is a CI check with the wrong status.

## Fix shape

Not specified here. Any resolution must **at minimum** answer the "was it ever green" question with evidence (git log of the workflow file, run history) — the three branches above have three different answers, three different fixes, three different owners.

Triage in the delivery wave, alongside the macOS/Windows install work that will need the platform working anyway. Do not chase this now; do not gate D2, D1, or the two-ticket PR on it.
