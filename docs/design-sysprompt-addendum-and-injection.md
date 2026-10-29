# Design brief — System-prompt addendum and mid-stream context injection

**Epic:** the V3 era — first-class memory, and the runtime's own voice on the stream
**Branch:** `docs/sysprompt-addendum-design`
**Status:** design pass complete. **No implementation proceeds from this document until the operator rules on §12.**
**Scope:** two mechanisms, one boundary. Sections 3–5 establish the boundary; 6 and 7 are the mechanisms; 12 is what I need decided.

**Provenance.** Every fenced quote below was extracted verbatim by two independent read-only reconnaissance passes against this branch's base, `adfb5ef6`. A bare `path:line` in prose with no quote block is a *pointer*, not a quotation — I checked existence and location, not wording. Where a claim is weaker than that (most importantly the frontend's unknown-event behaviour, §5.5), I say so explicitly at the point of use.

---

## 1. The dispatch, verbatim

> 1. **Sysprompt Addendum:** Read `docs/design-streaming-canonical-ir.md`. We need a mechanism to append a system prompt addendum that **survives context pruning** (a first-class memory).
> 2. **Context Injection:** How do we cleanly inject mid-stream config updates (using the streaming contract) **without looking like a provider change**?

Two requirements, stated separately, and they are usually designed separately: a *memory* problem and a *transport* problem. I think that is the mistake this brief exists to avoid. §2 states the thesis; §3 finds the precedent that justifies it.

---

## 2. Summary of the proposal

**Requirement 1 is a storage problem, solved by where the text is *not*.** The addendum becomes a first-class field on the session, and it reaches the model by being composed *into the single system-prompt string*, not by being inserted as a message. It then survives pruning structurally rather than by exemption: both pruners operate on `user_history`, and the addendum is not in `user_history` — it is re-emitted as the opening of every request. A memory that is not a message cannot be pruned by a pruner that prunes messages.

**Requirement 2 is a classification problem, solved by ruling what a config update *is not*.** A config update is not provider output, so it must never enter the provider-content channel; and it is not always a new request, so it must not be forced to a turn boundary. The design splits the existing config mailbox into three tiers by *what the change invalidates*, and drains the mid-stream tier at a poll site the loop already performs once per token.

**They are one mechanism because the injection's sink is the addendum.** The injection has to write *somewhere* that outlives the turn and does not disturb the message array. The addendum is exactly that place. Designed together, the transport becomes a few lines appended to a string; designed apart, it becomes a new protocol.

---

## 3. Finding: the absence is documented, and its stated reason is false

The canonical IR design pass already considered requirement 1 and rejected it:

```
docs/design-streaming-canonical-ir.md:18
- No sysprompt addendum, because there is no moment in the sync loop to receive one.
```

That premise is refutable from the code. The sync loop polls for out-of-band state **once per stream event**, which in a text turn means roughly once per token:

```
agent/core/agent.py:1367-1370
                        # Check for halt requests at each stream event to keep
                        # cancellation responsive even mid-generation.
```

and the structure that makes this poll the only window is stated plainly at the driver:

```
agent/core/agent.py:113-115
    Each ``__anext__()`` is run to completion on this thread's private event
    loop, so the caller can stay a plain sync generator (``process_query``) and
    keep yielding dict events to its own consumers.
```

So the moment exists. It is the same poll that reads `halt_check`, and its granularity is one event — good enough for a notice, and (per `docs/design-streaming-canonical-ir.md:98`) explicitly not good enough for interrupting a tool, which is a different problem with a different fix.

**What genuinely did not exist is a place to put what you received.** There is no session-level text channel that (a) outlives pruning and (b) is not a message. `summary` is the only durable text field on the session, and it is semantically occupied. Absence of a sink was mistaken for absence of a moment.

This distinction is the whole cost difference between two designs. "Add a signal path to the mid-stream reader" touches the provider adapter. "Add a sink and drain the mailbox you already have" touches neither the IR, nor `StreamEvent`, nor any adapter. §6 and §7 are the second design.

---

## 4. Three channels — and the boundary that makes the property structural

The repository does not have one event system. It has three, layered, and they are easy to conflate because two of them use the word "event" and all three are translated into each other.

### 4.1 The provider channel — `StreamEvent`

Closed union, content-only, isinstance-dispatched, and explicitly the type an *adapter* annotates:

```
agent/core/stream_events.py:108-119
#: The union an adapter annotates its iterator with:
#: ``AsyncIterator[StreamEvent]``.
StreamEvent = Union[
    TextDelta,
    ReasoningDelta,
    ToolCallStart,
    ToolCallArgumentsDelta,
    ToolCallComplete,
    UsageUpdate,
    Interrupted,
    Done,
]
```

