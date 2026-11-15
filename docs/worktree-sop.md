# Worktree SOP — and the Gen 64 process era

**Tier:** public (ships with the repo; `docs/*` is a catch-all, so the negation
line in `.gitignore` is what makes this file committable).
**Audience:** the agent working a task, and the Host/operator supervising it.

Why this file and not `HANDOFF.md`: `.thoughtmachine/knowledge/project/HANDOFF.md`
is **generated** by `scripts/gen_handoff.py` and its header says "do not
hand-edit". `scripts/check_kb_staleness.sh --check` gates it in CI, so a
hand-written paragraph in it would be clobbered at the next regeneration. Durable
lore goes in hand-maintained files; `HANDOFF.md` only gets refreshed digests.

Two labels are used throughout: **[verified]** = read out of this repo at
`dev` = `3eef2b56`; **[reported]** = operator brief, not checkable from inside
the repo.

---

## 1. Gen 64: The Process & Worktree Era

### 1.1 Process Mode is finally REAL (C62 → C63)

- **[verified]** `tools/workspace/worker_runtime.py:317` — the resolution rule:
  *"Absent -> `DEFAULT_RUNTIME_MODE` (`process`, per C62). Anything present that
  is not exactly `MODE_THREAD` or `MODE_PROCESS` -> `InvalidRuntimeModeError`
  (fail closed: no trimming, no case folding, no empty-string-as-default)."*
  So the default is now the honest one, and a typo in `WORKER_RUNTIME_MODE` is a
  loud failure rather than a silent downgrade to thread mode.
- **[verified]** commits on `dev`: `7ca694c4` *fix(runtime): make the default
  runtime mode truthful (C62 pre-work)* → `ac6698bb` *feat(worker): ship process
  mode as the default (C62)* → `db813265` *C62c: fix process-mode child bootstrap
  and stop the child re-running the spawn query* → `0a279d22` *C62d: let the
  machine inspect its own substrate — Worker action="inspect"*.
- **Why it mattered:** before C62 the machine *described* process isolation while
  the production spawn site built a thread handle. The digest text in
  `scripts/gen_handoff.py:183-189` is exactly that older story — see §4.

### 1.2 C68 — session identity is an authority fact, not an argument

- **[verified]** `ab102eab` *C68: session identity is an authority fact, never an
  argument* — `session_id` is injected by the executor. Absent → **fail closed**.
- **[verified]** `ff9ce1e0` *C68b: … hide injected fields from the model; tracked
  pre-commit gate* — the five executor-injected fields (`session_permissions`,
  `effective_permissions`, `agent_config`, `is_worker_context`, `session_id`) are
  `pydantic.json_schema.SkipJsonSchema` on the tool base model. They still
  validate at runtime, but they are absent from `model_json_schema()` /
  `model_to_openai_tool()`, so a model can neither see them nor forge them.
  Pinned by `tests/tools/test_tool_schemas.py`; filed as done in
  `PRIORITIES.md:366-375`.
- **The trap that was closed:** a plain `Field(exclude=True)` removes the field
  from *serialisation* only — the JSON schema still advertised it, so a model
  could send it and override an authority value. `SkipJsonSchema` is what the
  schema-level fix needed.

### 1.3 C73 — the "Shared Intern" collision

- **[verified]** `bf10c56d` *C73: scope worker dirs, bus subscriptions and resume
  cache per session*, merged as PR #369 (`3eef2b56`).
- **[verified]** `tools/workspace/worker_registry.py:205-236` —
  `WorkerRegistry.worker_dir(workspace_dir, session_id, worker_name,
  instance_id)` is **the single place** that builds the path and returns
  `<workspace_dir>/workers/<session_id>/<label>`, where `<label>` is
  `worker_name`, or `worker_name#<instance_id>` for a second instance. When
  `session_id` is not a safe single path segment the component is dropped and the
  path falls back to `workers/<label>` (no traversal, no `..`).
- **What it cost before:** every session shared one `workers/<name>` directory,
  so two sessions running an agent called `default` read and wrote the same
  journal/outbox — a second session's worker could adopt the first session's
  in-flight command. Namespacing plus per-session bus subscriptions and resume
  cache is the fix.

### 1.4 CI ruleset locked

- **[reported]** `dev` requires the **Python Version Matrix** check to be fully
  green; `--admin` merges are not part of the process. A ruleset is server-side
  GitHub state and cannot be verified from the working tree — trust the operator
  here, and behave as if it bites: **never** merge with `--admin`, never merge on
  a red or pending check.

