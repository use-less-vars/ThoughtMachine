# ticket: ux/workspace-scrollbar-visibility

**Filed:** 2026-11-01
**Status:** **Open**
**Severity:** **Low** (cosmetic/discoverability; the panel does scroll — the affordance just cannot be seen)

## Symptom

The workspace panel (`ConfigPanel`) is taller than its column and scrolls, but its
scrollbar is invisible until the pointer is already on it. The global Webkit scrollbar
rules paint a 6px-wide thumb in `var(--border)` (`#45475a`) on a `transparent` track,
over a panel painted `#313244` — **1.378:1** contrast. The thumb therefore reads as a
slightly different shade of the panel, and nothing on screen says there is more content
below the fold. Hovering brings it up to 5.647:1, which is why the panel is only
unusable-looking until you happen to move the mouse over its right edge.

## Evidence

- `web_ui/frontend/src/styles.css:1280-1292` — the only scrollbar styling in the tree,
  unscoped (so it governs every scroller in the app):
  `::-webkit-scrollbar { width: 6px }` (`:1280-1282`),
  `::-webkit-scrollbar-track { background: transparent }` (`:1283-1285`),
  `::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px }`
  (`:1286-1289`),
  `::-webkit-scrollbar-thumb:hover { background: var(--text-secondary) }` (`:1290-1292`).
- Palette: `web_ui/frontend/src/styles.css:23` `--border: #45475a`;
  `:17` `--text-secondary: #a6adc8`; `:15` `--bg-surface: #313244`;
  the token block is `:root {` at `:12`, closing `}` at `:27`.
- `web_ui/frontend/src/components/ConfigPanel.jsx:703` — the panel root, inline styles
  (not a class): `background: '#313244'`, `color: '#cdd6f4'`,
  `width: panelWidth || 280`, `minWidth: 200`, `maxWidth: 500`, `flexShrink: 0`,
  `overflowY: 'auto'`, `height: '100%'`, `padding: '1rem'`. `overflowY: 'auto'` over a
  `height: '100%'` box is what makes the panel scroll at all; `#313244` is what the
  transparent track resolves to.
- `ConfigPanel.jsx:657` — the loading/error variant of the same root, identical
  `background: '#313244'` + `overflowY: 'auto'` + `height: '100%'`.
  `ConfigPanel.jsx:1351` — a second `overflowY: 'auto'` scroller inside the panel (the
  addendum box), same invisible thumb.
- Contrast, computed with the WCAG relative-luminance formula from the two hexes:
  thumb `#45475a` against panel `#313244` = **1.378:1**. The track is `transparent`, so
  the effective track colour *is* the panel — 1.378:1 again. WCAG 2.1 SC 1.4.11
  (non-text contrast) asks **3:1** of a UI component that carries information about
  available content. Hovered thumb `#a6adc8` = **5.647:1**.
- No `scrollbar-width`, no `scrollbar-color`, no `scrollbar-gutter` anywhere under
  `web_ui/frontend/src` (grep, no matches). So the 6px Webkit rule is the entire
  contract: Chromium/WebKit/Safari get a 6px near-invisible thumb; Gecko falls back to
  its own default scrollbar, which *is* visible. The defect is engine-specific, not
  universal — worth knowing before "fixing" it for everyone.

## What was not verified, and why

No browser was driven for this ticket, so nothing here is a screenshot.

`web_ui/frontend/node_modules` **is** present (vitest 3.x, jsdom 24, testing-library),
and `package.json` has no `@vitest/browser` dependency. jsdom has no layout engine and
no CSSOM access to `::-webkit-scrollbar` pseudo-elements, so the vitest suite can
neither compute nor paint this. Chromium binaries are cached at `/workspace/.ms-playwright`
(`chromium-1234`, `chromium_headless_shell-1234`) for the pytest e2e suite, which skips
itself unless `--run-e2e` is passed (`tests/e2e/conftest.py:60-77`), and whose four
modules cover permission persistence, tab-switch state and an install smoke test — none
of them look at a scrollbar.

So "the thumb is invisible on a real panel" is read off the declarations plus the
arithmetic above. It is not a measurement. The arithmetic is exact and the declarations
are pinned by file:line; the *appearance* claim is inference.

## Proposals (no fix chosen here)

