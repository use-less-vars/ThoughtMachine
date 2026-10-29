# Ticket — UI: async tool-call pairing (Frontend Event Bus State Machine)

**Status:** IMPLEMENTED — pushed on `fix/ui-async-tool-pairing` (`bfdbbc6c`, `e4202f31`, `1d4377ce`), PR open
against `dev`
**Branch to fix on:** `fix/ui-async-tool-pairing` (pushed, PR open against `dev`)
**Severity:** medium — cosmetic ordering + duplicate bubbles, no data loss
**Filed:** from operator report, 2026-11 session
**Filename note:** operator asked for `bug/ui-async-tool-pairing.md`; filed flat as
`ticket-bug-ui-async-tool-pairing.md` instead so it matches the existing
`ticket-*.md` convention and the existing `!ticket-*.md` negation in `.gitignore`
(a `bug/` subdirectory under `working_docs/` is ignored by `.thoughtmachine/working_docs/*`
and would need a new negation line).

---

## Symptom (operator report)

1. **Duplicate tool bubbles.** The same tool call renders twice.
2. **Results land in the wrong place.** With parallel tool calls, `tool_result` rows appear
   in *arrival* order rather than attached to the call that produced them. A call that finishes
   fast "steals" the row of a call that started earlier.
3. **No `start → call → result` merging.** The pending/start row, the call row and the result
   row are three separate bubbles instead of one evolving row.

## Correction to the original premise

The brief attributed this to `SessionTab.jsx` and described the reasoning-streaming half as
"React state swallows the live deltas". **Both halves of that premise are stale:**

- `SessionTab.jsx` has **no** `tool_call_start` / `tool_call` / `tool_result` cases at all. It only
  has the worker-panel forwarders (`worker:tool_call_start|worker:tool_call|worker:tool_result`,
  `SessionTab.jsx:1018-1020`) which hand the event to `onWorkerEventRef`. Main-agent tool rows are
  built by the adapter below.
- **Reasoning streaming is already implemented and merged** — `SessionTab.jsx:623-633` handles
  `case 'reasoning_delta'` → `pushDelta('reasoning_content', msg.text || '')` with per-frame
  coalescing (`inFlight = { content, reasoning_content }` at `:117`, merged into the rendered
  history at `:154-168`, cleared on `conversation_changed`). Introduced by `ab94020de`
  (2026-10-28), the tip of `feat/reasoning-streaming-and-render` = **PR #231, already merged into
  `dev`**. Nothing to do there; do not re-implement.

## Actual root cause

`web_ui/frontend/src/components/chat/adaptWorkerEvent.js`, `eventId()` (~lines 25-38):

```js
function eventId(evt) {
  const parts = []
  if (evt.session_id) parts.push(evt.session_id)
  if (evt.worker_name) parts.push(evt.worker_name)
  if (evt.instance_id != null) parts.push(`i${evt.instance_id}`)
  if (evt.instance_label) parts.push(`l${evt.instance_label}`)
  if (evt.timestamp) parts.push(evt.timestamp)
  parts.push(evt.event || '')   // <-- the EVENT NAME is part of the identity
  return parts.join('_')
}
```

`evt.event` differs per lifecycle phase, therefore `tool_call_start`, `tool_call` and
`tool_result` **for one and the same tool call can never produce the same `_id`**. Dedup in the
chat reducer keys on `_id`, so:

- `tool_call_start` and `tool_call` both create a row → **duplicate bubble**.
- `tool_result` creates a third row, appended in arrival order → **wrong pairing** under
  parallelism. Cross-call mixing is possible once timestamps collide or are absent.

`tool_call_id` is **never read** by `adaptWorkerEvent.js` or `MessageBubble.jsx` (grep: 0 hits).
The data exists on the backend — `agent/core/tool_executor.py:261` (`"tool_call_id": tc["id"]`)
and `:273`/`:294` (`{'role': 'tool', 'tool_call_id': tool_call['id'], 'content': ...}`) — the
frontend simply never wires it up.

Related paths (note the real location of MessageBubble): `web_ui/frontend/src/components/chat/MessageBubble.jsx`
(role→label map at `:46`, `case 'tool_result'` at `:205`). Existing tests:
`web_ui/frontend/src/components/__tests__/adaptWorkerEvent.test.js` (already has a
`describe('tool_call_start')` block).

## Required fix — pair by `tool_call_id`, mutate a row instead of appending

Treat `tool_call_id` as the identity for tool lifecycle events and drive a small state machine per
tool call:

| event | action |
|---|---|
| `tool_call_start` | insert **one** pending row keyed `tool:<tool_call_id>`, status `pending` |
| `tool_call` | **update** that row in place (args/name, status `running`); do not append |
| `tool_result` | **update** the same row (output/error, status `ok`/`error`); do not append |

Consequence: a row's position is fixed when the call *starts*, so two parallel calls finishing in
reverse order still read in call order. Preserve this for the `worker:`-prefixed variants too.

## Open verification — do this BEFORE writing the fix

Confirm the WebSocket payloads for `tool_call_start` and `tool_result` actually carry the tool-call
id (most likely `evt.request.tool_call_id`, possibly `evt.tool_call_id`). The backend tool-result
message carries it (`tool_executor.py:273`), but the **bridge/event-bus emission** is not verified
from the session that filed this ticket. If the id is missing from the emitted event, the backend
must expose it first — in that case this ticket is blocked on a small bridge change and the
frontend work cannot be done standalone. Do not guess the field name; read the emitter.

## Acceptance criteria

- React test firing `tool_call_start` → `tool_call` → `tool_result` for **two tools, with results
  arriving in reverse order**, asserting:
  - exactly **two** tool bubbles (not three, not four);
  - each bubble carries its own result;
  - bubble order equals call-start order, not result order.
- Existing `adaptWorkerEvent` tests still pass (`npm test` = `vitest run` in `web_ui/frontend`).
- No regression in the worker panel path (`worker:tool_call*` forwarders in `SessionTab.jsx`).

## Out of scope

- Reasoning streaming (done in #231).
- Worker stop cascade (done in #236).
