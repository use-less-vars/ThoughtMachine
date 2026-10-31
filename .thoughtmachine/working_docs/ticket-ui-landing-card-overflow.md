# ticket: ui/landing-card-overflow

**Filed:** 2026-10-31
**Status:** **Fixed** (fix on branch `w2/ui-truncated-landing-page`)
**Severity:** **Low** (cosmetic; no data loss)

## Symptom

A workspace whose label is a long folder name spills its title and its
"Host: allowed/forbidden" badge past the card's right border. The card clips
nothing (`.ws-card` sets no `overflow`) and the grid column floors at 240px
(`.ws-grid { grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)) }`),
so the over-long title widens the card's flex head row and the text runs out
past the rounded border into the grid gap / the neighbouring card. Reachable
without any unusual data: `workspace_registry` derives the label from the
folder basename, so any deep folder name of 25+ characters does it at 240px.

## Evidence

- `web_ui/frontend/src/components/WorkspaceSelector.css:146-151` (pre-fix)
  `.ws-card-title` — `margin`/`font-size`/`color` only, plus
  `padding-right: 3.2rem; /* keep clear of the absolute risk badge */`.
  No `overflow`, no `text-overflow`, no `white-space` ⇒ nothing could clamp.
- `WorkspaceSelector.css:414` `.gms-card-head{display:flex;justify-content:
  space-between;align-items:flex-start;gap:0.5rem}` — the title (`<h4>`) and
  the badge (`<span>`) are flex items of one row, and
  `WorkspaceSelector.jsx:242-247` is the only place `.gms-card-head` is used.