1. **Wider thumb.** `::-webkit-scrollbar { width: 10px }` at `styles.css:1281`, with
   `::-webkit-scrollbar-thumb { border: 2px solid transparent; background-clip: content-box }`
   so the painted thumb keeps its current weight while the visible/hittable strip
   doubles. Cheap; does **not** fix contrast on its own (still 1.378:1).
2. **Higher-contrast thumb taken from the palette.** Replace `var(--border)` at
   `styles.css:1287` with a value that clears 3:1 against `#313244`. Within the ramp
   already in `:root`: `#7f849c` = **3.402:1** (passes), `#6c7086` = **2.574:1** (does
   not), `#9399b2` = **4.454:1**. Prefer a `--scrollbar-thumb` token in `:root`
   (`styles.css:12-27`) or `color-mix(in srgb, var(--text-secondary) 55%, var(--bg-surface))`
   over a fourth literal hex — the token is also what makes the acceptance test below
   checkable without a colour table in the test file.
3. **Reserve the gutter.** `scrollbar-gutter: stable` on the panel root
   (`ConfigPanel.jsx:703`) so the content column does not shift when the bar appears.
   Orthogonal to 1 and 2. Note this one cannot live in `styles.css`: the panel root's
   styles are inline, so it has to be a style-object key.

Not proposed: an app-wide scrollbar redesign; `scrollbar-color` for Gecko (Gecko already
draws a visible default, so it is not the defect); raising the panel's `maxWidth`/width.

## Acceptance criteria

Plainly first: of the two testable forms, only the static one is reachable in this
repo today, and neither replaces looking at it once in a browser.

- **(ii) Computed-style assertion — the form the ruling asks for, NOT feasible as
  written.** `getComputedStyle` on `::-webkit-scrollbar`-family pseudo-elements needs a
  real browser context: `@vitest/browser` is not a dependency, jsdom returns nothing for
  those pseudo-elements, and Chromium's CSSOM pseudo-element access is non-standard. This
  is rejected by constraint, not preference.
- **(i) Visual-regression test — NOT feasible as written.** There is no screenshot
  baseline anywhere and no Playwright screenshot harness in `tests/e2e/`; the suite is
  `--run-e2e`-gated and its modules assert behaviour, not pixels. A baseline added now
  would be the only one in the repo, so it needs a decision about where baselines live
  before it needs a test.
- **Static contract test — feasible now (vitest), and the only CI-cheap option.** New
  test that reads `web_ui/frontend/src/styles.css` and asserts (a) the declared
  `::-webkit-scrollbar` `width` is **≥ 10px**, and (b) the declared
  `::-webkit-scrollbar-thumb` `background` resolves — through `:root` when it is a
  token — to a colour whose contrast against `--bg-surface` `#313244` is **≥ 3:1** by the
  WCAG relative-luminance formula. Honest limitation: it pins the declarations, not the
  pixels; it would not have described the current bug, only its numbers. (b) is the
  reason proposal 2 prefers a token.
- **Browser check (manual, required before closing).** At a viewport where the panel
  overflows: the thumb is visible at rest without hovering, and the content column does
  not shift horizontally when the bar appears.

## Related

- `ui/workspace-panel-no-scrollbar` — the ticket this one supersedes — **never existed**:
  no file for it anywhere under `/workspace` (every worktree included) and no commit for
  its path on the branch where it was supposed to have been filed. Its premise was also
  wrong: the panel *does* scroll (`ConfigPanel.jsx:703`, `overflowY: 'auto'` over
  `height: '100%'`). What that phantom was reaching for is the visibility of the
  scrollbar, which is this ticket. Do not re-file the old name.
- `ticket-ui-landing-card-overflow.md` — the sibling `ui/` ticket, and the precedent for
  the "phantom ticket" paragraph above (it retires `ui-truncated-landing-page` the same
  way). Same panel-adjacent surface, different defect.
- `ticket-ui-session-tab-title-staleness.md` — the other `ui/` sibling.
- `ticket-hygiene-sandbox-vitest-cpu.md` — applies to any vitest run of the acceptance
  test above: with the worker container's default `TMPDIR=/var/tmp` vitest dies before
  collection and reports "no tests" at exit 0. Run with `TMPDIR=/tmp`.
