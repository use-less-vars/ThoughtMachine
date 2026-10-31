# Ticket: test/checksystem-event-log-session-workspace-conflation

**Filed:** 2026-10-31 (W3 isolation wave, worktree `w3/isolation-fix`).
**Severity:** Medium — an untested invariant on a CRITICAL control. Nothing in the suite would have noticed if the
`event_log` read scope silently widened from *session* to *workspace*, and a workspace-wide match is a real leak
(many sessions share one workspace).
**Status:** FIXED in tree (uncommitted at filing) — 2 tests added to `tests/test_event_log_scoped.py`.
Mutation-verified: the two new tests are the only detectors of the regression.

## Defect

`CheckSystem(query="event_log")` is scoped **by session**. `_event_belongs_to_session`
(`tools/workspace/check_system.py:1030-1055`) accepts exactly three attribution shapes — top-level `session_id`
(`:1048`), `data.session_id` (`:1053`), `data.conversation_id` (`:1055`) — and `_query_event_log` keeps only the
records that match the caller (`:1156`), rendering the last 50 of those (`:1158`).

The tool object *also* carries `workspace_id` (`check_system.py:264`), and every event record *also* carries a
`workspace_id` key (`agent/logging/event_logger.py:169`). Nothing in `_query_event_log`, in
`_event_belongs_to_session`, or in the test suite ever consulted it. The relationship between workspace
attribution and the read scope was therefore entirely unpinned:

- Nothing asserted that workspace context is **not** a scope.
- The consequence of widening it is not cosmetic: many sessions share one workspace, so a workspace-wide match
  hands every caller its neighbours' records — the "same tenant, different session" disclosure, one level below
  F17.
- The pre-existing tests could not have caught it. All 9 built their caller as
  `CheckSystem(query="event_log", session_id=..., session_permissions=...)` with no `workspace_id` (helper
  `_query()`, `:72-79`), and every fixture record carried a session and no workspace. A read path that consulted
  workspace context would have passed the whole file.

## What was pinned before, and what was not

Already pinned by the 9 pre-existing tests: scoping to the caller's session; fail-closed with no session
(`test_event_log_fails_closed_without_session_scope`, `:112`, *"No session scope on this call"*);
`data.conversation_id` and `data.session_id` attribution (`:128` onward); unattributed records excluded; body
redaction per event type; unparseable lines skipped; the render header.

Not pinned by anything: that a *workspace* match is not, and must never become, a scope — in either direction.

## Fix shape (as delivered)

`tests/test_event_log_scoped.py` 287 → 366 lines, 9 → 11 tests. The section is marked
`# -- Session/workspace conflation (W3-T7) --` at `:301`.

- **Helper** `_query_with_workspace(session_id, workspace_id)` (`:82-90`) — same real `execute()` path as
  `_query()`, with plausible workspace context supplied by the caller, so the scope is exercised *with* workspace
  present instead of vacuously absent.
- **`test_event_log_never_shows_a_same_workspace_neighbour`** (`:310`) — two sessions in the *same* workspace
  (`ws-shared`); the caller's own workspace context is that shared workspace. Asserts its own record is present
  and the neighbour's `source` and marker are absent. This is exactly the shape a workspace-widened filter leaks.
- **`test_event_log_never_shows_a_foreign_workspace_record`** (`:338`) — a record owned by another workspace,
  checked twice: once with the caller claiming its **own** workspace id, once with it claiming the **foreign** id.
  Workspace context is not a scope in either direction, so a caller claiming someone else's workspace must not
  filter foreign rows *in*.
- Both tests express workspace attribution as `data.workspace_id`, the shape these tests can actually produce
  today (see "Write-side state"), not as the top-level `workspace_id` key.
- The third case the task named — an unscoped caller getting nothing — is already pinned by
  `test_event_log_fails_closed_without_session_scope` and was deliberately not duplicated; a comment at `:301-307`
  records that, and records what the two new tests exist to fail on.

## Negative control (mutation evidence)

Mutated `_query_event_log`'s filter loop to *widen* the session scope into a workspace-wide fallback — keep the
session match, then also accept any record whose `data.workspace_id` equals the caller's `workspace_id` — i.e.
exactly the regression the tests exist for. Result on the 11-test file: **2 failed / 9 passed** (exit 1), and the
two failures are precisely the two new tests. Reverted; the file was restored byte-identically
(`sha256 4671134c4722ad202096d433b3134aba053af97ea3a2e707284843c512218051` before and after); clean re-run
**11 passed** (exit 0).

Before this change that widening was invisible to the suite; after it, it is a two-test failure.

## Write-side state (corrects an older ticket's claim)

`ticket-bug-event-log-global-no-workspace-scope.md` (Gen 44) states: *"`session_id` is dropped at write time. Zero
occurrences of `session_id` or `workspace_id` in the entire file."* That is **stale**. `agent/logging/event_logger.py:162-169`
now writes both keys at the top level of every record:

```python
                # Write-side half of session scoping for the event log: readers
                # (CheckSystem's event_log query) must be able to attribute a
                # record to a session instead of serving one global tail.
                # ``workspace_id`` is always None for now -- EventMetadata has
                # no such field today; the getattr keeps the key present so
                # readers can rely on it once the model grows one.
                "session_id": getattr(event.metadata, "session_id", None),
                "workspace_id": getattr(event.metadata, "workspace_id", None),
```

Consequence for anyone reading the tests: **`workspace_id` is present but always `None`** until `EventMetadata`
grows a field. Workspace attribution is therefore not real today, and no test may claim otherwise — which is why
the two new tests express it through `data.workspace_id`, and why "workspace scoping of the event log" remains
Gen 44's open work rather than a delivered property.

## Residuals (honest)

- **Workspace attribution does not exist yet.** `workspace_id` is `None` on every real record
  (`event_logger.py:165-169`). These tests pin that workspace is not a *scope*; they cannot pin that records are
  *attributed* to workspaces, because today they are not.
- **Top-level `workspace_id` is untested.** Nothing asserts the behaviour of a record carrying a non-`None`
  top-level `workspace_id`: that shape cannot be produced by the writer yet, so an expectation for it would be a
  guess written into a test. When the writer grows the field, that test is the first thing to add.
- **`container_lifecycle` records** carry attribution only in `data.session_id`
  (`infra/container_lifecycle.py::_build_event`); their tests are pre-existing and were not extended, since they
  have no workspace surface to conflate.
- **Scope of the evidence:** hermetic. `EventLogger.instance` is monkeypatched to a `tmp_path` JSONL file
  throughout, the real vault log is never read, and no server was started.
- No commits made; the worktree is uncommitted at filing.

## Related

- `ticket-bug-checksystem-event-log-cross-session-leak.md` (Gen 47, F17, CRITICAL) — the leak these tests defend.
- `ticket-bug-event-log-global-no-workspace-scope.md` (Gen 44) — writer-side attribution; see "Write-side state".
- `ticket-bug-health-route-event-log-tail-unscoped.md` — the other (now deleted) reader of the same file.
- `ticket-security-workspace-vault-isolation.md` — umbrella.

## MUST NOT TOUCH

Same list as the container tickets.
