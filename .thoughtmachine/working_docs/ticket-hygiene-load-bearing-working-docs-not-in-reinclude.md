# Ticket: hygiene/load-bearing-working-docs-not-in-reinclude

**Filed:** 2026-10-25 (Gen 50)
**Severity:** MEDIUM — a clone of this repo does not carry the working docs the KB points at
**Status:** open; filed, not dispatched

## Finding

`.thoughtmachine/working_docs/` is gitignored. PR 190 added a re-include set for the seven onboarding-contract files, but the working docs the KB treats as load-bearing are still outside it: design briefs, filed tickets, adjudication files, and drafting copies of docs meant for `docs/`.

A fresh clone carries the seven, not these. The KB points at files a clone cannot see. Fifth-boundary class applied to the working docs — the narrative says "read the design doc," the disk says "the design doc is not here."

## Instances observed (2026-10-25, Gen 50)

1. `.thoughtmachine/working_docs/redteam-g3-g5-adjudication.md` — G3/G4/G5 tables, referenced by the wave-2 PR body
2. `.thoughtmachine/working_docs/design-input-auth-layer-prerequisite.md` — drafting copy
3. `.thoughtmachine/knowledge/personal/redteam_clientclaim.md` — corpus update (F12, G6 residuals, %2F notes)
4. `docs/design-input-auth-layer-prerequisite.md` — **caught by a `design-*` ignore pattern, not the working-docs rule.** The file was meant for `docs/` (tracked), and the pattern catches it anyway. This is the first instance where the gitignore's effect was not the working-docs rule the ticket names.

## The smallest first step

Do **not** re-include blindly. Two questions must be answered before either fix:

1. **Is the `design-*` ignore pattern deliberate?** Check blame and history on `.gitignore` and against the `chore/track-registry-design-doc` and `chore/registry-design-stale-header` branches, which suggest design-doc handling already has deliberate machinery. A rename (dodging the pattern) is only correct if the pattern is accidental; a re-include is only correct if the reference should resolve. Do not do both without knowing which.
2. **Which working docs are load-bearing?** Enumerate those cited by `working_agreements.md`, `MANDATORY_STOP.md`, `docs/host-port.md`, and the current status/handoff chain, plus any ticket or adjudication file referenced by a merged PR. That set is the re-include candidate.

Then: file the answer as a decision in this ticket before touching `.gitignore`. The fix — re-include or rename — follows from the answer, not the other way around.

## What this is not

Not a re-open of PR 190's scope decision. Not a proposal to un-ignore all of `working_docs/`. Not an assertion that every gitignored file is a defect — only the ones the KB cites and a clone lacks.
