# Ticket: bug/health-route-event-log-tail-unscoped

**Filed:** 2026-10-31 (W3 isolation wave, worktree `w3/isolation-fix`).
**Severity:** HIGH — unauthenticated, unscopable content disclosure: `GET /api/system/health` returned the last
20 records of the ONE vault-global `event_log.jsonl` to any peer that could reach the port, and the route had no
caller gate of any kind.
**Status:** FIXED in tree (uncommitted at filing) — see "Fix shape". Pinned by
`tests/test_system_health_local_only.py` (new, 7 tests) and `tests/test_health_endpoint.py` (7 tests, premise
rewritten: the route no longer has a session or event-log surface to scope).

## Defect

Two independent defects shared one route.

**(1) An event-log tail that cannot be scoped.** The third response key was assembled straight out of the
process-wide sink:

```python
# web_ui/backend/health_routes.py:208-232  (before)
    # 3. recent_event_log_tail — from EventLogger (filesystem)
    event_log_entries: list[dict] = []
    event_log_note: Optional[str] = None
    try:
        from agent.logging.event_logger import EventLogger

        entries = EventLogger.instance().get_tail(20)
        if entries:
            event_log_entries = entries
        else:
            event_log_note = (
                f"Event log is empty or not found at "
                f"{EventLogger.instance().file_path}"
            )
    except Exception as exc:
        event_log_note = f"EventLogger unavailable: {exc}"

    return {
        "running_workers": running_workers,
        "active_sessions": active_sessions,
        "recent_event_log_tail": {
            "entries": event_log_entries,
            "note": event_log_note,
        },
    }
```

`EventLogger` writes **one vault-global file shared by every session and every workspace** — the module says so
in the reader-side comment it carries (`agent/logging/event_logger.py:162-164`). `get_tail(20)` applies no filter
whatsoever: no session, no workspace, no caller, no redaction at render. The records carry conversational working
material in `data` (`user_query` bodies, `tool_result` output, container commands); `_writer_loop` applies
`redact()` at write time (`event_logger.py:186`), which masks secret-*shaped* strings and removes nothing else,
and the tool path additionally masks message bodies at render (`_redact_event_body`,
`tools/workspace/check_system.py:1058-1086`, called at `:1167`). This route rendered the stored `data` dicts as
JSON with neither guard.

