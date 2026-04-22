ThoughtMachine Pruning & Context Management – Technical Documentation
1. Overview

ThoughtMachine maintains two parallel representations of a conversation:

    user_history – an append‑only list of every message (user, assistant, tool, system, warnings, summaries). This is the ground truth, used for the GUI and for reconstructing LLM context.

    LLM context – a sliding window built from user_history that is actually sent to the language model. It excludes messages that have been pruned or summarised, staying within token limits.

The pruning mechanism is the system that decides when and how to compress older conversation turns into a summary, resetting the LLM’s context window without losing critical information.
2. Core Concepts
2.1 Turns

A turn is one round‑trip interaction with the LLM that produces an assistant response.
Concretely:

    A user message always starts a new turn.

    An assistant message with tool_calls also starts a new turn (important after pruning, when the user part may be missing).

    tool result messages belong to the turn that requested them.

    An assistant message without tool_calls continues the current turn.

Turn grouping is used to insert summaries at turn boundaries, never splitting a turn.
2.2 user_history – The Append‑Only Log

    Every message is appended or inserted (e.g., a summary system message can be inserted at a turn boundary).

    Messages are never deleted – the full history is preserved for auditing, debugging, and GUI display.

    Each message has a sequential index (idx), a role (system, user, assistant, tool), and content.

2.3 LLM Context – The Sliding Window

Built by SummaryBuilder.build() (in session/context_builder.py).
The context always contains:

    The main system prompt (first system message).

    The latest summary system message (if any).

    All messages after that summary (the “kept” turns).

    Any system warnings that appear after the summary (relevant warnings).

Messages before the latest summary are excluded – they are considered “pruned”.
3. Summarisation (Pruning) Flow
3.1 Trigger

The agent calls SummarizeTool – usually after receiving a token warning or when it decides to reduce context.
The tool receives two parameters:

    summary – a textual summary of the pruned conversation.

    keep_recent_turns – number of most recent turns to keep intact (default 3).

3.2 What Happens Inside SummarizeTool

    Tool call and result are appended to user_history like any other tool.

    The agent’s core then calls _apply_summary_pruning() (in agent/core/agent.py).

3.3 _apply_summary_pruning Steps

    Compute insertion index – _find_summary_insertion_index(keep_recent_turns) scans user_history, groups messages into turns, and returns the index of the first message of the oldest turn to keep. This is the point where the summary will be inserted.

    Insert summary system message – a new system message with the summary text is inserted at that index. All later messages shift right.

    Append unwarning – after the summary tool result (already in user_history), an unwarning message is appended:
    text

    [SYSTEM NOTIFICATION] Context has been summarized. You now have a fresh context window and full access to tools.

    This is appended, not inserted, so it appears after the tool result, preserving chronological order.

    Logging – debug logs record before/after state, insertion index, and turn counts.

3.4 Insertion Example

Suppose user_history has indices 0…100, and keep_recent_turns=3.
_find_summary_insertion_index returns idx = 85 (start of the 3rd‑last turn).
After insertion:

    New summary system message at index 85.

    Old indices 85…100 become 86…101.

    Unwarning is appended at the end (index 102).

The LLM context will now start at the new summary system message (index 85) and include everything after it – i.e., the kept turns (originally 85…100) plus the unwarning. Old messages (0…84) are excluded.
4. Token Warnings and Their Lifecycle
4.1 Generation

AgentState.update_token_state() (in agent/core/state.py) monitors total tokens against thresholds:

    token_monitor_warning_threshold (default 35k) → emits a light warning.

    token_monitor_critical_threshold (default 50k) → emits a critical warning and starts a countdown (default 5 turns).

4.2 Injection into user_history

Warnings are added as messages with role="user" and content prefixed by [SYSTEM NOTIFICATION].
They are appended to user_history at the moment they are triggered.
4.3 Inclusion in LLM Context

    If a warning appears before the latest summary system message, it is excluded from the LLM context (stale).

    If it appears after the summary, it is included (current).

Bug fixed earlier: The context builder originally filtered all warnings from post_summary (including current ones). That was corrected by removing the warning‑filtering block in SummaryBuilder.build(). Now only the insertion index decides inclusion.

4.4 System Notification Metadata Flag

All system notifications (token warnings, turn warnings, countdown expiry, context cleared / unwarning) now carry a metadata flag:

{
"role": "user",
"content": "[...]",
"is_system_notification": true
}

This flag is used only in turn counting and summary insertion logic: in _find_summary_insertion_index and _group_messages_into_turns. Notifications with this flag are skipped when determining turn boundaries, ensuring that they never affect where a summary is inserted. However, they are not excluded from the LLM context; they appear in the context as normal user messages, preserving chronological order.

