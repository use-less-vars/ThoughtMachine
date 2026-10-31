# ticket: `hygiene/addendum-budget-math-duplicated`

**Filed:** 2026-10-31 (W1.5 slice-5 review)
**Status:** open
**Severity:** low

## Symptom

A refused addendum write is explained **twice, by two independent copies of the same
arithmetic**. The write boundary decides the refusal and renders its own sentence, but returns
only the reason CODE; the REST face then re-derives both formulas (characters and entry count)
from the unchanged set plus the candidate text so it can put numbers in the 409 `message`. One
fact, two formulas, and nothing that fails when they disagree — the operator would simply read a
number that no longer explains the refusal.

## Evidence

- `session/addendum.py:195-200` — the budget verdict, measured on the RESULTING set:
  `total_chars = sum(len(item['text']) for item in entries)` compared against
  `normalized['budget_chars']`, and the sentence is built inline
  (`f"addendum budget exceeded: {total_chars} > {...} chars"`).
- `session/addendum.py:201-205` — the entry-limit verdict, same shape:
  `len(entries) > normalized['max_entries']`, sentence built inline.
- `session/addendum.py:125-132` (`_dropped`) — the sentence is *consumed* there: it goes into the
  `runtime_notice` payload (`_notice_payload`, `:98-106`) and into
  `log('WARNING', 'core.addendum', f'[addendum] write dropped ({reason}): {message}')`. The
  **return value** is only `{'status': 'dropped', 'entry': None, 'reason': reason, 'entries': []}`
  — the sentence never leaves the module.
- `web_ui/backend/addendum_routes.py:122-147` (`_refusal_message`) — therefore recomputes both:
  `:135` `sum(len(item["text"]) for item in entries) + len(text)` and `:142` `len(entries) + 1`,
  each against `normalize_addendum(session.addendum)`'s own `budget_chars` / `max_entries`.
  Called from `:216` to build the 409 body.
- `web_ui/backend/addendum_routes.py:14-17` states the module "never re-implements the budget
  check, never truncates, never evicts" — the *verdict* honours that (the code comes from
  `write_addendum_entry`), but the *arithmetic in the rendered sentence* is a second
  implementation of the same measurement, which is the part that can drift.
- The copies agree today only because both are the exact character count of the same set. Nothing
  pins that: no test asserts the 409 `message` equals the boundary's own sentence, so a wording
  change or a measurement change (e.g. counting a per-entry overhead, or moving the candidate into
  the measurement differently) on one side drifts the other silently. The `runtime_notice` the UI
  shows and the 409 `message` would then tell the operator two different stories about one refusal.

## Options (needs a ruling; do not implement blind)

1. **Return the sentence from the write boundary.** Add `message` (or `notice`) to the drop dict
   next to `reason`, and have `_refusal_message` render `result['message']` verbatim for the two
   budget codes. One formula, no helper, and the drift becomes structurally impossible. Cost: the
   session layer's user-facing wording becomes part of the REST contract, and the drop dict —
   already four keys with a pinned shape — grows a fifth that every caller of the boundary
   inherits.
2. **Shared helper.** Export the measurement (or a `budget_refusal_message(reason, entries, text)`
   renderer) from `session/addendum.py` and call it from both sites. Keeps the sentence out of the
   return contract. Cost: a new public name in the boundary module for a display concern, and the
   helper has to keep the two call shapes (resulting set vs. unchanged-set-plus-candidate) honest.
3. **Leave the duplication, pin it.** Add a test asserting the 409 `message` and the boundary's
   sentence are byte-identical for both budget codes. Cost: the drift risk stays; only the cost of
   *discovering* it drops.

Whichever is chosen, the budget semantics must not move: measured on the resulting set, exact
character count, refuse-only (no truncation, no eviction), no grandfathering.

## Acceptance criteria

- Exactly one site computes `sum(len(text))` vs `budget_chars` and `len(entries)` vs
  `max_entries` for refusal wording; the other calls it.
- The 409 body's `message` and the boundary's `runtime_notice` / logged sentence remain
  byte-identical, pinned by a test.
- Transport contract unchanged: 409 `{status: 'refused', reason, message, entries, total_chars,
  budget_chars, max_entries}` and the drop dict `{status: 'dropped', entry: None, reason,
  entries: []}` (or a documented superset of it).
- No behaviour change to acceptance or refusal: a write that fits still persists and enqueues, a
  write that does not still persists nothing and enqueues only the notice.

## Related

- `session/addendum.py` — the write boundary (slice 4), budget enforcement (slice 5); this ticket
  is about its return contract, not its verdict.
- `web_ui/backend/addendum_routes.py` — the REST face (W1.5); `_refusal_message` is the second copy.
- `tests/test_addendum_budget.py`, `web_ui/backend/tests/test_addendum_routes.py` — where the
  refusal sentences are currently asserted, each on its own side.
- W1.5 slice 5 (budget enforcement) / ruling D-7 (refuse-only budget, no truncation, no eviction).
