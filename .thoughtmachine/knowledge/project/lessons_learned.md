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
6. **`npm run test` does not work here:** no summary and exit 124. Run named files instead — `cd web_ui/frontend && TMPDIR=/tmp timeout 150 npx vitest run --maxWorkers=2 <files>`.
7. **`TMPDIR=/tmp` is mandatory.** `/var/tmp` is read-only and the failure surfaces as a misleading `ENOENT` about a file that exists.
8. **A worker's turn is hard-cut at roughly 150–175 s,** and `timeout_seconds` is NOT an upper bound: a 175 s request returned at 154.8 s with `timeout_triggered` set. The cut lands precisely when verification begins, so write every edit first, verify last, and spend a separate cheap turn on verification alone.
9. **A worker that has burned its context is not reusable** — one sat at 84k tokens against an 80k `critical_threshold_tokens` holding nothing but `Respond`/`SummarizeTool`. Re-spawn with `force=True` rather than querying it. Ceilings differ per worker (85k vs 80k); never assume the default.
10. **`git` is absent from the worker sandbox (`host_bash` banned),** so workers cannot inspect history or commit. Git is the orchestrator's job; never put git commands in a worker brief.
11. **`FileSearchTool` over `.` fails on this repo (~42k files).** Always scope a search to a subdirectory.
12. **`FileEditor` under-reports multi-line appends** ("1 line(s)" for a whole section). Verify by re-reading.
13. **`FileSearchTool` can render synthetic bold markers that do not exist in the file,** which breaks a later exact-match edit. Build anchors from a plain read, not from search output.
14. **`git_read cat_file` silently ignores `line_start`/`line_end`** and dumps the file from the top — ~10k tokens on a large source file.
15. **`git_read diff` returns an empty result rather than an error when `file_path` is a list.** Pass one path, or loop per file.
16. **`git_read merge_base` takes its operands as `a` and `b`, not `commit1`/`commit2`,** and reports a non-ancestor as a sentence rather than a failure — read the text, not the exit status.
17. **A worker's context can be summarised mid-turn, destroying a long verbatim payload it was told to transcribe.** Keep payloads short, or split them across turns.

No stop/pause procedure failed. The standing V1 workaround (park the brief in a `WorkingDocument`, send a 3-5 line query naming the `doc_id`) worked again.

---

## Gen 61 — the control plane

### 1. The Stale Prompt Trap

A resumed agent obeyed a `[System Notice: You are paused]` that was three turns old and refused a fresh query, because the notice was still sitting in its history and nothing ever contradicted it. The agent was not confused about what it could do; it had been told, once, and the telling never expired.

**Rule.** Every unpause must inject a counter-notice as explicit as the pause notice was, and both must be written by symmetric code paths. The delivery path matters as much as the text: the pause notice is buffered in `_pending_warnings` and every flush site for that buffer runs *after* the LLM call, so a resume notice put in the same buffer would miss the first post-resume call — exactly the turn the stale notice poisons. Use `Agent.leave_reading_mode()`, the only `clear_restriction()` caller outside `state.py`, and write straight to history.

**Corollary.** An instruction the agent can read is an instruction the agent will keep obeying. Anything injected into history needs a stated lifetime, and something must revoke it.

### 2. The Split-Brain UI

The backend sent a tool call and its result as two independent events with no shared id, and the UI dutifully rendered both — one call, two bubbles, one of them a result with no visible cause. Neither half was wrong; the pairing simply did not exist anywhere in the system.

**Rule.** Every IPC/WebSocket event needs a unique id, so the consumer can upsert idempotently instead of appending blindly. For history written before the id existed, derive a synthetic one from position (`legacy:{msg}:{tc}:{name}`, `legacy:result:{msg}`) — position keeps genuinely repeated identical calls distinct, and a reserved prefix cannot collide with a real id. Never pair a result that has no id: an unpaired row is honest, a wrongly paired row is a lie.

**Corollary.** "The frontend should have known" is not a diagnosis. If the wire format cannot express the relationship, the fix belongs on the wire.

## 2026-11-16 — C76 marker sweep (Gen 69): seven rules, earned over four chunks

### 1. Three units of measure — never interchange them
A trust-harness marker sweep produces three *different* numbers, and every wrong claim this sprint came from treating one as another:
- **AST B sites** — syntactic occurrences of the trust gate in source. A property of the *code*, not of the markers. Marking tests does NOT move this number.
- **Decorator lines** — how many marker decorators were physically added.
- **Marked node ids** — how many *collected test ids* those decorators actually cover.
Measured on the 26-file `tests/` set (integration excluded): 111 sites, 177 decorator lines (45+53+59+20), 195 marked node ids (58+56+59+22). The decorator→node ratio is NOT a constant: 1.29x, 1.06x, 1.00x, 1.10x across four chunks. It can also be below 1.0 in the other direction, when one decorator lands on a `@parametrize` (one decorator / three ids). Never extrapolate a fan-out ratio from chunk N to chunk N+1; measure every chunk.

