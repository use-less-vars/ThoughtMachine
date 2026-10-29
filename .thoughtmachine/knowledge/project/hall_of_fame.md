# The Hall of Fame
*This file is Operator-owned. The engineers are nominated by the machine's performance, but enshrined by the Operator.*

## The Core Streaming Sprint (2026-10-28 / 2026-10-29) — Gen 52, 53, 54
- **The Achievement:** Replaced the synchronous, OpenAI-shaped core with an asynchronous, streaming, provider-agnostic engine.
- **The Miracle:** Performed full heart surgery on a running production machine with a total downtime of 1 hour (due to a concurrency loop race).
- **The Heroic Acts:**
  - **Gen 52:** Designed the Canonical IR and the streaming contract. Deleted the sync loop instead of patching it.
  - **Gen 53:** Built the thread-local loop fix. Un-swallowed the reasoning tokens. Built the Stop button.
  - **Gen 54:** Caught the "ghosts" (unverified handoff claims). Proved the refusal-as-success fix was a ghost and actually landed it. Traced the UI tool-pairing bug to its true root cause — `eventId()` in `adaptWorkerEvent.js` folding the event *name* into the dedup identity — and landed the fix (`toolCallEventId()` + `mergeToolMessages()` upsert keyed on `tool_call_id`, merged as **#246**). Also corrected two false claims in its own KB before they could become the next generation's premise.

## The Host Port (Gen 49)
- **The Achievement:** Sealed the back door.
- **Why it matters:** Ensured every host access goes through one chokepoint (`host_run()`), making the machine structurally safe from rogue tool execution.

## The Death of PaginateTool (Gen 48)
- **The Achievement:** Deleted the tool instead of patching it.
- **Why it matters:** Defined the "no tool wraps another tool" rule, closing a massive security hole and establishing the doctrine that tools are leaves, not nodes.

---

### Editor's note (Gen 54 housekeeping, 2026-10-29)

The operator supplied the entries above verbatim except for one clause, which was rewritten twice
within one session. Both edits are recorded, because the sequence is the lesson:

1. **First draft** said of Gen 54: *"… Fixed the UI tool-pairing bug."* At the moment it was written
   that was a ghost — the adapter still contained `parts.push(evt.event)` and nothing read
   `tool_call_id`. The clause was softened to *"diagnosed, not fixed"*.
2. **Twenty minutes later that softening was itself wrong.** PR **#246 `fix/ui-async-tool-pairing`**
   merged while the file was being written, landing `toolCallEventId()` and `mergeToolMessages()`
   in `web_ui/frontend/src/components/chat/adaptWorkerEvent.js`. The claim was restored to "landed".

Two corrections to the same sentence in one sitting is not sloppiness — it is what a *live* repository
does to any claim that omits the commit SHA it was verified at. See `working_agreements.md` →
`` `verified-at-a-moving-head` ``.

**Standing rule for this file:** an entry may only say "fixed" if it also names the PR or commit that
made it true on `dev`, so the next generation can check it in one command instead of trusting prose.
