# Structured Logging Guide

This document describes the structured JSONL logging subsystem: where logs live,
the streams that exist, the common envelope, rotation and redaction policy, and
how to query the logs with the `tm-logs` CLI. Everything here is grounded in the
implementation (`agent/logging/*`, `agent/cli/logs.py`, `agent/core/llm_client.py`,
`pyproject.toml`).

## 1. Canonical log root

All structured logs live in the **canonical vault log directory**:

```
~/.thoughtmachine/logs                    (default)
$THOUGHTMACHINE_VAULT_ROOT/logs           (when the env var is set)
```

Resolution rules differ slightly by module — read carefully:

| Module | Resolution | When |
|---|---|---|
| `agent/logging/lifecycle.py` | `LOG_DIR` computed at **import time** from `THOUGHTMACHINE_VAULT_ROOT` (fallback `~/.thoughtmachine/logs`) | import |
| `agent/logging/event_logger.py` | `log_dir = ~/.thoughtmachine/logs` via `expanduser` — **ignores `THOUGHTMACHINE_VAULT_ROOT`** | `EventLogger.__init__` |
| `agent/cli/logs.py` | `_log_root()` resolved at **runtime** from `THOUGHTMACHINE_VAULT_ROOT` (fallback `~/.thoughtmachine/logs`) | each invocation |

Practical consequences:

- In a fresh process with `THOUGHTMACHINE_VAULT_ROOT` set, everything lands under
  `$THOUGHTMACHINE_VAULT_ROOT/logs` (lifecycle streams honor it at import time and
  `tm-logs` honors it at run time).
- If you set the variable *after* `agent.logging.lifecycle` was imported, lifecycle
  streams still use the old `LOG_DIR` for the rest of the process.
- `EventLogger` always writes to `~/.thoughtmachine/logs` regardless of the env var.

### The codebase never writes logs into the repo

All sinks target the vault log directory above. A `./logs` directory at the repo
root is **host-runtime telemetry only** (`agent_*.jsonl` from the host harness) and
is gitignored (`logs/`, .gitignore line 23). Application code must never create or
write log files under the repository tree — there is a hermeticity test
(`tests/test_logging_foundation.py::TestHermeticity`) that fails if lifecycle or
EventLogger activity creates anything new at the repo root.

## 2. The five JSONL streams

All files are append-only JSON Lines (one JSON object per line, UTF-8,
`ensure_ascii=False`).

### 2.1 `event_log.jsonl` — EventBus persistence (`agent/logging/event_logger.py`)

`EventLogger` (singleton via `EventLogger.instance()`) subscribes to **every**
`EventType` on the (global) `EventBus` (`subscribe_all`) and writes each published
`BaseEvent` to `event_log.jsonl` through a background writer thread (queue-drained,
daemon; `stop()` joins with a 5 s timeout and drains the remainder).

Record schema — **differs from the lifecycle envelope**:

```json
{
  "timestamp": "2025-01-01T12:34:56.789012",
  "event_type": "llm_response",
  "event_id": "evt_...",
  "source": "unknown",
  "data": {}
}
```

- `event_type` is the `EventType` enum value (`agent/events.py`), e.g.
  `llm_response`, `tool_call`, `session_started`-style values from the enum.
- `timestamp` is `EventMetadata.timestamp.isoformat()` — a **naive** ISO-8601
  timestamp (no `Z`, no offset), because `EventMetadata.timestamp` defaults to
  `datetime.now()`. This is a documented deviation from the lifecycle envelope
  (see `docs/decisions/logging-architecture.md`).
- `metadata.session_id` / `metadata.turn` exist on events but are **not** persisted
  into the record (only `event_id` and `source` are copied from metadata).
- The serialized line is passed through `redact()` before hitting disk.
- Rotation: 5 MB threshold, exactly one backup (`event_log.jsonl.1`).
- `get_tail(n=20)` returns the last *n* parsed records (undecodable lines are
  returned as `{"raw": "<line>"}`).

### 2.2 Lifecycle streams (`agent/logging/lifecycle.py`)

Four streams share one writer (`agent/logging/streams.py::JsonlStreamWriter`) and
one **envelope** (see §3). All lifecycle functions are best-effort and **never
raise**.

| Stream | Function | Record payload (beyond the envelope) |
|---|---|---|
| `session.log` | `log_session_event(event_type, *, session_id, workspace_id, data)` | `event`, `stream: "session"`, `session_id`, `workspace_id`, `data` |
| `worker_<safe_name>.log` | `log_worker_event(worker_name, event_type, *, session_id, worker_id, data)` | `event`, `stream: "worker"`, `worker_name`, `worker_id`, `session_id`, `data` |
| `container.log` | `log_container_event(event_type, *, container_id, session_id, workspace_id, data)` | `event`, `stream: "container"`, `container_id`, `session_id`, `workspace_id`, `data` |
| `provider_raw.jsonl` | `log_provider_event(*, content, model_name, request_id, token_usage, latency, finish_reason, stop_reason, tool_call_count, temperature, session_id, worker_id, query_id, correlation_id, container_id)` | see §5 |

