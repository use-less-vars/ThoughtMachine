# Roadmap

Project milestones, planned features, and long-term goals.

## Current Status

**All previously planned phases 1–5 are ✅ COMPLETE.** See `task_tracker.md` for full completion details.

### Remaining Future Work (No Immediate Priority)
- **Phase 6 (Streaming LLM Support):** 📋 Planned
- **Phase 7 (Standalone Agent Extraction):** 📋 Planned
- **Security Layer GUI handler:** SECURITY_PROMPT events published but no dialog listens
- **WebSocket reconnection robustness:** Needs comprehensive testing
- **Docker container pooling image-ID verification:** Pattern documented, needs tests
- **DeepSeek Reasoner tool calling:** XML tool call parsing for non-function-calling models
- **Automatic RAG re-indexing:** No staleness detection; manual update-index only
- **Performance I/O audit:** Session store caching added, full audit pending

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

## Future Ideas
## Future Ideas (Foggy — No Timeline Yet)

| Item | Description |
|------|-------------|
| F1 | **KB query enhancement** — inject system hints (e.g., "split tasks") before user query. Needs injection-point spec from Phase 4. |
| F2 | **Multi‑agent timeouts** — stop agent after time limit using stop_event + stop_reason pattern. |
| F3 | **RequestUserInteraction mid‑turn resume** — replace current abort‑and‑restart with generator.send() protocol. |
| F4 | **Multi‑tool output total‑truncation** — cap sum of tool-result tokens per turn. |
| F5 | **Config‑change UI feedback** — warn user when a setting requires restart (utilize FIELD_CATEGORIES). |
| F6 | **Grace‑turn visibility on pause** — ensure the user always sees the last finished turn's content even after pressing pause. (Partially addressed in 3.1 but may need further GUI work.) |
| F7 | **Interchangeable system prompts in GUI** — allow swapping system prompts on the fly per session/tab, possibly from a library/dropdown. Could leverage Phase 4 injection points. |
| F8 | **Linux analysis tool (privileged read)** — a tool with elevated read access to inspect system state, logs, config files, hardware info, etc. Needs careful security consideration (capability-gated). |
| F9 | **Web research suite** — download videos (yt-dlp), transcribe them (Whisper/stt), scrape & summarize web pages, fetch RSS, etc. A bundle of web-centric tools working in concert. |
| F10 | **Evolution log / agent meta-cognition** — a persistent log of the agent's own evolution (config changes, tool additions, lessons learned) that the agent can query to talk about itself. Like an "agent autobiography" stored in the KB or a special session. |
| F11 | **Multi-agent "circuitry" system** — framework for composing multiple agents with: dedicated tooling per agent, security boundaries, independent sysprompts, and a "circuit" definition (data flow between agents). Think piping agents together like electronic components. |
| F12 | **Multipurpose viewer (lightweight reader + media)** — a unified viewing component in the GUI that can render: plain text, markdown, code with syntax highlighting, images, audio (waveform), video (embedded player), PDF, data tables. Could show tool outputs, analysis results, or user-provided content in context. |
| F13 | **Multi-agent async with thread joining** — pattern where multiple agent instances run concurrently (async) and a "join" operation synchronizes their results. Enables parallel research, divide-and-conquer workflows, ensemble reasoning. Think `Promise.all()` for agents. |
| F14 | **Config change awareness messages** — inject system notifications when the agent's configuration changes mid-session (workspace switched, context window expanded, tools changed). The agent should receive a message like "Your workspace has been changed to X" so it can adapt its behavior accordingly. |
| F15 | **Unify Final / FinalReport / RequestUserInteraction into one tool** — these three tools serve the same fundamental purpose (returning control to the user) with different flavors. Merge them into a single `Response` tool with parameters for finality, reporting, and user interaction needs. Simplifies the tool surface and reduces cognitive load. |
| F16 | **Remove the Thought tool** — `ThoughtTool` is a relic from an early architecture where internal reasoning needed to be explicitly exposed. It has proven useless in practice: the agent's chain-of-thought is already captured in its message stream, and the tool adds no value beyond clutter. Remove it to clean up tool definitions and reduce system prompt token waste. |


