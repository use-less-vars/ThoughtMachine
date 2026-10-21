# Ticket: hygiene/container-status-passive-read

**Filed:** Wave 10a commit 7 (branch `fix/wave10a-container-truth`, off dev @ 5528b10c)
**Source:** `ticket-bug/container-status-tool-executes` (field agent saw pid 326/500 running a `/proc` loop inside the target during a *read*).
**Severity:** Low (was Medium) — read tool no longer mutates / is no longer an execution path.
**Status:** FIXED in Wave 10a commit 7.

## Defect

`ContainerManager.status()` enumerated processes by exec'ing a shell inside the
target: `/bin/sh -c for p in /proc/[0-9]*; do ... done`. A read tool was an
execution path, and the exec was absent from `recent_commands`.

## Fix

`infra/container_manager.py::status()` now reads the process list from the
passive daemon call `container.top()` (`GET /containers/{id}/top`) via a new
`ContainerManager._parse_top()` helper that maps `{"Titles", "Processes"}`
into `running_processes`. No shell is exec'd for processes.

## Kept vs dropped

- **Kept:** `running_processes` (`[{"pid", "command"}, ...]`) — same shape and
  position; the datum *is* available passively, so nothing was dropped.
- **Failure mode preserved:** a failing `top()` appends `"process scan failed"`
  and omits the key, exactly as the old exec failure did.
- **Unchanged:** `installed_packages` (pip), `disk_usage` (du) and
  `recent_commands` (usage.log tail) still use the best-effort exec seam; no
  passive Docker API exposes them, so they stay (and stay in `recent_commands`).

## Future-wave guidance

`container.top()` is the only passive process source; if a *complete* no-exec
status is ever mandated it must drop `installed_packages` / `disk_usage` /
`recent_commands` too. The same exec seam (`_exec_checked`) still backs
`container_summary()` (list path) — a later wave could make that passive the
same way. Regression guard: `tests/test_wave10a_status_no_exec.py`.
