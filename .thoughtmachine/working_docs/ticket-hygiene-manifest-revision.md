# Ticket: hygiene/manifest-revision

**Filed:** 2026-10-25 (Gen 50), from `redteam-surface-complete.md` and `vault-ownership-sites.md`.
**Severity:** MEDIUM — the vault ownership manifest's vocabulary causes it to record the code as more scoped than it is.
**Status:** open; filed, not dispatched.

## Finding

Resolving the manifest's 46 rows to writer/reader/deleter symbols (`vault-ownership-sites.md`) surfaced eight divergences, D1–D8. The pattern is not eight errors. It is one bias: **every place the manifest guessed, it guessed that the code was more scoped than the code is.**

- `unknown` where the code had answers (D1, §3.1)
- an absent deleter where a route exists (D2, §3.2)
- a scoped realm where the namespace key cannot express a workspace at all (D3, §3.3)
- an out-of-vault store that is in fact a vault path (D4, §3.4)
- a leaky `host process` cwd where the code resolves and confines it (D5, §3.8/§5.3/§6.6)
- a blanket realm recorded for C1 branches that touch nothing at all (D6)
- two reads the manifest treats as scoped are not, on inspection (D8)

The one divergence pointing the other way, D7, is the same bias read backwards: eight machine-global state files carry no workspace segment, so the workspace is not a boundary for them — and the realm vocabulary has no word for "per machine", so those rows were filed under a realm that implies a boundary that isn't there.

## The two principles the revision needs

1. **`unknown` is a defect, not a value.** A row that cannot name its writer is a gap to close, not a status to record. The manifest treated `unknown` as an honest answer; it is an unanswered question.
2. **Add a realm for machine-global state.** The current vocabulary forces global files into workspace-implying realms. A machine-global realm (or an explicit `realm: none — global` with its own row status) removes the false implication.

A manifest built on those two principles would have caught most of D1–D8 by construction.

## What this is not

Not a rewrite instruction. Not a dispatch request. Not a claim that the manifest was careless — it was written before the symbols were resolved, which is the ordering problem, not the author.

## Smallest first step

When the manifest is next revised, apply the two principles, then re-run `vault-ownership-sites.md`'s resolution against the revised rows. Rows that still resolve to `UNOWNED` or `no writer` are the real debt.
