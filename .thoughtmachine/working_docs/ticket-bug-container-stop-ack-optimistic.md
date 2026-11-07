# Ticket: bug/container-stop-ack-optimistic

**Filed:** 2026-10-29, from a read of the three tiers of the stop path
(`ContainerLifecycle` → `ContainerManager._stop_container` → the container-view route).
**Severity:** medium — the durable record, the lifecycle event log and the HTTP response all
assert `stopped` without any tier observing that the container stopped.
**Status:** filed; static finding complete. Needs one live repro to confirm the visible half.
**Branch to fix on:** — (not dispatched)
**Non-overlap:** `ticket-fix-funnel-container-manager-stop-remove.md` asks for `stop()` /
`remove()` to be *routed through* the lifecycle funnel. This ticket is about the
*acknowledgement*: on whatever path is taken, "stopped" is reported before it is verified.
Fixing one does not fix the other.

---

## Symptom

`POST /api/workspace/{ws}/containers/{record_id}/action` with `{"action": "stop"}`
(`web_ui/backend/server.py:5440-5443`) answers **HTTP 200** with the container-view entry and
the container record reads `state="stopped"`, while the container is still there.

Cheapest repro for the structural half: stop a container whose id no longer exists
(`docker rm -f <id>` out of band, then press Stop) — the route still returns 200 and the
record still flips to `stopped`. For the alive half: `docker pause <id>`, then press Stop.

## Finding — three tiers, none of which verify

### Tier 1 — the only state-based guard is production-dead

`infra/container_lifecycle.py:266-278` is the one place in the tree that refuses to stop a
container because of its state:

```python
   266        # (5) Idle handling: only stop a running container.
   267        if intent.idle:
   268            if container is None:
   269                return ReconcileDecision(
   270                    "use", "no container; nothing to stop", "default"
   271                )
   272            if cls._container_status(container) == "running":
   273                return ReconcileDecision(
   274                    "stop", intent.reason or "idle container", "workspace"
   275                )
   276            return ReconcileDecision(
   277                "use", "idle container already stopped", "workspace"
   278            )
```

Nothing in production sets `idle=True`. The only `idle=True` /
`ContainerIntent(kind="idle"` occurrences are in
`tests/test_container_lifecycle_funnel.py:153,163,269,323`. The explicit stop cannot reach the
branch either: `ContainerManager.stop` builds `kind="reap", explicit=True`
(`infra/container_manager.py:3492-3500`) and goes straight to `_stop_container` on
`decision.destroys` (`:3502-3509`).

So the guard exists, is unit-tested, and is unreachable from the product. The real stop path has
no state gate at all.

### Tier 2 — `_stop_container` acks outside the `if`

`infra/container_manager.py:3531-3545`:

```python
   3531        try:
   3532            if container.status == "running":
   3533                _audit("CONTAINER_STOP",
   3534                       f"container={container_id} session={self.session_id}")
   3535                container.stop(timeout=grace)
   3536                container.reload()
   3537                if container.status == "running":
   3538                    container.kill()
   3539            self._drop_container(container_id)
   3540            log_container_event("stopped", container_id=container_id,
   3541                                session_id=self.session_id or "")
   3542            return {"status": "stopped", "container_id": container_id, "name": container.name}
   3543        except Exception as e:
   3544            return {"status": "error", "container_id": container_id, "error": str(e),
   3545                    "name": getattr(container, "name", "")}
```

The stop is conditional; the ack is not. Every outcome that is not exactly
`container.status == "running"` skips `container.stop()` entirely and still gets

* `{"status": "stopped"}` returned to the caller,
* a `log_container_event("stopped", ...)` entry in the lifecycle event log,
* `_drop_container` (`:4620-4623`) — the manager forgets the handle.

Non-`running` states that mean "still alive": `paused` (holds memory and the workspace volume),
`restarting`, `removing`. `dead` and a `reload()` failure (`:3525-3530` only logs a WARNING and
carries on with whatever `container.status` said) are the unreadable-status cases. Note the
`reload()` at `:3525` happens *before* the check, so status is normally fresh-on-fetch; the
exposure is a status change between fetch and check, or a failed reload.

There is also no post-`kill()` verification: `:3537-3538` re-reads status and kills, but nothing
re-reads after the kill, so `stopped` is never *observed* — only assumed from the fact that no
exception was raised.

All three callers trust that ack. `restart` (`:3593-3595`) and `remove` (`:3684-3687`) both gate
on

```python
   3594        if stopped.get("status") not in ("stopped", "missing"):
```

so "did the stop happen?" is answered by the unverified ack, and `restart` proceeds to
`start()` against a container that may still be paused.

### Tier 3 — the HTTP route discards the honest envelope

`ContainerManager.stop` is otherwise careful: it returns `missing` (`:3484-3485`), `error`
(`:3487`, `:3490-3491`) and `refused` (`:3507-3508`), and only stops on `decision.destroys`
(`:3502-3509`). The container-view action route throws all of that away —
`web_ui/backend/server.py:5527-5531`:

```python
   5527    try:
   5528        if action == "stop":
   5529            if container_id:
   5530                manager.stop(container_id)          # return value discarded
   5531            _cr_api.update_record(workspace_id, record_id, state="stopped")
```

and then falls through to `:5558-5562`, i.e. a 200:

```python
   5558    record_after = _cr_api.load_record(workspace_id, record_id)
   5560        return _json_error("record disappeared during action", status_code=500)
   5561    return JSONResponse(
   5562        _container_view_entry(record_after, manager, workspace_id))
```

So `missing`, `error` and `refused` all end in `state="stopped"` on the durable record plus a 200.

The sibling route for the *same verb* does check the envelope —
`web_ui/backend/server.py:5015-5023` (`workspace_container_stop`):

