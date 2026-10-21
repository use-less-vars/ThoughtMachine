# Ticket: security/commit-hook-gate-not-installed

**Filed:** 2026-10-21, from the session-persistence pass at `9d5ed869` (found while auditing what a `git commit` is allowed to run; the directory both execution modes resolve hooks from does not exist anywhere in the tree, and nothing in the product creates it).
**Severity:** HIGH — both modes advertise the same gate ("hooks may only originate from `.githooks`"), but container mode points `core.hooksPath` at `<workspace>/.githooks`, which nothing ever creates, and host mode's only guard is a refusal that fires *only if* `.githooks/pre-commit` already exists. Today commits run **zero** hooks in either mode, and `thoughtmachine/security.py`'s stated reason for leaving `.git/hooks` unblocked is that absent directory.
**Status:** open; verified by read + `find`/`grep` at `9d5ed869`, not exercised at runtime. NOT blocking, NOT to be fixed in this wave (the actual gate is `ticket-feat-pre-commit-gate-hook.md`).

## Finding

Container mode, `tools/git_info_tool.py:989-996`:

```python
if args and args[0] == "commit":
    hooks_dir = Path(self._resolved_workspace_path) / ".githooks"
    try:
        hooks_path = self._to_container_path(hooks_dir)
    except ValueError:
        # Defensive fallback: workspace path unresolvable → relative.
        hooks_path = ".githooks"
    args = ["-c", f"core.hooksPath={hooks_path}"] + args
```

and the rationale immediately above it, `tools/git_info_tool.py:981-988`:

```python
# Containerized commits run workspace-local hooks from the policy-owned
# .githooks directory (mounted at /workspace/.githooks). The explicit
# core.hooksPath override uses the container-mapped ABSOLUTE path so a
# nested repository (whose root is not the workspace root) cannot
# resolve the relative ".githooks" to some other directory; repo-local
# .git/hooks is never consulted. No --no-verify here: the resource
# container IS the security boundary, but hooks may only originate
# from .githooks.
```

Host mode, `tools/git_info_tool.py:868-878` — the guard runs only when the hook file exists:

```python
if args and args[0] == "commit":
    _ws = getattr(self, "_resolved_workspace_path", None)
    _hook = Path(_ws) / ".githooks" / "pre-commit" if _ws else None
    if _hook is not None and _hook.is_file():
        return (1, "", (
            "Error: refusing commit: repository hooks are configured "
            "(.githooks/pre-commit) but host execution mode neutralizes "
            "them (core.hooksPath=/dev/null, --no-verify); hooks cannot "
            "be enforced on the host. Use container mode, or remove the "
            "configured hooks to accept the bypass."
        ))
```

and everything past that guard is neutralized unconditionally — `tools/git_info_tool.py:884-899` opens the hardened argv with `"-c", "core.hooksPath=/dev/null"` (`:885`), and `:902-903` does `args = [args[0], "--no-verify"] + args[1:]` for `commit`.

Both branches resolve `.githooks` against `self._resolved_workspace_path`, which is the **user's project root**, not the vault workspace directory: `_resolve_registry_workspace_info` (`tools/git_info_tool.py:1324-1348`) takes `WorkspaceRegistry.get_workspace(ws_id).root_path` and stores it on the instance (`:565-574`). So the gate can only ever fire from a hook the *user* placed in *their* repository. The vault side that does get bootstrapped (`ensure_workspace_dirs`, `thoughtmachine/workspace_capabilities.py:279-307`) is a different directory and is not on this path.

But `.githooks` does not exist:

