# ADR: Structured Logging Architecture

- **Status:** accepted (implemented)
- **Date:** 2025
- **Deciders:** platform engineering
- **Technical story:** structured-logging feature (Chunks 1–6)

## Context

The agent runtime needs durable, queryable, machine-readable logs of lifecycle
events (sessions, workers, containers), provider interactions, and bus events.
The pre-existing `_AgentLogger` (`agent/logging/__init__.py`) writes verbose
verbatim-conversation files (`agent_<session>.jsonl`) under
`~/.thoughtmachine/logs` and is oriented toward replay/debugging rather than
operational querying; it is left in place and out of scope.

Requirements that shaped this decision:

1. Logs must live in a **canonical, predictable location** that is independent
   of the working directory and of ephemeral containers.
2. Records must be **structured** (JSONL) so they can be filtered and aggregated
   by tooling.
3. Secrets must **never reach disk** — every sink that can carry free-form text
   must pass through a central redaction utility.
4. Disk usage must be **bounded and predictable** (size-based rotation, few
   backups).
5. Operators need a **read-only query CLI** with filters and multiple output
   formats.
6. Logging must never break the caller (all public logging entry points are
   best-effort and never raise).

## Decision

### Canonical vault root

All structured logs are written under the canonical vault log directory:

- `~/.thoughtmachine/logs` by default;
- `$THOUGHTMACHINE_VAULT_ROOT/logs` when the variable is set.

The repository tree is **never** used for application logs; `./logs` at the repo
root is host-runtime telemetry only (gitignored). This rule is enforced by a
hermeticity test.

### Five JSONL streams

| Stream | Writer | Purpose |
|---|---|---|
| `event_log.jsonl` | `EventLogger` (EventBus subscription, async writer thread) | persistence of every typed bus event (`event_type`, `event_id`, `source`, `data`) |
| `session.log` | `log_session_event` | controller/session lifecycle events |
| `worker_<safe>.log` | `log_worker_event` | per-worker lifecycle events |
| `container.log` | `log_container_event` | container lifecycle events |
| `provider_raw.jsonl` | `log_provider_event` (wired in `LLMClient._log_provider_response`) | one record per successful provider completion: model, request id, token usage, latency, finish/stop reason, tool call count, temperature, 500-char content preview |

The four lifecycle streams share a common envelope (`timestamp` UTC ms `Z`,
`level`, `logger`, `pid`, `thread_id`, and `session_id` / `worker_id` /
`query_id` / `correlation_id` / `container_id` correlation ids). `event_log.jsonl`
deliberately deviates (see Consequences).

### Rotation: 5 MB, exactly one backup

- Size-based only; no time-based rotation.
- Threshold 5 MB per file (`DEFAULT_MAX_BYTES`); current file becomes `path.1`
  via `os.replace`, previous `path.1` removed first (`keep_backups=1`).
- Rotation happens before the write that crosses the threshold
  (`JsonlStreamWriter`) under the writer's reentrant lock; `EventLogger` rotates
  in its single writer thread.
- Bound: at most ~10 MB on disk per stream (5 MB main + 5 MB backup).

### Central redaction

`agent/logging/redaction.py::redact()` is the single redaction utility applied to
every sink that can carry free-form text: `provider_raw.jsonl`, `event_log.jsonl`,
and all lifecycle streams (`session.log`, `worker_*.log`, `container.log`) — each
is redacted whole-line at write time (`JsonlStreamWriter.write()` defaults
`redact_line=True`). Patterns: OpenAI `sk-*`, GitHub PATs
(`gh[pousr2]_*`), `Bearer` tokens, AWS `AKIA` access key ids, case-insensitive
`key=value` pairs for known secret names, and PEM private-key blocks. `redact()`
never raises and is JSON-safe.

### Console: stdlib logging

