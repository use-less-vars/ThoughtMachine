# Ideas And Brainstorming

Product ideas, feature concepts, brainstorming notes, and things not to forget

## Overview
(To be populated)

## View Artifact Tool
## View Artifact Tool
**Status:** Unconceptualized idea, needs broader design thinking.

**Core idea:** A tool that allows the agent to "put something into the user's view" — for example opening a markdown file, PDF, or HTML page that the user can see. Think of it as a "present to user" mechanism.

**Use cases:**
- Agent generates a report and wants to display it nicely (rendered Markdown, styled HTML)
- Agent wants to show a PDF preview of something
- Agent wants to present interactive content (charts, diagrams, etc.)

**Open questions:**
- How does this relate to the existing `Respond` tool's `report_body`/`report_title` feature (which writes downloadable files)?
- Is this a new tool, or an enhancement to `Respond`?
- Should it render content inline in the chat UI, open a new browser tab, or both?
- How does it handle different formats (MD, PDF, HTML)?
- Security considerations: HTML can contain JS, PDF can have embedded scripts — needs sandboxing.

**Filed:** 2026-06-01
**Updated:** 2026-06-03 — Independently came up again in conversation. Still an unconceptualized idea but clearly has persistent value. Naming candidate: "ViewArtifact" or "present_to_user". Might pair well with onboarding flow (agent generates a welcome page and presents it).

## Recent Entries

- **2026-06-01:** Added "View Artifact Tool" idea.

## 2026-06-01 — ## First-Time User Experience / Onboarding Flow
**Status:** ...

## First-Time User Experience / Onboarding Flow
**Status:** Raw observation, needs fleshing out.

**Trigger:** Watched a first-time user interact with ThoughtMachine. Key observations:

1. **No "hand-holding" onboarding** — TM doesn't communicate well about *how* to interact with it as a user. Users can change workspaces, TM can create KBs, TM gets notified of workspace changes — but this isn't surfaced proactively.

2. **Proposed "Let me take you by the hand" flow** — Instead of a blank session, TM could open with something like: *"Welcome! Why don't you create a folder first, then we move there and start a project notebook to gather all your ideas, then we start building it."* A soft, guided intro that shows what TM can do rather than leaving it to the user to discover.

3. **Session hygiene misconception** — The new user learned they should start a new session to avoid old context bleeding in. But with TM, this isn't necessary: pruning/summarization handles context automatically, and the KB persists knowledge across sessions. This needs to be communicated upfront.

4. **Global KB for user preferences** — Question raised: how flexible is TM with writing things to the global KB (user needs, preferences, things to remember beyond workspace)? Currently there's a `user/my_notes.md` domain in global KB. Could this be used for onboarding itself — e.g., storing user preferences, workspace history, or a "getting started" checklist?

**Open questions:**
- Should there be an onboarding wizard / checklist that TM runs on first session?
- How does TM detect "first time user" to trigger onboarding?
- Could the global KB store user preferences (e.g., preferred working style, common workflows)?
- Should there be a `first_time_user.md` or `onboarding.md` domain in the global KB?

**Filed:** 2026-06-03


## 2026-06-02 — ## 2026-06-01 — System Prompt Personas: Different Agent Beha...

## 2026-06-01 — System Prompt Personas: Different Agent Behaviors For Different Contexts

**Idea:** Ship multiple system prompt "personas" that users (or the agent) can switch between depending on context. Not necessarily different personalities — more like different *operating modes*:

| Persona | When | Behavior |
|---------|------|----------|
| **Guide** | New users, onboarding | Maximum handholding, explains everything, proactive orientation |
| **Builder** | Active development | Focused on writing code, less chatty, assumes moderate knowledge |
| **Architect** | Design/planning phase | Thinks about structure, points out debt, suggests refactoring |
| **Critic** | Code review / debugging | Hyper-focused on issues, edge cases, failure modes |

**Why this would help:**
- A UX designer needs "Guide" mode — constant orientation, no assumptions
- A senior developer needs "Builder" or "Architect" — less explanation, more action
- Currently the agent has ONE behavioral profile for everyone

**What it would take:**
- Each persona = a separate system prompt file (or section)
- A switching mechanism (user command? automatic detection?)
- The agent checks current persona before acting
- Could be as simple as a KB entry storing the current persona, checked in the system prompt

**Relationship to Rule 16 & 17:** These new rules (Proactive Guidance, Map the Territory) are the *default* behavior leaning toward "Guide" mode. Different personas would dial these up/down.

