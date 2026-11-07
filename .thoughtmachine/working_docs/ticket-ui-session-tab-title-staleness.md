# ticket: ui/session-tab-title-staleness

**Filed:** 2026-10-31
**Status:** PARTIALLY FIXED — R1 + R3 committed as `2bd1de09` on `w2/session-rename-staleness` (worktree `w2-rename`, not yet merged); R2 and R4 open, R3 has no browser verification.
**Severity:** Medium — wrong text in persistent chrome (tab strip / breadcrumb) with no data loss; the workaround is a reload.

**Provenance — this ticket was born from a phantom.** The parallel-dispatch schedule carried this item as `ui-session-naming-desync`. No such ticket exists: the corpus is 203 files matching `.thoughtmachine/working_docs/ticket-*.md` (203 counted in both `/workspace` and the worktree), and the slug `ui-session-naming-desync` occurs exactly once repo-wide — in the lesson that warned about it, `.thoughtmachine/knowledge/personal/lessons_learned.md:184`:

> "**Grep the corpus before claiming a ticket exists**: three "papercuts" (`ui-session-naming-desync`, `ui-workspace-panel-no-scrollbar`, `ui-truncated-landing-page`) were being planned around in a parallel-dispatch schedule and none of their files existed anywhere in the repo."

Those three are the series' phantoms; this is the third. This file is not that phantom rewritten: the recon run to disprove it read the session-tab code path and found the real defect documented below.

## Symptom
A session's tab keeps a name the server no longer uses. Rename a session out-of-band (another client, a CLI, a rename while this client was disconnected), then reload this client: the tab strip and the breadcrumb still show the old name, indefinitely, until the user renames again by hand. A second, cosmetic symptom: a session with no name yet renders a tab with an empty label — an invisible, unclickable-looking tab.

## Evidence
Mechanism, pre-fix (`2bd1de09^`) → post-fix, all in `web_ui/frontend/src`:

- `App.jsx:477-489` — the fix, **in the `open_sessions` handler** (NOT `sessions_list`; the recon that started this work mislabelled it, and a future reader will otherwise hunt in `sessions_list` at `App.jsx:372-390`, which does no title work at all). Pre-fix: `if (existing && !existing.title) st.setTabTitle(...)` — an existing title was never touched, with a comment that said so ("never clobber a persisted/user-set one"). Post-fix: `if (existing && existing.title !== s.name) st.setTabTitle(ws, s.session_id, s.name)`, guarded by `else if (s.name)` (:477) so an unnamed session never wipes a title.
- `sessionTabsStore.js:66-67` — why the stale title is durable: `openTab` no-ops for an existing tab (`:66`) and therefore never re-reads the server name, and it persists `title: title || ''` (`:67`), so an empty title is a real stored state.
- `TabBar.jsx:49` — pre-fix `<span className="tab-label">{tab.name}</span>`, no fallback: the empty title renders as a blank tab. Post-fix `{tab.name || 'Untitled'}` (R3). The `title={tab.name}` tooltip attribute was left alone.
- `App.jsx:229-231` — the breadcrumb prefers the tab title over the store (`tabs.find(...)?.title || nameFromStore(route.id) || null`), so a stale tab title also poisons the breadcrumb.
- `SessionTab.jsx:139-141` — the session header reads **only** the store (`s.sessions.find(...)?.name`), rendered at `SessionTab.jsx:1288` as `{sessionName || 'Untitled'}`. So header and tab can disagree in principle; see the race note below.

Empty-name state is real, which is why R3 exists: tabs are opened before the name list lands — `App.jsx:607, 844, 869, 884, 1073` open with `title: nameFromStore(id)`, i.e. `''` when the session is not yet in `useStore.sessions` (pre-fix numbering; +3 after the R1 edit → 610/847/872/887/1076) — and `sessionTabsStore.js:67` persists that `''`.

## Options
1. **R1 — server name is authoritative in `open_sessions` (SHIPPED, `2bd1de09`).** Minimal, no new store, no new event: `setTabTitle` already existed and is idempotent.
2. **R2 — hub-side rename broadcast (NOT DONE, deliberately out of scope).** The hub handler at `App.jsx:411-416` handles `session_renamed` and calls `applySessionRename` — but no backend ever broadcasts it on the hub socket. Both rename broadcasts in `server.py:2986-3016` go to the per-session tab-WS bridge (`:2991` and `:3009`), and `grep session_renamed` over `web_ui/**.py` returns only those sites. So `App.jsx:411-416` is production-dead — it fires only in tests, against a fake hub. Consequence: another client, or a rename that happened while this client was disconnected, keeps the old label until this client's next `sessions_list`/hub connect. Fixing it is a backend event-routing change (emit `session_renamed` to hub sockets too), which is why it is a separate ticket rather than a rider on this one.
3. **R3 — `Untitled` fallback (SHIPPED, `2bd1de09`).** One line; note the header at `SessionTab.jsx:1288` already had this fallback, so the strip was the inconsistency.
4. **R4 — uncontrolled rename input (NOT FIXED, cosmetic non-defect).** `SessionTab.jsx:1261` `defaultValue={sessionName}` makes the rename input uncontrolled: an echo that arrives while the input is open leaves the typed/pre-echo text in place until the input closes. Not obviously wrong, needs a human in a browser to judge; no code change made.

