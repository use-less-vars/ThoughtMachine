# ticket: ui/workspace-panel-no-scrollbar

**Filed:** 2026-11-01
**Status:** SUPERSEDED — phantom ticket. No defect, no patch, no commit; the file never existed. This document is a disposition record only.
**Severity:** None — no defect present; documentation-only disposition record.

## Symptom

Claimed: the Workspace Management panel overflowed its container without producing a scrollbar, leaving the content below the fold unreachable. Never reproduced, never observed in a running build.

## Evidence

- The id exists only in prose: `.thoughtmachine/knowledge/personal/lessons_learned.md:184` lists `ui-workspace-panel-no-scrollbar` among three phantom ids and states "none of their files existed anywhere in the repo".
- A directory listing of `.thoughtmachine/working_docs/` returned exactly three `ticket-ui-*.md` files — `ticket-ui-host-bash-danger-color.md`, `ticket-ui-session-tab-title-staleness.md`, `ticket-ui-landing-card-overflow.md`. None matched this id.
- **Fixed-position siblings displacing or clipping the scroller** — REJECTED. The only `position:fixed` elements in the panel subtree are full-viewport modals (`web_ui/frontend/src/components/WorkerManagementPanel.jsx:56-64`, `modalOverlay: { position:'fixed', inset:0, … }`); being out of flow, they take no layout space and can neither displace nor clip the scroller.
- The scroll container that does exist is the config pane: `web_ui/frontend/src/components/ConfigPanel.jsx:703` carries the inline style `{ padding:'1rem', …, overflowY:'auto', height:'100%' }`.
- The height chain the claim presupposed is intact: `web_ui/frontend/src/styles.css:39-41` `#root{height:100%}`; `:46-50` `.app-container{display:flex;flex-direction:column;height:100%}`; `:52-56` `.app-main{display:flex;flex:1;overflow:hidden}`; `:1469-1474` `.tab-wrapper{flex:1;…min-height:0}`; `:1479-1484` `.session-tab-pane{…min-height:0}`; `:1490-1495` `.session-tab-content{flex:1;…overflow:hidden}`.
- Box model is border-box globally: `web_ui/frontend/src/styles.css:4-10` `*{box-sizing:border-box;…}` — the usual `height:100%` + `padding` overflow source is absent.
- Scrollbar styling is present: `web_ui/frontend/src/styles.css:1280-1292` `::-webkit-scrollbar{width:6px}`, `::-webkit-scrollbar-track{background:transparent}`, `::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}` (`--border:#45475a` at `:23`).
- Mount point: `web_ui/frontend/src/App.jsx:1393` renders the sole `<SessionTab`; `web_ui/frontend/src/components/SessionTab.jsx:1370` nests its own `<div className="app-main">`, so panes stay inside the flex/min-height chain rather than escaping it.

Failure modes ruled out, one mode per bullet:

- **Missing `min-height:0` on a flex child** — REJECTED. The property is present where it is load-bearing: `styles.css:1469-1474` (`.tab-wrapper`) and `styles.css:1479-1484` (`.session-tab-pane`). `styles.css:52-56` (`.app-main`) and `styles.css:1490-1495` (`.session-tab-content`) do omit it, but both set `overflow:hidden`, which zeroes the automatic minimum size, so no scroll is blocked there.
- **`overflow:hidden` on the scroller itself** — REJECTED. The scroller (`ConfigPanel.jsx:703`) sets only `overflowY:'auto'`. `overflow:hidden` appears on ancestors only. (Note: this does mean any height miscalculation would clip silently rather than scroll — the one plausible route to the reported symptom, and it is indistinguishable from the subtle-scrollbar explanation without a browser.)
- **`height:100%` under a `height:auto` parent** — REJECTED. The chain is definite end to end: `styles.css:39-41` → `:46-50` → `:52-56` → `:1469-1474` → `:1479-1484` → `:1490-1495`, and `box-sizing:border-box` (`styles.css:4-10`) means the `1rem` padding cannot inflate the box.
- **A sticky/fixed sibling stealing vertical space** — REJECTED. `.session-header` is `flex-shrink:0` (`styles.css:1544-1553`); `.resize-handle` is 5px wide, not tall (`styles.css:1297-1312`); the only `position:fixed` elements are full-viewport modals, which take no layout space.
- **Content portalled or absolutely positioned outside the scroller** — REJECTED. Nothing in the panel subtree is portalled; the only `position:fixed` elements are the full-viewport modals.
- **Stale geometry from the keep-mounted deck's `display:none`** — REJECTED. `.session-tab-pane-hidden{display:none}` (`styles.css:1486-1488`) removes the box, so re-showing forces a full relayout and no stale height can survive.
- **"Fails only inside the keep-mounted deck"** — NOT TESTABLE from source: `<SessionTab` occurs exactly once, at `App.jsx:1393`, inside the deck. There is no second rendering path to compare against.

## Disposition

Superseded. The ticket described no reproducible behaviour and no file ever backed the id. **No CSS, JSX, or style file was changed** — there was nothing to change. Closure is documentary — no code patch, no test, no UI change; this file is the only artefact. The id is written down here so it resolves to a document instead of 404-ing in `.thoughtmachine/working_docs/`.

## Residual finding — scroll affordance (deferred, NOT fixed)

Distinct from the phantom claim, and out of scope for this record: the scrollbar is 6px wide (`web_ui/frontend/src/styles.css:1281`) over a transparent track (`:1286`) with a `--border`-coloured thumb (`web_ui/frontend/src/styles.css:23`), so it is subtle at rest on the dark theme. Real but cosmetic. **No scrollbar CSS was changed** and this ticket does not track that work.

## Related

- `.thoughtmachine/knowledge/personal/lessons_learned.md:184` — origin of the id, recorded alongside `ui-session-naming-desync` and `ui-truncated-landing-page`; neither of those has a ticket file either.
- `ticket-ui-session-tab-title-staleness.md:50` — cross-reference to this id.
- `ticket-ui-landing-card-overflow.md` — a real ticket in this directory, kept as the front-matter contrast case.
- `ticket-ui-host-bash-danger-color.md` — a real ticket in this directory.
