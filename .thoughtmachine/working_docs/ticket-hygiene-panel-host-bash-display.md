# Ticket (hygiene): panel host_bash display may echo declared, not enforced

**Frame / Tag:** hygiene
**Status:** open (FILE ONLY -- no fix in wave 9c; severity raised to MEDIUM 2026-10-22)
**Severity:** MEDIUM -- the grant can read as ordinary while it is in force
**Scope:** web UI workspace/session panel (non-agent)

## Summary

Wave 9c fixed the agent-facing `CheckSystem query='workspace_info'` so it reports
the GATE-RESOLVED `host_bash` value (via `_query_permissions().effective_permissions.host_bash`,
fail-closed to `banned`).

But web panels do NOT read `workspace_info`. `CheckSystem` is an agent-only tool.
The panels obtain `host_bash` from the session payload built in
`web_ui/backend/routes/workspace_routes.py:1010` plus the effective-permissions endpoint.

If that path echoes the CONFIG-DECLARED permission rather than the gate-resolved
value, the same "displayed != enforced" bug is present for the human-facing panel:
a session whose config allows host_bash but whose gate bans it would display `allow`.

## Fix shape

Same shape as the CheckSystem fix -- surface the gate-resolved value
(`effective_permissions.host_bash`), fail-closed to `banned`. One file
(`workspace_routes.py`) plus a test.

## Verification needed FIRST

Confirm whether `workspace_routes.py:1010` already sources the gate-resolved value.
If it does, close this ticket as invalid (no bug).

## Evidence

Raised during wave-9c independent verification (2026-10-16). See
`.thoughtmachine/working_docs/wave9c-spec.md` and the wave-9c commit.

## 2026-10-22 -- the documented promise, and the evidence nothing implements it

### The promise, as written

`docs/ARCHITECTURE_MACHINE_MODEL.md` §5 states three things a `host_bash` grant must come with.
One of them is a display obligation:

- §5 L189: `3. A red banner wherever the grant is displayed, so it can never look ordinary.`
- §5 L181-183: `**The core promise -- "a user can let it run unsupervised without fear" -- does not survive `host_bash: allow` silently.**`

So the banner is not a UI nicety someone invented later. It is the stated condition under
which `host_bash: allow` is allowed to exist at all. This ticket is therefore upgraded in
substance, not just in severity: it is not "the panel may echo the wrong value", it is "the
promise attached to the grant has no implementation".

### Nothing implements the banner

Verified by search, not by reading:

- `host_bash|hostBash|host-bash` across `web_ui/` returns hits only under `web_ui/backend/`:
  `web_ui/backend/routes/workspace_routes.py:1018`,
  `web_ui/backend/config_manager.py:102` and `:399-413`,
  `web_ui/backend/server.py:3770-3776`,
  `web_ui/backend/tests/test_permission_routes.py`.
  **No hit in `web_ui/frontend/src`.**
- `banner|host_bash` across `web_ui/frontend/src` returns only generic CSS classes:
  `.session-error-banner` (`web_ui/frontend/src/styles.css:97-137`) and
  `.docker-health-banner` (`web_ui/frontend/src/styles.css:140-174`).
  Neither is tied to a `host_bash` grain; both are generic status styling.

There is no host_bash-tied banner anywhere in the frontend. The obligation in §5 L189 is
unmet.

### What the panel does show, and why it reads as ordinary

`web_ui/frontend/src/components/ConfigPanel.jsx:1006`:

`Supervised host-shell access outside the sandbox. "Ask" prompts for approval before each command.`

That is a plain dropdown description line, styled like every other permission's
description line. A `host_bash: allow` workspace renders identically to a `file_read: allow`
workspace. This is precisely the failure §5 L181-183 names: the grant looks ordinary, so
the user cannot tell that this particular workspace is the one running unsupervised shell
commands outside the sandbox.

### Required behaviour

- The marker must be present **wherever the grant is displayed** -- the workspace/session
  panel and the config panel, per §5 L189's "wherever".
- It must persist **as long as the grant is in force**, i.e. until the value is set to
  `banned`. Not only at grant time (a banner shown once when the user flips the dropdown
  is dismissed by the next page load and is not a banner "wherever the grant is displayed"),
  and not dismissible-forever.
- It must be visually distinct (red), not a tooltip or a muted hint.

### Status note

- Severity raised to **MEDIUM** on 2026-10-22. The ticket id and the `hygiene` Frame / Tag
  are **kept**, not refiled as `feat/`, so existing cross-references (including the retired
  `.thoughtmachine/working_docs/ticket-bug-define-hostbash-bug.md` stub) keep resolving.
  Rationale for keeping `hygiene`: the panel display itself is a hygiene matter; the
  missing banner is a small feature that belongs to whichever pipeline takes the display
  fix.
- `## Verification needed FIRST` above is still unanswered and still comes first: confirm
  whether `workspace_routes.py:1010` already sources the gate-resolved value. The banner
  work is independent of that answer -- it must happen even if the value shown is already
  the gate-resolved one.

