# hygiene/audit-sink-in-tmp

`CONTAINER_DESTROY` (and other `CONTAINER_*` audit events) primary sink is
`/tmp/container_audit.log` (`thoughtmachine/audit_logger.py:27-30`, env
`CONTAINER_AUDIT_LOG_PATH`, RotatingFileHandler 10MB x3, `except: pass` on write
failure). No production code sets that env var. **Good news: the events ARE
also observed** — 53 `CONTAINER_*` entries in `~/.thoughtmachine/logs/session.log`,
`CONTAINER_DESTROY` in agent jsonl, and rotated `event_log.jsonl.1`. So the audit
trail exists; the `/tmp` primary sink is the fragile part (wiped on reboot,
swallowed write failures).

Also: `tools/docker_code_runner.py:409-410` is a second writer to the same
hardcoded `/tmp` path. And `tests/test_container_config_audit_redaction.py:4`
describes the log as `<vault>/**container_audit.log` — code's default is `/tmp`.
Docs disagree with code.

**Fix:** route primary audit to `~/.thoughtmachine/logs/container_audit.jsonl`;
treat `/tmp` as secondary/optional. Add a test that opens the sink and asserts
the file receives the event (current test only asserts the function was *called*).
