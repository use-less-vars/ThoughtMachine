# Ticket: hygiene/git-resource-audit-doc-claims-nonexistent-hook

**DO NOT FIX THE DOC IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, on the working tree of `/home/jojo/PycharmProjects/ThoughtMachine-dev` (branch `feat/workspace-panel`, HEAD `b23c26b`).
**Severity:** LOW — no code behaves differently because of this. It is filed because the document is a dated audit that reads as verified observation, and the specific claim is the kind that makes an operator believe commits are gated.
**Status:** open; verified by read plus direct filesystem checks. The doc is 11,780 B, dated 2026-08-15.

## Finding

`docs/git_resource_execution_audit.md` describes a repository-owned hook in forensic detail — `.githooks/pre-commit`, "tracked in workspace, readable", "executable, 11,677 B, 279 lines", with a four-step summary of what it does. The directory does not exist at this HEAD.

Direct check: `os.path.exists('.githooks') -> False`, `os.path.exists('.githooks/pre-commit') -> False`, `os.listdir('.githooks')` raises `OSError: [Errno 2] No such file or directory: '.githooks'`. `os.path.exists('.pre-commit-config.yaml') -> False` too, so at least that half of `:44` remains true.

The claim appears in four lines of the document (`:40`, `:43`, `:44`, `:99`); `:43` is the substantive one and carries the byte count and line count. `:40` and `:99` both repeat "`.githooks/pre-commit` inspected" as a table/inventory entry, so the false premise propagates into the audit's own summary table. `:44` compounds it: its clause "Whether `.githooks` actually fires on commit depends on the operator's host `core.hooksPath` wiring — unverifiable from the sandbox" is a conditional built on a directory that does not exist, so no wiring can make it fire. The honest statement at `:44` would have been "the directory is absent".

This is not a small wording slip that a reader can discount, because the document's whole value is that it distinguishes *what was read* from *what was inferred*. `:40` is headed "NOT READABLE from this environment (gitfile indirection); repo `.githooks/pre-commit` inspected" — i.e. it explicitly claims the repo-owned hook *was* read, and then describes its contents. A file that is not in the tree cannot have been read, and a 279-line description of it cannot be a description of anything.

The most likely origin is a workspace state that has since changed (the repo is a linked worktree; a sibling branch or an earlier checkout could have carried the file), which is precisely why the audit's date matters: a dated claim of 2026-08-15 about repository contents has no standing at HEAD `b23c26b` unless it is re-run. Nothing in the document says it was re-run.

## Evidence

Direct checks, run against the workspace root:

```
os.path.exists('.githooks') -> False
os.path.exists('.githooks/pre-commit') -> False
os.path.exists('.pre-commit-config.yaml') -> False
--- listdir .githooks ---
OSError: [Errno 2] No such file or directory: '.githooks'
```

Every occurrence of `.githooks` in `docs/git_resource_execution_audit.md` (four lines; `:43` is the false claim):

- `:40` — `## (d) Shared \`.git/hooks/pre-commit\` — NOT READABLE from this environment (gitfile indirection); repo \`.githooks/pre-commit\` inspected`
- `:43` — `- Repo-owned hook **\`.githooks/pre-commit\`** (tracked in workspace, readable): executable, 11,677 B, 279 lines. Content = "fast gate": (1) import \`web_ui.backend.server\`; (2) live health check on \`127.0.0.1:<random-high-port>\` via urllib, asserting \`{"status":"ok"}\` and \`revision == git rev-parse HEAD\`; (3) hermetic websocket smoke (starlette \`TestClient\`, temp HOME, patched \`Path.home\`, purged modules, \`new_session(custom)\` → \`session_loaded\`); (4) conditional pytest run of \`tests/integration/test_session_creation_contract.py\` only if steps 1-3 finish < 3 s, with \`--ignore\` for the RED test (exit 5 treated as SKIP). Python resolution \`$PYTHON\` → \`.venv/bin/python\` → \`python3\`; EXIT trap cleanup; \`mktemp -d ${TMPDIR:-/tmp}/tm_precommit.XXXXXX\`; \`setsid\` process-group kill. No \`mcp_servers.json\` creation anywhere.`
- `:44` — `- \`.pre-commit-config.yaml\` is **absent** from the repo (verified via glob). Whether \`.githooks\` actually fires on commit depends on the operator's host \`core.hooksPath\` wiring — unverifiable from the sandbox.` (first clause true; second clause inherits the false premise)
- `:99` — `| (d) Shared \`.git/hooks/pre-commit\` | NOT READABLE from sandbox (gitfile); \`.githooks/pre-commit\` inspected (279 lines); \`.pre-commit-config.yaml\` absent |`

Context: `docs/git_resource_execution_audit.md` is 11,780 B and dated 2026-08-15. `find . -maxdepth 3 -name "*pre-commit*"` yields only a report file (`./reports/2026-08-16_01-31-45_pre-commit_gate_remediation___verified_code_findin.md`), no hook.

## What this is not

- Not a claim that the audit was fabricated. Its other sections (git-command inventory, execution-mode analysis) match the code; this is one line group describing a file that is not in the tree.
- Not a claim about what the agent git tool does with hooks. That is a separate finding: `ticket-bug-commit-hook-gate-neutralised-by-agent-git-tool.md`. Both happen to be true and neither causes the other — with the doc corrected, the hook still would not run.
- Not a duplicate of `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §4, which records the same doc claim in passing as an incidental. That ticket's subject is the venv; this one is filed because a false claim inside an audit document deserves its own record and its own disposition.
- Not an assertion that the missing directory is a defect in itself. A repo with no commit hook is a legitimate configuration. The defect is the document saying otherwise.
- Not a licence to edit the audit's *conclusions*. Only `:40`, `:43`, `:44`, `:99` are implicated.

## Disposition

Either the hook is wanted — then it should exist in the tree, and the audit's description of its behaviour becomes a specification to implement — or it is not, and `:40`, `:43`, `:44`, `:99` should be corrected to record the absence, with a note on when the directory disappeared and why the entry was not re-verified at HEAD.

Not to be done inside this wave, and not to be done by silently deleting the claim: a corrected line that says "`.githooks/` absent at `b23c26b`; previously described in the 2026-08-15 pass" is more useful than a hole, because the next reader will otherwise re-derive the same false belief from the git history of the document itself.

## MUST NOT TOUCH

- Every other section of `docs/git_resource_execution_audit.md`. The document's execution-mode analysis and command inventory are corroborated by the code and are the reason it is worth correcting rather than deleting.
- `scripts/doctor_checks.py:500-560` and the venv bootstrap path — unrelated to this claim, and covered as untouchable in `ticket-hygiene-venv-broken-and-pytest-unreachable.md`.
- Any attempt to *create* `.githooks/pre-commit` to make the document true. That inverts the fix: it would add a heavyweight commit gate to satisfy a stale sentence, and `tools/git_info_tool.py:880-903` has already decided not to run hooks on the host path.