**Status:** Future idea, needs architectural changes (Rule 10 — sysprompt changes need restart). Not urgent but worth keeping on the radar.

**See also:** The entire onboarding system (Rule 14, 15, 16, 17 + onboarding_guide.md + capabilities_reference.md) could serve as the "Guide" persona foundation.

## 2026-06-02 — ## 2026-06-02 — Distribution Pipeline: How Global KB Content...

## 2026-06-02 — Distribution Pipeline: How Global KB Content Reaches Users

**The gap:** We're editing global KB content (`user/onboarding_guide.md`, `user/capabilities_reference.md`) in the TM-dev workspace, but there's no pipeline to ship these to end users. The global KB at `~/.thoughtmachine/knowledge/` is created per-user at runtime.

**Options:**
1. Create a `resources/default_global_kb/` directory containing seed content, copied to `~/.thoughtmachine/knowledge/` on first agent start
2. Bundle with PyInstaller (in the `resources/` directory that's already included)
3. Keep a separate "shippable KB" repo that gets pulled during install

**Related:** The `system/` directory in global KB is read-only and could be the place for shipped content, while `user/` is writable. Currently `system/` has no seed content mechanism either.

**Status:** Needs a decision on approach. Not urgent for v2.0 features but blocks the onboarding improvements from reaching users.

## 2026-06-02 — ## 2026-06-02 — Distribution Pipeline: RESOLVED

The pipelin...

## 2026-06-02 — Distribution Pipeline: RESOLVED

The pipeline already existed at `resources/global_kb/` → `~/.thoughtmachine/knowledge/system/` via `agent/knowledge/global_kb.py:ensure_global_kb()`. Our new `onboarding_guide.md` and `capabilities_reference.md` were placed in `~/.thoughtmachine/knowledge/user/` — the writable user area with no seed mechanism.

**Resolution (Option A):** Moved content to `resources/global_kb/` and updated system prompt references from `user/` to `system/`. The files will now ship with TM and auto-sync on version changes like all other system-level KB docs.

## 2026-07-01 — ## 2026-07-02 — Master Vault: Open Questions & Loose Threads...

## 2026-07-02 — Master Vault: Open Questions & Loose Threads

### Q1: Should the Bridge be split into multiple managers?
The bridge god object grows with every feature. Options:
- Split into SessionBridge, WorkerBridge, ConfigBridge
- Keep unified but refactor internals (facade pattern)
- Decision needed before Panel Unification Sprint 2

### Q2: Should Workers share a container or each get one?
- Shared container: resource-efficient, but no isolation between workers
- Per-worker container: strong isolation, higher resource usage
- Hybrid: default shared, opt-in per-worker isolation
- Decision needed before container persistence feature

### Q3: Should the default security model be deny-all or ask-first?
- Deny-all: most secure, but poor UX (everything breaks until configured)
- Ask-first: better UX, but normalizes granting permissions (habituation risk)
- Middle ground: deny-all + guided first-run wizard
- Decision needed before 1.0 release

### Q4: What is the long-term role of the Worker?
- Are workers sub-agents you talk to, or background automation scripts?
- Different answers lead to different UI (chat-based vs log-based)
- Current design: hybrid (chat + output panel)
- Decision: revisit after Panel Unification

### Q5: Should the config system support hot-swap or restart?
- Hot-swap: seamless UX, but complex to implement correctly
- Restart: simple, reliable, but disruptive
- Current: mixed (some hot-swap, some restart)
- Decision: full analysis done, but no final ruling

### Q6: Should session state be fully ephemeral with optional save, or always persisted?
- Ephemeral: simpler, faster, privacy-friendly
- Always persisted: crash recovery, audit trail
- Current: always persisted (sessions auto-save)
- Decision: seems settled for now

### Q7: Should we support multiple frontends (CLI, desktop, API)?
- Currently: Web UI only (React)
- CLI: useful for scripting and power users
- Desktop: OS-native feel (Tauri/Electron)
- API: programmatic access (REST + WebSocket already exists)
- Decision: no active work, but architecture supports it (Rule 5)

### Q8: Where should system prompt templates live?
- Currently: hardcoded in Python, overridable via config
- Option: move to config files entirely (YAML/markdown)
- Option: allow user-defined prompt profiles/personas
- Decision: Hot-swappable system prompts idea exists in roadmap
