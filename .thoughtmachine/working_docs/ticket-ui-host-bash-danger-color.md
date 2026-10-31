# Ticket (UI): host_bash tool calls render exactly like harmless ones

**Filed:** 2026-10-31, read-only pass over `web_ui/frontend/src` at `docs/w4-kb-braindump`.
**Severity:** MEDIUM — the transcript is the operator's evidence surface; the single widest
blast-radius action in the product is visually indistinguishable from a file read.
**Status:** open; verified by read.

## Finding

Transcript styling is keyed by **message role only**, never by tool. The map is a frozen
literal — `web_ui/frontend/src/components/chat/MessageBubble.jsx:42-52`:

```js
const ROLE_STYLE = {
  user:        { className: 'message-user',        label: 'You' },
  assistant:   { className: 'message-assistant',   label: 'Assistant' },
  tool_call:   { className: 'message-tool-call',   label: 'Tool Call' },   // :45
  tool_result: { className: 'message-tool-result', label: 'Tool Result' },
  ...
```

and it is applied at `MessageBubble.jsx:244` as `className={"message " + style.className}`.
One CSS rule serves every tool call: `web_ui/frontend/src/styles.css:397`
(`.message-tool-call`), with the nested pre blocks at `:1902-1903`. There is no per-tool
class, no tool-name lookup, no severity channel — a `host_bash` step and a `read_file`
step are the same bubble.

The backend already knows `host_bash` is different, so no new authority has to be invented:

- `tools/host_bash_tool.py:13-15` — the effective permission grain for `host_bash` must be
  `"ask"` or `"allow"`; unset defaults to banned.
- `tools/host_bash_tool.py:268-290` — fail-closed grain refusal at call time.
- `architecture_audit_2026-09.md` R5 row — `tools/host_bash_tool.py:290`
  `subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)` with
  the full ambient environment; called out there as *"the widest blast-radius I/O in the
  tree"*.

Danger tokens already exist in the stylesheet vocabulary, so this is not a design-system
change: `var(--danger, #f38ba8)` at `components/FolderBrowser.jsx:288`,
`var(--text-danger, #e74c3c)` at `components/LoggingPanel.jsx:179` and `:332`.

## Blast radius

When reviewing a finished session — or a worker's output — the operator cannot see at a
glance which step took the host shell with their own environment, their own credentials,
their own `PATH`. Everything that makes `host_bash` worth gating at call time is invisible
at review time, which is exactly when the second look happens.

## What is not covered

- This is **transcript rendering only**. The ask/gate dialog is a separate surface
  (`components/SecurityDialog.jsx`) and is not in scope.
- No claim about *how* the tool name reaches the frontend — the tool-call message carries
  the name in its rendered args (`MessageBubble.jsx:108-114` renders `tool-call-args`), but
  I did not verify the structured field name, so "read the tool name off the tool_call
  message" is **unverified — needs confirmation**.
- Not proposing a colour. The ask is a *marker* (danger token + the tool name shown as a
  hazard), not an aesthetic.

## Dedupe

- `ticket-hygiene-panel-host-bash-display.md` — different surface: that one is the
  *workspace/session panel* echoing the declared rather than gate-resolved `host_bash`
  grant. It is about permission display, not transcript rendering. Both are worth having;
  neither substitutes for the other.
- `ticket-bug-host-bash-cwd-wrong-workspace.md`,
  `ticket-bug-host-bash-double-prompt-on-ask.md` — behaviour, not display.
- `ticket-bug-define-hostbash-bug.md` — allowed-permission semantics.
