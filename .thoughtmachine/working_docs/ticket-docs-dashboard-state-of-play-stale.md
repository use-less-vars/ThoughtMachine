# ticket: `docs/dashboard-state-of-play-stale`

**Filed:** 2026-10-31 (Gen 55 onboarding)
**Status:** open
**Severity:** low

## Symptom

`operator-dashboard.md` lists "State of play: Branch `fix/phantom-config-notification`, HEAD
`6399efb4`" with two commits unpushed locally. That branch merged in #257 and those commits are on
`dev`. The dashboard is the Operator's external brain, and its first-read section describes a state
from three days ago.

## Evidence

`.thoughtmachine/knowledge/project/operator-dashboard.md`, section
`## State of play (read this first)`, quoted verbatim:

> - **Branch:** `fix/phantom-config-notification`, HEAD `6399efb4` — *"fix(agent): stop base_url
>   spelling variants forcing a restart and a phantom notification"*.
> - Known: URL normalisation bug in agent/core — fixed in `6399efb4`, awaiting push. Do not panic.
> - **Two commits sit unpushed locally on this branch. CI has not run on them.** Nothing has
>   validated them but the local gate, and no reviewer has seen them.
> - The branch's parent is `fbdef41b` (merge of PR #255, the sysprompt implementation brief), so
>   `project/status.md`'s "dev HEAD `12bd6408`" (2026-10-28) is stale — the chain has moved on.

The section's own last bullet is therefore the only part of it that is still true, and only in the
sense that it calls the *other* document stale. The branch named as unmerged work has merged; "Do not
panic" and "awaiting push" are instructions about a state that no longer exists.

The staleness is not measurable from here by SHA: this environment has no `git` binary, and
`6399efb4` occurs **0×** in `project/status.md` and `4c700791` occurs **nowhere** under
`.thoughtmachine`. The divergence is taken as reported (branch merged in #257; current base
`4c700791`), not re-derived.

## Options (needs a ruling; do not implement blind)

1. **Hand-refresh at each strand close.** The section is rewritten by hand when the strand closes,
   as part of the same act that closes it. Cost: it is exactly the discipline that already failed
   here — the section is the *first* thing read, so it is read long before the close.
2. **Regenerate the section from a single field.** A rule that the section names only
   `dev @ <sha>` and is generated, so it cannot describe an unmerged local branch at all. Cost: the
   section loses the "what to watch out for" commentary that makes it useful, and generation needs a
   git-capable job.

**Do not fix this opportunistically** — the section is quoted by other documents and its wording
embeds a judgment about what the Operator should be careful of. Ruling first.

## Acceptance criteria

- The section names `dev @ 4c700791` (or the then-current SHA) and nothing else as the branch state.
- It contains no claim about an unpushed local branch that is already merged.
- It contains no instruction ("do not panic", "awaiting push") that is true only of a state that has
  passed.

## Related

- Ticket `docs/status-md-stale` — the same drift in `project/status.md`, whose stale HEAD this
  section currently cites.
- The dashboard front-matter backfill ticket, `chore/ticket-front-matter-backfill`.
