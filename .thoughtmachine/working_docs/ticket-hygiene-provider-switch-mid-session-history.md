# Ticket: hygiene/provider-switch-mid-session-history

**Title:** Provider switch mid-session with non-empty history must be refused
**Ruling:** 2026-10-27 — reject a `provider_type` switch while this session's history is non-empty
**Status:** ruled; code change and doc patch owed, land together

## Ruling

A `provider_type` switch is **refused** while the session has history. The refusal is explicit, and its text is fixed:

> provider_type cannot change while this session has history; start a new session to switch providers

## Alternatives considered

- **(a) Convert the whole history** to the new provider's format — not chosen.
- **(c) Namespace history per provider** — not chosen.

Neither is filed. If user pain from the refusal is reported as real, (a) or (c) can be opened then; until then they are pre-optimisations, not backlog.

## Why refusal (b)

Fail-closed matches the machine's mandate. A history produced under one adapter must not be silently resumable under another, so the switch is refused rather than resolved by guesswork: the boundary stays visible at the point of switch instead of surfacing later as a transcript that does not add up.

## Doc patch owed

Two lines, in the same change as the code so they cannot drift:

- `docs/design-streaming-canonical-ir.md` §7 — the provider pass-through bullet
- `docs/design-streaming-canonical-ir.md` §8 — the Wave 4 sentence

## Corroboration

v1.4 C17: `_can_hot_swap` already refuses on `provider_type` mismatch and restarts the client carrying the conversation, so history is replayed through a different adapter today. This ruling makes the refusal **explicit**; it does not change the refusal logic.