Old sessions (without the flag) are still supported via fallback content-based checks for [SYSTEM NOTIFICATION] and [SYSTEM NOTIFICATION] patterns. The previously missing four-asterisk pattern [SYSTEM NOTIFICATION] has also been added to the fallback.

Because the flag prevents notifications from shifting the insertion index, they stay in their original chronological order relative to user and assistant messages. The LLM sees them in the correct sequence, and summarisation no longer causes expired warnings or other system notifications to appear out of place.


5. The Unwarning Placement Fix (Critical)
5.1 The Problem

Originally, the unwarning was inserted immediately after the summary system message (at the same turn boundary). This caused:

    Chronological inversion: the unwarning appeared before the SummarizeTool result and before any old warnings that were legitimately kept.

    Confusion for the agent, which sometimes missed the “context cleared” signal.

5.2 The Fix

In _apply_summary_pruning (and its fallback), the unwarning is now appended to the end of user_history after the summary tool result has already been added.

Code change (example):
python

# Before (buggy)
user_history.insert(summary_position + 1, context_cleared_msg)

# After (correct)
user_history.append(context_cleared_msg)

5.3 Resulting Order in user_history After Summarisation
text

... (older messages, now before summary insertion point)
[critical warning]                ← old, will be excluded
[assistant: tool call to SummarizeTool]
[tool result: summary text]
[unwarning]                       ← appended after tool result
[new user message ...]            ← kept turns (if any)

The LLM context starts at the inserted summary system message (which is at the turn boundary, before the kept turns). The old warning is before that boundary → excluded. The unwarning is after the boundary → included, in correct chronological order.
6. Interaction Between user_history and LLM Context
Aspect	user_history	LLM Context
Content	All messages ever added	Subset: system prompt + latest summary + messages after it
Mutability	Append‑only (except summary insertion)	Rebuilt on every LLM call
Includes warnings	All warnings ever added	Only warnings that appear after the latest summary
Includes summary	Two copies: system message (inserted) + tool result (appended)	Only the system message (tool result is before the summary boundary)
GUI display	Shows everything	Not used by GUI

The GUI reads directly from user_history; the LLM reads from the filtered context. This separation allows full history preservation while respecting token limits.
7. Key Code Locations
Component	File	Purpose
_apply_summary_pruning	agent/core/agent.py	Inserts summary system message, appends unwarning
_find_summary_insertion_index	agent/core/agent.py	Finds turn boundary for insertion
SummaryBuilder.build	session/context_builder.py	Builds LLM context from user_history
AgentState.update_token_state	agent/core/state.py	Generates token warnings
_add_to_conversation	agent/core/agent.py	Adds messages to user_history
8. Testing & Verification

To verify correct behaviour after a summarisation:

    Enable debug logging:
    bash

    export TM_LOG_LEVEL=DEBUG
    export TM_LOG_TAGS=core.context,debug.dump,core.pruning
    export TM_DEBUG_TRUNCATE_LENGTH=10000

    Run a conversation that triggers a token warning and then SummarizeTool.

    Inspect the JSONL log (logs/agent_*.jsonl) for the llm_context final entry.

    Confirm:

        The context starts with the summary system message.

        No old warnings appear before that message.

        The unwarning appears after the summary system message (and after any tool result, if kept).

        The agent acknowledges the unwarning (e.g., “I have a fresh context window”).

9. Common Pitfalls & Design Decisions

    Why two copies of the summary?
    The system message resets the LLM context; the tool result provides a record that the agent called the tool. Both are useful for different purposes (context building vs. audit/GUI).

    Why not delete old messages?
    Append‑only ensures full traceability, allows the GUI to show the complete conversation, and avoids data loss.

    Why is the unwarning appended instead of inserted?
    To preserve chronological order relative to the tool call that caused the summary. The agent must see the unwarning after it called SummarizeTool.

    What about stale warnings in the GUI?
    They are normal – the GUI shows the full history. If desired, a display filter can hide them, but that is a separate UI feature, not a core bug.

10. Summary

The pruning mechanism successfully balances token efficiency with conversation continuity by:

    Keeping an immutable full history (user_history).

    Inserting a summary system message at a turn boundary.

    Building the LLM context starting from that summary.

    Appending an unwarning after the summary tool result for correct chronology.

    Excluding messages before the summary from the LLM context.

The recent fix for unwarning placement resolved the last major ordering issue, making the agent reliably aware of context resets. The system is now stable and ready for production use.

Document version: 1.0 – Last updated: 2026‑04‑21