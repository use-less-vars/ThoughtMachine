# Ticket: hygiene/eventbus-subscribe-returns-none-bridge-leaks-listeners

**Status:** **recorded, NOT fixed** — out of scope of `fix/worker-panel-payload-drop`. Nothing in this document is code that exists; it is a defect description plus options.
**Filed:** 2026-10-22, surfaced while writing the payload-drop fix (see `ticket-bug-worker-panel-intermittent-missing-tool-calls.md:97`, where it was first recorded as a bullet). Line numbers read from the working tree of `ThoughtMachine-dev` at the time of writing.
**Severity:** LOW for correctness today, MEDIUM for longevity — the leak is bounded per bridge instance and every leaked handler is session-guarded, so it does not corrupt the panel. It becomes load-bearing if the session guard is ever removed, or on a long-lived server where each reconnect adds another permanent subscriber set to a process-global bus.
**Fix type:** hygiene / resource lifetime. No data loss, no user-visible symptom, no test asserts on it.

## The defect

`EventBus.subscribe()` registers the callback and then **returns nothing**, so there is no handle a subscriber can store and hand back to `unsubscribe()`. `WebAgentBridge` stores the return value anyway, in eight named attributes, all of which are therefore permanently `None`:

`agent/events.py:455-469`

```python
def subscribe(self, event_type: Optional[EventType]=None, callback: callable=None):
    """Subscribe to events of specific type or all events."""
    if callback is None:

        def decorator(func):
            self.subscribe(event_type, func)
            return func
        return decorator
    with self._lock:
        if event_type is None:
            self._wildcard_subscribers.append(callback)
        else:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
```

The registration branch (`:463-469`) has no `return`; the only `return` in the method is the decorator branch at `:462`. So `subscribe(...)` returns `None` on the call form the bridge uses, and returns a decorator on the partially-applied form.

`WebAgentBridge` keeps eight such handles:

- `bridge.py:463-471` — all initialised to `None`: `_security_subscription` (`:463`), `_worker_spawned_sub` (`:465`), `_worker_status_sub` (`:466`), `_worker_completed_sub` (`:467`), `_worker_error_sub` (`:468`), `_worker_partial_result_sub` (`:469`), `_worker_token_warning_sub` (`:470`), `_worker_message_sub` (`:471`).
- `bridge.py:562-564` — `self._security_subscription = global_event_bus.subscribe(EventType.SECURITY_PROMPT, _security_prompt_handler)`.
- `bridge.py:618-638` — the seven worker-lifecycle subscriptions inside `_subscribe_to_worker_events`, each written the same way (`_worker_status_sub`, `_worker_partial_result_sub` and `_worker_message_sub` take the `_make_handler(...)` closures built at `:580-616`; the other four take bound methods of the bridge).

Teardown then has nothing to tear down — and it fails silently at two levels. `_unsubscribe_worker_events` (`bridge.py:784-808`):

```python
        for attr_name, in worker_subs:
            sub = getattr(self, attr_name, None)
            if sub is not None:
                try:
                    if hasattr(global_event_bus, 'unsubscribe'):
                        global_event_bus.unsubscribe(sub)
                except Exception:
                    pass
                setattr(self, attr_name, None)
```

and `_unsubscribe_security_events` (`bridge.py:772-782`) is the same shape for the one security handle. Because every handle is `None`, the `if sub is not None:` guard at `:797` skips the call entirely — so the arity bug in the next line (`global_event_bus.unsubscribe(sub)` passes **one** positional argument to a two-parameter `unsubscribe(self, event_type=None, callback=None)` at `agent/events.py:492`) is **never executed on the real path**. That matters for diagnosis: the wrong-arity call does not raise `ValueError` here, and even if it were reached it would not surface, because `unsubscribe` binds `sub` to `event_type` and `callback=None`, lands in the wildcard branch, and swallows the resulting `ValueError` at `agent/events.py:496-499`. `except Exception: pass` at `:801-802` would hide anything else.

