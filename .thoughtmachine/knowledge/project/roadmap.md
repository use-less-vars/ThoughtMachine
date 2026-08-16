# Roadmap
Milestones and future plans. **Restructured 2026-08-16**: completed phases (Phase 2, Phase 2.5, Tasks 1-4) moved to `archive_arch_b.md`; future-ideas retained.
> **LOST (2026-08-16, S2 incident):** the `V3 ORDER` section (operator priority list) was uncommitted in the working tree at restore time and is not recoverable from git/workspace. The authoritative 1–14 status list survives in `personal/task_tracker.md` → OPERATOR HANDOFF. Please re-supply the section from host-side if available.
## Current Status (2026-08-16)
- V3 ORDER section: LOST (see note above); 1–14 status list preserved in task_tracker.md OPERATOR HANDOFF.

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
> **Superseded (2026-08-16).** Kept for context.

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


## 2026-05-14 — Future Idea: GUI Logging Toggle (Dynamic `TM_LOG_TAGS`)

...

## Future Idea: GUI Logging Toggle (Dynamic `TM_LOG_TAGS`)

**Problem**: `TM_LOG_TAGS` and `TM_LOG_LEVEL` are read at import time — changing them requires restarting the agent. For an active server developer, this is friction.

**Idea**: Add a runtime logging panel / dropdown in the Web UI that:
- Dynamically adjusts which tags are shown on console
- Could write to a settings endpoint that the logging system polls
- Would require modifying `unified.py` to read from a mutable source (settings object or polling env vars) instead of import-time constants

**Why it wasn't done yet**: This is a cross-cutting change to the logging facade itself. The import-time env var approach is simple and works for restart-driven development. A proper GUI toggle needs careful design to avoid perf overhead from polling or callback registration.

## 2026-05-25 — 2026-05-25 — Non-Urgent Items (Parked by Engineering Team...

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
| **F27 — GitInfoTool: add fetch/push operations** | The tool's write ops (commit/init/clone) are good for now. fetch (safe, read-only network) and push (natural pair with commit) were discussed. Pull/merge/rebase rejected due to conflict risk. **Parked — revisit when write-op demand increases.** See KB conversation history. |
| **F28 — WorkerOutputPanel styling ≠ ChatPanel styling** | Two completely separate component trees with separate CSS class systems (`.message-*` vs `.worker-event-*`), different HTML structure, different data models, no shared rendering primitives. Worker panel also lacks Markdown rendering. To unify: extract shared MessageBubble component or map event data to ChatPanel message format. **Parked — cosmetic, not functional.** |

*(Items already in roadmap — not duplicated: live logging toggle F15, streaming Phase 6, sysprompt library F7, async multi-agent F13)*
| **F26 — KB: semantic search, central "meta KB" vs workspace KB distinction** | KB engineer. After notebook panel. |
| **F27 — GitInfoTool: add fetch/push operations** | The tool's write ops (commit/init/clone) are good for now. fetch (safe, read-only network) and push (natural pair with commit) were discussed. Pull/merge/rebase rejected due to conflict risk. **Parked — revisit when write-op demand increases.** See KB conversation history. |
| **F28 — WorkerOutputPanel styling ≠ ChatPanel styling** | Two completely separate component trees with separate CSS class systems (`.message-*` vs `.worker-event-*`), different HTML structure, different data models, no shared rendering primitives. Worker panel also lacks Markdown rendering. To unify: extract shared MessageBubble component or map event data to ChatPanel message format. **Parked — cosmetic, not functional.** |

