# Ticket: hygiene/prompt-asserted-guards-audit

**Filed:** Gen 47, 2026-10-21, from the F24 global-KB pen-test (this session).
**Severity:** Medium–High — the prompt asserts security guards, at least one of which is false and one of which is unenforced-by-default. Not "docs are stale": an asserted guard that is false is worse than no guard, because reviewers and agents both stop looking (both treat the claim as already-audited).
**Status:** FILED — nothing fixed yet.

## Bearing

Three prompt files ship guard-shaped claims ("never", "cannot", "read-only", "only … allowed"):

- `system_prompt.txt` (repo root, 45 lines) — general/worker-facing prompt.
- `resources/default_system_prompt.txt` (114 lines) — shipped default prompt.
- `resources/engineer_system_prompt.txt` (87 lines) — engineer operating prompt.

Every conformance-critical decision in this codebase leans on these sentences: an agent that reads "you cannot write to the vault" stops checking, and a reviewer who reads "tools will never allow you to go outside the workspace" stops looking for escapes. So each claim was traced to the code that enforces it (or to the code that breaks it). Result below: of 15 distinct claims, 3 hold with a citation, 3 are false, 5 are only partly true, 4 could not be settled in this pass.

## Evidence

Verdicts are only HOLDS / FALSE / PARTIAL / UNVERIFIABLE. `E:` = `resources/engineer_system_prompt.txt`, `D:` = `resources/default_system_prompt.txt`, `S:` = `system_prompt.txt`.

