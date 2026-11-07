# ticket: `bug/state-bridge-engineer-mode-deletes-custom-prompt`

**Filed:** 2026-10-29 (Gen 54 delivery arc)
**Status:** open
**Severity:** high

## Symptom

Switching a live session into "engineer" mode **wipes the custom system prompt the operator had set
for that session**. The mode / preset switch overwrites the session's prompt instead of layering on
top of it, so the operator's own text is gone after the switch and is not recoverable from the
session.

## Evidence

- Operator report: the custom prompt was set; the session was switched into "engineer" mode; the
  custom text is no longer what the session runs with.
- **No reproduction has been run against a live session for this ticket.** There is no stack, no log
  line, no repro steps, and no code site cited here. The report is the evidence; nothing in this file
  is a diagnosis.

## Options (needs a ruling; do not implement blind)

1. **Layer, don't replace.** The mode's prompt is composed *with* the session's custom prompt rather
   than written over it — one resulting string, deterministic order, custom text survives.
2. **Preserve and restore.** The switch stores the custom prompt aside and restores it on switch-back.
   Cheaper, but the engineer-mode turn still runs without the operator's text.
3. **Refuse the switch** when a custom prompt is set, and surface the refusal in the UI. Honest, and
   the wrong trade if engineer mode is the common path.

The layer-vs-overwrite question is a `one-owner-per-fact` question: two surfaces — the mode preset
and the custom prompt — both currently claim to own "what this session's system prompt is".
Whichever option is chosen must leave **one** owner for that fact and one composition point.
**Needs a ruling before any code is written.**

## Acceptance criteria

- Switching a session into "engineer" mode and back leaves the operator's custom prompt
  byte-identical.
- Exactly one surface owns "the session's system prompt"; the other composes into it rather than
  assigning over it.
- A test asserts the surviving text **after** the switch — a state assertion, not "does not throw".

## Related

- Structural tag: `structural: one-owner-per-fact` — same class as the other two-surfaces-one-fact
  tickets on the dashboard.
- Not verified against the code; no file or line is cited, on purpose.
