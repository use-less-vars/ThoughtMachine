# Ticket: Add "clear addendum" action

**Status:** ruled — implemented in W1.5 (backend `021c4001` + ConfigPanel clear); body kept as filed
**Filed:** 2026-10-31

## Symptom
The addendum has a strict character budget (8000) and entry limit (64), enforced with a refuse-only policy (D-7). A session that exceeds these limits before enforcement lands (e.g., manual JSON edit, migration) will be permanently unable to add new entries.

## Options (needs a ruling; do not implement blind)
Add an operator action (UI button or API endpoint) to clear all addendum entries (`entries: []`), providing a recovery path for over-budget sessions.

## Acceptance criteria
- Operator can invoke "clear addendum" from the config panel.
- Session addendum is reset to empty.
- `addendum_updated` fires.

## Implementation note (W1.5 — shipped, not a proposal)

The ruling was to add the recovery action, not to relax the budget. Landed as:

- **Backend:** `DELETE /api/session/{session_id}/addendum`
  (`web_ui/backend/addendum_routes.py:234-269`, commit `021c4001`). Returns
  `{status: 'cleared', entries: [], total_chars: 0, budget_chars, max_entries, session_id}`;
  `budget_chars` / `max_entries` and any key the module does not know about survive (normalised
  through `normalize_addendum`), and the session is persisted via `save_session`, so
  `updated_at` bumps while a refused write's would not.
- **Frontend:** `web_ui/frontend/src/components/ConfigPanel.jsx` — two-step `Clear all` →
  `Confirm clear` control driving `handleAddendumClear`, which resets the local entry list only
  when the DELETE response carries `status: 'cleared'`.
- **Tests:** `web_ui/backend/tests/test_addendum_routes.py` (DELETE contract) and
  `web_ui/frontend/src/components/__tests__/ConfigPanel.addendum.test.jsx` (panel flow).
- **Wave:** W1.5 (slice 5 follow-up).

One deviation from the acceptance criterion above: the clear is a REST call and does **not** fire
`addendum_updated`. That event is emitted only by the write boundary
(`session/addendum.py::_updated_payload`) for a write made through it on a session carrying a live
Agent's `addendum_queue`, and a request thread must not touch that queue
(`addendum_routes.py:22-24`). The panel converges on the DELETE response body instead.
