# Ticket: signal-hijack probe fails spuriously when the suite is launched as an async child

Status: open

## Problem

`tests/test_server_import_no_signal_hijack.py::test_server_import_does_not_hijack_signal_handlers`
fails when the suite is launched as `cmd &` from a non-job-control shell, even though the code
under test is correct. POSIX sets `SIGINT`/`SIGQUIT` to `SIG_IGN` in async children, `nohup` adds
`SIGHUP`, and CPython preserves an inherited `SIG_IGN`. The probe then compares
`signal.getsignal(signal.SIGINT)` against `signal.default_int_handler` and fails at its first
assertion — before `web_ui.backend.server` is even relevant. The reported failure therefore says
nothing about signal hijacking; it reports the launcher.

## Evidence

- 5/5 pass when the suite runs in the foreground.
- 1 pass when launched foreground under `setsid`.
- 1 fail when launched as `setsid nohup … &`.
- The failing assertion is the pre-import baseline comparison, so the import under test has not
  run yet at the point of failure.

## Suggested fix

Snapshot the dispositions of `SIGINT`, `SIGQUIT` and `SIGHUP` *before* importing
`web_ui.backend.server`, then compare the post-import dispositions against that snapshot rather
than against `signal.default_int_handler`. An inherited `SIG_IGN` then matches itself and the test
measures only what the import changed. Keep the strict assertion that an inherited-`SIG_IGN`
disposition is preserved unchanged.
