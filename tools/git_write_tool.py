# tools/git_write_tool.py
import json
import re
import logging
import subprocess
from typing import Any, Literal, Optional, List, Union
from pathlib import Path
from pydantic import Field
from .git_info_tool import GitInfoTool, resolve_git_execution_mode
from agent.config.defaults import ALLOWED_GIT_PROTOCOLS

logger = logging.getLogger(__name__)

# Branch-name allowlist used by _validate_branch_name (letters, digits, dots,
# slashes, underscores, hyphens only).
_BRANCH_NAME_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')


class GitWriteTool(GitInfoTool):
    """
    Git write operations tool (commit, init, clone, branch_create, checkout,
    stage, unstage).
    Every write is gated (fail closed) on the operator flag
    ``agent_config['git_allow_worktree_commits']`` being exactly True, and on
    the agent's ask policy enforced by the ToolExecutor / security gate. The
    read surface (status, diff, diff_cached, log, branch, branch_list, show,
    remote, blame, config) lives in ``GitInfoTool`` (tools/git_info_tool.py);
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
    diagnostics are reported per call for EVERY operation via three trailing
    lines: ``execution_mode: <mode>`` (containerized | host_fallback |
    unavailable), ``failure_reason: <reason>`` (why a containerized resource
    could not be used, or ``none``) and ``fallback_used: <bool>`` (True when
    the call degraded to a host-side operation). Argument-validation errors
    keep their historical byte-exact form (no trailer).
    """

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
        "unstage",
    ] = Field(
        description="Git write operation to perform: commit, init, clone, "
        "branch_create, checkout, stage, unstage"
    )

    def _flag_gate_error(self) -> str:
        """Return the operator-flag denial message (fail-closed gate)."""
        return "Error: git:write requires the operator flag"

    def execute(self) -> str:
        # Reset per-call runtime state (tool instances may be reused).
        self._resource_manager = None
        self._resolved_workspace_path = None
        self._resolved_workspace_id = None
        self._last_execution_mode = None
        self._last_failure_reason = None
        self._last_fallback_used = False

        # Operator flag gate (fail closed): every write requires the explicit
        # operator flag ``agent_config['git_allow_worktree_commits']`` to be
        # exactly True. Also enforced at the top of each _git_* write method
        # as defense-in-depth for direct callers.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
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
                # Validate working_dir is within workspace
                try:
                    validated_working_dir = self._validate_path(self.working_dir)
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
            except (subprocess.TimeoutExpired, TimeoutError, FileNotFoundError):
                return self._truncate_output(f"Git not available or not a git repository: {repo_root}")
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
        1. ``agent_config['git_allow_worktree_commits']`` is exactly True.
        2. Container git execution is active (``_use_container_mode()``).
        3. Container execution is mandatory for the branch check too: no
           host fallback may resolve the branch, because the host backend
           injects ``--no-verify`` / ``core.hooksPath=/dev/null`` and would
           bypass the QA gate.
        4. The current branch is NOT a protected branch (``dev``, ``master``,
           ``main``); every other branch (feat/*, fix/*, refactor/*, chore/*,
           docs/*, ...) is allowed.
        Any violation returns False so the caller keeps the existing
        operator-managed-worktree block.
        """
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
            return False
        if not self._use_container_mode():
            return False
        try:
            output = self._run_git(
                repo_root,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                allow_host_fallback=False,
            )
        except (RuntimeError, PermissionError):
            # Container-mandatory branch resolution failed (container
            # unavailable, policy denial): fail closed, never degrade.
            return False
        branch = (output or "").strip().splitlines()[0].strip() if (output or "").strip() else ""
        if not branch:
            # Fail closed on empty/blank branch output.
            return False
        return branch not in ("dev", "master", "main")

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
        """Create a new branch (git branch <name>)."""
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
            return self._flag_gate_error()
        if not self.branch:
            return "Error: branch is required for branch_create operation"
        try:
            name = self._validate_branch_name(self.branch)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        output = self._run_git(repo_root, ["branch", name])
        return self._with_mode(self._truncate_output(output))

    def _git_checkout(self, repo_root: Path) -> str:
        """Check out an existing branch (git checkout <name>)."""
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
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
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
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
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
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
        failed ...", "Git command timed out", "Git command not found ..."
        or "Error running git command: ..."; _git_add prepends "Error: ..."
        for argument-validation failures. On success git add emits no
        stdout, so prefixing on "Git command" / "Error" is unambiguous.
        The commit flow uses this to short-circuit before running ``git
        commit`` on an un-staged file (which would otherwise fail with a
        confusing "pathspec did not match" error).
        """
        return output.startswith("Git command") or output.startswith("Error")

    def _git_add(
        self, repo_root: Path, allow_host_fallback: bool = True
    ) -> str:
        """Run git add. Accepts single file path (str) or multiple (list).

        ``allow_host_fallback`` is forwarded to ``_run_git``; the commit
        flow passes False so staging cannot degrade to the host backend
        when a policy-allowed worktree commit mandates container execution.
        """
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
        output = self._run_git(repo_root, args, allow_host_fallback=allow_host_fallback)
        return self._truncate_output(output)

    def _git_commit(self, repo_root: Path) -> str:
        """Run git commit (selective only).

        ``file_path`` is REQUIRED: the commit is limited to the listed paths
        (explicit ``git add -- <paths>`` staging + ``git commit -m <msg> --
        <paths>``). There is no full-worktree mode -- the historical ``git
        add -A`` auto-stage sweep is removed, so unvetted changes cannot be
        swept into a commit past the review gate. The named paths are staged
        explicitly (never ``-A``) before committing: ``git commit -- <paths>``
        only commits files git already knows, so untracked files (e.g. the
        first commit of a fresh repo) would otherwise fail with "pathspec ...
        did not match any file(s) known to git". In the policy-allowed
        worktree path the staging is container-mandatory
        (allow_host_fallback=False); otherwise it uses the default fallback
        policy.

        Commit hook policy lives in the execution backends: container mode
        runs the workspace-local .githooks dir (core.hooksPath override);
        host mode neutralizes hooks entirely (core.hooksPath=/dev/null plus
        --no-verify). No vault-backed hooks are consulted. No agent-visible
        flags are added here.
        """
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
            return self._flag_gate_error()
        # Operator-managed worktrees (a .git FILE pointing at a gitdir) are
        # committed host-side by the operator; block in-workspace commits
        # before any git subprocess can run. Narrow exception: agent commits
        # on feat/* or fix/* branches with the explicit config flag and
        # mandatory container execution (see
        # _unprotected_branch_agent_commit_allowed). When the exception applies,
        # container execution stays mandatory for the add/commit subprocesses
        # themselves (allow_host_fallback=False).
        worktree_commit_allowed = False
        if self._is_operator_managed_worktree(repo_root):
            if not self._unprotected_branch_agent_commit_allowed(repo_root):
                return self._truncate_output(
                    "Error: commits in this workspace are performed host-side by "
                    "the operator (workspace is an operator-managed git worktree)"
                )
            worktree_commit_allowed = True

        if not self.message or not self.message.strip():
            return "Error: message is required for commit operation"

        # Every commit must name its paths: the ``git add -A`` full-worktree
        # sweep is removed, so a commit without explicit file_path(s) is
        # rejected before any git subprocess runs.
        if not self.file_path:
            return self._truncate_output(
                "Error: file_path is required for commit operation (at least one path)"
            )

        if worktree_commit_allowed:
            # Policy-allowed agent commit: stage ONLY the named paths
            # (never ``-A``; _git_add uses self.file_path) and commit,
            # with mandatory container execution for both subprocesses.
            add_output = self._git_add(
                repo_root, allow_host_fallback=False
            )
            if self._is_git_error_output(add_output):
                return self._truncate_output(add_output)
            output = self._run_git(
                repo_root,
                ["commit", "-m", self.message],
                allow_host_fallback=False,
            )
            return self._with_mode(self._truncate_output(output))

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
        output = self._run_git(
            repo_root, args, allow_host_fallback=not worktree_commit_allowed
        )
        return self._with_mode(self._truncate_output(output))

    def _git_init(self, repo_root: Path) -> str:
        """Initialize a new git repository in the target directory."""
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
            return self._flag_gate_error()
        # Ensure the directory exists
        repo_root.mkdir(parents=True, exist_ok=True)
        args = ["init"]
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_clone(self, repo_root: Path) -> str:
        """Clone a remote git repository into the workspace."""
        # Operator flag gate (fail closed): direct callers must also pass the
        # exact-True operator flag check.
        config = getattr(self, "agent_config", None) or {}
        if config.get("git_allow_worktree_commits") is not True:
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
