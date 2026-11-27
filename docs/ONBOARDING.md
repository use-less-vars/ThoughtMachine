# ONBOARDING — read this first

Audience: a new engineer, or a fresh agent session, meeting this machine for the first time.
Purpose: give you the map before the territory, and tell you which of the two of us can do what.

Two ways to read it:

1. This file alone (~11 kB). It is the map. Read it in one sitting.
2. The curated corpus (~100 kB) that this file indexes: `bash scripts/onboard_read.sh > /tmp/tm_onboarding.md`.
   That script cats the orientation documents and `sed`s the load-bearing first pages of the rest, printing byte counts per file and a total to stderr. Read the result before you touch code.

Nothing here is authority. The repo is. Every claim below can be checked, and section 8 tells you how.

---

## 1. What this machine is

> "ThoughtMachine is a capability management platform. An operator grants an AI agent controlled access to computer capabilities. The security layer is the product - grants are enforced structurally, in code, not by prompt." — `README.md:3`

Four layers, and almost every bug is one layer answering for another:

| layer | what it is | where the truth lives |
|---|---|---|
| **Operator** | the human. Grants authority. Merges. Can press the kill switch. | outside the system, by design |
| **Agent session** | the main model loop; holds tools; spawns workers | `tools/`, `agent/` |
| **Worker** | a **separate OS process** with its own interpreter, registry entry and dispatches — not a thread | `tools/workspace/worker_*.py` |
| **Container** | docker sandboxes a worker (or the agent) executes inside; they can outlive the dispatch that made them | `infra/container_manager.py` |

Plus a per-workspace **vault** (`~/.thoughtmachine`): config, KB, session data. The agent may read it and must never write it.

## 2. The one invariant under all the others

**No snapshot of authority may outlive the moment it was true.**

The container envelope is a birth certificate, not a setting — changing it means a different container. A permission read is a measurement, not a cached grant. A `status.json` that says `ready` is a claim about the past until something reconciles it against the OS.

Four fears, in rank order. Work on them in this order:

| # | fear | test it fails if we get it wrong |
|---|---|---|
| F1 | the permissions the operator SEES must not lie | a UI row shows a grant that is not enforced |
| F2 | the agent must never execute on his computer outside a named gate | a tool reaches the host without a gate |
| F3 | the kill switch must be absolute | a process survives 'stopped' |
| F4 | one workspace's authority must never reach another | a session reads another workspace's vault |

## 3. Where we are — MEASURE, do not read this section as state

Every sha in a document rots. This section tells you how to ask, not what the answer was:

```bash
git log -1 --format='%h %ad %s' dev      # what dev is right now
gh pr list --state open                  # what is in flight
git merge-base --is-ancestor <sha> origin/dev && echo IN || echo NOT-IN
ls tests/test_*.py | wc -l               # 452 at the time of writing; find adds ~143 more
```

As of **2026-11-26**, `dev` carried: the worker-model design (`docs/DESIGN_WORKERS.md`), a dispatch expiry that is *visible* rather than only cleaned up (`orphaned`), and a synchronous `query` that registers a recordable job before it blocks. The pause/kill semantics of the worker are **deliberately unfixed** — they are waiting on the rulings listed in section 4.

## 4. Where we are going

The worker lifecycle comes first (security → features → worker lifecycle → MCP/credentials), because every feature sits on it.

Ruled and shipped (S1, S2): an expired dispatch reports `orphaned` with the query id it lost; a blocking query is a job with an id you can look up afterwards.

Open, in the order they block each other:

| # | question | the trade |
|---|---|---|
| D1 | what does *paused* mean for work already running? cooperative / real `SIGSTOP` / two-phase | a frozen process stops heartbeating, so the stale watchdog must learn *suspended ≠ hung* |
| D2 | one pause latch or two? the supervisor's is write-only; only the on-disk fenced command reaches the child | one fact, one owner |
| D3 | `interrupted` needs a terminal meaning (today a job can read `completed` while the work still runs) | cheap fix, changes one path |
| D4 | must a kill be verifiable? (today `killpg` is impossible: no `setsid` at spawn) | verification is the load-bearing half |
| D5 | a durable process ledger: pid, ppid, **process start time**, container ids | reconciled against `/proc` at read time, never trusted alone |
| D6 | one authoritative answer to "what is running?" (today: `list`, `check`, `inspect` — three sources) | the OS is authoritative; the rest decorate |
| D7 | container retention: TTL, reaper, an orphan list in the UI | the machine must not accumulate unnamed 5 G experiments |
| D8 | vocabulary: the worker prompt still says "isolated thread" | the model inherits the wrong mental model, and so do we |

After that: credentials and MCP, where the same invariant reappears as "one secret, one gate, one record".

## 5. What we did recently, and what it cost

| change | what it actually proved | what remained unverified |
|---|---|---|
| `docs/DESIGN_WORKERS.md` | a worker outlives the dispatch; four objects (definition/instance/dispatch/job); verbs with the refusals they owe | several anchors were re-read later and had moved — treat line numbers in any doc as perishable |
| dispatch expiry (`orphaned`) | a killed dispatch stops reading `busy` | one call site and a process-mode success path were verified by reading, not execution |
| blocking query = job (S2) | `job_status` answers during the dispatch and after it | an `interrupted` reply still closes `completed` (D3) |

