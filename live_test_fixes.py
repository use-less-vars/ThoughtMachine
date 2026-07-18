#!/usr/bin/env python3
"""
Live integration test for the two bug fixes in WorkerBusAdapter:

Fix #1: emit_state_sync dedup — skips publishing when payload hasn't changed
Fix #2: forward_agent_event — properly forwards token_recovery, context_cleared, token_warning

This script exercises the real WorkerBusAdapter with a real EventBus,
captures all pipeline logs, and reports a clear summary.
"""

import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Environment: enable all pipeline logging ────────────────────────────────
os.environ["TM_LOG_TAGS"] = "*pipeline*"
os.environ["TM_LOG_LEVEL"] = "TRACE"  # Show TRACE level too (for dedup messages)
os.environ["THOUGHTMACHINE_DEBUG"] = "1"

# Capture all console output
from io import StringIO
captured_output = StringIO()
_original_stdout = sys.stdout
sys.stdout = captured_output

# ── Imports ──────────────────────────────────────────────────────────────────
from agent.events import EventBus, EventType
from tools.workspace.worker import WorkerBusAdapter


def log(msg):
    """Write to both captured buffer and real stdout so we can see progress."""
    _original_stdout.write(msg + "\n")
    _original_stdout.flush()


log("=" * 72)
log("LIVE TEST: WorkerBusAdapter Bug Fixes")
log("=" * 72)
log("")

# ── Create a real EventBus ───────────────────────────────────────────────────
event_bus = EventBus()
published_events = []

def collector(event):
    """Collector subscriber that records all published events."""
    published_events.append(event)

# Subscribe to ALL event types so nothing is missed
for et in EventType:
    event_bus.subscribe(et, collector)

log("[SETUP] EventBus created with collector subscriber")
log("")

# ── Create WorkerBusAdapter ──────────────────────────────────────────────────
adapter = WorkerBusAdapter(event_bus=event_bus, worker_name="live_test_worker")
adapter._state = None  # Dummy state to prevent ExecutionState lookups

log("[SETUP] WorkerBusAdapter created: worker_name=" + adapter.worker_name)
log("")

# =============================================================================
# TEST 1: Dedup in emit_state_sync
# =============================================================================
log("─" * 60)
log("TEST 1: emit_state_sync DEDUPLICATION")
log("─" * 60)

# Make 3 calls with the same data — only the first should publish
log("[TEST 1a] 3 calls with IDENTICAL data (context_length=100)...")
published_before = len(published_events)

adapter.emit_state_sync(context_length=100)
adapter.emit_state_sync(context_length=100)
adapter.emit_state_sync(context_length=100)

published_after = len(published_events)
published_count_1a = published_after - published_before
log("  -> Published events: " + str(published_count_1a) + " (expected 1)")

# Make 3 more calls with different data
log("")
log("[TEST 1b] 3 calls with DIFFERENT data (100, 200, 300)...")
published_before = len(published_events)

adapter.emit_state_sync(context_length=100)  # Same, should be skipped
adapter.emit_state_sync(context_length=200)  # Different, should publish
adapter.emit_state_sync(context_length=300)  # Different, should publish

published_after = len(published_events)
published_count_1b = published_after - published_before
log("  -> Published events: " + str(published_count_1b) + " (expected 2)")

# Change data then go back to original
log("")
log("[TEST 1c] Sequence: A->B->A...")
published_before = len(published_events)

adapter.emit_state_sync(context_length=50)    # New data A
adapter.emit_state_sync(context_length=999)   # New data B
adapter.emit_state_sync(context_length=50)    # Back to A

published_after = len(published_events)
published_count_1c = published_after - published_before
log("  -> Published events: " + str(published_count_1c) + " (expected 3)")

# Same data twice in a row (should dedup)
log("")
log("[TEST 1d] Two identical calls: A->A...")
published_before = len(published_events)

adapter.emit_state_sync(context_length=777)
adapter.emit_state_sync(context_length=777)

published_after = len(published_events)
published_count_1d = published_after - published_before
log("  -> Published events: " + str(published_count_1d) + " (expected 1)")

# Full summary
total_published_emit = published_count_1a + published_count_1b + published_count_1c + published_count_1d
total_calls_emit = 3 + 3 + 3 + 2  # = 11
expected_publishes = 1 + 2 + 3 + 1  # = 7
log("")
log("  DEDUP SUMMARY:")
log("  Total calls made:   " + str(total_calls_emit))
log("  Total publishes:    " + str(total_published_emit))
log("  Calls skipped:      " + str(total_calls_emit - total_published_emit) + " (dedup savings)")
if total_published_emit == expected_publishes:
    log("  RESULT: DEDUP WORKING (publishes match expected " + str(expected_publishes) + ")")
else:
    log("  RESULT: DEDUP MISMATCH (got " + str(total_published_emit) + ", expected " + str(expected_publishes) + ")")

log("")

# =============================================================================
# TEST 2: forward_agent_event event types
# =============================================================================
log("─" * 60)
log("TEST 2: forward_agent_event - token_recovery, context_cleared, token_warning")
log("─" * 60)

