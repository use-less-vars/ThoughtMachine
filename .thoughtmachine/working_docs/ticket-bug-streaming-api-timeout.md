# Ticket — Streaming provider timeout kills the turn and discards the streamed text

**Status:** Open
**Severity:** High
**Filed:** 2026-10-29
**Branch to fix on:** `fix/streaming-api-timeout-retry` (not yet created)

---

## Summary

A live agent turn stops mid-answer and the UI surfaces `error_type: TIMEOUT`. Text was visibly streaming, then the turn ended on a terminal error
event and no assistant message was saved. Nothing is retried — neither the provider layer nor the turn loop re-issues the request. The trigger is a
request that outlives the provider's read timeout *while tokens are still arriving*, which is precisely the long, expensive turns. Recovery is manual:
re-ask the question and pay for the whole prompt again.

## Occurrences

Hit at least twice in operator sessions. One of those was a live ThoughtMachine session while this ticket was being drafted; that turn died and the
draft restarted in a fresh one. Both were long streaming answers. Recorded as an observation — no exact timestamps are claimed.

## Impact

- **Turn killed, no recovery.** The timeout path ends in a terminal error event and an immediate `return` (`agent/core/agent.py:1511-1514`); nothing
  tries to continue.
- **Long streams are more exposed than the old synchronous path.** `stream=True` is hardcoded (`llm_providers/openai_compatible.py:535`) and the
  connection stays open for the whole generation, so the gap between first and last token is the exposure window. The pre-streaming code sent one
  request and awaited one response object.
- **Work in progress vanishes.** Text already rendered to the operator is never persisted (secondary defect below), so the partial turn is lost from
  history as well as from the live view.
- **Cost.** Every occurrence burns a full prompt with nothing to show for it.

## Root cause

**Correction to the obvious reading first:** the raw SDK exception never reaches the caller. `APITimeoutError` is caught by the broad handler at
`llm_providers/openai_compatible.py:650`, logged at `:655`, and converted into an `Interrupted` event at `:656` before the generator returns at
`:657`. From that point the only trace of the original type is a *string* in `Interrupted.reason` (`provider_error: APITimeoutError`). No code catches
`APITimeoutError` as a class on this path — it has no dedicated clause and shares the generic provider-failure handler.

The chain:

1. `openai_compatible.py:593` opens the stream via `await self._ensure_async_client().chat.completions.create(**completion_kwargs)`.
2. `:594` iterates with a bare `async for chunk in stream:` — not `async with`; the stream is closed by hand in the `finally` at `:658-663`.
3. Failure mid-iteration hits `:650` (`except Exception as exc:`), is logged at `:655` (`Stream failed mid-flight: ...`), becomes
   `Interrupted(reason=f'provider_error: {type(exc).__name__}')` at `:656`, and the generator returns at `:657`. `GeneratorExit` / `CancelledError`
   are re-raised earlier at `:645` and correctly not converted.
4. `agent/core/llm_client.py:229-232` passes the generator straight through — no `try`/`except`, no retry, by design (docstring `:217-219`).
5. `agent/core/agent.py:1350-1351` builds `stream_agen`, drained by `_iter_stream_events` (`:109`).
6. `agent.py:1409-1420` sees the `Interrupted` event and raises at `:1420` through `_exception_for_interrupted(...)`.
7. `agent.py:262-269` maps `'apitimeouterror'` to `('timeout', 'Request timed out')`; `:282-284` formats `'{label} during streaming: {reason}'`;
   `:287` raises `LLMError(error_type='timeout', ...)`.
8. `agent.py:1482` catches it, `:1503` upper-cases to `error_type = 'TIMEOUT'`, `:1511` yields the terminal event the operator sees.

The surface symptom is therefore produced three layers downstream of the failure; by then the request is dead and closed.

**Where a retry has to live.** Either inside `stream_completion`, before `Interrupted` is yielded at `:656`, or in the agent loop around the
`Interrupted` event at `agent.py:1409-1420`. **Recommend the provider site** — it is the only place that still holds `completion_kwargs` and the
client needed to re-issue `create()` cheaply. Retrying from the agent loop means driving the whole event-generation path a second time over state the
loop has already partly mutated.

## Why the SDK's `max_retries` does not already cover this

`max_retries` is threaded into both clients — `openai_compatible.py:36-43` (sync `OpenAI`) and `:452-458` for `AsyncOpenAI`, constructed in
`_ensure_async_client` (`:444-460`) — defaulting to 3. The assumption that this covers streaming failures appears wrong.