Two lessons that cost real time and are cheap to reuse:

- **A green on the wrong tree is not a pass.** A suite passing on the subset you chose is not the suite.
- **A merge is verified by ancestry, not by the tool.** `gh pr merge` printing "Merged" did not put the commit in `dev`; `git merge-base --is-ancestor` did.

## 6. What matters to the operator

He is the gate. He merges, pushes, restarts, and rules. His scarcest resource is his own attention, so:

- **D1** write to disk, hand over a path — never a procedure that exists only in chat.
- **D2** no heredocs in a brief or a command block. Write the file, then reference it.
- **D3** GitHub is an interface (`gh pr create --body-file`), not a browser; never hand him a form to fill in by hand.
- **D4** probe the capability, never assume it. If a grant is banned, say so and stop; do not substitute a workaround that merely looks like progress.
- **D5** one authored text, reused verbatim: the commit message and the PR body are written once, to disk.

And on reporting: a skip is never a pass, a measurement without a root (`DISK:`/`REF:`/`TOOL:`) is a rumour, and "fixed" is unverified until the original failing case is re-executed through the channel that failed.

## 7. YOU vs THE ENGINEER — who can do what

This is the matrix that decides whether a session is useful or just busy. It is measured, not aspirational: the agent's session grants on this box are `filesystem: write`, `git: write`, `container: true`, and **banned**: `network`, `host_bash`, `mcp`, `ci`, `credential`, `visual`.

| action | who | why |
|---|---|---|
| read any file, diff, or history in the repo | **you (agent)** | `git_read` + file reads; no shell needed |
| commit, branch, stage | **you (agent)** | `git_write`, gated on the session grant |
| execute code, run pytest, probe a live process | **you, via a worker** | the worker has the full tool surface; you orchestrate |
| author docs, PR bodies, commit messages, reports | **you (agent)** | the expensive judgement, not the typing |
| `git push` | **engineer** | `network` is banned for you |
| open/merge PRs, retarget a base | **engineer** | network + branch protection |
| run host commands (`docker`, `ps`, `kill`) | **engineer** | `host_bash` is banned |
| restart the app / apply a merged change to a live process | **engineer** | only he can |
| change a permission grant | **engineer** | it is the gated thing itself |
| rule on semantics (D1–D8), accept risk | **engineer** | it is his machine and his name on it |

Corollary: when you need a measurement you cannot run, say *which* measurement and *why* it needs him — do not approximate it and do not present an inference as a result.

## 8. How to verify anything in this document

1. Name the layer before trusting a number: code truth, container truth, OS truth, or the report of a body that may be gone.
2. Measure at the moment you depend on it. A ledger, a handoff, a summary and a cached permission are all snapshots.
3. Two instruments beat one. A worker that wrote the code is not the instrument that proves it.
4. Re-execute the original failing case. "It passes now" is not a receipt unless it passes *there*.
5. After any worker turn that ran a shell: `git status`. A killed turn does not stop its container, and a broken redirect can write into the repo.

## 9. Reading order — the rest of the corpus

`scripts/onboard_read.sh` emits the first four in full and excerpts the rest. Beyond it, in the order I would read them:

| file | why |
|---|---|
| `docs/ARCHITECTURE_MACHINE_MODEL.md` | what runs where, and what stops it |
| `docs/CONTRACTS.md` | every invariant named, with what pins it |
| `docs/MACHINE_INVARIANTS_AND_LORE.md` | the hard core, in the operator's own words |
| `docs/design-pause-reading-mode.md` | "Pause is a reading mode, not a halt" — the argument behind D1 |
| `docs/CONTROL_PLANE.md` | pause / stop / timeout as one plane |
| `docs/DESIGN_CREDENTIALS.md`, `docs/DESIGN_RESOURCES.md`, `docs/DESIGN_TURN_LIFECYCLE.md` | the other three strands, same discipline |
| `docs/audit-process-mode-observability.md` | what a parent can know about a child — 323 kB, read by section |
| `.thoughtmachine/knowledge/project/PRIORITIES.md` | filed by seat, generation by generation |
| `.thoughtmachine/knowledge/personal/lessons_learned.md` | the newest entries only; older ones are archived |

## 10. Traps that have already cost this project time

- `docs/*` is a `.gitignore` catch-all. Every public doc needs its own `!docs/<file>` negation or it is silently never versioned. A directory negation is needed too if the file sits in a subdirectory — file negations inside an excluded directory are dead code.
- A worker turn that times out does not stop its container. Reclaim it, or it will run for hours.
- Docker at 512 m will SIGKILL a test that spawns a child process (exit 137). Use ~2 g for those.
- The synchronous `query` wait is the declared budget **plus** a grace period, which is why a 420 s grant reports "no reply within 480.0s".
- `pause_requested()` is per-thread: in the supervisor's wait loop it sees the *main agent's* pause, not the worker's.
- The KB grows faster than it is read. Date every entry you add, or it can never be archived.

---

*Map version 2026-11-26. If a line here contradicts the code, the code wins — and then fix this file.*
