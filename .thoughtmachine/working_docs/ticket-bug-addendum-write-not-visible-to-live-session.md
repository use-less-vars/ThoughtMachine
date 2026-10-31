# ticket: `bug/addendum-write-not-visible-to-live-session`

**Filed:** 2026-10-31 (W1.5 review of Task 1 trace)
**Status:** open
**Severity:** high

## Symptom

An operator's **accepted** addendum write does not reach a live session's system prompt.
`POST /api/session/{id}/addendum` loads a freshly deserialised `Session` **copy**, mutates that
copy's `addendum`, and saves it; the running `Agent` keeps composing from its own in-memory
`Session`, so the model never sees the entry on the next turn. The write is durable and the API
returns `status: 'accepted'` — it is simply invisible to the turn that follows it.

The observable split: the config panel shows the entry (its `GET` re-reads the store, so it sees
the new set) while the model does not have it — **UI/model divergence on one fact**. That makes
the whole W1.5 inlet inert for the normal case (any session with a live Agent); it is only
effective after a genuine reload (WS `load_session`, cold start, bridge restart).

Corollary — **INFERRED** (mechanism traced through the code, not executed): `bridge.save_session`
persists `self._session`, whose `addendum` is the stale copy, so a later full-session save can
silently **revert** the accepted write.

## Evidence

Read path (every request, no reload anywhere on it):

- `web_ui/backend/server.py:1459-1467` — WS `continue_session` CASE 2 (agent running) →
  `bridge.continue_session(query, config_dict)`.
- `web_ui/backend/bridge.py:1689-1696` — delegates to `self._controller.continue_session(query)`;
  `:1713-1714` — the queue-only path (`self.resume(); self._query_queue.put(query)`). No load, no
  session swap on either.
- `web_ui/backend/bridge.py:3293-3324` — `_run_loop` runs each queued query as
  `self._agent.process_query(query)`.
- `web_ui/backend/bridge.py:1654` — the `Agent` is built **once** (`Agent(agent_config,
  session=session)`) and is never re-pointed at a reloaded Session afterwards.
- `agent/controller/__init__.py:224-226` (scenario 2: `resume()` + `query_queue.put`) and `:732`
  (`for event in local_agent.process_query(query)`) — the same Session object throughout.
- `agent/presenter/session_lifecycle.py:343` — `load_session_by_id(session_id,
  target_session=None)` **does** support the in-place update this needs (`:356-362`:
  `loaded.to_persistable_dict()` → `target_session.update_from_persistable_dict(...)`), and has
  **ZERO callers under `web_ui/`**.
- `agent/core/llm_client.py:225-227` — the system prompt is composed from
  `self.session` (via `render_addendum_block`) at completion time ⇒ a stale `addendum` is what the
  model gets.

Why the copy is a distinct object (not a shared alias):

- `session/store.py:437/463/467` — `load_session` deserialises: `Session.from_persistable_dict(
  json.load(...))`, i.e. a new object with new dicts.
- `session/models.py:435-480` — `from_persistable_dict` rebuilds the fields, including
  `addendum=normalize_addendum(data.get('addendum'))`; `agent_instance=None` (`models.py:258`, set
  only at `models.py:589`), so the copy carries no reference back to the live Agent.

Why no live notification happens either:

- `session/addendum.py:109-122` — `_enqueue` reads `session.addendum_queue` and is a **no-op** on
  the queue-less store copy (the queue is attached only by the live `Agent`,
  `agent/core/agent.py:411-415`). Its docstring states this is deliberate: "the entry is already
  persisted, and the queue is a delivery detail, never a precondition for the write." Correct for
  delivery; the persistence it relies on is exactly what the live turn does not read.
- `web_ui/backend/addendum_routes.py` (committed `021c4001`) — the write path: `_load_addendum_session`
  (`:88-101`) → `write_addendum_entry` (`:210`) → `store.save_session` (`:219`). Transport-only by
  design; it never touches a running Agent (`:22-24`).

## Options (needs a ruling; do not implement blind)

**This needs a ruling.** All three change who owns the live `addendum`; picking one by
implementation would silently settle that ownership question.

1. **Write through the live session when one exists.** The REST route delegates to the bridge that
   owns `self._session` instead of loading from the store; the write then mutates the very object
   the Agent composes from (and `_enqueue` reaches the real queue, so the UI is notified on the
   same path). `agent/core/agent.py:411-415` already anticipates a live session carrying the queue.
   Cost: the route grows a "is a bridge live?" branch with its own concurrency story (a request
   thread mutating the session the turn thread is reading), and the idle case still needs the
   store path — two write routes to keep in step.
2. **Reload at the turn boundary.** Wire
   `session_lifecycle.load_session_by_id(session_id, target_session=live_session)` before
   `process_query` so every turn composes from store state. The helper already exists and already
   does the in-place update; it is unused by `web_ui/` today. Cost: a store read (and a
   deserialise) per turn on the query's critical path, and the live Session's other fields are
   overwritten from disk on every turn too — which is a semantic change well beyond the addendum.
   **Note: option 2 alone may not fix the INFERRED clobber** — it only guarantees the *read* is
   fresh; a later `bridge.save_session` of the in-memory session can still write a stale set back
   unless saves also reload before persisting.
3. **Make the store the single source of truth for `addendum`.** Stop holding it as mutable state
   on the Session and have the Agent re-read `session.addendum` from the store per request (the
   composition site is one function, `llm_client.render_addendum_block`). Cost: a store read inside
   prompt composition (which currently does no I/O), and it inverts the slice-1 decision that the
   addendum is a first-class *session field* rather than a sidecar — a wider contract change than
   either of the above.

## Acceptance criteria

- Write an entry via `POST /api/session/{id}/addendum` while a session is **live** (Agent running
  mid-turn or idle between turns) **and** while it is **idle/unloaded**; the next turn's composed
  system prompt contains the entry text in **both** cases.
- A subsequent full-session save (config apply / save-open / close) does **not** drop or revert the
  accepted entry.
- A regression test proves the live and idle paths behave the same — one test driving the live
  bridge and one driving the store-only path, asserting on the composed prompt, not on the store.
- Refusal behaviour is unchanged: a refused write still persists nothing and still returns 409.

## Related

- W1.5 backend `021c4001` (`web_ui/backend/addendum_routes.py` — the write path that is inert here).
- Slice 5 `f092ab39` (budget enforcement at the same write boundary).
- `ticket-feat-addendum-clear-action.md` — the sibling W1.5 surface; the clear has the same
  live-session question.
- `ticket-hygiene-addendum-budget-math-duplicated.md` — the other known seam in this inlet.
- `docs/design-sysprompt-addendum-and-injection.md` — the composition contract this violates.
