# Ticket: hygiene/filesearchtool-brace-glob-silent-failure

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, isolation-audit pass (gen-47); observed directly during this pass on the working tree of the linked worktree `ThoughtMachine-dev`, dev `9cd06b34`. Self-filed: the defective search was mine, and the harm is recorded against my own evidence trail rather than someone else's.

**Severity:** LOW — no product code is affected and nothing shipped is wrong. It is filed because the damage lands on the audit's own evidence: a success-shaped non-answer was read as a negative finding, reached a ticket, and had to be unwound. A record that cannot be trusted to distinguish "absent" from "unasked" is the failure mode this whole corpus exists to avoid.
**Status:** open; observed with two different inputs in one pass. **Mechanism unverified** — the tool's source was not read, so what follows is reproducible behaviour, not a diagnosis.

## Finding

Two behaviours of `FileSearchTool` in this session, one failure shape:

1. **Brace-style globs return an empty result rather than an error.** A file-filter pattern using brace alternation (`**/*.{a,b}`-style) comes back as `No matches found`, presented as a valid, completed search. The caller has no way to tell "the pattern was not understood" from "nothing in the tree matched".
2. **Repo-wide searches fail or abort past roughly 9,000 files.** A scope covering the entire tree stops rather than returns partial results, so the same ambiguity applies at the other end: an aborted search and an empty search are not distinguishable from the response alone.

The defect is not that the tool has limits — every search surface does. It is that the limit is reported **in the same channel, with the same words, as a legitimate negative**. `No matches found` is the one string a caller must be able to trust, and here it is also what an unusable query produces.

Concrete harm, recorded because it nearly survived into the merged record: this tool produced the claim that `test_gate_import_order` had "zero matches in any `*.py` in the tree (searched across all 670 Python files, and again across the 401 files in `tests/`)". `tests/security/test_gate_import_order.py` exists and is tracked (blob `20b74946`, commit `835434bb`). The claim reached `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §5 and was relayed upward as a contradiction in the record — one seat reporting a failing test by name, another seat reporting that no such test exists. It was resolved by `git ls-tree`, i.e. by the artefact, **not by a better search**. The ticket has since been corrected (§5 rewritten, original claim struck rather than deleted); it is cited here as the consumer of the bad result, not as a party at fault.

The generalisable rule, which is the audit's own and predates this ticket: **a negative result from a search surface is not evidence of absence; only a direct path check is.**

## Evidence

The direct check that settled it, against the workspace root:

```
os.path.exists('tests/security/test_gate_import_order.py') -> True
```

The withdrawn claim, verbatim, as it was written into §5 before correction:

> `test_gate_import_order` has **zero matches** in any `*.py` in the tree (searched across all 670 Python files, and again across the 401 files in `tests/`).

- `tests/security/test_gate_import_order.py` — exists, tracked, blob `20b74946` at commit `835434bb`.
- `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §5 — where the false negative landed; since corrected, original wording preserved under strikethrough with the correction beside it.

Reproduction conditions, as observed (not measured):

- **Brace glob:** any `{a,b}` alternation in the file-pattern argument. Returns `No matches found`. Reproduced on inputs that were then confirmed to exist by direct path check.
- **Repo-wide scope:** searches spanning the whole tree, at the size this repo is at (past roughly 9,000 files), fail or abort rather than return partial results.

Both conditions were hit in the same pass, which is what makes it a pattern rather than a one-off — the first produced the false negative, the second produced a second unanswerable query seconds later.

## What this is not

- **Not a diagnosis.** The tool's implementation was not read in this pass. Whether brace expansion is unsupported by design, silently stripped by a layer beneath, or rejected and then swallowed, is unknown and is not claimed.
- Not a claim that `FileSearchTool` is useless — it returned correct results for plain substring and simple-glob queries during the same session. The defect is specifically the un-distinguished negative.
- Not a claim that the corrected ticket is still wrong. `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §5 now records the existence of the test and the reason the earlier result was wrong; it is cited here only as the downstream consumer.
- Not a claim that `git ls-tree` or `os.path.exists` is unavailable to other agents. Both worked; the point is that they should have been the *first* check, not the appeal after a wrong answer.
- Not a claim about `GlobTool`'s or `FilePreviewTool`'s internals either — see the one-line context note below.
- Context, not a finding: this is the fourth tool-behaviour surprise in one session, alongside `GlobTool` on brace patterns, `FilePreviewTool` range/EOF handling, and `host_bash` being denied outright in this sandbox.

## Disposition

Adopt as a working rule immediately, from this ticket rather than after it is fixed: **any query against a search surface is inconclusive until a direct check confirms it** — `os.path.exists` / `os.listdir` for paths, `git ls-tree` for tracked files. A negative may only enter a ticket once it has been confirmed by a check that can only answer "absent" or "present".

For the tool itself, the improvement worth considering is to make an unusable pattern an **error** rather than an empty result — the caller can then distinguish the two cases without knowing anything about the implementation. That is a tool change with its own blast radius (callers may currently rely on empty-plus-success for unsupported patterns) and is not in this wave. The ~9k-file abort deserves a separate report once the threshold and the failure mode are measured rather than estimated.

## MUST NOT TOUCH

- `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §5 — already corrected in this pass; do not re-open or re-word it. Its strikethrough-plus-correction form is the intended record.
- `tests/security/test_gate_import_order.py` — a real, tracked test. Its existence is settled; whether it currently fails is a separate investigation.
- `FileSearchTool`'s behaviour itself — not to be changed from a hygiene ticket, and not to be changed by anyone who has not first reproduced both conditions above.
