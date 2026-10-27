# Ticket: hygiene/puppet-llm-scenario-format-unify

**Filed:** 2026-10-27, from the scripted-LLM fixture work (`tests/puppet_llm/`)
**Category:** hygiene
**Severity:** LOW — both doubles work; the cost is a second vocabulary to learn
**Status:** filed, not fixed

## Summary

The tree carries two scripted-LLM doubles at two substitution layers. Both are
wanted. They do not share a script format.

**Client-level — `PuppetLLM`** (`tests/mocks/puppet_agent.py:11`). Replaces
`LLMClient.chat_completion`. Scenario is a `List[Dict[str, Any]]` of
`{"type": "assistant" | "tool_call" | "respond"}` turns (`:18-24`), popped one per
call; one tool call per turn, no match guards, no error vocabulary, a
`default_response` when the list empties. Callers: `tests/test_puppet_agent_basic.py:25`,
`:39`; `tests/integration/test_credential_e2e_trust.py:79`, `:146`;
`tests/integration/test_ws_config_roundtrip.py:250`;
`tests/integration/test_first_query_fresh_vault.py`;
`tests/integration/test_full_session_lifecycle.py:125`, `:162`.

**Provider-level — `PuppetProvider`** (`tests/puppet_llm/puppet.py:112`). A real
`LLMProvider` subclass injected at `agent.llm_client.provider`. Script is a list of
frozen-dataclass `Step`s (`:84`) carrying `TextReply` (`:37`), `ToolCall` (`:44`),
`ToolCalls` (`:52`), `Raise` (`:62`) or `ContextFull` (`:69`), each with an optional
`match=` predicate. Used by `tests/test_puppet_llm_fixture.py` via the builders in
`tests/puppet_llm/scenarios.py` (`ScenarioResult` `:54`, `Scenario` `:71`).

## Ruling — keep both, migrate neither

The seams test different things:

- Client-level keeps no provider transport at all. Correct for end-to-end tests
  about the session, the vault or the WebSocket rather than the turn loop.
- Provider-level keeps the real `LLMClient.chat_completion` in the path, so the
  `ProviderError -> LLMError` mapping and the agent's real error handling are
  exercised (the reason is stated in `tests/puppet_llm/scenarios.py:3-14`).
  Correct for turn-loop behaviour, and it owns the vocabulary the client-level
  double lacks: tool-call batches, `Raise`, `ContextFull`, `Step(match=...)`.

## Scope — converge the vocabulary, not the seams

1. The provider-level vocabulary is the richer one and should be the one both
   doubles speak; `PuppetLLM`'s dict turns are the older, narrower shape.
2. `tests/puppet_llm/scenarios.py` already exports the `Scenario` /
   `ScenarioResult` builders (`:54`, `:71`). Concretely: let `PuppetLLM` accept
   `Step` objects and translate them at its `chat_completion` seam, so one script
   written once runs at either layer.
3. No behaviour change is requested: both doubles' current fixtures must keep
   passing unchanged (8 pass, 2 xfail today).

## Filed

2026-10-27, from the scripted-LLM fixture chunk. Verified by read at `6011d97e`.
