# Design pass — streaming and the canonical IR

*Written 2026-10-27, at the close of Wave 0 (API-key redaction, Anthropic tool-result, ConfigService delegation all on `dev`). Design only; no code, no tokens, no re-finding.*

## 1. The claim

The core's problems are not many. They are one shape.

The core calls the model once per turn, synchronously, and treats the response as a whole object in the OpenAI Chat-Completions shape. Every concrete problem the last month surfaced is a consequence of that shape:

- Anthropic's `tool_result` content blocks do not fit an OpenAI `role: "tool"` message.
- DeepSeek's `reasoning_content` replay is a per-turn state obligation the sync path re-normalises by hand.
- GPT-6 tool calling requires the Responses API, whose `output`/`Items` shape is not a flat `messages[]`.
- `Respond` + `SummarizeTool` in one batch silently drops the summary (spec `docs/agent-core-spec.md` §0.1 D1) — a batch-shape bug.
- The first tool call of every turn runs unguarded (spec `docs/agent-core-spec.md` §0.1 D2) — a "when does usage arrive" bug.
- No streaming, because the call returns a whole response.
- No cancellation, because you cannot interrupt a blocking call.
- No sysprompt addendum, because there is no moment in the sync loop to receive one.
- No external injection, same reason.
- No multimodal, because the request is OpenAI-shaped and OpenAI shape is text-and-tool-calls.

The design does not patch these. It changes the one boundary they all live against.

The change is: **the LLM call becomes a stream of typed events, and messages become a canonical internal representation that every provider adapter translates into and out of.** Everything else in the core — pruning, mailbox, transaction commit, refusal-as-result — is already correct and is *protected* by this pass, not touched by it.

## 2. The canonical IR

Today the internal message shape *is* the OpenAI wire shape; `ToolFormatConverter.to_openai()` returns its input. The consequence is that every vendor quirk lives in the generic adapter as a special case, and every new vendor is a new string-match.

The IR replaces this with a typed representation. The IR is the shape *between* the turn loop and the adapter. Adapters convert IR → wire and wire → IR. The turn loop never sees a wire shape.

**Types.**

- `CanonicalMessage` — `{ role, content: list[ContentBlock], reasoning?: str, reasoning_signature?: str }`
- `ContentBlock` is a union: `TextBlock`, `ImageBlock`, `ToolUseBlock { id, name, input: dict }`, `ToolResultBlock { tool_use_id, content, is_error }`, `ReasoningBlock { text, signature? }`.
- `CanonicalToolCall` — `{ id, name, arguments: dict, raw_arguments?: str }`. **`arguments` is always a `dict`.** A vendor that returns a JSON string (OpenAI, DeepSeek) is parsed by its adapter; a vendor that returns a parsed object (Anthropic) uses it directly. The turn loop never handles a JSON-string-encoded argument.
- `CanonicalToolDefinition` — `{ name, description, parameters: JSONSchema }`. This *is* today's tool shape, because the OpenAI tool schema is the de-facto standard and no provider wants a different one.

**Invariants.**

1. `arguments` is always a `dict`. It is never a string, never None.
2. `content` is always a list of blocks, even when there is exactly one text block. Single-string content is a wire-format special case, not an IR state.
3. A `ReasoningBlock` with a `signature` must be replayed verbatim on the next request for the same message. A `ReasoningBlock` without a signature is an annotation, not state, and may be dropped. Because `session.user_history` holds wire dicts rather than IR (§7, "Where `user_history` lives"), the obligation lands on the path that *writes* history, not on a history conversion. The assistant message is hand-built as a wire dict (`agent.py:1282-1288`) and appended by `TurnTransaction` (`turn_transaction.py:48`, `:91`), so the adapter converts **only the current turn's new content** IR → wire and writes `reasoning_signature` onto the wire dict it produces. Historical messages are replayed to the provider verbatim, with nothing to preserve: the signature is already on them.
4. A `ToolResultBlock` carries the `tool_use_id` of the `ToolUseBlock` it answers. The turn loop never re-serialises a tool result; it appends a `ToolResultBlock` to a `CanonicalMessage` with `role: "user"`.
5. The IR is provider-neutral. A `CanonicalMessage` written by the turn loop is valid input to any adapter. An adapter that cannot express a block (e.g. a text-only provider meeting an `ImageBlock`) raises a typed `UnsupportedContentError` rather than silently dropping it.
6. `arguments` is always a dict on `ToolCallComplete`. A streamed tool call whose accumulated arguments never parse to a JSON object produces a `ToolCallComplete` with `parse_error: str` set and `arguments: {}` (empty dict, not None). The turn loop turns a `parse_error` into a tool-result error event (refusal-shape, spec §8.6), not an `Interrupted` terminal. `Interrupted` is reserved for cancellation and provider failure; a malformed tool call is an ordinary failure the model can react to.

**What this shape dissolves.** The Anthropic `tool_result` round-trip (D1 fix from Wave 0) becomes the natural expression of `ToolResultBlock` — the adapter prepends the block to a user-role message; the turn loop does not change. DeepSeek `reasoning_content` replay becomes "the adapter preserves `ReasoningBlock.signature` on serialise; if absent, it emits the raw text field." GPT-6 Responses API becomes "the adapter walks the `Items` array and emits `TextBlock` / `ToolUseBlock` / `ToolResultBlock` per item; the turn loop does not know a Responses API exists."

