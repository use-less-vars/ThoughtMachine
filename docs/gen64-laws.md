# The Laws of Gen 64

Four rules that survived the Gen 64 migration. They are written down because each
one was re-learned the expensive way, and because a rule that lives only in a
chat log is a rule the next generation will re-litigate.

**How to read the status labels.** `[verified]` means the claim is checkable in
this tree: the citation is a file and line you can open, and it says what the law
says. `[ruling]` means it is an operator ruling — a decision, not a mechanism.
Nothing in this repository enforces a `[ruling]`; it is enforced by the people
merging. Do not dress a ruling up as code.

---

## Law I — No `--admin` merges. The CI matrix must be green.

**`[ruling]`** — operator, Gen 64.

A pull request merges when the full platform matrix has passed on the exact head
commit being merged. Not "passed on the previous push". Not "the red leg is
probably the flake we know about". If a leg is red, either it is a real failure
and gets fixed, or it is identified as a flake and **re-run** — never bypassed
with an administrative merge.

The reason this is a law rather than a preference: the matrix is the only place
that runs Windows, macOS and Linux against the same commit, and a bypass merges
the one configuration nobody measured. Every "we'll fix it forward" bypass on
this project cost more time than the re-run would have.

**Nothing in the tree can check this for you.** Branch protection and rulesets
live server-side in GitHub; the repository cannot see them, and no local script
can assert that a merge button was green. So the enforcement is social and the
law is written down here on purpose.

## Law II — The Host works in the root. The Agent works in a worktree.

**`[verified]`** — `.gitignore` ignores `.worktrees/`; the branch that carries
this document was cut and committed from `.worktrees/<name>`, not from the
repository root.

The operator's checkout is the root and holds the operator's branch. An agent
takes a **worktree** — a second checkout of the repository on its own branch,
under `.worktrees/<name>` — for every change it makes. Two processes never share
one index, so a long agent task cannot move the operator's HEAD, and cannot be
derailed by the operator's uncommitted work either.

The practical rules that fall out of it:

- Cut the branch from `dev` **first**, then add the worktree on that branch.
  Adding a worktree directly on `dev` fails when `dev` is already checked out in
  the root, and a detached worktree cannot commit.
- `git merge origin/dev` **into** the worktree branch when it falls behind. Do
  not rebase a branch that is already pushed.
- The worktree is a real checkout: it has its own working files, and edits in it
  are invisible to the root until they are committed and merged.

## Law III — Stop detaches. Pause parks. No interrupt verb kills a worker's context.

**`[verified]`** — `docs/CONTROL_PLANE.md` §4.3 (the three-way table), §4.4
(worker-side stop = detach) and §10.3 (the four semantics); the mechanism is
`tools/workspace/worker_thread.py:1771` (`detach()`), `:1798` (`_save_context()`),
`:1810` (status `detached`), `:1823` (`kill()` — the only termination path).

The three operator-visible interrupts are not three strengths of one action:

| Verb | What happens to the turn | What happens to the worker |
|---|---|---|
| **Pause** | soft wind-down: the turn concludes and is summarised | parks `paused`, resumable |
| **Stop** | hard abort: the in-flight turn is **discarded**, no summary | **detaches**: `_save_context()`, status `detached`, resumable |
| **Timeout** | the caller stops waiting; the worker keeps working | unchanged, still `running` |

So "Stop is a hard abort" is a statement about the **turn**, never about the
worker. Stop writes the context to disk and parks the worker as `detached`;
`resume()` brings it back. `kill()` is the only verb that terminates, and it is a
reaper's verb, not an interrupt's.

Corollary, and the reason this is a law: **an interrupt must never be the reason
a conversation is lost.** If a change makes stop discard context, the change is
wrong even if every test it ships with is green.

## Law IV — `session_id` is injected by the executor. The model can never supply it.

**`[verified]`** — `tools/base.py:77-89`: `session_id` is an **authority fact**,
typed `SkipJsonSchema[Optional[str]]` with `exclude=True`, documented as
"injected by ToolExecutor … popped out of the caller's arguments before
validation, never an argument".

The identity a tool acts on is decided by the runtime, not by the caller's
arguments, and the model is not even told the field exists: `SkipJsonSchema`
removes it from every generated JSON schema, so it never appears in the tool
description the model is shown. The executor sets it; validation pops it out of
whatever the model sent.

Two consequences worth stating separately, because both have been violated:

- **A user-supplied `session_id` is not a credential.** Anything that reads a
  session id out of arguments, a URL, or a payload and trusts it has re-created
  the hole the injection closes.
- **`exclude=True` alone would not have been enough.** It filters
  `model_dump()`, not the schema; only `SkipJsonSchema` keeps the field out of
  the tool definition. This is the C68 distinction, and it is the difference
  between "not documented" and "not offered".

---

## What is deliberately *not* in this list

- **"Merge only from a clean tree."** True, but it is a consequence of Law II,
  not a separate law.
- **"Never force-push a shared branch."** Also true, and enforced by the tooling
  rather than by discipline: the git write tool refuses a forced push outright.
  A rule the tool already keeps does not need a generation of prose.
- **"Always run the tests."** That is Law I seen from the other end.

*Written for Gen 64. When a law stops being true, change the law and say which
commit made it false — do not quietly stop citing it.*
