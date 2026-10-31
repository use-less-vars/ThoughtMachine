# Ticket: residual/w3-isolation-findings-not-fixed

**Filed:** 2026-10-31 (W3 isolation wave, worktree `w3/isolation-fix`).
**Severity:** Medium overall — one Medium (R1: unguarded divergence from the file-tool policy), two Low
(R2: duplicated gate, R4: reliability), one process gap (R3: unmeasured surface).
**Status:** OPEN — logged so the findings are not lost. None of these is dispatched, and none is covered by
`6b7f98bb` (which gated `/api/system/health` and deleted its event-log tail) or by
`ticket-test-checksystem-event-log-session-workspace-conflation.md` (which closed a test-coverage gap).

These four items were *seen* while doing the W3 isolation audit and deliberately left alone: they are either
out of the audit's scope, or they need a judgement call the audit was not chartered to make. They are recorded
here rather than in the fix ticket so that a reader does not mistake "the fix shipped" for "the audit came back
clean".

## R1 — `ReadFile` bypasses the tool-layer path policy (Medium)

`tools/read_file_tool.py:68-76` does its own containment and nothing else:

```python
# tools/read_file_tool.py:68-76
            # Resolve the path and prevent directory traversal
            root_path = Path(root).resolve()
            target = (root_path / self.file_path).resolve()

            if not str(target).startswith(str(root_path) + os.sep) \
                    and str(target) != str(root_path):
                return json.dumps({
                    "error": f"Path '{self.file_path}' escapes the workspace directory.",
                })
```

It never calls `self._validate_path(...)`, so it never applies `thoughtmachine/security.py:429-537`'s vault and
sensitive-directory block lists. Every other file tool does: `_validate_path` is called in
`tools/file_preview_tool.py:31`, `tools/directory_tree_tool.py:53`, `tools/file_summary_tool.py:27`,
`tools/code_modifier.py:165`, `tools/apply_edits.py:234/249/257`, `tools/git_info_tool.py:617/1540/1589/1647/1797`
and `tools/git_write_tool.py:333/1021/1115/1168`. `tools/read_file_tool.py` is absent from that list.

**Why it is not a live hole today:** containment is applied to the *resolved* path (`:69-70`), so `..` and
symlinks cannot leave the workspace root. The workspace root is what the caller's own workspace is, so a worker
cannot reach `~/.thoughtmachine/` through it — the vault block list has nothing left to catch **as long as the
workspace root is never itself inside the vault**.

**Why it is still a finding:** that safety is an *emergent* property of two independent facts (resolve-then-check,
and vault-outside-workspace). It is not the policy the platform states. A workspace rooted under the vault, or a
refactor that swaps `Path.resolve()` for a lexical join, silently removes the only barrier — and nothing in the
tool layer would report it.

**Proven by reading:** the absence of `_validate_path` in `tools/read_file_tool.py` and its presence in the nine
other tool modules (grep over `tools/`); the containment block itself; the block lists at
`thoughtmachine/security.py:429-537`.
**Unproven:** that no workspace can be rooted inside the vault — not tested; and no attempt was made to build
such a workspace. No exploit was demonstrated.

**Cheapest fix:** call `self._validate_path(...)` before the containment check, exactly as `FilePreviewTool` does,
then keep the containment check as the second, narrower gate.

## R2 — `_localhost_only` is duplicated, not shared (Low)

The gate now exists twice: `web_ui/backend/server.py:4068` and `web_ui/backend/health_routes.py:196`, each with
its own `_LOCAL_CLIENT_HOSTS` (`server.py:4023`, `health_routes.py:187`) and `_TEST_HARNESS_CLIENT_HOSTS`
(`server.py:4030`, `health_routes.py:193`). The copies are identical today and the reason is documented at
`health_routes.py:180-186`: `server.py` imports `health_routes`, so importing back is circular.

