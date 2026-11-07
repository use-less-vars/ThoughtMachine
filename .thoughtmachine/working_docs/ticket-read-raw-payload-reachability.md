# Ticket: read/raw-payload-reachability

**Filed:** Gen 44, 2026-10-15, from Main's ruling on the vault_status leak.
**Severity:** unknown until read - potentially higher than vault_status.
**Status:** filed as a read.

## Question

Are `~/.thoughtmachine/logs/provider_raw.jsonl` and
`~/.thoughtmachine/logs/tool_calls_raw_debug.log` reachable via ANY
CheckSystem query or any tool from an agent session?

If yes: **raw LLM payload exposure** - a different class from the
vault_status metadata leak, and higher severity (prompts, tool calls,
model responses from all sessions).

If no: document the exclusion so the next reader knows.

## Deliverable

A read-only report at `.thoughtmachine/reports/raw-payload-reachability.md`
enumerating every tool/query that could surface these paths (or their
contents), with a grep proof for each.

## MUST NOT TOUCH
Same list as the container tickets.
