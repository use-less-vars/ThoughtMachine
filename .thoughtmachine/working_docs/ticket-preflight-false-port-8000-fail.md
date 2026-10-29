# Ticket: preflight reports FAIL on port 8000 while a previous instance is still shutting down

Status: open

## Problem

Preflight reports `FAIL` on port 8000 when a previous instance is still shutting down, even though
the later `[4/8]` Ports step handles exactly that situation and passes. The early check runs
*before* the step that actually stops/kills the old listener, so it reports a transient occupancy
as a hard failure. The result is a red preflight for a condition the same run resolves a few
seconds later.

## Evidence

- Preflight's port check fails on `8000` in the window between the old process beginning to shut
  down and releasing the listening socket.
- The `[4/8]` Ports step, which stops/kills the old listener, runs afterwards and passes.
- The two checks disagree about the same port in the same run, which means one of them is measuring
  a state the other one repairs.

## Suggested fix

Either run the port check after the `[4/8]` Ports step (so it measures the settled state), or have
the early check retry for a bounded window while a known old instance is shutting down, and only
report `FAIL` if the port is still held afterwards. Do not simply downgrade the early check to a
warning: a port held by an unknown process after shutdown should still fail.
