# Ticket: bug/commit-hook-gate-neutralised-by-agent-git-tool

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, isolation-audit pass (gen-47); line numbers read from the working tree of the linked worktree `ThoughtMachine-dev`, dev `9cd06b34`. Provenance in the original draft (`feat/workspace-panel`, `b23c26b`) was template-contaminated and is withdrawn.
**Severity:** HIGH — revised upward in review from the LOW filed on first pass; the rejected reasoning is kept below rather than deleted. HIGH here is not a claim that a live exploit exists. It is the audit's own standard for a control-integrity finding: a control that **reads as enforcing and is structurally unable to fire**, with *two independent mechanisms* each sufficient on its own to neutralise it. Every code path in `tools/git_info_tool.py` that mentions hooks is written as enforcement — one branch refuses the commit outright when hooks are configured, the other deliberately preserves `core.hooksPath` and documents in-source why it does not pass `--no-verify` — and the net effect on the agent git tool is that no hook can ever run: silently, with no message, in the mode the file itself calls the security boundary.

**Rejected alternative, on the record:** a reviewer argued LOW on runtime-harm grounds — nothing is broken at runtime, no data is lost, no privilege is gained, git behaves as documented. Rejected because runtime harm is the wrong axis for a control-integrity finding. The defect is not that git misbehaves; it is that two mechanisms, each presented as a safety measure, jointly cannot do the thing they are presented as doing, and the only signal that would reveal this is itself gated on a file that is absent.
**Status:** open; verified by read plus two direct filesystem checks.

## Finding

`tools/git_info_tool.py` has two execution backends for git. Both discuss hooks. Neither can run one at this HEAD, and the difference between them is presented in the source as a meaningful safety asymmetry that, in practice, is not observable.

**Host mode** (`_exec_host_raw`, `:859-961`) contains a refusal branch (`:868-878`): if `commit` was requested and `<workspace>/.githooks/pre-commit` exists, it returns exit 1 with "refusing commit: repository hooks are configured (.githooks/pre-commit) but host execution mode neutralizes them (core.hooksPath=/dev/null, --no-verify); hooks cannot be enforced on the host." The branch is *guarded on the existence of the very file that is absent from this tree*, so it can never fire here — and it would be the only branch on either backend that ever causes a hook to matter. Everything else hardens in the opposite direction: `hardened_args` injects `-c core.hooksPath=/dev/null` (`:884-885`) on every call, and `commit` additionally gets `--no-verify` prepended (`:900-903`). So on the host path the neutralisation is unconditional; the refusal exists to make the neutralisation loud *when* a hook is configured, and there is no hook configured.

**Container mode** (`_exec_container_raw`, `:963-...`) is the reassuring half. Its docstring (`:981-988`) says "No --no-verify here: the resource container IS the security boundary, but hooks may only originate from .githooks", and for `commit` it injects `-c core.hooksPath=<container path to workspace>/.githooks` (`:989-996`), with the same reasoning repeated in the trailing comment (`:1049-1052`). That is a genuine restriction — hooks cannot be sourced from outside the workspace — and it is the claim an operator reads as "commits are gated in container mode".

It is not, because `.githooks` does not exist. Direct check on this checkout: `os.path.exists('.githooks') -> False`, `os.path.exists('.githooks/pre-commit') -> False`, and `os.listdir('.githooks')` raises `OSError: [Errno 2] No such file or directory: '.githooks'`. Pointing `core.hooksPath` at a directory that does not exist is not an error in git — it simply finds no hooks and proceeds. So container mode runs zero hooks and prints nothing; host mode runs zero hooks and prints nothing unless a hook file appears, in which case it refuses the commit instead of running it.

Net, on the agent git tool: **there is no configuration of this tree in which a hook executes.** The two mechanisms are independent (unconditional `--no-verify` on one side, a `core.hooksPath` pointing at a missing directory on the other), so this is not a single bug with a single fix — it is the joint behaviour of both backends plus the absent directory. Two things are worth separating:
1. The **asymmetry is honest but inert**: the source presents container mode as preserving hooks and host mode as destroying them, and describes an operator escape hatch ("Use container mode, or remove the configured hooks to accept the bypass") that in container mode does nothing.
2. **The failure is silent in the mode the project prefers.** Container mode is `is the security boundary` per its own comment; a hook that fails to load there should at minimum be reported, and instead it is indistinguishable from a hook that ran and passed.