published_before = len(published_events)

# token_warning
adapter.forward_agent_event({
    "type": "token_warning",
    "token_count": 45000,
    "warning_message": "Token usage warning: 45000 tokens",
    "old_state": "LOW",
    "new_state": "WARNING",
})

# token_recovery
adapter.forward_agent_event({
    "type": "token_recovery",
    "token_count": 5000,
    "recovery_message": "Token usage has returned to safe levels after summarization.",
    "old_state": "WARNING",
    "new_state": "LOW",
})

# context_cleared
adapter.forward_agent_event({
    "type": "context_cleared",
    "token_count": 2000,
    "old_state": "WARNING",
    "new_state": "LOW",
    "recovery_message": "Context cleared after summarization.",
})

# agent_responded (common type)
adapter.forward_agent_event({
    "type": "agent_responded",
    "content": "Hello! I am the ThoughtMachine agent.",
    "response_type": "answer",
})

# tool_call and tool_result
adapter.forward_agent_event({
    "type": "tool_call",
    "tool_name": "ReadFile",
    "arguments": {"file_path": "test.txt"},
})

adapter.forward_agent_event({
    "type": "tool_result",
    "tool_name": "ReadFile",
    "success": True,
    "result": "File contents here.",
})

published_after = len(published_events)
forwarded_count = published_after - published_before

# Check exactly which types were published
forwarded_types = {}
for evt in published_events[-forwarded_count:]:
    forwarded_types[evt.type.value] = forwarded_types.get(evt.type.value, 0) + 1

log("")
log("  Published forward_agent_event types:")
for t in sorted(forwarded_types.keys()):
    log("    - " + t + ": " + str(forwarded_types[t]) + " event(s)")

log("")

# =============================================================================
# Restore stdout and capture the pipeline logs
# =============================================================================
sys.stdout = _original_stdout
full_output = captured_output.getvalue()

# =============================================================================
# ANALYSIS: Search for key log lines
# =============================================================================
log("=" * 72)
log("ANALYSIS: Searching captured log output")
log("=" * 72)
log("")

lines = full_output.split("\n")

emit_sync_lines = [l for l in lines if "[TOKEN_PIPELINE] WorkerBusAdapter.emit_state_sync:" in l]
emit_forward_lines = [l for l in lines if "[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event:" in l]
dedup_trace_lines = [l for l in lines if "emit_state_sync SKIPPED" in l]

log("  [TOKEN_PIPELINE] emit_state_sync calls:           " + str(len(emit_sync_lines)))
log("  [TOKEN_PIPELINE] forward_agent_event calls:       " + str(len(emit_forward_lines)))
log("  Duplicate skip (dedup) messages:                  " + str(len(dedup_trace_lines)))
log("")

# Show key dedup lines
log("─" * 60)
log("KEY DEDUP LOG LINES:")
log("─" * 60)
for line in dedup_trace_lines[:5]:
    log("  " + line.strip())
log("")

# Show key forward_agent_event lines
log("─" * 60)
log("KEY FORWARD_AGENT_EVENT LOG LINES:")
log("─" * 60)
for line in emit_forward_lines[:10]:
    log("  " + line.strip())
log("")

# =============================================================================
# FINAL SUMMARY
# =============================================================================
log("=" * 72)
log("FINAL VERDICT")
log("=" * 72)
log("")

# Fix #1: Dedup
dedup_working = len(dedup_trace_lines) > 0 and total_published_emit < total_calls_emit
if dedup_working:
    log("  PASS FIX #1 (emit_state_sync dedup): WORKING")
    log("     " + str(len(dedup_trace_lines)) + " duplicate call(s) correctly skipped")
    log("     Published " + str(total_published_emit) + "/" + str(total_calls_emit) + " calls (" + str(total_calls_emit - total_published_emit) + " saved)")
else:
    log("  FAIL FIX #1 (emit_state_sync dedup): NOT WORKING")
log("")

# Fix #2: forward_agent_event
forward_types_found = set(forwarded_types.keys())
expected_forward_types = {"token_warning", "token_recovery", "context_cleared", "worker_message", "tool_call", "tool_result"}
if forward_types_found.intersection(expected_forward_types) == expected_forward_types:
    log("  PASS FIX #2 (forward_agent_event): WORKING")
    log("     All expected event types forwarded correctly")
else:
    missing = expected_forward_types - forward_types_found
    log("  PARTIAL FIX #2 (forward_agent_event): PARTIAL")
    log("     Missing types: " + str(missing))
log("")

# Detail per type
log("  Forwarded event types detail:")
for t in sorted(expected_forward_types):
    count = forwarded_types.get(t, 0)
    status = "PASS" if count > 0 else "FAIL"
    log("    " + status + " " + t + ": " + str(count) + " event(s)")

log("")
log("=" * 72)
log("LIVE TEST COMPLETE")
log("=" * 72)

# Print all captured output to show the raw pipeline logs
print(full_output)
