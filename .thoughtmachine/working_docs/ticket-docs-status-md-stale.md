# ticket: `docs/status-md-stale`

**Filed:** 2026-10-31 (Gen 55 onboarding)
**Status:** open
**Severity:** low

## Symptom

`status.md` (updated 2026-10-28, Gen 54 mid-session) records `dev HEAD: 12bd6408`. The actual HEAD
is `4c700791` (merge of #260). PRs #259 and #260 have landed since the last update, along with the
Addendum schema and prompt composition. The doc has not been updated at the strand close.

## Evidence

- `.thoughtmachine/knowledge/project/status.md` line 3 reads `Updated: 2026-10-28, Gen 54
  mid-session.` The `## Current state — 2026-10-28, Gen 54 mid-session` heading is at :42, and line
  45 reads `**dev HEAD:** 12bd6408`.
- `12bd6408` occurs **4×** in `status.md`; `6399efb4` (the HEAD the dashboard's "State of play"
  names) occurs **0×**.
- `4c700791` occurs **nowhere** under `.thoughtmachine` — `grep -rl` returns no hits, so no doc on
  disk records the current base. It **cannot be verified here**: this environment has no `git`
  binary. "The actual HEAD is `4c700791`" above is a reported fact carried into the ticket, not a
  measurement this pass was able to re-derive from the repository.

## Options (needs a ruling; do not implement blind)

1. **Update at strand close only.** Treat `status.md` as a close-of-strand artefact: rewrite the
   `Updated:` / `dev HEAD:` pair in the same act that closes the strand. Cost: nothing catches the
   drift mid-strand, which is exactly the window this ticket was filed in.
2. **Mechanical check before publication.** Add a check that `status.md`'s `dev HEAD:` matches
   `dev` and that `Updated:` is not older than the newest merge, run before a wave report is
   published; the report fails if they disagree. Cost: the check needs a job with repository access,
   and it converts a prose promise into a pipeline step.

## Acceptance criteria

- `status.md` reflects `4c700791` and notes the completion of W1 Addendum slices 1 & 2.
- The file carries a date/HEAD pair that is true on the day it is read.

## Related

- `operator-dashboard.md` — the sibling staleness in its "State of play" section, filed as ticket
  `docs/dashboard-state-of-play-stale`.
- The wave-report discipline in `working_agreements.md`.
