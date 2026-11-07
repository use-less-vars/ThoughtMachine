# Ticket: bug/container-sweep-rm-vs-explicit-stop

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** sub-finding of bug/container-turn-start-reap-kills-running
**Status:** filed, not dispatched.

## Defect

Auto-sweep teardown uses docker rm (unrecoverable, 404, dropped from ContainerListTool). Explicit ContainerStopTool uses docker stop (exited, listed, note intact). The invisible background path is MORE destructive than the deliberate user path. Backwards. Inv.6 violation: two functions compute 'end this container' with different destructiveness, no shared implementation. Surfaced by bug/container-turn-start-reap-kills-running.

## Bearing

Filed alongside the primary ticket. Not to be dispatched until the Wave 9
read-first (turn-start reap) returns — the primary read may reshape these.
