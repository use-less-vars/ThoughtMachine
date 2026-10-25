# Ticket: bug/integrity-status-docstring-drift

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit).
**Severity:** MEDIUM
**Status:** open; verified by read at `24012a89` — docstring, funnel decision and pinning test read; `UNRUN: no docker`. Not to be fixed in this wave.

## Finding

`verify_container_integrity` (`infra/containers/executor.py:434`) still opens with an untrue sentence (:440-442):

> If permissions have been tightened since the container was created, the container is stopped and removed so it will be recreated with the new settings on next use.

Nothing stops or removes a container on a permissions-tightening mismatch. The branch hands the decision to the funnel with a hard-coded workspace-owned intent (`executor.py:572-583`: `kind="use"`, `workspace_owned=True`, `reason="verify_integrity_mismatch"`), and `_decide` step (10) (`infra/container_lifecycle.py:298-304`) answers `ReconcileDecision("refuse", "workspace-owned envelope mismatch; refusing to destroy", dsource)`. `decision.destroys` (`action == "reap"`) is therefore false, the destructive block at `executor.py:585-624` is unreachable from this call site, and `:625-632` sets `action_taken = "refused"` after logging "refused (container left intact)". The comment above the hand-off (:562-565) says so.

The behaviour is pinned by `tests/test_container_lifecycle_funnel.py:374`, `test_get_integrity_status_mismatch_is_read_only`: `result["status"] == "mismatch"`, `stop_calls == []`, `remove_calls == []`; its sibling at `:389` asserts `action_taken == "refused"`.

`get_integrity_status` (`executor.py:1618`) is not the drifting site; its docstring (:1624-1635) already separates `"removed"` from a refused mismatch reporting `"mismatch"`. The claim is not single: `web_ui/backend/bridge.py:1752-1756` (`_maybe_re_sync_container` docstring, "…is stopped and removed. The container will be recreated…") and `:2465-2466` (the `load_session` comment, "it is stopped and removed (recreated on next use)") repeat it, and that helper's `action_taken == 'removed'` log branch (`:1767`) is unreachable too.

Caller-supplied `permissions` JSON does reach the funnel as its `session` argument (`executor.py:574`) but is inert — `_desired_envelope` (`container_lifecycle.py:314-333`) prefers the intent fields, which the verify path always sets (:577-578).

## Bearing

It is what a maintainer reads before touching the destroy block, and it contradicts both the code beside it and the test that guards it. Read at face value, tightening permissions looks self-remediating: drift detected, container recycled, new envelope applied. The caller instead gets `status: "mismatch"` with a populated `mismatch_reason`, and the container keeps the old, wider envelope (bridge/rw where `none`/ro is now wanted) until something else reaps it. Remediation is manual; the prose hides that.

Tag: `verification-written-by-author` — code, docstring and pinning test written in the same pass, the prose never re-read against the test once the funnel was made non-destructive.

## Blast radius

Documentation only. Nothing reads the docstring at runtime; no control flow, response or container state depends on it. It can move a human decision: a maintainer assuming auto-recycle and skipping manual remediation, or a reviewer approving against stated behaviour rather than tested behaviour. The exposure is stale container state that is already the safe outcome; the error is believing it was cleaned up.

## What this is not

- Not a live container-destruction risk: no path here stops or removes anything; the destructive block is unreachable from this call site.
- Not exploitable: prose is not reachable input, and the `permissions` JSON reaching the funnel as `session` cannot flip `refuse` to `reap`.
- Not a funnel behaviour change: nothing here proposes touching `_decide`, `reconcile` or `apply`.
- Not a runtime defect: code and test agree, only the prose disagrees. `UNRUN: no docker`.
- Not the same row as `ticket-hygiene-executor-dead-code.md` item 3, which names this sentence inside a parked dead-code list; this ticket owns the prose contradiction and the two `bridge.py` sites.

## Fix shape

Rewrite the three sites above to match behaviour: on a workspace-owned mismatch the funnel refuses, the container is left intact, the caller reports `status: "mismatch"` with `mismatch_reason`; destruction happens only on an explicit, orphaned or TTL reap, not on drift. Optionally pin the claim with an assertion in `tests/test_container_lifecycle_funnel.py`.
