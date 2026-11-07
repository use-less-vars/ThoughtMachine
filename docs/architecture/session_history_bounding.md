# Session History Bounding (Phase 1)

Status: implemented on branch `feat/session-history-bounding` (backend only).
Spec: working doc `4523ecf6c5de` — "Session History Bounding + Session Size Visibility".

## Purpose

Main-agent session files can grow without limit (large tool outputs, long
multi-cycle conversations), bloating the vault and the context sent to
downstream consumers. This phase introduces a byte cap
(`SESSION_SIZE_CAP_BYTES = 2_000_000`) enforced in
`FileSystemSessionStore.save_session` right before the atomic write, plus a
`session_size_bytes` field exposed through the session API.

Bounding is lossy by design (compaction/truncation), so it applies **only to
main-agent sessions** — worker sessions and any session file without the
`agent_type == 'main'` flag are never touched. The existing
`prune_user_history` (history_pruner) call keeps its current behavior and
runs before bounding; this phase does not alter pruner semantics.

## Cycle definition

A **cycle** is one real user turn plus everything that follows until the next
real user turn:

- A *real user query* is a message with `role == 'user'` that is **not** a
  system notification. The explicit `is_system_notification is True` flag is
  authoritative (never derived from content).
- All following `assistant` / `tool` / `system` messages belong to that cycle
  until the next real user query.
- User-role system notifications are treated as *trailing content of the
  preceding cycle*, not as new cycles.
- Messages before the first real user query ("leading" messages) are left
  untouched by bounding.

## Terminal-answer rule

Each cycle has at most one *terminal answer*. It is identified by scanning
the cycle **from the end**; the last match wins, in this order:

1. A `tool` message with `response_type != null` (set after a `Respond` call).
2. A `tool` message whose `tool_call_id` maps to a **Respond-family** call
   (`Respond`, or legacy `Final` / `FinalReport` / `RequestUserInteraction`)
   in a preceding assistant message's `tool_calls` — covers legacy sessions
   whose tool results never got `response_type`.
3. A standalone `assistant` message without `tool_calls` (and not a system
   notification).

Fallbacks:

- If no match (e.g. an aborted cycle): the **last assistant message without
  `tool_calls`** is kept as the terminal answer.
- If none exists either: the cycle is kept as **query only**.

The Respond-family name set mirrors `web_ui/backend/session_manager.py`
(`FINAL_TOOL_NAMES = {'Respond'}`, `LEGACY_TO_RESPOND =
{'Final', 'FinalReport', 'RequestUserInteraction'}` with their assigned
`response_type`).

## Cap + enforcement order

`SESSION_SIZE_CAP_BYTES = 2_000_000` (session/size_bounding.py). Bounding
runs only when the gate passes (main-agent flag present **and** serialized
payload > cap). Enforcement is strictly sequential; each step runs only while
still over the cap:

1. **Keep the 2 most recent full cycles untouched.**
2. **Compact every older cycle** to exactly `[query, terminal_answer]` (or
   `[query]` when there is no terminal answer). The original user query
   content is never truncated or altered. `reasoning_content` and
   `tool_calls` are stripped from the kept messages; all intermediate
   messages (non-terminal tool outputs, non-terminal
   Final/RequestUserInteraction results, notifications, summaries) are
   dropped.
3. **Drop oldest compacted cycles first**, one at a time, until under the
   cap.
4. **Truncate ONLY non-terminal tool-result messages** (`role == 'tool'`,
   no `response_type`, `tool_call_id` not in the Respond-family set) inside
   the 2 kept full cycles, to a per-message content budget of
   `TOOL_CONTENT_BUDGET_BYTES = 4096` bytes with the `...[truncated]`
   suffix (byte-aware: never splits a multi-byte UTF-8 char). User queries
   and terminal answers are never truncated.
5. **If only the 2 full cycles remain and still over the cap:** allow the
   overrun and record `metadata['history_over_capacity'] = True` (never
   cleared).

Entry point: `apply_session_size_bounding(data)` (pure, mutates `data` in
place, returns True when bounding ran). The hook in
`session/store.py::save_session` gates on
`metadata['agent_type'] == 'main'` and runs **before** the atomic write;
`set_session_size_bytes(data)` then writes the final size via fixpoint.

## Main-agent-only flag

- Added at session creation on both creation paths:
  - `web_ui/backend/session_manager.py::create_session` sets
    `new_session.metadata["agent_type"] = "main"` (next to the existing
    `"source": "web_ui"`).
  - `agent/presenter/session_lifecycle.py::start_session` and `::new_session`
    set `metadata['agent_type'] = 'main'` on their fresh `Session(...)`
    constructions. `_build_session_from_current_state` copies all existing
    metadata keys (except the stale `agent_config`) onto every save, so the
    flag survives all save paths without extra handling.
