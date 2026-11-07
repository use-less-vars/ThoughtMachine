# design/start-unification

`ContainerManager.start()` today has **three semantic cases** overlaid on one
method:

1. **Fresh create** — envelope from session context (gate → `resolve_container_config`).
2. **Rebuild from intent** (resize/restart) — envelope from `record.intent_snapshot`.
3. **Use-path verify** — envelope checked against current session (`verify_container_integrity`).

Cases 1 and 2 use the same `start()` entry point, differing only by
`allow_fresh` and (as of #134) an optional `restored_envelope` flag. This is
why the panel path was able to silently downgrade — same call site, no
distinguishable "this is a rebuild, use the record's intent" signal.

Proposed: two methods, one source each.

- `start_fresh(session_context, ...)` — reads session, gates, creates.
- `rebuild(record, mem_limit=None, ...)` — reads record.intent_snapshot, preserves envelope, updates what was asked.

Not urgent. File for post-Credential architecture pass. Current patch works.
