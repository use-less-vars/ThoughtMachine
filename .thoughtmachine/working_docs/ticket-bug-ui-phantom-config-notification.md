# Ticket: bug/ui-phantom-config-notification

**Filed:** 2026-10-29, from operator report ("Memory & Stats Wave" briefing).
**Verified at:** HEAD `84bfd019` — every line reference below was read at that SHA.
**Severity:** Medium — noise plus context pollution, no data loss.
**Status:** filed, not dispatched.
**Naming note:** the briefing proposed `bug/ui-phantom-config-notification.md`.
`.thoughtmachine/working_docs/*` is gitignored except `ticket-*.md`,
`protocol-*.md` and named files (`.gitignore:120-124`), so a file named to the
briefing's pattern would be invisible to git and vanish on the next clone.
Filed under the tracked convention instead.

## Symptom

On a **new** session, the **second** query surfaces:

    [SYSTEM NOTIFICATION] Configuration updated: base_url=<url>.

Nothing was changed by the operator. Queries 1, 3, 4… are clean.

## Root cause — verified, and it is backend, not frontend

The briefing attributed this to the frontend re-sending a slightly different
payload and asserted "PR #241 fixed the backend restart, frontend still diffs".
**That diagnosis is not supported by the code at this SHA.** The backend has a
guard intended to swallow exactly this, and the guard is what fails:

1. `agent/core/agent.py:498-530` `Agent._configs_are_identical()` is the
   "semantically identical" check. It normalises **only** `''` → `None`
   (`:517-521`), then compares every remaining field with raw `!=` (`:523`).
2. `agent/core/agent.py:455-461` is the early return that relies on it. Its own
   comment states the intent: *"Prevents spurious 'config updated'
   notifications on every query when the frontend re-sends an identical
   config."*
3. So any pair of strings that denote the same endpoint but differ in
   spelling — trailing slash, `v1/` vs `v1`, default-fill — makes
   `_configs_are_identical` return `False`. The guard does not swallow; it
   **passes the call through**.
4. `base_url` is RESTART_REQUIRED (`agent/config/models.py:29`) and
   `_can_hot_swap` (`agent/core/agent.py:591`) refuses on any `base_url`
   difference, so the flow lands on the full-restart branch
   (`agent/core/agent.py:487-495`), which calls
   `_notify_config_change()` on success (`:494`).
5. `_notify_config_change` (`:532-575`) rebuilds the change list with its **own**
   comparison — and this one does **no** `''`/`None` normalisation at all
   (`:552-558`), so it is strictly laxer about what counts as "changed" than the
   guard it sits behind. It emits `f"{field_name}={new_val}"` (`:569`) →
   `"[SYSTEM NOTIFICATION] Configuration updated: " + ", ".join(changes)` (`:573`).
   That is the exact string in the report, including the `base_url=` field name.

The banner names `base_url` because `base_url` is the field that differs.

### Why the *second* query specifically

This falls out of the mailbox design, not out of anything the frontend does
wrongly:

- `AgentController.update_config()` (`agent/controller/__init__.py:150-168`)
  forwards to `agent.request_config_update(config)` **only if the agent already
  exists**; otherwise it just stores `self._config` for the next start.
- A brand-new session has no agent yet, so the first query constructs the agent
  from `self._config` with **no** pending config — nothing to diff, no banner.
- The GUI's config push then arrives against a live agent and parks the config
  in `self._pending_config` (`agent/core/agent.py:428`).
- `_apply_pending_config()` runs at the **start** of the next `process_query()`
  (`agent/core/agent.py:431`), i.e. before the user_query event is yielded —
  the code says so explicitly at `agent/core/agent.py:1150-1151`.

Hence: pending config set after query 1 → applied at the top of query 2 →
notification appears with query 2. Exactly the reported pattern.

### Where the mismatched spellings come from

`base_url` has **three different canonical spellings** in-tree, which is enough
to manufacture this mismatch without anyone editing a setting:

| Site | Value |
|---|---|
| `agent/config/models.py:92` (`AgentConfig.base_url` default) | `https://api.deepseek.com` |
| `agent/config/schema_manifest.json:468` (`safe_default`) | `https://api.deepseek.com/v1/` |
| GUI/provider-profile value | whatever the profile holds, commonly `…/v1` |

Additionally `StateBridge.update_config` (`agent/presenter/state_bridge.py:78-80`)
round-trips the live config through
`current_dict = model_dump(exclude_none=True)` → `update(...)` →
`AgentConfig(**current_dict)`. Dropping `None` values and re-validating means
model defaults get re-filled, so a field that was explicitly `''` or absent can
come back populated. That is a real second generator of "identical config, new
value" and it happens entirely inside the backend.

## Not established

- **Which** spelling pair is actually in play for the operator. The discriminator
  already exists and is cheap: `agent/core/agent.py:523-527` logs
  `Config field "<name>" differs: old=<type>(<val>) != new=<type>(<val>)` at
  DEBUG. Grep the log for `Config field "` before fixing; that names both sides.
- **PR #241's actual content.** It cannot be checked from here: the remote refs
  are frozen and `git_write` exposes no fetch verb, so "PR #241 fixed the
  backend restart" is an unverified claim inherited from the briefing. Do not
  cite it in the fix. The backend restart path is present and reachable at this
  SHA regardless of what #241 did.
- Whether the operator's frontend re-sends config at all, or whether the
  state_bridge round-trip alone is sufficient to produce it. The backend
  explanation above needs no frontend misbehaviour.

## Fix direction (two parts, both backend)

1. **One comparator, canonicalised.** `_configs_are_identical` and
   `_notify_config_change` currently implement two different comparison
   semantics over the same fields, and `_config_diff` (`agent/core/agent.py:55-80`)
   is a **third**. Collapse to one function used by all three, and extend the
   normalisation to the obvious URL equivalences: strip a single trailing `/`,
   treat `''`/`None` as equal (already there), and compare `base_url` after
   normalising the `/v1` suffix question explicitly rather than by accident.
   The briefing's requirement — "only notify if the **effective** config
   changed" — is exactly this, and it is a one-function change, not a UI change.
2. **Fix the dead diagnostic.** `_notify_config_change` builds `field_log`
   (`:546`, appended at `:556`) with `changed=… old=… new=…` for every field and
   then **never logs it or returns it** — verified: nothing reads `field_log`
   after `:556`. It is dead code that computes precisely the evidence needed to
   debug this ticket. Either log it at DEBUG or delete it; leaving it silently
   discarded is how this took a generation to explain.

Optional third: make `base_url`'s three spellings agree (pick `/v1/` or `/v1`
and use it in `models.py`, `schema_manifest.json` and the profiles). Cheap, and
it removes the generator rather than the symptom.

## Severity note (why Medium, not Low)

The notification is not just a banner. `_notify_config_change` injects it as a
**user-role message into conversation history**
(`agent/core/agent.py:575`, `Message(role='user', …,
is_system_notification=True)`). It therefore stays in the transcript, is re-sent
to the LLM on every subsequent turn, and inflates context on long sessions —
which is how a cosmetic bug becomes a token-cost bug on an overnight run.

## MUST NOT TOUCH

Standard list. Do not "fix" this by suppressing notifications on the frontend:
the string is generated in `agent/core/agent.py:573` and the duplicate-guard is
in the same file.