The IR layer beneath it is equally strict about being pure data:

```
agent/core/ir.py:1-9
"""Canonical internal representation (IR) for messages, content and tool calls.

This is the type layer described in ``docs/design-streaming-canonical-ir.md`` §2.
It is pure data: it imports nothing from ``agent.core.agent`` and nothing from
``llm_providers``. Provider adapters convert IR -> wire and wire -> IR; the turn
loop never sees a wire shape.
```

### 4.2 The runtime channel — the turn loop's own dict events

Everything `process_query` yields is a plain dict with a string `type`, a monotonic `seq`, and conversation stamps:

```
agent/core/agent.py:1074-1083
    def _add_conversation_data_to_event(self, event: Dict[str, Any]) -> None:
        """Add conversation version and history to event."""
        current_time = time.time()
        event['created_at'] = current_time
        if 'timestamp' not in event:
            event['timestamp'] = current_time
        if self.session:
            event['seq'] = self.session._get_next_seq()
        conv_data = self._get_conversation_data_for_event()
        event.update(conv_data)
```

This channel is **already mostly runtime-originated**. Of the kinds the loop emits — `user_query`, `text_delta`, `reasoning_delta`, `token_update`, `token_warning`, `turn`, `tool_call_start`, `tool_call`, `tool_result`, `context_summarized`, `agent_responded`, `paused`, `stopped`, `stop_reason`, `rate_limit_warning`, `error`, `interrupted` — only `text_delta`, `reasoning_delta` and the usage-derived token state come straight off the stream. The rest are the runtime talking about itself.

The precedent is documented *as* a precedent, for exactly our reason:

```
agent/core/agent.py:1654-1659
                        # Emit a START event for each tool call BEFORE executing
                        # it, so consumers (e.g. the worker panel) can render a
                        # pending "running" tool-call row.
```

`tool_call_start` exists because the loop needed to say something the provider cannot say. A config injection is the same kind of fact, one layer over.

### 4.3 The client channel — the WebSocket broadcast

The bridge renames and reshapes on the way out, per arm, with hand-built payloads:

```
web_ui/backend/bridge.py:3144-3153
        elif event_type == "text_delta":
            # Token-level assistant text. Rendered into an in-flight bubble by
            # the frontend and reconciled against the terminal `turn`
            # conversation_changed. session_id rides INSIDE the payload because
            # the forwarder's broadcast() ignores its session_id argument.
            self._forwarder.broadcast(self._session_id, "assistant_delta", {
                "session_id": self._session_id,
                "text": raw_event.get("text", ""),
                "turn": raw_event.get("turn"),
            })
```

### 4.4 The rule this brief proposes

> **A config injection is legal on the runtime channel and the client channel. It is never legal on the provider channel.** The provider sees exactly two things: a `messages` array and a `tools` array. Anything that needs to reach the model reaches it by changing the `messages` array *between* requests, and only at the system string.

Once the rule is stated that way, "without looking like a provider change" stops being a matter of taste or code review and becomes five checkable properties.

---

## 5. "Without looking like a provider change", as five testable invariants

### 5.1 P1 — No fabricated roles

Today the runtime tells the model something mid-session by manufacturing a user turn. The flag is an explicit key, and the codebase is deliberate about the fact that content is not evidence:

```
agent/core/message.py:4-6
``is_system_notification`` is a regular dict key set explicitly at injection
sites.  No content-based derivation — a user typing ``[SYSTEM NOTIFICATION]``
in their message will **not** be treated as a system notification.
```

There are fourteen such sites, all `role='user'`. The problem is not the flag; it is that the role does not survive the adapter. On Anthropic, any role that is neither `user`, `system` nor `tool` is coerced:

```
llm_providers/anthropic_provider.py:112
    anthropic_messages.append({'role': 'user' if role == 'user' else 'assistant', 'content': msg.get('content', '')})
```

So the same `[SYSTEM NOTIFICATION]` is a user turn on one provider and (for any non-user role) an assistant turn on another. A message-borne notice *is* provider-shaped: its speaker changes with the adapter. **Invariant: an injection may not introduce a new speaker.**

### 5.2 P2 — Never a second system message

This is the sharpest trap, and it is invisible on the provider most developers test against. The context builder emits the main prompt and the compaction summary as two separate system messages, which is fine for a provider that keeps them both:

```
llm_providers/openai_compatible.py:279
    completion_kwargs = {'model': self.config.model, 'messages': messages}
```

Anthropic does not keep them both. It *overwrites*, last wins:

```
llm_providers/anthropic_provider.py:88-91
    role = msg.get('role')
    if role == 'system':
        system_msg = msg.get('content')
```

followed by `api_kwargs['system'] = system_msg` at `:122`. An addendum added as a sibling system message would therefore **replace the entire system prompt on Anthropic** while sitting harmlessly alongside it on OpenAI-compatible. Same input, two different model-visible realities — which is the definition of looking like a provider change.

**Invariant: exactly one system message, and the addendum lives inside its string.**

### 5.3 P3 — The provider array changes only between requests

A config update mid-turn must never be spliced into a request that is already in flight. It cannot be: the request was serialised and handed to `stream_completion` at `agent/core/agent.py:1350`. What a mid-stream injection can do is land in state that the *next* request reads.

**Invariant: no injection is observed by the model before the request after next.** This is a feature, not a limitation — it makes injection idempotent, because the next request is built fresh from state at `agent/core/agent.py:1285-1288` regardless of how many injections happened.

### 5.4 P4 — The base prefix stays byte-stable

The system prompt string is currently static: no timestamp, no file list, no git status, no mode string is interpolated into it. Grep for clock access in prompt construction returns nothing in `agent/core`. The only structural break in the whole conversation prefix today is compaction, which *inserts* rather than deletes:

```
agent/core/agent.py:1901
summary_msg = {'role': 'system', 'content': f'Summary of previous conversation: {truncated_summary}', 'summary': True, ...}
```

There is no prompt caching anywhere in this repository — zero hits for `cache_control`, `prompt_cache`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `cached_tokens` outside `.venv`. So P4 protects a property we do not yet exploit. It costs one rule to keep: **entries are append-only and ordered oldest-first, so the base text and all prior entries stay a stable prefix.** A rewrite or reordering of the addendum is the only thing that may invalidate the prefix, and it should be as rare and as explicit as compaction is.

### 5.5 P5 — Unknown kinds are ignored deliberately, not accidentally

Adding a member to the `StreamEvent` union without adding an arm to the consumption ladder fails **silently**:

```
agent/core/agent.py:1371-1420
                        # (an if/elif isinstance ladder; the bottom is)
agent/core/agent.py:1421-1422
                        # ToolCallStart / ToolCallArgumentsDelta are progress signals
                        # only (no UI event).
```

The comment sits where the `else` would be. The frontend has the same shape twice with *different* safety: the worker adapter has an explicit fallback arm (`web_ui/frontend/src/components/chat/adaptWorkerEvent.js:500-501`, "Fallback: unknown event type" / `default:`), and the main session switch in `web_ui/frontend/src/components/SessionTab.jsx` is a closed enumeration of about forty string cases (`state_changed` `:568`, `assistant_delta` `:612`, `reasoning_delta` `:623`, `config_changed` `:714`, `config_queued` `:735`, `config_apply_failed` `:746`, `status_message` `:767`, `error` `:1069`, `interrupted` `:1079`, `session_stop` `:1111`, plus the `worker:*` block at `:1018-1059`) whose default arm **I have not read**. I am flagging that as an unverified claim rather than asserting it: before implementation, someone must confirm whether an unknown `type` on the main switch is a no-op or a crash. Either way the design requires an explicit arm.

**Invariant: every new kind gets an explicit arm at every layer, and every layer keeps a deliberate default.**

---

## 6. Design A — the addendum

### 6.1 Where it lives

A new first-class field on the session, sibling to `summary`, in the same persisted record:

```
session/models.py:326
    def to_persistable_dict(self) -> Dict[str, Any]:
```

whose field set already includes `summary`, `mode`, `metadata`, `preset_name`, `workspace_id`, `security_config` and the token counters; and the inverse at `:341 from_persistable_dict`, which re-wraps every history entry into a `Message`. Persistence itself needs no new mechanism — one JSON file per session, `session/store.py:424` writing with `json.dump(data, f, indent=2, default=str)`, `:463` reading it back.

The `summary` precedent is the one to copy, because it is already the "durable text that the model must see" pattern:

```
agent/core/agent.py:1914
        self.session.summary = summary_msg
```

**Shape (proposed):** `session.addendum = { "entries": [ {"id", "ts", "source", "text", "pinned"} ], "budget_tokens": int }`. Append-only. Oldest first. Not in `user_history`. Not in `metadata` — a durable prompt channel deserves a type and a budget, not a dictionary key nobody validates.

### 6.2 How it reaches the model

The assembly site is already a single function, already called per turn:

