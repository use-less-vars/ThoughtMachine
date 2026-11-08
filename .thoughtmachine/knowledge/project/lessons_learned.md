# lessons_learned.md — what cost us time, and the rule that came out of it

**How to read this file.** One section per generation. Each entry is a real event, the cost it imposed, and the rule that prevents a repeat. Entries are append-only: a rule that later turns out to be wrong gets a dated correction under it, not a deletion. If you are about to repeat something below, you are about to pay for it again.

---

## Gen 60 — The Heat of the Moment

### 1. State versus renderer: the machine is usually fine and something in the middle is lying

Three "the machine is broken" reports in one session, one shape:

- A **stale restart badge** kept claiming a restart was pending after the restarts had happened.
- **"The guards were never committed"** — the working tree was checked out on `dev`, so `git show` rendered pre-fix code and an agent confidently reported that hardening which existed was absent.
- **"0 sessions"** — the registry held 17,300 bytes and 50 entries with valid `workspace_id`s; 46 were marked closed by an unconditional `rebuild_from_disk()` at startup, and every "sessions" surface was counting **open** sessions while calling it a total.

**Rule.** Before believing any report about state, establish **which tree, which layer, and which boolean** the renderer is actually reading. `git read` the exact file on the exact branch first — cheapest check available, and it would have saved all three detours. Corollary: name the subset. A count of open sessions labelled "sessions" is a lie even when the number is right.

### 2. Ask a worker to quote verbatim; never ask it to confirm

A worker asked to "confirm" a claim will confirm it. Workers answer the question that was asked, and confirmation is a question with a preferred answer. A worker asked to "quote the first line of the file, verbatim, including any timestamp prefix" produces something falsifiable in one glance.

**Rule.** Every worker report must contain at least one **verbatim quote** (first line, exact test summary line, exact `git diff --stat` line) plus the command that produced it. Then check the quote against the artifact yourself. Testimony is not evidence (see `vision_ledger.md` V2).

### 3. The tooling itself lies — log it, route around it, do not build on it

Observed live, all in Gen 60/61:

- **`Worker spawn` drops `context["meta"]`.** Metadata sent with a spawn never reaches the worker. Anything the worker must know goes in the `query` text, a `WorkingDocument`, or the task is lost.
- **Long inline `query` payloads are truncated.** A worker was given a multi-page brief inline, received part of it, and **hallucinated the missing part** — the output looked complete and was wrong. Proven workaround: park the payload in a `WorkingDocument`, send a 3–5 line query naming the `doc_id`.
- **`WorkingDocument append` injects a `[YYYY-MM-DD HH:MM:SS] ` prefix** into the section it appends to. Any text copied out of a working document must have that prefix stripped before it reaches a repository file.
- **`git_read show_file` ignores `line_start` / `line_end`** and dumps the whole file — reading "50 lines of `.gitignore`" returned all ~400 of them.
- **`git_read` has no `fetch`.** Remote state cannot be refreshed from the agent surface; a stale `origin/dev` reads exactly like a fresh one.
- **`Worker` spawn status fields are untrustworthy.** One spawn returned `status: "timeout"`, `timeout_triggered: true`, `elapsed_seconds: 421.6` against a `timeout_seconds: 600` request, and the content was complete and correct. Judge the deliverable, not the envelope.

**Rule.** When a tool's report and the artifact disagree, the artifact wins and the tool gets a note. Do not build a workflow that depends on the broken surface — route around it once, and write the workaround down.

### 4. Premise corrections — four things the session "knew" that were false

Each of these was stated with confidence and was wrong. Each one inverted a plan.

- **"The executor was reused."** It was not. Assuming reuse inverted the precedence order of the execution path.
- **"The registry was wiped."** It was not — nothing was lost. The registry was structurally intact and lying about one boolean; the "flat versus wrapped dict" question that followed was a phantom of the wrong premise, and a recovery script written from that premise would have destroyed healthy state.
- **"`vault_repair` deletes directories."** It does not. A documented behaviour that does not exist will be relied on by the next reader.
- **"`.githooks/pre-commit` exists."** It never did. A file that is asserted instead of listed is a hallucination with a plausible path.

**Rule.** A premise is a claim. Verify it with the cheapest artifact that can falsify it — `ls`, `git ls-tree`, `git show <branch>:<path>` — **before** designing anything on top of it. Two of the four above would have been settled by a single directory listing, and the cost of not doing it was a plan built backwards.

### 5. Work that was declared done and was not

Housekeeping files were written into `.thoughtmachine/knowledge/`, declared complete, and were invisible to git: `project/**/*.md` is ignored wholesale and only a named set is re-included by `!` negations. A new file with no negation line does not appear in `git status`, stages as a silent no-op, and dies on the next clone.

**Rule.** Create the `.gitignore` negation in the same commit as the KB file, and verify with `git ls-tree <branch> <path>` — not `git status`, which will happily show an already-tracked-but-ignored file as unchanged. A document that does not survive a clone was never written (see `vision_ledger.md` V5).

---

## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)

Shortcomings of the system observed while dispatching the C1/C2 test-authoring work, recorded as the operator asked. These are properties of the tooling, not of the task.

1. **`Worker spawn` rejects a context-only spawn with `{"error": "worker_name is required for action 'spawn'"}`** even though the tool's own documentation says the name defaults to the default worker. Every spawn needs `worker_name: "default"` spelled out. The error text and the schema description disagree.
2. **`git_write checkout` is unverifiable from its own output.** Previously it returned a plausible success line ("Your branch is up to date with 'origin/...'") while HEAD stayed put, and only a second identical call moved it. This session the first call moved HEAD correctly and printed no output at all. The only reliable check is `git_read rev_parse ref=HEAD abbrev=true` afterwards, every time. Related: `rev_parse` with `abbrev=true` returns the **branch name**, not a short hash (`HEAD` -> `dev`), which reads like a bug until you know.
3. **A merged-and-deleted branch is indistinguishable from data loss on the agent side.** `git_read rev_parse ref=docs/gen60-housekeeping` returned `fatal: ambiguous argument '...': unknown revision or path not in the working tree`, which reads exactly like the branch never existed. The truth was in `git_read log`: PR #311 had merged it (`5eb33d12 Merge pull request #311 ...`). When a ref vanishes, read the log before concluding anything; deletion after merge is normal and the commit is still reachable through the merge commit.
4. **Worker `ApplyEdits` was refused mid-task with reason `token`** ("tool access restricted at the critical context threshold"). The worker called `SummarizeTool` and the same edit then applied cleanly. A context-threshold refusal can look like a permissions failure, and it can arrive in the middle of an edit sequence, so a worker's file may be half-edited when it stops.
5. **`KnowledgeBaseTool` `domain=lessons_learned` resolves to `personal/lessons_learned.md`, not `project/lessons_learned.md`.** The two files genuinely coexist (the tracked generation-scoped one and the large historical one), and the domain name silently picks the personal one. Anything that must survive a clone has to be written to the project file another way.

No stop/pause procedure failed. The standing V1 workaround (park the brief in a `WorkingDocument`, send a 3-5 line query naming the `doc_id`) worked again.

