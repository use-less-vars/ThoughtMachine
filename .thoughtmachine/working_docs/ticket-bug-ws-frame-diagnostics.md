# Ticket: bug/ws-frame-diagnostics

**Filed:** 2026-10-29, from a read of the WebSocket fan-out and the client's frame dispatch.
**Severity:** medium — diagnosability, not data loss on its own; it is the reason the *other*
frame bugs are unfalsifiable.
**Status:** filed; no repro needed (the absence is the finding).
**Branch to fix on:** — (not dispatched)

---

## Observed

The whole server-side fan-out is 13 lines, `web_ui/backend/event_forwarder.py:84-96`:

```python
    84    def broadcast(
    85        self,
    86        _session_id: str,
    87        event_type: str,
    88        data: Dict[str, Any],
    89    ) -> None:
    90        """Broadcast an event to every registered callback."""
    91        for cb in list(self._callbacks.values()):
    92            if cb is not None:
    93                try:
    94                    cb({"type": event_type, **data})
    95                except Exception:
    96                    traceback.print_exc()
```

**1. The session argument is decorative.** `_session_id` (86) is unused; the envelope is
`{"type", **data}` (94) and every registered socket gets every frame. The consequence is
already documented at the call site — `web_ui/backend/bridge.py:3148-3157`:

```python
   3149            # Token-level assistant text. Rendered into an in-flight bubble by
   3150            # the frontend and reconciled against the terminal `turn`
   3151            # conversation_changed. session_id rides INSIDE the payload because
   3152            # the forwarder's broadcast() ignores its session_id argument.
   3153            self._forwarder.broadcast(self._session_id, "assistant_delta", {
   3154                "session_id": self._session_id,
```

i.e. every frame is broadcast to every tab and each client repairs the mistake locally.

