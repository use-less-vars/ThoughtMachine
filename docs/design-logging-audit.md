# Logging & audit sinks — inventory note

**Status:** draft inventory for Wave 0 — deletes nothing. **Date:** 2026-10-27.

This note *enumerates* the logging/audit sinks in the tree; it does not preserve any of
them. Every statement below is tied to `file:line`. Where the code does not establish a
fact, the text says **"not established from code"** instead of guessing.

Scope: the three sinks named by the operator ruling —
(a) `EventLogger`, (b) `agent.logging`, (c) `thoughtmachine/audit_logger.py`.
Additional file writers discovered while auditing are listed in §3.4 as inventory gaps;
they are *enumerated*, not audited.

All three resolve their directory through one helper, so they share a root by default:

```python
# agent/_log_root.py:29-32
    override = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    if override:
        return Path(override) / "logs"
    return Path(os.path.expanduser("~")) / ".thoughtmachine" / "logs"
```

---

## 1. The three sinks

### 1.1 Sink (a) — `EventLogger` (`agent/logging/event_logger.py`)

**Owns:** the `EventBus` → single JSONL file bridge. Constructor fixes the target and
takes the bus:

```python
# agent/logging/event_logger.py:45-58
        self.event_bus = event_bus or global_event_bus
        ...
        self._event_queue: queue.Queue = queue.Queue()
        ...
        # Determine log directory and file path — always use vault path
        vault_log_dir = str(get_log_root())
        self.log_dir = vault_log_dir
        self._file_path = os.path.join(vault_log_dir, "event_log.jsonl")
```

`start()` opens the file, spawns a daemon writer thread (async queue, `:49`) and
subscribes to **every** `EventType`:

```python
# agent/logging/event_logger.py:109-113
        self._ensure_file_open()
        self._stop_requested.clear()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
        self.subscribe_all(self.event_bus)
```

