# Ticket: hygiene/build-log-length-metadata

**Filed:** Gen 45, 2026-10-18, from V4 build_log length unmeasurability.
**Severity:** low — measurement quality.
**Status:** filed.

## Finding

`ContainerBuildTool(nocache=True)` produces a ~10k token build log. The
harness truncates tool responses at 10,000 tokens. Result: the response
body is truncated mid-log, and `build_log` length is **unmeasurable**
through this transport — the final `Successfully tagged` line is never
visible.

Consequence: any test that says "report build_log length" is unpassable
as written. The datum we can recover: `success: true`, first 12 steps
visible, response size 10,016 tokens total → log ≥ 10,000 tokens.

## Fix

`ContainerBuildTool` should return a `build_log_length` field (character
count) and a head/tail sample, in addition to (or instead of) the full
`build_log` body. Tests can then assert on length without depending on
the body surviving transport.

Alternatively: increase the harness truncation cap. Less durable —
build logs are unbounded.

## Reference

ADDENDUM D, V4 in `reports/gen44-post9c-verification-2026-10-16.md`.
