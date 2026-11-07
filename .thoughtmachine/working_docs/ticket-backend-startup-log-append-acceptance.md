# Ticket: acceptance greps against `logs/backend_startup.log` can match a stale boot

Status: open

## Problem

`logs/backend_startup.log` is append-only. An acceptance check that greps it can therefore match a
warning written by a *previous* boot and report it as evidence about the current one. The check
then fails (or passes) for a reason that has nothing to do with the build under test — the failure
mode is a false positive that looks like a regression.

## Evidence

- The log file is opened in append mode and every boot writes into the same file; nothing rotates
  or truncates it at startup.
- An acceptance grep of the form `grep <pattern> logs/backend_startup.log` returns matches from
  earlier boots as well as the current one, so a fixed warning from an older build keeps appearing
  after it is fixed.

## Suggested fix

Make acceptance greps scope themselves to the current boot, by either:

- filtering by timestamp (record the boot start time, then match only lines after it), or
- rotating/truncating the log before the run so the file contains exactly one boot.

Prefer the timestamp filter: rotation changes operator-visible behaviour and loses history that is
sometimes the only record of a failed boot.
