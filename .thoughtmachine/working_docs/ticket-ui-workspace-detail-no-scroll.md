# ticket: ui/workspace-detail-no-scroll

**Filed:** 2026-10-31
**Status:** **Fixed** (fix on branch `w2/ui-workspace-panel-no-scrollbar`)
**Severity:** **High** (content below the fold is unreachable — not cosmetic)

## Symptom

On the workspace route (`#/workspace/<id>`) the user cannot reach anything below the
first screenful. No scrollbar appears anywhere on the page; the page simply stops.
The mouse wheel does nothing, keyboard paging does nothing, and the tail of the page
(root path, security posture, host-execution state, session/container counts, the
`Apply` row on *Permissions & Resources*, the Dockerfile content, the container list,
the worker lists on *Workers*) is unreachable. The frame is drawn correctly — header,
tab strip and borders all render — which makes it look like a styling preference
rather than a layout bug.

## Evidence

Render path (workspace route is a sibling flex item in the app shell's content column):

- `web_ui/frontend/src/App.jsx:1353-1355`
  ```jsx
  <div className="app-main">
    <div className="app-center tab-content-area">
      <LayerNav route={route} workspaceLabel={layerNavWorkspaceLabel} sessionLabel={layerNavSessionLabel} />
  ```
- `web_ui/frontend/src/App.jsx:1417`
  ```jsx
  {route?.view === 'workspace' && <WorkspaceDetailPage workspaceId={route.id} />}
  ```
- `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:655` — the page root:
  ```jsx
  <div className="wdp-panel">
  ```
  Direct children: `.wdp-header` (`:656`), `<VaultHealthBanner />` (`:713`),
  `.wdp-tabs` (`:714`), `.wdp-body` (`:729`). The two modals are `position: fixed`
  (`WorkspaceDetailPage.css:287-295`) and are therefore outside flex flow.

Before the fix, `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.css:5-12`:

```css
.wdp-panel {
  background: #1e1e2e;
  border: 1px solid #313244;
  border-radius: 12px;
  color: #cdd6f4;
  font-size: 0.85rem;
  overflow: hidden;
}
```

and `WorkspaceDetailPage.css:187-189`:

```css
.wdp-body {
  padding: 1.1rem;
}
```

**This is a missing scroll container, not a missing scrollbar style.** `.wdp-panel`
declared no `height`, no `display: flex` and no `min-height: 0`, so as a flex item in
the `overflow: hidden` column of `.tab-content-area` it kept `flex: 0 1 auto`, was
shrunk to the viewport height, and then clipped its own overflow via its own
`overflow: hidden`. `.wdp-body` was plain padding — no `flex`, no `min-height: 0`, no
`overflow-y`. Between `#root` and the leaf there was no scroll container at all, so
the browser had nothing to attach a scrollbar to.

The shell is **correct** and is not implicated:
`html/body { height: 100% }` (`src/styles.css:29-32`) → `#root { height: 100% }`
(`:39-41`) → `.app-container { display:flex; flex-direction:column; height:100% }`
(`:46-50`) → `.app-main { display:flex; flex:1; overflow:hidden }` (`:52-56`) →
`.app-center { flex:1; display:flex; flex-direction:column; overflow:hidden }`
(`:689-695`) → `.tab-content-area { flex:1; display:flex; flex-direction:column;
overflow:hidden; position:relative }` (`:1461-1467`). The session layer re-establishes
flex on its own (`SessionTab.jsx:1370` `<div className="app-main">`, `:1398`
`<div className="app-center">`); the workspace route had no such second pass.

The intended values were already spelled out in CSS for a markup tree that no longer
exists — `web_ui/frontend/src/components/workspace/WorkspacePanel.css:5-12`:

```css
.wp-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  ...
}
```

and `WorkspacePanel.css:112-116`:

```css
.wp-content {
  flex: 1;
  overflow-y: auto;
  padding: 1rem 1.25rem;
}
```

No JSX anywhere uses `wp-panel` / `wp-body` / `wp-main` / `wp-content` / `wp-tabs`
(grep over `web_ui/frontend/src`: zero hits). `WorkspacePanel.css` is imported only by
`components/workspace/modals/NewSessionModal.jsx:10`, which uses just the
`wp-modal*` / `wp-field*` / `wp-btn*` subset. The `wp-*` tree was rewritten as `wdp-*`
and `.wp-content`'s `flex: 1; overflow-y: auto` and `.wp-panel`'s
`height: 100%; display: flex` were dropped in the translation.

Single-use check (required before changing the shared class names):
`.wdp-panel` has exactly one JSX site (`WorkspaceDetailPage.jsx:655`, the page root)
and `.wdp-body` exactly one (`:729`); the only other references in the repo are
*absence* assertions in `components/__tests__/Navigation.test.jsx:179,202`
(`expect(document.querySelector('.wdp-panel')).toBeNull()` on non-workspace routes),
which a CSS change cannot affect. So the chain was put on the existing class names
rather than inventing a root-only class.

## Options

**Chosen — flex chain on `.wdp-panel` + `.wdp-body`** (`WorkspaceDetailPage.css:5-24`
and `:202-208`, post-fix):

```css
.wdp-panel {
  ...existing visual properties untouched...
  overflow: hidden;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.wdp-body {
  padding: 1.1rem;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
```

`min-height: 0` is load-bearing: the automatic minimum size of a column flex item is
its content height, so without it the panel never shrinks and the overflow is clipped
by the ancestor instead of scrolling in `.wdp-body`. This mirrors the intent already
recorded in `WorkspacePanel.css:5-12` / `:112-116`.

One additional, minimal adjustment was required and applied: `flex-shrink: 0` on
`.wdp-header` (`:25-37`) and `.wdp-tabs` (`:171-181`). Because `.wdp-body` takes
`flex-basis: 0`, the header and tab-strip auto bases are the only shrinkable ones —
without the pin, a short viewport compresses the chrome instead of letting the body
scroll, which would violate "header and tabs stay fixed". `.wdp-tabs` keeps its
existing `flex-wrap: wrap` (it may *grow* to a second line when narrow; it must never
shrink).

Rejected alternatives:

- **`height: 100%` on `.wdp-panel` instead of the flex chain.** Rejected: a percentage
  height resolves against `.tab-content-area`, but the panel is *not* the only child of
  that column — `LayerNav`, the `TabBar` strip and the session deck are also in it, so
  `height: 100%` would over-tall the panel by exactly the chrome height and push the
  bottom past the clipped ancestor. The flex chain derives the height from the actual
  remaining space instead of guessing.
- **`overflow-y: auto` on `.wdp-panel` itself.** Rejected: the header and tab strip are
  inside the panel, so they would scroll out of view with the content. The scroll
  container must be `.wdp-body`, below the chrome.
- **Prune or re-point `WorkspacePanel.css`.** Rejected here: correct in spirit but it
  is hygiene work on 883 lines with a live consumer (`NewSessionModal`), and it would
  widen the blast radius of a High-severity layout fix. Tracked separately (see
  Related).
- **Change `.tab-content-area` or the app shell.** Rejected: the shell is correct
  (evidence above). Touching it to compensate for a broken leaf would risk the session
  route, which shares the same ancestors.

## Acceptance criteria

Nothing in this repo's test harness can assert this, and no automated test is claimed:

- Vitest runs with its default `css: false` (`vite.config.js:27-29` sets only
  `{ fileParallelism: false }`; there is no `setupFiles`, no `css: true`, no setup
  file), so stylesheets are never loaded or evaluated in tests.
- jsdom performs no layout: `clientHeight`, `scrollHeight` and `offsetHeight` are all
  `0`, and `getComputedStyle` cannot see `WorkspaceDetailPage.css`.
- There is no Playwright or Puppeteer in `package.json` and no e2e directory.

Therefore the criteria are browser-checked at a **900 × 700** window on
`#/workspace/<id>`:

1. A vertical scrollbar appears in the body area, and scrolling reaches the last
   element of the longest tab (*Permissions & Resources* and the Dockerfile content on
   *Containers* are the tallest).
2. The header (title, workspace id, counts, host-execution toggle, `Apply`) and the tab
   strip do **not** move while the body scrolls.
3. No second/double scrollbar appears anywhere on the page (the outer column must stay
   clipped, not scroll).
4. The session route (`#/workspace/<id>/session/<sid>`) layout is unchanged, before and
   after.

Plus: the existing suites still pass — `WorkspaceDetailPage.test.jsx` (25 tests),
`WorkspaceDetailPage.records.test.jsx` (2), `WorkspaceDetailPagePermissions.test.jsx`,
`Navigation.test.jsx` — verified green after the change, exit code 0.

## Related

**This ticket replaces the schedule's `ui-workspace-panel-no-scrollbar`, which does not
exist.** That slug appears nowhere except `.thoughtmachine/knowledge/personal/lessons_learned.md:184`,
where it is listed as one of three phantom "papercuts"
(`ui-session-naming-desync`, `ui-workspace-panel-no-scrollbar`,
`ui-truncated-landing-page`) that were "being planned around in a parallel-dispatch
schedule and none of their files existed anywhere in the repo". No file in the 203-file
`ticket-*.md` corpus starts with `ticket-ui-`, and a grep of all 203 tickets for
`scroll|scrollbar|overflow|clip|cut off|unreachable` returns 19 hits that are all
metaphorical or non-UI ("unreachable branch/code", "text the operator watched scroll
past", "gets cut off with no diagnostic" = a worker timeout). The defect described above
had never been filed under any name.

Sibling findings, deliberately **not** fixed here (separate scope):

- **Orphaned CSS.** ~170 lines of `wp-panel` / `wp-header` / `wp-body` / `wp-main` /
  `wp-tabs` / `wp-content` / `wp-risk` in `WorkspacePanel.css:5-116` have zero JSX
  consumers. It should be pruned or re-pointed, and it is the reason a CSS-only reading
  of the repo concludes this bug is already fixed.
- **Worker panels may clip.** `components/WorkerPanelArea.jsx:160-161`
  (`.worker-panel-bodies { flex: 1; min-height: 0; overflow: hidden; position: relative }`,
  `.worker-panel-body { height: 100% }`) combined with `src/styles.css:907-912`
  (`.worker-output-panel { height: 100%; overflow: hidden; flex-shrink: 0 }`) means each
  panel body must supply its own scroller, and `components/WorkerOutputPanel.jsx`
  declares no `overflow` or `height` at all. **Unverified, medium confidence** — inner
  children (tool output, log lines) may scroll on their own; needs its own recon before
  being filed.
