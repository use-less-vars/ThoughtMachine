# Ticket: bug/admit-count-includes-recreated-record

**Filed:** Gen 45, 2026-10-17, from admit() coverage read.
**Severity:** medium (destroy-then-cannot-recreate on a full workspace).
**Status:** filed; needs live repro before fix shape is decided.

## Finding

`admission_gate.admit()` step 6 counts the workspace's consuming
containers *including the record being recreated*. On a workspace at its
container-count ceiling, a heal/recreate/resize would be
`Deny(container_limit_exceeded)` — i.e. after the resize-reap (P7 fix),
the destroy succeeds, and the recreate is denied.

Whether this false-denies in practice depends on whether the count
excludes the record's own dead container. Needs a live repro: workspace
at ceiling, resize attempt, watch admit + remove + start ordering.

## Reference

`.thoughtmachine/working_docs/admit-coverage-read.md` §C5