### 2. The flag-off measurement recipe: <=8 files per batch, redirect, never pipe
- Batch at most ~8 files per pytest invocation (integration tests are far slower: batch ~3-4).
- ALWAYS redirect output to a file under `logs/` in the worktree and read the file afterwards. NEVER pipe pytest into `tail`/`grep`/`head`: the container is hard-capped at 512 MB, piping makes the process die with rc 143/137 and you lose the summary line, which is the only part you needed.
- Containers are ephemeral per tool call, so `/tmp` does not survive between calls. Logs go under the worktree, not `/tmp`.

### 3. Mutate by symbol anchor, verify the sha, restore byte-identically
- Mutate the trust fixture by searching for its **symbol** (a def/name), never by line number or by a remembered short name. Line numbers drift; briefs lie.
- Record `sha256sum` of the file before mutating, after mutating, and after restoring. The restore must be byte-identical, not merely 'looks right'.
- Verify restoration with `git diff <file>` from inside the worktree; an empty diff is the only acceptable proof.
- Practical trap when asserting on fixture source: a `count()` over a Python *list* of lines never matches an indented string — count on `'\n'.join(lines)` instead. And substring-matching a line you want to KEEP is unsafe when it is a substring of a line you want to REMOVE (e.g. `enable_test_harness_trust(True)` inside `auth_boundary.enable_test_harness_trust(True)`); match on the exact stripped line.

### 4. Probe-first: make it fail, read the ids, then mark exactly those
The sequence that worked four times: mutate the trust gate OFF -> run the target file(s) flag-off -> read the actual failing node ids from the report -> apply markers to exactly those tests -> re-run flag-off and require zero failures -> restore and re-run flag-on and require zero failures. Marking before probing guesses at coverage and silently over-marks. A whole-file mark where only some tests are flag-dependent produces *inert* markers: they pass in both directions and therefore prove nothing (chunk 2 left 5 such ids in one file; they are queued for the dead-test burn-down, not celebrated as coverage).

### 5. The closure proof is a summation, not a percentage
The gate that actually convinces: flag-ON `257 passed, 0 failed`; flag-OFF `0 failed, 257 passed`. Both directions equal the *collected* total (257), so no test is left unaccounted for in either direction. State all three numbers (collected, on, off). A pass count alone can be satisfied by tests that skip or vanish.

### 6. Short names in a brief are a hazard, not shorthand
A brief that names files by fragment (`sidecar_clobber_guard`, `grain_projection`, `vocabulary_closure`) will resolve to the wrong file roughly half the time — in one chunk, 7 of 11 short names pointed at the wrong path. Two specific traps:
- `tests/web_ui/backend/...` and `web_ui/backend/tests/...` are transposed twins; both exist, only one is collected.
- `sidecar_clobber_guard` is `test_permission_sidecar_clobber_guard.py`, NOT `test_sidecar_clobber_guard.py`.
Resolve every path before writing anything, and echo the resolved absolute-ish paths back in the report so the reviewer can check them without re-deriving them.

### 7. Tooling gaps observed live (for the engineers)
- `git_write commit` can fail with exit 1 and an EMPTY body when nothing is staged. Always run `git status` after a commit; never trust the return alone.
- `git_read diff` on a CLEAN file returns 'no diff output ... check the path spelling'. It reads like an error but means success; there is no way to distinguish 'no change' from 'no such path'.
- `git_read show_file` requires the parameter named `path`; passing `file_path` errors 'path is required'.
- `git_read show` maps `format` to `--format=`, so `--stat` is rejected (exit 128).
- `git_read ls_tree` ignores the `prefix`/`path` arguments, so a subtree listing returns the whole tree (and ~5k tokens).
- `Worker` results intermittently come back with `status: null` / `confidence: null`. That is NOT a completion signal; verify the artefact on disk.
- The worker container is hard-capped at 512 MB regardless of a larger `mem_limit` in config.
- The worker container has no `git`, so a worker's claim to have run git commands is false by construction.
- Positive note: asking for a 900 s worker timeout is refused and clamped to 600 s, with a clear explanation. That is exemplary behaviour and worth keeping.

## 2026-11-19 — three rules from a documentation ledger (Gen 70)

### 1. The branch is a shared reference; the worktree is the isolation

Every checkout of this repo points `dev` at the same `refs/heads/dev`. Two agents on two branches, one shared checkout, and whichever one runs `git checkout` last decides what the other one is looking at. `724dcc89` fixed the acute form by making the default `worktree add --detach`; C100 records the hole that remains — when the resolver cannot create a worktree it falls back to the shared checkout instead of refusing (`tools/workspace/worktree_defaults.py:170-191`). A fallback that succeeds quietly is worse than a failure that stops you, because the failure is the only signal you were about to share state.

### 2. The key names an owner and the lookup ignores it

A key can exist, be documented, be present in the file — and still be read by nothing on the path that acts on it. Anything that answers "who owns this?" by reading the key is answering a different question than the code answers, and the two answers stay compatible for months because neither is checked against the other. Before trusting an ownership or permission field, find the line that reads it. If you cannot, the field is a comment.

### 3. A measurement whose root is unstated is a rumour

"~40 hardcoded dev/master literals" became **5 occurrences on 4 lines** the moment somebody named the directories being counted (agent 0, scripts 3, tools 2, web_ui/backend 0). "10 required checks", "11 unique contexts", "18 in-tree" are not three estimates of one number — they are three tallies of three different objects, and a sentence that mixes them is wrong whichever digit it picks. A count without its root is not a low-confidence count; it is not a count.