Net effect: **every `WebAgentBridge` instance leaves eight callbacks permanently attached to the process-global `global_event_bus`**, each holding a strong reference to the bridge — four bound methods (`_on_worker_spawned`, `_on_worker_completed`, `_on_worker_error`, `_on_worker_token_warning`), three closures over `self` from `_make_handler`, and `_security_prompt_handler`. Nothing leaks the *event data*; what leaks is the bridge object graph and the publisher's per-event work.

The per-worker buses are **not** affected: `_unsubscribe_worker_bus` detaches with the correct two-argument form against the stored bus (`bridge.py:1137-1141`), so this defect is scoped to the global bus only.

### Illustrative repro (not a test, not run)

```python
from agent.events import EventBus, EventType, create_event

bus = EventBus()
seen = []
handle = bus.subscribe(EventType.WORKER_SPAWNED, lambda e: seen.append(e))
print(handle)                                   # None — nothing to pass back
bus.publish(create_event(EventType.WORKER_SPAWNED, data={}, source="demo"))
bus.unsubscribe(handle)                         # 1 arg -> binds to event_type, callback=None
                                                # -> wildcard remove(None) -> ValueError, swallowed
bus.publish(create_event(EventType.WORKER_SPAWNED, data={}, source="demo"))
print(len(seen))                                # 2 — still subscribed; nothing raised, nothing logged
```

## Why it was invisible

1. **`publish` cannot fail loudly.** `EventBus.publish` (`agent/events.py:471-485`) copies both subscriber lists under the lock and then invokes each callback inside `try/except Exception`, logging `core.event_bus` and continuing. A leaked *or* a dead subscriber is therefore indistinguishable from a healthy one at the publisher.
2. **`unsubscribe` cannot fail loudly either.** `agent/events.py:492-507` wraps both `list.remove(callback)` calls in `try/except ValueError: pass` and treats "not present" as success — by design, since it is meant to be idempotent.
3. **The teardown never even attempts the call.** The `if sub is not None:` guard at `bridge.py:797` makes the code look correct on a read-through ("only unsubscribe if we have a handle") while guaranteeing the branch never runs. The attribute list is spelled out explicitly at `:786-794`, which reads as diligence.
4. **No counter, no log, no assertion.** Nothing reports subscriber-list length. The only place this defect is written down today is a test-fixture docstring — `tests/test_worker_bus_respawn_bridge.py:45-56` explains that the per-test unique `session_id` exists precisely because `_unsubscribe_worker_events` cannot detach and earlier bridges stay subscribed forever. That is a workaround documented as a fixture, not a failing test.

## Blast radius, honestly bounded

- **Growth is per bridge instance, not per event.** Bridges are constructed at `server.py:1352`, `:1629`, `:2463`, `:3067`, `:3299`; the server's `_session_bridges` map is popped at `:2808`, `:2992`, `:2996`, `:3005`, which drops only the server's reference — the bus keeps its callbacks. Each tab, each reconnect that builds a new bridge, and each `apply_config` rebind adds another set.
- **Cross-session effects are filtered, not prevented.** `_make_handler`'s closure checks `if data.get('session_id') and data['session_id'] != self._session_id: … return` (`bridge.py:589`), `_on_worker_spawned` has its own session guard (`:836`), and a closed bridge's `_session_id` is set to `None` (`:2579`), which also fails that comparison. So leaked handlers are inert for other sessions' events.
- **Two handlers are unguarded.** `_on_worker_completed` (`bridge.py:1218-1229`) and `_on_worker_error` (`:1231-1242`) forward, then call `_unsubscribe_worker_bus(...)` **unconditionally** — the guard, if any, is inside `_forward_worker_event` (`:1244+`), i.e. after the fact. A leaked bridge therefore does dictionary work for every other session's completion, popping an entry from its own `_worker_bus_subs`. Bounded and harmless (the dict is per-bridge), but it is work on a dead object and it is exactly the kind of thing that stops being harmless when the session guard is refactored.
- **Not a user-visible bug today.** No event is misrouted and no row is dropped because of this. The sibling ticket (`ticket-bug-worker-panel-intermittent-missing-tool-calls.md`) is a *different* defect with a different blast radius — dropped worker events — and a fix for one is not a fix for the other.