- `find . -name '.githooks'` → nothing; the only `pre-commit`-ish artefact in the tree is a report under `reports/`.
- `grep -rn githooks --include=*.py` → only `tools/git_info_tool.py:870`, `:982-995`, `:1050`; a docstring mention in `tools/git_write_tool.py:823`; and tests. No `mkdir`, no copy, no install step, no `.gitignore` entry.
- `resources/MANIFEST.json` is the only deploy manifest (`source`/`dest`/`description`, `base: repo_root`, `never_overwrite`, `directories[].condition`; consumed by `bootstrap.py`'s `RESOURCE_MAP`, asserted in `tests/workspace/test_vault_seeding.py:83-93`) and it targets `~/.thoughtmachine/` — deploying a hook through it would land the hook in the vault, i.e. still not at `<project>/.githooks`.

So the security justification is circular. `thoughtmachine/security.py:76-80` declines to block `.git/hooks` on the grounds that commits run ONLY the policy-owned `.githooks` directory and that the host fallback neutralizes hooks entirely. Both halves are true as code paths and vacuous in effect: the policy-owned directory is absent, and host mode only *notices* hooks when the file it is looking for is present.

The audit doc has drifted to match the intent rather than the tree: `docs/git_resource_execution_audit.md:43` describes `.githooks/pre-commit` as "executable, 11,677 B, 279 lines"; no such file exists at `9d5ed869`. (The vault-side hooks it discusses at `:46-50` were genuinely removed — `_run_vault_hooks` is gone and the removal is guarded by `tests/security/test_permission_routing_fix.py:260-291`, `test_vault_pre_commit_hook_never_runs` — so the doc is stale in both directions, and its `:27`/`:28`/`:36` line references no longer point at the code they name.)

Test coverage confirms the gap is invisible: `tests/security/test_git_container_hooks.py:118,187,209` and `tests/security/test_git_execution_mode.py:118-127` assert the *shape* of the argv (`core.hooksPath=<abs>/.githooks`, absence of `--no-verify`, host refusal when a hook file is staged into place). Nothing asserts that the directory a real commit would use has anything in it.

## Why it matters

The stated security boundary — "the resource container IS the security boundary, but hooks may only originate from `.githooks`" — is the load-bearing sentence for not blocking `.git/hooks` (`thoughtmachine/security.py:76-80`). With `.githooks` absent, an operator reading either the code comment or the audit doc will conclude that repository hooks are being honoured under policy control, when in fact nothing runs. That is the failure mode that matters more than a missing feature: a reviewer who trusts the comment will also trust "hooks are neutralized in both modes", which is only true because there is nothing to neutralize.

It also mis-signals the roadmap. This ticket is not "hooks are broken" — it is "the gate was designed and its shell exists, but the payload was never installed, and two documents assert otherwise."

## Disposition options

- **A. Install a hook into the user's project.** Rejected for now: `<project>/.githooks` is user-owned, so the product would be writing executable content into a repository it does not own, and `resources/MANIFEST.json` cannot express that destination. This is also the wrong ticket to decide it in — it is `ticket-feat-pre-commit-gate-hook.md`, and that ticket's job is to *define* what the hook runs.
- **B. Delete the `core.hooksPath` wiring and rely on `.git/hooks`.** Rejected: it re-opens the `.git/hooks` surface that `security.py:76-80` deliberately closed, and it throws away the one place a policy-owned hook could be honoured once it exists.
- **C. Leave the wiring, correct the claims, and make the absence explicit.** *This is the recommendation.* Concretely: (1) amend the `security.py:76-80` rationale so it does not assert a policy-owned hook directory that is not installed — state that repository hooks are neutralized in both modes and that the `.githooks` mount is a *reserved* location, not an active gate; (2) correct `docs/git_resource_execution_audit.md:43` and its stale `:27`/`:28`/`:36` references; (3) add one assertion that makes the reserved-location status visible — a test that asserting `Path(_resolved_workspace_path)/".githooks"` exists is expected to FAIL today, or an explicit `@pytest.mark.xfail`-style marker naming this ticket. Do not add the directory itself here; that is #2's install decision.

Recommendation: **C**, because it converts a false claim into a recorded gap without touching either execution path, and it leaves the install decision in the ticket that owns the gate's content. Cross-reference: `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §4 records the same facts from the hygiene angle and explicitly defers a commit-time gate to a separate ticket — this is that ticket's *converse* (the shell exists, the payload does not); the two should be read together and neither duplicates the other.

## MUST NOT TOUCH

- `tools/git_info_tool.py:884-899` — the hardening pins (`core.hooksPath=/dev/null`, `diff.external=`, `filter.*`, `credential.helper=`, `core.autocrlf`/`core.eol`). This is the host-mode neutralization; removing any pin widens the surface this ticket is about.
- `tools/git_info_tool.py:902-903` — `--no-verify` insertion for `commit`. Second line of host defense.
- `tools/git_info_tool.py:989-996` — the container `core.hooksPath` wiring itself. Option C keeps it byte-identical; correcting the comment at `:981-988` must not change the code.
- `tests/security/test_permission_routing_fix.py:260-291` — guards the *removal* of vault hooks (`test_vault_pre_commit_hook_never_runs`). Do not "restore" vault hooks under cover of this ticket.
- `resources/MANIFEST.json` — the vault deploy manifest. Adding a hook here silently satisfies nothing (wrong destination) and would break `tests/workspace/test_vault_seeding.py:83-93`.