```python
   5016        result = manager.stop(container_id)
   5017        if result.get("status") == "missing":
   5018            return _json_error(result.get("error", "container not found"),
   5019                               status_code=404)
   5020        if result.get("status") == "error":
   5021            return _json_error(result.get("error", "stop failed"),
   5022                               status_code=503)
```

Two HTTP surfaces, one verb, opposite honesty — the Inv.6 shape (one fact computed in two places;
they agree only because one of them never looks).

## Reachability (honest ranking)

| case | needs an operator? | consequence today |
| --- | --- | --- |
| container gone (`missing`) | no — sweeper, out-of-band `docker rm` | 200 + record `stopped` (arguably harmless: it *is* stopped) |
| Docker verb error (`error`) | no — daemon hiccup | 200 + record `stopped`, container still running |
| `refused` by the funnel | unclear (see OPERATOR TO CONFIRM #3) | 200 + record `stopped`, container left running by design |
| `paused` / `restarting` / `removing` | `paused` needs hand-run `docker pause` (nothing in the tree calls `container.pause()`; every `.pause(` hit is a worker/bridge/controller signal) | `stopped` ack, event log entry, handle dropped, container alive |

The `missing`/`error` rows are reachable without an operator and are the everyday half of this
ticket. The `paused` row is the sharp one but needs a human. That ordering is deliberate: the
structural defect (ack written outside the observation) is unconditional; only its worst
consequence is operator-gated.

## Why it matters

1. **The record is the fallback truth.** `_container_view_entry` prefers live Docker status
   (`server.py:4702-4709` → `_map_container_view_state`, `:4446-4477`), so the *immediate*
   response can still read `paused`. The record's `state` is what renders whenever live status is
   unreadable — `manager.status()` raising at `:4705-4708` gives `status=None` and the view falls
   back to the record. The false value surfaces exactly when the system is least able to correct
   it.
2. **The audit trail records a stop that did not happen** — `log_container_event("stopped")` is
   unconditional (`:3540-3541`).
3. **A paused container keeps the workspace volume mounted** and its memory resident. Telling the
   operator it is stopped invites a second container, a resize, or a workspace "reset" against a
   volume that something is still holding.
4. **The manager forgets a live container.** `_drop_container` (`:4620-4623`) removes every
   registry entry pointing at the id; after a false ack, cleanup depends on a sweeper finding it
   by label.
5. **An optimistic write erases the better claim.** `state="stopped"` is "makes no claim" for
   `thoughtmachine/container_record/state.py:181-192` (`recorded` is compared only against
   `running` / `creating`; anything else returns `("", True)`), so writing it over a previous
   `running` removes the staleness signal the resolver would otherwise raise.

## Required fix

In `_stop_container` (`infra/container_manager.py:3511-3545`):

- Observe before acking: after the stop/kill ladder, `reload()` and read the status; emit the
  `stopped` event, call `_drop_container`, and return `{"status": "stopped"}` only when the
  observed status is `exited` / `dead` / absent. Otherwise return a distinct ack
  (`"unverified"`, or `{"status": "error", ...}` with the observed state) and keep the handle.
- A failed `reload()` at `:3525-3530` must make the ack `unverified`, not `stopped`.
- Act on the alive-but-not-running states instead of skipping them (unpause then stop, or `kill`).
- Make the callers' gate (`:3594`, `:3686`) treat `unverified` as failure, not as success.

In `web_ui/backend/server.py:5527-5531`: bind the result and propagate it — `missing` → 404,
`error` / `unverified` → 503, `refused` → 403/409 — and write `state="stopped"` only on a
verified stop. The cleanest form is to share one helper with `workspace_container_stop`
(`:5015-5023`) so the two surfaces cannot drift again.

## Acceptance criteria

- Stop on a healthy container → 200, `state="stopped"`, exactly one `stopped` lifecycle event,
  handle dropped, and the container observed `exited` after the call (assert on the observation,
  not on the return value).
- Container deleted out of band, then Stop → non-200, `record.state` unchanged.
- Injected `reload()` failure → ack is not `"stopped"`; no `stopped` event.
- `docker pause <id>`, then Stop → either the container ends stopped (unpause+stop/kill) with a
  200, or the route returns non-200 with `record.state` unchanged. Never 200 + alive.
- Both HTTP surfaces for stop return the same code class for the same manager result (one shared
  helper, or a test that pins the agreement).

## Out of scope

- Routing `stop()` / `remove()` through the funnel — see
  `ticket-fix-funnel-container-manager-stop-remove.md`.
- Idle *shutdown* (the runtime closing its own idle container):
  `infra/containers/executor.py:1082-1084` calls `self.close()` and discards the result, and
  `close()` (`:1117-1151`) emits `emit_destroy_audit` *before* the stop and lets non-NotFound
  exceptions from `stop()` / `remove()` propagate. Same family, different surface — its own
  ticket.

## OPERATOR TO CONFIRM

1. **Is a paused container reachable in the product?** Nothing in the tree calls
   `container.pause()` (every `.pause(` match is a worker / bridge / controller signal). If
   `paused` only ever comes from a hand-run `docker pause`, say so and the ticket's live half is
   scoped to `restarting` / `removing` / failed-`reload` instead.
2. **Which status should a non-running-but-alive container end in** — unpause-then-stop, or
   `kill`? Product decision, not a code one.
3. **Is `refused` reachable on this route?** `remove` has an explicit class allow-list
   (`server.py:5498-5503`); `stop` has no class gate. If the funnel can refuse a `reap` for any
   class this route can reach, the optimistic 200 is currently hiding a policy refusal.
4. **Does anything depend on the optimistic write?** `state="stopped"` is read as "no claim" by
   the resolver; confirm no panel relies on it to hide a container that is genuinely running.