- `is_main_agent_session(data)` requires `metadata` to be a dict with
  `agent_type == 'main'`.
- **Contract:** bounding AND `session_size_bytes` are written **only** for
  main-agent sessions. Non-main sessions / legacy files without the flag are
  skipped entirely (no mutation, no `session_size_bytes` written, even
  `history_over_capacity` is never set). Worker sessions
  (`worker-<uuid>`, persisted as `context.json`, never in session files)
  are untouched by construction.

## session_size_bytes contract

- `session_size_bytes` is a top-level `int` in the session JSON, written on
  every save of a main-agent session (even when under the cap).
- Value = byte length of the serialized payload, measured with the **same
  serialization the store's atomic write uses** (`json.dumps(data, indent=2,
  default=str)`), so re-serializing the saved file yields exactly this
  number. The store writes `json.dump(data, f, indent=2, default=str)`.
- Written via a small fixpoint loop (the field itself contributes to the
  payload size; convergence is immediate once the digit count stabilizes).
- Exposed in:
  - `GET /api/session/{id}` → `SessionDetailResponse.session_size_bytes`
    (from `store.get_session_size_bytes`, which reads the raw JSON field).
  - `GET /api/session/list` → `SessionListItem.session_size_bytes` (via
    `load_sessions_metadata_batch`, which now also carries the field).
  - Session metadata batch rows (`load_sessions_metadata_batch` /
    `list_sessions`) include `session_size_bytes` for main-agent sessions;
    `None` when absent (e.g. sessions saved before this feature).
- WS `get_conversation` / `list_sessions` are not extended in this phase
  (they do not expose per-session metadata; kept backend-only).

## Test strategy

Hermetic vault only (tests/conftest.py guards real-vault writes). New file
`tests/test_session_size_bounding.py` (model style on
`tests/test_history_pruner.py` helpers) covers:

- 2 most recent cycles stay full after bounding;
- middle cycles compacted to exactly `[query, terminal]`;
- query content byte-identical (never truncated/altered);
- terminal answer never truncated;
- `reasoning_content` stripped from compacted cycles;
- `tool_calls` removed from compacted cycles;
- intermediate tool outputs removed;
- Final/RequestUserInteraction removed unless they ARE the terminal answer
  (legacy mapping);
- oldest cycles dropped first when still over cap;
- torture case: huge tool outputs vs small queries/answers → queries/answers
  intact, tool outputs truncated at 4096B budget with suffix;
- only 2 full cycles + still over cap → overrun allowed +
  `metadata['history_over_capacity'] == True`;
- main-agent gate: non-main / missing flag → no mutation, no
  `session_size_bytes`;
- store integration: `save_session` → file size equals `session_size_bytes`
  (re-serialize with same serialization and compare);
- round-trip: bounded result re-loadable via `FileSystemSessionStore.load_session`.

Route exposure: `web_ui/backend/tests/test_session_size_routes.py` (4 tests)
asserts `session_size_bytes` present in `GET /api/session/{id}` detail and
`GET /api/session/list`, and `None` for non-main sessions. These tests live
outside `testpaths = ["tests"]` and are run separately
(`python -m pytest web_ui/backend/tests/test_session_size_routes.py`): 4 passed.

Full suite (`pytest -q` from repo root, `testpaths = ["tests"]`):
1703 passed / 43 skipped (baseline 1686 / 43 + 17 new bounding tests).
Targeted run of bounding + routes + store tests: 52 passed.

## Known issues

- **C1 — history_pruner stale FINAL_TOOL_NAMES:** `session/history_pruner.py`
  defines `FINAL_TOOL_NAMES = {'Final', 'FinalReport',
  'RequestUserInteraction'}` — missing `'Respond'` (pre-existing; its
  keep-all-final-turns branch is dead code and final answers get dropped in
  pruned regions). Not fixed in this phase (tests assert
  `len(FINAL_TOOL_NAMES) == 3`). The new bounding code uses the correct
  response_type-based, Respond-aware mapping.
- **C2 — history_pruner `_is_system_notification`:** pruner detects system
  notifications via content substring; the explicit
  `is_system_notification is True` flag (agent/core/message.py semantics) is
  authoritative. Not fixed in this phase.
- Environment-only (not code defects): running `pytest` on
  `tests/` + `web_ui/backend/tests/` together hits a tracked duplicate tree
  (`tests/web_ui/backend/`, committed in 4a1ea23) → "import file mismatch"
  collection error; `pytest web_ui/backend/tests` dir-wide fails on
  `test_bridge_dedup.py` (`No module named 'agent.config.session_config'` —
  module absent, pre-existing). Run `pytest -q` (testpaths `tests/`) for the
  full suite and web_ui backend test files individually.
