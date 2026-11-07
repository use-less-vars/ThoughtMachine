# Ticket: documentation can be silently untracked, and nothing notices

Status: open

## Problem

Documentation in this repo is not reliably tracked by git. `.gitignore` excludes
`.thoughtmachine/working_docs/` wholesale and re-includes individual files by hand-written
glob, so a doc can exist on disk, be invisible to `git status`, and never reach a clone,
a review, or CI. There is no failure mode: the file is simply not there for everyone else.

Two symptoms were observed during the auth-boundary work:

1. 18 documentation files were found untracked and had to be re-included by hand.
2. `.gitignore` itself was found containing conflict markers.

Neither condition is caught by anything in CI. The second one is the worse one: a
`.gitignore` with conflict markers does not fail; it silently changes what is ignored,
which is how the first condition happens.

## Evidence

- `.gitignore:117-128` — `.thoughtmachine/workers.json` and
  `.thoughtmachine/working_docs/` are ignored (`:117`, `:118`), then re-opened by
  `!.thoughtmachine/working_docs/` (`:119`) and narrowed again by
  `.thoughtmachine/working_docs/*` (`:120`), followed by per-file re-includes:
  `reproduce_p0.py` (`:121`), `reproduce_p0.sh` (`:122`),
  `P0_REPRODUCER_README.md` (`:123`), `ticket-*.md` (`:124`),
  `tool_no_wrapping_guard.py` (`:126`), `protocol-*.md` (`:128`).
- `.thoughtmachine/working_docs/` currently holds **215** `ticket-*.md` files. One wrong
  character in the `ticket-*.md` re-include (`:124`) silently drops all 215 from every
  clone — and from every review of a change to any of them.
- The 18-file re-include fix from the previous wave was applied by hand for exactly this
  reason; the mechanism that made it necessary is unchanged.
- One of the silently-untracked files was the auth boundary threat model
  (`docs/design-auth-boundary.md`). A security design that reviewers cannot see in the
  diff is a security design that does not get reviewed.

## Suggested fix

One small script plus one step in the `ubuntu-suite` job of
`.github/workflows/platform-matrix.yml` (the job that already runs
`scripts/ci_assert_collected.py`, `:76`). The lint fails when:

1. `.gitignore` **or any tracked file** contains conflict markers — `<<<<<<<`,
   `=======` at line start, `>>>>>>>`.
2. A file matching `docs/**/*.md` or `.thoughtmachine/working_docs/ticket-*.md` exists on
   disk but `git ls-files --error-unmatch <path>` does not know it.
3. It prints the untracked list, not just a count — a count tells a reader that something
   is wrong but not which document they are about to lose.

Prefer this over extending the hand-written `!` re-includes: every new `!` line is a new
opportunity for the same silent failure, and the lint is what would have caught all three
of the observed symptoms.

## Why it matters

The pattern is "the artefact exists but is not in the repository", which is indistinguishable
from "the artefact does not exist" for everyone who is not the author. For docs that record
security decisions, that is the difference between a reviewed design and an oral tradition.

## What was not verified

The exact count of files that were untracked during the previous wave (18, as reported)
was not re-measured here: `git` was not run in this session, so the current tracked/untracked
split of `.thoughtmachine/working_docs/` is unverified from this tree.
