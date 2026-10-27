# Provider compatibility findings, read against 643fc100

## Q1. Provider surface and endpoint shape

Evidence:
- llm_providers/openai_compatible.py:2-3, :20-25 (targets "OpenAI, DeepSeek, OpenCode/Big Pickle, and any OpenAI-compatible API")
- llm_providers/openai_compatible.py:29-36 (client built with api_key, timeout, max_retries, optional base_url, optional default_headers)
- llm_providers/openai_compatible.py:306 (`response = self.client.chat.completions.create(**completion_kwargs)`) - the only HTTP call in the file
- llm_providers/anthropic_provider.py:22-25, :31 (anthropic.Anthropic client), :52 (`response = self.client.messages.create(**api_kwargs)`) - the only completion call
- llm_providers/anthropic_provider.py:199 (token counting via self.client.beta.messages.count_tokens)
- llm_providers/base.py:66-77 (abstract chat_completion contract; no transport), :84-89 (format_tools default is identity)
- llm_providers/tool_converter.py:11-169 (pure format conversion, no HTTP)
- grep 'responses' over llm_providers/ returns nothing

Finding: exactly two adapters exist and each targets one endpoint family. OpenAICompatibleProvider (llm_providers/openai_compatible.py:20) serves every OpenAI-protocol vendor - OpenAI, DeepSeek, OpenCode-Big Pickle, local servers - through the chat.completions endpoint, llm_providers/openai_compatible.py:306, with the vendor selected only by base_url passed to the SDK client at llm_providers/openai_compatible.py:31-36; there is no per-vendor subclass and no per-vendor URL assembly in this package (the alias-to-default-base_url step lives in factory.py, already read). AnthropicProvider (llm_providers/anthropic_provider.py:22) serves Claude through the messages endpoint, llm_providers/anthropic_provider.py:52, plus a beta.messages.count_tokens call at llm_providers/anthropic_provider.py:199. Nothing calls the responses endpoint: the string does not occur anywhere under llm_providers/. base.py and tool_converter.py carry no transport at all, so the endpoint surface is two call sites, both inside chat_completion.

Confidence: read.


## Q2. Wire format and the ToolFormatConverter claim

Evidence:
- llm_providers/openai_compatible.py:260-268 (completion_kwargs = {'model', 'messages'} + kwargs, 'temperature', optional 'top_p', then 'tools' and 'tool_choice' from format_tools)
- llm_providers/openai_compatible.py:266-268 (`completion_kwargs['tools'] = self.format_tools(tools)`; `tool_choice` defaults to 'auto')
- llm_providers/base.py:84-89 (format_tools returns tools unchanged)
- llm_providers/tool_converter.py:17-23 (`def to_openai(...): return tools  # Already in OpenAI format`)
- llm_providers/tool_converter.py:25-56 (to_anthropic: function.name/description/parameters -> name/description/input_schema)
- llm_providers/tool_converter.py:58-89 (to_gemini: wrapped as {"function_declarations": [...]})
- llm_providers/tool_converter.py:91-102, :104-137, :139-168 (from_tool_calls dispatch and the two extractors)
- llm_providers/anthropic_provider.py:49-50 (`api_kwargs['tools'] = self.converter.to_anthropic(tools)`)
- llm_providers/anthropic_provider.py:166-183 and llm_providers/openai_compatible.py:474-500 (tool calls extracted inline in each parse_response)
- llm_providers/base.py:10-24 (LLMResponse: the only typed object, and it is a response, not a request)

