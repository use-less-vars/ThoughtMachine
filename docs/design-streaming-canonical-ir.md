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
3. A `ReasoningBlock` with a `signature` must be replayed verbatim on the next request for the same message. A `ReasoningBlock` without a signature is an annotation, not state, and may be dropped.
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

**Commits fire on `Done` or `Interrupted`, not on a returned response.** `TurnTransaction.commit()` (agent/core/turn_transaction.py:102) runs when the stream closes for any reason. On `Done`, the accumulated assistant message is complete. On `Interrupted`, the accumulated *partial* assistant message is committed — because a cancelled turn that produced text the user saw should survive in history, and a cancelled turn that produced nothing should not corrupt the transaction. The partial-vs-empty case is `TurnTransaction.has_assistant_message()`, which already exists.

**Tool dispatch fires on `ToolCallComplete`, not on `Done`.** A tool can begin executing while the model is still producing subsequent tool calls. This is what makes the "interrupt a long tool" capability usable: the user sees tool 1 running while the model is still calling tool 2, and can cancel between them. The dispatch table is the tool executor's existing `execute_tool_calls` — unchanged; the difference is *when* it is called.

**D1 dissolves explicitly.** The bug (`Respond` + `SummarizeTool` in one batch drops the summary) exists because the sync loop does `if final_detected: return` and the summary branch is unreachable. Under streaming, `Respond` arrives as a `ToolCallComplete` and `SummarizeTool` arrives as a *separate* `ToolCallComplete`. Two candidate mechanisms:

- *(a) Defer terminal emission.* `Respond` sets a `final_pending` flag; the loop does not terminal-yield `agent_responded` until `Done`. `SummarizeTool`'s `ToolCallComplete` still dispatches, the summary is applied, and only then is `agent_responded` yielded.
- *(b) Continue after Respond.* If `final_detected` is true but a summary was also requested, process the summary first, then terminal-yield.

**Recommendation: (a).** The rule "no tool can suppress another tool" is general; it scales to every future pair of tool calls without a special case. (b) requires the loop to know about specific combinations. (a) is the shape that makes D1 *structurally impossible* rather than *specifically fixed*. The cost of (a) is that `agent_responded` is delayed by a few events — the frontend must handle a partial render between `TextDelta`s and `agent_responded`, which it needs to handle anyway for streaming.

**D2 fixes naturally.** `UsageUpdate` fires on every provider usage report (OpenAI sends one per response, Anthropic per response, DeepSeek at end-of-stream). The turn loop calls `state.update_token_state(usage.prompt_tokens)` on every `UsageUpdate`, not once at response return. The first tool call of every turn is guarded against the true prompt-token count because the true count arrives *before* the first `ToolCallComplete`. This is not a special case — it is what "usage arrives during the stream" means.

## 5. Cancellation

`break` out of the `for event in stream:` loop, then `stream.close()`. The adapter closes the HTTP connection. The provider stops generating. The turn loop's `finally` block (agent/core/agent.py:1465) gains a body:

1. Emit `Interrupted { reason: "user" }` as the terminal `StreamEvent` the turn loop sees.
2. Call `TurnTransaction.commit()` if the transaction has a partial assistant message.
3. Emit an `agent_responded`-shaped event with `response_type: "interrupted"` — the frontend can render this differently from a normal answer, but the shape is uniform.

**What commits on cancel:** the partial assistant message and any tool call/result pairs already committed by `ToolCallComplete` dispatch.

**What does not commit:** anything the model was still producing (partial tool call arguments, unflushed `TextDelta`s not yet part of a committed assistant message). The streaming boundary is the commit boundary: text the user saw is committed; text the model was generating but had not yet produced is lost. This is the same rule `TurnTransaction` already enforces for tool dispatch.

**Composes with refusal-as-result.** A cancelled turn is not an exception. A refused tool call inside a cancelled turn is still a refusal result inside the partial commit. The turn loop does not have two ways to end — it has one, `Interrupted`, and the refusal invariant is unchanged.