- Two-layer collision: `WorkspaceSelector.css:210-214`
  `.ws-card .ws-risk { position: absolute; top: 0.7rem; right: 0.7rem }`
  versus the minified GMS block's `.gms-card-head .ws-risk{position:static;
  white-space:nowrap;text-transform:none}` (same line 414). Both selectors are
  specificity 0-2-0, so the later one wins ⇒ `position:absolute` never applied,
  the badge always sat in normal flow, and the 3.2rem title padding was
  reserving space for a badge that was never there — pure width loss on the
  one element that needed width.
- `thoughtmachine/workspace_registry.py:376-379` — label derivation:
  `if not label or label == "default": label = os.path.basename(
  os.path.abspath(root_path).rstrip("/\\"))`, then `if not label: label = ws_id`.
  No length bound anywhere, so the label is exactly as long as the folder name.
- Flexbox: a flex item's automatic minimum size is its `min-content` width, so
  an unbroken long word refuses to shrink and no `text-overflow` can engage
  until the item gets `min-width: 0`.
- Corrected premise (the reason this stayed unfiled): `text-overflow: ellipsis`
  is IGNORED on a `-webkit-box` + `-webkit-line-clamp` element — that is
  `.ws-card-desc`, and Blink/Gecko/WebKit all paint the clamp's own trailing
  ellipsis there. The description was never cut mid-glyph without an ellipsis.
  Only the *title* (a plain block, unclamped) needed the
  `overflow`/`text-overflow`/`nowrap` triple.

## Options

Chosen:

1. `.ws-card-title` (`WorkspaceSelector.css:146-159`) now carries
   `min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap`
   and `padding-right: 3.2rem` + its comment are deleted. `min-width: 0` is the
   load-bearing declaration (without it the ellipsis never engages);
   `overflow: hidden` is required for `text-overflow` to paint; `white-space:
   nowrap` is required or the label wraps instead of ellipsizing.
2. `.gms-card-head .ws-risk` (`:422`) gains `flex-shrink: 0` so the badge is
   pinned as a non-shrinking item. Honest note: this is redundant today —
   `white-space: nowrap` already floors the badge's `min-content` width at its
   full text width, and `min-width: auto` honours that — so it is an intent
   marker, not a behavioural change.
3. `.ws-card-desc` (`:161-172`) gains the standard `line-clamp: 2` alongside
   `-webkit-line-clamp: 2` (the WebKit one stays: it is what actually clamps on
   every shipping engine today; the standard one is the forward-compatible
   spelling).
4. The collision is neutralised rather than stacked: the dead
   `.ws-card .ws-risk` rule is gone, replaced by a comment at
   `:219-225` recording why it was dead, and the surviving
   `.gms-card-head .ws-risk` is marked in-line as the single source of truth
   for the badge's position. Zero pixel change (the deleted rule never applied).
5. `title={ws.label || ws.id}` on the card's `<h4>` (`WorkspaceSelector.jsx:243`)
   so the clipped label stays reachable via the native tooltip.

Rejected:

- `overflow: hidden` on `.ws-card`: clips the hover `translateY(-2px)` ring and
  the `box-shadow` highlight, and clips `.ws-card-details`, which is revealed
  by hover. Treats the symptom, not the source.
- Truncating the label server-side in `workspace_registry`: the label is
  identity data reused by the "+ New Session" chooser and the session modal; a
  UI-only clamp is reversible and does not change an API payload.
- Raising the grid floor (e.g. `minmax(320px, 1fr)`): does not help at narrow
  viewports, and some label will always outgrow some column.
- `overflow-wrap: anywhere` / `word-break: break-all` on the title: wraps the
  label onto 2-3 lines, which pushes every card's stats row down and breaks the
  240px card rhythm.
- Deleting the dead `.ws-risk.low/.medium/.high/.critical` blocks (`:194-208`):
  the JSX only ever emits `allow`/`forbid`, so they are dead too, but that is a
  different ticket.

## Acceptance criteria

jsdom-proxy-checkable only — say so plainly: jsdom has no layout engine, so no
test here can assert "the text is clipped at 240px". What the suite pins is the
DOM contract the CSS hangs off:

- New test `WorkspaceSelector.test.jsx` →
  `clamps a long workspace label inside the card and keeps the full text in
  title`: fixture label = one unbroken 64-char token (the worst case, since an
  unbroken token is exactly the element's `min-content` width); asserts the
  rendered `h4` has class `ws-card-title`, has `title` === the full label, and
  that the sibling `.ws-card .ws-risk.allow` badge still renders in the same
  card.
- Browser-only (inspected by hand, deliberately not asserted in CI): at a 240px
  column the label ends in an ellipsis inside the card's border, the badge
  stays inside the border, and the full label appears in the native tooltip.
- `npx vitest run src/components/__tests__/WorkspaceSelector.test.jsx` →
  18 passed (17 pre-existing + this one).

## Related

- `ui-truncated-landing-page` — the ticket this work was originally planned
  from — never existed: no file for it anywhere in the repo, and three
  "papercut" tickets were being scheduled around it blind
  (`.thoughtmachine/knowledge/personal/lessons_learned.md:184`). This ticket
  replaces it; do not re-file the old name.
- `.thoughtmachine/working_docs/ticket-hygiene-sandbox-vitest-cpu.md` — same
  class of trap. Sandbox note from this run: with the worker container's
  default `TMPDIR=/var/tmp`, vitest 3.2.7 dies before collection with
  `ENOENT: mkdir '/var/tmp/<rand>/web'` and reports `Tests no tests` at exit
  code 0 (the error is only a warning banner). Running with `TMPDIR=/tmp`
  fixes it; `--maxWorkers=2` (that ticket's remedy) is about OOM, not this.
- Sibling fix, same landing view: `.gms-card-head` is used by exactly one
  component (`WorkspaceSelector.jsx:242`), so no other panel
  (GlobalSessions / GlobalContainers / GlobalResources / GlobalCredentials /
  PromptLibrary use `.gms-session-row` / `.gms-container-row` /
  `.gms-resource-head` / `.gms-cred-row`) is affected by the badge or title
  change.
