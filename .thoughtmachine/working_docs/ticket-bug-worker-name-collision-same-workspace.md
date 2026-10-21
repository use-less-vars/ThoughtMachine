# bug/worker-name-collision-same-workspace

**Confirmed 2026-10-19 by field agent in fierce-owl-43:** two sessions in the
same workspace, both spawning worker named `default` with `force=True`, kill
each other's worker. Field agent worked around it by switching to `default2`.

**Open question:** does the same happen cross-workspace? Registry keying unknown.
If keyed by `(workspace_id, worker_name)`, no cross-workspace bug. If keyed by
`worker_name` alone, cross-workspace kill is live.

**Read first:**
1. Where is the worker registry? What's the key?
2. `WorkerThread.spawn(force=True)` — filter by workspace_id or not?
3. Test both scenarios.

**Fix shape:** registry keyed by `(workspace_id, worker_name)`. Same-workspace
same-name across sessions should either refuse or scope by `session_id` too.

**Filed by:** Gen 46, 2026-10-19.
