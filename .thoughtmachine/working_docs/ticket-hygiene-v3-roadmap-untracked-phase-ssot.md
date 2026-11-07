# hygiene/v3-roadmap-untracked-phase-ssot — the phase SSOT is the one document no branch, no reviewer and no CI can see

**Category:** `hygiene`
**Status:** open; filed from evidence gathered in the `docs/w4-kb-braindump` pass. Not dispatched.
**Filed:** 2026-10-31, Gen 55

## 1. What this ticket is

`working_agreements.md` names `project/v3_roadmap.md` as the document that **owns phases**.
That file is gitignored. It exists in no git ref. It exists as exactly **one** copy, in the live
workspace knowledge base, which no clone contains.

So the authoritative phase plan is the single project document that a reviewer cannot read, a
branch cannot carry, and CI cannot check. Everything else in `project/` is opted back into git one
line at a time; the roadmap is the one that was not.

This ticket records the structural problem and the options. **It does not decide.**

## 2. Evidence (each item is a command, not a recollection)

**2.1 The SSOT claim.**
`working_agreements.md:4`, the scope note at the top of the file:

> `project/v3_roadmap.md` owns phases; this file owns *how we work*.

and `working_agreements.md:49`, in the table under `## 5. Where things live (so "written down"
means "findable")`:

> | Phases, ordering, branches, gates | `project/v3_roadmap.md` |

The section heading is the finding: the thing the protocol guarantees is *findable* is not findable
from a clone.

**2.2 The ignore rule and the eight exceptions.**
`.gitignore:221` — `.thoughtmachine/knowledge/project/**/*.md` ignores the whole directory.
`.gitignore:239-247` re-includes, by explicit line, exactly eight project docs: `MANDATORY_STOP.md`,
`INFRA_ONBOARDING.md`, `working_agreements.md` (`:241`), `security_architecture.md`, `status.md`,
`operator_observations.md`, `hall_of_fame.md`, `operator-dashboard.md` (`:247`).
**`v3_roadmap.md` is not among them.** The file that defines phases is the one exception nobody
wrote.

**2.3 No ref carries it.**
`git ls-tree -r HEAD .thoughtmachine/knowledge/project` lists **16** tracked `.md` files and
`v3_roadmap.md` is not one of them (checked at HEAD `6e01a004`; `origin/dev` likewise). The
worktree materialises those 16 and no roadmap:
`ls .worktrees/kb-braindump/.thoughtmachine/knowledge/project/v3_roadmap.md` → *No such file or
directory*.

**2.4 One copy on disk.**
`find /workspace -maxdepth 6 -name v3_roadmap.md` → exactly one hit,
`/workspace/.thoughtmachine/knowledge/project/v3_roadmap.md` (214 lines before the 2026-10-31
append, 281 after). It is unversioned: no revert path, no blame, no review.

**2.5 The loss mode has already fired in this directory.**
`project/roadmap.md:3` — a *tracked* sibling — records:

> **LOST (2026-08-16, S2 incident):** the `V3 ORDER` section (operator priority list) was
> uncommitted in the working tree at restore time and is not recoverable from git/workspace.

Roadmap content in `knowledge/project/`, lost to an uncommitted single-copy state, once already.

**2.6 A decoy in the same directory.**
A tracked `project/roadmap.md` exists (388 lines). It is **not** the phase order and says so
(`roadmap.md:5-12` disclaims itself, pointing at `project/epoch_plan.md` for the 2026-09-09
sequence and describing its own phase tables as "earlier-era history/ideas"). A reader who follows
`:49`, finds nothing, and looks next door lands on a document that states it is history.

## 3. Blast radius

- **A maintainer reading any branch, or the PR queue, cannot tell what the waves are.** The only
  in-repo trace of W1 / W1.5 / W2 / W3 / W4 as of 2026-10-31 is a block in `operator-dashboard.md` —
  itself a snapshot, and one carrying an `INTERIM … DO NOT TRUST AS COMPLETE` banner that admits it
  drifts. A snapshot is not a mirror.
- **A fresh clone has no roadmap at all.** `project/` materialises; the phase plan does not. The
  first thing a new contributor — or a new agent session — is told to consult is absent.
- **Review cannot police phase claims.** The roadmap's own mechanism (`## Claims per PR`: every PR
  description carries `Claim added/changed: <text>`, and "every phase boundary includes a 15-minute
  `machine_model.md` review") is a check against a file the reviewer cannot open. A claim line can
  assert anything about phases and no one reviewing the PR can falsify it.
- **The single copy is one restore away from gone**, and this directory has already lost roadmap
  content exactly that way (§2.5).
- **Two documents are plausibly "the roadmap"** and the tracked one says it is not (§2.6).

## 4. Options — stated as options, not a decision

**(a) Force-add it in place.** `git add -f .thoughtmachine/knowledge/project/v3_roadmap.md`, plus a
matching `!` line beside the other eight so the ignore rules state what is actually tracked.
Cheapest, and it keeps the exact path that `working_agreements.md:4,49` names. Cost: it is an
exception to the wildcard ignore, and a forced add *without* the `!` line leaves the next reader
unable to tell which rule governs the file.

**(b) Move it to a tracked `docs/` path and treat the live KB as a cache.** The repo already
re-includes specific `docs/` files one by one (e.g. `!docs/workspace_isolation_matrix.md`,
`.gitignore:171`), so the pattern is established. Requires updating `working_agreements.md:4,49` and
confirming what reads the live vault path, since a move changes what the knowledge tool serves until
the cache is refreshed.

**(c) Drop the SSOT claim.** Amend `working_agreements.md:4,49` so no document is named as owning
phases, and let ordering live where it actually lives today. Cost: the protocol loses its named home
for "ordering, branches, gates", which is the thing that file was written to provide.

**Not claimed here:** that the roadmap's *content* is wrong, or that the waves are unplanned. The
finding is about visibility, not correctness. Nor was the live-KB read path audited (knowledge tool,
doctor checks); whichever option is chosen, that audit is part of the work.

**Note on today's own edit:** the 2026-10-31 wave-ledger section was appended to the live file — an
unreviewable edit to an unreviewable document. That is an *instance* of this ticket, not a fix for
it, and it is recorded here rather than presented as the repair.

## 5. Related

- `ticket-hygiene-docs-gitignore-tax.md` — the same tax in `docs/`: every doc needs a hand-written
  `!` line, and a ruling on which shape wins is still owed.
- `ticket-hygiene-load-bearing-working-docs-not-in-reinclude.md` — identical failure shape in
  `working_docs/`; this ticket is its `knowledge/project/` sibling.
- `ticket-hygiene-working-docs-cluster-reports-gitignored.md` — related, and deliberately **not to
  be fixed**.
- `ticket-hygiene-global-kb-docs-claim-writable-global-scope.md` — docs claiming a scope the system
  does not provide; this is docs claiming a location the repo does not carry.
- `ticket-docs-dashboard-state-of-play-stale.md` — the tracked record currently standing in for the
  roadmap drifts by construction.