| # | claim (quoted, with path:line) | verdict | enforcing or breaking code |
|---|---|---|---|
| 1 | "You can inspect the vault with `CheckSystem` but **never** write to it." — `resources/engineer_system_prompt.txt:15` | **FALSE** | Broken by `agent/knowledge/global_kb.py:28-30` (`GLOBAL_KB_DIR = ~/.thoughtmachine/global`, `USER_DIR = …/user`), `:146` (`USER_DIR.mkdir(parents=True, exist_ok=True)`), `:166-169` (`get_global_kb_root()`); reached from `tools/knowledge_base.py:250-257` (scope=global resolves `_get_global_kb_root()`), `:757-793` (`create_domain` writes `user/{domain}.md`) and `:794+` (append/update). Gate is only `check_atomic_operation("filesystem:write", …)` in the `elif mode in write_modes:` branch (`tools/knowledge_base.py:~320-330`) — the same grain any workspace write needs; no `vault` ceiling is consulted. Live proof: `~/.thoughtmachine/global/user/pentest_write_probe.md`, written 2026-10-21 02:11 from a live agent context. |
| 2 | "The vault (`~/.thoughtmachine`) is inspectable via `CheckSystem` but never writable." — `resources/engineer_system_prompt.txt:66` | **FALSE** | Same code as #1 (`agent/knowledge/global_kb.py:28-30,146`; `tools/knowledge_base.py:757-793`). Duplicate statement of a false claim. |
| 3 | "The only window out of the workspace is into the **vault** … a read-only/inspectable area … you cannot write to it." — `resources/default_system_prompt.txt:13-16` | **FALSE** | Same as #1 (write half); the "read-only" adjective is refuted by the same `user/` write path. |
| 4 | "A project notebook is available in `.thoughtmachine/knowledge/` (workspace scope) or `~/.thoughtmachine/knowledge/` (global scope)." — `system_prompt.txt:18`, and the same sentence at `resources/default_system_prompt.txt:72-73` | **FALSE** | Real global root is `agent/knowledge/global_kb.py:28` (`~/.thoughtmachine/global`, not `…/knowledge`). Full audit lives in `ticket-bug-knowledge-tool-reports-wrong-path.md`; folded in here only because it is the same defect class (prompt asserts a path the code does not use). Related sites listed there: `system_prompt.txt:18`, `resources/default_system_prompt.txt:72-73`, `resources/global_kb/*.md`, `MANIFEST.json:70`. |
| 5 | "…tools will never allow you to go outside [the workspace]." — `resources/engineer_system_prompt.txt:15`; and "tools will never allow you to go outside the workspace." — `resources/default_system_prompt.txt:11-13` | **PARTIAL** | Holds by *default*, not structurally. `tools/host_bash_tool.py:4` docstring ("runs a shell command on the host machine (outside the Docker …"), `:47 import subprocess`, `:88 required_categories = ["host_bash:allow"]`, `:93-102` grain resolved fail-closed from `effective_permissions['host_bash']`. Default is `banned` (`agent/config/resource_catalog.py:75`, `agent/config/defaults.py:268`), so the claim is true as shipped — but a session granted `host_bash: allow` runs arbitrary host commands. No path/cwd scoping was found in the grep of `tools/host_bash_tool.py` (no `cwd` hits); file not read in full. |
| 6 | "Docker containers only have the workspace mounted." — `resources/engineer_system_prompt.txt:15` | **UNVERIFIABLE** | Not settled in this pass: grep for `volumes=`, `--mount`, `"-v"` across `agent/` + `tools/` returned only `tools/git_info_tool.py:1681` (`git remote -v`). Would be settled by reading the `docker run` argv / mount spec in `tools/docker_code_runner.py`, `tools/worker_container.py`, `tools/container_control.py`. |
| 7 | "Code execution happens in Docker containers; you run on the host." — `resources/engineer_system_prompt.txt:64`; and "All your file operations, git commands, and network calls execute on the host itself." — `resources/default_system_prompt.txt:5-6` | **PARTIAL** | "you run on the host" is true of the agent process; "**all** your … git commands … execute on the host" is false. Counter-evidence from this session: `git_read`/`git_write` returned the trailer `execution_mode: containerized` (and `git_read`'s own docstring documents `containerized | host | unavailable`), i.e. git executes in a container when one is available; `DockerCodeRunner`/`ContainerExecTool` likewise. |
| 8 | "You do **not** have direct file editing, code execution, or search tools." — `resources/engineer_system_prompt.txt:11` | **UNVERIFIABLE** | Would be settled by the engineer agent's tool-registry construction — registration in `tools/__init__.py:213+` plus the engineer/worker role wiring (`agent/controller/`, `agent/core/` role config not read in this pass). |
| 9 | "Workers run in isolated threads with separate context windows. They share no memory with you." — `resources/engineer_system_prompt.txt:62`; "The worker's conversation is isolated." — `:87` | **UNVERIFIABLE** | Would be settled by `agent/core/worker_context.py` (not read this pass). |
| 10 | "`Git` — view history and commit. **You are responsible for all commits**; workers cannot commit." — `resources/engineer_system_prompt.txt:9`; "Only you can commit to Git; workers cannot." — `:65`; "workers can only read Git history." — `resources/default_system_prompt.txt:65-66` | **PARTIAL** | The gate is the *session's* git grain, not the agent's role: `agent/config/models.py:327` maps `"GitWriteTool" → "git_write"`; `agent/config/resource_catalog.py:111-116` folds `git_write`/`git_read` onto the canonical `git` ceiling; `agent/config/workspace_purpose.py:30,47` shows purposes where `git_write` is ask/banned. Enforcement lives in `tools/git_write_tool.py` (fail-closed on the effective session git permission: write / write_on_feature_branch / full, or ask). Whether a *worker* session can carry `git = write` was not settled here — see `ticket-bug-git-write-not-bound-to-filesystem-grain.md`. |
| 11 | "After countdown expires, only SummarizeTool and Respond are allowed." — `system_prompt.txt:16` | **HOLDS** | `agent/core/state.py:392 get_allowed_tools()`, `:398` docstring (`'token': Respond + SummarizeTool allowed`), `:405`; enforced at `agent/core/tool_executor.py:774-777` (`allowed_tools = self.state.get_allowed_tools()` → rejection naming the allowed set). Warning text: `agent/core/state.py:129`. |
| 12 | "Docker is for *me (the agent)* to run code in a sandbox." — `system_prompt.txt:25` | **HOLDS** | `DockerCodeRunner` / `ContainerExecTool` / `tools/worker_container.py` execute agent-requested code inside a container; consistent with #7's containerized git. |
| 13 | "`scope=global` … (with read-only `system/` and writable `user/` domains)." — `system_prompt.txt:18` | **HOLDS** | `agent/knowledge/global_kb.py:28-30` splits `SYSTEM_DIR` / `USER_DIR`; `tools/knowledge_base.py:757-793` forces `category = "user"` for global writes, so the `system/` half of the claim is enforced. (The writable `user/` half is honest — and is exactly why #1/#2/#3 are false.) |
| 14 | "A **default** worker is always available." — `resources/engineer_system_prompt.txt:19` and `:84`; likewise `resources/default_system_prompt.txt:100` | **UNVERIFIABLE** | Believed true by design (`agent/core/tool_executor.py:599` self-gate comment; worker definitions surfaced by `tools/workspace/check_system.py`), but no code path was read that guarantees a default worker exists in every session. Not promoted to HOLDS. |
| 15 | "The only window out of the workspace is into the **vault**" — `resources/engineer_system_prompt.txt:15`, `resources/default_system_prompt.txt:13-16` | **PARTIAL** | The vault is a real window (`CheckSystem`), but it is not the *only* one while #5's `host_bash` grain exists, and it is not read-only (#1). Two independent reasons the "only" quantifier fails. |

**Tally: 3 HOLDS (#11, #12, #13) · 4 FALSE (#1, #2, #3, #4) · 5 PARTIAL (#5, #7, #10, #15, and #7's duplicate in `default`) · 4 UNVERIFIABLE (#6, #8, #9, #14).**

Corollary worth recording: the false vault-write claim is *invisible* to drift. `agent/config/vault_drift.py:167` calls `self._check_unknown_root_files(manifest, report)`; the body at `:616` iterates `self.vault_root.iterdir()` (`:623`) and skips anything that is not a file — `:624 if not entry.is_file(): continue` — then also skips dotfiles/.bak/.tmp at `:628-631`. The scan is depth-1. So `~/.thoughtmachine/global/user/pentest_write_probe.md` is a *directory* away from the root and produced no warning at all. A guard the drift checker cannot see is a guard nobody will notice being violated. (Cross-ref F24(d).)

## Why it matters

The three highest-traffic guard sentences in the shipped prompts are wrong in the same direction: they under-state what the agent can do. An agent that believes it cannot write to the vault will not report a vault write it performs; a reviewer who believes `host_bash` cannot escape the workspace will not ask for the grain; an agent told the global KB lives at `~/.thoughtmachine/knowledge/` writes to — or looks in — a directory that does not exist. And because drift's unknown-root-file check is depth-1 and file-only, none of this shows up as an anomaly.

## Fix sketch

Do not rewrite the prompt. Three mechanical moves:

1. **Fix the FALSE ones.** Delete or correct the vault-is-read-only sentences (`resources/engineer_system_prompt.txt:15,66`; `resources/default_system_prompt.txt:13-16`) and the global-KB path (`system_prompt.txt:18`, `resources/default_system_prompt.txt:72-73`) — path fix tracked in `ticket-bug-knowledge-tool-reports-wrong-path.md`. Either say "the vault is inspectable and the global KB `user/` domain is writable", or close the write path; do not leave a "never" that the code contradicts.
2. **For each HOLDS, pin the citation.** Put the enforcing `path:line` next to the claiming code (as a comment in `agent/core/state.py` / `global_kb.py`) or collect all of them into one `guards.md` that the prompt references by name. A guard claim with no pointer is unfalsifiable on the next read.
3. **UNVERIFIABLE → test, or delete.** `#6` (mount spec), `#8` (engineer tool list), `#9` (worker memory isolation), `#14` (default worker present) each become an assertion in the test suite; if nobody will write the test, remove the claim rather than shipping an audit-shaped sentence.

Companion fix, one line each, in the same PR: teach `agent/config/vault_drift.py:616-631` to recurse into `global/` (or at least to report unknown *directories*), so a vault write is at minimum observable.

## Verification

Repeatable procedure:

1. Re-run the vocabulary grep over all three prompt files and re-derive the table:
   `grep -n -i -E 'never|cannot|read-only|only .*allowed|will not|no access' system_prompt.txt resources/default_system_prompt.txt resources/engineer_system_prompt.txt`
2. For every hit, walk to the enforcing code and record `path:line`. Any row without a citation is UNVERIFIABLE, not HOLDS — that rule is the whole point of the table.
3. Standing rule: a **new** guard claim in prompt text requires a linked test in the same PR. No test, no claim.
4. Drift hook-check test: write one file under `~/.thoughtmachine/global/user/` from a session and assert that drift reports it. Today it does not (`agent/config/vault_drift.py:624` skips directories); the test fails until the depth-1 skip is fixed. Cross-ref F24(d).

## MUST NOT TOUCH

Same list as the container tickets — the shared `MUST NOT TOUCH` list of the container-escape tickets (permissions model, `tools/host_bash_tool.py` grain wiring, `agent/core/tool_executor.py` gate ordering). This ticket is a *prompt-and-drift* ticket: fix the sentences and the drift skip, do not relax or re-plumb the permission system to match the prose.
