# Roadmap

Project milestones, planned features, and long-term goals.

## Current Status
## Current Status
- **Phase 1 (Core State Machine):** ✅ Complete (1.1–1.14)
- **Phase 2 (Event Pipeline + Token Restrictions):** ✅ Complete (1.15–1.18, plus event fixes)
- **Phase 2.5 (Multi‑Session Tab Support & Full Session Restore):** 🟢 **Now** — being planned
- **Phase 3 (GUI Adaptation & Grace-Turn Preservation):** 📋 Queued (after 2.5)
- **Phase 4 (System Message Injection Audit):** 📋 Planned
- **Phase 5 (Token Counting & Output Truncation Audit):** 📋 Planned
- **Phase 6 (Streaming LLM Support):** 📋 Planned
- **Phase 7 (Standalone Agent Extraction):** 📋 Planned

## Upcoming Milestones (Ordered by Priority)

### Phase 3 — GUI Adaptation & Grace-Turn Preservation 🟢 NOW
| Item | Description | Status |
|------|-------------|--------|
| 3.1 | **Preserve grace turn on pause:** When user pauses and current turn finishes, commit turn content to user_history BEFORE yielding pause event. | **Now** |
| 3.2 | **Refine pause-display timing:** Ensure GUI shows "Pausing…" before transition to READY (signal ordering, small delay). | **Now** |
| 3.3 | Verify all stop_reason values (final, error, max_turns_reached, paused, rate_limit, user_interaction) produce correct UI banner and input-box state. | After 3.1–3.2 |
| 3.4 | Remove any remaining GUI branches that reference deleted ExecutionState values (harmless dead code). | After 3.3 |

### Phase 4 — System Message Injection Points Audit 📋
| Item | Description | Status |
|------|-------------|--------|
| 4.1 | Map every location where system/notification messages are injected into user_history (token warnings, turn warnings, errors, summary notification). | Planned |
| 4.2 | Map every location where messages are injected into the live LLM request (like the old pre‑LLM block did). | Planned |
| 4.3 | Define the correct ordering: warnings should appear BEFORE the assistant's response, not after. | Planned |
| 4.4 | Document injection-point roles so future features (KB hints, timeouts) can plug in cleanly. | Planned |

### Phase 5 — Token Counting & Tool Output Truncation Audit 📋
| Item | Description | Status |
|------|-------------|--------|
| 5.1 | Audit per‑tool output truncation (10k chars) — is it still working correctly? | Planned |
| 5.2 | Assess whether we need a total‑across‑tools token cap per turn (not just per‑tool). | Planned |
| 5.3 | Document hybrid token counting: ground truth from LLM usage + tiktoken estimates between calls. | Planned |
| 5.4 | Verify multi‑tool atomicity when LLM calls 3+ tools in one turn via TurnTransaction. | Planned |
| 5.5 | Audit dormant rate‑limit/throttling mechanism — decide to keep, remove, or repurpose for multi‑agent timeouts. | Planned |

### Phase 6 — Streaming LLM Support 📋
| Item | Description | Status |
|------|-------------|--------|
| 6.1 | Replace blocking LLM call with streaming: yield partial thought chunks. | Planned |
| 6.2 | Adapt process_query() generator to stream; keep tool-call boundary intact. | Planned |
| 6.3 | Update controller and GUI to handle incremental output events. | Planned |

### Phase 7 — Standalone Agent Extraction 📋
| Item | Description | Status |
|------|-------------|--------|
| 7.1 | Convert process_query() into a pure function: run(messages, config, tools, stop_event) → Iterator[Event]. | Planned |
| 7.2 | Remove daemon‑thread dependency; support both blocking and async invocation. | Planned |
| 7.3 | Provide backward‑compatible wrapper so the current GUI still works. | Planned |

## Future Ideas (Foggy — No Timeline Yet)

| Item | Description |
|------|-------------|
| F1 | **KB query enhancement** — inject system hints (e.g., "split tasks") before user query. Needs injection-point spec from Phase 4. |
| F2 | **Multi‑agent timeouts** — stop agent after time limit using stop_event + stop_reason pattern. |
| F3 | **RequestUserInteraction mid‑turn resume** — replace current abort‑and‑restart with generator.send() protocol. |
| F4 | **Multi‑tool output total‑truncation** — cap sum of tool-result tokens per turn. |
| F5 | **Config‑change UI feedback** — warn user when a setting requires restart (utilize FIELD_CATEGORIES). |
| F6 | **Grace‑turn visibility on pause** — ensure the user always sees the last finished turn's content even after pressing pause. (Partially addressed in 3.1 but may need further GUI work.) |

## 2026-05-11 — ## Phase 2.5 — Multi‑Session Tab Support & Full Session Rest...

## Phase 2.5 — Multi‑Session Tab Support & Full Session Restore 🟢 NOW

| Item | Description | Status |
|------|-------------|--------|
| 2.5.1a | **Backend: load_session also loads agent_config** — extract `session.metadata['agent_config']` and store as overrides so next `start_session` uses saved config (system prompt, tools, etc.) | Planned |
| 2.5.1b | **Backend: new `get_open_sessions` command** — returns session IDs from `open_sessions.json` | Planned |
| 2.5.1c | **Backend: new `close_session` command** — save session, remove from open list, stop bridge | Planned |
| 2.5.1d | **Backend: WebSocket disconnect handler** — treat unexpected close as tab close (save + remove from open list) | Planned |
| 2.5.2a | **Frontend: Tab bar component** — replace single-session view with tab manager; each tab has `tabId`, `sessionId`, `ws`, local chat state | Planned |
| 2.5.2b | **Frontend: Each tab is independent** — refactor `App.jsx` into `SessionTab` component with own WebSocket lifecycle; per-tab Zustand context or local state | Planned |
| 2.5.2c | **Frontend: Initialisation flow** — on load, send `get_open_sessions`, open tabs for each returned session_id via `load_session` | Planned |
| 2.5.2d | **Frontend: "+" button** — creates blank tab; on first query sends `start_session` with default config | Planned |
| 2.5.2e | **Frontend: Tab close** — send `close_session`, close WebSocket, remove tab | Planned |
| 2.5.2f | **Frontend: Config per session** (deferred to Phase 3) | Deferred |

### Architectural Mapping
| Old PyQt6 GUI | New Web GUI |
|---------------|-------------|
| Each `SessionTab` has its own Presenter + Controller | Each browser tab gets its own WebSocket → backend spawns `WebAgentBridge` + `AgentController` |
| `QTabWidget` with "+" button | React tab bar component, "+" creates new WebSocket connection |
| On start, restore open sessions from `open_sessions.json` | Frontend sends `get_open_sessions`, opens tabs for each session_id with `load_session` |
| Close tab → save session | Frontend sends `close_session`, backend saves + updates open list, client closes WebSocket |
| Load session from file → `presenter.load_session(filepath)` | WebSocket command `load_session { session_id }` → bridge loads session with full agent_config from metadata |

### Design Notes
- No agent logic changes — this is a thin-shell reproduction of the old PyQt6 tab system
- `load_session` must extract `session.metadata['agent_config']` so that system prompt, tools, and settings are restored exactly as saved
- `close_session` passes through to existing `SessionLifecycle`/`FileSystemSessionStore` methods — no new logic
- Per-tab WebSocket isolation ensures each tab is an independent "mini-app"