Worker stream filenames are sanitized: `_safe_name` replaces every character not
in `[A-Za-z0-9_.-]` with `_` (e.g. `worker "My Worker!"` → `worker_My_Worker_.log`).

## 3. The lifecycle envelope

`JsonlStreamWriter.write(record)` injects the following fields via `setdefault`
(caller-supplied values win):

```json
{
  "timestamp":      "2025-01-01T12:34:56.789Z",
  "level":          "INFO",
  "logger":         "thoughtmachine.lifecycle",
  "pid":            12345,
  "thread_id":      140123456789,
  "session_id":     "",
  "worker_id":      "",
  "query_id":       "",
  "correlation_id": "",
  "container_id":   ""
}
```

- `timestamp` is UTC with millisecond precision and a `Z` suffix
  (`datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")`).
- `level` defaults to `INFO`; `logger` defaults to `thoughtmachine.lifecycle`.
- The correlation ids (`session_id`, `worker_id`, `query_id`, `correlation_id`,
  `container_id`) default to `""` and are filled in by the caller where known
  (e.g. `log_provider_event` propagates all five).

## 4. Rotation policy

- **Size-based only** — there is no time-based rotation.
- Threshold: **5 MB per file** (`DEFAULT_MAX_BYTES = 5 * 1024 * 1024`).
  `JsonlStreamWriter` rotates *before* the write when the current file size
  reaches the threshold (`>= max_bytes`); `EventLogger` rotates when the file
  exceeds 5 MB (`>`).
- Backups: **exactly one** — `path` → `path.1` via `os.replace`, any previous
  `path.1` is removed first (`keep_backups=1`, `DEFAULT_KEEP_BACKUPS = 1`).
  `EventLogger` rotates `event_log.jsonl` → `event_log.jsonl.1`.
- The rotation+write sequence in `JsonlStreamWriter` happens under a reentrant
  lock; `EventLogger` rotates in its single writer thread (serialized by the
  event queue), so concurrent writers cannot interleave.
- Disk bound: at most **~10 MB per stream** (5 MB main + 5 MB backup, plus at
  most one record line in the worst case).
- Data older than the retained window is discarded by design: with
  `keep_backups=1` the previous `.1` backup is overwritten at the next rotation.
  Surviving records across `path` + `path.1` are always a contiguous, ordered
  suffix of the written sequence (no gaps or duplicates within the window).

## 5. `provider_raw.jsonl` — provider response records

Written by `log_provider_event` (`agent/logging/lifecycle.py`), called from
`LLMClient._log_provider_response` (`agent/core/llm_client.py`) after every
**successful** `chat_completion` (provider errors are wrapped into `LLMError` and
are not logged here).

Record shape (all fields optional except the core ones):

```json
{
  "event": "provider_response",
  "stream": "provider",
  "tool_call_count": 2,
  "content_preview": "The first 500 characters of the response content...",
  "content_empty": false,
  "empty_content": false,
  "session_id": "sess-1",
  "worker_id": "",
  "query_id": "",
  "correlation_id": "",
  "container_id": "",
  "model_name": "gpt-4o",
  "request_id": "chatcmpl-...",
  "token_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
  "latency": 1.234,
  "finish_reason": "stop",
  "stop_reason": "end_turn",
  "temperature": 0.7
}
```

Semantics:

- `content_preview` — the raw provider text truncated to **500 characters**
  (`raw[:500]`); `content_empty` and `empty_content` are both `true` when the
  content is empty. **Prompts / full conversation context are never dumped** into
  this stream.
- `model_name` — `response.model`, falling back to `config.model` when the
  response reports `"unknown"`.
- `request_id` / `finish_reason` / `stop_reason` — extracted defensively from the
  provider's `raw_response` (OpenAI-style `raw.id` / `raw.choices[0].finish_reason`,
  Anthropic-style `raw.stop_reason`).
- `token_usage` — `response.usage` serialized via `model_dump()`/`vars()` when
  available.
- `latency` — wall-clock seconds of the completion call.
- `temperature` — from the call kwargs, falling back to `config.temperature`.
- `session_id` — propagated from `self.session.session_id` when a session is
  attached; the other correlation ids default to `""`.
- Optional fields (`model_name`, `request_id`, `token_usage`, `latency`,
  `finish_reason`, `stop_reason`, `temperature`) are **omitted** from the record
  when absent/empty.

The **whole serialized line** is passed through `redact()` before writing
(`redact_line=True`), so secrets embedded in the content preview never reach disk.

## 6. Redaction

`agent/logging/redaction.py::redact(text)` is the central secret-redaction
utility. It is applied to **every sink that can carry free-form text**:

