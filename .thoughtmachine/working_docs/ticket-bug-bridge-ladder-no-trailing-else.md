# Ticket: Bridge ladder drops unhandled runtime dicts silently

**Status:** open
**Filed:** 2026-10-31

## Symptom
The bridge event ladder has no trailing `else` block. Any runtime dict kind that is not explicitly handled is dropped silently without a warning or fallback.

## Evidence
Agent flagged this during W1 Slice 4 (commit `641e24f6`). Renaming the bridge arm for `addendum_updated` caused the event to disappear from the stream with no error.

## Options (needs a ruling; do not implement blind)
Add a trailing `else` that logs a warning (or raises in dev), so unhandled runtime kinds are visible rather than swallowed.

## Acceptance criteria
Unhandled runtime dicts emit a warning to the server log.
