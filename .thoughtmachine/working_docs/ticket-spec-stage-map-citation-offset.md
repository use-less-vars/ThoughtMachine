# Ticket: `docs/agent-core-spec.md` stage map is offset by ~+320 lines from current `agent/core/agent.py`

Status: open

## Problem

The line citations in the `docs/agent-core-spec.md` stage map (Stages 7–11) were not updated when `agent/core/agent.py` grew, so they point at the wrong code — off by roughly **+320 lines**. A reader who trusts them lands in unrelated code, and the drift is not uniform: the classifier region grew by several lines of its own, so a single constant offset does not fix every citation.

Concrete example: the document gives Stage 9 as `1305-1355`, but the real span is `1635-1687` (the tool phase runs from the `tool_call_start` emission loop through the buffered-warnings flush; the enclosing `if tool_calls:` guard itself sits at `1628` and the trailing debug log at `1688`).

## Evidence

Document citations that are stale, with the current reality:

| Document cites (`agent/core/agent.py:`) | Current location |
| --- | --- |
| `1305-1355` (Stage 9 span) | `1635-1687` |
| `1312-1322` (`tool_call_start` loop) | `1635-1645` |
| `1323` (execution call) | `1646` |
| `1324-1333` (result classifier) | `1651-1665` |
| `1336-1337` (`turn_transaction.commit()`) | `1668-1669` |
| `1341` (`tool_call` yield) | `1670-1673` |
| `1345` (`tool_result` yield) | `1674-1677` |
| `1348-1355` (buffered-warnings flush) | `1678-1687` |
| `1357-1367` (Stage 10 entry) | ~`1689-1699` |
| `1382-1384` (`_apply_summary_pruning` region) | ~`1714-1716` |
| `1424-1425` (Stage 11 commit) | ~`1756-1757` |

Current file: `agent/core/agent.py` is 2,007 lines.

**Only the Stage 9 paragraph and the classifier citations have been corrected so far**, so the document is presently a mix of old and new numbering — Stage 9 now cites `1635-1687` while Stages 7, 8, 10 and 11 immediately above and below it still cite the old ranges. That inconsistency is itself a reading hazard and is why this needs one sweep rather than incremental fixes.

## Suggested fix

A single mechanical sweep, not paragraph-by-paragraph repair: re-derive every `agent/core/agent.py` citation in `docs/agent-core-spec.md` from the file itself and re-verify each one before writing it.

1. Extract every `` `agent/core/agent.py:NNN` `` / `` :NNN-MMM `` reference in the document with line numbers of the document itself.
2. For each, locate the construct the surrounding prose describes (stage heading, yield, call, guard) and read the real range.
3. Rewrite all of them in one pass, so the document never sits in a half-converted state.
4. Add a short "citations verified against `<commit>`" note at the top of the stage map, so future drift is detectable by date rather than by accident.

Do the same for `agent/core/tool_executor.py` and `security/security_gate.py` citations in the same document while the sweep is open — they show the same symptom (`tool_executor.py:408/428/526/540/695` around the gate-exit shape discussion).