*(Items already in roadmap — not duplicated: live logging toggle F15, streaming Phase 6, sysprompt library F7, async multi-agent F13)*
| **F26 — KB: semantic search, central "meta KB" vs workspace KB distinction** | KB engineer. After notebook panel. |
| **F27 — GitInfoTool: add fetch/push operations** | The tool's write ops (commit/init/clone) are good for now. fetch (safe, read-only network) and push (natural pair with commit) were discussed. Pull/merge/rebase rejected due to conflict risk. **Parked — revisit when write-op demand increases.** See KB conversation history. |
| **F28 — WorkerOutputPanel styling ≠ ChatPanel styling** | Two completely separate component trees with separate CSS class systems (`.message-*` vs `.worker-event-*`), different HTML structure, different data models, no shared rendering primitives. Worker panel also lacks Markdown rendering. To unify: extract shared MessageBubble component or map event data to ChatPanel message format. **Parked — cosmetic, not functional.** |

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

## 2026-05-28 — Feature Ideas (Rough — added 2026-05-28)

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

## 2026-06-03 — Session Gossip Protocol (Idea)

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

## 2026-06-04 — Future: Logging flags available in GUI with hot-switch to...

## 2026-06-04 — **Hot-swappable system prompts** (feature idea):
- ConfigPan...

**Hot-swappable system prompts** (feature idea):
- ConfigPanel.jsx already has a sysprompt field but it's currently a dummy/placeholder
- Need: ability to browse files from `~/.thoughtmachine/`, load a chosen prompt file
- Need: core function to swap the active system prompt at runtime
- Don't implement yet, just record the idea
- See also: DirectoryBrowser component in ConfigPanel.jsx (the file browsing pattern already exists)

## 2026-06-04 — 2026-06-05 — Ask-prompts need more expressiveness

The pe...

## 2026-06-05 — Ask-prompts need more expressiveness

The permission/security ask-prompts (shown when a tool requires user approval) are currently too terse. Users need clearer explanations of what the tool will do, what data it accesses, and what the implications are.

**Planned action:** Revamp the security prompt UI (SecurityDialog.jsx) and the underlying prompt construction to be more descriptive and user-friendly.

**Status:** TODO — planned for near-term work.


## 2026-06-05 — 2026-06-05 — **ThoughtHub — The ThoughtMachine Network** ...

## 2026-06-05 — **ThoughtHub — The ThoughtMachine Network** (Major New Direction — design phase)

### Vision
A web server ("ThoughtHub") that allows multiple ThoughtMachine instances to communicate securely. Users host their own hubs (personal, team, or public). Each TM instance gets an **identifier** and can exchange messages with other TMs via a hub.

### Core Architecture
- **ThoughtHub server**: A lightweight, secure message relay with authentication, rate-limiting, trust scoring, and message queuing (like a TM-focused mail server).
- **TM Tool: `send_tm_message`**: A new agent tool that lets TM send/receive messages to/from hubs.
- **TM Tool: `receive_tm_messages`**: Poll a hub for incoming messages, TM processes them (spam/malicious filter, triage, respond).
- **Message envelopes**: Every message is signed (ed25519?) with the sender's TM identity.

### Trust & Reputation System (Key Innovation)
- Each TM starts with **low trust**: limited messages per day, messages are queued for review.
- **Good behaviour is rewarded**: Legit bug reports, useful contributions → trust score rises → higher message limits, direct commit access, etc.
- The "hub owner's TM" reviews incoming mail triaged by priority and trust level — spam/low-trust goes to sandbox, known reporters get through.
- Identities gain reputation that can be shared across hubs (like a web of trust).

### Security Principles
- All communication over TLS.
- Message signing + optional encryption (content hidden from hub, only visible to recipient TM).
- Hubs run in restricted environments — configurable policies per sender identity.
- Sandboxed message processing: TM never executes code from messages without explicit user review.
- Rate limiting per identity, per hub.
- DDoS protection at hub level.
- The hub itself has minimal attack surface — it's a relay, not an execution engine.

### Use Cases (in order of ambition)

1. **Bug Report Mailbox**: Friends run TM → send bug report to hub → your TM reviews, triages, possibly auto-fixes, replies with a download link or patch. Trust grows over time.

2. **Team Project Hub**: Team members host a shared hub. Policy: "all machines here are friends." Shared git access, cross-TM task assignment, collaborative debugging.