**2. Nothing counts.** `_callbacks` is keyed by ws id (`:46`). There is no counter, no
per-callback delivery log, no sequence number and no message id. Two identical frames are
indistinguishable from one frame delivered twice — "duplicate" is not measurable, only tolerated
(the tolerance is written down at `web_ui/frontend/src/App.jsx:518-524`: *"SessionTab's own
WebSocket also delivers state_changed — store writes are idempotent, so a duplicate is
harmless"*). A real duplication bug in the fan-out looks exactly like the accepted one.

**3. Failures are stderr and a shrug.** `:95-96` prints a traceback and continues. A callback
that raises on every frame produces unbounded tracebacks and no structured signal; `_callbacks`
still reports N healthy registrations.

**4. Zero recipients is silent.** `broadcast` never notices an empty `_callbacks`. A registered
ws whose browser tab died without a clean close stays registered (`unregister_websocket`,
`:64-66`) and silently swallows frames until something clears it.

**5. Registration can evict silently.** `register_websocket` (`:52-62`):

```python
    57        """Register an event callback by WebSocket id.  If *key* is None, replaces all callbacks."""
    58        if key is None:
    59            self._callbacks.clear()
    60            self._callbacks[id(callback)] = callback
    61        else:
    62            self._callbacks[key] = callback
```

A tab that registers with a null key **clears every other tab's callback** and then registers
itself. No log line, no count — the evicted tabs simply go quiet.

**6. Client-side, every diagnosis is a console call.** All of these are `console.*` only, never
surfaced in the UI, never reported to the server:

| site | condition |
| --- | --- |
| `web_ui/frontend/src/components/SessionTab.jsx:350-356` | frame fails `JSON.parse` → `console.error` |
| `:612-616` | `assistant_delta` for a different session → `console.warn` + drop |
| `:623-627` | `reasoning_delta`, same |
| `:635-639` | `conversation_changed`, same |
| `:735-742` | `config_queued`, same |
| `:1133-1134` | unknown event type → `console.warn('[SessionTab] Unknown event type:')` |

(The file does have ad-hoc `console.log` lines elsewhere, e.g. `:607` `[TOKEN_PIPELINE]`; the
point is that none of them is a *frame* diagnostic, none is countable, and none survives
outside the operator's devtools.)

## Why it matters

The symptom family — a delta that never arrives (truncated or rolled-back bubble), a frame
rendered in the wrong session, a tab that goes quiet — is currently unresolvable after the fact.
For any given frame there are four mutually exclusive explanations and no way to tell them apart:

1. never emitted by the bridge;
2. emitted and dropped by the fan-out (dead callback, evicted callback, zero recipients);
3. delivered and discarded by a client cross-session guard;
4. delivered twice and absorbed by idempotent store writes.

Each is a single counter away from being decided, and none of the counters exists. This is also
the reason a bug report of the form "the UI lost a frame" cannot be turned into a test today: the
observation is unrepeatable.

## Proposed fix sketch (additive, no behaviour change required)

1. **Count in `broadcast`.** Per-instance `{"broadcasts", "deliveries", "failures", "empty"}` plus
   a monotonic `seq`; put `seq` in the envelope at `:94` so the client can detect a gap (missing
   frame) as well as a duplicate — turning the tolerated duplicate (`App.jsx:518-524`) into a
   measured one.
2. **WARN on anomalies only.** One WARNING when a broadcast reaches zero callbacks (include the
   event type and `len(self._callbacks)`), one when a callback raises (already printed — make it
   structured). No INFO logging in the per-delta path: a delta stream is thousands of frames per
   turn.
3. **Either use or remove `_session_id`.** Filtering in `broadcast` would retire the
   `bridge.py:3150-3153` workaround and demote the four client guards to a fallback; deleting the
   parameter makes the call sites honest. Two different tickets depending on the answer to
   OPERATOR TO CONFIRM #3.
4. **Expose the numbers.** One operator-visible read — either a line in the existing
   log/health panel or a `GET /api/debug/ws_stats` returning `_callbacks` keys + the counters.
   No dashboard.
5. **One client funnel.** Replace the six `console.*` sites with a single
   `reportFrameIssue(kind, msg)` that warns **and** increments a counter on
   `window.__tmWsDiag` (`{received, dropped, unknown, parse_errors}`), optionally sending a
   `client_frame_diag` frame back so the server's counters see drops too. That round trip is the
   only way to correlate the two halves of a missing frame.

## Acceptance criteria

- A broadcast to N registered sockets reports `deliveries += N`; a callback that raises reports
  `failures += 1` and the *remaining* callbacks still receive the frame.
- A broadcast with zero registered callbacks logs exactly one WARNING naming the event type.
- Every emitted frame carries a `seq` increasing by exactly 1 per broadcast; a replayed frame is
  detectable as a duplicate by the client helper (unit test, no browser).
- With two tabs open, closing one does not silence the other, and `_callbacks` returns to the
  expected size (pins the `register_websocket(None, ...)` eviction at `:58-60`).
- After injecting one deliberate cross-session frame, `window.__tmWsDiag.dropped` is non-zero
  without attaching a debugger.
- Under a simulated 1000-frame turn, no INFO-level line is emitted from the delta path (assert on
  log volume, not just on content).

## OPERATOR TO CONFIRM

1. **Where should the counters be readable** — the existing log/health panel, or a dedicated
   route? And is `window.__tmWsDiag` acceptable, or must the frontend push a diag frame so the
   whole record stays server-side?
2. **Is `seq` per-connection or per-session?** Per-connection is trivial and catches duplicates.
   Per-session also catches "this tab never received frame 41", which is the interesting failure —
   but it needs a decision on who owns the counter (the forwarder instance is per-bridge).
3. **Does session-scoping move into `broadcast`?** That retires `bridge.py:3150-3153`'s
   documented workaround and four client guards, but it is a behaviour change in the fan-out, not
   diagnostics. If out of scope, this ticket stays purely additive and the guards stay primary.
4. **Is the `register_websocket(None, ...)` eviction (`:58-60`) its own bug?** It is one line to
   read and looks like a second-tab-opened-everything-went-quiet bug, but the fix (register
   alongside, or refuse a null key) changes tab semantics — a separate decision, separate ticket.
