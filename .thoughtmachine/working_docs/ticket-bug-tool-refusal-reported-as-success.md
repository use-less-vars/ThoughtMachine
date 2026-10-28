# Ticket: bug/tool-refusal-reported-as-success

**Filed:** 2026-10-27, from a scripted single-turn reproduction
**Category:** bug
**Severity:** MEDIUM — the event stream lies about the outcome of a refused call
**Status:** fix built and pushed, NOT merged — `c37943a1` on `fix/tool-refusal-structure` (base `12bd6408`); full gate 5592 passed, 66 skipped, 5 xfailed, exit 0. The prose marker this ticket describes ("TOOL CALL REJECTED") still has no producer anywhere in the tree: the fix carries the verdict structurally as `'success': False`, and `agent.py` reads that before falling back to the sniff.

## Summary

A tool call whose arguments fail schema validation is refused, and the resulting
`tool_result` event is reported with `success: True`.

1. `tool_class.model_validate(tool_args)` raises `ValidationError`
   (`agent/core/tool_executor.py:379`). The handler converts it to prose:
   `{'result': f'Invalid arguments: {e}', 'tool_type': 'normal'}` (`:696-697`).
2. `agent/core/agent.py:1330` decides `success` by sniffing the result string:
   `result.startswith('❌') or 'TOOL CALL REJECTED' in result or 'Error executing
   tool' in result`. `Invalid arguments: 1 validation error for ReadFile ...`
   matches none of the three, so `success` stays `True` (`:1327`) and `error` stays
   `None` (`:1328`).

## Reproduction

One scripted turn: a `ReadFile` call with invalid arguments, driven end-to-end
through the real turn loop.

    tool_calls  [('ReadFile', True)]
    result      'Invalid arguments: 1 validation error for ReadFile ...'

The tool was not executed — correct — and the event stream still reports success.

## Why the string match is the defect

The literals at `:1330` are the *formatting* of unrelated producer paths; the
`Invalid arguments` message was added later and the sniffer was not updated. Two
tell-tale drifts: `tool_executor.py:699` emits `**Error executing tool**: ...` and
survives only because `:1330` does a substring test; and `TOOL CALL REJECTED` has
no producer anywhere in the tree (grep, `.py` excluding `.venv`) — the sniffer
matches a literal nothing emits. Every future refusal message inherits the bug
unless failure is carried as data.

## Suggested fix

Return a structured failure from the validation path — e.g.
`{'result': ..., 'success': False, 'error': str(e)}` — and have `agent.py:1325-1333`
read the field instead of parsing the string. Whether the legacy sniff stays as a
fallback is a decision; if it does, each remaining producer needs naming.

## Pinned by

`tests/test_puppet_llm_fixture.py::test_d2_first_tool_call_without_valid_arguments_is_not_executed`
(`:69`), marked `@pytest.mark.xfail(strict=False)` (`:59-67`). The mark is
deliberately non-strict so the fix surfaces as an XPASS rather than a silent no-op.
**When this ticket is fixed the mark must be tightened** (removed, or `strict=True`),
not left behind as a passing xfail.

As of `c37943a1` the xfail count is unchanged (5 xfailed, 0 xpassed), so this pin was not tightened by the fix; tightening it is still owed.

## Filed

2026-10-27. Verified by read at `6011d97e`; observed in a scripted turn, no live
session involved.
