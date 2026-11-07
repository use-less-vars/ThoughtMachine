# ticket: `bug/addendum-write-not-visible-to-live-session`

**Filed:** 2026-10-31 (W1.5 review of Task 1 trace)
**Status:** FIXED — implemented on branch `w1-5/writer` (base `18f6dc02`), awaiting operator push/PR.
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

## Resolution

Ruled: Option A (write-through). Invariant: the live session is the runtime authority, the store is the durability authority; every write touches both under one lock before ack; the cold path is an atomic store read-modify-write.

Commits (branch `w1-5/writer`, base `18f6dc02`):

- `37d8adfe` fix(session): make store writes atomic behind one locked read-modify-write — `_atomic_write_json` (temp file in the same directory + `os.replace`, cleanup on failure), `_save_session_metadata` locked on the metadata path, new `mutate_session` where a single `FileLock` spans read → mutator → atomic rewrite (`should_write=False` writes nothing; unknown id returns `(None, None)`), `_read_session_file`, `load_session` refactor. **Condition (3), deliberately its own commit.**
- `84a5412b` fix(bridge): serialise session snapshot+save and carry addendum through the fallback — `_session_state_lock` (`threading.RLock`) created in `__init__` and held around the `_session_manager.save_session` delegate (the store builds `to_persistable_dict()` *before* taking its own `FileLock`, so only a lock above the store closes the snapshot/write window); the end-of-turn fallback (`bridge.py`, around line 2232) now carries `summary` and `addendum` through by `deepcopy` instead of rebuilding them from `user_history`. **The rebuild was the wipe**: it wrote a default empty addendum over the stored one. **Condition (4).**
- `4045ba39` feat(addendum): write through to the live session, read and refuse through one resolver — `_resolve_addendum_session` is the single resolver behind GET/POST/DELETE; `_held_sessions` (`_agent.session` → `_session` → `_loaded_session`, identity-deduped); `_live_bridges` (snapshot of `event_forwarder._active_tab_bridges`, agent-bearing bridges first); `_bridge_lock` (nullcontext fallback); `_write_through` (mutate + persist under the bridge lock, then mirror the accepted set onto the other held copies and the other tabs' copies under their own locks — no lock nesting; cold path delegates to `store.mutate_session`); `_addendum_state(session, boundary)` takes every number from the boundary result; `_refusal_message(result)` uses the boundary's own sentence. The module contains zero `sum(` and zero `len(`. **Conditions (1) and (2).**
- `5324b367` feat(addendum): let the write boundary report its own verdict — `session/addendum.py` results now carry `attempted_chars`, `budget_chars`, `attempted_entries`, `max_entries`, `total_chars`, `message`; new `addendum_total_chars(session)` is the single char count; `notify=False` suppresses both enqueues. **Also condition (2): the HTTP layer never recomputes the budget math.**
- `77354f78` test(addendum): write-through, live-chain and no-recompute coverage (+974 lines).

Reviewer caveat: `4045ba39` and `5324b367` are mutually dependent (the route reads the new boundary fields; the boundary was reshaped for the route). They must be taken together — a bisect landing on `4045ba39` alone is not self-contained. Squash-merging the branch is fine.

Evidence (all runs with `TMPDIR=/tmp`):

- Targeted battery, 16 files: **170 passed, 1 xfailed**. Per file: `test_addendum_write_through.py` 17, `test_addendum_live_chain.py` 2, `test_addendum_routes.py` 8, `test_session_store.py` 38, addendum_{budget 6, drain 11, composition 12, schema 15, survival 1}, `test_permission_routes.py` 28 + 1 pre-existing xfail, bridge_{continue_session_config_log 2, load_session_workspace_scope 10, normalize_for_frontend 2, permissions_sync 5, stop_semantics 6}, `test_bridge_dedup.py` 7.
- Broader sweep, `-k "session or addendum or bridge or persist or store"`: `tests/` → **923 passed, 9 skipped** (4688 deselected, 119.89s); `web_ui/backend/tests/` → **74 passed, 1 xfailed** (6.78s). Zero failures.
- The fix commits do not touch the frontend. The baseline was measured rather than assumed: worktree 55 files / **697 tests** green (291.61s) vs main checkout 54 files / **692 tests** green (244.48s) — the delta is exactly the new addendum test file. (An earlier quoted baseline of 687/53 was stale and is hereby corrected.)
- The tests are not vacuous — four negative verifications were run: reverting `_addendum_state` to a pass-through tuple fails `test_a1` with `KeyError: 'attempted_chars'`; monkeypatching `_bridge_lock` to `nullcontext()` fails `test_b7`; replacing `render_addendum_block` with `lambda s: ''` fails both live-chain tests; stripping `addendum` from `to_persistable_dict` fails both live-chain tests.
- Condition (2) has its own provenance trio in `web_ui/backend/tests/test_addendum_write_through.py`: `test_a1_refusal_reports_the_boundarys_numbers_verbatim` replaces `write_addendum_entry` with a canned refusal (`attempted_chars: 1000001`, `budget_chars: 12345`, `attempted_entries: 3`, `max_entries: 424242`, `total_chars: 999999`) while posting text well under the real budget, then asserts the 409 echoes those numbers and that sentence verbatim — a route that recounted would emit its own length and its own wording; `test_a2_route_never_measures_the_submitted_text` injects a `str` subclass whose `__len__` raises, so any route-side measurement fails loudly (with a meta-assert that the poison was actually measured, so it cannot pass vacuously); `test_a3_route_module_never_sums_or_measures_text` greps `addendum_routes.__file__` for `sum(` and `len(`. Honest limitation: `test_addendum_routes.py` covers the refusal contract (status, caps, non-persistence) but would *not* detect a reintroduced recomputation — the provenance guarantee rests on the a1/a2/a3 trio.
- Live-chain test, real/fake boundary: REAL `FileSystemSessionStore`, `Session`, `WebAgentBridge` (with the real `_session_state_lock` and real SessionManager) registered in the real `_active_tab_bridges`, real route coroutines, real `write_addendum_entry`, real `Agent.process_query` (composition at `agent.py:1217` `ensure_system_prompt` → `llm_client.py:201` → addendum appended at `llm_client.py:225-227` via `render_addendum_block` at `llm_client.py:128` → context at `agent.py:1334/1344` → outgoing request at `agent.py:1398`), real `bridge.save_session`. The ONLY fake is `agent.llm_client.stream_completion` (`llm_client.py:277`), which captures the outgoing `messages` list — the exact system string the model would have received. The agent is constructed BEFORE the POST, so only per-turn composition can deliver the entry; a stale read cannot pass the test.
- Recovery path covered: write over budget → HTTP 409 carrying the boundary's own sentence plus `attempted_chars`/`budget_chars` → clear the addendum → the next write succeeds, in both the live and the cold-store cases.
- Acceptance criteria: met. The full-chain test against a live session asserts the next turn's system string contains the text; the over-budget → clear → next-write-succeeds recovery test exists and passes.

## Related

- W1.5 backend `021c4001` (`web_ui/backend/addendum_routes.py` — the write path that is inert here).
- Slice 5 `f092ab39` (budget enforcement at the same write boundary).
- `ticket-feat-addendum-clear-action.md` — the sibling W1.5 surface; the clear has the same
  live-session question.
- `ticket-hygiene-addendum-budget-math-duplicated.md` — the other known seam in this inlet.
- `docs/design-sysprompt-addendum-and-injection.md` — the composition contract this violates.