```
agent/core/llm_client.py:144-179
  if self.config.system_prompt:
      system_prompt = self.config.system_prompt
  else:
      system_prompt = self.load_system_prompt()
```

and further down the same function it does something that dictates the entire design:

```
agent/core/llm_client.py:170-174
  conversation[:] = [
      msg for msg in conversation
      if not (msg.get('role') == 'system'
              and not msg.get('content', '').startswith('Summary of previous conversation:'))
  ]
```

```
agent/core/llm_client.py:177
  conversation.insert(0, {'role': 'system', 'content': system_prompt})
```

Every non-summary system message is **deleted and rebuilt every turn**. So "append to the system prompt" cannot mean "add a message" — the message is destroyed a turn later, and on Anthropic (P2) it would be destroyed immediately in a worse way. It must mean "add to the string, before the insert".

The base text is not `system_prompt.txt` at the repo root, which is only the fallback path of `load_system_prompt()` (`llm_client.py:128-142`, raising at `:141`). In practice `config.system_prompt` is populated from a per-mode resource file by an after-validator:

```
agent/config/models.py:270-280
  if self.mode == 'agent':
      prompt_path = resources_dir / 'default_system_prompt.txt'
      ...
          object.__setattr__(self, 'system_prompt', text)
  ...
  elif self.mode == 'engineer':
      prompt_path = resources_dir / 'engineer_system_prompt.txt'
```

That matters twice: the base is *per mode* (so a mode change is a base change, not an addendum change), and the root `system_prompt.txt` is not the base. Whoever implements this must not compose the addendum onto the wrong file.

The per-turn refresh point is free:

```
agent/core/agent.py:1285-1288
  if hasattr(self, 'context_builder') and self.context_builder is not None:
      messages = self.context_builder.build(self.conversation)
  else:
      messages = self.conversation
```

`ensure_system_prompt` is already invoked per turn (`agent/core/agent.py:1169`), so a mid-stream write becomes visible on the next request with no extra plumbing — which is exactly what P3 promises.

### 6.3 Why it survives both pruners — and why "exempt" would be the wrong word

There are two pruners and they are independent.

**The replay window.** `session/context_builder.py` composes the request in one place:

```
session/context_builder.py:284-291
  context = []
  if main_prompt:
      context.append(main_prompt)
  if summary_msg is not None:
      context.append(summary_msg)
  context.extend(system_warnings)
  for turn in turns:
      context.extend(turn)
```

with only the newest turns replayed (`self.default_keep_turns = 5` at `:212`) and the main prompt always re-emitted (`:357`).

**The on-disk pruner.** `session/history_pruner.py` compacts the old region at save time (`:97`), and its treatment of system messages is unconditional:

```
session/history_pruner.py:164-165
  if role == 'system':
      result.append(msg)
```

while the runtime's own notices are dropped by *default*:

```
session/history_pruner.py:68
  keep_system_notifications: bool = False
```

That `False` is the tell. The codebase's instinct is to treat notification-shaped injections as disposable, because they were disposable. A durable memory must not join that class.

**The structural point, which is the answer to "survives pruning":** the addendum is inside the system string, which is not a message in `user_history` at all — it is re-derived at request time. Pruner 1 replays it because it *is* the main prompt's opening. Pruner 2 cannot reach it because it never enters the array it compacts. The addendum is not exempt from pruning; it is outside pruning's jurisdiction. That is stronger than an exemption, because an exemption is a list someone can forget to update.

### 6.4 The trap: do not make it a second message

If the addendum were inserted as its own system message, the replay logic would misidentify it. The main prompt is found by a structural search, not by a marker:

```
session/context_builder.py:342-382
    _find_main_prompt_and_summary
```

which classifies the first system message lacking the `'Summary of previous conversation:'` marker as the main prompt (`:357`), the last such-marker message as the summary (`:363`), and everything else after the summary index as a `system_warning` (`:253-259`). An addendum message inserted before the prompt becomes "the system prompt"; the real prompt becomes a warning. The failure is silent, and it looks like a prompt regression rather than a structural bug. Recorded here so nobody rediscovers it.

### 6.5 Budget and eviction

An addendum is an unbounded prompt channel unless bounded. The repository has two good precedents and one bad one. Good: KB entries declare `budget_tokens` and carry a `measured_tokens` observation, and `SummarizeTool`'s output is exempt from truncation (`tools/summarize_tool.py:12`, `skip_output_truncation: ClassVar[bool] = True`) precisely because it is *meant* to be large. Bad: everything else is truncated at the tool boundary.