Record shape is fixed in `_on_event` (`:145-171`): `timestamp`, `event_type`,
`event_id`, `source`, `session_id`, `workspace_id`, `data`. `workspace_id` is always
`None` today, by its own admission (`:165-167`: *"``workspace_id`` is always None for now
-- EventMetadata has no such field today"*).

**Writes:** `<log_root>/event_log.jsonl`, UTF-8, append mode (`:75`), one JSON object per
line (`:186`). Rotation is size-only, 5 MB, exactly one backup — and the old backup is
deleted first:

```python
# agent/logging/event_logger.py:81-86
        if os.path.getsize(self._file_path) > 5 * 1024 * 1024:
            # Rotate: current -> .1 (keep exactly one backup)
            backup = os.path.join(self.log_dir, "event_log.jsonl.1")
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(self._file_path, backup)
```

**Explicitly does not own:** console output (no `print` anywhere in the module);
per-session files (the file is vault-global, `:162-164`: *"The event log is ONE
vault-global file shared by every session"* — that comment lives in the reader, see
below); age-based retention (nothing in this module deletes by mtime); workspace scoping.

**Who reads it (process):**
- `tools/workspace/check_system.py:1088` `_query_event_log()` — the agent-facing
  `event_log` query (`:371`). It is session-scoped and refuses to serve the global tail:
  `:1102-1105` *"No session scope on this call; refusing to return the global event
  log."* It reads a widened 500-line tail (`:1123`) and masks message bodies for display
  only (`_redact_event_body`, `:1058-1086`).
- `web_ui/backend/health_routes.py:214` `entries = EventLogger.instance().get_tail(20)`,
  returned as `recent_event_log_tail` (`:228-231`).

**Who starts/stops it:** `web_ui/backend/server.py:745-746` (`EventLogger()` /
`.start()`), stopped at `:1060-1062`; per-worker buses attach at
`tools/workspace/worker_thread.py:2270` (`attach_worker_bus`).

**Who deletes it:** nobody in (a). But see §3.1 — sink (b)'s constructor deletes *any*
file in the same directory older than 7 days, so `event_log.jsonl` is deleted as
collateral, not by design. Otherwise the file grows to 5 MB and is bounded at ~10 MB
(current + one backup) forever.

**CLI reader:** none. `tm-logs` serves only the lifecycle streams
(`agent/cli/logs.py:69-74`: `session.log`, `container.log`, `worker`-derived name,
`provider_raw.jsonl`). Whether that omission is intentional is **not established from
code**.

---

### 1.2 Sink (b) — `agent.logging` (`_AgentLogger` + `unified.log` facade)

**Owns:** two distinct outputs under one API — a per-session JSONL file and the console
line.

File side (`agent/logging/__init__.py`): directory defaults to the shared root, file name
is per session, mode is append:

```python
# agent/logging/__init__.py:127-129
        if log_dir is None:
            log_dir = str(get_log_root())
        self.log_dir = os.path.abspath(log_dir)
# agent/logging/__init__.py:159
        self.log_file_path = os.path.join(self.log_dir, f"agent_{self.session_id}.{('jsonl' if jsonl_format else 'log')}")
```

Envelope fields are stamped at write time (`:232-234`: `timestamp`, `session_id`,
`version = CURRENT_SESSION_VERSION`). Ingress filters: category
(`_should_log_event`, `:214-219`, table `EVENT_TYPE_TO_CATEGORY` `:96`, categories from
`config.log_categories` overridable by `AGENT_LOG_CATEGORIES`, `:121-126`) and level
(`_should_log_to_file`, `:207-212`).

Rotation: at 10 MB (default `max_file_size_mb=10`, `:106` / `create_logger` `:734`) the
file is renamed with a timestamp and reopened, then the directory is pruned:

```python
# agent/logging/__init__.py:245-251
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        archived_path = f'{self.log_file_path}.{timestamp}'
                        if os.path.exists(self.log_file_path):
                            os.replace(self.log_file_path, archived_path)
                        self._file_handle = open(self.log_file_path, 'a', encoding='utf-8')
                        self._current_file_size = 0
                        _prune_logs_by_size(self.log_dir)
```

Retention is owned here, by two helpers, both called from `__init__` (`:157-158`):

```python
# agent/logging/__init__.py:635-650
def _cleanup_old_logs(log_dir: str) -> None:
    """Remove log files older than TM_LOG_MAX_AGE_DAYS (default: 7)."""
    ...
        max_age_days = int(os.environ.get('TM_LOG_MAX_AGE_DAYS', '7'))
    ...
        for fname in os.listdir(log_dir):
            fpath = os.path.join(log_dir, fname)
            if os.path.isfile(fpath):
                mtime = os.path.getmtime(fpath)
                age_days = (now.timestamp() - mtime) / 86400
                if age_days > max_age_days:
                    os.remove(fpath)
```

```python
# agent/logging/__init__.py:676-680 (name-restricted, unlike the helper above)
        log_patterns = ('agent_*.jsonl', 'agent_*.jsonl.*', 'agent_*.log', 'agent_*.log.*')
        ...
            if not any(fnmatch.fnmatch(fname, pat) for pat in log_patterns):
                continue
```

`_prune_logs_by_size` budget: `TM_LOG_DIR_MAX_MB` default 50 (`:668`), oldest-first
deletion (`:697-703`).

Console side (`agent/logging/unified.py`): `log()` decides console visibility
(`:530`), formats, and prints to stderr with **no redaction call**:

```python
# agent/logging/unified.py:538-548
        console_msg = f"{color}{timestamp} {level_str:8s} {tag_str} {message}{RESET}"
        if truncated_data:
            # Format data for console (truncate further if needed)
            data_str = str(truncated_data)
            console_limit = get_limit_for_hint(truncate_hint)
            if len(data_str) > console_limit:
                data_str = _truncate_string(data_str, console_limit)
            console_msg += f" | {data_str}"

        try:
            print(console_msg, file=sys.stderr)
```

The same call forwards to the file logger (`:580-586`, `logger._log_event(...)`).

**Explicitly does not own:** the event bus (no `EventBus` import in `__init__.py`);
`event_log.jsonl`; the audit log; structured *audit* semantics — the file is a session
transcript ("Records verbatim conversation between agent and LLM", module docstring
`:2-3`), not an accountability record.

**Who reads it:** **not established from code.** No production reader of
`agent_*.jsonl` was found; `tm-logs` deliberately maps only the lifecycle streams
(`agent/cli/logs.py:69-74`); `check_system` has no `agent log` query (query list at
`tools/workspace/check_system.py:369-373`). The file is write-only from the codebase's
point of view; humans `tail` it.

**Who deletes it:** itself (`_cleanup_old_logs` by age, `_prune_logs_by_size` by total
size), on the next `_AgentLogger` construction — i.e. deletion is triggered by the
*next* logger, not by a background janitor.

**Console knob = the GUI control.** The only logging control the web UI exposes changes
console filtering/truncation; it cannot touch file content or redaction:

```python
# web_ui/backend/logging_routes.py:25-31
router = APIRouter(prefix="/api/logging")

class LoggingConfigBody(BaseModel):
    level: Optional[str] = None
    tags: Optional[str] = None
    truncation_limits: Optional[Dict[str, int]] = None
    session_id: Optional[str] = None
```

(`PUT /api/logging/config` → `set_log_level` / `set_log_tags` /
`set_truncation_limits`, `:76-86`.)

---

### 1.3 Sink (c) — `thoughtmachine/audit_logger.py`

**Owns:** one plaintext, append-only audit line per event, through a module-level
`RotatingFileHandler`. Target resolution:

```python
# thoughtmachine/audit_logger.py:37-40
_ENV_LOG_PATH = "CONTAINER_AUDIT_LOG_PATH"
_DEFAULT_LOG_NAME = "container_audit.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 3
```

Format is pipe-delimited text, not JSON, and the write bypasses the logging API:

```python
# thoughtmachine/audit_logger.py:150-157
    line = f"{time.time()} | {event_type} | {details}\n"
    try:
        handler = _get_handler()
        if handler is None:
            return
        handler.terminator = ""  # we include \n in the line ourselves
        handler.stream.write(line)
        handler.flush()
```

Path: `$CONTAINER_AUDIT_LOG_PATH`, else `<log_root>/container_audit.log`
(`:69-74`, `:49-66`). Rotation: `RotatingFileHandler(maxBytes=_MAX_BYTES,
backupCount=_BACKUP_COUNT, ...)` (`:112-117`) → 10 MB × 3 backups.
Failure is sticky and reported **through sink (b)**:

```python
# thoughtmachine/audit_logger.py:83-86
        from agent.logging import log

        log("WARNING", "audit", message)
```

**Explicitly does not own:** redaction (no import of `agent.logging.redaction` in the
module — verified by grep: zero `redact` matches in `thoughtmachine/audit_logger.py`);
structure (no JSON, no event ids, no session/workspace columns); durability guarantees —
errors are swallowed (`:158-159`: *"best-effort: don't crash the agent if the audit log
is unwritable"*).

**Who writes:** seven production modules, one of them at **import time**:

```python
# infra/containers/executor.py:59-60
_audit = lambda e, d: audit_event(e, d)  # module-level convenience
_audit("MODULE_LOAD", f"file={__file__} pid={os.getpid()}")
```

Others: `infra/container_manager.py:104`, `infra/container_registry.py:809,818,870`,
`infra/container_lifecycle.py:150`, `infra/resource_container_manager.py:1437,1632,2326`,
`tools/docker_code_runner.py:410`, `agent/core/tool_executor.py:741`.

**Who reads it:** **not established from code.** No production reader; only tests
(`tests/test_container_config_audit_redaction.py`, which itself documents the sink as
*"plaintext audit log (``<vault>/container_audit.log``, i.e. plaintext on disk)"*, `:1-6`).

**Who deletes it:** only handler rotation (3 backups). No age-based cleanup inside the
module; the file is deleted as collateral by sink (b)'s `_cleanup_old_logs` (§3.1).

---

## 2. Where redaction actually happens

One utility, called in exactly three production places — all of them **file writes**:

| call site | sink | timing |
|---|---|---|
| `agent/logging/__init__.py:237` (`_write_jsonl`) | (b) file | before JSON is written |
| `agent/logging/__init__.py:269` (fallback file) | (b) file | before fallback write |
| `agent/logging/event_logger.py:186` (`_writer_loop`) | (a) file | after dequeue, before write |
| `agent/logging/event_logger.py:198` (drain on stop) | (a) file | same rule |
| `agent/logging/streams.py:120` | lifecycle streams | line level |

The utility itself: `redact(text)` at `agent/logging/redaction.py:51-67`, patterns at
`:17-41`, prefix-preserving replacement (`:44-48`), never raises.

**(b) — the redaction point is not `log()`.** `unified.py` never imports `redact`; the
console path prints the (truncated) payload as-is at `:548`. Redaction happens one layer
down, inside the file writer:

```python
# agent/logging/__init__.py:235-238
                # Redact before serialising to disk: secrets must never hit
                # the session log file.
                json_line = redact(json.dumps(event, ensure_ascii=False)) + '\n'
                self._file_handle.write(json_line)
```

So the ruling's "exactly one place per sink, at `log()`" is **not what the code does
today**: it is at `_write_jsonl` (file only), and `log()` is not the only ingress —
`logger._log_event(...)` is also called directly (`unified.py:580` forwards to it;
`thoughtmachine/security.py:780` uses it as a fallback). Moving the call up to `log()`
would (i) start redacting the console line and (ii) leave the direct `_log_event`
callers uncovered unless they are folded into `log()`.

**(a) — `EventLogger.emit()` does not exist.** The class' members are `instance`,
`__init__`, `file_path`, `_ensure_file_open`, `_maybe_rotate`, `subscribe_all`,
`attach_worker_bus`, `start`, `stop`, `_on_event`, `_writer_loop`, `get_tail`,
`__enter__`, `__exit__` (`agent/logging/event_logger.py:21-240`). Redaction today sits in
`_writer_loop` (`:186`), i.e. **after** the event has been queued and serialised. The
nearest "emit boundary" hook in the class is `_on_event` (`:142-174`), which runs on the
bus thread before serialisation. Either anchor satisfies "one place per sink" today
(count: two call sites, one rule, one function); neither is literally named `emit()`, so
the ruling needs a concrete method target.

**(c) — there is no single place, and no place at all inside the module.** The only
redaction applied to an audit line happens at one caller, by hand:

```python
# infra/container_manager.py:1959
               f"session=[REDACTED {_redact_permission_shape(self.session_permissions)}] "
```

(helper `:107`; regression test `tests/test_container_config_audit_redaction.py:1-6`).
Every other caller passes whatever string it built. Until a hook exists in
`audit_event()` (`thoughtmachine/audit_logger.py:125-159`), the ruling "exactly one place
per sink" is **false for (c)** and honestly reported as such.

**Upstream regimes that are not `redaction.py`** (they must not be mistaken for the one
place):

- `thoughtmachine/security.py:322-341` `_redact_sensitive_data` — *key-name* matching
  only, and strings pass through untouched (`:339-341`); used by `_log_security_event`
  (`:768`) which then calls sink (b)'s `log()` (`:777`).
- `tools/host_bash_tool.py:135-140` `_redact_command` — scrubs `os.environ` values.
- `agent/config/audit.py` `redact_config` applied before bus broadcast
  (`tools/workspace/worker_thread.py:402-404`).

Net: a secret can be redacted by shape (`redaction.py`), dropped by key name
(`security.py`), or neither — depending on which sink the line lands in.

---

## 3. Who deletes what — and what is therefore a deletion candidate

### 3.1 The shared directory makes deletion non-local

Every sink writes into `get_log_root()`. The only *directory-wide* deleter is sink (b)'s
age sweep, and it is name-agnostic:

```python
# agent/logging/__init__.py:644-650
        for fname in os.listdir(log_dir):
            fpath = os.path.join(log_dir, fname)
            if os.path.isfile(fpath):
                mtime = os.path.getmtime(fpath)
                age_days = (now.timestamp() - mtime) / 86400
                if age_days > max_age_days:
                    os.remove(fpath)
```

Consequence: `event_log.jsonl`, `event_log.jsonl.1`, `container_audit.log*` and the
lifecycle streams are all deleted after 7 days idle **by sink (b)**, whenever a new
`_AgentLogger` is constructed. Whether the sweep is *meant* to cover other sinks is
**not established from code** (no comment and no name filter, unlike
`_prune_logs_by_size` at `:676`). Any Wave-0 deletion of (b) would silently change the
retention behaviour of (a), (c) and the lifecycle streams.

### 3.2 Retention per file (as the code stands)

| file | writer | rotation | age deletion | size deletion | readers |
|---|---|---|---|---|---|
| `event_log.jsonl` (+`.1`) | (a) | 5 MB, 1 backup (`event_logger.py:81-90`) | collateral via (b) | none | `check_system` `event_log` query; `/health` tail |
| `agent_<session>.jsonl` (+`.<ts>`) | (b) | 10 MB archive (`__init__.py:245-251`) | yes, `TM_LOG_MAX_AGE_DAYS` (`:635`) | yes, `TM_LOG_DIR_MAX_MB` (`:655`) | none found |
| `agent_fallback.jsonl` | (b) fallback (`:265-269`) | none | yes (same sweep) | yes (matches `agent_*`) | none found |
| `container_audit.log` (+3) | (c) | 10 MB × 3 (`audit_logger.py:112-117`) | collateral via (b) | none | none found |
| `session.log`, `worker_*.log`, `container.log`, `provider_raw.jsonl`, `tool_calls_raw_debug.log` | `agent.logging.lifecycle` | 5 MB, 1 backup (`streams.py:20-21`) | collateral via (b) | none | `tm-logs` (`agent/cli/logs.py:69-74`) |

### 3.3 Deletion decidability per sink

- **(a) `EventLogger`** — *not* a free deletion. Two live readers must be replaced
  first: `tools/workspace/check_system.py:1088` (agent-visible `event_log` query) and
  `web_ui/backend/health_routes.py:214` (health payload). Nothing else depends on it;
  the file has no other consumer.
- **(b) `agent.logging`** — two separable halves. The *file* half has no reader found in
  code (§1.2) and is the only thing `_cleanup_old_logs` / `_prune_logs_by_size` exist
  for; the *console* half is the actual UI/CLI surface (`unified.log`, `:488-591`) and
  is what `/api/logging/config` tunes. They can be decided independently.
- **(c) `thoughtmachine/audit_logger.py`** — no reader, no redaction, plaintext, seven
  call sites including an import-time emit (`infra/containers/executor.py:59-60`). It is
  the clearest deletion candidate on accountability grounds *and* the most entangled:
  removal touches seven modules and one regression test
  (`tests/test_container_config_audit_redaction.py`).

### 3.4 Inventory gaps (enumerated, not audited)

Found while establishing the three sinks; they must be on the list for deletion to be
decidable, but they are outside this dispatch's scope:

1. `agent/logging/lifecycle.py` — four+ JSONL streams (`session.log`, `worker_<name>.log`,
   `container.log`, `provider_raw.jsonl`, `tool_calls_raw_debug.log`), docstring
   `:10-14`, writer `agent/logging/streams.py` with line-level redaction (`:120`), read by
   `tm-logs`.
2. `tools/host_bash_tool.py:142-166` — its own `host_bash_audit.jsonl` with an ad-hoc
   audit record and its own redaction (`_redact_command`, `:135`); a sibling
   `host_port_audit.jsonl` is named at `:96-98` (its writer was not inspected here).
3. `thoughtmachine/container_record/api.read_event_log` (referenced from
   `web_ui/backend/container_record_routes.py:211`) — per-record container events stored
   in the vault; these are *data*, not a sink, but they are also called "event log" and
   must not be confused with (a).

**Not established from code:** whether any of §3.4 is considered in-scope by the
operator's three-sink ruling.

---

## 4. GUI logging control — fix or kill

The sidebar "Logging" panel (`web_ui/frontend/src/components/LoggingPanel.jsx`, mounted at
`web_ui/frontend/src/App.jsx:43`, `:162-164`, opened at `:1363`, rendered at `:1421-1439`)
plus its REST route (`web_ui/backend/logging_routes.py`, `router` at `:25`, `GET /config` `:35-52`,
`PUT /config` `:55-105`) is the only user-visible logging control in the product. This section
records what it does, what it looks like it does, the distance between the two, and whether
that distance is worth closing.

### 4.1 What it does today

**Wiring.** The panel is a dumb view over four props (`LoggingPanel.jsx:8-12`, signature `:56`).
`App.jsx` fetches `GET /api/logging/config` on mount (`:1283-1285`, helper `:1199-1211`) and on
retry (`:1425`), and PUTs on save (`:1427-1435`), replacing its `loggingConfig` state with the
response (`:1434`). The route writes back `result["config"]` from `get_log_config()`
(`logging_routes.py:89`) and broadcasts it to every open tab
(`EventForwarder.broadcast_logging_config`, `web_ui/backend/event_forwarder.py:112-119`, event
type `logging_config_changed`), which the frontend does consume
(`web_ui/frontend/src/components/SessionTab.jsx:952-959` → `App.jsx:1403`). Origin-checked at
`logging_routes.py:68`. So: no dead plumbing — fetch, save, and live update all work.

**Payload.** The save body is built at `LoggingPanel.jsx:141-145` and contains exactly three
keys: `level`, `tags` (joined from the `log_tags` array), `truncation_limits`. The route's model
matches: `LoggingConfigBody(level, tags, truncation_limits, session_id)`
(`logging_routes.py:28-32`).

**Effect.** Each field maps to one module-global mutation in `agent/logging/unified.py`:

| UI control | Payload key | Setter | Global mutated |
|---|---|---|---|
| Log Level select, `LoggingPanel.jsx:231-242` | `level` | `set_log_level` `unified.py:355-380` | `CURRENT_LOG_LEVEL` (`:372`, `:379`) |
| Log tags text input, `:249-256` | `tags` | `set_log_tags` `:383-410` | `_LOG_TAGS` (`:405`, `:407`, `:409`) |
| Truncation limits, `:305-327` | `truncation_limits` | `set_truncation_limits` `:413-432` | `_runtime_truncation_limits` (`:431`) |

Those globals are read in exactly two places per log call: the console gate
`_should_log_to_console` (`:234-277`, called at `:530`) which guards a `print(..., file=sys.stderr)`
(`:548`), and the truncation applied to the data payload (`:527`) which is then both printed
(`:542-545`) and forwarded to the `AgentLogger` (`:584`). The forwarding block (`:556-586`) sits
*outside* the console `if`, so `level` and `tags` do not filter the file/AgentLogger path —
matching the setter's own docstring, "Affects console output filtering; JSONL file logging is
unaffected" (`:360`).

**Scope.** The agent runs in the web server's own process (`web_ui/backend/bridge.py:78`
`from agent import Agent`), and the server registers a logger on the same facades
(`web_ui/backend/server.py:761-779`), so the globals the route mutates are the ones the agent
logs through in that process. Out-of-process writers (e.g. worker subprocesses emitting
`worker_*.log` per §3.4) are **not** reached by these globals — not established from code beyond
noting they are separate processes.

**No persistence.** The setters only assign globals (`:368-380`, `:401-410`, `:425-432`); the
route imports only those four functions (`logging_routes.py:16-21`) and touches no config
store. Initial values come from the environment at import: `TM_LOG_LEVEL` (`:98`, parsed `:112-115`),
`TM_LOG_TAGS` (`:99`, parsed `:118-120`), `THOUGHTMACHINE_DEBUG` (`:102-109`). So a save lives
until process exit and is then gone; `log_level` in `web_ui/backend/config_manager.py:75-77` is
never written by this path.

### 4.2 What a user would reasonably assume

The panel's own labels are the basis for the assumption:

1. "Set the verbosity of log output" (`:230`) — that raising Log Level to DEBUG raises verbosity
   of *the log output*, i.e. the thing they will later open in `~/.thoughtmachine/logs`.
2. "Toggle individual tags on/off and set per-tag levels" (`:263`) with a per-tag `INHERIT/
   DEBUG/INFO/WARNING/ERROR/OFF` select (`:281-294`) — that per-tag levels are applied and
   remembered.
3. "Maximum character length per log field" (`:308`) — that this bounds what is stored.
4. That Save writes *configuration*, not a session-scoped debug override: the panel docstring
   calls it "viewing and modifying runtime logging configuration" (`:4-5`), the route docstring
   offers "session_id (str, optional): Apply changes to a specific session only"
   (`logging_routes.py:63`), and the raw-config view (`:365-373`) plus the `dirty`/`saving`
   states (`:58-60`) imply the displayed JSON is the truth that was just written.
5. That after Save, closing and reopening the app preserves the setting.

### 4.3 The gap

Five gaps, each decidable from the code above:

1. **The level control is inert unless tags are also set.** `_should_log_to_console` ignores
   `CURRENT_LOG_LEVEL` entirely when `_LOG_TAGS` is empty: `if not _LOG_TAGS: return msg_priority
   >= WARNING` (`unified.py:263-264`). The default is empty tags (`:118-120`, from `TM_LOG_TAGS`
   defaulting to `""`). So the advertised primary action — set Log Level to DEBUG — changes
   nothing at all until the user separately types a tag pattern. This is the exact inverse of the
   promise at `:230`.
2. **Per-tag levels are silently discarded.** `handleTagLevelChange` writes
   `localConfig.tag_levels` and sets `dirty` (`LoggingPanel.jsx:112-124`), but the save payload
   (`:141-145`) omits `tag_levels` and the backend model has no such field
   (`logging_routes.py:28-32`). The control therefore collects a value, shows it as dirty, saves
   successfully, and drops it — a silent no-op with a success path.
3. **`session_id` is decorative.** It is accepted (`logging_routes.py:32`), documented as
   "Apply changes to a specific session only" (`:63`), and used only to *read* back overrides
   (`:89` → `get_log_config`, `unified.py:457-467`). Nothing anywhere writes
   `_session_configs` (declared `unified.py:146`; the only references in `agent/` or `web_ui/`
   are the declaration and the two reads). Per-session logging config cannot exist, so the
   documented behaviour is unreachable.
4. **State is process-local and is not the config the user thinks they are editing.** Restart
   discards it (§4.1, "No persistence"), while `config_manager.py:75-77` keeps a `log_level` the
   panel neither reads nor writes — two notions of "the log level", one durable and inert, one
   live and ephemeral. Relatedly, the tag checkbox list the user sees is not a registry: it is
   `available_tags` from `_seen_tags` (`unified.py:143`, populated only when a line is actually
   emitted, `:522`), so the panel's contents depend on what traffic has happened since start —
   a fresh process shows almost no tags to toggle.
5. **The file sinks are untouched by design.** `level`/`tags` never reach the write path
   (`:556-586` outside the console gate; redaction at `agent/logging/__init__.py:237` is
   downstream), so the post-hoc artefacts that §2 and §3 are entirely about cannot be influenced
   from this panel. `truncation_limits` is the sole field with a durable effect, and only on the
   `data` payload of lines that go through this facade (`:527`, `:584`).

Also relevant to any fix: no test covers the route or the panel payload. The only exercise of
the setters outside the route is `tests/security/test_api_key_not_logged.py:96-105`, `:144-145`
(which sets `DEBUG` *and* `"*"` together, i.e. it never hits gap 1). Gaps 2 and 3 can therefore
regress — or, as here, have never worked — with a green suite.

### 4.4 Verdict

**Kill it.**

Reasoning, from what was read rather than from the prior:

- **It controls one thing and advertises five.** The reachable effect is "console threshold of
  the server process, conditional on a tag pattern being set as well". Two of the visible
  controls are wired to nothing (per-tag level, session scope); one is inert alone (level); one
  is durable by accident (truncation); one is a firehose toggle (tags). A panel whose majority
  controls are no-ops is worse than no panel, because it converts "I don't know how to debug
  this" into "I set it to DEBUG and still saw nothing", which is the failure mode gap 1 makes
  near-certain.
- **What it does, the environment already does better.** `TM_LOG_LEVEL` and `TM_LOG_TAGS`
  (`unified.py:98-99`) are read at import, are the only form of the setting that survives a
  restart, and are reproducible in one line. The panel is a less reliable spelling of two
  env vars — and it is *not* persistent, so it cannot even claim convenience across restarts.
- **The fix is a rebuild, not a patch.** Closing gaps 1–4 means: decoupling level from tags in
  `_should_log_to_console` (a behaviour change to a hot path used by every log call), adding
  `tag_levels` to the payload, the model, and the gate; writing or deleting `_session_configs`;
  and adding a persistence path into `config_manager`. That is four independent changes to reach
  parity with a shell variable, for a surface that §1.2 already establishes is *not* the audit
  trail — `tm-logs` reads the files, and the panel does not touch the files.
- **Kill is cheap and reversible in the right direction.** The deletion is `LoggingPanel.jsx`,
  `logging_routes.py`, the `broadcast_logging_config`/`logging_config_changed` plumbing
  (`event_forwarder.py:112-119`, `bridge.py:1411-1413`, `SessionTab.jsx:952-959`,
  `App.jsx:162-164`, `:1199-1211`, `:1283-1285`, `:1421-1439`), and the app's mount-time fetch.
  It does **not** require touching `agent/logging/unified.py`: the setters stay exported
  (`agent/logging/__init__.py:740-741`) and used by tests, and they are the honest API for a CLI
  or a future control. Nothing outside the route calls them in production code today.

**Caveat, stated so the operator can overrule the read:** if the intent of the control was
narrowly "make the server console loud while I poke at it in a browser", the correct deliverable
is a two-line doc note naming `TM_LOG_LEVEL` / `TM_LOG_TAGS`, not a sidebar panel — which is
still *kill*, only with a note. The one thing that would change the verdict is evidence that
someone depends on live, no-restart console verbosity from the UI; nothing in the tree
establishes such a dependency (no test, and the only production caller of the setters is this
route).

**Not established from code:** whether the operator counts "console verbosity while the server
is running" as a supported use case; whether any out-of-tree client calls `/api/logging/config`
(the route is not versioned or documented outside its own docstring).


---

## 5. Call-site inventory — keep / delete / unclear

Every logging ingress in `agent/`, `llm_providers/`, `web_ui/backend/` is accounted for below exactly once. This is the last section of the note. **This note deletes nothing** — it marks; the deletions are a separate change.

### 5.1 Marking criteria (stated once, used throughout)

- **keep** — a human or a tool reads it, and it carries information nothing else carries.
- **delete** — nothing reads it, *or* it duplicates a sink already counted in §1, *or* it logs a non-event at a level that will never be enabled.
- **unclear-needs-ruling** — it reads like it was load-bearing once, or you cannot tell whether something parses it. Not softened to keep.

The level rule that follows from §1.2 and is applied mechanically:

- `log('DEBUG'|'INFO', …)` → **delete**. `_should_log_to_console` returns `msg_priority >= WARNING` when `_LOG_TAGS` is empty (`agent/logging/unified.py:263-264`), and the default `TM_LOG_TAGS` is `""` (`:99`, `:118-120`); the file half of sink (b) has no reader.
- `log('WARNING'|'ERROR'|'CRITICAL', …)` → **keep**. It prints to stderr (`print(…, file=sys.stderr)`, `unified.py:548`) on a default install.

### 5.2 The mechanical universe

Line-based count, comment-only lines and `def log(` / `def _log_event(` definitions excluded:

| component | agent/ | llm_providers/ | web_ui/backend/ | total |
|---|---|---|---|---|
| `log(` | 534 | 76 | 376 | **986** |
| `logger.<level>(` | 11 | 0 | 14 | **25** |
| `logging.getLogger(` | 10 | 0 | 5 | **15** |
| `_log_event(` | 42 | 0 | 0 | **42** |
| EventLogger reference lines | 5 | 0 | 10 | **15** |
| **total** | **602** | **76** | **405** | **1083** |

Of the 986 `log(` sites, **963** resolve on one line to the literal form `log('<LEVEL>', '<tag>', …)`, in **141 (file, level, tag) groups**:

| level | count |
|---|---|
| DEBUG | 420 |
| WARNING | 220 |
| INFO | 195 |
| ERROR | 125 |
| EXCEPTION | 2 |
| CRITICAL | 1 |
| **total** | **963** |

420 + 220 + 195 + 125 + 2 + 1 = 963. The remaining **23** `log(` sites are not same-line-literal and are handled individually in §5.5.

> Footnote on precision: a second counting pass with the same regex reported `logger.<level>(` as 33 rather than 25, which would move the grand total to 1091. The discrepancy was not resolved and does not change any marking (all of those sites are `unclear` either way). Treat the `logger.*` row and the 1083 total as correct-to-±8, not as a point estimate.

### 5.3 Group rules

A group rule covers every site of one `(file, level, tag)` and states the count it covers. No site is covered by two rules. The tag is the second argument of `log(level, tag, msg)`; it is a stable string, so grouping by it is mechanical, not a judgement call. Marking is by level, per §5.1:

- **R-D** (delete): every DEBUG group.
- **R-I** (delete): every INFO group.
- **R-K** (keep): every WARNING / ERROR / CRITICAL group.
- **R-X** (unclear): `log('EXCEPTION', …)` — not a `LogLevel` member, priority lookup unverified.

`R-K` is a keep *on the level rule alone*: these print on a default install. It is explicitly **not** a claim that each one passes the duplicate test — the duplicate criterion would need a line-by-line read of 346 sites, which this note did not do. If a later pass finds that a `R-K` site duplicates an exception the caller already surfaces, that site flips to delete.

#### agent/ (534 `log(`)

| file | `log(` | D | I | W | E | C | EXC | rules |
|---|---|---|---|---|---|---|---|---|
| `agent/events.py` | 4 | 2 | 0 | 0 | 2 | 0 | 0 | R-D 2, R-K 2 |
| `agent/startup_health_check.py` | 4 | 0 | 1 | 2 | 1 | 0 | 0 | R-I 1, R-K 3 |
| `agent/core/agent.py` | 106 | 77 | 10 | 13 | 5 | 1 | 0 | R-D 77, R-I 10, R-K 19 |
| `agent/core/debug_context.py` | 23 | 0 | 23 | 0 | 0 | 0 | 0 | R-I 23 |
| `agent/core/llm_client.py` | 7 | 4 | 1 | 2 | 0 | 0 | 0 | R-D 4, R-I 1, R-K 2 |
| `agent/core/state.py` | 18 | 18 | 0 | 0 | 0 | 0 | 0 | R-D 18 |
| `agent/core/tool_executor.py` | 6 | 3 | 1 | 0 | 2 | 0 | 0 | R-D 3, R-I 1, R-K 2 |
| `agent/core/turn_transaction.py` | 10 | 10 | 0 | 0 | 0 | 0 | 0 | R-D 10 |
| `agent/knowledge/codebase_indexer.py` | 62 | 10 | 23 | 21 | 6 | 0 | 2 | R-D 10, R-I 23, R-K 27, R-X 2 |
| `agent/knowledge/codebase_kb.py` | 13 | 7 | 0 | 5 | 1 | 0 | 0 | R-D 7, R-K 6 |
| `agent/knowledge/dependencies.py` | 10 | 8 | 1 | 1 | 0 | 0 | 0 | R-D 8, R-I 1, R-K 1 |
| `agent/logging/debug_log_adapter.py` | 2 | 0 | 0 | 1 | 1 | 0 | 0 | R-K 2 |
| `agent/presenter/agent_presenter.py` | 18 | 10 | 0 | 2 | 6 | 0 | 0 | R-D 10, R-K 8 |
| `agent/presenter/event_processor.py` | 20 | 20 | 0 | 0 | 0 | 0 | 0 | R-D 20 |
| `agent/presenter/session_lifecycle.py` | 60 | 52 | 2 | 4 | 2 | 0 | 0 | R-D 52, R-I 2, R-K 6 |
| `agent/presenter/state_bridge.py` | 22 | 16 | 1 | 5 | 0 | 0 | 0 | R-D 16, R-I 1, R-K 5 |
| `agent/config/loader.py` | 23 | 6 | 2 | 13 | 2 | 0 | 0 | R-D 6, R-I 2, R-K 15 |
| `agent/config/preset.py` | 9 | 2 | 1 | 5 | 1 | 0 | 0 | R-D 2, R-I 1, R-K 6 |
| `agent/config/service.py` | 17 | 9 | 3 | 0 | 5 | 0 | 0 | R-D 9, R-I 3, R-K 5 |
| `agent/controller/__init__.py` | 95 | 58 | 17 | 13 | 7 | 0 | 0 | R-D 58, R-I 17, R-K 20 |
| **subtotal** | **534** | **312** | **86** | **87** | **41** | **1** | **2** | |

529 of the 534 agent/ `log(` sites are group-covered by the table above; the remaining 5 are the delegating wrappers in `agent/logging/unified.py:595-613`, marked individually in §5.5. Remainder: 0.

Tag splits worth keeping (each is one group rule, counts as stated):

- `agent/core/agent.py` DEBUG 77 = `core.agent` 20, `core.pause` 9, `core.token` 9, `core.pruning` 8, `**pipeline.token_update**` 7, `core.message_insertion` 5, `core.context_builder` 4, `core.emergency` 4, `core.session_history` 4, `**pipeline.warning**` 4, `core.summary` 2, `core.config` 1 → R-D 77.
- `agent/core/agent.py` WARNING 13 = `core.agent` 6, `core.validation` 2, `core.pruning` 2, `core.config` 1, `credential` 1, `core.token` 1 → R-K 13. ERROR 5 = `core.agent` 5; CRITICAL 1 = `core.agent` 1 → R-K 6. INFO 10 = `core.agent` 5, `core.config` 2, `core.token` 2, `credential` 1 → R-I 10.
- `agent/controller/__init__.py` DEBUG 58 = `core.controller` 51, `core.pause` 5, `pipeline.controller` 2; WARNING 13 = `core.controller` 7, `presenter.pause_flow` 6.
- `agent/knowledge/codebase_indexer.py` WARNING 21 / ERROR 6 / EXCEPTION 2 all carry the single tag `indexer`.

#### llm_providers/ (76 `log(`)

| file | `log(` | D | I | W | E | C | EXC | rules |
|---|---|---|---|---|---|---|---|---|
| `llm_providers/anthropic_provider.py` | 27 | 25 | 0 | 2 | 0 | 0 | 0 | R-D 25, R-K 2 |
| `llm_providers/openai_compatible.py` | 47 | 40 | 0 | 5 | 2 | 0 | 0 | R-D 40, R-K 7 |
| `llm_providers/factory.py` | 1 | 0 | 0 | 1 | 0 | 0 | 0 | R-K 1 |
| `llm_providers/tool_converter.py` | 1 | 0 | 0 | 1 | 0 | 0 | 0 | R-K 1 |
| **subtotal** | **76** | **65** | **0** | **9** | **2** | **0** | **0** | |

All 76 are group-covered (0 remainder). 65 delete / 11 keep. The 40 DEBUG lines in `openai_compatible.py` are request/response tracing — the classic "would be useful if anyone could turn it on" set, and per §1.2 nobody can.

#### web_ui/backend/ (376 `log(`)

| file | `log(` | D | I | W | E | C | EXC | rules |
|---|---|---|---|---|---|---|---|---|
| `web_ui/backend/bridge.py` | 115 | 36 | 36 | 30 | 13 | 0 | 0 | R-D 36, R-I 36, R-K 43 |
| `web_ui/backend/config_manager.py` | 17 | 1 | 2 | 13 | 1 | 0 | 0 | R-D 1, R-I 2, R-K 14 |
| `web_ui/backend/server.py` | 221 | 6 | 71 | 80 | 64 | 0 | 0 | R-D 6, R-I 71, R-K 144 |
| `web_ui/backend/session_manager.py` | 22 | 0 | 0 | 0 | 4 | 0 | 0 | R-K 4 + 18 individual (§5.5) |
| `web_ui/backend/session_routes.py` | 1 | 0 | 0 | 1 | 0 | 0 | 0 | R-K 1 |
| **subtotal** | **376** | **43** | **109** | **124** | **82** | **0** | **0** | |

358 of the 376 `log(` sites are group-covered; the remaining 18 are the multi-line `log(` calls in `session_manager.py`, marked individually in §5.5. Remainder: 0.

Tag splits:

- `server.py` 221 in 24 (file, level, tag) groups: WARNING 80 = `server` 67, `server.ws` 9, `server.config` 4. INFO 71 = `server` 43, `server.ws` 13, `server.config` 13, `server.health_containers` 2. DEBUG 6 = `server.ws` 5, `server` 1. ERROR 64 = `server.ws` 14, `server.config` 10, `server.record_user_action` 7, `server` 5, `workspace_container_resources` 4, `workspace_container_logs` 4, `workspace_containers` 3, `workspace_container_status` 3, `workspace_container_stop` 3, `workspace_container_delete` 3, `workspace_container_action` 2, `workspace_container_start` 2, `health_containers` 2, `container_integrity` 1, `container_status` 1.
- `bridge.py` 115 in 8 groups: WARNING 30 = `server.bridge` 28, `pipeline.bridge` 2. INFO 36 = `server.bridge`. ERROR 13 = `server.bridge`. DEBUG 36 = `pipeline.bridge` 21, `server.bridge` 9, `pipeline.hops` 5, `core.token` 1.
- `config_manager.py` WARNING 13 = `server.config` 9, `server.bridge` 4.

### 5.4 EventLogger — zero emission call sites

There are **zero** `EventLogger(...).emit(...)` call sites anywhere in these three trees. The 15 EventLogger reference lines (§5.2) are the implementation itself (`agent/logging/event_logger.py`, 5), reads in `web_ui/backend/health_routes.py` (4) and construction/reads in `web_ui/backend/server.py` (6). Sink (a) emissions arrive over the event bus, not through a direct `emit()` call. Consequence: there are no emit rows to mark, and **there is no `emit()` call site to attach redaction to** — §2 has to hook the bus subscriber, not the callers.

### 5.5 Individual rows (not group-covered)

**23 non-literal `log(` sites** — one row each; all **unclear-needs-ruling**:

| site | count | marking | why |
|---|---|---|---|
| `web_ui/backend/session_manager.py` multi-line `log(` calls | 18 | unclear | level literal sits on the continuation line, so the mechanical pass could not resolve it; in `try/except` handlers throughout (delete/rename/list/get_open_sessions), so plausibly mostly WARNING/ERROR and plausibly duplicating responses already returned to the client — unresolved either way |
| `agent/logging/unified.py:595-613` delegating wrappers `debug/info/warning/error/critical` | 5 | unclear | not independent call sites, but `web_ui/backend/logging_routes.py:16` imports names from this module, so deleting them is an API change, not a log deletion |

**`logger.*` sites: 25 (11 agent/, 14 web_ui/backend/) — all unclear-needs-ruling.** Whether they reach a human depends on which logger name each site holds: `AgentLogger._initialize_logging` attaches a `NullHandler` (`agent/logging/__init__.py:191`) to suppress `logging.lastResort`, while `agent/logging/console.py:61-63` installs a real `StreamHandler(sys.stderr)` on the `thoughtmachine` logger, and `agent/logging/lifecycle.py:76-80` attaches `_ClosedStreamSafeHandler` to `_console_logger`. Sites: `agent/knowledge/global_kb.py` 6, `agent/config/config_manager.py` 4, `agent/core/tool_executor.py` 1; `web_ui/backend/workspace_routes.py` 5, `web_ui/backend/global_routes.py` 3, `web_ui/backend/origin_policy.py` 3, `web_ui/backend/health_routes.py` 2, `web_ui/backend/config_manager.py` 1.

**`logging.getLogger(` sites: 15 — all unclear-needs-ruling**, same reason (they are the logger construction that decides the above): `agent/core/message.py` 1, `agent/core/tool_executor.py` 1, `agent/knowledge/global_kb.py` 1, `agent/logging/__init__.py` 1, `agent/logging/console.py` 2, `agent/logging/lifecycle.py` 1, `agent/config/config_manager.py` 1, `agent/config/vault_drift.py` 2, `web_ui/backend/config_manager.py` 1, `global_routes.py` 1, `health_routes.py` 1, `origin_policy.py` 1, `workspace_routes.py` 1.

**`_log_event(` sites: 42 — all delete.** 41 in `agent/logging/__init__.py` are the body of sink (b) itself (the per-event-type writers reached from `_write_jsonl`), and 1 is the forwarding hop `unified.py:580`. Deleting sink (b)'s file half (§3) deletes all 42 by construction; none of them is independently observable.

**EventLogger references: 15 — all keep.** Sink (a) is read: `tools/workspace/check_system.py:1088` and `web_ui/backend/health_routes.py:214` (`EventLogger.instance().get_tail(20)`). Kept as one group, count 15.

### 5.6 Arithmetic

```
1083 sites    = 986 log(  +  25 logger.*  +  15 getLogger(  +  42 _log_event(  +  15 EventLogger refs
    per dir    = 986 (534+76+376) + 25 (11+14) + 15 (10+5) + 42 (42+0+0) + 15 (5+0+10)

986 log(      = 963 group-covered (141 (file, level, tag) groups) + 23 individual + 0 remainder
963 literal   = 420 DEBUG + 220 WARNING + 195 INFO + 125 ERROR + 2 EXCEPTION + 1 CRITICAL

marking tally
  agent/           134 keep + 440 delete +  28 unclear =  602
  llm_providers/    11 keep +  65 delete +   0 unclear =   76
  web_ui/backend/  216 keep + 152 delete +  37 unclear =  405
  ---------------------------------------------------------------
                  361 keep + 657 delete +  65 unclear = 1083   ✓
```

Composition of the tally: keep 361 = 346 `R-K` WARNING/ERROR/CRITICAL + 15 EventLogger refs. Delete 657 = 615 `R-D`/`R-I` DEBUG+INFO + 42 `_log_event`. Unclear 65 = 2 `R-X` EXCEPTION + 23 non-literal `log(` + 25 `logger.*` + 15 `logging.getLogger(`.

Nothing is dropped: 361 + 657 + 65 = 1083, which is the sum of the components in §5.2.

### 5.7 What this implies for Wave 0

**361 keep / 657 delete / 65 unclear of 1083** — 61% of every logging ingress in the product is deletable with no loss of any information anyone currently receives, and the deletions are mechanical: two level filters (`R-D`, `R-I`) and one module (`sink (b)`'s file half, which takes its own 42 `_log_event` sites with it). Wave 0 should therefore be sequenced as (i) delete the DEBUG/INFO surface and the file half of sink (b), which is where the bulk and all of the redaction cost live, then (ii) audit the surviving 346 `R-K` warnings against the duplicate criterion — that is the only judgement-heavy work left, and it is 32% of the original corpus rather than all of it.

