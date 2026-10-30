# ticket: `chore/ticket-front-matter-backfill`

**Filed:** 2026-10-29 (Gen 54 delivery arc)
**Status:** open
**Severity:** low

## Symptom

The dashboard's ticket counts are **scraped from prose**. Each ticket is counted by reading its
`**Status:**` line as free text, so every re-count is a manual pass and the numbers drift between
them. **36 of 193** `ticket-*.md` files have **no `Status:` line at all**, so their disposition
cannot be scraped at all and they are counted as `stale` rather than described.

The drift is not hypothetical: the dashboard's previous summary block claimed
**190 files — 12 closed, 2 superseded, 176 open**, and was **wrong on all four numbers** (the
measured partition was 192 files — 10 closed, 3 superseded, 143 open, 36 with no `Status:` line).

## Evidence

- `10 + 3 + 143 + 36 = 192` at `.thoughtmachine/working_docs/` before the reconciliation pass, by
  anchored match on the first `^**Status:**` line: `SUPERSEDED|RETIRED` → superseded,
  `CLOSED|SHIPPED|FIXED|RESOLVED` → closed, anything else → open, no line → stale.
- 36 files returned by `grep -L '^\*\*Status:\*\*' .thoughtmachine/working_docs/ticket-*.md`.
- The scrape is *ordering-sensitive* in a way prose invites: an unanchored match on `fixed` buckets
  `filed, not fixed` as closed. The classifier had to be anchored to be correct — that is a
  hand-rolled parser standing in for a field.
- The dashboard now carries an explicit "Interim snapshot" paragraph admitting exactly this, and
  naming the backfill as queued. **This ticket is that queue entry.**

## Options (needs a ruling; do not implement blind)

1. **Front-matter + generated page.** Add a `status:` (and `filed:` / `severity:`) front-matter block
   to every `ticket-*.md`, and generate `operator-dashboard.md`'s counts from it. One source of truth,
   machine-readable, no scraping. Cost: a touch to all 193 files, and the 36 no-`Status:` files each
   need a human decision on what their status actually *is*.
2. **Prose stays; a lint fails the gap.** Keep the `**Status:**` prose line as the record, and add a
   check that **fails** on any `ticket-*.md` without one (and on any status whose first token is
   outside a known vocabulary). Cheap, no bulk edit, but the dashboard is still scraped and still
   hand-written.
3. **Both.** Front-matter as the machine-readable field, plus a lint that no ticket may be missing
   it. The lint is what keeps option 1 from rotting the moment a new ticket is filed by hand.

Options 1 and 3 need a ruling on the status vocabulary before any file is touched — the current
prose values are 60+ distinct sentences, and a vocabulary that does not fit the existing tickets will
be bypassed on the first new filing.

## Acceptance criteria

- Every `ticket-*.md` carries a machine-readable status field; none is inferred from prose.
- `operator-dashboard.md`'s counts are **generated** from those fields, not hand-written.
- A re-count run twice, with nothing changed in between, reproduces the same numbers — and the
  buckets still sum to the file count on disk.

## Related

- `.thoughtmachine/knowledge/project/operator-dashboard.md` — the page whose counts depend on this.
- `.thoughtmachine/knowledge/project/working_agreements.md` — the record/filing conventions this
  would amend.
- Filed by the same reconciliation pass as
  `ticket-bug-state-bridge-engineer-mode-deletes-custom-prompt.md`.