3. **Open Source Contribution Gateway**: Strangers can file issues/suggestions via TM→hub→your TM. Low trust initially, but good contributions unlock faster response, direct PR ability, etc.

4. **The ThoughtMachine Network**: Multiple hubs federated. TMs can discover each other, share tools, share prompts, share lessons learned across a decentralized network.

### Implementation Phases (Suggested)

| Phase | What | Scope |
|---|---|---|
| **P0** | Prototype hub: basic relay, one identity, no trust system | ~1 week |
| **P1** | Identity system + message signing | +1 week |
| **P2** | Trust scoring + rate limiting | +1 week |
| **P3** | `send_tm_message` + `receive_tm_messages` tools | +1 week |
| **P4** | Hub federation (TMs discover each other across hubs) | +2 weeks |
| **P5** | Web UI for hub management | +1 week |

### Open Questions (for later design)
- Should the hub store messages at all, or just route? (Store-and-forward seems right for async.)
- How do we handle identity revocation? (Lost key = lost identity? Key rotation?)
- Should the hub be a new standalone binary or a plugin to the existing TM server?
- How to handle "merge requests" — can a remote TM propose code changes via hub?
- What's the trust bootstrap for a new identity? (Proof-of-work? Reputation delegation from a known identity?)

### Status
🟡 DESIGN — Rough vision written down. Not started.



## 2026-06-05 — 2026-06-06 — Workspace Panel (Feature Idea)

A slide-in p...

## 2026-06-06 — Workspace Panel (Feature Idea)

A slide-in panel (similar to VS Code's sidebar) that gives the user full visibility into the workspace they're operating in:

- **Workers** — list of worker processes running in the workspace, their status, lifecycle controls
- **Container privileges** — a dashboard showing what permissions/access the current container has (filesystem, network, docker socket, etc.)
- **Knowledge Base browser** — a browsable view of the knowledge base, laid out like a "book" with sections, chapters, and search — letting the user flip through architecture docs, bug logs, lessons learned, etc. in a readable, non-linear way
- **Session context** — current session metadata, token usage, attached files, active config
- **Environment info** — OS, runtime versions, environment variables

## 2026-07-01 — 2026-07-02 — Master Vault: Future Plans

### 1. Workspace...

## 2026-07-02 — Master Vault: Future Plans

### 1. Workspace Config Panel 🟡 HIGH
- GUI panel for managing workspaces (create, delete, switch, configure)
- Backend workspace CRUD API exists, frontend panel needed

### 2. Docker Containers 🟡 HIGH
- Container persistence / resurrection across restarts
- Container health monitoring with auto-restart
- Container snapshots and rollback
- Per-worker container isolation

### 3. Permissions / Security 🟡 HIGH
- Security defaults: deny-all with explicit allow
- Granular tool-level permissions for workers
- Permission presets (e.g., "safe coder", "file reader", "admin")
- Audit logging for permission grants/denials

### 4. MCP Integration 🟢 MEDIUM
- Model Context Protocol server integration
- Allow agents/workers to use MCP tools from configured servers
- MCP server management UI

### 5. Agent Capabilities 🟢 MEDIUM
- Multi-agent collaboration (agents spawning sub-agents)
- Agent memory/persistence improvement
- Agent tool-use planning and optimization
- Agent personas (system prompt profiles)

### 6. Advanced Logging 🟢 MEDIUM
- Dynamic log tag toggling from GUI
- Centralized log viewer in frontend
- Log search and filtering
- Log export

### 7. Config Distribution 🟢 MEDIUM
- Sync/merge configs across machines
- Config versioning and rollback
- Shared team configurations

### 8. Multi-Session Visibility 🟢 MEDIUM
- View all sessions across all workspaces
- Cross-session search and compare
- Session templates

### 9. Installer / First-Run Experience 🟢 LOW
- One-click installer for Windows/Mac/Linux
- First-run wizard: provider key setup, workspace init, guided tour
- Auto-update mechanism

## Archived
Completed phases → `archive_arch_b.md` (`## SOURCE: roadmap.md — completed phases`).
