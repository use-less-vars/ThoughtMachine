# Ticket: Add "clear addendum" action

**Status:** open
**Filed:** 2026-10-31

## Symptom
The addendum has a strict character budget (8000) and entry limit (64), enforced with a refuse-only policy (D-7). A session that exceeds these limits before enforcement lands (e.g., manual JSON edit, migration) will be permanently unable to add new entries.

## Options (needs a ruling; do not implement blind)
Add an operator action (UI button or API endpoint) to clear all addendum entries (`entries: []`), providing a recovery path for over-budget sessions.

## Acceptance criteria
- Operator can invoke "clear addendum" from the config panel.
- Session addendum is reset to empty.
- `addendum_updated` fires.
