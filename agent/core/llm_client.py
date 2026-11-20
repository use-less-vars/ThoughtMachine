"""
LLM client for handling LLM provider interactions.

Extracted from agent.py to separate LLM communication concerns.
"""
import json
import os
import time
from typing import Optional, List, Dict, Any
from agent.logging import log
from llm_providers.factory import ProviderFactory
from llm_providers.exceptions import ProviderError, RateLimitExceeded, AuthenticationError, ModelNotFoundError, TokenLimitExceededError, ProviderTimeoutError, InvalidConfigError, ProviderNotFoundError, ToolFormatError
from .ir import CanonicalToolCall
from .stream_events import Done, ReasoningDelta, TextDelta, ToolCallComplete, UsageUpdate


def _coerce_positive_int(value, default):
    """Coerce a value to a positive int, falling back to default for bad input."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _resolve_timeout_setting(provider_config, key, env_name, default):
    """Resolve a timeout/retry setting: env var wins, then provider_config, then default."""
    env_value = os.environ.get(env_name)
    if env_value is not None and env_value.strip() != "":
        return _coerce_positive_int(env_value, default)
    return _coerce_positive_int(provider_config.get(key), default)


def _to_canonical_tool_call(tool_call):
    """Wrap one wire-format tool call (OpenAI shape) in a ``CanonicalToolCall``.

    The wire shape is ``{'id', 'type', 'function': {'name', 'arguments'}}`` with
    ``arguments`` a JSON string. A string that does not parse as a JSON object
    yields ``arguments={}`` plus ``parse_error`` (IR invariant 6) rather than a
    half-parsed dict; the original bytes are kept in ``raw_arguments`` so the
    tool executor can still attempt a repair.
    """
    if isinstance(tool_call, dict):
        tc_id = tool_call.get('id')
        function = tool_call.get('function')
        if isinstance(function, dict):
            name = function.get('name')
            raw = function.get('arguments')
        else:
            name = tool_call.get('name')
            raw = tool_call.get('arguments')
    else:
        function = getattr(tool_call, 'function', None)
        tc_id = getattr(tool_call, 'id', None)
        if function is not None:
            name = getattr(function, 'name', None)
            raw = getattr(function, 'arguments', None)
        else:
            name = getattr(tool_call, 'name', None)
            raw = getattr(tool_call, 'arguments', None)
    arguments = {}
    raw_arguments = None
    parse_error = None
    if isinstance(raw, dict):
        arguments = raw
        raw_arguments = json.dumps(raw)
    elif isinstance(raw, str):
        raw_arguments = raw
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                parse_error = f'Invalid JSON in tool call arguments: {exc}'
            else:
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    parse_error = 'Tool call arguments are not a JSON object'
    return CanonicalToolCall(id=tc_id or '', name=name or '', arguments=arguments, raw_arguments=raw_arguments, parse_error=parse_error)


#: Framing line that separates the session addendum from the rest of the system
#: prompt. It is the only structural text added: entry metadata (id / ts /
#: source / pinned) is never rendered, so the model sees prose only.
ADDENDUM_HEADING = 'Additional session context:'

#: Framing line for the operator's *configured* system-prompt addendum
#: (``AgentConfig.system_prompt_addendum``).  Deliberately distinct from
#: ``ADDENDUM_HEADING``: that block is cut and re-appended on EVERY request
#: (``strip_addendum_block``), while this one is baked into the assembled
#: prompt and must never be mistaken for - and stripped with - the session
#: block.
CONFIG_ADDENDUM_HEADING = 'Operator instructions:'


def _session_addendum(session):
    """Return ``session``'s addendum, or ``None`` when it has none.

    The addendum is an optional first-class session field and the session may
    be exposed as an object *or* as a plain dict, so both shapes are read here.
    A session that never heard of the field (no attribute, no key) yields
    ``None`` rather than raising.
    """
    if session is None:
        return None
    addendum = getattr(session, 'addendum', None)
    if addendum is None and isinstance(session, dict):
        addendum = session.get('addendum')
    return addendum


def _addendum_entry_texts(addendum) -> List[str]:
    """Return the stored entry texts of *addendum* in stored order.

    Stored order is preserved exactly as written: nothing is sorted, filtered,
    reordered by ``pinned`` or re-budgeted here (the budget is enforced at the
    write boundary, not at composition time). Entries whose text is not a
    string carry nothing renderable and are skipped.
    """
    if isinstance(addendum, dict):
        entries = addendum.get('entries')
    else:
        entries = getattr(addendum, 'entries', None)
    if not isinstance(entries, (list, tuple)):
        return []
    texts = []
    for entry in entries:
        text = entry.get('text') if isinstance(entry, dict) else getattr(entry, 'text', None)
        if isinstance(text, str):
            texts.append(text)
    return texts


def render_addendum_block(session) -> str:
    """Render *session*'s addendum entries as prompt text.

    Returns ``''`` when the session has no addendum, a ``None`` addendum or an
    addendum with no entries - the caller then leaves the system prompt
    completely untouched (byte-identical to the un-composed prompt).

    ``pinned`` is stored and rendered-adjacent but inert BY DESIGN: the block is
    rendered in stored order, nothing sorts or filters by ``pinned``, and no
    writer sets it True.  It exists so the field can gain a meaning later without
    a migration; until then it must not appear to do anything.
    """
    texts = _addendum_entry_texts(_session_addendum(session))
    if not texts:
        return ''
    return ADDENDUM_HEADING + '\n' + '\n\n'.join(texts)


def _addendum_entry_ids(addendum) -> List[str]:
    """Return the stored ids of *addendum* in stored order (diagnostics only)."""
    if isinstance(addendum, dict):
        entries = addendum.get('entries')
    else:
        entries = getattr(addendum, 'entries', None)
    if not isinstance(entries, (list, tuple)):
        return []
    ids = []
    for entry in entries:
        entry_id = entry.get('id') if isinstance(entry, dict) else getattr(entry, 'id', None)
        ids.append(entry_id if isinstance(entry_id, str) else '?')
    return ids


def strip_addendum_block(text) -> str:
    """Remove an addendum block previously baked into *text*, if any.

    Older builds composed the addendum INTO the stored system prompt
    (``ensure_system_prompt``) and persisted the result, so a resumed session
    can still carry a stale block at the end of its system message.  Composing
    onto such a message would send the stale entries *and* the fresh ones, which
    is exactly the "I deleted it but the model still sees it" failure.  Cutting
    the block off the base before appending the current one makes the outgoing
    prompt a function of the CURRENT entry set only, whatever history holds.

    The cut is at the LAST occurrence: a rebuild always replaced the whole
    system message, so at most one block can be present, and taking the last one
    keeps a legitimate mention of the heading earlier in a prompt text intact.

    A *text* that carries no block is returned unchanged (identity), so the
    byte-stable-prefix property (design doc P4) is unaffected for clean sessions.
    """
    if not isinstance(text, str):
        return text
    marker = f'\n\n{ADDENDUM_HEADING}\n'
    index = text.rfind(marker)
    if index == -1:
        return text
    return text[:index]


def compose_addendum_into_messages(messages, session):
    """Return *messages* with *session*'s addendum appended to its system message.

    THIS is where the addendum enters a request (design doc §6.2, ruling D-2):
    the block is composed onto the OUTGOING message list on every single turn,
    so a write, an edit or a removal is carried by the very next query — no
    rebuild, no restart, no resume.

    Why the outgoing list and not the stored conversation:

    * ``context_builder.build()`` returns REFERENCES into ``session.user_history``,
      so composing in place would write the block back into the durable record
      (and into compaction summaries) and freeze it again.  This returns a NEW
      list whose system message is a COPY; the caller's history is untouched.
    * The base prompt stays byte-identical: the block is appended after it, never
      interleaved, which is what makes the prompt prefix cache-stable (P4).
    * Exactly one system message is produced (P2): the append targets the single
      system message the builder emits, and a stale block already baked into it
      is stripped first so no second copy can appear.

    Returns *messages* UNCHANGED (same object) when there is nothing to compose:
    no addendum, an empty one, or an empty message list.  The un-composed payload
    is therefore byte-identical to what a session without an addendum sends.
    """
    if not messages:
        return messages
    block = render_addendum_block(session)
    if not block:
        return messages

    addendum = _session_addendum(session)
    entry_ids = _addendum_entry_ids(addendum)
    try:
        import hashlib
        digest = hashlib.sha256(block.encode('utf-8')).hexdigest()[:12]
    except Exception:
        digest = 'unhashed'
    log('DEBUG', 'core.addendum',
        f'[addendum] composing {len(entry_ids)} entries into the outgoing system '
        f'prompt: ids={entry_ids} sha256={digest} chars={len(block)}')

    composed = list(messages)
    for index, message in enumerate(composed):
        if message.get('role') != 'system':
            continue
        fresh = dict(message)
        base = strip_addendum_block(fresh.get('content', ''))
        fresh['content'] = f'{base}\n\n{block}' if base else block
        composed[index] = fresh
        return composed

    # No system message in the payload at all (a conversation built without one):
    # the block is still carried, as the single system message P2 allows.
    composed.insert(0, {'role': 'system', 'content': block})
    return composed


class LLMError(ProviderError):
    """Generic LLM error for provider-independent error handling."""

    def __init__(self, error_type: str, message: str, original_exception: Exception=None):
        self.error_type = error_type
        self.message = message
        self.original_exception = original_exception
        super().__init__(f'{error_type}: {message}')

class LLMClient:
    """Handles LLM provider communication, context building, and system prompts."""

    def __init__(self, config, session=None, logger=None):
        """
        Initialize LLM client.

        Args:
            config: AgentConfig instance.
            session: Optional Session object.
            logger: Optional logger instance.
        """
        self.config = config
        self.session = session
        self.logger = logger
        pc = dict(getattr(config, 'provider_config', None) or {})
        timeout = _resolve_timeout_setting(pc, 'timeout', 'LLM_TIMEOUT', 120)
        max_retries = _resolve_timeout_setting(pc, 'max_retries', 'LLM_MAX_RETRIES', 3)
        self.provider = ProviderFactory.create_provider(provider_type=config.provider_type, api_key=config.api_key, base_url=config.base_url, model=config.model, temperature=config.temperature, timeout=timeout, max_retries=max_retries)
        self.context_builder = None

    def create_context_builder(self):
        """Create a ContextBuilder based on configuration."""
        from session.history_provider import HistoryProvider
        if self.session is None:
            if self.logger and hasattr(self.logger, 'py_logger'):
                log('WARNING', 'core.llm_client', 'Creating HistoryProvider without session')
            elif self.logger:
                self.logger.log_warning('Creating HistoryProvider without session')
            else:
                log('WARNING', 'core.llm_client', 'Creating HistoryProvider without session')
            log('DEBUG', 'core.context_builder', f'LLMClient.create_context_builder: session is None, returning None')
            return None
        log('INFO', 'core.llm_client', f'[CONTEXT_BUILDER] Creating HistoryProvider')
        return HistoryProvider(session=self.session)
    def load_system_prompt(self) -> str:
        """Load system prompt from file."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [os.path.join(script_dir, 'system_prompt.txt'), os.path.join(script_dir, '..', 'system_prompt.txt'), './system_prompt.txt']
        system_prompt = None
        for path in possible_paths:
            try:
                with open(path, 'r') as f:
                    system_prompt = f.read()
                    break
            except FileNotFoundError:
                continue
        if system_prompt is None:
            raise RuntimeError('Could not find system_prompt.txt in any known location')
        return system_prompt

    def ensure_system_prompt(self, conversation: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Ensure system prompt is present in conversation, replacing any existing one.

        On agent restart after a config change, the conversation may still
        contain the *old* system prompt.  This method strips any existing
        system messages and inserts the fresh prompt from the current config,
        so that system-prompt changes take effect immediately.

        Args:
            conversation: Current conversation messages.

        Returns:
            Updated conversation with the current system prompt at position 0.
        """
        # Determine the current system prompt (from config or file fallback)
        if self.config.system_prompt:
            system_prompt = self.config.system_prompt
        else:
            system_prompt = self.load_system_prompt()

        # The CONFIGURED addendum (``AgentConfig.system_prompt_addendum``) IS
        # composed here, at assembly time.  It is configuration, not session
        # content, so it belongs with the base prompt - and this method re-runs
        # on every agent (re)build, config apply and resume, which is what makes
        # an edit visible without a restart-vs-hot-swap guess (a change to the
        # field is classified as respawn-required, so this method always sees
        # the new value).
        #
        # It is APPENDED, never substituted: the mode factory prompt (agent /
        # engineer) stays authoritative and keeps its byte-stable prefix, which
        # is also why this cannot be clobbered by
        # ``~/.thoughtmachine/custom_system_prompt.txt`` precedence - that
        # precedence only ever rewrites ``system_prompt``.
        config_addendum = getattr(self.config, 'system_prompt_addendum', '') or ''
        if config_addendum.strip():
            system_prompt = (
                f'{system_prompt}\n\n{CONFIG_ADDENDUM_HEADING}\n'
                f'{config_addendum.strip()}'
            )

        # The session addendum is deliberately NOT composed here.  This method
        # runs once per agent construction (and on pause/resume / config apply),
        # so baking the block in made the model read whatever the entry set was
        # at that moment - edits and removals after it were invisible and
        # deleted text kept being sent.  The block now enters each request in
        # ``compose_addendum_into_messages`` (called from the turn loop), which
        # is the ONE composer and leaves the stored history addendum-free.

        # Remove old system prompts but preserve summary messages
        # (Summaries also use role='system' and are needed by SummaryBuilder.
        #  Without this guard, the next SummaryBuilder.build() call will find
        #  no summary and fall back to including the ENTIRE history, causing
        #  a ~4.7x token explosion on restart.)
        conversation[:] = [
            msg for msg in conversation
            if not (msg.get('role') == 'system'
                    and not msg.get('content', '').startswith('Summary of previous conversation:'))
        ]

        # Insert the fresh system prompt at the front
        conversation.insert(0, {'role': 'system', 'content': system_prompt})

        return conversation

    def chat_completion(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]=None, **kwargs):
        """
        Make LLM chat completion request.

        Args:
            messages: List of message dictionaries.
            tools: Optional tool definitions.
            **kwargs: Additional chat completion parameters.

        Returns:
            LLM response object.

        Raises:
            RateLimitExceeded: If rate limit is hit.
            LLMError: For provider-independent errors (authentication, timeout, etc.).
        """
        try:
            start_time = time.time()
            response = self.provider.chat_completion(messages=messages, tools=tools, **kwargs)
            self._log_provider_response(response, start_time=start_time, kwargs=kwargs)
            return response
        except RateLimitExceeded as e:
            raise
        except ProviderError as e:
            error_mapping = {AuthenticationError: 'authentication_error', ModelNotFoundError: 'model_not_found', TokenLimitExceededError: 'token_limit_exceeded', ProviderTimeoutError: 'timeout', InvalidConfigError: 'invalid_config', ProviderNotFoundError: 'provider_not_found', ToolFormatError: 'tool_format_error'}
            error_type = 'provider_error'
            for provider_exception, generic_type in error_mapping.items():
                if isinstance(e, provider_exception):
                    error_type = generic_type
                    break
            raise LLMError(error_type=error_type, message=str(e), original_exception=e)

    async def stream_completion(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]=None, **kwargs):
        """
        Stream a completion from the provider as provider-neutral events.

        When the provider exposes ``stream_completion`` its events are delegated
        straight through (duck-typed; errors are not mapped - a failing stream
        ends with an ``Interrupted`` event rather than an exception).

        When it does not (e.g. the Anthropic adapter), the same event sequence is
        SYNTHESISED from one blocking ``chat_completion()`` call so callers have a
        single code path: ``UsageUpdate`` (only when usage was reported), the
        ``TextDelta``, then ``ReasoningDelta`` (only when non-empty), then one
        ``ToolCallComplete`` per tool call, then ``Done``. Streaming support is a
        provider capability, not a caller-side flag.
        """
        provider = getattr(self, 'provider', None)
        if provider is not None and hasattr(provider, 'stream_completion'):
            async for event in provider.stream_completion(messages=messages, tools=tools, **kwargs):
                yield event
            return
        # No native streaming: one blocking call, replayed as events. Errors are
        # mapped by chat_completion() itself (ProviderError -> LLMError), which is
        # what a native stream's caller has to handle anyway.
        response = self.chat_completion(messages=messages, tools=tools, **kwargs)
        usage = getattr(response, 'usage', None)
        if usage:
            yield UsageUpdate(input_tokens=usage.get('prompt_tokens', 0) or 0, output_tokens=usage.get('completion_tokens', 0) or 0)
        content = getattr(response, 'content', None)
        if content:
            yield TextDelta(text=content)
        reasoning = getattr(response, 'reasoning', None)
        if reasoning:
            yield ReasoningDelta(text=reasoning)
        for tool_call in getattr(response, 'tool_calls', None) or []:
            canonical = _to_canonical_tool_call(tool_call)
            yield ToolCallComplete(tool_call=canonical, parse_error=canonical.parse_error)
        yield Done()

    def _provider_meta(self, response) -> Dict[str, Any]:
        """Best-effort extraction of provider-specific metadata from a response.

        ``request_id`` / ``finish_reason`` (OpenAI-style ``raw.id`` /
        ``raw.choices[0].finish_reason``) and ``stop_reason`` (Anthropic-style
        ``raw.stop_reason``) live only inside ``raw_response``; everything
        here is defensive and never raises.
        """
        meta: Dict[str, Any] = {}
        try:
            model = getattr(response, "model", "") or ""
            if not model or model == "unknown":
                model = getattr(self.config, "model", "") or ""
            if model:
                meta["model_name"] = model
        except Exception:
            pass
        try:
            raw = getattr(response, "raw_response", None)
            if raw is None:
                return meta
            if isinstance(raw, dict):
                rid = raw.get("id") or raw.get("request_id")
                if rid:
                    meta["request_id"] = str(rid)
                sr = raw.get("stop_reason")
                if sr:
                    meta["stop_reason"] = str(sr)
                choices = raw.get("choices") or []
            else:
                rid = getattr(raw, "id", None) or getattr(raw, "request_id", None)
                if rid:
                    meta["request_id"] = str(rid)
                sr = getattr(raw, "stop_reason", None)
                if sr:
                    meta["stop_reason"] = str(sr)
                choices = getattr(raw, "choices", None) or []
            if choices:
                first = choices[0]
                if isinstance(first, dict):
                    fr = first.get("finish_reason")
                else:
                    fr = getattr(first, "finish_reason", None)
                if fr:
                    meta["finish_reason"] = str(fr)
        except Exception:
            pass
        return meta

    def _log_provider_response(self, response, *, start_time: float, kwargs: Dict[str, Any]) -> None:
        """Best-effort ``provider_raw.jsonl`` record for a successful completion.

        Never raises - provider raw logging must never affect the call path.
        """
        try:
            from agent.logging.lifecycle import log_provider_event

            content = getattr(response, "content", "") or ""
            tool_calls = getattr(response, "tool_calls", None) or []
            meta = self._provider_meta(response)
            usage = getattr(response, "usage", None)
            token_usage = None
            if usage:
                try:
                    if hasattr(usage, "model_dump"):
                        token_usage = usage.model_dump()
                    elif hasattr(usage, "__dict__"):
                        token_usage = vars(usage)
                    else:
                        token_usage = usage
                except Exception:
                    token_usage = usage
            temperature = kwargs.get("temperature")
            if temperature is None:
                temperature = getattr(self.config, "temperature", None)
            session_id = ""
            if self.session is not None:
                session_id = getattr(self.session, "session_id", "") or ""
            log_provider_event(
                content=content,
                model_name=meta.get("model_name", ""),
                request_id=meta.get("request_id", ""),
                token_usage=token_usage,
                latency=time.time() - start_time,
                finish_reason=meta.get("finish_reason", ""),
                stop_reason=meta.get("stop_reason", ""),
                tool_call_count=len(tool_calls),
                temperature=temperature,
                session_id=session_id,
            )
        except Exception:
            pass

    def close(self):
        """Close and release provider resources."""
        provider = getattr(self, 'provider', None)
        if provider is not None:
            if hasattr(provider, 'close'):
                try:
                    provider.close()
                except Exception as e:
                    log('DEBUG', 'core.llm_client', f'Error closing provider: {e}')
            elif hasattr(provider, 'aclose'):
                try:
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(provider.aclose())
                        else:
                            loop.run_until_complete(provider.aclose())
                    except RuntimeError:
                        pass
                    except Exception as e:
                        log('DEBUG', 'core.llm_client', f'Error closing async provider: {e}')
                except Exception as e:
                    log('DEBUG', 'core.llm_client', f'Error closing provider: {e}')
        self.provider = None

    def format_tools(self, tool_definitions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format tool definitions for the provider.

        Args:
            tool_definitions: Raw tool definitions.

        Returns:
            Formatted tool definitions for the provider.
        """
        return self.provider.format_tools(tool_definitions) if hasattr(self.provider, 'format_tools') else tool_definitions
