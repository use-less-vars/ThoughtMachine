# Roadmap

Project milestones, planned features, and long-term goals.

## Current Status
- **Phase 1 (Core State Machine):** ✅ Complete (1.1–1.14)
- **Phase 2 (Event Pipeline + Token Restrictions):** ✅ Complete (1.15–1.18, plus event fixes)
- **Phase 3 (GUI Adaptation & Grace-Turn Preservation):** 🟢 **Now** — task 3.1 first
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
