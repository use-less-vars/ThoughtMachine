# KB Policy — three tiers, one rule

**Status:** policy. Public tier, ships with the repo.
**Written:** Gen 46, 2026-10-20, against `dev` @ `44a638b8`.
**Audience:** anyone about to write a document and unsure where it belongs.

## The three tiers

| Tier | Lives in | In git? | Owner | Contains |
|---|---|---|---|---|
| **Public** | `docs/` | **Yes**, by explicit allowlist | Infra | Architecture, contracts, protocols, operational guides. Written for a reader who is not in this project yet. |
| **Internal** | `.thoughtmachine/knowledge/project/` | **No** (`.gitignore`) | Main | Status, roadmap, doctrine, design grids, audit findings. Written for whoever sits in the seat next session. |
| **Diary** | `.thoughtmachine/knowledge/personal/`, `.thoughtmachine/knowledge/archive/` | **No** (`.gitignore`) | Infra | Lessons, bug logs, operator observations, session transcripts. Written for the person working, never deleted. |

Working documents (`.thoughtmachine/working_docs/`) sit beside these and are a
**fourth thing, not a fourth tier**: tickets and specs in flight. They are
gitignore-exceptional (`ticket-*.md` is re-included) so a ticket can be attached
to a PR; that does not promote them to the Public tier.

## The one rule

> **The KB is the source. `docs/` is the publication.**

Internal knowledge is written for the seat: terse, dated, revisable, allowed to
be wrong and then fixed. A public doc is a *claim made to a stranger* — it must
be true without the reader knowing our history, and it must be maintained,
because a stranger cannot tell stale from current.

So the flow is one direction:

```
internal KB (draft, dated, revisable)  ──promotion──▶  docs/ (published claim)
```

Nothing is published first and reconciled later.

## Promotion

Promotion is an **explicit act with a receipt**, never a copy-paste and never a
symlink:

1. Someone decides the claim is public-quality (true without our context,
   maintained by someone named).
2. The file is **copied** to `docs/`, rewritten for a stranger: drop the
   generation numbers, the ticket names, and the "as of last week" phrasing.
3. **`.gitignore` gets a line.** `docs/*` is excluded wholesale; each public doc
   is re-included individually (`!docs/KB_POLICY.md`). A doc without its line
   exists on disk and is invisible to git — the classic silent failure here.
   Subdirectory contents need a directory negation **and** a contents negation
   (`!docs/ci/` and `!docs/ci/**`), because git cannot re-include a file beneath
   an excluded directory.
4. A **receipt** is recorded in the internal KB: what was promoted, from where,
   when, at which commit. Without it, the two copies drift and nobody notices
   which one is authoritative.
5. From then on the **public** copy is authoritative for that claim. Fixes go to
   `docs/` first. The internal file may keep a pointer; it must not keep a rival
   version.

## What is *not* this policy

`three_tier_process_model.md` names a **different three tiers** — intelligence /
immune / operations. That is an axis over *process*; this is an axis over
*documents*. They compose (the intelligence tier writes the KB; the operations
tier is why so little gets published) but do not restate each other. For layers
of the *machine*, see `docs/ARCHITECTURE_MACHINE_MODEL.md`.

## The correction this policy has to carry

The intuitive statement — *"nothing under `.thoughtmachine/knowledge/` is
tracked"* — is what the `.gitignore` rules say and what everyone believes. **It
is currently false.** `git ls-tree -r .thoughtmachine/knowledge` at `44a638b8`
lists eleven blobs:

- `.thoughtmachine/knowledge/.gitkeep`
- `.thoughtmachine/knowledge/tool_categories.md`
- `.thoughtmachine/knowledge/project/`: `architecture_audit_2026-09.md`,
  `archive_arch_a.md`, `archive_arch_b.md`,
  `design_container_lifecycle_and_reclamation.md`, `development_guides.md`,
  `ideas_and_brainstorming.md`, `roadmap.md`, `security_architecture.md`,
  `self_description_reconciliation.md`

`.thoughtmachine/knowledge/project/**/*.md` is ignored, but a file already in the
index is not un-tracked by adding an ignore rule — the ignore only stops *new*
files from being added. These ten were added before the rule (or force-added
after it) and have been riding along ever since. They show up as *modified* in
`git status` on every checkout, which is why sessions keep inheriting "dirty"
KB files.

This is **drift, not policy**. Two resolutions, one decision:

- **Untrack them** — `git rm --cached` those ten (`.gitkeep` stays). The policy
  statement becomes true. Cost: the files leave `dev` history going forward and
  any reader who fetched them keeps a stale copy.
- **Adopt them** — accept a small, named, public-readable subset of the internal
  KB and say so here. Cost: they become public claims that must be maintained,
  which is exactly what the Internal tier exists to avoid.

Until that decision is made, treat a tracked KB file as an **accident**, never
as a precedent. Do not add more.

## Rules of thumb

- **Dated findings → Internal.** Anything that will be wrong in three months
  ("dev HEAD is `44a638b8`") never goes public.
- **Contracts and protocols → Public.** If it constrains someone's future
  decision, they need it without asking us.
- **Never delete Diary.** `personal/` and `archive/` are append-only by
  convention; archive by moving, not removing.
- **One claim, one authoritative copy.** Two copies of the same sentence is a
  future contradiction with a filing date.