**Proposal:** the addendum declares a token budget, measured on every write, and refuses a write that would exceed it rather than silently truncating. Eviction is oldest-first among unpinned entries, and eviction is a *reported* event, not a quiet one. Compaction may fold old entries into the summary — with the explicit consequence that once folded they are summary text and inherit the summary's fate.

### 6.6 Who may write it, and what it is not

The operator's own knowledge base already posed this question and left it open:

```
.thoughtmachine/knowledge/personal/parking_lot.md:643
*Decide explicitly whether this is a **mode** (predefined, user-chosen) or an **addendum** (freeform, agent-edited) — the answer determines the whole UI.*
```

**My answer: it is an addendum, and it is not a mode — and they must never share a field.** The evidence is that mode is already a different, immutable thing:

```
web_ui/backend/config_manager.py:1180-1181
              f"apply_config: mode change to '{new_mode}' rejected — "
              f"mode is immutable after session start")
```

with prompt changes and tool changes both mode-locked to `custom` (`:1191`, `:1198`). A mode is *chosen*, is a base-prompt swap, and is frozen at session start. An addendum is *accumulated*, is appended to whichever base is current, and changes mid-session by construction. Conflating them would mean the addendum inherits mode's immutability — which would defeat requirement 1.

Writers, proposed in order of confidence: (a) the config mailbox, for system-originated facts; (b) an explicit tool, so the model can record a durable instruction it was given; (c) the operator, via the config panel. Each write carries a `source` and a `ts` so that "why does the model believe this?" is answerable from the record. Note that adding a tool to `engineer` mode changes `ENGINEER_TOOLS`, which is currently exactly eight tools — a visible, reviewable change, and one that should be its own decision (§12, D-4).

---

## 7. Design B — the injection

### 7.1 First, the physics: tier by what the change invalidates

Mid-stream injection is only *possible* for changes that affect the next request's prompt. Anything that changes the current request is, by definition, a new request — you cannot change the model of a completion that is already streaming. This is not a policy choice to be argued; it is the reason the tiering below is the only coherent one.

| Tier | What it is | Example | Where it lands | Latency |
|---|---|---|---|---|
| **0 — boundary** | Changes the *shape* of the request | `model`, `temperature`, `tools`, `mode` | existing `_pending_config`, applied at the turn boundary | next turn |
| **1 — per-event** | Changes only the *content* of the next request's system string | addendum entry, durable operator note | new tier, drained at the per-token poll | between events |
| **2 — UI only** | The model never needs it | "mode change rejected", quota notices | runtime dict event, bridge, no model path | immediate |

The existing mailbox already covers Tier 0 and is proven; it should be left alone:

```
agent/core/agent.py:417-428
    def request_config_update(self, new_config: AgentConfig):
        """
        Request a configuration update via the mailbox pattern.

        The update is queued in _pending_config and will be applied at the
        start of the next process_query() call. This allows safe, atomic
        config changes at turn boundaries.
```

with the drain point deliberately placed before the first yield:

```
agent/core/agent.py:1120-1123
            # Apply any pending configuration update BEFORE yielding user_query,
            # so the [SYSTEM NOTIFICATION] is already in user_history when the
            # bridge syncs the conversation for the frontend.
            config_applied = self._apply_pending_config()
```

The bridge has a second, coarser queue for when the controller is busy (`web_ui/backend/bridge.py:2139-2155`, `apply_config_queued`), drained at `:2969-2981`. That is three queues for one concept already. **This brief adds no fourth queue; it adds a tier to the first one.**

### 7.2 The drain site

Tier 1 is drained at the poll the loop already performs once per stream event (`agent/core/agent.py:1367-1370`, §3). Concretely: alongside the halt check, drain the pending-addendum mailbox into `session.addendum`, then continue the ladder. No new thread, no new channel, no contention with the blocking `__anext__`, because the drain happens *between* `__anext__` calls — which is the only window that exists and the only window that is needed.

### 7.3 The event, and the one place it must not go

**It must not go into `StreamEvent`.** That union is the provider-content vocabulary; its own comment says it is the type an *adapter* annotates. Putting a config update there would mean the runtime's own state change is typed as provider output — literally the thing requirement 2 forbids. It would also be silently swallowed by the ladder's missing `else` (P5).

**It goes on the runtime channel**, as one more string-keyed dict alongside `token_update`, `tool_call_start` and `paused`: proposed kind `addendum_updated`, payload `{session_id, entry_id, source, tokens_used, budget_tokens, evicted: [entry_id…]}`. For Tier 2 the same channel carries a UI-only notice; proposed kind `runtime_notice`.

