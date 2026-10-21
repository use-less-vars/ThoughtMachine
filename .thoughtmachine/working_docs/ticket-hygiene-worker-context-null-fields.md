# hygiene/worker-context-null-fields

`worker/*/context.json` exists in every worker dir but has all fields `null`:
`state`, `spawned_at`, `stopped_at`, `stop_reason`, `tool_calls_made` — only
`last_heartbeat` is populated.

**Impact:** cannot diagnose worker stops from on-disk state. `dmesg`/PSI are
workarounds, but the worker's *own* record of its stop is empty.

**Fix:** populate `state`, `spawned_at`, `stopped_at`, `stop_reason`,
`tool_calls_made` at the appropriate lifecycle points. `stop_reason` should be
one of `{soft_timeout, hard_timeout, force_killed, completed, error, user_stop,
workspace_closed}`.

**Owner:** worker lifecycle. Do after `worker-internal-time-budget` so the
stop reason values are meaningful.

**Filed by:** Gen 46, 2026-10-19.
