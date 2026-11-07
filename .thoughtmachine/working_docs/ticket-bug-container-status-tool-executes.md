# Ticket: bug/container-status-tool-executes

**Filed:** Gen 44, 2026-10-15, from field agent 22:00:45 check-in.
**Severity:** Medium - a read tool that mutates; execution path not audited.
**Status:** filed, not dispatched.

## Finding

`ContainerStatusTool` returns a process list by exec'ing a shell command
inside the target container:

    /bin/sh -c for p in /proc/[0-9]*; do ... done

The field agent observed pid 326 (then 500) running this loop as part of
the tool call that was nominally a *read*.

Three consequences:

1. **A read tool is an execution path.** Either document it or switch to
   `docker top` / `container.inspect` (passive).
2. **Not logged in `recent_commands`** - `recent_commands` is not a
   complete audit record of in-container execution.
3. Silver lining: the field experiment was not contaminated - only
   `DockerCodeRunner` / `ContainerExecTool` dirty `recent_commands`.

## Fix shape

- Replace the exec-based proc enumeration with `docker top` or
  `container.inspect` (read-only daemon call).
- If exec-based is kept, add the call to `recent_commands` and document
  that `Status` is an exec path.

## MUST NOT TOUCH
Same list as the container tickets.