Finding: the request body is OpenAI-shaped, not a typed intermediate representation. Messages are raw dicts with role and content, assembled into {'model', 'messages'} at llm_providers/openai_compatible.py:260 and flattened into the SDK call at llm_providers/openai_compatible.py:306; tools go in as an OpenAI tools[] array with tool_choice 'auto', llm_providers/openai_compatible.py:266-268, unchanged by format_tools (llm_providers/base.py:84-89). The only typed object in the package is LLMResponse (llm_providers/base.py:10-24) on the way back. ToolFormatConverter exists and the research document's claim about to_openai is literally correct - llm_providers/tool_converter.py:17-23 returns its input with the comment "Already in OpenAI format" - but the class does considerably more than that claim admits: to_anthropic (llm_providers/tool_converter.py:25-56) performs a real schema change (function.parameters -> input_schema) and is on the live Anthropic path (llm_providers/anthropic_provider.py:49-50), to_gemini (llm_providers/tool_converter.py:58-89) builds a function_declarations wrapper that no adapter in this tree calls, and from_tool_calls (llm_providers/tool_converter.py:91-168) is a complete third implementation of tool-call extraction that neither provider uses, because both parse_response methods extract tool calls inline (llm_providers/anthropic_provider.py:166-183, llm_providers/openai_compatible.py:474-500). So the converter is half load-bearing (to_anthropic) and half dead (to_openai identity, to_gemini, from_tool_calls), and the "to_openai is a no-op" observation is an artefact of OpenAI being the internal standard rather than evidence of a thin conversion layer.

Confidence: read.


## Q3. Streaming

Evidence:
- grep 'stream' over llm_providers/ returns zero matches in all seven files (no stream= parameter, no chunk loop, no delta accumulator, no SSE handling)
- llm_providers/base.py:66-77 (the only abstract completion method is the blocking one; no streaming variant declared)
- agent/core/agent.py:1121 -> agent/core/llm_client.py:130 and :148 (`self.provider.chat_completion(messages=messages, tools=tools, **kwargs)` pass-through) -> llm_providers/openai_compatible.py:261 (`completion_kwargs.update(kwargs)`) -> :306
- llm_providers/openai_compatible.py:350-357 (parse wrapped; failure becomes ProviderError 'Failed to parse API response')
- llm_providers/openai_compatible.py:460-470 (`if not hasattr(raw_response, 'choices')` -> AttributeError; then `if not raw_response.choices` -> ValueError on empty choices)
- llm_providers/anthropic_provider.py:46 (`**kwargs` splatted into api_kwargs) -> :52; :156-162 (requires a 'content' attribute, else AttributeError); :115-133 (generic handler re-raises as ProviderError 'Unexpected error')
- grep 'stream' over agent/ yields only lifecycle-log stream references (agent/logging/lifecycle.py, agent/logging/streams.py, agent/cli/logs.py) and comments at agent/core/agent.py:801, :809, :1351, :1430 - no caller anywhere passes stream=True

Finding: streaming is absent, not partial. There is no loop, no accumulator and no reassembly of tool-call deltas, because the word stream never appears in the package. The flag would still travel: agent/core/llm_client.py:148 forwards arbitrary **kwargs, llm_providers/openai_compatible.py:261 merges them into completion_kwargs, and llm_providers/openai_compatible.py:306 hands them to the SDK, which accepts stream=True and returns a Stream object instead of a ChatCompletion. That object has no choices attribute, so parse_response dies at llm_providers/openai_compatible.py:466 and the failure surfaces as ProviderError('Failed to parse API response: ...') at llm_providers/openai_compatible.py:355-357. The Anthropic path behaves the same way one step earlier: the kwarg reaches messages.create at llm_providers/anthropic_provider.py:52, the returned MessageStream has no content list, and llm_providers/anthropic_provider.py:162 raises AttributeError which the catch-all at llm_providers/anthropic_provider.py:115-133 converts to ProviderError('Unexpected error: ...'). So a stream=True request is not silently ignored - it fails at parse time with a misleading "failed to parse" error rather than a "streaming unsupported" error. No production caller passes the flag today (agent/core/agent.py:1121 passes chat_kwargs built for the blocking path), which is why the gap has never been visible.

Confidence: read (the flag path and both failure sites are read; that no caller sets stream=True is read from grep, not from tracing every chat_kwargs construction).