Scope: the agent git **tool** only. This says nothing about a hook the operator types by hand in their own shell — that is governed by the common-dir `.git/hooks/` and any global `core.hooksPath`, neither of which is readable from this workspace (the checkout is a linked worktree; see `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §1).

## Evidence

Direct checks, run against the workspace root:

```
os.path.exists('.githooks') -> False
os.path.exists('.githooks/pre-commit') -> False
os.path.exists('.pre-commit-config.yaml') -> False
--- listdir .githooks ---
OSError: [Errno 2] No such file or directory: '.githooks'
```

Host backend, `tools/git_info_tool.py` (2028 lines):

- `:868` — `if args and args[0] == "commit":`
- `:869` — `_ws = getattr(self, "_resolved_workspace_path", None)`
- `:870` — `_hook = Path(_ws) / ".githooks" / "pre-commit" if _ws else None`
- `:871` — `if _hook is not None and _hook.is_file():` — the guard that makes the refusal unreachable here.
- `:872-878` — the refusal text, verbatim: `"Error: refusing commit: repository hooks are configured " / "(.githooks/pre-commit) but host execution mode neutralizes " / "them (core.hooksPath=/dev/null, --no-verify); hooks cannot " / "be enforced on the host. Use container mode, or remove the " / "configured hooks to accept the bypass."`
- `:884-885` — `hardened_args = [` / `"-c", "core.hooksPath=/dev/null",`
- `:900-901` — `# commit additionally skips pre-commit/commit-msg hooks via` / `# --no-verify as a second line of defense.`
- `:902-903` — `if args and args[0] == "commit":` / `args = [args[0], "--no-verify"] + args[1:]`

Container backend, `tools/git_info_tool.py`:

- `:989` — `if args and args[0] == "commit":`
- `:990` — `hooks_dir = Path(self._resolved_workspace_path) / ".githooks"`
- `:991-995` — `try:` / `hooks_path = self._to_container_path(hooks_dir)` / `except ValueError:` / `# Defensive fallback: workspace path unresolvable → relative.` / `hooks_path = ".githooks"`
- `:996` — `args = ["-c", f"core.hooksPath={hooks_path}"] + args`
- `:1049-1052` — `# NOTE: no --no-verify here. The resource container IS the security` / `# boundary; hooks are restricted to the workspace .githooks dir via` / `# the core.hooksPath override above (unlike the host path, which` / `# neutralizes hooks entirely).`
- `:981-988` — docstring: "No --no-verify here: the resource container IS the security boundary, but hooks may only originate from .githooks."
- `:993-995` — the `ValueError` fallback sets a **relative** `.githooks`, i.e. relative to the in-container cwd, not the workspace root; a second silent-miss path on the same line group.

## What this is not

- Not a claim that a hook file was deleted recently, or that anyone should restore it. The directory is simply not in the tree at this HEAD.
- Not a claim that CI is ungated. CI is the gate that exists: `.github/workflows/platform-matrix.yml` and the other workflows run pytest on every push/PR.
- Not a claim about the operator's own shell. Hand-typed `git commit` uses the common-dir `.git/hooks/` and any global `core.hooksPath`; both are outside this workspace and unreadable from here.
- Not a duplicate of `ticket-hygiene-git-resource-audit-doc-claims-nonexistent-hook.md`. That ticket is about a document asserting a 279-line hook that is not in the tree; this one is about the code's hook semantics being inert regardless of what the document says.
- Not the same defect as the `--no-verify` decision. Host-mode `--no-verify` is deliberate and documented (`:900-901`); a ticket must not treat a design decision as a bug. What is filed here is that the *contrast* drawn between the two modes (`:1049-1052`) is untrue in this tree.
- Not proven end-to-end. No commit was made and no hook was written to test either backend; the conclusion is from reading the two code paths against a filesystem check.

## Disposition

Decide what container mode should do when `core.hooksPath` points at a directory that does not exist: report it (the mode claims hooks are honoured, so silence is the anomaly), or drop the claim from the docstring and comment so the two backends are honestly described as "hooks never run". Do not "fix" host mode — `--no-verify` there is the documented decision, and the refusal branch at `:868-878` is the honest half of it. If commit-time gating is actually wanted, that is a separate decision with its own risk surface, not a change to make inside a hygiene or bug sweep.

## MUST NOT TOUCH

- `tools/git_info_tool.py:884-885`, `:900-903` — `core.hooksPath=/dev/null` and `--no-verify` on the host path are deliberate hardening; reverting them because a hook "should" run would reintroduce host-side hook execution that nothing in this environment can police.
- `tools/git_info_tool.py:868-878` — the refuse-if-hooks-configured branch. It is the one place that would make the neutralisation visible; weakening it removes the only signal.
- `tools/git_info_tool.py:989-996`, `:1049-1052` — the container-mode `core.hooksPath` restriction. Even with no `.githooks` present, this is what prevents a committed/global hook from executing inside the container.
- `.github/workflows/*` — the real gate; untouched by anything above.