**What this shape accommodates but does not dissolve.** Providers that forbid reasoning on tool-call turns (some DeepSeek modes) still require the adapter to *drop* the reasoning field on that turn; the IR expresses what *should* be replayed, the adapter expresses what *is legal*. Providers that require `tool_choice: "required"` to make tools fire (some Gemini shapes) are the adapter's concern. The IR tells the adapter what the user asked; the adapter tells the provider what it accepts.

## 3. The streaming contract

The turn loop is a generator. Under streaming it becomes a generator over typed `StreamEvent`s from the adapter, not a single `LLMResponse`.

**The event union.**

- `TextDelta { text }` — a chunk of assistant text.
- `ReasoningDelta { text }` — a chunk of reasoning text; may arrive interleaved with `TextDelta` (Anthropic, DeepSeek).
- `ToolCallStart { id, name }` — the model has begun producing a tool call.
- `ToolCallArgumentsDelta { id, arguments_delta }` — incremental argument bytes; the adapter emits a stream of these per tool call.
- `ToolCallComplete { tool_call: CanonicalToolCall, parse_error?: str }` — the arguments are complete or definitively malformed. If `parse_error` is set, `arguments` is `{}` and the turn loop treats the call as a refusal-shape error result. The tool is dispatched either way; the executor sees a normal refusal result on parse error.
- `UsageUpdate { input_tokens, output_tokens }` — the provider reported authoritative counts.
- `Interrupted { reason }` — a *first-class terminal*. The stream was closed by the caller (cancellation) or the provider (error). Not an exception; a signal.
- `Done` — the stream completed normally.

**Why `Interrupted` is first-class.** A synchronous call cannot be interrupted — the caller blocks until the provider returns, and cancellation is a thread kill. A stream can be closed at any moment. `Interrupted` is the shape that makes "the model was stopped mid-generation" an ordinary outcome the turn loop handles, not an edge case it catches. This is why cancellation cannot exist without streaming, and why streaming is not a performance feature but a capability substrate.

**What the events are not.** `StreamEvent`s are the contract between the adapter and the turn loop. They are *not* the `EventBus` events (§7 of the spec) — those are the observable channel that reaches the frontend, and they stay. The turn loop translates `StreamEvent`s into `EventBus` events and into generator yields to the bridge. Two contracts, named, not merged.

## 4. The turn loop under streaming

The turn loop's job — apply pending config, assemble request, call model, classify response, dispatch tools, commit, prune, emit — survives unchanged. What changes is the "call model, classify response" step and the timing of the commit.

**Commit is two-phase, and streaming preserves both phases.** Today the turn loop commits twice per turn, and the ordering is the protection:

1. *Assistant, before dispatch.* `turn_transaction.commit_assistant_only()` (`agent/core/agent.py:1295`) fires once the assistant message has been added and **before any event is yielded** — see the comment at `agent.py:1291-1293` and the `token_update` yield two lines later. It sets `_assistant_committed` (`agent/core/turn_transaction.py:36`, set at `:98`) without closing the transaction, which leaves room for the tool results to be committed into the same `TurnTransaction`.
2. *Tool results, after dispatch.* `turn_transaction.commit()` (`agent/core/agent.py:1337`) fires **after** tool execution and **before** the `tool_call` / `tool_result` yields (comment at `agent.py:1334-1335`). Because `_assistant_committed` is set, `commit()` takes the tool-buffer-only branch (`agent/core/turn_transaction.py:116-118`) and does not re-append the assistant message. The `RuntimeError('Transaction already committed')` guard against a genuine second close exists on **both** close paths — `commit_assistant_only()` at `:84-85` as well as `commit()` at `:114-115`; the `:142` cited by the previous revision is `self._committed = True`, the flag being *set*, not a raise. Nor are those the only closes in a turn: the no-tool turn closes at `agent.py:1424-1425` and the pause path closes a throwaway transaction at `agent.py:1266`. A streaming rewrite has to place all four, not just the two above.

The streaming path keeps both phases, at both places. On `Done`: the assistant message accumulates from `TextDelta` / `ReasoningDelta` → `commit_assistant_only()` → `token_update` and `turn` are yielded → each `ToolCallComplete` is dispatched synchronously → `commit()` → `tool_call` / `tool_result` are yielded. On `Interrupted`: phase 1 fires with whatever partial assistant message exists, and phase 2 fires for any tool call whose `ToolCallComplete` had already dispatched.

**The uncommitted window stays one statement wide, not one stream wide.** The invariant is unchanged — no event is yielded for data that is not yet in `user_history` — and streaming exercises it *more often* (every delta group is now a yield point) rather than differently. Collapsing the two phases into a single commit at stream close would widen the window to the whole stream and is out of scope for this pass.

**The partial-vs-empty discriminator does not exist yet.** The earlier revision of this document claimed the case was covered by `TurnTransaction.has_assistant_message()`, "which already exists". It is not: that method is `return self._assistant_message is not None` (`agent/core/turn_transaction.py:171-173`) — it tests *presence*, not content. A cancelled turn that emitted only `ReasoningDelta`s, or that added an assistant message with `content: ''`, passes that test and commits an empty message into `user_history`, which is the corruption the paragraph above forbids. Three ways to close it, in order of preference:

- **(i) Recommended — extend `has_assistant_message()`** so it also requires non-empty content (or a non-empty `tool_calls` list), not merely non-`None`. One predicate, one place. The call sites are fewer than the previous revision claimed: there are exactly two, both in `agent.py` — `:1294` and `:1424`. `turn_transaction.py:116-122` was named as a third and is not one; it is the `_assistant_committed` flag branch. `:1424` is the no-tool close, and that is where the stricter reading actually bites. `:1294` is a tautology today (`add_assistant_message` runs at `:1289`, five lines above) and stops being one under the stricter predicate — an empty assistant message would no longer be committed before the `token_update` yield — so (i) must come with a decision at `:1294`: when the predicate says no, commit nothing *and* yield nothing, rather than yielding and skipping the commit, which would break the uncommitted-window invariant. The remaining presence guards (`:86-87`, `:120-121`) are untouched and out of scope for this pass.
- **(ii) Introduce a separate `has_content()`** and leave `has_assistant_message()` as a presence test. Cleaner naming, but two predicates that can disagree, and the streaming path has to remember which one it is asking.
- **(iii) Reuse the pause-path pattern.** The existing pause path at `agent.py:1257-1266` already refuses to route its grace turn through the accumulated message: it hand-builds a fresh `assistant_msg`, drops `tool_calls` with the comment "Don't include tool_calls — they weren't executed", and commits that through a throwaway `TurnTransaction`. It works, but it is a workaround for the gap rather than a fix, and copying it into the cancellation path gives two hand-built-message sites that must be kept in sync.

Take (i). The pause path may then simplify to a plain `commit_assistant_only()`; this pass does not require that cleanup.

**Tool dispatch fires on `ToolCallComplete`, not on `Done`.** A tool can begin executing while the model is still producing subsequent tool calls: the loop is consuming events, and each `ToolCallComplete` goes straight to the tool executor. The dispatch table is the tool executor's existing `execute_tool_calls` — unchanged; the difference is *when* it is called.

### Not in scope: interrupting a running tool

An earlier revision of this document claimed dispatch-per-`ToolCallComplete` made "interrupt a long tool" usable. It does not, and that claim is withdrawn. Dispatch is synchronous inside the same loop that consumes the stream: while `execute_tool_calls` is running, the loop is not reading events, so there is no event boundary at which a `break` could be observed and no checkpoint at which a stop flag could be read. The cancellation described in §5 is mid-*stream* — between events, which means between pieces of model output, not during a tool's execution.

Interrupting a running tool needs a different architecture, not a different event ordering: the tool runs on a sub-agent or a separate thread, the stream reader keeps consuming while it runs, and results are joined back into the turn. That is a Wave 5-or-later capability and it is explicitly out of scope here. A long-running tool blocks cancellation in the streaming path exactly as it blocks it in the sync path; this pass does not improve that, and the document no longer claims to.

**D1 dissolves explicitly.** The bug (`Respond` + `SummarizeTool` in one batch drops the summary) exists because the sync loop does `if final_detected: return` and the summary branch is unreachable. Under streaming, `Respond` arrives as a `ToolCallComplete` and `SummarizeTool` arrives as a *separate* `ToolCallComplete`. Two candidate mechanisms:

- *(a) Defer terminal emission.* `Respond` sets a `final_pending` flag; the loop does not terminal-yield `agent_responded` until `Done`. `SummarizeTool`'s `ToolCallComplete` still dispatches, the summary is applied, and only then is `agent_responded` yielded.
- *(b) Continue after Respond.* If `final_detected` is true but a summary was also requested, process the summary first, then terminal-yield.

**Recommendation: (a).** The rule "no tool can suppress another tool" is general; it scales to every future pair of tool calls without a special case. (b) requires the loop to know about specific combinations. (a) is the shape that makes D1 *structurally impossible* rather than *specifically fixed*. The cost of (a) is that `agent_responded` is delayed by a few events — the frontend must handle a partial render between `TextDelta`s and `agent_responded`, which it needs to handle anyway for streaming.

**D2 fixes naturally.** `UsageUpdate` fires on every provider usage report (OpenAI sends one per response, Anthropic per response, DeepSeek at end-of-stream). The turn loop calls `state.update_token_state(usage.prompt_tokens)` on every `UsageUpdate`, not once at response return. The first tool call of every turn is guarded against the true prompt-token count because the true count arrives *before* the first `ToolCallComplete`. This is not a special case — it is what "usage arrives during the stream" means.

## 5. Cancellation

`break` out of the `for event in stream:` loop, then `stream.close()`. The adapter closes the HTTP connection. The provider stops generating.

**Two cancellation paths, not one — and the `finally` already in the code is neither of them.** The `finally` at `agent/core/agent.py:1465` is *query*-level: it sits at the same indentation as the `for turn in range(self.config.max_turns)` loop (that loop opens at `agent.py:982`) and its entire body is `self.state.execution_state = ExecutionState.READY` plus a log line. It runs once per `process_query` call, after the turn loop has exited, and it cannot observe a mid-stream cancel because the stream lives inside a turn. Naming it as the cancellation point would be wrong twice over: it fires too late to close the stream, and it cannot tell a cancelled turn from a completed one.

What cancellation actually needs is these two, kept separate:

1. **Between turns — the existing checkpoint, unchanged.** `if self.stop_check and self.stop_check():` at `agent.py:991`, at the top of each turn, transitions to `ExecutionState.PAUSING`. This is the only cancellation that exists today. Streaming does not change it.
2. **Mid-stream — a new turn-level `finally`.** A `try/finally` *inside* the `for turn in range(self.config.max_turns):` body (`agent.py:982`), wrapping the event-consuming loop, so a `break` out of `for event in stream:` always reaches the close.

   **A `finally` fires on every exit, not only on `break`.** The turn body returns early in ten places — `agent.py:1005`, `:1169`, `:1188`, `:1206`, `:1221`, `:1234`, `:1280`, `:1381`, `:1421`, `:1446` — plus the loop-level `return` at `:1464`, and it propagates exceptions. Without a discriminator the cancellation body would run on every normal turn end. So the turn body keeps one local flag:

   - `turn_completed = False` at the top of the turn body; set `True` on the `Done` path, immediately before the commits of §4's phase 1 and phase 2. Only the `break` out of `for event in stream:` and an exception leave it `False`.

   The `finally` body then does exactly this, and **yields nothing**:
   - If `turn_completed` is `True`, do nothing at all. The normal path has already committed.
   - Otherwise, set `interrupted_terminal = {'type': 'interrupted', 'reason': 'user'}` and commit: `commit_assistant_only()` only if the transaction holds a partial assistant message *with content* (§4's discriminator) **and** neither `_committed` nor `_assistant_committed` is set; `commit()` for tool results already dispatched, and only if `_committed` is `False`.

   **Idempotence is a real hazard here, not a formality.** `commit_assistant_only()` tests `self._committed` (`turn_transaction.py:84-85`), *not* `self._assistant_committed`, so calling it a second time appends the assistant message to `user_history` again (`:88-91`) instead of raising. And `commit()` raises `RuntimeError('Transaction already committed')` on a second close (`:114-115`) — inside a `finally` during a normal `return`, that converts a completed turn into a crash. The streaming path must therefore check both flags, or `TurnTransaction` grows an `is_assistant_committed()` accessor and both call sites use it. Either is acceptable; guessing is not.

   **Nothing is yielded from the `finally`.** Yielding there fails under `GeneratorExit` (`RuntimeError: generator ignored GeneratorExit`) and delivers events into the wrong consumer state during exception unwinding. The `finally` body produces `interrupted_terminal` as a sentinel; the enclosing scope — after the `try/finally` returns control — yields `Interrupted` and then the `agent_responded`-shaped event with `response_type: "interrupted"`. The frontend can render that differently from a normal answer; the shape stays uniform.

The query-level `finally` at `agent.py:1465` keeps its current body and stays where it is. The two do not merge, and Wave 3 must not move the cancellation body into it.

**What commits on cancel:** the partial assistant message and any tool call/result pairs already committed by `ToolCallComplete` dispatch.

**What does not commit:** anything the model was still producing (partial tool call arguments, unflushed `TextDelta`s not yet part of a committed assistant message). The streaming boundary is the commit boundary: text the user saw is committed; text the model was generating but had not yet produced is lost. This is the same rule `TurnTransaction` already enforces for tool dispatch.

**Composes with refusal-as-result.** A cancelled turn is not an exception. A refused tool call inside a cancelled turn is still a refusal result inside the partial commit. The turn loop does not have two ways to end — it has one, `Interrupted`, and the refusal invariant is unchanged.

## 6. Pruning — protected

`_apply_summary_pruning` (agent/core/agent.py:1468) is untouched by this pass. The pass's job is to *name* the protection, not to redesign the mechanism.

**Why untouched.** The 80k token window is a cost boundary, not a capability boundary. Every turn re-sends the history, and re-sent tokens — while cheaper than new tokens — are paid for repeatedly. The append-only summary-with-kept-turns form is the shape that survived because every alternative ("prune older than N turns", "prune by token count only") destroyed information irreversibly. The mechanism is not naive; it is the result of a fight. Do not re-open that fight.

**What streaming must not change about pruning's algorithm.** The function's inputs (a summary string, a keep-recent count) and outputs (token-recovery events, mutated history) are unchanged. The reading of `session.user_history` and the writing of the summary message are unchanged. **If streaming requires a change to the algorithm here, the design pass stops and asks the operator for a scope ruling.** Do not change pruning in the dark.

**The call site does not move. Leave it at `agent.py:1384`.** The previous revision of this section said the invocation should move to after the terminal event, on the premise that at `:1384` the current turn's assistant message is not yet in `user_history`, so pruning would compute its keep-recent window and insertion index against a stale list. That premise is false on the code. `commit_assistant_only()` at `agent.py:1295` puts the assistant message into `user_history` before any event is yielded (`agent.py:1291-1293`), and `turn_transaction.commit()` at `agent.py:1337` puts the tool results there before the `tool_call` / `tool_result` yields. `_apply_summary_pruning` is invoked at `:1384`, *after* both — the summary branch is reached only after `execute_tool_calls` (`:1323`) and the commit at `:1337`. By the time the read happens, history contains the whole current turn. Nothing can land mid-stream at this statement index, because the index is only reached once the stream has been consumed and dispatched.

Moving it would be actively worse. `_apply_summary_pruning` performs two unconditional appends — the summary message (`:1517` / `:1520`) and the `[SYSTEM NOTIFICATION] Context has been summarized` user message (`:1524`) — and computes `pruning_insertion_idx` / `pruning_discarded_msg_count` from the list it reads (`:1496`, `:1503-1509`). Placing the call immediately after `Done`, before `execute_tool_calls`, would append both messages ahead of the tool-result commit, so the tool results would land *after* the summary and the notification, against an index computed from a list that never contained them. The one safe reading of "after the terminal event" — after the tool-result commit — is the position the code already occupies.

The algorithm, the arguments, the keep-recent count and the insertion index are all unchanged, and the call site is unchanged. **Do not move this call site.** This is a settled finding: it was raised in `.thoughtmachine/working_docs/design-pass-adversarial-read-2026-10-27-v1.2.md` (finding C5/H2) and resolved here in favour of leaving the code alone. A later pass that proposes relocating it must first show that the assistant message and the tool results are *both* in `user_history` at the new position.

**One test that pins it.** `tests/test_pruning_orthogonal_to_streaming.py`: run the same summary scenario with the old sync path and with the streaming path; assert `session.user_history` is byte-identical after both. **And assert explicitly that the summary observes a `user_history` that includes the current turn's assistant message** — that is the property the *unchanged* call site already provides, and it is the assertion that would catch a future pass moving the invocation earlier. Byte-identical histories alone would still pass if pruning fired early and happened to derive the same result from a shorter list. If the two paths differ, streaming changed pruning; that is a stop-the-line signal, not a fix.

## 7. What does not change

Each item below is a *protected shape* — correct as it is, and the pass's job is to name it so it survives the migration. Each entry names the mechanism it actually stands on, not the one the earlier revision of this section asserted.

- **`_apply_summary_pruning`** — **safe at its call site; at risk only from a future reordering.** The append-only summary-with-kept-turns form is correct and untouched. The call site at `agent.py:1384` is *also* correct: `commit_assistant_only()` (`:1295`) and `commit()` (`:1337`) both precede it, so the read sees a complete current turn. The earlier revision of this entry said the call site was the risk and §6 moved it; that was wrong and §6 now forbids the move. What remains is a standing hazard: any pass that moves this invocation earlier — up to `Done`, before `execute_tool_calls` — corrupts history ordering. §6 states the prohibition. This is not streaming risk; it is a guard against the design pass's own earlier mistake.
- **Mailbox pattern** — **safe, with a narrowed claim.** `_apply_pending_config()` is called once per *query* (`agent.py:905`), before the turn loop opens; the loop reads `self.config.max_turns` fresh at entry (`agent.py:982`) and does not re-read config between turns. So the mailbox is checked at query boundaries — not turn boundaries, and certainly not mid-stream. Streaming does not weaken the mechanism: a config change still takes effect at the next query. But it multiplies yield points, and every yield is a preemption window in which a caller can enqueue a config update; none of those are observed until the next query. That is correct, and it should be stated so nobody "improves" it by polling the mailbox inside the event loop, where a mid-stream swap would change config under a request already in flight.
- **Commit-before-emit** — **materially at risk, from ordering, not from mechanism.** It is *two* calls, not one: `commit_assistant_only()` (`agent.py:1295`, before any yield — comment at `agent.py:1291-1293`) and `commit()` (`agent.py:1337`, after dispatch, before the `tool_call` / `tool_result` yields). The earlier revision of this section named neither and never said the words `commit_assistant_only` at all: the scorecard asserted a protection it had not located. Streaming multiplies the yield points this ordering defends, which makes it the place a careless rewrite does the most damage. §4.
- **Refusal-as-result** — **the transport is safe; the classification is broken today.** A permission refusal reaches the model as a text result, not an exception (spec §8.6, `agent/core/tool_executor.py:408/428/526/540`), and streaming does not touch that path. The earlier revision called the string classifier a *caveat* — "fragile against any change to the refusal wording" — which understates it. The classifier is `result.startswith('❌') or 'TOOL CALL REJECTED' in result or 'Error executing tool' in result` (`agent.py:1330`), and **two of those three markers have no producer anywhere in the repository.** `'TOOL CALL REJECTED'` occurs exactly once in the tree — in the classifier itself; `'❌'` appears in first-party code only on that same line and as a test's status glyph; only `'Error executing tool'` is ever written, by one producer (`tool_executor.py:699`). The four denial sites cited above return plain prose — `'Permission denied: security gate unavailable…'` (`:408`), `'DENIED: could not resolve workspace_id…'` (`:428`), and the gate's `error_msg` / `err2` (`:526`, `:540`) — so none matches a marker, and each is recorded as `success = True` (`agent.py:1327`). **A denied tool call is reported to the UI as a success today**, before any streaming work; ticket `bug/tool-refusal-reported-as-success` records the same defect. Two remedies, neither taken by this pass: (a) give the dead markers a producer — cheap, keeps the string contract, still prose-based; (b) put a structured flag on the tool result and stop sniffing strings — correct, but changes every producer and the result dict shape. This pass records the defect and chooses neither. It is not streaming risk; it is a pre-existing bug the rewrite must not inherit.
- **`host_bash` grain gate** — safe, but it is one of **two** gates, and the earlier revision named only this one. The grain is read from `effective_permissions`, never the session mirror, so a stale session mirror cannot re-open host execution (`tools/host_bash_tool.py:268`; absent ⇒ `"banned"`); it is then whitelisted to `ask` / `allow` (`:283`), with the worker-context ask denied in-tool (`:293-302`). Before any of that runs, a *workspace* gate (`:272-282`) requires `workspace_allows_host_resources(ws_id)` — and it is **skipped**, not passed, when `_workspace_id()` does not resolve (`:115`, `''` when unknown). So the grain gate is necessary and not sufficient. The pass does not touch `host_bash_tool.py`, and streaming reorders neither gate.
- **`_exec_container_raw` substrate split** — safe. Container git runs on its own path, not through the host port (host-port.md §7). The pass does not touch this boundary.

**Of the six, exactly one is materially at risk under streaming:** commit-before-emit (ordering — §4). The pruning call site was assessed as a second and is now **cleared** (§6): the read already follows both commits. The mailbox is safe and narrowed; `host_bash` (two gates, now both named) and `_exec_container_raw` are safe; **refusal-as-result is not at risk from streaming at all — it is broken now**, and the entry above says how.

### Where `user_history` lives: wire dicts, not IR (Option A)

The pass left one question implicit: now that the IR exists, does `session.user_history` hold `CanonicalMessage`s or the wire dicts it holds today? Implicit is not good enough — the two readings give different obligations, so the choice is made here.

**Option A — `user_history` keeps holding wire dicts. This is the decision.** The IR is the request/response boundary only, and it is a boundary the *new* turn crosses one direction at a time: the turn loop builds `CanonicalMessage`s for the new content, the adapter converts that content IR → wire (writing the signature onto the wire dict), wire → IR conversion happens on the provider's *response*, and **stored history is never converted in either direction** — it is passed through verbatim. What lands in `session.user_history` is the same `{'role': ..., 'content': ..., 'tool_calls': [...]}` dicts as today. Consequences, stated so they are not rediscovered in Wave 3:

- **Persistence is unchanged.** `user_history` is read by the bridge, by session serialisation, and by pruning's insertion logic. None of those learn about `ContentBlock`, and no migration of stored sessions is required.
- **Invariant 3 of §2 narrows.** It is not "the IR carries the signature through persistence"; it is **"the adapter preserves `reasoning_signature` on the wire message"** — the signature lives on the wire dict (today's `reasoning_content` / signature field), and the adapter's job is to round-trip it without dropping it on the tool-call turn. §2's invariant is to be read that way.
- **No conversion of history at all — pass-through.** The whole history goes to the provider verbatim: history is wire by construction, so replay is a slice, not a conversion. Only the current turn's new content is converted IR → wire, once, on the way out; only the provider's response is converted wire → IR, once, on the way in. No IR ⇄ wire re-encoding of stored messages exists per request, and streaming adds none.
- **What this costs.** The typed guarantees hold only inside the adapter/loop boundary. Anything reading `user_history` directly — pruning, the bridge, tests — still sees wire shape and is not protected by the IR's invariants. Accepted: those readers are not where the vendor quirks live.

**Option B — persist IR — is deferred, not rejected.** It would give the typed guarantees to every reader of history, at the cost of a session-format migration and a conversion on every history read. Wave 3 may revisit it as a separate decision with its own migration plan; this pass does not take it and is not blocked on it.

This is written down because leaving it implicit is what made the earlier §7 readable two ways.

## 8. Migration order

Each wave is a separate PR, each with its own verification. No wave begins before the previous one is on `dev` and green.

**Wave 1 — this design pass.** No code. The deliverable is this document. Wave 2 begins only after the adversarial gate in §9 passes.

**Wave 2 — IR and OpenAI adapter, flag-gated.** Implement `CanonicalMessage`, `ContentBlock`, `CanonicalToolCall`, `CanonicalToolDefinition` in `agent/core/ir.py`. Implement `OpenAIAdapter.stream_completion(messages, tools, **kwargs) -> AsyncIterator[StreamEvent]` in `llm_providers/openai_compatible.py`. The turn loop still runs the old sync path. A new `Agent(streaming=True)` parameter selects the streaming path for testing; `streaming=False` (default) runs the old path. The new path runs in tests, not in production. **This is the only wave where two paths coexist.**

**Wave 3 — turn loop rewrite against the streaming contract.** `process_query` becomes a `for event in stream:` loop. The `streaming` flag is deleted; one path forward. D1 dissolves via mechanism (a). D2 fixes via `UsageUpdate`. Cancellation lands in the new turn-level `finally` inside the turn loop (§5); the query-level `finally` at `agent.py:1465` is unchanged and keeps only its `execution_state` reset. The `has_assistant_message()` content check (§4) lands here. The pruning call site does **not** move (§6); the pruning-pin test lands, asserting that the summary observes a history that includes the current turn's assistant message. **Old sync path deleted in the same wave — no drift window.**

**Wave 4 — Responses API adapter, capability-gated.** `ResponsesAdapter` in `llm_providers/responses_provider.py`. Capability detection by model name. The turn loop does not change — that is the point of the IR.

**Wave 5 — sysprompt addendum, external injection, multimodal.** Each feature lands on the substrate the first four waves built. None is a rewrite; each is a small additive layer on the streaming contract.

## 9. Adversarial gate

**Deferred to when the Puppet LLM fixture lands (`feat/puppet-llm-fixture`).** The gate runs the puppet against this design pass. The puppet's brief: try to break the streaming contract.

At minimum, the puppet must attempt:

1. **Cancellation state corruption.** Cancel mid-stream while a tool is dispatching. Does the partial assistant message commit cleanly? Does the tool's result land in the transaction? Does history end in a coherent state?
2. **IR information loss on round-trip.** Take a `CanonicalMessage` with `ToolUseBlock`/`ToolResultBlock`/`ReasoningBlock`, serialise it through the OpenAI adapter to a wire shape, deserialise back to IR, and assert the IR is byte-identical (modulo the specific fields the provider cannot express — named, not silently dropped).
3. **DeepSeek `reasoning_content` replay.** A two-turn exchange with reasoning in turn 1 must replay it in turn 2. Adapter test.
4. **`Interrupted`-vs-`Done` race on partial tool arguments.** The model is mid-`ToolCallArgumentsDelta` when the stream is closed. `ToolCallComplete` never fires. Does the turn loop emit `Interrupted` without a partial tool call reaching the executor? If it does not, that is a state corruption the gate must catch.

If the design pass survives the gate, Wave 2 begins. If it does not, the pass returns to this document with the specific failure named.

## Corrections

*This section will be appended to as the pass is read, adversarially or otherwise. Each correction names the carrier — this document, the puppet report, the operator ruling — so that no correction is silent.*

### C1 — Commit ordering was asserted, not located (§4)

Carrier: adversarial review (`.thoughtmachine/working_docs/design-pass-adversarial-read-2026-10-27.md`, Attack 1 and the §7 audit). The v1.1 text named one call (`TurnTransaction.commit()`, `turn_transaction.py:102`) and said it "runs when the stream closes". The mechanism is **two** calls: `commit_assistant_only()` (`agent.py:1295`) before any yield, `commit()` (`agent.py:1337`) after dispatch and before the tool yields. §4 rewritten as two-phase; the uncommitted window is restated as one statement wide, not one stream wide.

### C2 — "Interrupt a long tool" withdrawn (§4)

Carrier: adversarial review, Attack 3. Dispatch is synchronous inside the consuming loop — no break point exists while a tool runs. The claim is removed, a "Not in scope" subsection added naming sub-agent/thread architecture as the prerequisite (Wave 5-or-later), and §5 restated as mid-*stream* cancel, not mid-*tool*.

### C3 — `has_assistant_message()` tests presence, not content (§4)

Carrier: adversarial review, Attack 2. `turn_transaction.py:171-173` is `return self._assistant_message is not None`. The pause path at `agent.py:1257-1266` already hand-builds a separate message because of the gap. v1.2 recommends extending the predicate (option i), with `has_content()` (ii) and the pause-path pattern (iii) named as alternatives.

### C4 — The `finally` at `agent.py:1465` is query-level (§5)

Carrier: adversarial review, Attack 4. The turn loop opens at `agent.py:982`; the `finally` sits outside it and only resets `execution_state`. §5 now names both paths and does not conflate them: the existing `stop_check` (`agent.py:991`) for between-turn cancel, plus a **new** turn-level `finally` inside the loop for mid-stream cancel. §8's Wave 3 line corrected to match.

### C5 — Pruning's call site must move, not merely survive (§6)

Carrier: adversarial review, the §7 audit. `agent.py:1384`. The algorithm is untouched; the **invocation** moves to after the terminal event (`Done` / `Interrupted`). The pruning-pin test gains an assertion that the summary observes a `user_history` including the current turn's assistant message.

### C6 — §7 rewritten against mechanisms; `user_history` storage decided (§2, §7)

Carrier: adversarial review, the §7 audit and its Gaps section. All six entries re-worded against the code, with the two materially at risk named explicitly (commit-before-emit; pruning position) and the other four scoped down (mailbox: query boundaries only, with preemption windows; refusal-as-result: safe but string-classified; `host_bash` gate and `_exec_container_raw` split: safe). §7 gains "Where `user_history` lives": **Option A — `user_history` holds wire dicts; the IR is the request/response boundary only.** Invariant 3 narrows to "the adapter preserves `reasoning_signature` on the wire message." Option B (IR in persistence) deferred to Wave 3 as a separate decision.

### C7 — §6 reversed: the pruning call site does **not** move (§6)

Carrier: adversarial read v1.2 (`.thoughtmachine/working_docs/design-pass-adversarial-read-2026-10-27-v1.2.md`, H2). **This supersedes C5.** C5's premise — that the invocation must move after the terminal event — is false on the code: `commit_assistant_only()` (`agent.py:1295`, comment `:1291-1293`) puts the assistant message into `user_history` before the pruning call at `:1384`, and `commit()` (`:1337`) precedes it too; the summary branch already sits inside `if tool_calls:` (opened `:1305`) after `execute_tool_calls` (`:1323`). Moving the call would let pruning read a history the current turn has not yet been appended to, and the append (`:1517`, `:1520`, `:1524`) would land on the wrong side of the insertion index (`:1496`, `:1503-1509`). §6 now states that the call site does not move and forbids the move; §7's pruning entry, §7's closing count, §6's pin-test sentence and §8's Wave 3 line were brought into line in the same pass.

### C8 — §5's `finally` repair cannot be a plain commit; "six early returns" was ten (§5)

Carrier: adversarial read v1.2 (H3). The turn body does not have six early returns, it has **ten** — `agent.py:1005`, `:1169`, `:1188`, `:1206`, `:1221`, `:1234`, `:1280`, `:1381`, `:1421`, `:1446` — plus a loop-level `return` at `:1464` outside the loop, so enumerating them is not a repair. §5 now specifies a `turn_completed` flag set on the `Done` path, a turn-level `finally` that does nothing when it is set, an `interrupted_terminal` sentinel dict, and a commit guarded on **both** `_committed` and `_assistant_committed`: `commit_assistant_only()` checks only `_committed` (`turn_transaction.py:84-85`) and would double-append at `:88-91` on a second call, while `commit()` raises at `:114-115`. The `finally` yields nothing — a yield there meets `GeneratorExit` — so the sentinel is yielded by the enclosing scope.

### C9 — Refusal-as-result is not "safe with a caveat"; two of its three markers have no producer (§7)

Carrier: adversarial read v1.2, corroborated by `bug/tool-refusal-reported-as-success` (`.thoughtmachine/working_docs/ticket-bug-tool-refusal-reported-as-success.md`). The classifier at `agent.py:1330` tests three strings; `'TOOL CALL REJECTED'` occurs in the tree only on that line, `'❌'` only there and as a test's status glyph, so only `'Error executing tool'` (`tool_executor.py:699`) is ever produced. The four denial sites (`tool_executor.py:408`, `:428`, `:526`, `:540`) return plain prose matching no marker, so each is recorded `success = True` (`agent.py:1327`) — a denied call is reported as a success today. §7's entry now says so, names remedies (a) a producer for the dead marker and (b) a structured result field, and takes neither; the closing count no longer calls this entry safe.

### C10 — `host_bash` has two gates, not one (§7)

Carrier: adversarial read v1.2. The grain gate is real — `effective_permissions` only (`tools/host_bash_tool.py:268`, absent ⇒ `"banned"`), whitelist at `:283`, worker ask denied in-tool at `:293-302` — but a workspace gate (`:272-282`) runs first and is **skipped**, not passed, when `_workspace_id()` resolves to `''` (`:115`). §7's entry now names both gates instead of reading as though the grain whitelist were the whole protection, and the closing count drops the "under-described" hedge that referred to it.

### C11 — Option A's obligation sits on the write path; the reversal is declined (§2, §7)

Carrier: adversarial read v1.2, adjudicated against source. v1.2 read Option A as claiming a per-request IR ⇄ wire conversion of history and proposed reversing the storage decision. The text was ambiguous; **the decision stands**, because a smaller coherent reading exists and the code forces it. `agent.py:1282-1288` hand-builds a wire dict (`role` / `content`, `['tool_calls']`, `['reasoning_content']`) and `TurnTransaction` copies and appends it (`turn_transaction.py:48`, `:91`), so history is wire *by construction*, and every reader — pruning's `_find_summary_insertion_index` / `_group_messages_into_turns`, the bridge, serialisation — reads dicts. History therefore goes to the provider verbatim; the adapter converts only the new turn's content IR → wire and writes `reasoning_signature` onto that dict; wire → IR happens only on the provider's response. Reversing would force a session-format migration *and* a whole-history conversion per request — strictly more work for the same guarantee. §2's Invariant 3 and §7's Option A paragraph and conversion bullet now say this.

### C12 — `has_assistant_message()` has two call sites, not three (§4)

Carrier: adversarial read v1.2. C3 named `agent.py:1294` and a "branch at `turn_transaction.py:116-122`" as call sites. The predicate is defined at `turn_transaction.py:171` and called exactly twice, both in `agent.py`: `:1294` and `:1424`. `:116-122` is the `_assistant_committed` flag branch, not a call. Of the two real sites, `:1294` is a tautology (`add_assistant_message` runs at `:1289`) and `:1424` — the no-tool close — is where the stricter reading bites, so §4's option (i) now says that, and adds that `:1294` stops being vacuous once the predicate tightens: it must then commit nothing *and* yield nothing.

### C13 — The second-close raise lives on both close paths, and a turn has four closes (§4)

Carrier: adversarial read v1.2. §4 cited `:114-115` and `:142` for `RuntimeError('Transaction already committed')`. `:142` is `self._committed = True` — the flag being *set*, not a raise; the same error is raised by `commit_assistant_only()` at `:84-85` and by `commit()` at `:114-115`. §4 also listed only those two closes; a turn can additionally close at `agent.py:1424-1425` (no-tool turn) and at `agent.py:1266` (`grace_tx.commit()`, pause path). All four are now named, because a streaming rewrite has to place every one of them.

### Line numbers verified against source for this pass

`agent/core/agent.py`: 905 (`_apply_pending_config`), 982 (`for turn in range(max_turns)`), 991 (`stop_check`), 1257-1266 (pause grace path), 1294-1295 (`commit_assistant_only`), 1334-1337 (`commit`), 1384 (`_apply_summary_pruning` call), 1465 (query-level `finally`), 1468 (`_apply_summary_pruning` def). `agent/core/turn_transaction.py`: 72 (`commit_assistant_only`), 102-142 (`commit`), 171-173 (`has_assistant_message`).