## Q4. Responses API (v1/responses) - absent; what a flag-gated version would touch

Evidence:
- `grep -rnE 'v1/responses|responses\.create|\.responses\.|/responses' --include=*.py .` (excluding .venv) returns zero matches; a plain `grep -rn 'responses' llm_providers/` also returns nothing, so the word does not occur even as prose in the package
- llm_providers/base.py:33-41 (ProviderConfig fields are api_key, base_url, model, temperature, top_p, timeout, max_retries, extra_headers - no api_style and no capability flag of any kind)
- llm_providers/openai_compatible.py:31-36 (the only place base_url shapes transport), plus read-only base_url references at :242 (deepseek detection), :257 (openrouter detection), :290 (log line), :302, :384-386, :501
- llm_providers/openai_compatible.py:306 (`self.client.chat.completions.create`) - the single OpenAI-family call site
- llm_providers/openai_compatible.py:460-470 (parse_response requires a `choices` attribute and then a non-empty `choices` list)
- llm_providers/base.py:16 (usage key contract is prompt_tokens / completion_tokens)
- llm_providers/factory.py:105 (ProviderConfig is constructed there, i.e. the pass-through point for any new field)

Finding: there is no Responses API path, partial or otherwise - not a call site, not a branch, not a mention. The OpenAI family is reached exclusively through chat.completions (llm_providers/openai_compatible.py:306) and the only per-vendor differentiation the package performs is base_url manipulation around the client constructor (llm_providers/openai_compatible.py:31-36), so no endpoint family can be selected by configuration today. Behind a capability flag the smallest coherent change is four sites: (1) a field on ProviderConfig, e.g. `api_style: str = "chat_completions"`, llm_providers/base.py:33-41, defaulted so every existing constructor call keeps working; (2) pass-through where ProviderConfig is built, llm_providers/factory.py:105; (3) a branch at llm_providers/openai_compatible.py:306 choosing `self.client.responses.create(**completion_kwargs)` over `chat.completions.create`; (4) a second shape of parse_response, because the Responses payload carries `output[]` items (message items holding output_text, and separate function_call items) rather than `choices[0].message`, and reports `input_tokens`/`output_tokens` rather than `prompt_tokens`/`completion_tokens`, which must be mapped onto the LLMResponse.usage keys defined at llm_providers/base.py:16. Without step 4 the existing guard at llm_providers/openai_compatible.py:460-470 raises AttributeError on the missing `choices` and the call surfaces as the generic ProviderError. The alternative shape - a ResponsesProvider subclass - keeps llm_providers/openai_compatible.py untouched but re-implements the message-building, tool-definition, logging, retry, tokenizer and cost-accounting block that the generic adapter already owns (roughly the whole of the class body), for one endpoint difference.

Confidence: read for the absence (two independent greps) and for every line cited; the four-site change sketch is a design reading of those lines, not something I executed or tested.



## Q5. max_tokens - not set by the caller; the two adapters disagree

Evidence:
- agent/core/agent.py:1112 (`chat_kwargs = {'temperature': self.runtime_params.temperature}`), :1113-1114 (top_p added only when set), :1121 (the call)
- grep 'max_tokens' over agent/ and llm_providers/: no hit in agent/config/, none in agent/core/llm_client.py; the only hits in the package are llm_providers/openai_compatible.py:269, :293 and llm_providers/anthropic_provider.py:46
- llm_providers/openai_compatible.py:269 (max_tokens read only to be logged), :293 (read only into the diagnostic payload)
- llm_providers/anthropic_provider.py:46 (`'max_tokens': kwargs.get('max_tokens', 4096)`, with `**kwargs` splatted after it in the same dict literal, so a caller value would win if one were ever passed)

