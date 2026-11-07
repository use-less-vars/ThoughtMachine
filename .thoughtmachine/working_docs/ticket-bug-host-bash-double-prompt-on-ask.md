# Ticket: bug/host-bash-double-prompt-on-ask

**Filed:** 2026-10-22, bookkeeping chunk A
**Severity:** MEDIUM — the user is asked to approve the same command twice; every
`host_bash` command under `ask` produces two approval records for one act
**Status:** open
**Fix type:** behavioural — one approval transaction per approved act

## The defect

With `host_bash` set to `ask`, a single `host_bash` invocation produces **two** user
approval prompts. Both are produced by code that is individually correct; the defect is
that there are two of them.

The two chokepoints:

**Chokepoint 1 — the generic outer gate.** `security/security_gate.py`,
`check_required_categories` (def at L1480-1491), which evaluates each declared
`required_categories` entry via `_value_satisfies` (L1565) and, on an `ASK` verdict
(`if result == "ASK"`, L1601-1603), denies in worker/no-bus contexts (L1609-1613) or
otherwise builds a `SecurityPromptEvent` (L1623-1633) under
`# ── Prompt the user for approval` (L1615), looking the category up in the registry
(L1620).

**Chokepoint 2 — the tool's own gate.** `tools/host_bash_tool.py::_request_approval`
(L170-205): builds a description `Run host shell command: {command} (workspace: …, session: …)`
(L189-192), publishes an event carrying `"capabilities": ["host_bash:execute"]`
(L193-203), and publishes at L205. It is reached from `execute()` at L293
(`decision = self._request_approval(cmd)`), after the grain gate (L273-282) and the
worker-context denial (L283-292, message
`host_bash: ask requires interactive approval; not available in worker context`).

## Why chokepoint 1 fires for `host_bash: allow`

`tools/host_bash_tool.py:88` declares:

    required_categories: ClassVar[List[str]] = ["host_bash:allow"]

`security/security_gate.py:1559` splits that into
`category, required_value = req.split(":", 1)`, so `required_value == "allow"`.
`security/gate_helpers.py:37` `_value_satisfies` then finds that `"allow"`:

- is not in the false/no/banned set,
- is not true/yes,
- is not `outbound`,
- is not a grant alias,
- is **absent from `GRANT_LEVEL_RANKS`** (`security/resource_catalog.py:141-150`, which
  knows `banned`, `read`, `ask`, `connect`, `write`, `write_on_feature_branch`,
  `outbound`, `full`),

so `required_level` is `None`, and the `allowed_str == "ask"` branch returns its
`sentinel_ask` when `required_level is None`.

Executed on the live tree (`_value_satisfies` called directly):

| allowed_str | required_value | result |
| --- | --- | --- |
| `allow` | `ask` | `'ASK'` |
| `allow` | `full` | `False` |
| `allow` | `banned` | `False` |
| `allow` | `write` | `False` |
| `ask` | `ask` | `True` |

So `host_bash: allow` renders as "the value is a level I do not recognise, and the allowed
string is `ask` — ask the user". The fifth row shows the level comparison itself works;
only the unrecognised `"allow"` keyword misroutes.

## Why the first prompt looks empty

`HostBashTool` defines no `describe_action` (grep of `tools/host_bash_tool.py`: no match),
so the executor's
`description=getattr(tool_class, 'describe_action', lambda a: '')(arguments)`
(`agent/core/tool_executor.py:472`) supplies `""` to the event, and the event carries
`"description": description` (L1623-1633). The UI then falls back to generic text
(`web_ui/frontend/src/components/SecurityDialog.jsx:112`, `SessionTab.jsx:906`).

Consequence: the operator sees an empty/opaque first prompt and a fully-described second
prompt (`Run host shell command: …`) for the same command. That is what makes it read as
a UI glitch rather than two approval transactions.

## How it was found

Reading `host_bash_tool.execute()` end to end and asking where an approval can come from,
then executing `_value_satisfies` on the live tree for the `("allow", X)` matrix above
rather than reading it.

## Why it matters

- **Two approval records for one act.** The audit trail cannot distinguish "one command,
  approved twice" from "two commands, one approval each". A security-relevant log that
  over-counts approvals is not a record of what happened.
- **Second-prompt fatigue.** Approving the second prompt is the natural next action; the
  second prompt is the one that carries the command text, and the first carries nothing.
  Habituation to prompts is exactly what the approval gate exists to prevent.
- **Cost of a fix is asymmetric.** Both gates are correct in isolation, so no test is red
  and no invariant is visibly broken — which is why this has survived.

## Fix

Do not implement anything here. Two shapes, one recommended.

**(i) Single transaction — RECOMMENDED.** The in-tool gate consumes the outer gate's
approval decision instead of raising a second one: the tool learns that
`host_bash:ask` was already approved for this act and proceeds to its own grain/grain
checks without publishing a second event. One prompt, one approval record, the command
text in it.

**(ii) Delegate outward.** The outer gate stops evaluating `host_bash:ask` itself and
defers that category to the tool's own gate, so only `_request_approval` prompts. Smaller
change to the tool, but it puts a tool-specific special case in the generic gate — the
generic gate then has one category it does not evaluate, which is a precedent worth not
setting.

Either way the invariant to preserve: **one approved act ⇒ exactly one approval event**.

Adjacent facts a fix must not break:

- `allow` currently prompts **zero** times (`_value_satisfies("allow", "allow")` is an
  exact match → `True`), which is the intended reading of `allow`.
- `banned` never reaches either prompt: the tool's grain gate denies first (L273-282).
- The double prompt is specific to `ask`.

## Test

The gap, stated plainly: **every** test in `tests/test_host_bash_tool.py` drives
`execute()` with `_request_approval` mocked (`:151`, `:221`, `:236`, `:252`, `:272`,
`:293`). No test exercises `executor → outer gate` for a `host_bash:allow` tool at all.

So the missing test is not "does `_request_approval` publish an event" (covered) nor "does
the gate ask for `ask`" (covered elsewhere) but the seam: drive one `host_bash` call
through the executor with `host_bash = ask` and assert **exactly one** approval event is
published for one command. That test is the regression test for the fix, and it should be
written so it would go red on today's tree.

## Bearing

Related, not duplicates:

- `.thoughtmachine/working_docs/ticket-hygiene-panel-host-bash-display.md` — the panel can
  show the declared value rather than the enforced one, and no banner marks `allow`.
  Same area (what the operator is told about `host_bash`), different defect: that one is
  about display, this one is about the number of approval transactions.
- `.thoughtmachine/working_docs/ticket-bug-define-hostbash-bug.md` — retired stub; this
  ticket is one of the two that replaced the single "define hostbash bug" framing.

## MUST NOT TOUCH

- `security/gate_helpers.py::_value_satisfies` — the `("allow", "ask") → "ASK"` behaviour
  is load-bearing for other tools' declared categories; it is the *reason* this is a
  double prompt rather than a bug in the tool.
- `security/resource_catalog.py::GRANT_LEVEL_RANKS` — adding `allow` to the ranks would
  change every category that declares it, far beyond `host_bash`.
- The worker-context denial at `tools/host_bash_tool.py:283-292` — must keep denying;
  the fix must not make an in-worker `ask` reachable.
