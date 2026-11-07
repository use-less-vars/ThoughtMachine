# config/silent-api-key-overwrite

**Filed:** 2026-10-28, from the turn-1 / turn-N credential-derivation fix review.
**Severity:** MEDIUM — security-relevant transparency gap (no secret leaks today; the *event* is invisible).
**Status:** filed; no code change started.

## Problem

When the provider genuinely changes, `ConfigManager.apply_config` replaces
`session_config.api_key` with the value resolved from the provider profile, and the
replacement is **completely silent**.

The overwrite happens in the shared helper `resolve_provider_fields()`
(`web_ui/backend/config_manager.py:809`), called from `apply_config`
(`:1259-1266`) with `overwrite=True` exactly when
`payload.provider_id != live provider_id` (`provider_changed`, `:1250-1253`).
With `overwrite` set, the guard collapses to `session_config.api_key = resolved["api_key"]`
(`:844-847`) — a workspace- or session-layer credential in use is swapped for the
provider-profile credential, in place, with no return value and no log line.

Nothing downstream can report it either, because `api_key` is in the excluded-field
set of every config comparison and diff:

- `_config_diff()` — `agent/core/agent.py:63`: `excluded_fields = {'api_key', 'stop_check'}`
- `Agent._configs_are_identical()` — `agent/core/agent.py:508`: same set
- the config-log projection — `agent/core/agent.py:543`: `sensitive_fields = {'api_key', 'stop_check'}`

So the key change contributes nothing to the hot-swap/restart decision
(`_can_hot_swap` compares the identical-check), nothing to the
"config changed" diff list that `Bridge.apply_config` captures for the notification,
and nothing to the audit record. The exclusion is *correct* for the value — a secret
must never land in a log or a WS payload — but it currently excludes the **event**
along with the value.

## Why it matters

A credential scoped to a workspace or session layer can be replaced by a
provider-profile credential with no operator-visible signal anywhere: not the UI,
not the config-changed notification, not the audit trail. The user keeps running on
a credential that changed underneath them and has no way to observe it. For a
credential-bearing field that is a transparency gap with security consequences, not
a cosmetic one — "which key is this session actually using" becomes unanswerable
from the product surface.

## Verification (pre-filed checks)

- `api_key` in the excluded set of both helpers: confirmed (`agent/core/agent.py:63`,
  `:508`; additionally `:543` for the log projection).
- Overwrite site now centralised in `resolve_provider_fields()`
  (`web_ui/backend/config_manager.py:809`), invoked with `overwrite=True` on a
  provider change from `apply_config` (`:1259-1266`, `provider_changed` at `:1250-1253`).
- The start path (`Bridge.start`, `web_ui/backend/bridge.py:1562-1578`) only fills
  blanks, so the silent replacement is specific to the apply path.

## Proposed direction

Do not start emitting the key. Emit the **event**: a provider/credential change should
appear in the config-changed notification and/or the audit trail as
*"credential replaced for provider X"* — no value, no prefix, no length, no last-four.

Sketch: have `resolve_provider_fields()` return (or report via an out-param/callback)
which fields it replaced and why (`overwrite` vs `blank fill`), then let
`Bridge.apply_config` add a fixed-string entry such as
`api_key: replaced from provider profile '<provider_id>'` to the existing diff list it
already captures at `:2014`, and log the same line at INFO. The comparison helpers in
`agent.py` stay untouched.

## Trade-off to settle in the change

Keep `api_key` absolute-excluded, or introduce a redacted marker in the diff?

- **Absolute exclusion** (status quo): zero risk of the value reaching a log, a WS
  message, or a bug report; the notification/audit trail stays blind to the swap, so
  the ticket only gets half-closed.
- **Redacted marker** (e.g. `api_key: changed`): the event becomes visible in exactly
  the places a user looks; costs a new class of string in the diff path, which must be
  a constant and must never be derived from the value (no prefix, no hash, no length),
  or it re-introduces a side channel.

Recommendation: redacted marker, implemented as a fixed string generated in
`resolve_provider_fields()`'s caller — not inside `_config_diff`, which must keep
excluding the field so the two mechanisms cannot be conflated.

## Acceptance criteria

- A provider switch with a differing credential produces one visible, value-free
  "credential replaced" line in the config-changed notification and in the audit log.
- No test, log, or WS frame anywhere contains the api_key value or a derivative of it
  (which is what the exclusion sets at `agent.py:63`, `:508`, `:543` exist to guarantee).
- A pure echo of the resolved config (no provider change) produces **no** such line —
  otherwise this becomes per-turn noise of the kind filed in
  `ticket-config-strip-empty-dict-log-noise.md`.
- `_can_hot_swap` behaviour is unchanged: a credential-only swap still must not force
  a restart (the value is excluded from the comparison), while the event is still
  reported.
