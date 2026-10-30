# ticket: `feat/tm-metrics-explorer`

**Filed:** 2026-10-29 (Gen 54 delivery arc)
**Source:** operator request — a standalone page instead of chat-panel-only numbers
**Status:** filed, not started
**Severity:** medium — the machine keeps the numbers but only shows them one turn at a time

## Symptom

Everything the operator wants to know about the machine's own work is either inside a chat
transcript or only in files on disk. There is no page where you can:

- browse the **KB domains** (what the agent actually knows, by domain, and where each domain's
  entry point is);
- see **per-turn durations** (which turns were slow, and how slow);
- see **token usage** per turn / per session, not just the current turn's meter.

Today the only surface is the chat panel mid-conversation, so the moment you want to compare,
or look at a finished session, the numbers are gone from view. The operator asked for this as a
standalone page in the web UI rather than another chat-panel widget.

## Evidence

No such page exists (checked 2026-10-29):

- `web_ui/frontend/src/components/` holds 35 entries — `ChatPanel`, `ConfigPanel`,
  `ContainerPanel`, `GlobalSessions`, `GlobalContainers`, `LoggingPanel`, `VaultHealthPanel`,
  `WorkerManagementPanel`, `PromptLibrary`, `WorkspaceSelector`, ... — **no** KB-browsing
  component and **no** metrics/usage/telemetry component.
- No `/api/kb` or `/api/metrics` route literal appears in `web_ui/backend/server.py`. Routes are
  split across `APIRouter` modules (`workspace_routes.py` → prefix `/api/workspace`;
  `onboarding_routes.py`), so a census of the remaining modules is owed before claiming the
  surface does not exist anywhere — but nothing in the UI consumes such a route.

The data mostly exists already, which is why this is a *surface* ticket and not an
instrumentation ticket:

- Provider-reported usage is already in-process: `prompt_tokens` / `token_usage` appear in
  `agent/core/llm_client.py`, `agent/core/agent.py` and `agent/logging/lifecycle.py`; the
  streaming path surfaces it as a `UsageUpdate` (`project/status.md`, Gen 52 block, Wave 3 —
  "D2 (first tool call) fixed via `UsageUpdate`").
- Durations are measured but not exposed: `project/core.md` **§C6** is *"Per-call /
  per-response TIME DELTAS — the core measures query-start → now"* — and §C6 is a **pending
  core change**, not a shipped capability. Whether a per-turn delta is persisted anywhere
  queryable was not verified from this seat.
- KB structure is documented as files: `project/KB_MAP.md` is the map, and the agent already
  reaches the KB through a knowledge tool whose wrong-path bugs are tracked in
  `ticket-bug-knowledge-tool-reports-wrong-path.md` (4 of 11 sites fixed). The explorer must
  reuse that resolver rather than re-implement KB path lookup.

Related but distinct, read before designing:

- `ticket-feat-dispatch-report-duration.md` — duration reporting into the *dispatch report*.
  Same measurement, different output surface. Do not merge without a ruling.
- `ticket-hygiene-llm-usage-cost-dead-code.md` — if usage/cost accounting is partly dead code,
  there may be **no live source** for the token panel yet. This is the first thing to check.

## Options (needs a ruling; do not implement blind)

1. **Read-only explorer over existing sources.** New `/api/metrics` + `/api/kb/browse` routers
   over (a) KB directory structure via the existing resolver, (b) whatever turn records already
   exist, (c) log-derived durations. Cheapest; risks showing numbers re-derived from timestamps
   rather than the real measurement.
2. **Persist a turn record first, then build the view.** Every turn writes
   `{session, workspace, started_at, duration_ms, prompt_tokens, completion_tokens, tools[],
   kb_domains_read[]}` to a store; the explorer is a pure view over it. The only shape that
   answers cross-session questions and survives a restart. Costs a write path + retention
   policy.
3. **Log-scrape.** Parse `logs/` into the page. Zero write-path change, brittle, and durations
   become inferred rather than measured. Acceptable only as a prototype.
4. **Smallest visible slice now.** Session-level summary only (duration + token totals, no
   per-turn detail), shipped as one panel; per-turn detail and KB browse deferred to a second
   wave. Gives the operator something this generation without committing the data model.

Every option must answer: where per-turn durations come from (§C6 is pending), whether usage is
persisted after the turn, retention/pruning, and **scoping** — every other global-list surface in
this codebase has been filed as a defect for lacking workspace/session scope (see Related), so
this one must ship scoped by construction.

## Acceptance criteria

- One page, reachable without the chat panel, showing: KB domains with an entry point each;
  per-turn durations for a chosen session; token usage per turn plus session totals.
- Answers three questions in one screen: *which turns were slow*, *which turns were expensive*,
  *what KB did this session touch*.
- Explicitly scoped by workspace and session. No unscoped global list anywhere on the page.
- Works offline: reads local store/logs only, no network, and degrades visibly (not silently)
  when a source is missing.
- No second source of truth: the numbers are the same measurement the agent uses, with the
  source named on the page.

## Related

- `ticket-feat-dispatch-report-duration.md` — same measurement, different surface.
- `ticket-hygiene-llm-usage-cost-dead-code.md` — possible absence of a live usage source.
- `ticket-bug-knowledge-tool-reports-wrong-path.md` — the KB resolver to reuse (still buggy).
- `project/core.md` §C6 (time deltas, pending), `project/KB_MAP.md`.
- Scoping precedents to copy from, not repeat: `ticket-bug-event-log-global-no-workspace-scope.md`,
  `ticket-bug-checksystem-event-log-cross-session-leak.md`,
  `ticket-bug-checksystem-running-workers-global-leak.md`.
