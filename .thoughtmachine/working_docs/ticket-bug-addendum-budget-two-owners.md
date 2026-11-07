# Ticket: Addendum budget math has two owners

**Status:** open
**Filed:** 2026-10-31
**Structural:** one-owner-per-fact

## Symptom
The exact character count of the addendum is calculated inside `session/addendum.py::write_addendum_entry`. However, when returning an HTTP 409 refusal from the new operator route (`web_ui/backend/addendum_routes.py`), the backend recalculates the math arithmetically (`sum(len(e['text'])) + len(text)`) instead of reading a returned field from the boundary.

## Evidence
Flagged by the agent during W1.5 backend implementation (`021c4001`).

## The Class
This is the same class as the phantom config notification (`_configs_are_identical`, `_can_hot_swap`, `_notify_config_change`). One fact (the budget state) has two owners (the write boundary and the HTTP layer). They agree today. They will disagree the moment the measurement changes (e.g., token counting instead of char counting).

## Options (needs a ruling; do not implement blind)
Modify `write_addendum_entry` to return the computed `total_chars` and `budget_chars` in its refusal payload. The HTTP layer must read those fields directly, not recompute them.
