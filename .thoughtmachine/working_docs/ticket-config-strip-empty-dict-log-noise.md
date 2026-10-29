# config/strip-empty-dict-log-noise

**Filed:** 2026-10-28, from the turn-1 / turn-N credential-derivation fix review.
**Severity:** LOW — no functional effect; costs log signal on every turn of every session.
**Status:** filed; no code change started.

## Problem

Two WARNING-level config log lines fire on **every** `continue_session`, i.e. on every
query of every session, and neither one indicates a problem.

### a. `session_permissions: {}` — emitted by the server, then warned about by the server

`frontend_config_from_bridge()` (`web_ui/backend/config_manager.py:282`) stamps
`result['session_permissions'] = _canonical_session_permissions(bridge)` unconditionally —
at `:320` (early return) and again at `:344` (main return) — and
`_canonical_session_permissions()` (`:247`) returns `{}` whenever it cannot resolve
`session_id` + `workspace_id`. So the snapshot handed to the browser always carries the
key, empty or not.

The browser echoes the snapshot back verbatim on the next `continue_session`
(`QueryBar.jsx`: `{...(config ?? {}), mode}`), and `translate_frontend_config` preserves
it (normalising a non-dict to `{}` at `:429-430`). `apply_config` then strips it and
warns:

```python
if "session_permissions" in config_dict:
    config_dict = {k: v for k, v in config_dict.items() if k != "session_permissions"}
    logger.warning("apply_config: ignoring %r in config payload — the canonical "
                   "grant store is the session permission sidecar; use "
                   "PUT /api/session/{id}/permissions to change grants",
                   "session_permissions")
```

(`web_ui/backend/config_manager.py:1222-1232`.)

The strip itself is correct — the permission sidecar (P1) is the single canonical grant
store and no config carrier may resurrect a grant. The **warning** is not: its own
in-code comment says "the shipped frontend already strips the key before sending
(`ConfigPanel.jsx`)", which is true of the panel and false of the echo path, because the
key originates server-side in `frontend_config_from_bridge`. The server is warning about
a field the server put there.

### b. `mode change … rejected` — fires on an identical mode

```python
elif has_session or is_running:
    log("WARNING", "server.bridge",
        f"apply_config: mode change to '{new_mode}' rejected — "
        f"mode is immutable after session start")
elif new_mode != session_config.mode:
    ...
```

(`web_ui/backend/config_manager.py:1177-1182`.) The `has_session or is_running` branch
is tested **before** the equality branch, so any payload carrying a `mode` key while a
session is live warns "mode change rejected" — including a pure echo where
`new_mode == session_config.mode` and nothing was rejected at all.

## Why it matters

Both lines fire per request, from the ordinary path, forever. The exact log lines needed
to diagnose config-propagation defects — the class of bug just fixed in the
turn-1/turn-N credential derivation — are buried in per-request spam, and an operator
who greps for WARNING in a long session cannot tell the real ones from the ceiling noise.
The second-order cost is worse than the first: warnings that fire on the normal path
train operators to ignore WARNING-level config messages, which is precisely how a real
one gets missed.

## Proposed direction

Prefer fixing the cause over suppressing the symptom.

1. **Stop emitting the empty key.** In `frontend_config_from_bridge`, only set
   `session_permissions` when the canonical resolution produced a non-empty dict
   (both assignment sites, `:320` and `:344`); leave the key absent otherwise. Then an
   echo carries nothing to strip and the warning stops at source for the common case.
2. **Make the remaining warning honest.** Once (1) lands, a `session_permissions` key in
   a payload is genuinely abnormal (an older client, or a hand-rolled carrier) and the
   WARNING becomes informative rather than constant. Keep it.
3. **Reorder the mode check.** Test equality first — `elif new_mode == session_config.mode: pass`
   (or fall through silently) — and warn only when the mode actually differs *and* the
   session is live. That preserves the refusal semantics exactly while removing the false
   positive.

Do not simply downgrade these to DEBUG: (2) and (3) are load-bearing signals once the
normal path stops producing them.

## Acceptance criteria

- A `continue_session` whose payload is a pure echo of the current config produces zero
  WARNING lines from `apply_config`.
- A genuine attempt to change `mode` mid-session still produces the existing refusal
  warning, verbatim.
- A payload that genuinely carries `session_permissions` (constructed by hand or by an
  older client) still produces the existing "ignoring …" warning, verbatim.
- The frontend snapshot either omits `session_permissions` when it is empty or carries
  the non-empty canonical map — verified on both `frontend_config_from_bridge` return
  paths.
- No change to grant enforcement: the sidecar (P1) remains the only grant store and the
  key is still stripped from the payload.