Downstream, per layer: an explicit arm in the ladder (not merely a union member), an explicit `elif event_type == …` arm in `web_ui/backend/bridge.py:3144-3183` with a whitelisted payload — the existing arms hand-build their payloads and that discipline is worth keeping — and an explicit `case` in `SessionTab.jsx` beside `:612` and `:623`.

### 7.4 A real deficit this design has to confront: ordering

The runtime stamps a monotonic `seq` on every event (`agent/core/agent.py:1074-1083`, §4.2). The bridge then discards it on the streaming arms, because each arm builds its own payload (§4.3). And the broadcast envelope has no room for it either:

```
web_ui/backend/event_forwarder.py:84-96
    def broadcast(
        self,
        _session_id: str,
        event_type: str,
        data: Dict[str, Any],
    ) -> None:
        """Broadcast an event to every registered callback."""
        for cb in list(self._callbacks.values()):
            if cb is not None:
                try:
                    cb({"type": event_type, **data})
```

Note `_session_id` — underscore-prefixed and unused, which the bridge comments on directly at `:3147-3148`. **Consequence for this design:** an injected notice cannot today be ordered against the deltas it interleaves with. Two honest options: (a) make injection order-insensitive by construction — it is state, carries its own `ts` and `entry_id`, and a client that receives it late or twice converges to the same display; or (b) restore `seq` to the wire, which is a small change to `broadcast` and every arm, and which also fixes the pre-existing inability of the client to detect a dropped event. I recommend (a) for the first cut and (b) as its own ticket, because (b) is a change to a stable contract that deserves its own review.

---

## 8. What happens to `[SYSTEM NOTIFICATION]`

It stays, and its role narrows. Fourteen sites currently use it, and the ones I read are *transient* facts about what just happened — a config failed, a token threshold was crossed, context was summarised. Those belong in history: they are part of the record, and dropping them would make the transcript lie:

```
agent/core/agent.py:1908
        context_cleared_msg = Message(role='user', content='[SYSTEM NOTIFICATION] Context has been summarized. You now have a fresh context window and full access to tools.', is_system_notification=True)
```

The category to retire is the *durable* one: a fact that must remain true for the rest of the session, told once as a user turn and then compacted away by `keep_system_notifications: bool = False` (§6.3). That is the mechanism requirement 1 exists to replace.

So: no flag day, and no rule that "injection means addendum". The rule is a question asked at each of the fourteen sites — *is this a thing that happened, or a thing that is now true?* Happened → notification. True → addendum. Migration proceeds site by site as each is touched.

---

## 9. Test obligations

Each invariant in §5 gets a test that fails when it is violated. Stated as obligations, not as a test plan:

1. **P1** — a request built after an injection contains the same multiset of roles as one built before it. Catches a message-borne notice.
2. **P2** — the outgoing payload has exactly one `role=='system'` entry, asserted for *both* adapters, with the Anthropic case asserted through its own conversion, not through the shared list. This is the test that would have caught the last-wins collapse.
3. **P3** — inject during a turn; assert the in-flight request is byte-identical and the next request differs only in the system string.
4. **P4** — with an entry appended, the serialised prefix up to the new entry is byte-identical to the previous request's. Catches entry reordering and any accidental timestamp in the base.
5. **P5** — feed every layer an unknown kind and assert a documented, non-crashing behaviour; assert the main session switch has an explicit default arm.
6. **Survival (requirement 1)** — run a real compaction via `SummarizeTool`, then assert the addendum is present in the next request and the token counter accounts for it. Then do it twice.
7. **Persistence** — write an entry, reload the session from disk through `session/store.py`, assert byte-equal entries. This is the test that catches a field added to `to_persistable_dict` but not `from_persistable_dict`.

---

## 10. What this design does not do

- **It does not add prompt caching.** There is none in the repository (§5.4). P4 keeps the door open; it does not walk through it.
- **It does not make tool execution interruptible.** `docs/design-streaming-canonical-ir.md:98` is explicit that this needs a different architecture (sub-agent or separate thread, stream reader still consuming), and `docs/design-interruptible-tool-calls.md` is a fourteen-line sketch that depends on it. Out of scope, and the newer document is right about that.
- **It does not add backpressure, resume, or a replayable event log.** Those terms have zero hits in the canonical IR document; this design inherits their absence.
- **It does not unify the two event schemas.** `agent/events.py` carries a pydantic `BaseEvent` with `event_id`, `timestamp` and `source` (`:114-118`, `to_dict` at `:131`) and an `EventType` enum at `:23`; the turn loop's dicts carry `seq` and conversation stamps (§4.2). Which of the two is live on the main path was not established by recon, and this brief does not depend on the answer — but any new kind must be registered in whichever one the bridge actually reads, and a third authority must not be created.