- `provider_raw.jsonl` — whole-line redaction (`redact_line=True`).
- `event_log.jsonl` — whole-line redaction in the writer thread.
- (The lifecycle event streams `session.log` / `worker_*.log` / `container.log`
  are **not** line-redacted: their `data` payloads are caller-supplied, so
  callers must not place secrets there.)

Patterns (in order, first match wins per position; group 1 is preserved as a
recognizable prefix):

1. OpenAI-style keys: `sk-`, `sk-or-`, `sk-ant-` + ≥8 token chars → `sk-<REDACTED>`
2. GitHub PATs: `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`gh2_` + ≥20 chars → `ghp_<REDACTED>`
3. `Authorization: Bearer <token>` → `Bearer <REDACTED>`
4. Bare `Bearer <token>` → `Bearer <REDACTED>`
5. AWS access key ids: `AKIA` + 16 chars → `AKIA<REDACTED>`
6. `key=value` / `"key": value` / `key: value` pairs for known secret names
   (`api_key`, `apikey`, `access_token`, `auth_token`, `secret`, `password`,
   `passwd`), **case-insensitive**, quoted or bare → `<REDACTED>`
7. PEM private-key blocks (RSA/EC/OpenSSH, `-----BEGIN ... PRIVATE KEY-----`
   through `-----END ... PRIVATE KEY-----`, DOTALL) → `<REDACTED>`

Properties:

- `redact()` **never raises** — non-`str` inputs are coerced via `str()`, and any
  unexpected failure returns the original text unchanged.
- Redaction is JSON-safe: the output remains parseable JSON (the replacement
  `"<REDACTED>"` cannot break string quoting).
- Short lookalike tokens are untouched (`sk-abc`, `ghp_abc`) to avoid
  false positives.

## 7. Console logging (`agent/logging/console.py`)

Human-readable console output uses the **stdlib `logging`** hierarchy:

- `configure_console_logging(level=None)` installs a single `StreamHandler` on
  the `thoughtmachine` logger → **stderr**.
- Format: `%(asctime)s %(levelname)-8s %(name)s: %(message)s` with `HH:MM:SS`
  timestamps.
- Level resolution: explicit `level` argument → `TM_LOG_CONSOLE_LEVEL` env var →
  `WARNING`.
- Idempotent (installs once per process).
- Lifecycle functions emit concise, **secret-free summary lines** through the
  `thoughtmachine.lifecycle` logger (e.g.
  `session session_started session_id=sess-1`,
  `provider response model=gpt-4o request_id=- tool_calls=0 empty=False`).
- Deliberately does **not** touch the legacy py-logger file handlers
  (`agent_<session>` loggers keep their `NullHandler`) nor the print-based
  presenter console (`agent/presenters/unified.py`).

## 8. `tm-logs` CLI

Entry point: `tm-logs = "agent.cli.logs:main"` (`pyproject.toml` `[project.scripts]`);
also runnable as `python -m agent.cli.logs`.

```
tm-logs session [FILTERS] [--format table|json|human]
tm-logs worker --worker-name NAME [FILTERS] [--format ...]
tm-logs container [FILTERS] [--format ...]
tm-logs stop-reasons [FILTERS] [--stop-reason REASON] [--format ...]
```

Shared filters (all subcommands): `--since ISO8601`, `--until ISO8601`
(naive values assumed UTC; records with unparseable timestamps are excluded when
a time filter is present), `--session-id ID` (exact), `--level LEVEL` and
`--event-type TYPE` (case-insensitive). `--format` is `table|json|human`
(default `human`).

Subcommand-specific arguments (rejected with exit code 2 anywhere else — never
silently ignored):

- `--worker-name NAME` — only on `worker`, required; selects
  `worker_<safe name>.log`.
- `--stop-reason REASON` — only on `stop-reasons`; counts only records whose
  `finish_reason` or `stop_reason` equals the (lowercased) reason.

Exit codes:

- `0` — success, **including a missing stream file** (a friendly message is
  printed to stderr and the CLI exits 0 so scripts do not break before a stream
  has been written; malformed lines are skipped and reported on stderr).
- `1` — the stream file exists but cannot be read.
- `2` — argparse error (unknown subcommand, bad `--since/--until` ISO-8601,
  filter on the wrong subcommand, missing `--worker-name`).

`stop-reasons` output: JSON prints
`{"finish_reason": {"stop": 5, "length": 1}, "stop_reason": {"max_tokens": 1}, "total": 6}`;
table/human print rows sorted by count desc then name, plus a `TOTAL` row.
Empty results print `[]` (JSON) or `(no matching records)` (table/human).

Examples:

```bash
tm-logs session --format table
tm-logs session --since 2025-01-01T00:00:00Z --event-type session_started --format json
tm-logs worker --worker-name engineer --level INFO
tm-logs container --session-id sess-1
tm-logs stop-reasons --stop-reason max_tokens --format json
```
