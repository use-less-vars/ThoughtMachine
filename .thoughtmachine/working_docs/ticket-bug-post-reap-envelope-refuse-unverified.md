# Ticket: bug/post-reap-envelope-refuse-unverified

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run, unresolved event-log entry #40 (not part of the report's F-series); code verified at `124477b7`
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §Step 9, unnumbered "Entry #40" bullet (lines 292–304) — the report files it outside its F-series, so no §F-label applies.
**Status:** unverified — non-destructive.

## Defect

During the gen-47 run an operator removal of the container `gen47-s3-lifecycle` succeeded (`reap`), and a `refuse` whose reason contained `workspace-owned envelope mismatch` was observed in the same window. It was NOT possible to prove which container that refuse concerns, so this ticket records a falsifiable forward test rather than an observation.

What was ruled out, and why the obvious grep fails:

- `grep '"seq": 40'` over the host `event_log.jsonl` matched only a coincidental `token_update` belonging to an unrelated conversation. `seq` is nested per-conversation, so a bare `"seq": 40` cannot identify a top-level event — a false positive, not evidence.
- `grep -E 'refus|envelope'` over the same log returned only report-writing tool calls, no lifecycle decision.
- No destructive outcome occurred: all three surviving containers were intact with stable ids and `count == 3`.

Code facts that bound the hypothesis (verified, not inferred):

- a destroy routed through the funnel is gated on `decision.destroys`: `restart()` returns `{"status": "refused"}` without touching the container when the gate refuses (`infra/container_manager.py:3593-3597`), and `remove()` routes an explicit reap intent through `ContainerLifecycle.reconcile` (docstring `:4841-4860`);
- the start-path `refuse`/`recreate` split is separate: `_start_drift_decision` returns `deny` or `recreate` and a refused recreate falls through to reuse with a WARNING (`:1715-1735`, `:1759-1764`).

So a `refuse` AFTER a successful `reap` is only interesting if it names the just-removed container: that would mean a second destroy is being scheduled against a stale snapshot (a post-remove sweep holding a handle that no longer resolves), which should no-op loudly on `missing` instead of refusing.

## Forward test (prediction)

On the next operator removal, read the `event_log.jsonl` tail within seconds and run:

`grep -A2 'action.*reap' event_log.jsonl | grep refuse`

The refuse, if real, should surface within 3 entries of the reap. Then branch:

- refuse names the JUST-REMOVED `container_id` → bug: stale post-remove snapshot schedules a second destroy; fix = no-op loudly on `missing`.
- refuse names a DIFFERENT container → normal lifecycle refuse on a use-path, not a bug; close this ticket with that finding.

## Bearing

Blocks nothing. It is the only unresolved observation from the gen-47 run that could not be settled from the log, and it must not be converted into a claim until the forward test resolves the container id.