---

## 11. Defects found while reconnoitring, independent of this design

These are consequences of reading, not of the proposal. Each is worth its own ruling, and none is fixed here.

**11.1 `docs/agent-core-spec.md` line references are stale.** The document cites `agent/core/agent.py:951` for the `ensure_system_prompt` call (now `:1169`) and `:1468-1545` for `_apply_summary_pruning` (now `:1855-1938`). Its structure is sound and its numbers are not, which is worse than either alone: a reader who trusts it will edit the wrong place. Either the document carries a commit stamp per section or it carries no line numbers.

**11.2 `docs/design-interruptible-tool-calls.md` contradicts a later document and describes a mechanism that does not exist.** It is 14 lines, it proposes "the main agent loop continues yielding heartbeats" (`:10`) — a runtime-originated stream kind absent from the union — and it closes by conceding dependency on §5 of the IR document while that document's `:98` declares the capability out of scope. One of the two should be marked superseded.

**11.3 The bridge discards `seq`** (`web_ui/backend/bridge.py:3144-3153` and siblings), so no consumer can detect a dropped or reordered event. Pre-existing; §7.4 is the first design that has to care.

**11.4 `event_forwarder.broadcast` accepts a `session_id` and ignores it** (`web_ui/backend/event_forwarder.py:84-96`), forcing every caller to embed it in the payload instead. The bridge documents the workaround rather than fixing it.

**11.5 Unknown-event behaviour on the main frontend switch is unverified** (§5.5). One `grep` settles it; it should happen before implementation, not during.

**11.6 `docs/design-streaming-canonical-ir.md:18` states a premise the code refutes** (§3). The line should be corrected rather than silently contradicted by this brief, because as written it will cause the next reader to re-reject the addendum for a bad reason.

---

## 12. Decisions requested

Each with my recommendation, so that silence is not an option but disagreement is cheap.

- **D-1 — Is the addendum a session field with its own record, or an entry list inside existing `metadata`?** Recommend the field. `metadata` is an unvalidated dict; a durable prompt channel needs a type, a budget and a persistence test.
- **D-2 — Composed into the single system string, or a second system message?** Recommend the string, on P2 grounds. A second message is a different prompt on Anthropic and the same prompt on OpenAI-compatible, which is unacceptable and also demonstrably (the `summary` message) already latent in the codebase.
- **D-3 — Does the addendum replace `[SYSTEM NOTIFICATION]` for durable facts, or coexist indefinitely?** Recommend: narrow it, site by site, as §8 describes. No flag day, no dual-write once a site is migrated.
- **D-4 — Who may write the addendum: mailbox only, a new tool, the operator, or all three?** Recommend mailbox + operator for the first cut, with the tool as a second decision, because `ENGINEER_TOOLS` is exactly eight tools and adding a ninth is a visible contract change that deserves its own review.
- **D-5 — Tier 1 drain at the per-token poll: adopt?** Recommend yes. It is the moment §3 found, it costs one branch beside an existing one, and it touches neither the IR nor any adapter.
- **D-6 — Ordering: make injection order-insensitive (a), or restore `seq` to the wire (b)?** Recommend (a) now, (b) as a separate ticket, because (b) changes a stable contract for reasons that predate this work.
- **D-7 — Budget enforcement: refuse the write, or truncate oldest-first and report?** Recommend refuse-and-report for the operator path, evict-and-report for the mailbox path. Silent truncation of a remembered instruction is the worst of the three outcomes.

---

## 13. Rulings — Gen 54, Delivery Arc

Operator rulings, recorded verbatim where quoted, with the consequences each one forces inside the sections above. Where a ruling and a recommendation in §12 differ, the ruling governs; the recommendation is left in place as history rather than quietly rewritten.

### 13.1 Decisions closed

| # | Question (§12) | Ruling |
|---|---|---|
| D-2 | Composed into the single system string, or a second system message? | **Composed into the single system string.** |
| D-4 | Who may write the addendum: mailbox, tool, operator? | **Mailbox + operator first.** No ninth tool in `ENGINEER_TOOLS` yet — separate decision. |
| D-5 | Tier 1 drain at the per-token poll? | **Adopted.** |
| D-6 | Ordering: order-insensitive now, or restore `seq` to the wire? | **Order-insensitive now.** `seq` restoration is its own ticket. |
| §6.6 | Mode, or addendum? | **Addendum. The two must never share a field.** Mode is immutable; the addendum is a mutable session field. |

### 13.2 What each ruling changes in the text above