Every other reader of that file refuses to serve it unscoped: `_query_event_log` (`check_system.py:1088`) fails
closed on a missing session (`:1100-1105`, *"No session scope on this call; refusing to return the global event
log."*). This route served it unconditionally to anybody.

**(2) No local-caller gate.** No `Depends(...)`, no API key, no principal/session lookup, no peer-address check;
the module's only FastAPI imports were `APIRouter, Query`. The router is mounted with no guard
(`web_ui/backend/server.py:3800 app.include_router(health_router)`; import at `:223`), so reachability was
whatever the bind allowed: `--host` defaults to loopback (`server.py:6185`,
`os.environ.get("HOST", "127.0.0.1")`; docstring `:25-27`), but the override is documented and CORS policy is
computed permissively at `server.py:1211` (`_cors_allowed_origins()`, with the `*` + credentials combination
described at `:1203`). On a shared machine, or on any deployment that sets `HOST`, the audience was "any local
process or any browser page".

The asymmetry that made (2) load-bearing rather than cosmetic: the *unstructured* `GET /health` in `server.py`
**is** loopback-gated (`server.py:4019-4063` — `_LOCAL_CLIENT_HOSTS` and the loopback refusal path). Its
structured sibling, which returns strictly more content, was not.

## Why it matters

- The tail is working material, not telemetry. 20 records is roughly one busy turn of somebody else's session.
- No scope means "the last 20 records of the whole machine". That is a superset of what
  `ticket-bug-checksystem-event-log-cross-session-leak.md` (CRITICAL, F17) is about, reached without a tool call
  and without any of the scoping that ticket's fix installed on the tool path.
- Deleting the tail costs the operator nothing: the route's job is the worker/session inventory, and the
  session-scoped `event_log` query already serves any real need for log content.
- Making the route loopback-only is also free: it is an operator probe, not a client-facing endpoint (no UI
  consumer — `docs/ui_map.md` records that App hits `/api/health` instead).

## Fix shape (as delivered)

Both halves, in one pass, because each is cheap and they close different doors.

- **Delete the tail.** `health_routes.py` no longer imports or touches `EventLogger` (grep: 0 occurrences).
  Response keys are exactly `running_workers` and `active_sessions` (`:254-257`). The reason is recorded in the
  handler docstring (`:220-226`): one vault-global file has no safe tail to render, so callers use
  `CheckSystem(query="event_log")`, which is session-scoped. The inert `session_id` query parameter is gone;
  `workspace` stays (a pre-existing substring filter, `:240-252`, pinned by `test_workspace_filter`).
- **Gate to local callers.** `_localhost_only(request)` (`health_routes.py:196-209`) returns
  `403 {"error": "localhost only"}` for any peer outside `_LOCAL_CLIENT_HOSTS` (`:187`: `127.0.0.1`, `::1`,
  `localhost`) plus the in-process ASGI harness name `_TEST_HARNESS_CLIENT_HOSTS` (`:193`: `testclient`, not
  routable). `request.client is None` — the ASGI server could not report a peer — is treated as **not** local
  (fail closed). The caller's address is never echoed in the body. Applied as the first statement of the handler
  (`:234-236`).
  Duplicated from `server.py::_localhost_only` rather than imported, because `server.py` imports this module
  (`:223`) and importing back is circular; the duplication is commented at `:180-186`.
- **Tests.** New `tests/test_system_health_local_only.py` (233 lines, 7 tests, hermetic — no log file, no session
  store, no HTTP server):
  `test_remote_client_is_refused`, `test_remote_client_cannot_reach_workers_or_sessions`,
  `test_missing_peer_address_is_refused`, `test_loopback_client_is_served`,
  `test_payload_has_no_event_log_content` (installs a tripwire `EventLogger` whose `get_tail` records any call
  and whose single record holds `EVENT-LOG-SECRET-DO-NOT-LEAK`), `test_workspace_filter_still_applies`,
  `test_session_id_parameter_no_longer_scopes_anything`.
  `tests/test_health_endpoint.py` (249 lines, 7 tests) keeps its collection-level tests: the two
  `..._session_scoped_...` names pin the *on-disk layout* (`workers/<session_id>/<name>/status.json` and the
  dir-name fallback for the `session_id` *field*), which is unrelated to the deleted query parameter.

## Negative controls (mutation evidence, not just green tests)

1. The new test file, run against the **unfixed** `health_routes.py` (copy of the pre-fix tree): **4 failed /
   3 passed** (exit 1). Failures: the three gate tests, plus `test_payload_has_no_event_log_content`, which fails
   with `Extra items in the right set: 'recent_event_log_tail'` — it detects the tail by key name, not by luck.
2. Fixed tree, three files together
   (`test_health_endpoint.py`, `test_system_health_local_only.py`, `test_event_log_scoped.py`): **25 passed**
   (exit 0).

## Residuals (honest)

- **Not verified against a real server.** All gate evidence is in-process `TestClient` with an injected
  `scope["client"]`. No uvicorn was started and no second host was probed. `--host 0.0.0.0` plus an off-box
  `curl` remains unrun.
- **The rest of the payload is still not workspace-scoped.** `running_workers` carries every workspace's
  `workspace_path` and each worker's `current_task` verbatim; the only filter is the optional `?workspace=`
  substring (`:240-244`). Loopback gating changes *who* can read that, not *what* it contains. The content half
  stays with `ticket-bug-health-route-running-workers-bypasses-check-system.md` (delegate to the tool layer vs.
  filter in place) and the tool half with `ticket-bug-checksystem-running-workers-global-leak.md`.
- **The stale code comment is now fixed.** The gate's rationale block used to read "`/api/system/health`
  exposes vault-global state (the worker registry, the open sessions, and the event log)" (`health_routes.py:182-184`)
  — two of three once the tail was deleted. Corrected on 2026-10-31 in a follow-up comment-only edit, now naming
  the running-worker registry and the open sessions; no code in that file changed, and the server was not
  restarted.
- **Findings seen but not fixed** are logged in `ticket-residual-w3-isolation-findings-not-fixed.md`
  (`ReadFile` path-policy divergence, the duplicated `_localhost_only`, the un-audited sibling routes, the
  500-line read window).
- **Sibling routes untouched.** `GET /api/global/summary` and `GET /api/credentials` (`global_routes.py`) are the
  same shape of ungated exposure; see `ticket-bug-unauthenticated-routes-beyond-health.md`.
- **Docs drift corrected in the same wave** (markdown only): `docs/design-logging-audit.md` (reader list,
  §3.2 table, §3.3, §5.2 counts, §5.4 breakdown, §5.5 line, §5.6 arithmetic), `docs/workspace_isolation_matrix.md`
  (rows at `:189` and `:275`), `docs/ui_map.md` (`:194`, `:1522`, `:1689`, `:2073`).
- No commits made; the worktree is uncommitted at filing.

## Related

- `ticket-bug-health-route-running-workers-bypasses-check-system.md` — the *content* half of this route (worker
  inventory assembled by the route itself, `current_task` verbatim, no ownership check). Its Evidence line listing
  `recent_event_log_tail` among the response keys is now historical; a dated note has been appended there.
- `ticket-bug-checksystem-event-log-cross-session-leak.md` (Gen 47, F17, CRITICAL) — the tool-path half of the
  same file.
- `ticket-bug-event-log-global-no-workspace-scope.md` (Gen 44) — writer-side attribution; partly landed, see
  `ticket-test-checksystem-event-log-session-workspace-conflation.md` for what is still missing.
- `ticket-bug-unauthenticated-routes-beyond-health.md` — the remaining unguarded routes.
- `ticket-security-workspace-vault-isolation.md` — umbrella.

## MUST NOT TOUCH

Same list as the container tickets.
