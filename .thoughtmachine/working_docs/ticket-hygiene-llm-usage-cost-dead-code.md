# Ticket: hygiene/llm-usage-cost-dead-code

**Filed:** 2026-10-27, from the scripted-LLM fixture work (reading `llm_providers/`)
**Category:** hygiene
**Severity:** LOW — dead path, but it reads as working cost accounting
**Status:** filed, not fixed

## Summary

Cost accounting runs on every real provider call and nothing reads the result.

- `track_usage` (`llm_providers/base.py:105`) increments the token counters and
  `calls`, then `total_cost` at `:112` via `self._calculate_cost(response)`. The
  whole body is gated on a truthy `response.usage` (`:107`), so a response with no
  usage dict tracks nothing — not even the call count.
- `_calculate_cost` is a `0.0` stub in the base class (`:114`), overridden in
  `llm_providers/openai_compatible.py:546` (`self.PRICING.get(model, ...)` at
  `:549`; table at `:25`) and `llm_providers/anthropic_provider.py:206`
  (`PRICING.get(self.config.model, ...)` at `:208`; table at `:27`).
- `track_usage` has exactly two production call sites: `openai_compatible.py:358`
  and `anthropic_provider.py:78`.

The consumers are missing:

- `total_cost` is initialised (`base.py:62`), incremented (`:112`) and reset
  (`:127`). It is read nowhere in the tree.
- `get_usage_stats` (`base.py:118`) has zero callers repo-wide. Neither does the
  `_usage_stats` dict have any reader outside `base.py`.
- No test asserts on `total_cost` or on `_calculate_cost`, so the per-million-token
  arithmetic is unverified.

## Consequence

Not dead code in the harmless sense: live code with no consumer. A reader concludes
cost tracking exists, and the next feature built on it (budget caps, usage panel,
per-session reporting) would rest on numbers nobody has ever seen. The two pricing
tables also drift unobserved, and an unknown model silently falls back to
`gpt-3.5-turbo` / `claude-3-haiku` pricing rather than failing.

## Ask — one of two

1. **Wire it.** Give `total_cost` / `get_usage_stats` a real consumer and add the
   missing assertions: known model + known token counts produce a known cost; an
   unknown model is a stated fallback, not a surprise.
2. **Delete it.** Remove all three `_calculate_cost` definitions, both `PRICING`
   tables, the `total_cost` key and `get_usage_stats`. The token counters and
   `calls` stay — `track_usage` is what populates them.

## Prior art

PR #214's record says "PRICING was never consumed". Wrong in mechanism — `PRICING`
is consulted on every tracked call by both subclasses — right in effect: the number
reaches no consumer. This ticket supersedes that line rather than repeating it.

## Filed

2026-10-27, from the scripted-LLM fixture chunk. Verified by read at `6011d97e`.