**D-2 closes §6.2 and §6.4, and makes P2 structural rather than advisory.** The addendum is composed into `system_prompt` before the `insert(0, …)` at `llm_client.py:177`. This is a commitment, not a preference: an implementation that adds a second system message is now a defect against this document. It also retires the §6.4 trap by construction, because there is no sibling message for `_find_main_prompt_and_summary` to misread.

**D-5 accepts §7.2 unchanged.** Tier 1 drains beside the existing halt check at `agent/core/agent.py:1367-1370`. No new thread, no new channel, no contention with the blocking `__anext__` — the drain runs *between* `__anext__` calls, which is the only window that exists and the window §3 established. Tier 0 (§7.1) remains the existing `_pending_config` mailbox, untouched; this ruling does not widen it.

**D-4 narrows §6.6's writer list to two.** In the first cut: the config mailbox, for system-originated facts; and the operator, via the config panel. An agent-side tool is explicitly deferred, so `ENGINEER_TOOLS` stays at eight. The consequence is worth stating plainly: **for the first cut the model cannot write its own memory.** An addendum entry exists only because the config layer or the operator put it there. That is the safer direction of error, and it keeps the §9 test obligations reachable without touching the tool registry.

**D-6 adopts §7.4 option (a) and puts an obligation on the payload.** Order-insensitivity is only true if the notification is self-describing and idempotent, so `addendum_updated` must carry `entry_id`, `ts` and the resulting entry set (or a convergent diff) rather than a delta whose meaning depends on arrival order; and every client arm must tolerate duplicate and out-of-order arrival by converging on the same display. Without that, "order-insensitive" is a claim about intent rather than a property of the wire. Option (b) becomes ticket T-1 below.

**§6.6 rules the mode question.** The addendum is a mutable session field, sibling to `summary`, and never shares a field with `mode`. The immutability at `config_manager.py:1180-1181` therefore cannot leak into the addendum — which is precisely what requirement 1 needs.

### 13.3 Still open

The rulings did not address these, and I have not inferred them. The recommendations in §12 stand as provisional, not settled:

- **D-1** — the addendum as its own session field rather than an entry list inside `metadata`. The §6.6 ruling uses the words "mutable session field", which is consistent with D-1 but is not the same statement. Confirm before the schema is written, because it fixes `to_persistable_dict` and `from_persistable_dict` (`session/models.py:326`, `:341`).
- **D-3** — how fast `[SYSTEM NOTIFICATION]` narrows (§8). The recommendation is site by site with no flag day; unruled.
- **D-7** — budget enforcement: refuse the write, or evict oldest-first and report (§6.5). Unruled, and it decides whether the addendum can silently lose an entry.
- **§5.5** — whether an unknown `type` on the main frontend switch is a no-op or a crash is still unverified. One `grep` settles it. This closes before implementation begins, not during, because the answer decides whether a P5 test is a regression guard or a bug fix.
- **§11.1–11.6** — six pre-existing defects, each wanting its own ruling.

### 13.4 Work items split off by these rulings

Filed separately, not part of the addendum implementation:

- **T-1** — restore `seq` to the wire: `event_forwarder.broadcast` (`web_ui/backend/event_forwarder.py:84-96`) and every hand-built bridge payload arm (`web_ui/backend/bridge.py:3144-3183`). Also the natural place to use the `session_id` argument the forwarder currently ignores.
- **T-2** — a ninth tool, so the model can write its own addendum. An `ENGINEER_TOOLS` contract change, deferred by D-4.
- **T-3** — correct the false premise at `docs/design-streaming-canonical-ir.md:18` (§3, §11.6). As written it will cause the next reader to re-reject the addendum for a reason the code refutes.
- **T-4** — mark `docs/design-interruptible-tool-calls.md` superseded by the IR document's `:98` (§11.2).
- **T-5** — `docs/agent-core-spec.md` stale line references (§11.1).
- **T-6** — the unverified frontend default arm (§5.5): one `grep`, then either a test or a fix.

### 13.5 What the rulings did not change

The invariants (§5) are untouched: P1 through P5 stand as written, and D-2 hardens P2 rather than relaxing it. The three-channel rule (§4.4) is unchanged — injection remains illegal on the provider channel, which is why the new kinds are runtime-channel kinds (§7.3). The non-scope list (§10) stands in full: no prompt caching, no interruptible tools, no backpressure or resume, no event-schema unification.

### 13.6 Next

An implementation brief for Design A (the addendum sink) and Design B (Tier 1 drain) follows this document's merge to `dev`. It should not be written against an unmerged design, because §13.3 leaves three schema-affecting questions — D-1, D-3 and D-7 — that a brief would otherwise have to guess at.
