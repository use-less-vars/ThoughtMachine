# Brief — System-Prompt Addendum: Implementation

**Status:** implementation brief. All three rulings are in (§4.0) — implementation may start. Slice order and gating in §6.
**Design:** `docs/design-sysprompt-addendum-and-injection.md`, 590 lines, on `dev` at `8a2e9e38` (merged in `9d964208`, PR #254) — see §0.1.
**Audience:** the implementing agent. Main rules; the agent implements slices 1-5.
**Authored by:** Gen 54 worker-side, from a verified read of `dev` at `9831e08b`; rulings recorded against `dev` at `9d964208`.

---

## 0. Provenance, and two housekeeping notes

### 0.1 The design doc is on `dev` (it was not, at authoring time)

When this brief was authored, `dev` = `origin/dev` = `9831e08b`, and asking git for `docs/design-sysprompt-addendum-and-injection.md` at that ref returned `fatal: path ... does not exist in 'dev'`. That gap is now closed: PR #254 merged as `9d964208`, so current `dev` carries the design doc. The design pass itself is three commits on `docs/sysprompt-addendum-design`, all now in `dev`'s history: `d8696a4c` (gitignore ticket), `bc02333e` (the design pass), `8a2e9e38` (§13 rulings).

#254 was briefly blocked by newly-enabled required status checks and had to be re-verified against a `dev` that had moved; it merged immediately before this brief. On current `dev`, every `§n` reference below resolves in the design doc and every `file:line` reference resolves in the same tree. **If you are on `dev` and the design doc is absent, that is a real defect — stop and report it** rather than working around it.

### 0.2 Push was not possible from the authoring machine

`network` was banned in the authoring footprint. Branch `docs/sysprompt-implementation-brief` exists locally and **has not been pushed**; the operator pushes it. Contents were verified by `sha256sum` and `wc -lc` at write time (numbers recorded in the commit message), **not** by CI — no pipeline ran on this branch, and nothing in this brief should be treated as CI-verified.

### 0.3 Verified vs unverified

Everything carrying a `file:line` citation in this brief was read from the tree. Two things are *not* independently verified and are flagged where they appear: (i) CI behaviour on any branch mentioned anywhere in this arc — no network in the authoring footprint; (ii) which of the two event schemas is live at runtime — `agent/events.py` `EventType` versus the turn-loop dicts. The design does not depend on that answer (§3, out of scope), so it was not chased.

---

## 1. What this is, and the gate

The design pass established one thing cheaply: **the sync loop already contains a per-token moment in which the agent can observe new state.** `agent/core/agent.py:1367-1370` polls for a halt once per stream event — that is roughly per token. The `design-streaming-canonical-ir.md:18` objection that there is "no moment in the sync loop" rested on a false premise. The moment existed; the *sink* did not. Everything below follows from adding a sink at a moment that already exists, which is why the design is cheap.

The sink is a **system-prompt addendum**: a small, mutable, session-scoped block of text appended to the composed system prompt. Updatable at the next token boundary, survives context compaction, persists with the session.

**The gate.** Three schema questions were open, and each changed the shape of the code, the shape of the tests, and the cost of being wrong. They are §4.1 (D-1), §4.2 (D-3), §4.3 (D-7). **All three are now ruled (§4.0) — production code may start.** The cost of waiting is one round trip; the cost of guessing is at least one rewrite — D-1 decides the session schema, D-7 decides whether the addendum can silently lose an entry, D-3 decides whether a 14-site notification migration can even be in the same PR.

Everything *else* is settled. §2 lists the rulings that are already binding. Anything in this brief that contradicts them is a bug in the brief.

---

## 2. Binding constraints (do not relitigate)

From the design pass's §13.1. Non-negotiable for this implementation. If one turns out to be wrong, stop and escalate — do not work around it, because every workaround re-introduces the failure the constraint exists to prevent.

**C1 — D-2: the addendum is composed *into* the single system string.** Never a second `role='system'` message. This makes test obligation P2 (exactly one system message in the request) a **defect condition**, not an aspiration, and it retires the §6.4 demotion trap by construction: with no second marker-less system message in the array, `_find_main_prompt_and_summary` (`session/context_builder.py:342-382`, which takes the *first* marker-less system message as the main prompt at `:357`) has nothing to mistake for the real prompt, so the real prompt cannot be demoted to `system_warning` (`:253-259`).

**C2 — D-4: writes arrive via the mailbox and the operator only.** No ninth entry in `ENGINEER_TOOLS`. The consequence was named and deliberately accepted in §13: **for the first cut the model cannot write its own memory.** It can read the addendum — it is in the system prompt — and the operator can update it; the model cannot. T-2 (§3) is the ticket that lifts this, and lifting it is a separate decision with its own blast radius (prompt-injection surface, tool-loop budget, undo semantics).

**C3 — D-5: Tier 1 drains at the per-token poll.** Not per turn, not per request. The drain site is beside the halt poll at `agent/core/agent.py:1367-1370`. A per-turn drain silently degrades the feature into a Tier 0 mechanism with more code; if you find yourself needing a per-turn drain to make a test pass, the test is wrong.

**C4 — D-6: order-insensitive now.** `addendum_updated` carries `entry_id`, `ts` and the **resulting entry set**; the receiver is idempotent and tolerant of duplicates and of out-of-order arrival. Do **not** build ordering on `seq`. `seq` is stamped at `agent.py:1074-1083` and then discarded before the client sees it (`event_forwarder.py:84-96`, `bridge.py:3144-3183`), and `event_forwarder.broadcast` ignores its `_session_id` argument. Restoring `seq` is T-1, out of scope.

**C5 — §6.6: the addendum is a mutable session field, and it never shares a field with `mode`.** `mode` is immutable for the life of a session (`agent/config_manager.py:1180-1181`); the addendum is mutable by definition. A shared field would give the schema an immutability guarantee that is false for half its contents.

---

## 3. Scope

### In scope

1. Session schema and persistence for the addendum — **unblocked (D-1: dedicated field)**.
2. Composition of the addendum into the system string, in the right place, over the right base text.
3. A survival test across real compaction.
4. The Tier 1 drain, the `addendum_updated` runtime event, the bridge arm, the frontend arm.
5. Budget enforcement — **unblocked (D-7: refuse)**, and it must surface the refusal in the UI (§4.0).
6. Narrowing `[SYSTEM NOTIFICATION]` sites — **ruled out of this implementation** (D-3: defer, §4.2). Tracked as a named backlog ticket, not silently dropped.

### Out of scope — split-off tickets

Listed so that nobody rediscovers them mid-implementation and calls them in-scope.

- **T-1** restore `seq` on the wire — `event_forwarder.py:84-96`, `bridge.py:3144-3183`, and the ignored `session_id` argument at `event_forwarder.broadcast`.
- **T-2** a ninth `ENGINEER_TOOLS` entry so the model can write its own memory (see C2).
- **T-3** correct the false premise at `design-streaming-canonical-ir.md:18`.
- **T-4** mark `design-interruptible-tool-calls.md` superseded — 14 lines, contradicting IR `:98`.
- **T-5** stale `agent-core-spec.md` line references.
- **T-6** ~~frontend `default:` arm~~ — **closed, see §5.** No ticket needed; the arm already exists and works. The obligation there is a regression guard, not a fix.
- **T-9** a UI surface for the addendum — a panel where the operator can read and edit memory. Not this wave (Q6); slice 4's frontend obligation is event handling only.
- **T-10** a real tokenizer at the write boundary, replacing `len(text)`. Required before `budget_chars` can honestly become `budget_tokens` again (Q1).

Also out of scope, from design §10: prompt caching (there is none in the repo — which is precisely why P4 is cheap to satisfy), interruptible tool calls, backpressure/resume, and any unification of the two event schemas. On that last point: whether `agent/events.py:114-118` / `:131` / `EventType:23` or the turn-loop dicts are live at runtime was **not established**. The design does not depend on the answer. Do not go looking — it is a rabbit hole with no bearing on the deliverable.

---

## 4. The three rulings

Each of these changes the code shape, the test shape, and the cost of being wrong. For each: the question, the two paths, what each path forces you to touch, **what the test looks like under each path** (asked for explicitly, so that the ruling can be made by reading the test rather than by reasoning about the code), the recommendation that was made, and what the cost would have been had the ruling gone the other way. The three rulings are recorded in §4.0; the path analysis below is kept as the record, not as homework.

A note on how to read the tests below: none of them are integration tests. Every one is a unit test over a pure function or an in-memory session object. If a proposed test needs a live model, the test is wrong.

### 4.0 Rulings — all three settled

| # | Ruling | What it forces |
|---|---|---|
| **D-1** | **Dedicated session field** (Path A) | Slice 1 touches the `session/models.py` and `session/store.py` serialisers. `metadata` is explicitly rejected: an untyped blob that is echoed to the client and reinterpreted at every read site is a trap. |
| **D-3** | **Defer** (Path B) | Slice 6 does not ship. The 14 sites become a named backlog ticket, and the §4.2 Path B obligations — coexistence and attribution — are locked in with it. |
| **D-7** | **Refuse** (Path A), **and fail visibly** | The refusal surfaces in the UI, not only to the caller. See below. |

Two notes on D-3 and D-7.

- **D-3's deferral is conditional on the obligations being *recorded*, not on good intentions.** The coexistence and attribution tests in §4.2 are what stop "defer" becoming "never". The backlog ticket must carry them and must name all 14 sites.
- **D-7's ruling is stronger than the recommendation it accepted.** Refusing is only half of it: the refusal has to be *visible in the UI*, not merely returned to the caller. The carrier is the design's Tier 2, UI-only runtime kind — `runtime_notice` (design §7) — which must **not** enter `StreamEvent`. That is why **slice 5 cannot land before slice 4 has built the client channel** that carries it.

### 4.1 `[RULED — dedicated session field]` — D-1: where does the addendum live on the session object?

**The question.** The addendum needs a storage shape. Two candidates: a first-class session field, or a list of entries tucked into the existing `metadata` dict.

**Path A — a dedicated session field** (sibling to `summary`).

Forces changes in exactly two files:
- `session/models.py` — `to_persistable_dict` at `:326`, `from_persistable_dict` at `:341`.
- `session/store.py` — the corresponding persist/load pair at `:424` and `:463`.

Proposed shape, from design §6.5:

```python
{
  "entries": [
    {"id": str, "ts": str, "source": "operator|mailbox|tool", "text": str, "pinned": bool}
  ],
  "budget_chars": int,
}
```

**The test under Path A** — one round-trip test, one default test, one tolerance test:

1. *Round-trip.* Build a session with three entries: one plain, one `pinned=True`, one containing multi-line text and non-ASCII. Persist with `to_persistable_dict`, reload with `from_persistable_dict`, assert the entry set is **byte-equal** after JSON normalisation, including order and ids. This is the same assertion as test obligation 7 in §7.2, and under Path A it is a one-liner because the field is already carried by both serialisers.
2. *Default.* Load a persisted session dict that **predates the field** (no key at all). Assert it loads without raising and yields an empty entry list. This is the backward-compatibility test, and under Path A it must exist, because the field is new.
3. *Forward tolerance.* Load a session dict whose addendum contains an unknown key (e.g. a field added by a later version). Assert it does not raise. Cheap, and it buys us the right to add fields later without a migration.

**Path B — an entry list inside the existing `metadata` dict.**

Forces changes in **no** serialiser: `metadata` is already persisted, so `session/models.py` and `session/store.py` are untouched. Everything concentrates at the read sites — whatever composes the system string, plus anything that treats `metadata` as display state.

**The test under Path B** — same three ideas, plus one that Path A does not need:

1. *Round-trip.* Same assertion, but it passes trivially because `metadata` already round-trips. It proves nothing new — and that is the honest cost of Path B: **the persistence test stops being a test of the addendum.**
2. *Default.* Absent `metadata` key yields an empty entry list.
3. *Forward tolerance.* Same as A.
4. *Plumbing.* A test proving that `metadata` actually reaches the composition point in `agent/core/llm_client.py:144-179`. Under Path A the field is on the session object that composition already receives; under Path B you must verify the dict survives whatever strips session objects down for the LLM call, and that nothing between the store and the composer recreates `metadata` from a whitelist. **This test exists only to prove Path B works at all**, which is a signal about Path B.

**The estimator and the two caps (Q1, Q2).** `budget_chars` is measured with `len(text)` on each entry's text, summed over the set. There is no tokenizer at the write boundary, so a character count is the only provider-neutral measure available — and it errs conservatively, which under D-7's refuse semantics costs a needless refusal, never a silent loss. The entry count is capped separately at **32** (Q2): a hundred tiny entries and three large ones behave very differently in a prompt, and the character budget bounds only the second case. A real tokenizer is **T-10** (§3).

**Risks specific to Path B.** `metadata` is a free-form dict. Nothing in the schema says what is in it, which means: (i) anything that echoes `metadata` to the client echoes the model's memory text too; (ii) anything that summarises or truncates display state can touch it; (iii) an entry list in an untyped dict has no place to express the `pinned` and `budget_chars` semantics that D-7 depends on, so Path B makes D-7 harder, not easier. There is a real, if small, chance that Path B leaks internal memory text to the UI.

**Ruling: Path A — dedicated session field.** (Recommended, and accepted.) Two reasons. First, design §6.6 (C5) already treats the addendum as *a mutable session field* — that phrasing is a constraint on mutability, not on storage, but a first-class field is the shape the rest of the design assumes. Second, Path B defers a schema decision while making every read site responsible for interpreting an untyped blob; the saving is two call sites and the cost is paid at every future read.

**Cost had the ruling gone the other way.** If Main rules B, the wasted work is small: two serialiser call sites and a shape — a localised revert, no data migration, because nothing has shipped. If Main rules A and we had built B, the follow-up is a real migration with real persisted sessions in the wild. **The asymmetry favours A**, which is why A is the recommendation even though it is the more committed option.

### 4.2 `[RULED — defer]` — D-3: how fast do we narrow `[SYSTEM NOTIFICATION]`?

**The question.** There are 14 sites that emit a `[SYSTEM NOTIFICATION]` message with `role='user'`, marker defined at `agent/.../message.py:16`. Design §8 gives the rule for what belongs where: **something that *happened* is a notification; something that is *true now* is an addendum.** Applying that rule across 14 sites is a migration with a blast radius much larger than the addendum itself. Does it belong in this implementation?

**Path A — narrow inside this implementation,** one slice, all 14 sites, one PR.

Forces touching every site that emits the marker, plus every test that asserts on prompt composition, because the composed prompt for those paths changes shape.

**The test under Path A** — for each converted site, a pair of assertions, and one global one:

1. *Per site:* the fact no longer appears as a `role='user'` message in the built request, **and** appears in the composed system string once the addendum is populated. Two assertions, both cheap, both on the same built request.
2. *Per site:* the message array's role multiset is unchanged except for the removed user message (this is P1 from §7.2 — no fabricated roles — applied as a regression net across 14 conversions).
3. *Global:* a test asserting zero `[SYSTEM NOTIFICATION]` emissions on the paths exercised. Without this, a half-finished conversion passes the per-site tests while still emitting the old channel, and you get both channels for the same fact — the worst outcome.
4. *Global:* every pre-existing test that asserts a notification is present gets updated. **Budget for this**; under Path A, test churn is larger than code churn, and that is the strongest argument against Path A.

**Path B — ship the addendum alongside the unchanged notification channel; narrow later,** per-site, in follow-up PRs.

Forces **no** changes to the 14 sites. All existing notification tests stay green untouched. The 14 sites stay listed in design §8 as the backlog.

**The test under Path B** — different in kind, not just in size:

1. *Coexistence:* a fact that exists in both channels simultaneously does not appear twice in the built request — i.e. no duplicate text, no duplicate user message. The test can be as blunt as asserting the fact string appears exactly once across the serialised request.
2. *Attribution:* a table-driven test that asserts, for a named set of facts, which channel each one is in. This is the test that keeps Path B honest: without it, the two channels drift and the same fact lands in both.
3. *No change* to the 14 existing tests. Under Path B that is a feature, and it is the whole argument: zero churn on the most-touched assertions in the codebase.

**Ruling: Path B — defer, with a hard commitment.** (Recommended, and accepted.) The reasoning is not that narrowing is unnecessary — it is that Path B keeps the addendum PR reviewable. A PR that adds a feature *and* rewrites 14 prompt-composition sites is a PR nobody can actually review, and the failure mode under Path A is subtle: a botched conversion can fabricate a role (violating P1) or silently drop a fact, and 14 conversions in one diff is 14 chances to do that invisibly. Path B's risk is the opposite kind — two channels for "current state" indefinitely — which is visible, boring, and fixable per site. **The commitment:** the follow-up must be filed as a ticket that names all 14 sites, so Path B does not become "never".

**Cost had the ruling gone the other way.** If Main rules A and we built B, the remaining work is identical to what A would have been — same sites, same conversions — just scheduled later. Nothing is thrown away. If Main rules B and we built A, we would have shipped an unreviewable PR. Again the asymmetry favours the smaller commitment.
### 4.3 `[RULED — refuse]` — D-7: what does the budget do when it is full?

**The question.** The addendum has a `budget_chars` ceiling. Writes can arrive from the operator or the mailbox, in any order, while a turn is running. What happens on the write that would exceed the ceiling?

Two candidate semantics, and they differ in one respect only — but it is the respect that matters for a memory feature:

**Path A — refuse the write.** The over-budget write is rejected; the entry set is unchanged; the caller is told why.

**Path B — evict oldest-first and report.** The write always succeeds; the oldest non-pinned entry is dropped to make room; the response names what was evicted.

**Enforcement point — the same under both paths, and worth stating because it is easy to get wrong.** The budget is enforced **at the write boundary** — the setter that the mailbox drain and the operator path both call — not at composition time. Enforcing at composition means the same over-budget state gets rejected on every single turn, forever, and the operator's write appears to succeed and then silently never takes effect. That is the worst of both paths.

**The test under Path A:**

1. *Refusal.* A write that would exceed the budget is rejected: the entry set is byte-identical before and after, and the caller receives an error rather than a silent success. Assert on the entry set, not just on the return value — a rejected write that mutates on its way to being rejected is the bug this test exists for.
2. *Boundary.* A write that lands *exactly* at the ceiling succeeds. Off-by-one at a token boundary is the most likely defect and the cheapest to pin.
3. *No queued side effect.* The rejected write leaves nothing behind in the Tier 1 drain queue — i.e. rejection happens before enqueueing, not after. This is the test that catches an implementation that accepts into the queue and drops later.
4. *Pinned.* A `pinned` entry cannot be refused for budget reasons in a way that would make pinning meaningless — under Path A this reduces to: the budget check counts pinned entries, and the operator is told which ones are consuming it. Pinning is a promise about eviction, so under Path A the interesting assertion is just that the error message enumerates the pinned occupants.

**The test under Path B:**

1. *Eviction.* The write succeeds, the oldest non-pinned entry is gone, the new entry is present, and **the response names the evicted `entry_id`**. The naming is the whole point of Path B: eviction without reporting is silent data loss with extra steps.
2. *Pinned is never evicted.* Fill the budget entirely with pinned entries, then attempt another write. Under Path B the correct behaviour is to refuse anyway — eviction cannot free anything, and an implementation that evicts a pinned entry to make room has silently broken the `pinned` contract. **This corner exists under both paths**, so it must be tested under either ruling; only the framing differs.
3. *Oversized single entry.* One entry larger than the entire budget: refuse, under both paths, because even evicting everything cannot fit it. Testing this prevents an infinite eviction loop, which is a real way to hang a turn.
4. *Report survives the wire.* The evicted `entry_id` reaches the client in the `addendum_updated` event, so a UI can show that memory shrank. Under Path B this is not optional reporting hygiene — it is the only mechanism by which silent loss becomes non-silent.

**Ruling: Path A — refuse, and report it visibly.** (Recommended, and accepted — plus one requirement beyond the recommendation: the refusal must reach the UI, not only the caller, carried by the `runtime_notice` kind that slice 4 builds. See §4.0.)

A memory feature that can silently lose an entry is a memory feature nobody can trust, and the failure is invisible: the model simply stops knowing something, and no one can tell whether that is a bug or the budget working as designed. Refusal is a visible, recoverable, operator-facing failure — the operator writes again, smaller, and knows the state of the world. Path B's advantage is that the write never bounces, which matters if writes were frequent and automated; they are not, because per C2 they come from the operator and the mailbox, which is human-paced.

There is a second reason, and it is the one that decided it: **under Path A the `pinned` flag is nearly redundant, and that is fine.** Under Path A the semantics are complete without eviction — pinning exists so that a future Path B (or a future T-2, where the model writes) has a safety apparatus already in the schema. Under Path B, `pinned` is load-bearing from day one, which means a `pinned` bug and a budget bug interact, and the tests multiply.

**Cost had the ruling gone the other way.** If Main rules B, the additional work is one eviction function and its four tests — small, additive, and it does not invalidate any of the Path A tests except the refusal assertion. If Main rules A and we had built B, we ship a memory feature whose losses are only detectable by grepping logs. The asymmetry favours A.

**The one thing the ruling left open is now settled (Q1).** The unit is `budget_chars`, measured with `len(text)` summed over the entry set. There is no tokenizer at the write boundary, and a character count is provider-neutral, so it cannot be wrong in a provider-dependent way — it can only be a *conservative* approximation of tokens, which under Path A costs a needless refusal rather than a silent eviction. A real tokenizer is **T-10** (§3).

---

## 5. §5.5 is resolved — the P5 obligation is a regression guard, not a bug fix

The design pass listed, in §11, a suspicion that the frontend's event switch had no `default:` arm, and left it open. That suspicion is **wrong**. The arm exists, it is two lines long, and it works. This section records what was verified so nobody reopens it, and — more usefully — explains why the obligation nevertheless got *sharper* rather than disappearing.

### 5.1 What is actually there

`web_ui/frontend/src/components/SessionTab.jsx`, 1422 lines. `function handleEvent(msg)` opens at `:557`. A `conversation_changed` special case short-circuits it at `:560-566` with a `console.log`. Then `switch (msg.type)` opens at `:567` and closes at `:1135`; `handleEvent` itself closes at `:1136`.

**`default:` is present at `:1133-1134`:**

```js
console.warn('[SessionTab] Unknown event type:', msg.type)
```

Two lines. No comment, no store mutation, no `throw`, no UI artefact. **The behaviour is a logged no-op:** the only side effect of an unrecognised message is one console warning.

`handleEvent` is not internally try/catch'd. Its sole call site is `ws.onmessage` at `:350-357`, which wraps `JSON.parse` and the `handleEvent` call in a `try` and logs parse failures with `console.error('[SessionTab] Failed to parse message:', ...)`. That catch is generic and does not re-throw or set a session error state. Because `default:` never throws, **an unknown event type never reaches that catch** — the two mechanisms are sequential, not redundant.

### 5.2 The worker path is materially different, and the difference is the point

The session path above is one of two frontends consuming events. The worker path arrives via `web_ui/frontend/src/components/chat/adaptWorkerEvent.js:500-509`, and an unknown event there does something visibly different: it returns a synthetic system-notification bubble with `content: `${UNKNOWN_EVENT_TEXT}${evt.event}`` and `is_system_notification: true`, where `UNKNOWN_EVENT_TEXT = 'Unknown event: '` is defined at `:21`. The behaviour is asserted at `__tests__/adaptWorkerEvent.test.js:896`. (Note it interpolates `evt.event`, not `evt.type` — the two adapters key off different fields, which is itself worth knowing when writing the new arm.)

So: **on the worker path an unknown event surfaces visibly to the user; on the session path it is dropped with a console warning.**

### 5.3 What that means for the P5 obligation

Design §5's P5 is: *every new kind gets an explicit arm at every layer.* Given the above:

- **It is a regression guard, not a bug fix.** There is nothing to repair in `SessionTab.jsx`; T-6 as originally conceived is **closed**, and §3 records it as such. Do not file it. Do not "add the missing default arm" — you would be adding a second one.
- **The guard is still mandatory, and for a better reason than "P5 says so".** On the session path, forgetting the arm is *invisible*: `addendum_updated` would arrive, hit `default:`, and be dropped with one line in a browser console that nobody reads. The addendum would appear to work — the system prompt would still update on the next turn, because that path is server-side — while the UI never reflected it. Debugging that starts with "the UI is wrong" and ends three hours later in a console log.

So the guard must assert **two** things, not one:

1. **The explicit arm exists and does something.** A test that dispatches an `addendum_updated` message through `handleEvent` and asserts the resulting state change — not merely that it does not throw. A test asserting "does not throw" passes on `default:`, which is exactly the failure mode being guarded against. If the arm only logs, the test proves nothing.
2. **A genuinely unknown type still hits `default:` and does not throw,** so the next person to add a kind gets the warning rather than a crash. This is a cheap two-line test and it pins the existing behaviour so that a future refactor of the switch cannot turn silent-drop into exception-in-`onmessage`.

One consequence for the slice plan (§6, slice 4): the frontend arm and its test land **together** with the bridge arm, in the same slice. Split across slices, the guard is written against an event kind that does not yet exist on the wire, and it degrades into a test of the test.

---

## 6. Slice plan

Six slices. Each is independently reviewable and independently revertable, and each is ordered so that the tests it needs can be written against code that already exists. With the rulings in §4.0: slices 1-4 are unblocked now, slice 5 is unblocked but depends on slice 4 having built the client channel that carries the visible refusal, and slice 6 does not ship in this implementation. Estimated blast radius per slice — the persisted-dict keys and the runtime event kinds each one adds — is §13.

### Slice 1 — schema and persistence · **unblocked (D-1: dedicated field)**

Add the addendum's storage shape and both serialiser directions. Defaults for a session that predates the field. **No behaviour change:** nothing reads the field yet, so the only way this slice can break anything is by being wrong about defaults, which is what its tests are for.

Touches: `session/models.py` (`to_persistable_dict` `:326`, `from_persistable_dict` `:341`) and `session/store.py` (`:424`, `:463`) — under Path A. Under Path B this slice collapses to a dataclass and three tests, and shrinks accordingly.

Done when: round-trip is byte-equal, absent field yields an empty default, unknown keys do not raise, and the whole existing session test suite is untouched and green.

### Slice 2 — composition into the single system string · **carries C1 and P2/P4**

This is the slice where the design either works or silently does not. The addendum is composed into the system string inside `agent/core/llm_client.py:144-179`, **after** the deletion of every non-summary system message at `:170-174` and **before** `insert(0, ...)` at `:177`. That ordering is not stylistic: compose before `:174` and your new text is deleted on every turn, silently, and the surviving tests would all still pass because the base prompt survives.

The base text is `resources/<mode>_system_prompt.txt`, resolved via `agent/config/models.py:270-280` — **not** the root `system_prompt.txt`, which is only a fallback at `llm_client.py:128-142`. Composing onto the fallback produces a prompt that looks plausible in a test that does not assert the mode text, and wrong in production.

Done when: exactly one `role='system'` message is present in the built request, asserted **per adapter** — the Anthropic assertion must run through Anthropic's own conversion, because Anthropic's system handling is last-wins (`anthropic_provider.py:88-91` and `:122`) and coerces non-user roles (`:112`). A generic-array assertion passes while Anthropic silently drops the base prompt.

### Slice 3 — survival across compaction

A test, not a feature. Run a **real** `SummarizeTool` compaction and assert the addendum is still present in the next request; then do it **twice**. Twice matters: the first compaction is the one you designed for, the second is the one nobody tests and where a re-composition bug shows up as text duplication rather than disappearance.

The structural argument that survival is free (pruner 1 replays the main prompt at `session/context_builder.py:284-291` / `:357` with `keep_turns=5` at `:212`; pruner 2 cannot reach a non-message at `history_pruner.py:164-165` with `keep_system_notifications=False` at `:68`) is exactly that — an argument. This slice converts it into an assertion. Do not skip it on the grounds that the argument is sound; the argument is sound *given* slices 1 and 2 are correct, and something has to check that.

### Slice 4 — Tier 1 drain, both runtime events, bridge arm, frontend arm · **carries C3 and C4**

The drain sits beside the halt poll at `agent/core/agent.py:1367-1370` — per stream event, which is roughly per token. **This slice builds two runtime kinds, not one** — Main caught the one-event version in review, and the correction is load-bearing rather than cosmetic. `addendum_updated` is the feature channel. `runtime_notice` is the Tier 2, UI-only carrier that must exist *before* slice 5 can refuse visibly (D-7, §4.0): building only `addendum_updated` leaves the budget with no way to reach the user's eyes, which is exactly the failure D-7's ruling exists to prevent. Both follow the precedent of `tool_call_start` at `agent.py:1654-1659` — turn-loop dicts, **not** `StreamEvent`s. That distinction is a hard rule from design §4 — the provider channel (`agent/core/stream_events.py:108-119`) is a closed, content-only union, and injection is legal on the runtime and client channels only.

The bridge arm renames per arm as `bridge.py:3144-3153` already does for existing kinds. The frontend arm and its guard land **in this slice** (§5.3) — a guard written before the event exists on the wire is a test of a test.

Done when: `runtime_notice` reaches the session UI as a rendered notice, asserted by a state change and not by absence of exception; a duplicate `addendum_updated` carrying the same entry set is idempotent; an out-of-order pair converges on the same entry set (C4 — order-insensitive, no reliance on `seq`); a genuinely unknown kind hits the frontend `default:` without throwing; and the new kind's frontend arm is asserted by a *state change*, not by absence of exception.

### Slice 5 — budget enforcement · **unblocked (D-7: refuse) — must surface the refusal in the UI**

Enforced at the write boundary, not at composition (§4.3). D-7 ruled **refuse, and report it visibly** (Path A): an over-budget write is rejected, the entry set is unchanged, and the refusal is surfaced through the `runtime_notice` channel slice 4 built — to the UI, not only to the caller. Lands after slice 4 for exactly that reason: without a notice channel, "refuse" degrades into a silent no-op, the worst of the three outcomes.

### Slice 6 — narrowing `[SYSTEM NOTIFICATION]` · **deferred out of this implementation (D-3)**

D-3 ruled defer, so this slice does not ship in this implementation; the 14 sites stay a named backlog ticket. If that decision is ever reversed to Path A, this is the largest slice by test churn and should still land **last**, after everything above is merged and green, so that a bisect over prompt-shape changes has a clean starting point.

---

## 7. The seven test obligations, mapped to slices

From design §9. Restated here with the slice that satisfies each, because the mapping is the part that gets lost.

| # | Obligation | Slice | Note on how it fails if done carelessly |
|---|---|---|---|
| 1 | **P1 — no fabricated roles.** The role multiset of the built request is exactly what it was before the addendum, unchanged — slice 6 does not ship (D-3). | 2 | Converting a notification into a fabricated `assistant` or `system` role to "make it stick" passes a naive presence assertion. Assert the multiset, not the presence. |
| 2 | **P2 — exactly one `role='system'`, asserted per adapter.** | 2 | Asserting on the shared array only. Anthropic is last-wins (`anthropic_provider.py:88-91`, `:122`), so a second system message vanishes silently there while the generic assertion stays green. |
| 3 | **P3 — an in-flight completion is unaffected.** The request for a turn that is already streaming is byte-identical whether or not a write lands mid-turn. | 4 | Testing only that "the next request differs". The obligation is about the request that must *not* change. |
| 4 | **P4 — base prefix byte-stable, append-only, oldest-first.** | 2 | Re-sorting entries by `ts` or injecting at the front. Both produce a correct-looking prompt that is not append-only, and both are invisible without a byte comparison against the previous request. There is zero prompt caching in the repo, so this is cheap to satisfy and cheap to break undetected. |
| 5 | **P5 — every new kind has an explicit arm at every layer; unknown kinds do not crash.** | 4 | See §5. A "does not throw" assertion passes on the existing `default:` and therefore tests nothing. |
| 6 | **Survival — a real `SummarizeTool` compaction, then the addendum is present in the next request; twice.** | 3 | Stubbing the compaction instead of running it. The stub encodes your assumption about pruner behaviour, which is the thing under test. |
| 7 | **Persistence — write, reload through `session/store.py`, byte-equal.** | 1 | Asserting equality of two in-memory objects instead of going through the store. The store is where the shape actually gets projected. |

Two cross-cutting notes. First, obligations 2 and 4 are the two that a reviewer should look at hardest, because they are the two whose failure is silent in production and green in CI. Second, obligation 3 is the only one that needs any notion of concurrency; if it proves awkward, implement it as: build request A, apply a write, assert the already-built request A object is unchanged by identity and content, then build B and assert B differs from A only in the system string. That formulation needs no threads.

---

## 8. Implementation hazards

Seven ways this goes wrong quietly. Each is a real failure mode found during the design pass, not a hypothetical.

**H1 — The demotion trap.** `_find_main_prompt_and_summary` (`session/context_builder.py:342-382`) identifies the main prompt as the **first marker-less system message** (`:357`). Anything else that looks like a bare system message can be mistaken for it, at which point the real prompt is reclassified as `system_warning` (`:253-259`) and the model is told, in a warning, that its instructions are a warning. **C1 makes this structurally impossible** by forbidding a second system message — but the guard against reintroducing it is a test asserting exactly one system message, which is obligation 2. Do not "temporarily" add a second system message to debug something and then forget.

**H2 — Anthropic's last-wins system handling.** `anthropic_provider.py:88-91` and `:122`. If two system messages ever do reach Anthropic, the later silently replaces the earlier — no error, no warning, and the base prompt is simply gone. The same coercion rule at `:112` turns any non-user role into `user`. This is why obligation 2 is asserted **per adapter**: the shared array is not the thing that has to be correct.

**H3 — The wrong base prompt file.** The real base text is `resources/<mode>_system_prompt.txt`, resolved through `agent/config/models.py:270-280`. The root `system_prompt.txt` is a fallback used at `llm_client.py:128-142` when mode resolution fails. Composing onto the fallback yields a prompt that reads fine, passes a presence test, and is missing the entire mode prompt in production. Any slice 2 test should assert a distinctive string from the *mode* prompt, not merely that some prompt text is present.

**H4 — Composition in the wrong place.** `llm_client.py:170-174` deletes every non-summary system message on every turn; `insert(0, ...)` at `:177` is what survives. Compose anywhere before `:174` and your addendum is deleted every turn — and every test that asserts "the base prompt is intact" still passes, because the base prompt *is* intact.

**H5 — The ladder's missing `else`.** The halt poll at `agent.py:1367-1370` sits above a dispatch ladder whose branches around `agent.py:1421-1422` do not all have an `else`. A drain placed inside a ladder branch rather than beside the poll will therefore be skipped on exactly the path you did not think about — which is the path a real turn takes when the model emits a kind you did not enumerate. **This was flagged in the design pass and should be re-verified against the code at implementation time**; treat it as a known-unknown, not as settled fact.

**H6 — Expressing current state as a notification.** `history_pruner.py:68` sets `keep_system_notifications=False`, and `:164-165` is where notifications are dropped. Anything expressed as a notification is therefore *prunable by design* — which is correct for a transient fact and fatal for a standing one. This is the mechanical reason for the §8 rule (*happened* → notification; *true now* → addendum), and it is also the reason slice 6 must not convert standing facts into notifications "just to get them in front of the model".

**H7 — Routing writes through the Tier 0 boundary.** `_pending_config` (`agent.py:417-428`, drained at `:1120-1123`) and the bridge's second queue (`bridge.py:2139-2155`, `:2969-2981`) already exist and already work. They drain at the turn boundary. Reusing them for addendum writes is tempting and defeats the entire design — it is C3's failure mode, and it will pass every test except the one about mid-turn visibility.

---

## 9. The six questions — answered

Raised as smaller-than-§4 questions, on the grounds that each would otherwise be decided by whoever wrote the code first. All six are now answered by Main, the answers are binding, and each answer is folded into the section it touches rather than left here as prose.

1. **What is the unit of the budget, and which estimator?** No token estimator was established as available at the write boundary, and a character-count proxy is wrong by a variable factor across providers. Path A tolerates a conservative estimate (cost of being wrong: a needless refusal); Path B does not (cost: wrongful eviction). If the answer is "a character budget, renamed", say so, and D-7 gets much simpler. **Answered (Q1):** the field is `budget_chars`, measured with `len(text)` summed over the entry set — provider-neutral, conservative by construction, and honest about what it counts. A real tokenizer is **T-10** (§3), out of scope this wave.
2. **Is there an entry-count cap distinct from the character budget?** A hundred tiny entries and three large ones behave very differently in a prompt, and only one of the two is bounded by a character ceiling. **Answered (Q2):** yes — **32 entries**, enforced at the same write boundary as the character budget and independently of it.
3. **What are the legal values of `source`?** The proposed shape carries a `source` string per entry. If it is free-form, nobody will ever filter by it; if it is an enum, it needs listing now. **Answered (Q3):** an enum — `operator | mailbox | tool`. Nothing else is legal; the loader validates on read rather than trusting the persisted file.
4. **Can the operator write while a turn is in flight, and does it queue or apply immediately?** Under C3 an in-flight write applies at the next token boundary — but the *operator* path does not go through the mailbox, so its synchronisation is a separate question and the answer determines whether slice 4 needs a lock. **Answered (Q4):** the operator path calls the **same write-boundary function and the same queue** as the mailbox, so there is no separate lock and no second entry point where the budget check could be forgotten. An in-flight write still applies at the next token boundary (C3).
5. **Does `addendum_updated` carry the full entry set every time, or a delta?** C4 says the resulting entry set, for idempotence. That is the right call for correctness and it means message size grows with memory size. Confirm, or name a cap. **Answered (Q5):** the full entry set every time. Message size grows with memory, but memory is now bounded by 32 entries and `budget_chars`, so that is bounded growth.
6. **Is there a UI surface for the addendum in scope?** The design treats the frontend arm as an event-handling obligation only. If the operator is meant to *see* memory, that is additional work in slice 4 and should be named before the slice is estimated. **Answered (Q6):** not in this wave. The frontend obligation stays event handling; a visible memory panel is **T-9** (§3), out of scope, and slice 4 is estimated without it.

---

## 10. Where to start

1. Read `docs/design-sysprompt-addendum-and-injection.md` on `dev` at `8a2e9e38` (§0.1). §3 (the pivot) and §13 (the rulings) are the parts that change how the rest reads.
2. ~~Rule on §4.1, §4.2 and §4.3.~~ **Done — see §4.0.**
3. Start slices 1 and 2 together — both are unblocked.

What **not** to do: do not add a `default:` arm to `SessionTab.jsx` (§5.3 — it exists), do not restore `seq` on the wire (T-1), do not add a ninth `ENGINEER_TOOLS` entry (C2/T-2), and do not route writes through `_pending_config` (H7).

---

## 11. Record

| | |
|---|---|
| Branch | `docs/sysprompt-implementation-brief`, cut from `dev` at `9831e08b` |
| Pushed | **No.** `network` was banned in the authoring footprint; the operator pushes this branch. |
| CI | **Not run.** Nothing in this brief is CI-verified. |
| Contents verified by | `wc -lc` and `sha256sum` at write time; the authoritative numbers and hash are recorded in the commit message, since a file cannot contain its own hash. |
| Design source | `docs/design-sysprompt-addendum-and-injection.md`, 590 lines, `8a2e9e38` |
| Tree read for every `file:line` | `dev` at `9831e08b`; rulings recorded against `dev` at `9d964208` |
| Rulings | D-1 dedicated session field · D-3 defer · D-7 refuse (visibly) — recorded in §4.0 |
| Inversions / Ripple | §12 / §13 — the assumptions this design overturns, and the per-slice blast radius |
| Supersedes | Nothing. This is the first implementation brief for the addendum. |

Two claims in this document are explicitly **not** independently verified, and are marked where they appear: CI behaviour on any branch in this arc (§0.3), and which of the two event schemas is live at runtime (§3). One claim is flagged for re-verification at implementation time rather than asserted: **H5** (§8).

---

## 12. Inversions

Four assumptions this design overturns. They are listed because an implementer who imports the default will build the wrong thing, and in three of the four cases the wrong thing passes its own tests.

1. **"There is no moment in the sync loop" → there is one, and it is per token.** `design-streaming-canonical-ir.md:18` states the sync loop offers no per-token moment. It does: the halt poll at `agent/core/agent.py:1367-1370` runs once per stream event. The *sink* was missing, not the moment. (T-3 corrects the source document.)
2. **"Mid-stream state needs a `StreamEvent`" → it needs a runtime/client kind.** The provider channel (`agent/core/stream_events.py:108-119`) is closed and content-only; injection is legal on the runtime and client channels only. `addendum_updated` and `runtime_notice` are therefore turn-loop dicts, never `StreamEvent`s. An implementer who reaches for a `StreamEvent` variant will find the type system fighting them, and should read that as the design working.
3. **"Current state is a notification" → current state is the addendum.** The house rule inverts the default: something that *happened* is a notification (prunable by design — `history_pruner.py:68`, `:164-165`); something that is *true now* is an addendum, which survives compaction. Expressing a standing fact as a notification is the H6 failure mode, and it fails silently by being pruned.
4. **"The addendum is display state, so it goes in `metadata`" → it is a first-class session field.** Ruled D-1. `metadata` is untyped, echoed to the client, reinterpreted at every read site, and has nowhere to express `pinned`, `budget_chars` or the 32-entry cap.

The direction is consistent: each inversion moves work *earlier* — into the schema, the write boundary, or composition — and out of paths that fail silently.

## 13. Ripple (est.)

Estimated blast radius by slice. "Keys" and "kinds" are the two axes that matter, because both are interface changes that a reviewer would not expect in a feature PR this size.

| Slice | Persisted-dict keys | Runtime event kinds | Files (est.) | Test churn |
|---|---|---|---|---|
| 1 | **+1** (`addendum`: `entries`, `budget_chars`) | none | 2 (`session/models.py`, `session/store.py`) | low — 3 new tests, existing suite untouched |
| 2 | none | none | 1 (`agent/core/llm_client.py`) | medium — per-adapter assertions |
| 3 | none | none | 0 (test only) | 1 new test, run twice |
| 4 | none | **+2** (`addendum_updated`, `runtime_notice`) | 4-5 (`agent/core/agent.py`, `bridge.py`, `SessionTab.jsx`, a reducer) | medium-high — idempotence, ordering, unknown-kind guard |
| 5 | none | none (reuses `runtime_notice`) | 1-2 (write boundary + its UI surfacing) | medium — refusal, boundary, oversized, pinned |
| 6 | none | none | 14 sites | deferred (D-3) |

Two notes. **Slice 1 carries the only persisted-shape change in the implementation** — additive and defaulted, so a session that predates the field loads as empty and there is no migration. **Slice 4 adds two runtime kinds, not one**; that is the A3 correction, and it is what makes slice 5 possible at all.

Confidence: file counts are derived from citations already in this brief, not from a fresh enumeration of call sites. Treat them as floors, not ceilings.