`agent/logging/console.py` installs one `StreamHandler` on the `thoughtmachine`
logger (stderr, compact human format, level from `TM_LOG_CONSOLE_LEVEL` else
`WARNING`). Lifecycle functions emit short, secret-free summary lines through
`thoughtmachine.lifecycle`. The legacy `agent_<session>` py-loggers keep their
`NullHandler` and the print-based presenter console is untouched.

### Query CLI: `tm-logs`

`tm-logs` (`agent/cli/logs.py`, console script in `pyproject.toml`) reads the
streams with subcommands `session`, `worker`, `container`, `stop-reasons`,
shared filters (`--since`, `--until`, `--session-id`, `--level`, `--event-type`),
formats `table|json|human`, and deliberate exit-code semantics: `0` for a missing
stream file (script-friendly), `2` for bad arguments, `1` for unreadable files.

## Alternatives considered

1. **Repo-local `./logs` root.** Rejected: pollutes the workspace, is
   gitignored-but-present (confuses tooling), does not survive container
   teardown, and mixes host telemetry with application logs. The vault root is
   already the established location (`_AgentLogger` and `EventLogger` both use
   `~/.thoughtmachine/logs`).
2. **Time-based rotation (hourly/daily).** Rejected: lifecycle events are
   irregular, so time-based rotation produces unevenly sized files; size-based
   rotation gives a hard, predictable disk bound without a scheduler.
3. **Multiple backups (`keep_backups > 1`).** Rejected: unbounded-ish disk
   growth and added tooling complexity. The rotation logic is explicitly
   single-backup (`path.1` only); this is a documented limitation, not a bug.
4. **Unredacted sinks.** Rejected outright: provider responses and bus event
   payloads routinely contain free-form text; writing them verbatim would put
   secrets on disk. Whole-line redaction costs little and is strictly safer.
5. **A single all-purpose stream.** Rejected: separate streams give operators
   narrow, purpose-built files (session vs. worker vs. provider) and let
   `tm-logs` query each with columns/filters tailored to it.

## Consequences

### Positive

- Predictable location and bounded disk usage (~10 MB max per stream).
- All secrets pass through one redaction utility; provider, event, and lifecycle
  streams are redacted at the line level by default.
- Operators can query logs with `tm-logs` without touching the files directly.
- Logging is best-effort everywhere: a logging failure can never crash the
  caller.

### Trade-offs / known deviations (accepted)

- **Log root is resolved dynamically, never at import time**: one shared
  `agent._log_root.get_log_root()` serves lifecycle, EventLogger, `_AgentLogger`,
  and `tm-logs`; setting `THOUGHTMACHINE_VAULT_ROOT` mid-process takes effect at
  the next writer open / CLI invocation. Writers already opened under a previous
  root stay there until closed. The `EventLogger` constructor still accepts
  `workspace_path` but the implementation does not use it.
- **`event_log.jsonl` schema deviation**: records carry `timestamp` (naive
  ISO-8601, no `Z`/offset — from `EventMetadata.timestamp = datetime.now()`),
  `event_type`, `event_id`, `source`, `data`, and **no** envelope
  (`level`/`logger`/`pid`/`thread_id`/correlation ids). `metadata.session_id`
  and `metadata.turn` are not persisted. Consumers must not assume the lifecycle
  envelope here.
- **`keep_backups > 1` is not supported** by the rotation logic: the previous
  `.1` backup is overwritten at each rotation. With `keep_backups=1` the total
  retained data is bounded by 2 × max_bytes; records beyond that window are
  discarded by design (surviving records across `path` + `path.1` are always a
  contiguous suffix of the written sequence).
- **`provider_raw.jsonl` covers successful completions only**: provider errors
  are raised as `LLMError` before `_log_provider_response` runs, so error
  diagnostics do not appear in this stream.
- **Redaction trade-offs**: short lookalike tokens (`sk-abc`, `ghp_abc`) are
  deliberately left untouched to avoid false positives; redaction is a
  best-effort heuristic, not a security boundary.
