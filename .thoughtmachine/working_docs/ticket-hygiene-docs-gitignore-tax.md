# ticket: `hygiene/docs-gitignore-tax`

**Filed:** 2026-10-29 (Gen 54 delivery arc)
**Status:** filed, not started
**Severity:** low per instance; it recurs on every new doc

## Symptom

Adding a document under `docs/` does not make it trackable. `.gitignore` carries `docs/*` as a catch-all, so a new file is ignored until someone appends a dedicated `!docs/<name>` negation line. `git add` fails with:

    The following paths are ignored by one of your .gitignore files:
    docs/<new>.md
    hint: Use -f if you really want to add them.

## Evidence

Hit on `docs/global-kb-workspace-runtime-guidance.md` (commit `9c8a771e`): the doc could not be staged until `.gitignore` gained

    !docs/global-kb-workspace-runtime-guidance.md

`.gitignore` now carries **43** `^!docs/` negation lines (42 before that commit), each added by hand for one document, several with multi-line explanatory comments. The file is 304 lines / ~9.7 KB, and a large share of it is this one pattern.

## Why it exists (do not "fix" it by deleting the line)

`docs/*` protects against `git clean -xdf` destroying unpublishable working docs, and against accidentally publishing internal notes. The catch-all is doing real work: KB_POLICY separates public docs from internal KB from diary, and the default must stay "not tracked".

## What actually hurts

- Every doc request becomes a two-file change, and the second file is configuration nobody asked about. That is how "write a doc" turns into a gitignore archaeology session.
- A negation only works *after* the ignore it overrides, so new lines land at the end of a 300-line file whose ordering is load-bearing.
- A forgotten negation is silent: the file exists on disk, `git status` says nothing, and the commit looks complete. Nothing is tracked and nobody notices until a clone is missing the file.
- The inverse trap has already bitten once: the overbroad `*secret*` ignore silently swallowed `agent/config/secret.py`, a real source module, until it was re-included.

## Options (needs a ruling; do not implement blind)

1. **Invert to an allow-list.** Ignore `docs/` wholesale and enumerate the tracked set. Same tax, different shape. Probably worse.
2. **Switch to `docs/**` with explicit ignores** for the internal and diary tiers, so a new public doc is tracked by default and only internal ones need naming. Reverses the default; needs a sweep to confirm nothing currently untracked-and-internal would start shipping.
3. **Add a pre-commit or CI check** that fails when an untracked file exists under `docs/`. Does not remove the tax; removes the silence around it.
4. **Leave it, document it.** Accept the tax and put the recipe in the docs workflow guide so every agent knows the extra step up front.

## Acceptance criteria

- Adding a doc under `docs/` needs no `.gitignore` edit (option 2), **or** an omission is caught mechanically before commit (option 3).
- No currently-ignored internal doc becomes tracked as a side effect.
- `git clean -xdf` still cannot destroy an untracked working doc.

## Related

- `.gitignore`: the `docs/*` catch-all, and the `*secret*` to `!agent/config/secret.py` precedent.
- `docs/KB_POLICY.md` for the three document tiers.
- Filed from the delivery arc that produced `9c8a771e` (runtime guidance doc) and `27f1a0b2` (vitest gating).