**Inference, pending SDK confirmation:** the SDK's retry wrapper surrounds *opening* the stream (the `create()` call and its retryable status codes),
not *iterating* an already-open `Stream`. Once `create()` has returned and chunks are consumed at `openai_compatible.py:594`, a mid-iteration timeout
surfaces from the iterator, outside any retry the SDK installed. This has not been verified against the installed SDK version — confirm in the client
source before relying on it. There is no `for attempt in range(...)`, no tenacity, no backoff decorator on this path to compensate.

The agent loop's existing machinery does not help either: `agent.py:1483-1501` is an emergency retry gated on `error_type == 'token_limit_exceeded'`
only, and `agent.py:1515-1532` (rate limits) only *sleeps* — `:1519` `min(delay * backoff_factor, max_wait)`, `:1526` `time.sleep`, then it yields a
`rate_limit` stop reason and returns at `:1532`. It never re-issues, and is unreachable from a `timeout` `LLMError`.

## Secondary defect: the streamed text is discarded

Text is accumulated but committed only *after* the loop: `agent.py:1343-1345` initialises `content = ''` / `reasoning = None` / `tool_calls = []`,
`:1372-1373` appends each delta and yields `text_delta` live, and the commit happens at `:1604` (`TurnTransaction`), `:1612`
(`add_assistant_message`), `:1617-1618` (`commit_assistant_only()`). The timeout path returns at `:1514`, long before any of that, so nothing is
persisted and the text the operator already watched scroll past is gone on reload.

**Precedent to copy:** the hard-stop branch at `agent.py:1423-1437` already handles this — `halt_tx.add_assistant_message` /
`commit_assistant_only()`, with the comment at `:1424-1425` "Persist the partial answer so the truncated text is not lost". The timeout path should
use the same pattern rather than leaving the two stop reasons asymmetric.

## Evidence

**Provider boundary** (`llm_providers/openai_compatible.py`): `:535` `stream=True` hardcoded; `:540-541` `stream_options={'include_usage': True}`;
`:593` `create()`; `:594` bare `async for`; `:645` re-raise of `GeneratorExit`/`CancelledError`; `:650` broad `except Exception`; `:655` log; `:656`
`yield Interrupted(...)`; `:657` `return`; `:658-663` `finally` closes the stream; `:36-43` / `:444-460` / `:452-458` client construction and
`max_retries`.

**Agent loop** (`agent/core/agent.py`): `:109` `_iter_stream_events`; `:1343-1345` accumulator init; `:1350-1351` `stream_agen`; `:1372-1373` live
yield; `:1409-1420` `Interrupted` → raise; `:262-269` / `:282-284` / `:287` mapping to `LLMError(error_type='timeout')`; `:1423-1437` hard-stop
partial persist (pattern to copy); `:1482` / `:1503` / `:1511-1514` terminal error event; `:1483-1501` token-limit-only emergency retry; `:1515-1532`
rate-limit sleep without re-issue; `:1604` / `:1612` / `:1617-1618` deferred commit.

**Passthrough** (`agent/core/llm_client.py`): `:217-219` docstring, `:229-232` no-op passthrough, `:225-226` streaming is a provider capability.

**Config**: `base.py:70` `timeout: int = 120`, `:71` `max_retries: int = 3`, `:90-93` `None` → default with `0` preserved (so `0` already means no
retries); `llm_client.py:109-110` resolves `LLM_TIMEOUT` / `LLM_MAX_RETRIES`, precedence env → provider_config → default at `:28-33`;
`provider_profile.py:178-180` copies profile values. One scalar timeout only — no `http_client=`, no `httpx.Timeout`, no connect-vs-read split.

**Tests**: `tests/providers/test_openai_streaming.py:346` mid-stream error → `Interrupted` (assert `:357`), `:363` create failure → `Interrupted`
(assert `:375`), `:397` cancellation propagates with no `Interrupted`; `tests/test_llm_timeout_config.py:150`
`test_openai_apitimeout_error_maps_to_provider_timeout` is **synchronous only** (raises `:158`, asserts `:168`); `tests/test_agent_stop_midstream.py`
is the only partial-retention coverage and exercises the hard-stop path (injection `:68`, `:117`, `:169`), not the timeout path. **Gap: no test drives
a timeout through a retry.**

## Proposed fix

Add a bounded backoff retry around the stream inside `stream_completion`, before `Interrupted` is yielded. Constraints:

- **(a) Re-issue `create()`, never reuse the dead stream.** The `Stream` from `:593` is finished once the iterator raises; a retry re-runs
  `create(**completion_kwargs)` and rebinds `stream`, and the `finally` at `:658-663` must close whichever stream is current.
- **(b) Reset the pending tool-call accumulator** initialised at `openai_compatible.py:546`. A naive `continue` would splice argument fragments from
  two streams into one malformed tool call — silent corruption, worse than a clean failure.
- **(c) Do not retry once any delta has been yielded downstream.** The operator has seen it and the loop has accumulated it; a second stream
  duplicates it. Define the budget as the window *before the first successful token*, or emit an explicit reset signal the consumer honours. The first
  option is simpler and recommended.
- **(d) Never retry on `GeneratorExit` / `asyncio.CancelledError`** — already re-raised at `:645`; a retry there would fight cancellation.
- **(e) Cap attempts *and* total elapsed time**, so a repeatedly-hanging provider cannot pin the turn for minutes.
- **(f) Honour `max_retries == 0` as no retries** — `base.py:90-93` preserves an explicit zero, so the loop must treat it as a hard off switch, not
  fall back to a default.
- On exhaustion keep today's contract: log, `yield Interrupted(...)`, return. The agent loop must keep receiving an event, not an exception.

## Short-term workaround

Lower `LLM_TIMEOUT` and retry the prompt by hand. That turns a long hang into a faster, cheaper failure but does **not** recover the turn — the
request is still lost and the streamed text is still discarded. Set it via the `LLM_TIMEOUT` / `LLM_MAX_RETRIES` env keys (resolved at
`llm_client.py:109-110`; `max_retries` is effectively inert for this failure) or per-provider through `provider_config`
(`provider_profile.py:178-180`). Env wins over provider config.

## Acceptance criteria

- A mid-stream `APITimeoutError` before the first successful token is retried within the configured budget, with backoff, and the turn completes
  normally on the retry.
- A timeout after at least one delta has been yielded is not retried; the operator sees one error event and no duplicated text.
- `max_retries == 0` produces zero retries and today's behaviour exactly.
- Cancellation still propagates with no `Interrupted` event and no retry.
- Tool calls survive a retry intact — no spliced argument fragments.
- Exhausting the budget still yields a single terminal `error_type: 'TIMEOUT'` event.
- Partial text is persisted on the failed turn, matching the hard-stop branch.

## Tests to add

In `tests/providers/test_openai_streaming.py`, alongside the existing cases at `:346` / `:363` / `:397`:

- `test_timeout_before_first_token_is_retried` — `APITimeoutError` on first `__anext__`, second attempt succeeds, no `Interrupted`, full text emitted.
- `test_timeout_after_first_token_is_not_retried` — one delta then timeout, exactly one `Interrupted` and one text payload.
- `test_retry_resets_pending_tool_call_accumulator` — fragments on attempt 1, complete call on attempt 2, one well-formed call.
- `test_max_retries_zero_disables_retry`.
- `test_retry_budget_exhausted_yields_single_interrupted`.

Also: extend `tests/test_llm_timeout_config.py` past `:150` so the mapping is covered on the **async streaming** path, not just the synchronous one;
and add a timeout-path partial-retention case to `tests/test_agent_stop_midstream.py` beside the hard-stop injections.

## Out of scope

- Turning streaming off — there is no toggle today (`openai_compatible.py:535`; `llm_client.py:225-226` states streaming is a provider capability, not
  a caller-side flag) and this ticket does not add one.
- Splitting the scalar timeout into connect vs read timeouts (`httpx.Timeout` / `http_client=`). Orthogonal and smaller; separate ticket.

## OPERATOR TO CONFIRM

- Which provider and base URL produced the failures — a self-hosted or gateway endpoint may time out differently, and the budget should be sized for
  the one actually used.
- Intermittent or deterministic: does the same long prompt fail repeatedly? This decides whether retrying is a real fix or just shifts the failure
  rate.
- Retry scope: only the window before the first token (recommended — preserves what the operator has seen), or restart the whole turn (recovers more
  cases, duplicates visible text).
- Desired retry budget and maximum total wait — how long may the turn stall before the operator should just see the error.
- Whether the discarded-partial-text defect ships inside this fix or splits out. Same file and code path, but independently testable; splitting is
  defensible.
- `max_retries` semantics once explicit backoff exists: leave the SDK at 3 and layer the new budget on top (two budgets, harder to reason about), or
  set the SDK to 0 and own all retries in `stream_completion` (one budget, one place).
