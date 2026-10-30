# ticket: `bug/panels-poll-instead-of-subscribe`

**Filed:** 2026-10-29 (Gen 54 delivery arc) — **merge of two predecessors**, both now superseded:
`ticket-bug-live-view-container-stale-no-push.md` (filed Gen 43, 2026-10-14 at `d378c206`;
reproduced Gen 47, 2026-10-20 at `124477b7`) and `ticket-hygiene-panel-polls-no-subscribe.md`
(filed in wave 9c, FILE ONLY). Their bodies are kept in place; both point here.
**Status:** open — not dispatched. Candidate for the UI sprint.
**Severity:** medium — visibility only; no data loss, no security impact. Reclassified from
`hygiene` to `bug` because the stale window is reproducible wrong-state display (two live
reproductions), not only a latency/cost tradeoff.

## Symptom

One mechanism, two faces:

1. **Observed (operator):** a container that was just created — or just removed — is invisible
   in the panel's live view until the operator manually reloads the page (F5). The container set
   can change twice in a run and the panel shows neither change.
2. **Mechanism:** the panel refreshes container/workspace state by **polling** status endpoints
   on a timer — `/api/container/status`,
   `/api/workspace/{id}/containers/{name}/status`, and the analogous `CheckSystem`-backed
   routes (`container_status`, `runtime_state`). It never subscribes to the `EventBus` /
   websocket, so it cannot receive push updates and its freshness window is exactly one poll
   interval.

## Evidence

- Reproduced live **2026-10-20** on build `124477b7` (gen-47 container-ops run):
  `report-gen47-container-ops-run-2026-10-20.md` §Step 2 (line 65) and §Step 5 (line 109).
  During that run the container set changed twice (create at §Step 2, removal at §Step 9) and
  the panel showed **neither** without a manual reload — so the stale window is not tied to one
  transition type (create vs remove), and not to one endpoint.
- Originally filed **2026-10-14** at `d378c206` from live operator observation.
- The `EventBus` exists and publishes container/workspace lifecycle events; the panel does not
  subscribe to it.
- Polled routes confirmed in the tree: `GET /api/workspace/{workspace_id}/containers/{name}/status`
  = `web_ui/backend/server.py:4907`; `GET /api/workspace/{workspace_id}/containers` = `:4849`.
- A poll can miss a transient state entirely (a container that starts and exits between ticks)
  that an event stream would deliver; and polled truth and push-driven truth can disagree for up
  to one interval — the same display-truth drift class wave 9c was tightening.

**Two blockers a push-only fix inherits (this is why the two tickets were one defect):**

- `ticket-bug-container-lifecycle-events-missing.md` — the bus does not emit every transition
  today. Subscribing to a bus that never publishes a removal does not fix removals.
- `ticket-hygiene-eventbus-subscribe-returns-none-bridge-leaks-listeners.md` — subscribing
  correctly is itself an unfixed defect: the subscribe call returns `None` and the bridge leaks
  listeners. A panel that subscribes naively will leak on every mount.

## Options (needs a ruling; do not implement blind)

1. **Push-first, poll-fallback.** Subscribe the panel to the `EventBus` / websocket for
   container + workspace lifecycle events; keep polling only as a low-frequency reconciler. The
   right shape, but it depends on the two blockers above and therefore cannot be bounded without
   them.
2. **Fix the bus first, then the panel.** Emit the missing lifecycle events and fix
   subscribe/teardown, then wire the panel. Correct order; means this ticket starts with two
   other tickets.
3. **Make polling honest now.** Shorten the interval for *known-pending* transitions (arm a
   burst poll on the panel's own create/remove actions) and show a visible "as of HH:MM:SS"
   timestamp so staleness is at least legible. Cheap, ships before the bus work, and does not
   make the display lie — it labels it.
4. **Manual refresh affordance made honest.** Explicit refresh button plus the timestamp from
   option 3. Smallest possible; leaves the panel stale by default.

## Acceptance criteria

- A container created or removed from the panel appears/disappears without a manual page reload,
  for both transition types (create **and** remove).
- Freshness is either push-driven or visibly timestamped — never silently stale.
- If polling remains (as fallback or interim), its interval is bounded and its cost does not
  scale with the number of open panels.
- Any subscription taken by the panel is torn down on unmount (the listener-leak defect must not
  be reproduced by the fix).
- A test that asserts the panel state follows a lifecycle event, not the poll timer.

## Related

- Superseded predecessors: `ticket-bug-live-view-container-stale-no-push.md`,
  `ticket-hygiene-panel-polls-no-subscribe.md`.
- `ticket-bug-container-lifecycle-events-missing.md` — prerequisite: the bus does not emit every
  transition.
- `ticket-hygiene-eventbus-subscribe-returns-none-bridge-leaks-listeners.md` — prerequisite: the
  subscribe path itself returns `None` and leaks listeners.
- `ticket-bug-checksystem-containers-denied-by-allowlist.md` — agent-side container visibility
  (distinct surface: this ticket is the operator-side one).
- `ticket-bug-container-remove-ui-no-op.md` — same UI area; a remove that no-ops and a remove
  that is invisible look identical to the operator. Read together.
