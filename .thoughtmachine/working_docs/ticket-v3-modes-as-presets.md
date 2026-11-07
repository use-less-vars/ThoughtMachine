# ticket: `feat/v3-modes-as-presets`

**Filed:** 2026-10-29 (Gen 54 delivery arc)
**Source:** operator wish-list, v3 scope. Recorded in the KB since Gen 20-odd; never filed as a ticket until now.
**Status:** filed, not started — v3 scope, not this generation's arc
**Severity:** medium — product-shape decision; any v3 UI that assumes modes are editable sits on this

## Symptom

A session carries a `mode` (`"engineer"`, `"architect"`, or `"custom"`). When the mode is
not `"custom"`, the session's tools and system prompt are **locked to the mode preset**: the
update methods are no-ops, so neither the operator nor the agent can edit the sysprompt or the
tool set for that session. The mode vocabulary itself is fixed in the model, not data — you
cannot create a mode, edit what an existing mode binds, or delete one.

The operator's v3 position, recorded in the KB: **modes are presets, not locks.** A mode should
be a user-editable preset — create / edit / delete — covering the sysprompt + behaviour/tool
set it binds, with `custom` remaining the "no preset" case rather than the only escape hatch.

## Evidence

The lock is by construction, not by policy:

- `agent/config/session_config.py:147-149` — `mode: Optional[str]`, description verbatim:
  *'Session mode — e.g. "engineer", "architect", or "custom". When not "custom", tools and
  prompts are locked to the mode preset.'*
- Same class, docstring `session_config.py:77-78` — *'When `mode` is not `'custom'`,
  `update_tools` and `update_prompt` are **no-ops** — the mode preset is enforced.'*
- `agent/config/models.py:115` — the config restart-policy map lists `'mode': RESTART_REQUIRED`,
  so a mode change today costs a restart.
- Mode endpoints exist but are single-mode: `POST /api/config/mode` and `GET /api/config/mode`
  (asserted by `web_ui/backend/tests/test_config_mode.py:2`, classes `TestSetMode` `:63` and
  `TestGetMode` `:161`). There is no list/create/edit/delete surface.

The consequence is already documented, and it is the same defect seen from the user's side:

- `archive_arch_b.md:1225` — with mode `agent`/`engineer` the frontend's `{tools: [{name,
  enabled}]}` list is translated into `enabled_tools` and then **immediately overwritten** by the
  mode preset. The user's toggles are silently discarded. (Archive note; re-verify against
  current code before quoting it as live.)

Recorded in the KB but never ticketed (this ticket is the missing ticket, not a new wish):

- `project/v3_blueprint.md:74` — section *"(h) Session modes are PRESETS, not locks"*.
- `project/thoughtmachine_current_state.md:141` — *"Modes are presets, not constraints."*
- `project/open_items_register.md:212` — **D16**, *session-mode target model*: modes are presets
  not locks; a 5-step minimal removal path; future modes `welcome` / `engineer_async` /
  `engineer_sync`; whether sync-vs-async is a mode or a Worker parameter. Points at
  `session_modes.md` §4-§5.
- `project/operator_observations.md:198` — *"remove user-blockage on sysprompt/tool editing in
  pre-set modes (engineer, agent) — modes must become flexible and extendable"*; names the work
  `design/beginner-mode-and-guidance`. **No such ticket file exists** in
  `.thoughtmachine/working_docs/` (checked 2026-10-29).

No `ticket-*.md` covers modes/presets (checked across all 187 ticket files, 2026-10-29).

## Options (needs a ruling; do not implement blind)

1. **Minimal removal — presets as a base, not a cage.** Keep the existing names; delete the
   no-op branch so `update_tools` / `update_prompt` apply *on top of* the preset. No new UI, no
   schema change. Smallest possible change that makes "modes are presets" true. Does not give
   create/edit/delete.
2. **Modes as data.** A mode-definition store (vault-managed): `name → {sysprompt,
   enabled_tools, session_permissions, worker_mode, model/provider hints}` with
   `/api/config/modes` CRUD and a UI. This is what "editable presets" means literally. Needs a
   schema + migration, and a ruling on the hard question below.
3. **Modes as session-creation templates only** ("clone-on-use"): a preset is read once when a
   session is created and never re-enforced afterwards, so a session cannot be re-locked. Cheap
   and simple; gives up guard rails on edit, which is the thing presets were for.
4. **Defer to v3, ship option 1 now** as the interim: the operator's immediate complaint (cannot
   edit sysprompt/tools in a preset mode) goes away without committing the v3 data model.

**The hard question every option above must answer:** `mode` is `RESTART_REQUIRED`
(`models.py:115`). Editing a preset must decide whether the change re-binds **live** sessions,
or only future ones. That decision, not the CRUD endpoints, is what makes this a design ticket.

## Acceptance criteria

- A mode can be created, edited and deleted without hand-editing JSON and without a restart
  (or the restart requirement is stated explicitly and shown in the UI).
- Editing a mode's sysprompt / tool set changes what a session bound to it can do — and the
  ticket states whether that is live or next-session, with the answer visible in the UI.
- `custom` still works, and existing sessions naming `engineer` / `architect` load with no
  migration step and no silent loss of tools.
- User tool toggles are no longer silently discarded against a preset (the `archive_arch_b.md:1225`
  behaviour): honoured on top of the preset, or refused with a visible message.
- Tests: mode CRUD, plus a regression pin that `update_tools` / `update_prompt` are not no-ops.

## Related

- `project/v3_blueprint.md` (h)/(i), `project/open_items_register.md` D16,
  `project/operator_observations.md:198`.
- The D16 target model itself lives in **`knowledge/personal/session_modes.md`** §4-§5 — the
  personal KB tier, which `.gitignore` excludes (only `personal/handoffs.md` is re-included).
  So the one document that describes the target model does not survive a clone. Read it from
  this seat while it is still here; if option 2 is chosen, the model belongs in the project tier.
- `design/beginner-mode-and-guidance` — **named only; never filed.** If the v3 work happens, this
  is either this ticket's UI half or a sibling.
- `ticket-design-agent-role-control.md` — adjacent (who controls agent behaviour mid-session).
  Read it before designing option 2; do not assume the two are disjoint.
- `ticket-hygiene-provider-switch-mid-session-history.md` — same family of "session-level setting
  changes under a live session" questions; check for collision.