## 6. Pruning — protected

`_apply_summary_pruning` (agent/core/agent.py:1468) is untouched by this pass. The pass's job is to *name* the protection, not to redesign the mechanism.

**Why untouched.** The 80k token window is a cost boundary, not a capability boundary. Every turn re-sends the history, and re-sent tokens — while cheaper than new tokens — are paid for repeatedly. The append-only summary-with-kept-turns form is the shape that survived because every alternative ("prune older than N turns", "prune by token count only") destroyed information irreversibly. The mechanism is not naive; it is the result of a fight. Do not re-open that fight.

**What streaming must not change about pruning.** The call site (`agent.py:1384`, in the compaction exit of Stage 10) is unchanged. The function's inputs (a summary string, a keep-recent count) and outputs (token-recovery events, mutated history) are unchanged. The reading of `session.user_history` and the writing of the summary message are unchanged. **If streaming requires a change here, the design pass stops and asks the operator for a scope ruling.** Do not change pruning in the dark.

**One test that pins it.** `tests/test_pruning_orthogonal_to_streaming.py`: run the same summary scenario with the old sync path and with the streaming path; assert `session.user_history` is byte-identical after both. If they differ, streaming changed pruning; that is a stop-the-line signal, not a fix.

## 7. What does not change

Each item below is a *protected shape* — correct as it is, and the pass's job is to name it so it survives the migration.

- **`_apply_summary_pruning`** — cost boundary, append-only summary form. §6 above.
- **Mailbox pattern** — config-change-per-query. The turn loop applies a pending config at the start of each turn (`agent.py:905`), before any model call. Under streaming this is unchanged: the mailbox is read once, config is applied once, and the streaming turn runs against the resulting config.
- **`TurnTransaction.commit-before-emit`** — the assistant message goes into `user_history` before any `agent_responded` event, so a consumer that dies mid-turn does not lose a message the model already produced (agent.py:1291-1297). Under streaming this survives: `commit()` fires on `Done`/`Interrupted`, then the terminal event is yielded.
- **Refusal-as-result** — a permission refusal reaches the model as a text result, not an exception (spec §8.6, `agent/core/tool_executor.py:408/428/526/540`). Under streaming this survives unchanged; the tool executor is not in scope.
- **`host_bash` grain gate** — reads only `effective_permissions`, never the session mirror (tools/host_bash_tool.py:267-300). The pass does not touch `host_bash_tool.py`.
- **`_exec_container_raw` substrate split** — container git runs on its own path, not through the host port (host-port.md §7). The pass does not touch this boundary.

## 8. Migration order

Each wave is a separate PR, each with its own verification. No wave begins before the previous one is on `dev` and green.

**Wave 1 — this design pass.** No code. The deliverable is this document. Wave 2 begins only after the adversarial gate in §9 passes.

**Wave 2 — IR and OpenAI adapter, flag-gated.** Implement `CanonicalMessage`, `ContentBlock`, `CanonicalToolCall`, `CanonicalToolDefinition` in `agent/core/ir.py`. Implement `OpenAIAdapter.stream_completion(messages, tools, **kwargs) -> AsyncIterator[StreamEvent]` in `llm_providers/openai_compatible.py`. The turn loop still runs the old sync path. A new `Agent(streaming=True)` parameter selects the streaming path for testing; `streaming=False` (default) runs the old path. The new path runs in tests, not in production. **This is the only wave where two paths coexist.**

**Wave 3 — turn loop rewrite against the streaming contract.** `process_query` becomes a `for event in stream:` loop. The `streaming` flag is deleted; one path forward. D1 dissolves via mechanism (a). D2 fixes via `UsageUpdate`. Cancellation lands in the `finally` at `agent.py:1465`. The pruning-pin test lands. **Old sync path deleted in the same wave — no drift window.**

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