## Future Ideas (Foggy — No Timeline Yet)

| Item | Description |
|------|-------------|
| F1 | **KB query enhancement** — inject system hints (e.g., "split tasks") before user query. Needs injection-point spec from Phase 4. |
| F2 | **Multi‑agent timeouts** — stop agent after time limit using stop_event + stop_reason pattern. |
| F3 | **RequestUserInteraction mid‑turn resume** — replace current abort‑and‑restart with generator.send() protocol. |
| F4 | **Multi‑tool output total‑truncation** — cap sum of tool-result tokens per turn. |
| F5 | **Config‑change UI feedback** — warn user when a setting requires restart (utilize FIELD_CATEGORIES). |
| F6 | **Grace‑turn visibility on pause** — ensure the user always sees the last finished turn's content even after pressing pause. (Partially addressed in 3.1 but may need further GUI work.) |
| F7 | **Interchangeable system prompts in GUI** — allow swapping system prompts on the fly per session/tab, possibly from a library/dropdown. Could leverage Phase 4 injection points. |
| F8 | **Linux analysis tool (privileged read)** — a tool with elevated read access to inspect system state, logs, config files, hardware info, etc. Needs careful security consideration (capability-gated). |
| F9 | **Web research suite** — download videos (yt-dlp), transcribe them (Whisper/stt), scrape & summarize web pages, fetch RSS, etc. A bundle of web-centric tools working in concert. |
| F10 | **Evolution log / agent meta-cognition** — a persistent log of the agent's own evolution (config changes, tool additions, lessons learned) that the agent can query to talk about itself. Like an "agent autobiography" stored in the KB or a special session. |
| F11 | **Multi-agent "circuitry" system** — framework for composing multiple agents with: dedicated tooling per agent, security boundaries, independent sysprompts, and a "circuit" definition (data flow between agents). Think piping agents together like electronic components. |
| F12 | **Multipurpose viewer (lightweight reader + media)** — a unified viewing component in the GUI that can render: plain text, markdown, code with syntax highlighting, images, audio (waveform), video (embedded player), PDF, data tables. Could show tool outputs, analysis results, or user-provided content in context. |
| F13 | **Multi-agent async with thread joining** — pattern where multiple agent instances run concurrently (async) and a "join" operation synchronizes their results. Enables parallel research, divide-and-conquer workflows, ensemble reasoning. Think `Promise.all()` for agents. |
| F14 | **Config change awareness messages** — inject system notifications when the agent's configuration changes mid-session (workspace switched, context window expanded, tools changed). The agent should receive a message like "Your workspace has been changed to X" so it can adapt its behavior accordingly. |


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

## 2026-05-14 — ## Future Idea: GUI Logging Toggle (Dynamic `TM_LOG_TAGS`)

...

## Future Idea: GUI Logging Toggle (Dynamic `TM_LOG_TAGS`)

**Problem**: `TM_LOG_TAGS` and `TM_LOG_LEVEL` are read at import time — changing them requires restarting the agent. For an active server developer, this is friction.

**Idea**: Add a runtime logging panel / dropdown in the Web UI that:
- Dynamically adjusts which tags are shown on console
- Could write to a settings endpoint that the logging system polls
- Would require modifying `unified.py` to read from a mutable source (settings object or polling env vars) instead of import-time constants

**Why it wasn't done yet**: This is a cross-cutting change to the logging facade itself. The import-time env var approach is simple and works for restart-driven development. A proper GUI toggle needs careful design to avoid perf overhead from polling or callback registration.

## 2026-05-25 — ## 2026-05-25 — Non-Urgent Items (Parked by Engineering Team...

## 2026-05-25 — Non-Urgent Items (Parked by Engineering Team)

Items with one-line dispositions so they don't clutter active thinking:

| Item | Disposition |
|------|-------------|
| **F17 — Session management bugs** (naming, restore, third-query issue) | Real bugs. Session engineer to investigate with current code, not old assumptions. |
| **F18 — Provider config + defaults for new installs** | Essential before public release. GUI engineer's next task after security prompts. |
| **F19 — Installation (venv, Electron, one-click)** | Defer until provider config works. Then package. |
| **F20 — Drag-and-drop file import** | See detailed design below (appended). GUI engineer side task. Pure frontend + one backend endpoint. |
| **F21 — Scroll-to-latest button when user is scrolled up** | GUI engineer side task. |
| **F22 — Config changes → system messages to LLM** | Core engineer. Tricky — the LLM might overreact. Needs a design doc. |
| **F23 — Test other providers (not just DeepSeek)** | Do during provider config work (F18). |
| **F24 — Delta message updates (not full snapshot each turn)** | GUI engineer optimization. After security prompts. |
| **F25 — Docker: long-running tasks, Dockerfile view in GUI, "ask user" for env changes** | Part of the workspace-centric security model. |
| **F26 — KB: semantic search, central "meta KB" vs workspace KB distinction** | KB engineer. After notebook panel. |

*(Items already in roadmap — not duplicated: live logging toggle F15, streaming Phase 6, sysprompt library F7, async multi-agent F13)*

## 2026-05-25 — F20 Design Detail: Drag-and-Drop File Import

**Implementation sketch:**

1. **Frontend:**
   - Add `onDrop` / `onDragOver` handlers to the chat area (or a dedicated drop zone).
   - Extract `File` objects from the drop event, create `FormData`, `POST` to a new backend endpoint.
   
2. **Backend:**
   - `POST /api/workspace/upload` — receives the file, sanitises the filename (no path traversal), writes to the current session's workspace folder.
   - Emit `workspace_changed` so the file browser refreshes.

3. **Security:**
   - Same‑origin check, token validation, file size cap (e.g. 50 MB).

4. **UX:**
   - Dashed border / highlight appears when a file is dragged over the agent area.

## 2026-05-28 — ## Feature Ideas (Rough — added 2026-05-28)

### 1. GUI-swit...

## Feature Ideas (Rough — added 2026-05-28)

### 1. GUI-switchable logging with tag selection
Add a panel in the web UI that lets users:
- Enable/disable logging in real time
- Select which specific logging tags/levels are active (e.g., debug, info, tool_calls, sessions, errors)
- Ideally backed by a dynamic log-filtering mechanism on the Python side so the agent can also toggle it

### 2. Workspace size viewer / bloat monitor
A small panel or status-bar widget that shows:
- Total workspace directory size
- File count and size breakdown by category (e.g., `.py`, logs, temp files, `.git`)
- A warning indicator when something grows unexpectedly (bloat creeping in)
- Maybe a "largest files" listing

### 3. ShowFileToUser tool → dedicated GUI panel
A new agent tool (e.g. `ShowFileToUser`) that:
- Takes a file path
- Opens/displays the file content in a special GUI panel in the web UI
- Allows the user to view, scroll, and maybe copy content
- The panel could be a dedicated "viewer" tab separate from the chat
- Useful for the agent to show results without blowing up the context with file content

## 2026-06-03 — ## Session Gossip Protocol (Idea)

**Origin**: Config audit ...

## Session Gossip Protocol (Idea)

**Origin**: Config audit discussion, 2025

**Concept**: Enable sessions (agent working threads) to "gossip" with each other — sharing relevant information autonomously without requiring the user to manually bridge them.

**Proposed workflow**:
1. User puts the system into a "safe position" (checkpoint)
2. Agents gossip/shared relevant context across sessions
3. Each agent can only continue work after the user reviews what it plans to do next

**Known challenges**:
- Security and access control (which sessions can talk to which?)
- Information leakage prevention
- User consent/permission at each step
- Serialization of partial work state
- Consensus/voting mechanisms if sessions disagree

**Status**: Idea only — no implementation planned yet. Requires careful security design before any prototyping.
