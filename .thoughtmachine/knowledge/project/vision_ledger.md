# vision_ledger.md — the Vision Ledger (V1–V5)

**What this file is.** Five standing commitments (V1–V5) that the machine is built to keep, each with the evidence that shows whether it is being kept. This is not a roadmap (`v3_roadmap.md`) and not an invariant list (`architecture_invariants.md`): V-items are *properties of the whole system as experienced by its operator*, and every one of them has been violated at least once in a way that was visible only after the fact. It is also not the pending-core backlog — that lives in `core.md` and uses C-IDs. Two files, two namespaces: **V** is a standing commitment the whole machine must keep, **C** is a piece of pending core work.

**Rules for this file.**
- IDs are permanent: V1–V5 are never renumbered, merged or reused. New items take the next free ID.
- One screen per item, no padding: statement → why it matters → how you would know it is broken → current verdict with a commit hash.
- A verdict may be SATISFIED / PARTIAL / BROKEN. Only a measured check changes a verdict; a worker's report or a chat claim is not evidence.
- Append-only. When a verdict changes, add a dated line under the item; do not delete the history.

---

## V1 — Dispatch integrity: the envelope carries what it says it carries

**Statement.** What the orchestrator hands to a worker is exactly what the worker receives, and what the worker returns is exactly what the orchestrator reads. No silent truncation, no dropped fields, no status field that contradicts the payload beside it.

**Why it matters.** Every downstream guarantee is fake if the message bus edits the message. A worker that never saw the second half of its brief still answers confidently, and the orchestrator cannot tell a wrong answer from a truncated question.

**How you would know it is broken.**
- A worker builds something plausible but unrelated to the middle of the brief you sent.
- A worker reports it never received a field you definitely sent.
- A spawn returns `status: timeout` / `timeout_triggered: true` next to content that is complete and correct, or `elapsed_seconds` far below the configured `timeout_seconds`.

**Verdict: BROKEN** (Gen 60/61, 2026-11). `Worker spawn` drops `context["meta"]`. Long inline `query` payloads are truncated — a worker answered a brief it had only half of and hallucinated the rest. A spawn returned `timeout` / `elapsed_seconds: 421.6` on a `timeout_seconds: 600` run whose content was correct. Standing mitigation until this is fixed in code: park every payload longer than a few lines in a `WorkingDocument` and send a short query naming the `doc_id`.

---

## V2 — Process seams are verifiable, not trustworthy

**Statement.** Any claim about state that crosses a process boundary (agent → worker, host → container, orchestrator → vault) must be reproducible from an artifact the reader can inspect: which tree, which layer, which boolean.

**Why it matters.** A worker runs in its own thread with its own context, container and git view. Its report is testimony; the artifact is evidence. Three "the machine is broken" reports in Gen 61 were each a seam lying: a stale restart badge kept claiming a restart was pending; an agent reported that hardening was absent because the working tree was checked out on `dev` and `git show` rendered pre-fix code; a healthy registry reported 46 of its 50 sessions closed.

**How you would know it is broken.**
- A worker's diff does not match `git diff` on the host byte-for-byte.
- A worker cannot commit, cannot reach the vault, or has `host_bash` fail closed, and its brief never accounted for that.
- "It is merged" cannot be answered by `git merge-base --is-ancestor <sha> dev` returning 0.

**Verdict: PARTIAL** (Gen 61). Worker git access is not guaranteed across workers — one ran `git status` / `git diff` and its hunks matched the host byte-for-byte, another exited 127. Worker `host_bash` fails closed (`no usable 'root' in config.json`). The vault is unreachable from workers by four independent paths. All of it is workable only because the orchestrator re-verifies with its own git tools before anything is committed.

---

## V3 — No lying renderer: state and its projection are kept distinct

**Statement.** Every number and every empty state on screen names exactly which subset of state it counts, and "we could not load it" is never rendered as "there is nothing there".

**Why it matters.** The state layer can be perfectly healthy and the machine still be misread. The operator's mental model comes from the renderer, not from the file.

**How you would know it is broken.**
- An open-only count is labelled as a total — "0 sessions" on a workspace with 50 sessions, 4 of them open.
- A failed fetch is collapsed to a falsy default (`tryGet` → `null` → `[]`), so an outage looks like an empty list.
- A refresh failure silently replaces a stale-but-real list with an empty one.

**Verdict: SATISFIED** as of `fbc563d5` (Gen 61). `rebuild_from_disk()` carries `is_open` through the rebuild instead of hardcoding `False`; `total_sessions_count` sits beside the open-only `active_sessions_count`; `tryGetResult()` → `sessionsError` distinguishes "could not load" from "none". The rule outlives the fix: name the subset, keep failure separate from absence.

---

## V4 — Destructive action is reversible or preceded by an inspectable dry run

**Statement.** Nothing that can lose the operator's work runs without either a dry run the operator can read first, or a park that an operator can revive.

**Why it matters.** An agent cannot be trusted with a delete, and neither can a repair command with "apply" in its name. The recovery path has to exist before the destructive path is armed.

**How you would know it is broken.**
- A maintenance operation does something its own report never mentioned — a documented "removes empty directories" that does not, or a clean-up that removes more than it listed.
- "Stop" terminates a worker instead of detaching it, taking its context with it.
- A startup path rebuilds state unconditionally and silently, because a guard that failed closed was removed for convenience.

**Verdict: PARTIAL, and SATISFIED everywhere it is armed.** `Worker stop` detaches — context, containers and queued work are kept — and killing is operator-only. `vault_repair` / `vault_gc` have never been run against the vault from this seat. The three registry guards hold and must not be weakened: `_quarantine` on unparseable session files, `RuntimeError` when a disk scan returns zero entries over a non-empty registry, and no auto-rebuild.

---

## V5 — The machine's memory survives the machine

**Statement.** A file the machine is told to read on entry must be in the repository. Memory that lives only in one checkout is a promise the next seat cannot keep.

**Why it matters.** `MANDATORY_STOP.md` is the first file every new seat is told to read. If it is gitignored, the clone instructs a seat to read a file it does not have — and the same is true of every document a handoff names.

**How you would know it is broken.**
- `.thoughtmachine/knowledge/**` is ignored wholesale, so a new KB file does not appear in `git status` after you create it.
- A `git add` of a new KB file "succeeds" and the file is still absent from `git ls-tree`.
- The tier policy says "the KB is gitignored" and nobody notices that the entry file went with it.

**Verdict: PARTIAL, corrected in this commit** (Gen 61). `project/**/*.md` is ignored wholesale, so tracking is per file via explicit `!` negations. `vision_ledger.md`, `MAIN_ONBOARDING.md` and `lessons_learned.md` join the onboarding-contract negation set in this commit, and `core.md` is deliberately left out of it — publishing the pending-core backlog is a tier ruling, not a housekeeping edit. The standing rule: **add the negation line in the same commit as the file, and verify with `git ls-tree`, never with `git status`.**