Finding: max_tokens never originates from configuration or from a caller argument - the request the agent builds at agent/core/agent.py:1112 carries only temperature and optionally top_p, and neither the config layer nor llm_client.py has a max_tokens setting to add. The two adapters then treat that absence differently, and only one of them is safe. The OpenAI adapter never sets the key; it only reads it, at llm_providers/openai_compatible.py:269 for a log line and at :293 for the payload dump, so nothing is sent and the SDK/provider default applies (which is the OpenAI-side default cap, not the model's real maximum). The Anthropic adapter hardcodes a fallback of 4096 at llm_providers/anthropic_provider.py:46, so every Claude request is capped at 4096 output tokens whether or not that is what the caller wanted, and because the fallback is a default inside `kwargs.get`, no log line or error will ever mention that a cap was applied. So the same run under the two providers is not the same request: Anthropic is silently truncated at 4096, OpenAI is silently unbounded up to the provider default.

Confidence: read (all three read sites and the absence in agent/ are read; that no caller anywhere in the tree passes max_tokens rests on the grep, not on tracing every call site by hand).



## Q6. Tool round-trip - correct on OpenAI, silently degraded on Anthropic

Evidence:
- llm_providers/anthropic_provider.py:41-45 (system pulled out to system_msg at :41-42; every other message flattened by `role = 'user' if msg['role'] == 'user' else 'assistant'` at :44 with `content` passed through as the raw value at :45)
- llm_providers/anthropic_provider.py:46 (`api_kwargs` literal: model, messages, max_tokens, temperature, **kwargs)
- llm_providers/anthropic_provider.py:49-50 (`api_kwargs['tools'] = self.converter.to_anthropic(tools)`) and llm_providers/tool_converter.py:25-56 (the conversion itself: function.name/description/parameters -> name/description/input_schema)
- llm_providers/anthropic_provider.py:135-187 (parse_response iterates raw_response.content and returns tool_calls on LLMResponse; stop_reason is not read anywhere in the method)
- llm_providers/base.py:15 (LLMResponse.tool_calls - the in-band signal the loop tests)
- agent/core/tool_executor.py:334, :273, :292 and agent/core/turn_transaction.py:61-70 (results appended as {'role': 'tool', 'tool_call_id': ..., 'content': ...}, i.e. OpenAI shape)
- grep 'stop_reason|tool_result' over *.py (excluding .venv): hits only in agent/core/tool_executor.py and agent/core/turn_transaction.py as the string 'tool', no stop_reason handling found

Finding: the outbound half of the tool protocol works on both providers and the return half works only on OpenAI. Tool definitions are converted correctly for Claude (llm_providers/anthropic_provider.py:49-50 through llm_providers/tool_converter.py:25-56), so Claude does receive callable tools and will emit tool_use content blocks; llm_providers/anthropic_provider.py:166-183 picks those blocks out and returns them as tool_calls, which is what the agent loop consumes. What is missing is the request half after the first call. The loop appends results as {'role': 'tool', 'tool_call_id': ..., 'content': ...} (agent/core/tool_executor.py:334), which the messages API does not accept: it requires a user turn whose content is a tool_result block carrying the tool_use_id, and it requires the assistant turn before it to contain the matching tool_use block. The provider does neither. At llm_providers/anthropic_provider.py:44 every role that is not 'user' - including 'tool' and including the assistant turn that carried tool_calls - is mapped to 'assistant', and at :45 the content is passed as whatever string it is, so a tool result is transmitted as assistant prose, and the assistant turn's tool_calls are dropped entirely because only msg['content'] is copied. The result is a conversation the API will reject on the second turn (tool_result/tool_use pairing) or, if it is tolerated, one in which the model reads its own tool output as something it said. Nothing detects this: stop_reason is never consulted at llm_providers/anthropic_provider.py:135-187, tool use is signalled purely by whether tool_calls came back non-empty (llm_providers/base.py:15), and no branch anywhere distinguishes a provider that cannot represent tool results. So multi-turn tool use is functional against every OpenAI-protocol vendor and degraded against Claude, with the degradation showing up as an API error or as a confused model rather than as a capability check.

Confidence: read for the message mapping, the converter call, the parse path and the result shape; the "API will reject" consequence is the messages-API contract applied to those lines, not something I exercised (nothing was sent to Anthropic).



## Q7. Secrets in provider logging (scope: llm_providers/ only)

Evidence - cleartext key:
- llm_providers/openai_compatible.py:30 - constructor log, f-string `api_key={config.api_key}`: fires once per provider construction
- llm_providers/openai_compatible.py:269 - per-call log, f-string `api_key={self.config.api_key}`: fires on every completion
- llm_providers/anthropic_provider.py:51 - per-call log, f-string `api_key={self.config.api_key}`: fires on every completion

Evidence - safe:
- llm_providers/openai_compatible.py:291 (`'api_key_present': bool(self.config.api_key)`) inside _log_payload, serialised by json.dumps at :301 - boolean only, no key material

Evidence - dumps that are clean but adjacent:
- llm_providers/openai_compatible.py:285-287, :288-301 (payload log: base_url, api_key_present, messages_preview with each content truncated to 200 chars; no headers)
- llm_providers/openai_compatible.py:326-348 (DEBUG_OPENAI response dump: response __dict__ only, no request echo)
- llm_providers/anthropic_provider.py:53-76 and :138-151 (response objects only, no request headers)

Evidence - non-logs, listed so they are not mistaken for leaks:
- llm_providers/openai_compatible.py:29 and llm_providers/anthropic_provider.py:31 (key handed to the SDK constructor), llm_providers/base.py:29 (dataclass field), llm_providers/factory.py:54, :62, :91-105 (resolve and pass the key), llm_providers/factory.py:100 (only the env-var NAME appears in the exception text), llm_providers/factory.py:126 (documentation example, literal `sk-...`)

Finding: the key is written in cleartext to the application log at three sites, two of which are per-call, so any DEBUG-level run publishes the live provider credential into the log stream (and from there into whatever collects it). The diagnostic payload path, which is the one designed to capture request detail, is actually the careful one - it records api_key_present as a boolean (llm_providers/openai_compatible.py:291) and truncates message content to 200 characters (:285-287) - while the plain status line beside it prints the key verbatim (:269). All three sites are DEBUG-level log() calls, so exposure depends on the configured level, which is set outside this package; at DEBUG the credential is in the log on every call, at INFO it is not, and the constructor line at llm_providers/openai_compatible.py:30 additionally prints `extra_headers` verbatim, so a credential supplied as a header rather than as a key would land in the log the same way. That header case has a second defect: `extra_headers` reaches the OpenAI client as default_headers (llm_providers/openai_compatible.py:33-34) but is never passed to the Anthropic client at all (llm_providers/anthropic_provider.py:31 constructs `anthropic.Anthropic` with api_key, timeout and max_retries only), so a config-supplied auth header is logged in cleartext on both providers and silently ignored on Anthropic.

Confidence: read (every site above was read in place; whether the log lines actually fire in a given deployment depends on log-level configuration outside llm_providers/ and is not verified here).



## Q8. Any trace of a gpt6 model in the tree

Search performed (all excluding .venv/.git/node_modules/__pycache__):
- `grep -rin -E 'gpt[-_ ]?6' .` over the whole workspace, run in the sandbox mount so it covers the 27 MB logs/backend_startup.log as well as every source file: zero matches
- FileSearchTool `gpt[-_ ]?6` (regex, case-insensitive) over *.py at the repo root: 703 files, no match; over agent/: 64 files, no match; over docs/: 62 files, no match; over tests/: 428 files, no match; over logs/: no match except that one file exceeded the tool's 10 MB read cap, which is why the container grep above was used as the authoritative pass
- `grep 'gpt'` over llm_providers/ returns only PRICING keys and tokenizer probes: gpt-4, gpt-4-turbo, gpt-3.5-turbo at llm_providers/openai_compatible.py:25, the 'gpt-4' / 'gpt-3.5' substring tests at llm_providers/openai_compatible.py:50, and the pricing default lookup at llm_providers/openai_compatible.py:549

Finding: nothing in the workspace names a gpt6 model - not a constant, not a config default, not a test fixture, not a log line, not an error string. The only literal gpt tokens anywhere are the three PRICING keys and the two tokenizer substring tests, all of which name gpt-4 or gpt-3.5. The literal substring "gpt" does occur inside logs/backend_startup.log, but only as part of opaque identifiers (e.g. `gpt7owwjmbcei9toh1858`), never as a model name. For completeness on what the log does name: the string `model=` appears 4780 times in logs/backend_startup.log and is `deepseek-v4-flash` on every one of them. Search scope caveat: the application's own log directory is the vault's (~/.thoughtmachine/logs), which is outside the sandbox mount, so host-side application logs were not searched - only the in-repo logs/ directory was. I am not inferring a cause for the gpt6 behaviour from this; the only defensible statement is that the tree contains no gpt6-specific handling, and since the factory never validates the model name (llm_providers/factory.py builds ProviderConfig from whatever string the config holds), an unrecognised model id is passed straight through to the vendor rather than rejected locally, and PRICING has no entry for anything outside the five keys above, so its cost silently falls back (llm_providers/openai_compatible.py:549, llm_providers/anthropic_provider.py:208).

Confidence: read (the absences are grep results with file counts cited; the vault log path is stated from the workspace layout, not opened).



## Contradictions with the research document

Claim wording below is taken from the handoff, not re-read from the document, so each line is a verdict on the claim as stated there.
- Package name `providers/` - does not hold. There is no providers/ package; the package is llm_providers/ (base.py, factory.py, openai_compatible.py, anthropic_provider.py, tool_converter.py, exceptions.py, __init__.py). Every line reference in this file is against that name.
- `ToolFormatConverter.to_openai` is an identity function - holds literally (llm_providers/tool_converter.py:17-23 returns its argument), but misleading as a characterisation of the class: to_anthropic (llm_providers/tool_converter.py:25-56) is load-bearing on the live Claude path, to_gemini (:58-89) has no caller, and from_tool_calls (:91-168) duplicates extraction that both providers do inline (llm_providers/anthropic_provider.py:166-183, llm_providers/openai_compatible.py:474-500). Identity here reflects OpenAI being the internal format, not a thin layer.
- Streaming is supported / partially implemented - does not hold in any form. The word stream does not occur in any of the seven files; there is no flag, loop, accumulator or delta handling (Q3).
- A Responses API adapter exists or is reachable - does not hold. The string does not occur in the package at all, and chat.completions at llm_providers/openai_compatible.py:306 is the only OpenAI-family call (Q4).
- Tool schemas are portable across providers - half holds. Definition portability is real for Anthropic (llm_providers/tool_converter.py:25-56) and trivial for OpenAI; what is not portable is the conversation: results are OpenAI-shaped ({'role': 'tool', ...}, agent/core/tool_executor.py:334) and are flattened to assistant text for Claude (llm_providers/anthropic_provider.py:44-45), with stop_reason never read.
- Implicit claim that the adapter layer normalises the request - does not hold. Requests are OpenAI-shaped dicts (llm_providers/openai_compatible.py:260), LLMResponse (llm_providers/base.py:10-24) is response-only, and the two adapters disagree on whether max_tokens is sent at all (Q5).
- Any claim that credentials are handled carefully in logging - does not hold; three cleartext sites, two of them per-call (Q7).
- gpt6 behaviour explained by something in the tree - unverifiable. Nothing names gpt6 anywhere (Q8); the cause is outside the searched surface.

## Ruling points for Main

Sequencing. Smallest shippable step is (a) the Anthropic tool round-trip, then (b) streaming, then (c) Responses; the trade-offs of each order:
- (a) -> (b) -> (c): (a) is the smallest change that fixes something already broken in production - it is confined to the message loop at llm_providers/anthropic_provider.py:41-48 (keep 'tool' role as a user turn carrying a tool_result block with tool_use_id, echo tool_use blocks on assistant turns, read stop_reason) and needs no new config surface. Its real cost is the seam it reveals: to echo tool_use the adapter must see tool_calls on historical assistant messages; agent/core/turn_transaction.py:43-44 documents that assistant messages may carry content and/or tool_calls, but whether the loop repopulates them consistently on every turn is not verified here, and if it does not, the fix stops being provider-local. (b) is orthogonal but the largest of the three - it changes the completion contract at llm_providers/base.py:66-77, both adapters, and the consumer at agent/core/agent.py:1121 that expects one complete LLMResponse, with tool-call delta assembly as the bug-prone part. (c) last because it is pure added surface with no current consumer, and it reuses the same capability seam (b) would want. Risk of this order: streaming work later has to fit a request-shaping hook that (a) introduced, so (a)'s shape should be chosen with (b) in mind.
- (b) -> (a) -> (c): buys faster perceived latency first, but ships a faster path through a conversation that Claude still cannot complete; the tool-result defect keeps getting reported while the diff is in the streaming layer.
- (c) -> (a) -> (b): the ProviderConfig capability field needed by (c) (llm_providers/base.py:33-41, pass-through at llm_providers/factory.py:105) is the same plumbing (b) needs, so (c) first builds the seam; cost is shipping a new endpoint for zero current callers while a known-broken path stays broken, and (c) carries a second response shape (output[] items, input_tokens/output_tokens) that will need its own parse branch and its own tests.
- Parallel option: (a) and (c) touch disjoint files and can land together; (b) collides with both at llm_providers/base.py.

Cleartext key. Options: (1) delete the api_key interpolation from the three sites (llm_providers/openai_compatible.py:30, :269, llm_providers/anthropic_provider.py:51) and keep api_key_present (llm_providers/openai_compatible.py:291) as the only signal - three one-line edits, no behavioural change, but loses the ability to correlate a log line to a specific key when several are configured; (2) mask to last-4 or a hash prefix - keeps correlation, needs a small helper and has to be applied at every future log site; (3) leave the code and rely on log level - zero diff, but the exposure then depends on a configuration outside this package and any DEBUG window writes the live key to disk. In all three cases the same line at llm_providers/openai_compatible.py:30 prints extra_headers verbatim, so a header-borne credential needs the same treatment, and the extra_headers-never-passed-to-Anthropic defect (llm_providers/anthropic_provider.py:31) is a separate one-line decision. Independent of the choice: existing log files already contain the key if DEBUG has ever been on, which is a rotation/revocation question rather than a code one, and llm_providers/factory.py:100 already shows the safer pattern (env-var name only).
Silent pricing. Both _calculate_cost overrides fall back to a hardcoded model - gpt-3.5-turbo at llm_providers/openai_compatible.py:549, claude-3-haiku at llm_providers/anthropic_provider.py:208 - and base.py:112 accumulates the result into total_cost, so an unlisted model produces a plausible but wrong number with no warning. This is live, not hypothetical: every model= line in the in-repo log is deepseek-v4-flash (Q8), which is not one of the five PRICING keys. Options: (1) log a WARNING once when the model misses the table and keep the fallback - smallest change, keeps totals numeric, but the number stays wrong; (2) return None/0 and mark cost unknown - honest, but every consumer that sums total_cost (llm_providers/base.py:112, :118-129) needs a decision about a non-numeric value, and I did not trace those consumers; (3) add the missing model entries - fixes today's traffic only and re-creates the gap with the next model. Any of the three should state whether max_tokens is sent at all (Q5), because the 4096 Anthropic cap is the same class of silent divergence.