**Divergence risk, stated plainly:** the two copies are consistent *by copy*, which means they stay consistent
only until someone edits one. A future change to the loopback definition — adding an IPv4-mapped form, relaxing
for a container network, or exempting a proxy peer — applied to `server.py:4068` alone would leave
`health_routes.py:196` on the old rule, and the weaker of the two would be the one guarding the vault-global
payload. There is no test that asserts the two copies agree.

**Proven by reading:** both definitions and both constant pairs; the pointer comment; the circular-import reason.
**Unproven:** that they are *behaviourally* identical under a non-loopback peer — no test drives both gates with
the same injected client. (Each gate is tested separately, and `tests/test_system_health_local_only.py:139`
pins only the health copy.)

**Cleanup:** a small shared module (e.g. `thoughtmachine/local_client.py` or `web_ui/backend/local_gate.py`)
imported by both, plus one test that fails if either module defines `frozenset({"127.0.0.1", ...})` locally
again.

## R3 — No loopback-gate sweep of the other vault-global read surfaces (process gap)

Only `/api/system/health` was gated. **No other route was audited for caller gating in this wave.**

That absence must not be read as a result. The isolation matrix (`docs/workspace_isolation_matrix.md`) already
lists vault-scope and worker-scope surfaces whose owner is `UNOWNED` or `NONE` — e.g. `GET /api/global/summary`
(`global_routes.py:322`), `GET /api/credentials` (`global_routes.py:390`), `GET /api/container-records`
(`container_record_routes.py:130`) — and each of those rows records a *scoping* verdict, not a *reachability*
verdict. Nobody asked, for those routes, the question that produced this wave's finding: "can a non-loopback
peer reach it?"

**Proven by reading:** that the sweep did not happen — the wave's own evidence base contains no host/reachability
column for any route but `/api/system/health`; the matrix rows cited above have no caller-gate field.
**Unproven:** whether any of those routes is in fact reachable off-box. The correct reading of R3 is
**"not looked at"**, never "clean".

**Work item:** add a `caller gate` column to the isolation matrix and fill it by reading the mount site plus the
route body for every `UNOWNED`/`NONE` row. Sibling context:
`ticket-bug-unauthenticated-routes-beyond-health.md`.

## R4 — The 500-line read window starves a session on a busy machine (reliability, **not** a leak)

`tools/workspace/check_system.py` reads a fixed backwards window of the single vault-global `event_log.jsonl`
before filtering to the caller's own records:

- `:1123` `max_lines = 500`; the window is filled by seeking from EOF backwards in doubling chunks (`:1124-1145`).
- `:1145` trims to the last 500 whole lines.
- `:1147-1156` keeps only records that `_event_belongs_to_session(...)` claims for this caller.
- `:1158` `shown = owned[-50:]`.

Because the window is taken from the *shared* file **before** the ownership filter, the caller's own records
compete with every other session's. A machine running many workers can write more than 500 records between two of
this session's, pushing this session's records out of the window entirely — the query then truthfully reports an
empty or thin view of the caller's own log.

**Explicitly NOT a leak.** The filter is applied to everything read, unmatched records are discarded, and message
bodies are masked at render (`_redact_event_body`, `:1058-1086`, called `:1167`). Nothing belonging to another
session is ever returned. The defect is starvation, not disclosure — and it does not become a disclosure if the
window grows.

**Proven by reading:** the read-then-filter order and the fixed window size.
**Unproven:** how often it bites in practice — no measurement of records-per-minute, and no test that generates
>500 neighbour records. A test that writes 600 foreign records between two owned ones would pin it.

**Fix shapes (either):** read forward from a per-session offset, or filter while scanning backwards and stop once
`shown` is full, so the window is spent on the caller's own records first.

## Related

- `ticket-bug-health-route-event-log-tail-unscoped.md` — the fix that shipped in `6b7f98bb`; R2 and R3 are its
  direct leftovers.