**Honest limit on R1.** `get_open_sessions` is requested **once per hub WS connection** — `App.jsx:363`, the only sender in `src/` — and the reply is computed fresh inside its handler (`server.py:3039-3061`: `get_open_sessions()` → `load_sessions_metadata_batch()` → `await ws.send_json(...)`, no await between read and send). Therefore R1 repairs a stale title **at hub connect / reload**, not in real time. Real-time repair is exactly what R2 would buy; R2 is not done, so do not read this ticket as claiming live convergence.

**Race check on R1 (asked in review; verdict: not-real).** `open_sessions` writes only the tab strip (`App.jsx:476`, `:485`), never `useStore.sessions` (it only reads it at `:452`), so a stale overwrite could in principle leave header=new (store, `SessionTab.jsx:139-141`) / tab=old. It cannot fire: the hub emits `open_sessions` only in reply to `get_open_sessions`, the reply is computed fresh inside the command handler, and the server is a single-threaded asyncio loop on which a later `rename_session` (`server.py:2969`) is handled — and its `session_renamed` broadcast delivered — strictly after that reply is queued on the hub socket (FIFO). Cross-socket paths (`rename_session` travels on the tab WS, `SessionTab.jsx:1266/1278`) cannot reorder it. This stops being true if `open_sessions` ever becomes an unsolicited broadcast or is re-sent on rename; then it becomes real-and-healing (a later `session_renamed` via `applySessionRename`, `App.jsx:239-250`, writes both store and strip; or the next connect's `open_sessions`).

## Acceptance criteria
- [ ] Unit (done, `SessionTabsTitleReconcile.test.jsx`): with `sess-1` persisted as `Stale Old Name` and `open_sessions` reporting `Authoritative One`, the persisted strip, the in-memory store and `.tab-label` all read `Authoritative One`, and a foreign workspace's persisted strip is untouched. Pins R1; fails if `App.jsx:485` is reverted to `!existing.title`.
- [ ] Unit (done, `TabBar.test.jsx`): tabs with `name: ''`, a missing `name`, and a real name render `['Untitled', 'Untitled', 'Real Name']`. Pins R3.
- [ ] Unit (done): `open_sessions` carrying an **empty** name must NOT change an existing title — the `else if (s.name)` guard. Exercised as a fixture invariant in `SessionRename.test.jsx` and `SessionTabsIntegration.test.jsx` (see below), not asserted directly; a direct assertion would be a cheap addition.
- [ ] Browser (NOT verified, needs a human): two clients on the same session, rename in client A, reload client B → B's tab shows the new name (R1 through a real hub, not a mock).
- [ ] Browser (NOT verified): rename in client A, do nothing in client B → B's tab keeps the OLD name until it reconnects or its list refreshes. This is R2's gap and should be recorded as expected-current behaviour, not a regression.
- [ ] Browser (NOT verified): a brand-new session with no name shows `Untitled` in the strip and does not look like an empty/broken tab; the `title` tooltip on such a tab is still empty (known, unfixed, part of R4's cosmetic family).
- [ ] Browser (NOT verified): rename input open while another client renames → text does not jump (R4). Expected to be acceptable, unproven.

**What the tests do and do not prove.** New: `SessionTabsTitleReconcile.test.jsx` (R1, full-App harness: mock hub feeds `sessions_list` then `open_sessions`; asserts persisted + in-memory + DOM, plus workspace isolation) and `TabBar.test.jsx` (R3, renders the component directly). Two existing fixtures were **changed** and it is deliberate: `SessionRename.test.jsx` (reload step) and `SessionTabsIntegration.test.jsx` (two reload payloads) used to replay the *pre-rename* names (`S1`/`S2`) after a simulated reload and to assert that `open_sessions` must not clobber the persisted title — a state the backend cannot produce, since `rename_session` persists the name (`server.py:3003-3004`, and the bridge path at `:2985`) — so they encoded precisely the behaviour this change inverts. They now send `name: ''`, which makes the reconciliation branch inert and forces the labels to come from `localStorage`, keeping every original assertion unchanged; a mutation check (persisted `Tab 1` → `MUTATED TITLE`, payload still empty-name) failed the test (`1 failed | 4 passed`), proving the fixtures still discriminate. **No test here can observe rendering**: Vitest runs with `css: false`, jsdom has no layout engine, and the project has no Playwright — every visual/label-width claim in this ticket is unverified by tests.

## Related
- `2bd1de09` — R1 + R3 + two new test files (`w2/session-rename-staleness`).
- `ui-workspace-panel-no-scrollbar`, `ui-truncated-landing-page` — the other two phantoms from `lessons_learned.md:184`.
- `web_ui/backend/server.py:2986-3016` — rename broadcasts, R2's site.
- `App.jsx:411-416` — hub `session_renamed` handler, production-dead today (R2).
