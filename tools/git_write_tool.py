# tools/git_write_tool.py
import json
import re
import logging
import subprocess
from typing import Any, ClassVar, Literal, Optional, List, Union
from pathlib import Path
from pydantic import Field
from .git_info_tool import GitReadTool, resolve_git_execution_mode
from agent.config.defaults import ALLOWED_GIT_PROTOCOLS

logger = logging.getLogger(__name__)

# Branch-name allowlist used by _validate_branch_name (letters, digits, dots,
# slashes, underscores, hyphens only).
_BRANCH_NAME_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')

# Detached-HEAD refusal. ``git rev-parse --abbrev-ref HEAD`` reports the
# literal string "HEAD" when HEAD is detached. "HEAD" is a *valid* ref and is
# NOT in ``_PROTECTED_BRANCHES``, so the commit gates below would otherwise
# treat a detached HEAD as an unprotected branch and permit the commit
# (fail-OPEN). A detached HEAD is not on a branch: both commit gates refuse.
_DETACHED_HEAD_ERROR = (
    "Error: refusing to commit: the workspace HEAD is detached "
    "(not on a branch); check out a branch before committing"
)


class GitWriteTool(GitReadTool):
    """
    Git write operations tool (commit, init, clone, branch_create, checkout,
    stage, unstage).
    Every write is gated (fail closed) on the session git permission
    (``session_permissions['git']`` / the effective ``git`` grain) being
    write-capable (``write``, ``full`` or ``write_on_feature_branch``) or
    ``ask`` (the outer ask gate having run), and on the agent's
    ask policy enforced by the ToolExecutor / security gate. The
    read surface (status, diff, diff_cached, log, branch, branch_list, show,
    remote, blame, config) lives in ``GitReadTool`` (tools/git_info_tool.py);
    this subclass inherits the hardened execution backends, path validation,
    the operator-managed-worktree detection and the per-call execution-mode
    trailer, and adds the write dispatch.
    Parameters:
        working_dir: repository root (defaults to workspace root).
        file_path: single path or list of paths, used by stage, unstage and
            commit (selective commit; always required, never ``-A``).
        branch: branch name for branch_create and checkout.
        message: commit message (required for commit).
        clone_url / clone_target: remote URL and target directory for clone.
    Explicit surface: no raw git flags are accepted from the agent. Every
    invocation is assembled from fixed argv lists and hardened internally;
    --no-verify, -c/--config/core.hooksPath, credential/filter/textconv
    configuration and hooks are never taken from agent input (the execution
    backends inject their own hardening flags). Execution mode and failure
    diagnostics are reported per call for EVERY operation via two trailing
    lines: ``execution_mode: <mode>`` (containerized | host | unavailable)
    and ``failure_reason: <reason>`` (why a containerized resource could not
    be used, or ``none``). Argument-validation errors keep their historical
    byte-exact form (no trailer).
    """

    # Stable tool identifier: used in LLM schemas, preset lists and the
    # /api/tools endpoint. The internal ``tool`` Literal field below is
    # excluded from the LLM schema and kept unchanged for backward
    # compatibility with legacy callers.
    name: ClassVar[str] = "git_write"

    # Branches on which a ``write_on_feature_branch`` session may never
    # commit. Shared by the commit-time feature-branch restriction and the
    # operator-managed-worktree agent-commit narrow allow, so the protected
    # set stays in one place.
    _PROTECTED_BRANCHES: ClassVar[tuple] = ("dev", "master", "main")

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return dynamic permission categories based on the git operation.
        Every write operation requires ``git:write``; ``clone`` additionally
        needs network egress to reach the remote.
        """
        if params:
            op = params.get("operation", "")
            if op == "clone":
                return ["git:write", "network:outbound"]
        return ["git:write"]

    tool: Literal["GitWriteTool"] = "GitWriteTool"

    operation: Literal[
        "commit", "init", "clone", "branch_create", "checkout", "stage",
        "unstage", "worktree_add", "worktree_remove", "stash_push",
        "stash_pop",
    ] = Field(
        description="Git write operation to perform: commit, init, clone, "
        "branch_create, checkout, stage, unstage, worktree_add, "
        "worktree_remove, stash_push, stash_pop"
    )

    force: bool = Field(
        default=False,
        description="Force a destructive worktree operation. worktree_remove "
        "refuses a locked worktree or one with local changes unless this is "
        "true (maps to git worktree remove --force)."
    )

    paths: Optional[List[str]] = Field(
        default=None,
        description="Optional path list for stash_push: only the named "
        "pathspecs are stashed (git stash push -m <msg> -- <paths>). When "
        "omitted, git's default tracked-file stash is used (never -a/-u)."
    )

    index: int = Field(
        default=0,
        description="Stash index for stash_pop (git stash pop stash@{index}). "
        "Must be a non-negative integer; default 0 is the most recent stash."
    )

    base: Optional[str] = Field(
        default=None,
        description="Base ref for branch_create. The ref is resolved to an "
        "immutable commit SHA (git rev-parse --verify <base>^{commit}) before "
        "the branch is created. None (default) pins the branch to the "
        "WORKSPACE checkout HEAD -- never the gitdir HEAD."
    )

    def _flag_gate_error(self) -> str:
        """Return the git permission denial message (fail-closed gate)."""
        return 'Error: git:write denied: session git_write permission is not "write"'

    def _git_write_allowed(self) -> bool:
        """Fail-closed check that this write call may proceed.

        True when any of the following hold:
        - the effective permissions carry a ``git`` level of ``write``,
          ``full`` or ``write_on_feature_branch``;
        - the effective ``git`` level is ``ask`` (the outer ToolExecutor
          gate already prompted and approved this call);
        - the session_permissions dict explicitly sets ``git`` to ``write``,
          ``full`` or ``write_on_feature_branch`` (direct-call
          defense-in-depth).

        A ``write_on_feature_branch`` grant passes this gate (the outer
        category gate admits it too); the feature-branch-only restriction
        is enforced separately at commit time (see
        ``_git_write_restricted_to_feature_branch``).
        """
        effective = self.effective_permissions or {}
        if effective:
            gw = effective.get("git")
            if gw in ("write", "full", "write_on_feature_branch"):
                return True
            if gw == "ask":
                return True
        sp = (getattr(self, "agent_config", None) or {}).get("session_permissions") or {}
        if isinstance(sp, dict) and sp.get("git") in (
            "write", "full", "write_on_feature_branch",
        ):
            return True
        return False

    def _git_write_restricted_to_feature_branch(self) -> bool:
        """True when this write call is governed by the
        ``write_on_feature_branch`` grant (the effective ``git`` level when
        present, else the session_permissions dict for direct callers).

        ``write`` / ``full`` / ``ask`` grants are never branch-restricted by
        this tool: an effective ``git`` level that is present but not
        ``write_on_feature_branch`` is authoritative and returns False.
        """
        effective = self.effective_permissions or {}
        if effective:
            gw = effective.get("git")
            if gw == "write_on_feature_branch":
                return True
            if gw is not None:
                return False
        sp = (getattr(self, "agent_config", None) or {}).get("session_permissions") or {}
        if isinstance(sp, dict):
            return sp.get("git") == "write_on_feature_branch"
        return False

    def execute(self) -> str:
        # Reset per-call runtime state (tool instances may be reused).
        self._resource_manager = None
        self._resolved_workspace_path = None
        self._resolved_workspace_id = None
        self._last_execution_mode = None
        self._last_failure_reason = None

        # git permission gate (fail closed): every write requires a
        # write-capable session git permission (effective or session_permissions).
        # Also enforced at the top of each _git_* write method as
        # defense-in-depth for direct callers.
        if not self._git_write_allowed():
            return self._flag_gate_error()

        # Atomic permission re-check for network operations. An 'ask' level
        # is NOT re-checked here: it defers to the ToolExecutor's outer gate,
        # which already prompted the user and approved this call, so effective
        # permissions still read 'ask'. Missing/banned/False stay fail-closed
        # (the atomic check runs and denies).
        operation = self.operation
        network_ops = {"clone"}
        if operation in network_ops:
            effective = self.effective_permissions or {}
            if effective.get("network") != "ask":
                from security.security_gate import check_atomic_operation
                if not check_atomic_operation(
                    "network:outbound",
                    effective,
                    "GitWriteTool",
                    f"{operation} on remote"
                ):
                    return json.dumps({"error": f"Atomic permission check failed: network:outbound required for {operation}"})
            # Protocol allowlist pre-validation before any git subprocess can
            # run (defense-in-depth; _git_clone re-validates).
            if operation == "clone":
                if not self.clone_url:
                    return "Error: clone_url is required for clone operation"
                try:
                    self._validate_clone_url(self.clone_url)
                except ValueError as e:
                    return self._truncate_output(f"Error: {e}")
        try:
            # Determine working directory
            if self.working_dir:
                # Validate working_dir is within workspace. The workspace's
                # own container mount (/workspace and below) is accepted (see
                # _normalise_working_dir); genuine violations are still
                # rejected here with their byte-exact message.
                try:
                    validated_working_dir = self._validate_path(
                        self._normalise_working_dir(self.working_dir)
                    )
                except ValueError as e:
                    return self._truncate_output(f"Error: {e}")
                repo_root = Path(validated_working_dir).expanduser().resolve()
                # Resolve the registry workspace (if any) so container-backed
                # git execution can map host paths to /workspace. Only
                # registry-resolved workspaces enable container mode; the
                # deprecated workspace_path fallback and direct test callers
                # keep the legacy host execution path.
                ws_id, ws_path = self._resolve_registry_workspace_info()
                if ws_path:
                    self._resolved_workspace_id = ws_id
                    self._resolved_workspace_path = ws_path
            elif getattr(self, 'session_id', None) or getattr(self, 'workspace_path', None):
                # === Resolve workspace path from registries (primary) ===
                ws_id, ws_path = self._resolve_registry_workspace_info()
                if ws_path:
                    self._resolved_workspace_id = ws_id
                    self._resolved_workspace_path = ws_path
                # Fallback to deprecated AgentConfig.workspace_path
                if not ws_path:
                    ws_path = getattr(self, 'workspace_path', None)
                    if ws_path:
                        logging.warning(
                            "GitWriteTool falling back to deprecated AgentConfig.workspace_path")
                if ws_path:
                    repo_root = Path(ws_path).expanduser().resolve()
                else:
                    repo_root = Path.cwd()
            else:
                repo_root = Path.cwd()
            # Security: the git repository root must stay inside the
            # workspace.
            try:
                repo_root = self._validate_repo_root(repo_root)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            # init and clone target a directory that is not yet a git
            # repository, so they dispatch before _git_repo_root validation.
            if operation == "init":
                return self._git_init(repo_root)
            if operation == "clone":
                return self._git_clone(repo_root)
            # Validate git repository. _git_repo_root() re-points repo_root to
            # the actual repository root (via `git rev-parse
            # --show-toplevel`) when <repo_root>/.git is absent; in container
            # mode the returned /workspace path is reverse-mapped to the host
            # path before re-validation.
            try:
                resolved_root = self._git_repo_root(repo_root)
            except (subprocess.TimeoutExpired, TimeoutError) as e:
                # A hung git binary is an availability failure, not a
                # "not a repository" condition.
                return self._truncate_output(f"git executable not available (timed out): {e}")
            except FileNotFoundError as e:
                # GitUnavailableError (raised by _git_repo_root when the git
                # binary is missing or unspawnable) subclasses
                # FileNotFoundError, so it lands here and is reported as an
                # availability failure rather than as "not a git repository".
                return self._truncate_output(f"git executable not available: {e}")
            if resolved_root is None:
                return self._truncate_output(f"Not a git repository: {repo_root}")
            repo_root = resolved_root
            # The resolved root may sit ABOVE the workspace (a repo
            # that contains the workspace); reject it before any git
            # operation runs against it.
            try:
                repo_root = self._validate_repo_root(repo_root)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            # Surface the effective git execution mode (containerized vs
            # host fallback) for diagnostics; log it when determined and
            # again whenever it changes across calls on a reused instance.
            mode = resolve_git_execution_mode(
                getattr(self, "agent_config", None),
                self._workspace_metadata(),
                self._resolved_workspace_path,
                self._resolved_workspace_id,
            )
            if mode != getattr(self, "_last_mode", None):
                logger.info(
                    "GitWriteTool git execution mode: %s (operation=%s, workspace_id=%s)",
                    mode,
                    self.operation,
                    self._resolved_workspace_id or "none",
                )
                self._last_mode = mode
            # Record the effective execution mode for per-call reporting
            # (surfaced via _with_mode() on the new operation outputs).
            self._last_execution_mode = mode
            # Execute operation
            if self.operation == "commit":
                return self._git_commit(repo_root)
            elif self.operation == "branch_create":
                return self._git_branch_create(repo_root)
            elif self.operation == "checkout":
                return self._git_checkout(repo_root)
            elif self.operation == "stage":
                return self._git_stage(repo_root)
            elif self.operation == "unstage":
                return self._git_unstage(repo_root)
            elif self.operation == "worktree_add":
                return self._git_worktree_add(repo_root)
            elif self.operation == "worktree_remove":
                return self._git_worktree_remove(repo_root)
            elif self.operation == "stash_push":
                return self._git_stash_push(repo_root)
            elif self.operation == "stash_pop":
                return self._git_stash_pop(repo_root)
            else:
                return self._truncate_output(f"Unknown operation: {self.operation}")
        except Exception as e:
            if isinstance(e, (RuntimeError, PermissionError)):
                # Hard security errors (permission denials) are re-raised
                # instead of swallowing into a generic string.
                raise
            return self._truncate_output(f"Error executing git operation: {e}")

    def _unprotected_branch_agent_commit_allowed(self, repo_root: Path) -> bool:
        """Narrow allow for agent commits in operator-managed worktrees.
        An agent commit is permitted in an operator-managed worktree only
        when ALL of the following hold:
        1. The session git permission is write-capable (``_git_write_allowed()``).
        2. Container git execution is active (``_use_container_mode()``).
        3. The branch check runs through the normal execution-mode dispatch
           (no silent host fallback exists any more): a container-mode call
           whose git resource is unavailable/degraded raises, so the hardened
           host backend (which injects ``--no-verify`` /
           ``core.hooksPath=/dev/null``) can never silently bypass the gate.
        4. The current branch is NOT a protected branch (``dev``, ``master``,
           ``main``); every other branch (feat/*, fix/*, refactor/*, chore/*,
           docs/*, ...) is allowed.
        Any violation returns False so the caller keeps the existing
        operator-managed-worktree block.
        """
        # Clear any refusal reason left over from a previous call (tool
        # instances may be reused); it is set only when THIS call detects a
        # detached HEAD, so the caller can surface a distinguishable error.
        self._agent_commit_refusal_reason = None
        if not self._git_write_allowed():
            return False
        if not self._use_container_mode():
            return False
        try:
            output = self._run_git(
                repo_root,
                ["rev-parse", "--abbrev-ref", "HEAD"],
            )
        except (RuntimeError, PermissionError):
            # Container-mandatory branch resolution failed (container
            # unavailable, policy denial): fail closed, never degrade.
            return False
        branch = (output or "").strip()
        if branch == "HEAD":
            # Detached HEAD: rev-parse --abbrev-ref HEAD reports the literal
            # string "HEAD" -- a valid ref that is NOT protected, so the
            # checks below would permit the commit (fail-OPEN). Refuse, and
            # record the reason so the commit entry point can surface it.
            self._agent_commit_refusal_reason = _DETACHED_HEAD_ERROR
            return False
        if not self._is_valid_branch_ref(branch):
            # Invalid branch output (empty / multi-line / error-shaped /
            # over-long): fail closed.  Closes the fail-open where a swallowed
            # FileNotFoundError / OSError return string ("Git command not
            # found ...", "Error running git command: ...") was parsed as a
            # branch name and permitted the commit.
            return False
        return branch not in self._PROTECTED_BRANCHES

    @staticmethod
    def _validate_clone_url(clone_url: str) -> bool:
        """
        Validate that a clone URL uses an allowed transport.

        Returns ``True`` on success and raises ``ValueError`` (message
        ``Unsupported git protocol: <clone_url>``) otherwise.

        Rules:
        - ``https://``, ``http://``, ``git://`` and ``ssh://`` are allowed;
          scheme comparison is case-insensitive (RFC 3986).
        - scp-like ``user@host:path`` is allowed when no ``://`` scheme is
          present: the URL must contain ``@`` with a ``:`` after it and before
          any ``/``.
        - Empty strings and URLs with leading/trailing whitespace are rejected
          (whitespace is deliberately *not* stripped — a padded URL is a
          paste-injection red flag).
        - Anything else (``ext::`` transports, ``file://``, ``ftp://``, local
          paths, ...) is rejected.
        """
        if not clone_url or clone_url != clone_url.strip():
            raise ValueError(f"Unsupported git protocol: {clone_url}")

        # Scheme-based URLs. Only allowlisted schemes are permitted; the scheme
        # prefix is compared case-insensitively.
        if "://" in clone_url:
            scheme = clone_url.split("://", 1)[0] + "://"
            if scheme.lower() in ALLOWED_GIT_PROTOCOLS:
                return True
            raise ValueError(f"Unsupported git protocol: {clone_url}")

        # scp-like syntax: user@host:path. Only reached when no '://' scheme
        # was found, so 'https://user@host/repo.git' never hits this branch.
        at_index = clone_url.find("@")
        if at_index != -1:
            colon_index = clone_url.find(":", at_index)
            slash_index = clone_url.find("/")
            if colon_index != -1 and (slash_index == -1 or colon_index < slash_index):
                return True

        raise ValueError(f"Unsupported git protocol: {clone_url}")

    def _is_operator_managed_worktree(self, repo_root: Path) -> bool:
        """True when ``repo_root`` is an operator-managed git worktree.

        Git worktrees represent ``.git`` as a regular file whose contents
        start with ``gitdir: <path>`` (instead of a directory). Such
        workspaces are checked out by operator/host tooling and commits are
        performed host-side, so in-workspace commits are blocked.
        """
        dot_git = repo_root / ".git"
        if not dot_git.exists() or not dot_git.is_file():
            return False
        try:
            content = dot_git.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable gitfile: treat as not operator-managed so read ops
            # and staging keep working; a broken worktree surfaces the
            # underlying git error at commit time instead.
            return False
        return content.startswith("gitdir:")

    @staticmethod
    def _validate_branch_name(name: str) -> str:
        """Validate a branch name against the tool's safe-name allowlist.

        Only letters, digits, dots, slashes, underscores and hyphens are
        allowed; names must not start with '-' or '.', must not contain
        '..', '@{', whitespace or control characters. Returns the name
        unchanged on success; raises ``ValueError`` otherwise.
        """
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or not _BRANCH_NAME_RE.match(name)
            or name.startswith(("-", "."))
            or ".." in name
            or "@{" in name
            or "--" in name
        ):
            raise ValueError(
                f"Invalid branch name: {name!r} - branch names may only contain "
                "letters, digits, dots, slashes, underscores and hyphens; must "
                "not start with '-' or '.', and must not contain '..', '@{', "
                "whitespace or control characters"
            )
        return name

    def _git_branch_create(self, repo_root: Path) -> str:
        """Create a new branch pinned to an immutable commit SHA.

        ``branch_create`` is CREATE-ONLY: it never moves the working-tree
        HEAD (that is ``checkout``'s job). The new branch is based on the
        WORKSPACE checkout HEAD by default, or on the caller's explicit
        ``base`` ref when supplied -- never on the gitdir HEAD (which may
        point elsewhere for an operator-managed worktree).

        The base is resolved to an immutable SHA via ``git rev-parse --verify
        <base>^{commit}`` BEFORE the branch is created, so the branch-creation
        argv always pins an explicit commit and never emits the bare ``git
        branch <name>`` form (which would follow whatever HEAD happens to be
        at create time).
        """
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.branch:
            return "Error: branch is required for branch_create operation"
        try:
            name = self._validate_branch_name(self.branch)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")

        # Resolve the base ref to an immutable SHA BEFORE creating the
        # branch. A caller-supplied base is validated BEFORE any git call so
        # an option-like value (e.g. a leading '-') can never reach
        # rev-parse/branch as a smuggled flag.
        if self.base is None:
            base_expr = "HEAD"
            resolve_args = ["rev-parse", "--verify", "HEAD^{commit}"]
        else:
            try:
                base_expr = self._validate_branch_name(self.base)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            if not self._is_valid_branch_ref(base_expr):
                return self._truncate_output(
                    f"Error: Invalid base ref: {base_expr!r}"
                )
            resolve_args = ["rev-parse", "--verify", f"{base_expr}^{{commit}}"]

        exit_code, stdout, stderr = self._run_git_raw(
            repo_root, resolve_args, timeout=30
        )
        if exit_code != 0:
            return self._with_mode(self._truncate_output(
                f"Git command failed (exit code {exit_code}):\n{stderr}"
            ))
        sha = stdout.strip()
        if not sha:
            # Fail closed: never create a branch named after an empty start
            # point (rev-parse returned nothing despite exit_code == 0).
            return self._with_mode(self._truncate_output(
                "Error: could not resolve base ref to a commit SHA"
            ))

        output = self._run_git(repo_root, ["branch", name, sha])
        if self._is_git_error_output(output):
            return self._with_mode(self._truncate_output(output))
        return self._with_mode(self._truncate_output(
            f"Created branch '{name}' at {sha} (base: {base_expr}); "
            "working tree HEAD not moved \u2014 use checkout to switch."
        ))

    def _git_checkout(self, repo_root: Path) -> str:
        """Check out an existing branch (git checkout <name>)."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.branch:
            return "Error: branch is required for checkout operation"
        try:
            name = self._validate_branch_name(self.branch)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        # No -b: only existing branches may be checked out (creating branches
        # is the branch_create operation). No '--' separator: the validated
        # name can never look like an option.
        output = self._run_git(repo_root, ["checkout", name])
        return self._with_mode(self._truncate_output(output))

    def _git_stage(self, repo_root: Path) -> str:
        """Stage file path(s) (git add -- <paths>)."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.file_path:
            return "Error: file_path is required for stage operation (at least one path)"
        try:
            rels = self._validated_rel_paths(repo_root, self.file_path)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        if not rels:
            return "Error: file_path is required for stage operation (at least one path)"
        output = self._run_git(repo_root, ["add", "--"] + rels)
        return self._with_mode(self._truncate_output(output))

    def _git_unstage(self, repo_root: Path) -> str:
        """Unstage file path(s) (git reset HEAD -- <paths>)."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.file_path:
            return "Error: file_path is required for unstage operation (at least one path)"
        try:
            rels = self._validated_rel_paths(repo_root, self.file_path)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        if not rels:
            return "Error: file_path is required for unstage operation (at least one path)"
        # Path-scoped reset only; a bare `git reset` is never issued.
        output = self._run_git(repo_root, ["reset", "HEAD", "--"] + rels)
        return self._with_mode(self._truncate_output(output))

    def _is_git_error_output(self, output: str) -> bool:
        """True when a _run_git/_git_add result string signals failure.

        _run_git returns error-shaped strings on failure: "Git command
        failed ...", "Git command not found ..."
        or "Error running git command: ..."; _git_add prepends "Error: ..."
        for argument-validation failures. On success git add emits no
        stdout, so prefixing on "Git command" / "Error" is unambiguous.
        The commit flow uses this to short-circuit before running ``git
        commit`` on an un-staged file (which would otherwise fail with a
        confusing "pathspec did not match" error).
        """
        return output.startswith("Git command") or output.startswith("Error")

    @staticmethod
    def _is_valid_branch_ref(output: str) -> bool:
        """True only when ``output`` is a plausible single git branch name.

        Fail-closed validator for the branch-resolution result of
        ``git rev-parse --abbrev-ref HEAD``.  ``_run_git`` returns error-shaped
        STRINGS -- not exceptions -- for some failures: ``"Git command not
        found (git may not be installed)"`` (FileNotFoundError) and
        ``"Error running git command: ..."`` (generic OSError).  Both commit
        gates otherwise parse ANY non-empty string as a branch name, so a
        string that is not in ``_PROTECTED_BRANCHES`` would open the gate
        (fail-OPEN).  Rejecting every error shape closes that hole.

        A valid ref is: non-empty, single line (no embedded newline), free of
        any whitespace, not error-prefixed (``Git command`` / ``Error`` /
        ``fatal:``) and length-bounded (<= 255).
        """
        if not output:
            return False
        if "\n" in output or "\r" in output:
            return False
        if any(c.isspace() for c in output):
            return False
        if output.startswith("Git command") or output.startswith("Error"):
            return False
        if output.startswith("fatal:"):
            return False
        return len(output) <= 255

    def _git_add(self, repo_root: Path) -> str:
        """Run git add. Accepts single file path (str) or multiple (list)."""
        args = ["add"]
        if self.file_path:
            # Same validation choke point as every other path consumer
            # (_validated_rel_paths): rejects ".", "..", globs / pathspec
            # magic and option-like "-" paths. "--" keeps option-like
            # filenames from being parsed as git add flags.
            try:
                rels = self._validated_rel_paths(repo_root, self.file_path)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            args.append("--")
            args.extend(rels)
        else:
            # The full-worktree sweep (git add -A) is removed: every caller
            # must name the paths to stage explicitly.
            return "Error: file_path is required for stage operation (at least one path)"
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)

    def _git_commit(self, repo_root: Path) -> str:
        """Run git commit (selective only).

        ``file_path`` is REQUIRED: the commit is limited to the listed paths
        (explicit ``git add -- <paths>`` staging + ``git commit -m <msg> --
        <paths>``). There is no full-worktree mode -- the historical ``git
        add -A`` auto-stage sweep is removed, so unvetted changes cannot be
        swept into a commit past the review gate. The ``-- <paths>`` pathspec
        applies on EVERY code path -- including the operator-managed-worktree
        path -- so a pre-staged unrelated file can never slip into the commit.
        The named paths are staged
        explicitly (never ``-A``) before committing: ``git commit -- <paths>``
        only commits files git already knows, so untracked files (e.g. the
        first commit of a fresh repo) would otherwise fail with "pathspec ...
        did not match any file(s) known to git". Staging and commit both run
        through the normal execution-mode dispatch; no silent host fallback
        exists, so a container-mode resource outage fails loudly.

        Commit hook policy lives in the execution backends: container mode
        runs the workspace-local .githooks dir (core.hooksPath override);
        host mode neutralizes hooks entirely (core.hooksPath=/dev/null plus
        --no-verify). No vault-backed hooks are consulted. No agent-visible
        flags are added here.
        """
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        # write_on_feature_branch grants: the outer git:write category gate
        # passes, so the branch restriction is enforced HERE, at commit time
        # (mirroring _unprotected_branch_agent_commit_allowed, which also
        # gates commits only). Commits are allowed only on non-protected
        # branches; resolving the current branch fails closed (empty /
        # unresolved output -> denied). Full write/full/ask grains are not
        # branch-restricted by this tool.
        if self._git_write_restricted_to_feature_branch():
            try:
                branch_output = self._run_git(
                    repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]
                )
            except (RuntimeError, PermissionError):
                branch_output = ""
            branch = (branch_output or "").strip()
            if branch == "HEAD":
                # Detached HEAD (see _unprotected_branch_agent_commit_allowed):
                # fail closed instead of reading the literal "HEAD" as an
                # unprotected branch.
                return self._truncate_output(_DETACHED_HEAD_ERROR)
            if not self._is_valid_branch_ref(branch) or branch in self._PROTECTED_BRANCHES:
                branch_label = branch or "unknown"
                return self._truncate_output(
                    "Error: git:write denied: write_on_feature_branch "
                    "permission only allows commits on non-protected branches; "
                    f"current branch is '{branch_label}' (protected branches: "
                    "dev, master, main)"
                )
        # Operator-managed worktrees (a .git FILE pointing at a gitdir) are
        # committed host-side by the operator; block in-workspace commits
        # before any git subprocess can run. Narrow exception: agent commits
        # on feat/* or fix/* branches with the explicit config flag and
        # mandatory container execution (see
        # _unprotected_branch_agent_commit_allowed). When the exception applies,
        # the add/commit subprocesses themselves run with no silent host
        # fallback (a container outage fails loudly).
        if self._is_operator_managed_worktree(repo_root):
            if not self._unprotected_branch_agent_commit_allowed(repo_root):
                return self._truncate_output(
                    getattr(self, "_agent_commit_refusal_reason", None)
                    or (
                        "Error: commits in this workspace are performed "
                        "host-side by the operator (workspace is an "
                        "operator-managed git worktree)"
                    )
                )

        if not self.message or not self.message.strip():
            return "Error: message is required for commit operation"

        # Every commit must name its paths: the ``git add -A`` full-worktree
        # sweep is removed, so a commit without explicit file_path(s) is
        # rejected before any git subprocess runs.
        if not self.file_path:
            return self._truncate_output(
                "Error: file_path is required for commit operation (at least one path)"
            )

        # Single path-scoped commit flow for EVERY code path (plain and
        # operator-managed-worktree alike). Validate the named paths first,
        # then stage exactly those paths (never ``-A``) and commit only them.
        try:
            rels = self._validated_rel_paths(repo_root, self.file_path)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        if not rels:
            return "Error: file_path is required for commit operation (at least one path)"
        # Stage exactly the named paths first (never ``-A``): ``git commit --
        # <paths>`` only works for files git already knows, so an untracked
        # file (e.g. the first commit of a fresh repo) must be staged first.
        add_output = self._git_add(repo_root)
        if self._is_git_error_output(add_output):
            return self._truncate_output(add_output)
        args = ["commit", "-m", self.message, "--"] + rels
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_init(self, repo_root: Path) -> str:
        """Initialize a new git repository in the target directory."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        # Ensure the directory exists
        repo_root.mkdir(parents=True, exist_ok=True)
        args = ["init"]
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_clone(self, repo_root: Path) -> str:
        """Clone a remote git repository into the workspace."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.clone_url:
            return "Error: clone_url is required for clone operation"

        # Protocol allowlist check — reject ext::/file:///unknown schemes
        # before the URL is handed to a git subprocess. (execute() also
        # validates pre-try; this is defense-in-depth for direct callers.)
        self._validate_clone_url(self.clone_url)

        args = ["clone", self.clone_url]
        if self.clone_target:
            # Validate target path is within workspace
            try:
                target_abs = (repo_root / self.clone_target).resolve()
                validated_target = self._validate_path(str(target_abs))
                # In container mode the target must be passed as the
                # container-visible /workspace path.
                if self._use_container_mode():
                    validated_target = self._to_container_path(Path(validated_target))
                args.append(validated_target)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")

        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    @staticmethod
    def _resolved_worktree_path(path: str) -> str:
        """Best-effort resolved form of a worktree path, for comparison."""
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def _git_worktree_add(self, repo_root: Path) -> str:
        """Create a linked git worktree (git worktree add <path> <base>)."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.path:
            return "Error: path is required for worktree_add operation"
        # Validate the target path BEFORE any git call: a target outside the
        # workspace (or outside the repo) must never reach git.
        try:
            target_abs = self._validate_path(
                str((repo_root / self.path).resolve())
            )
            rel = str(Path(target_abs).relative_to(repo_root))
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        if not rel or rel in (".", ".."):
            return self._truncate_output(
                f"Error: invalid worktree path: {self.path!r}"
            )
        base = self.base or "HEAD"
        if not self._is_valid_branch_ref(base):
            return self._truncate_output(
                f"Error: invalid base ref for worktree_add: {base!r}"
            )
        # Probe the existing registrations ONCE (read-only). If a worktree is
        # already registered at the target path, refuse without running
        # `git worktree add` (which would fail or reuse the registration).
        target_norm = self._resolved_worktree_path(target_abs)
        probe_exit, probe_out, _probe_err = self._run_git_raw(
            repo_root, ["worktree", "list", "--porcelain"], timeout=30
        )
        if probe_exit == 0:
            for line in probe_out.splitlines():
                if line.startswith("worktree "):
                    existing = line[len("worktree "):].strip()
                    if self._resolved_worktree_path(existing) == target_norm:
                        return self._truncate_output(
                            f"Error: a worktree is already registered at "
                            f"'{rel}'; remove it before adding"
                        )
        output = self._run_git(repo_root, ["worktree", "add", rel, base])
        if self._is_git_error_output(output):
            return self._with_mode(self._truncate_output(output))
        return self._with_mode(self._truncate_output(
            f"Created worktree at '{rel}' (base: {base})"
        ))

    def _git_worktree_remove(self, repo_root: Path) -> str:
        """Remove a linked git worktree (git worktree remove [--force] <path>)."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.path:
            return "Error: path is required for worktree_remove operation"
        try:
            target_abs = self._validate_path(
                str((repo_root / self.path).resolve())
            )
            rel = str(Path(target_abs).relative_to(repo_root))
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        # Never remove the PRIMARY worktree (the repo root itself): that is a
        # whole-repo deletion, not a linked-worktree cleanup. Refuse before
        # any git call.
        if self._resolved_worktree_path(target_abs) == self._resolved_worktree_path(
            str(repo_root)
        ) or rel in (".", "..") or not rel:
            return self._truncate_output(
                f"Error: refusing to remove the primary worktree ({repo_root}); "
                "worktree_remove only removes linked worktrees"
            )
        # Probe registrations ONCE (read-only) to detect a registered, locked
        # or dirty worktree; refuse without `worktree remove` unless forced.
        registered = False
        locked = False
        probe_exit, probe_out, _probe_err = self._run_git_raw(
            repo_root, ["worktree", "list", "--porcelain"], timeout=30
        )
        if probe_exit == 0:
            entries = {}
            order = []
            current = None
            for line in probe_out.splitlines():
                if line.startswith("worktree "):
                    current = line[len("worktree "):].strip()
                    entries[current] = False
                    order.append(current)
                elif current is not None and (
                    line == "locked" or line.startswith("locked ")
                ):
                    entries[current] = True
            target_norm = self._resolved_worktree_path(target_abs)
            for candidate in order:
                if self._resolved_worktree_path(candidate) == target_norm:
                    registered = True
                    locked = entries[candidate]
                    break
        if registered and not self.force:
            reason = None
            if locked:
                reason = "locked"
            else:
                status_exit, status_out, _status_err = self._run_git_raw(
                    repo_root, ["-C", rel, "status", "--porcelain"], timeout=30
                )
                if status_exit == 0 and status_out.strip():
                    reason = "has local changes"
            if reason:
                return self._truncate_output(
                    f"Error: refusing to remove worktree '{rel}': it is "
                    f"{reason}; retry with force=true"
                )
        args = ["worktree", "remove"]
        if self.force:
            args.append("--force")
        args.append(rel)
        output = self._run_git(repo_root, args)
        if self._is_git_error_output(output):
            return self._with_mode(self._truncate_output(output))
        return self._with_mode(self._truncate_output(
            f"Removed worktree '{rel}'"
        ))

    def _git_stash_push(self, repo_root: Path) -> str:
        """Stash changes (git stash push -m <msg> [-- <paths>]).

        Never uses ``-a``/``-u``: only tracked changes (or the explicitly
        named pathspecs) are stashed, so untracked/ignored files are left in
        the worktree.
        """
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if not self.message or not self.message.strip():
            return "Error: message is required for stash_push operation"
        args = ["stash", "push", "-m", self.message]
        if self.paths:
            try:
                rels = self._validated_rel_paths(repo_root, self.paths)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            if rels:
                args.append("--")
                args.extend(rels)
        exit_code, stdout, stderr = self._run_git_raw(repo_root, args, timeout=30)
        if exit_code != 0:
            return self._with_mode(self._truncate_output(
                f"Git command failed (exit code {exit_code}):\n{stderr}"
            ))
        if "No local changes" in stdout:
            return self._with_mode(self._truncate_output(
                "No local changes to save."
            ))
        return self._with_mode(self._truncate_output(stdout))

    def _git_stash_pop(self, repo_root: Path) -> str:
        """Pop a stash (git stash pop stash@{index})."""
        # git permission gate (fail closed): direct callers must also
        # pass the session git permission check.
        if not self._git_write_allowed():
            return self._flag_gate_error()
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            return "Error: index must be a non-negative integer for stash_pop"
        args = ["stash", "pop", f"stash@{{{self.index}}}"]
        exit_code, stdout, stderr = self._run_git_raw(repo_root, args, timeout=30)
        if exit_code != 0:
            return self._with_mode(self._truncate_output(
                f"Git command failed (exit code {exit_code}):\n{stdout}{stderr}"
            ))
        return self._with_mode(self._truncate_output(stdout))
