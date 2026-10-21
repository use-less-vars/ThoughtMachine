# Ticket: feat/dispatch-report-duration

**Filed:** Gen 44, 2026-10-16, operator request.
**Severity:** Medium — measurement is the precondition for planning session length.
**Status:** filed, not dispatched.
**Bearing:** Layer 5 (Delivery) + Layer 6 (Product). Precondition for the
"self-driving session" arc.

## What

Every worker/agent report must state **how long it took to produce**, in a
standard form:

    duration_wall: 205 s
    tool_calls: 326
    tokens: ~72k
    verdict: green | red | partial

## Why

The operator needs to know: how long does a job take an agent? Without this,
we cannot plan:

- **Session length.** "Run overnight" requires knowing whether the next wave
  takes 20 minutes or 4 hours.
- **Budget model.** Which recon strategies are worth the cost? Which flail?
- **Regression detection.** Are we getting faster or slower at the same
  class of work?

Today this is occasionally in the report body (informal, ad hoc), not in the
report schema. It is not aggregated, not comparable, not trendline-able.

## Shape

Two parts:

1. **Discipline (adopt now).** Every dispatch spec adds: "Report
   `duration_wall`, `tool_calls`, `tokens` in the report header." Every
   Infra-to-Main status includes them. No code required.
2. **Feature (dispatch when the current wave closes).** The dispatch
   mechanism auto-stamps every report with `started_at`, `finished_at`,
   `duration_wall`, `tool_calls`, `tokens`. Written into the report file's
   header. The read path (`Worker(...)` response) surfaces it.

## Design questions

- Is the timing owned by the Worker dispatcher, the report serializer, or
  both?
- Should durations be aggregated somewhere (a metrics log) or per-report?
- Do we want per-phase timing (recon vs fix) or per-report?

## MUST NOT TOUCH
Standard list.

## Bearing

Arc 1 (Delivery) — but the deliverable feeds Arc 4 (self-driving sessions).
Small. One dispatch once the funnel branch is quiet.
