# Ticket: hygiene/docs-hidden-files-triage

**Filed:** 2026-10-27, from the streaming/IR design pass, at base commit `f6a70d81`
**Category:** hygiene
**Severity:** MEDIUM — a cited public doc was never committed; six docs are one `git clean -xdf` from deletion
**Status:** filed, not fixed

## Summary

`.gitignore:136` is `docs/*`, a catch-all with no negation for the entries below. They
exist on disk and are invisible to git.

Top-level docs, ignored: `audit-worker-core.md`, `backend-audit.md`,
`design-input-auth-layer-prerequisite.md`, `frontend-audit.md`, `frontend-state-qa.md`,
`system-map.md` — all under `docs/`.

Directories, ignored: `docs/audit/` and `docs/xray/`. `docs/*` excludes the directory
entry itself and git cannot re-include a file beneath an excluded directory, so nothing
under either is reachable until the directory is negated; the two-line pattern
(`!docs/<dir>/`, then `!docs/<dir>/**`) is documented at `.gitignore:143-149`.

**The urgent case.** `docs/design-input-auth-layer-prerequisite.md` is named as a
carrier in the Corrections section of `docs/design-auth-layer.md:119`. A committed
record cites a document that was never committed; it is meant to be public, and no
reader of the repo can open it.

**Triage questions.** For each of the six files and the two directories: public tier
(deserves a negation), private scratch to delete, or superseded (name the successor)?
The tier is the operator's call, not this ticket's.

**This ticket fixes none of them.** It records the state only. `git clean -xdf` in
ignored space would destroy all of it, and none of it is committed, so there is no
recovery path.

## Filed

2026-10-27, from the streaming/IR design pass, at base commit `f6a70d81`. Verified by
`git ls-tree -r HEAD docs`: none of the six files, and no file under `docs/audit/` or
`docs/xray/`, is in the tree, while all of them are present on disk.