- `ticket-test-checksystem-event-log-session-workspace-conflation.md` — the test gap, closed.
- `ticket-bug-event-log-global-no-workspace-scope.md` — the writer-side `workspace_id` schema gap behind R4.
- `ticket-bug-health-route-running-workers-bypasses-check-system.md` — the worker-inventory half of the route,
  still open.
- `ticket-bug-unauthenticated-routes-beyond-health.md` — the sibling-route half of R3.

## MUST NOT TOUCH

- Do not treat R1 as permission to change `ReadFile`'s containment logic; the containment is correct and load
  bearing. The ask is to *add* the policy call, not to replace the containment.
- Do not "fix" R2 by making one module import the other; the circularity is real.
- Do not read R3 as evidence of cleanliness in either direction.
- Do not raise the 500-line window as a fix for R4 — that only moves the starvation point and increases the
  per-query cost on a vault-global file.

---

## Status (2026-10-31) — R1–R4 closed, two residuals documented

Branch `w3/residuals` (worktree `.worktrees/w3-resid`). **No commits made.** `web_ui/backend/health_routes.py` untouched (FROZEN).

**R1 — FIXED.** `tools/read_file_tool.py` now calls `self._validate_path(self.file_path)` (the `ToolBase` vault-compartment / sensitive-path policy the other file tools already applied) *before* its existing containment check; containment is unchanged and still runs as the second gate. Tests: `tests/tools/test_read_file_tool_path_policy.py` (14 pass), including a defence-in-depth case that no-ops `_validate_path` and asserts containment still refuses.

**R2 — FIXED for `server.py`, one duplication left by the freeze.** New module `web_ui/backend/local_gate.py` owns `LOCAL_CLIENT_HOSTS`, `TEST_HARNESS_CLIENT_HOSTS` and `localhost_only`. `server.py` now imports all three under its old names (`_LOCAL_CLIENT_HOSTS`, `_TEST_HARNESS_CLIENT_HOSTS`, `_localhost_only`) and its local copies are deleted; `_validate_bind_host` and both call sites are untouched. `health_routes.py` still carries its own copy because it is frozen — that is the **accepted residual**. It is pinned rather than trusted: `web_ui/backend/tests/test_local_gate_shared.py` (28 pass) asserts identity for the `server.py` aliases, source-scans `server.py` for the deleted literals and `def _localhost_only(`, and drives `health_routes._localhost_only`, `server._localhost_only` and `local_gate.localhost_only` through the same host matrix (including `testclient`, `127.0.0.1.evil.com`, empty and `None` peers) asserting identical verdicts and identical 403 bodies. Unfreezing `health_routes.py` collapses the third copy into an import; until then a divergence fails CI.

**R3 — SWEPT, with stated scope limits.** `docs/workspace_isolation_matrix.md` gains §5.5 *Caller gate sweep* (surface × mount site × gate site × verdict), a pointer under §5.2 explaining why the gate is recorded per transport surface rather than per entry-point row, and finding **F17**. Finding: three surfaces gate (`/health`, `/api/health/containers`, `/api/system/health`); 82 of 85 HTTP routes and `WS /ws` do not; reachability rests on a start-up bind-host check that is waivable and proxy-transparent. Also filed into `ticket-bug-unauthenticated-routes-beyond-health.md`. **Limits, so this is not over-read:** the sweep covers peer/caller gating only — not authorisation, not per-fact scoping (§5.1–§5.3 are untouched), and not the vault/tool layers (marked `not audited` in the table). Stale line numbers in §5.2/§5.2b were left stale on purpose and flagged.

**R4 — FIXED, bounded.** `tools/workspace/check_system.py::_query_event_log` no longer reads a fixed 500-line tail and filters afterwards; it scans backwards in growing chunks and applies the session-ownership filter *while* scanning, stopping at 50 owned records. Tests in `tests/test_event_log_scoped.py` (13 pass) cover 1 owned + 600 foreign + 1 owned, and 50 owned behind a 700-record foreign block. **Residual:** the scan is capped at 4 MiB, so a single uninterrupted foreign run larger than that can still starve the window — bounded now, not eliminated.