---

## 2. The SOP (the loop, in order)

1. **Agent works in a worktree.** `.worktrees/<task>` on branch
   `<type>/<task>`, cut from `dev`. `.worktrees/` is ignored
   (`.gitignore:380`), so no task tree can be committed by accident.
2. **Agent commits and pushes the branch.** In practice the agent commits; the
   **Host pushes** (§3.1).
3. **Host creates the PR.**
4. **Host waits for CI.** The ruleset enforces it; a red matrix is a stop, not an
   obstacle to route around.
5. **Host merges on GitHub.**
6. **Host syncs the trunk:** `git checkout dev && git pull origin dev`.
7. **If `dev` moved and the PR is blocked:** merge `origin/dev` **into the
   worktree branch** (inside the worktree, so the task's edits are the ones being
   resolved), push, and wait for the *new* CI run before merging. Do not rebase a
   branch other people have reviewed.

The invariant behind the loop: the agent never moves `dev`, and `dev` only moves
through a green PR.

---

## 3. Tooling quirks (the ones that actually bite)

### 3.1 `git_write push` is blocked for the agent

The agent's session has `network: banned`, so a push from the agent is refused.
Committing is the agent's job (§3.2); **pushing is the Host's**. Same for
`clone` against a remote.

### 3.2 `git_write` has no merge/rebase verbs

The git tool exposes `commit`, `init`, `clone`, `push`, `branch_create`,
`checkout`, `stage`, `unstage`, `worktree_add`, `worktree_remove`,
`stash_push`, `stash_pop` — and **no merge, no rebase**. Any branch
reconciliation (the `origin/dev` merge in step 7, or resolving a conflict) is
done by the **Host**, in the worktree, with a real `git`.

### 3.3 `prove_worker_pid.py` was rigged; the proof moved into pytest

`scripts/prove_worker_pid.py` is a *demonstration* script: it imports
`MODE_PROCESS` / `MODE_THREAD` and drives the layers itself, and its older form
could report "isolated" while the body still shared the interpreter. It is not
evidence. The evidence is the tracked test
`tests/test_worker_is_a_real_process.py` — a real forked child observed through
`inspect`, asserted to have a **different OS pid**. Commit `d5384912` (*observe a
real forked child through inspect, assert it is a different OS process*) and
`87a3b03f` (*retarget the PID-isolation suite at the real witness script*) are
that retargeting. Rule: **a proof that lives in a script nobody runs is
decoration; a proof is a test the matrix runs.**

### 3.4 `_fence_epoch` vs `_generation` — two numbers, never interchangeable

- **`_generation`** advances: it is the journal's incarnation number. Each
  incarnation must ADVANCE it, so two incarnations never share a journal row and
  a successor can tell a crashed predecessor's record from its own and adopt it
  (`tools/workspace/worker_thread.py:2660-2670`).
- **`_fence_epoch`** is constant: it is the supervisor's *handed* epoch and stays
  FIXED for the lifetime of the conversation, so the supervisor's own fenced
  commands keep being accepted by the child it spawned
  (`worker_thread.py:1592-1599`; `worker_runtime_main.py:319-326` hands the epoch
  to the child).
- **`_fence()` uses `_fence_epoch`, NOT `_generation`** — stated in the code as
  *"(C63-4). The two are deliberately separate numbers … Using the moving number
  here would make a respawn reject the commands of the supervisor that spawned
  it"* (`worker_thread.py:2799-2817`). Thread mode hands nothing, so the two
  coincide there — which is exactly why the bug was invisible until process mode
  became the default.
- Corollary: a fence built once and cached in `__init__` is wrong; it is rebuilt
  fresh per call because `_generation` is re-allocated on every spawn.

---

## 4. Known-stale, do not trust

- **`scripts/gen_handoff.py:183-189`** still renders the pre-C62 sentence
  (*"`MODE_THREAD`, the truthful default … workers still run inside the
  supervisor's interpreter … C62 flips the default once the spawn site starts
  one"*) into the generated `HANDOFF.md`. Regenerating the handoff does **not**
  fix this — the source string must change, and then be regenerated. Until then
  `HANDOFF.md` under-describes §1.1.
- **C69 (Replay Resistance) is OPEN**, not done. Filed during the C68 work
  (`52f8108d`), scope only: replay resistance via a spec nonce — a captured
  spec/argv pair should not be replayable. `PRIORITIES.md:377-382`.
