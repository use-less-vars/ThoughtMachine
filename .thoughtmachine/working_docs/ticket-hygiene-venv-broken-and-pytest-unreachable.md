# Ticket: hygiene/venv-broken-and-pytest-unreachable

**Filed:** 2026-10-21, from the environment/hygiene pass at `9d5ed869`, re-verifying three operator-reported defects on the local checkout `/home/jojo/PycharmProjects/ThoughtMachine-dev`.
**Severity:** LOW — **downgraded at filing time.** The report arrived as P1 ("venv is broken, pytest is unreachable, nothing gates commits"). Every headline claim either fails verification outright or cannot be checked by the stated method. What survives is one narrow behavioural truth (no commit-time pytest through the *agent's* git path) plus a stale doc.
**Status:** open; NOT blocking, NOT to be fixed in this wave.

## Finding — the verification result, in the order it matters

### 1. The stated verification method cannot work on this checkout (this invalidates claims 3 and 4 before anything else)

`/home/jojo/PycharmProjects/ThoughtMachine-dev` is **a linked git worktree, not a repository root.** `.git` is an 84-byte *file* whose entire content is:

```
gitdir: /home/jojo/PycharmProjects/ThoughtMachine/.git/worktrees/ThoughtMachine-dev
```

Consequences, each observed rather than inferred:

* `.git/hooks/` **does not exist here** — directory listing returns `Directory '.git/hooks' does not exist`.
* `.git/config` **does not exist here** — read returns `File not found` (and the preview layer refuses `.git/config` as a blocked prefix regardless).
* The sandbox cannot reach outside the workspace to read the real ones: directory listing outside the workspace is refused (`outside workspace`), reads are refused (`escapes the workspace directory`), and the execution container has no `/home/jojo` at all (`ls /home` → `agent` only).

So for this checkout, hooks live in the **common** dir `/home/jojo/PycharmProjects/ThoughtMachine/.git/hooks/` and config in `/home/jojo/PycharmProjects/ThoughtMachine/.git/config`, both **unreadable from inside this workspace by design**. The instruction "read `.git/hooks` and `.git/config` to check the pre-commit hook" therefore **cannot produce an answer here** — not because the hooks are missing, but because those paths are the wrong paths for a worktree. Claims 3 and 4 are **NOT CONFIRMED and not confirmable at this HEAD from here.**

### 2. "The venv is broken / has no site-packages / gives no isolation" — REFUTED

`.venv/` exists and is populated:

* `.venv/pyvenv.cfg` (190 B): `home = /usr/bin`, `include-system-site-packages = false`, `version = 3.12.3`, `executable = /usr/bin/python3.12`, `command = /usr/bin/python3 -m venv /home/jojo/PycharmProjects/ThoughtMachine-dev/.venv`.
* `.venv/bin/` = 30 files / 23 MB, including `python`, `python3`, `python3.12` (7.6 MB, 2026-08-31), `pytest` and `py.test` (278 B, 2026-10-14), `pip`, `uvicorn`, `fastapi`, `mcp`, `httpx`.
* `.venv/lib/python3.12/site-packages/` **is populated**: `_cffi_backend.cpython-312-x86_64-linux-gnu.so` (348,856 B), `py.py`, `pytest_timeout.py` (19,081 B), `six.py`, `sseclient.py`, `typing_extensions.py` (165,012 B).

The single clause of the claim that is literally true is that `.venv/bin/python3` is a symlink to `/usr/bin/python3` (`ls -l`: `python -> python3`, `python3 -> /usr/bin/python3`). That is **standard `python3 -m venv` behaviour on Linux, not a defect**: the interpreter binary is linked, while `sys.prefix` is relocated and `include-system-site-packages = false` keeps the environment isolated. Reporting it as "the venv just points at the system python" misreads the mechanism.

The repository's own health check agrees the venv is good: `.venv/.tm-requirements.stamp` contains `eccd8acb184f6494e4dd536c62e2db138d3e15f925c1ad046051deb95ebe40c7`, and `sha256sum requirements.txt` returns the identical value, which is exactly the condition `scripts/doctor_checks.py:538-539` treats as "virtualenv .venv ready; dependencies up to date (stamp matches requirements.txt)". A broken venv would have been recreated by `scripts/doctor_checks.py:500-560` (`ensure_venv`, self-healing: `_create_venv` at `:484-497` deletes and re-runs `python3 -m venv`), so "broken and left broken" also contradicts the documented tool's own logic.

### 3. "pytest is unreachable" — REFUTED for the interpreter the docs tell you to use

* pytest is a **first-class runtime requirement**, not a dev extra: `requirements.txt:26-27` = `pytest>=8.0.0`, `pytest-timeout>=2.0.0`. (`requirements-dev.txt` only adds `playwright>=1.40` and `pytest-playwright>=0.4` on top of `-r requirements.txt`.)
* `.venv/bin/pytest` (and `py.test`) exist and are regular executables, with `pytest_timeout.py` beside them.
* The docs *contractually* produce this venv: `docs/installation_guide.md:69-70` "[4/5] venv — critical. Runs `doctor --ensure-venv`, which creates `.venv` and `pip install -r requirements.txt`"; `:133` (Windows) "Creates `.venv`, runs `pip install -r requirements.txt`"; `:218` "re-runs all 5 checks; creates .venv if missing". Implementation: `scripts/doctor_checks.py:500-560` (`ensure_venv(path=".venv", requirements="requirements.txt")`), `:484-497` (`_create_venv`), `:329-337` (`check_venv`), CLI flag `--ensure-venv` at `:770-771` and dispatch at `:815-818`. `.gitignore:9` ignores `.venv/`, so this is a bootstrap artefact by design.
* CI treats the venv as *the* way to get pytest: `.github/workflows/platform-matrix.yml:164,:211` `source .venv/bin/activate`, then `:81,:122,:212,:251,:290` `python -m pytest`.

The likely origin of the claim: invoking bare **system** `python3` (not `.venv/bin/python`) hits a `ModuleNotFoundError: pytest`, because isolation is working exactly as configured (`include-system-site-packages = false`). That is not a defect and the fix is the documented one-liner, not a code change. It also cannot be verified from here — there is no host shell, and the container's python is not the host's python — so it is recorded as a probable cause, not a fact.

### 4. "A pre-commit hook runs pytest on every commit" — the hook does not exist in the tree, and the doc that says it does is stale

`ls -la .githooks` → **No such file or directory**, and `find . -maxdepth 3 -name "*pre-commit*"` finds only a report: `./reports/2026-08-16_01-31-45_pre-commit_gate_remediation___verified_code_findin.md`.

But `docs/git_resource_execution_audit.md:43` states the opposite in detail — a repo-owned `.githooks/pre-commit` "tracked in workspace, readable: executable, 11,677 B, 279 lines" — with `:44` adding that `.pre-commit-config.yaml` is absent. **The doc describes a 279-line tracked file that does not exist at this HEAD.** That is the real hygiene defect in this area, and it is worth a line of its own: it is exactly the kind of statement that makes an operator believe commits are gated.

What the code actually does (this part is solid):

* `tools/git_info_tool.py:981-996` — **container-mode** commits inject `-c core.hooksPath=<abs workspace>/.githooks`, and `:1049-1052` confirm no `--no-verify` is added there. With `.githooks` absent, that path resolves to nothing and no hook runs.
* `tools/git_info_tool.py:868-878` — **host mode** *refuses* the commit outright when `.githooks/pre-commit` exists ("hooks are configured … but host execution mode neutralizes them"). Since the file is absent, this refusal branch cannot fire either.
* `tools/git_info_tool.py:880-903` — host mode hardens every call with `-c core.hooksPath=/dev/null` plus `--no-verify` on commit.

**Honest conclusion: commits made through the agent's git tool cannot run pytest at this HEAD — in either execution mode.** That is a much narrower statement than "no commit is gated", and it says nothing about a hook the operator types by hand in their own shell, which would be governed by the common-dir `.git/hooks/` and any global `core.hooksPath` — both unreadable here (§1). The gating that demonstrably exists is CI (`.github/workflows/platform-matrix.yml`, `python-version-matrix.yml`, `first_start_ci.yml`, `cross-platform-smoke.yml`).

### 5. Incidental: the failing test named in the report does not exist at this HEAD

`test_gate_import_order` has **zero matches** in any `*.py` in the tree (searched across all 670 Python files, and again across the 401 files in `tests/`). Whatever produced "1 failed, 81 passed" with that test name is not reproducible by name at `9d5ed869`; it may be renamed, deleted, or a recollection from a different tree. Recorded so nobody spends an hour looking for it.

## Why it matters

Three quarters of the report is an artefact of *where the checks were run from*, not of the repository's state. Left unrecorded, the failure mode is predictable and expensive:

* someone "fixes" a healthy venv (recreating it, or worse, editing the documented `--ensure-venv` path because it "must be broken"),
* someone adds a heavyweight pre-commit hook to satisfy a gate that the code has already decided not to run (host mode passes `--no-verify` by design at `:880-903`),
* and the one genuinely misleading artefact — `docs/git_resource_execution_audit.md:43` claiming a 279-line hook file that is not in the tree — stays in place to mislead the next reader.

The narrow truth worth keeping is the §4 conclusion: **no commit-time pytest exists on the agent git path; CI is the gate.** That is a design statement someone should either ratify or explicitly fix, not a bug discovered by accident.

## Disposition options

- **A. Leave it; treat the report as operator-local cosmetics.** Nothing here is broken at `9d5ed869`. Cheapest, but leaves `docs/git_resource_execution_audit.md:43` lying about a tracked file.
- **B. Run the documented self-heal if and when a real venv defect appears**, not pre-emptively: `python3 scripts/doctor_checks.py --ensure-venv` (`scripts/doctor_checks.py:500-560`, `:770-771`, `:815-818`). This is the supported repair path and needs no code change. *This is the recommendation* — but note it is a no-op here, since the stamp already matches (`:538-539`).
- **C. Fix the stale doc claim** at `docs/git_resource_execution_audit.md:43` (and `:44`): either the file exists → it should be in the tree; or it does not → the audit entry is describing a workspace state that no longer holds. Cheapest genuine improvement in this ticket, and independent of A or B.
- **D. If commit-time gating is actually wanted**, that is a decision, not a hygiene fix: today `tools/git_info_tool.py:880-903` deliberately neutralizes hooks in host mode. Changing it is a separate ticket with its own risk surface.

Recommendation: **C**, optionally with **A**. Do **not** do B "just in case" and do **not** do D inside a hygiene wave.

## MUST NOT TOUCH

- `scripts/doctor_checks.py:500-560` / `:484-497` (`ensure_venv` / `_create_venv`) — the documented, self-healing bootstrap path; it is working and its stamp semantics (`:538-539`) are the health contract.
- `.venv/pyvenv.cfg` — `include-system-site-packages = false` and the `python3 → /usr/bin/python3` interpreter link are correct venv mechanics, not defects.
- `requirements.txt:26-27` — `pytest` and `pytest-timeout` are intentional runtime requirements; do not demote them to a dev extra to "explain" an unreachable-pytest report.
- `tools/git_info_tool.py:868-878`, `:880-903`, `:981-996` — the host-mode hook refusal and the `--no-verify` / `core.hooksPath=/dev/null` hardening are deliberate asymmetry with documented reasoning; not to be "fixed" from a hygiene ticket.
- The CI workflows — they are the actual gate (`.github/workflows/platform-matrix.yml:81,:122,:212,:251,:290`).
