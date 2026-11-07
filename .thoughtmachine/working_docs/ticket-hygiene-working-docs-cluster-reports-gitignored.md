# Ticket: hygiene/working-docs-cluster-reports-gitignored

**Filed:** 2026-10-21, from the Cluster 2 / Cluster 3 audit sessions (HEAD `9d5ed869`).
**Severity:** low — hygiene / artifact disposition. No runtime behaviour; nothing is broken. It is a
question about whether two pieces of finished work can be committed at all.
**Status:** open; NOT to be fixed in this wave. **Do not edit `.gitignore`.**

## Finding

`.thoughtmachine/working_docs/` is carved out of tracking by a four-step block at `.gitignore:118-126`:

```
 118  .thoughtmachine/working_docs/
 119  !.thoughtmachine/working_docs/
 120  .thoughtmachine/working_docs/*
 121  !.thoughtmachine/working_docs/reproduce_p0.py
 122  !.thoughtmachine/working_docs/reproduce_p0.sh
 123  !.thoughtmachine/working_docs/P0_REPRODUCER_README.md
 124  !.thoughtmachine/working_docs/ticket-*.md
 125  # Protocols are meant to be run by someone else — they are not scratch.
 126  !.thoughtmachine/working_docs/protocol-*.md
```

(lines 118→119 re-include the *directory* — required for any negation inside it to take effect —
and 120 then re-excludes every *file* in it; 121-126 un-ignore only the five named patterns.)

Consequence: two audit deliverables now on disk match no negation and are therefore invisible to
`git status`:

- `.thoughtmachine/working_docs/cluster2-http-surface-map.md` — 254 lines
- `.thoughtmachine/working_docs/cluster3-registry-state-map.md` — 214 lines (25,440 bytes)

Both are finished products of a full read pass at `9d5ed869` (surface inventory + scope matrix, with
per-claim file:line citations and an explicit NOT-READ manifest). Neither was edited from a tracked
state — they were created ignored — so nothing in this wave has made them committable, and a
`git clean`/fresh checkout loses them with the working tree.

For contrast, the sibling `ticket-*.md` and `protocol-*.md` files in the same directory *are*
trackable, which is how the ticket set survives; the two cluster reports fall outside every pattern.

## Why it matters / the tradeoff

**Probably deliberate, and probably still right.** Excluding scratch working docs from the repo keeps
transient agent output out of review diffs, and the negation list is narrow on purpose. The gap is
categorical, not accidental: `cluster2-…` and `cluster3-…` are not scratch — they are the audit
artifacts themselves, i.e. the thing a reviewer would want to open, cite and diff against later.

So the question is not "why is this ignored" but "where do multi-session audit/report artifacts
belong". Three shapes it could take, all operator calls:

1. **Leave as is** — accept that cluster reports are ephemeral working state; they live and die with
   the tree. Cheapest, and consistent with the current block.
2. **Widen the negation** — add a pattern (e.g. `!.thoughtmachine/working_docs/cluster-*.md`, or a
   `report-*.md` class alongside the existing `report-gen47-container-ops-run-2026-10-20.md`, which
   is in the same directory and equally invisible today) so audit artifacts are committable by name
   class rather than by one-off `git add -f`.
3. **Move the artifacts** — give audit deliverables a tracked home (e.g. `docs/audit/`) and leave
   `.thoughtmachine/working_docs/` purely scratch. Cleanest separation; costs a convention change and
   a place to put the "these are read-only finding sets at HEAD X" header that the cluster files
   already carry.

Note for whichever option is chosen: `git add -f` on the two files *would* work (ignore rules stop
applying once a path is tracked), but it makes their tracked/ignored status an accident of who ran
what, rather than a stated policy — so it is a workaround, not option.

**Status of the work itself is not affected:** both files are on disk now, unmodified, and the
findings in them stand as recorded at `9d5ed869`. This ticket is only about their durability.

## MUST NOT TOUCH

- **Do not edit `.gitignore` in this wave** (or any other, without an explicit operator decision on
  one of the options above).
- Do not re-open, re-scope or "refactor" the Cluster 2 / Cluster 3 findings here; this ticket is
  artifact disposition only.
- Do not move the two cluster files to make them trackable — a move is a content-hygiene decision
  (option 3), not a cleanup action.
- No other files are to be filed from this observation.