## Options

**Option 1 — give `subscribe()` a return value.** `return callback` at the end of the registration branch (or a small `Subscription` object). This is the "obvious" fix and it makes `unsubscribe(handle)`-style code work. Cost: it changes a process-wide, widely-used API (`agent/events.py` is consumed by the agent loop, tools, and the web UI), it must not disturb the decorator branch at `:457-462`, and it makes every existing call site's ignored return value *look* meaningful. Any new handle type would also need a decision about what `unsubscribe` does with it. Broad blast radius for a hygiene bug.

**Option 2 — store the pair the bridge already knows (RECOMMENDED).** The call site has both arguments at subscribe time; `unsubscribe(event_type, callback)` already accepts exactly them. Replace the eight `*_sub = global_event_bus.subscribe(...)` assignments with a list of `(event_type, callback)` tuples filled by one small helper (e.g. `self._global_subs.append((EventType.WORKER_SPAWNED, self._on_worker_spawned))`), and make `_unsubscribe_worker_events` / `_unsubscribe_security_events` iterate that list, call `global_event_bus.unsubscribe(event_type, callback)` behind the existing `hasattr` guard, and then clear it. No producer-side change, no API change, no arity bug to get right — the arity problem disappears because the arguments are supplied in the correct shape. Everything here is one file (`web_ui/backend/bridge.py`) and one constructor-initialised list.

**Recommended:** Option 2. It is call-site-only, it is reversible, and it is the option whose test is trivial to write: build **two** bridges with the *same* session id in one process, `stop()` the first, publish a matching event, and assert the stopped bridge's callback is no longer invoked. That test is currently impossible to write honestly — which is why `tests/test_worker_bus_respawn_bridge.py`'s fixture works around it instead — so it doubles as the RED test.

**Do nothing (the status quo).** Acceptable while bridges are short-lived and the session guard holds; not acceptable as the substrate for any future "make the bridge session-scoped" work, because that work will remove the guard that is currently containing this leak.

## Cross-reference

- **First recorded in** `ticket-bug-worker-panel-intermittent-missing-tool-calls.md:97` ("The global-bus unsubscribe handles are always `None` … recorded, not bundled"), which is where this ticket's evidence came from. Two corrections to that bullet, for whoever picks this up: it is **eight** handles (seven worker + `_security_subscription`), and the one-argument `unsubscribe` call it flags is unreachable behind the `None` guard, so it never raises.
- **Documented as a workaround, not as a bug:** `tests/test_worker_bus_respawn_bridge.py:45-56` — the `session_id` fixture's docstring states the symptom ("bridges from earlier tests in the same process stay subscribed to `global_event_bus` forever") and the mitigation (a unique session id per test keeps them inert). Fixing this defect lets that fixture docstring shrink; it must not be "fixed" by deleting the workaround first.
- **Not fixed in this branch:** `fix/worker-panel-payload-drop` is bridge-side payload delivery. Option 2 would rewrite the teardown (`stop()` at `bridge.py:1674`, which calls `_unsubscribe_worker_events()` at `:1681`, and `close_session()` at `:2532` → `:2549` → `:2579`) that the payload-drop fix deliberately left alone, and Option 1 is an `agent/events.py` API change with a process-wide blast radius. Neither belongs in a delivery fix.

## MUST NOT TOUCH

- `agent/events.py:471-485` — the publish loop's copy-then-invoke-under-no-lock shape, and the per-subscriber `try/except` with a log. Both are the reason a raising subscriber loses only its own events; tightening it to propagate would turn one bad subscriber into a global outage.
- `agent/events.py:492-507` — `unsubscribe`'s idempotent, silent `ValueError` handling. Callers rely on double-unsubscribe being safe.
- `tests/test_worker_bus_respawn_bridge.py:45-56` — do not weaken the per-test `session_id` while this defect exists; it is the only thing keeping the leaked listeners inert across tests in one process.
