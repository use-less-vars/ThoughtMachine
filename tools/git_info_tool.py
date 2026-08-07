# tools/git_info_tool.py
import json
from typing import Any, ClassVar, Literal, Optional, List, Union
from pydantic import Field
import logging
import subprocess
import os
from pathlib import Path
from .base import ToolBase
from security.sandboxed_execution import SandboxedExecution


# Clone URL protocol allowlist. ``git clone`` accepts arbitrary transport URLs
# (including ``ext::`` shell executors and ``file://`` local access), so clone
# URLs are restricted to these schemes plus scp-like ``user@host:path`` syntax.
ALLOWED_GIT_PROTOCOLS = ["https://", "http://", "git://", "ssh://"]


class GitInfoTool(ToolBase):
    """
    Git repository tool with read-only operations (status, diff, log, branch, show,
    remote, blame, config) and write operations (commit, init, clone).
    Write operations are subject to the agent's ask policy.
    """

    # ------------------------------------------------------------------
    # Private runtime state. Leading-underscore attributes are assignable in
    # Pydantic v2 (same pattern as ToolBase._logger): they are not validated
    # fields, so they never conflict with extra="forbid".
    # ------------------------------------------------------------------
    _resource_manager: Optional[Any] = None
    _resolved_workspace_path: Optional[str] = None
    _resolved_workspace_id: Optional[str] = None

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return dynamic permission categories based on the git operation."""
        if params:
            op = params.get("operation", "")
            if op in ("remote",):
                return ["git:read", "network:outbound"]
            if op in ("commit", "init"):
                return ["git:write"]
            if op in ("clone",):
                return ["git:write", "network:outbound"]
            if op in ("push", "pull", "fetch", "merge", "rebase"):
                return ["git:write", "network:outbound"]
        # All other operations (status, diff, log, branch, show, blame, config) are read-only
        return ["git:read"]

    tool: Literal["GitInfoTool"] = "GitInfoTool"

    
    operation: Literal["status", "diff", "log", "branch", "show", "remote", "blame", "config", "commit", "init", "clone"] = Field(
        description="Git operation to perform: status, diff, log, branch, show, remote, blame, config, commit, init, clone"
    )
    
    # Common parameters
    working_dir: Optional[str] = Field(
        default=None,
        description="Path to git repository root (defaults to workspace root)"
    )

    workspace_id: Optional[str] = Field(
        default=None,
        description="Workspace identifier used to locate vault-backed hooks "
        "(~/.thoughtmachine/hooks/<workspace_id>/<hook_name>)"
    )
    
    # Operation-specific parameters
    commit1: Optional[str] = Field(
        default=None,
        description="First commit reference for diff operation (default: HEAD)"
    )
    commit2: Optional[str] = Field(
        default=None,
        description="Second commit reference for diff operation (default: working tree)"
    )
    file_path: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="File path(s) for diff, log, blame, or commit operations. Accepts a single path (string) or multiple paths (list of strings)."
    )
    
    # Log parameters
    max_count: Optional[int] = Field(
        default=50,
        description="Maximum number of commits to show for log operation"
    )
    since: Optional[str] = Field(
        default=None,
        description="Show commits more recent than specified date for log operation"
    )
    until: Optional[str] = Field(
        default=None,
        description="Show commits older than specified date for log operation"
    )
    author: Optional[str] = Field(
        default=None,
        description="Filter commits by author for log operation"
    )
    grep: Optional[str] = Field(
        default=None,
        description="Filter commits by commit message pattern for log operation"
    )
    
    # Branch parameters
    all_branches: bool = Field(
        default=False,
        description="Include remote branches for branch operation"
    )
    
    # Show parameters
    commit: Optional[str] = Field(
        default="HEAD",
        description="Commit reference for show operation"
    )
    format: Optional[str] = Field(
        default=None,
        description="Format string for show operation (e.g., '%H %s')"
    )
    
    # Blame parameters
    line_start: Optional[int] = Field(
        default=None,
        description="Start line number for blame operation"
    )
    line_end: Optional[int] = Field(
        default=None,
        description="End line number for blame operation"
    )
    
    # Commit parameters
    message: Optional[str] = Field(
        default=None,
        description="Commit message for commit operation"
    )

    # Clone parameters
    clone_url: Optional[str] = Field(
        default=None,
        description="Remote URL to clone from (required for clone operation)"
    )
    clone_target: Optional[str] = Field(
        default=None,
        description="Target directory for clone operation (default: derived from URL)"
    )

    # Config parameters
    config_name: Optional[str] = Field(
        default=None,
        description="Config name to retrieve (if not specified, list all configs)"
    )
    
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

    def _validate_repo_root(self, repo_root: Path) -> Path:
        """
        Ensure the git repository root stays inside the workspace.

        Resolves the workspace through the same registry mechanism used by
        ``_validate_path`` (``_resolve_registry_workspace``). Returns the
        resolved ``repo_root`` when it is inside the workspace; raises
        ``ValueError`` otherwise. When no workspace can be resolved (no
        session, no ``workspace_path``), no restriction is applied.
        """
        repo_root = Path(repo_root).expanduser().resolve()
        ws_path = self._resolve_registry_workspace()
        if not ws_path:
            return repo_root

        ws_abs = Path(ws_path).expanduser().resolve()
        try:
            repo_root.relative_to(ws_abs)
        except ValueError:
            raise ValueError(
                f"Git repository {repo_root} is outside the workspace {ws_abs}"
            ) from None
        return repo_root

    def execute(self) -> str:
        # Reset per-call runtime state (tool instances may be reused).
        self._resource_manager = None
        self._resolved_workspace_path = None
        self._resolved_workspace_id = None

        # Atomic permission re-check for network operations
        operation = self.operation
        network_ops = {"remote", "clone", "push", "pull", "fetch", "merge", "rebase"}
        if operation in network_ops:
            from security.security_gate import check_atomic_operation
            effective = self.effective_permissions or {}
            if not check_atomic_operation(
                "network:outbound",
                effective,
                "GitInfoTool",
                f"{operation} on remote"
            ):
                return json.dumps({"error": f"Atomic permission check failed: network:outbound required for {operation}"})

        # Validate the clone URL protocol BEFORE any subprocess can run.
        # This check sits outside the try/except below so the ValueError
        # surfaces to the caller instead of being swallowed into an error
        # string by the catch-all handler.
        if operation == "clone" and self.clone_url:
            self._validate_clone_url(self.clone_url)

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
                            "GitInfoTool falling back to deprecated AgentConfig.workspace_path")

                if ws_path:
                    repo_root = Path(ws_path).expanduser().resolve()
                else:
                    repo_root = Path.cwd()
            else:
                repo_root = Path.cwd()

            # Security: the git repository root must stay inside the
            # workspace. ``rev-parse --show-toplevel`` below can re-point
            # repo_root, so it is re-validated after the override too.
            try:
                repo_root = self._validate_repo_root(repo_root)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")

            # Handle operations that don't require an existing repo
            if self.operation == "init":
                return self._git_init(repo_root)
            elif self.operation == "clone":
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
            
            # Execute operation
            if self.operation == "status":
                return self._git_status(repo_root)
            elif self.operation == "diff":
                return self._git_diff(repo_root)
            elif self.operation == "log":
                return self._git_log(repo_root)
            elif self.operation == "branch":
                return self._git_branch(repo_root)
            elif self.operation == "show":
                return self._git_show(repo_root)
            elif self.operation == "remote":
                return self._git_remote(repo_root)
            elif self.operation == "blame":
                return self._git_blame(repo_root)
            elif self.operation == "config":
                return self._git_config(repo_root)
            elif self.operation == "commit":
                return self._git_commit(repo_root)
            else:
                return self._truncate_output(f"Unknown operation: {self.operation}")
        
        except Exception as e:
            if isinstance(e, (RuntimeError, PermissionError)):
                # Vault hook failures and permission denials are hard errors:
                # surface them instead of swallowing into a generic string.
                raise
            return self._truncate_output(f"Error executing git operation: {e}")
    
    def _run_git(self, repo_root: Path, args: List[str], timeout: int = 30) -> str:
        """Run git command and return output."""
        # Defense-in-depth: never run git with a cwd outside the workspace.
        # Raises ValueError (handled by execute()'s caller) if repo_root
        # escapes the workspace. execute() already validates before calling
        # _run_git, so this only fires for direct callers.
        self._validate_repo_root(repo_root)

        try:
            exit_code, stdout, stderr = self._run_git_raw(
                repo_root, args, timeout=timeout
            )
            if exit_code != 0:
                return f"Git command failed (exit code {exit_code}):\n{stderr}"
            return stdout
        except subprocess.TimeoutExpired:
            return "Git command timed out"
        except TimeoutError:
            return "Git command timed out"
        except FileNotFoundError:
            return "Git command not found (git may not be installed)"
        except PermissionError:
            # Fail closed: a denied git:read/git:write permission must surface
            # to the caller, not be swallowed into a generic error string.
            raise
        except Exception as e:
            return f"Error running git command: {e}"

    def _run_git_raw(
        self, repo_root: Path, args: List[str], timeout: int = 30
    ) -> tuple:
        """Execute git in the active execution mode.

        Returns ``(exit_code, stdout, stderr)``. Dispatches between the host
        hermetic sandbox and the workspace resource container based on
        ``_use_container_mode()``. No path validation is performed here --
        that lives in ``_run_git`` so internal callers (e.g.
        ``_git_repo_root``) do not re-validate.
        """
        if self._use_container_mode():
            return self._exec_container_raw(repo_root, args, timeout=timeout)
        return self._exec_host_raw(repo_root, args, timeout=timeout)

    def _exec_host_raw(
        self, repo_root: Path, args: List[str], timeout: int = 30
    ) -> tuple:
        """Run git on the host inside the hermetic sandbox."""
        # Hardened args, applied to EVERY git invocation: hooks are
        # neutralized (core.hooksPath=/dev/null), external diff drivers /
        # textconv filters, fsmonitor helpers and credential helpers are
        # disabled so ambient or repo-local config cannot inject
        # executable behavior.
        hardened_args = [
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.attributesFile=/dev/null",
            "-c", "diff.external=",
            "-c", "core.fsmonitor=",
            "-c", "filter.clean=",
            "-c", "filter.smudge=",
            "-c", "diff.textconv=",
            "-c", "credential.helper=",
        ]
        # commit additionally skips pre-commit/commit-msg hooks via
        # --no-verify as a second line of defense.
        if args and args[0] == "commit":
            args = [args[0], "--no-verify"] + args[1:]

        executor = SandboxedExecution(
            session_permissions=self.session_permissions,
            workspace_id=getattr(self, "workspace_id", None),
            logger=getattr(self, "_logger", None) or logging.getLogger(__name__),
        )
        # Permission gate: enforce git:read/git:write ONLY when session
        # permissions are present (the ToolExecutor always injects them,
        # falling back to DEFAULT_SESSION_PERMISSIONS; legacy/direct
        # callers without permissions keep the hermetic-environment
        # guarantees but skip the gate).
        required_category = None
        if self.session_permissions is not None:
            required_category = f"git:{self._get_operation_level(args)}"

        result = executor.run(
            ["git"] + hardened_args + args,
            cwd=str(repo_root),
            timeout=timeout,
            required_category=required_category,
            extra_env={
                "GIT_PAGER": "cat",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        return (result.returncode, result.stdout, result.stderr)

    def _exec_container_raw(
        self, repo_root: Path, args: List[str], timeout: int = 30
    ) -> tuple:
        """Run git inside the workspace resource container.

        Only reached when a registry-derived workspace is present
        (``_use_container_mode()``), so host paths are mapped to
        ``/workspace/...`` before dispatch. The same git:read/git:write
        permission gate as the host path is enforced here (fail closed).
        """
        manager = self._ensure_resource_container()

        # Permission gate: enforce git:read/git:write ONLY when session
        # permissions are present (mirrors the host path).
        if self.session_permissions is not None:
            from security.security_gate import check_atomic_operation

            level = self._get_operation_level(args)
            effective = self.effective_permissions or {}
            if not check_atomic_operation(
                f"git:{level}",
                effective,
                "GitInfoTool",
                f"git {' '.join(args)}",
            ):
                raise PermissionError(
                    f"Permission denied: git:{level} required for this operation"
                )

        # commit additionally skips pre-commit/commit-msg hooks via
        # --no-verify as a second line of defense (same as host path).
        if args and args[0] == "commit":
            args = [args[0], "--no-verify"] + args[1:]

        environment = {
            "GIT_PAGER": "cat",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        agent_config = getattr(self, "agent_config", None) or {}
        git_env = agent_config.get("git_environment")
        if isinstance(git_env, dict):
            for key, value in git_env.items():
                if isinstance(key, str) and isinstance(value, str):
                    environment[key] = value

        result = manager.exec(
            ["git"] + args,
            workdir=self._to_container_path(repo_root),
            environment=environment,
            timeout=timeout,
        )
        return (result["exit_code"], result["stdout"], result["stderr"])

    def _git_repo_root(self, repo_root: Path) -> Optional[Path]:
        """Resolve the actual repository root, or None when not a repo.

        Fast path: ``<repo_root>/.git`` exists as a directory. Otherwise
        consult ``git rev-parse --show-toplevel``; in container mode the
        returned ``/workspace`` path is reverse-mapped to the host path.
        """
        dot_git = repo_root / ".git"
        if dot_git.exists() and dot_git.is_dir():
            return repo_root

        exit_code, stdout, _stderr = self._run_git_raw(
            repo_root, ["rev-parse", "--show-toplevel"], timeout=10
        )
        if exit_code != 0 or not stdout.strip():
            return None
        if self._use_container_mode():
            return self._from_container_path(stdout.strip())
        return Path(stdout.strip())

    def _git_execution_mode(self) -> str:
        """Return 'host' or 'container' for git execution.

        Precedence: ``agent_config['git_execution_mode']`` (per-session),
        then workspace metadata ``git_execution_mode``, then the default
        ``'container'``. Container mode additionally requires a
        registry-derived workspace (enforced by ``_use_container_mode()``).
        """
        config = getattr(self, "agent_config", None) or {}
        mode = config.get("git_execution_mode")
        if mode not in ("host", "container"):
            mode = self._workspace_metadata().get("git_execution_mode")
        return mode if mode in ("host", "container") else "container"

    def _workspace_metadata(self) -> dict:
        """Return metadata of the session's registered workspace.

        Best-effort: any registry failure or missing entry yields ``{}`` so
        execution-mode resolution can fall back to the default.
        """
        session_id = getattr(self, "session_id", None)
        if not session_id:
            return {}
        try:
            from session.session_registry import SessionRegistry
            from thoughtmachine.workspace_registry import WorkspaceRegistry

            session_info = SessionRegistry.get_default().get(session_id)
            if not session_info:
                return {}
            ws_id = session_info.get("workspace_id") if session_info else None
            if not ws_id:
                return {}
            entry = WorkspaceRegistry.get_default().get_workspace(ws_id)
            metadata = getattr(entry, "metadata", None)
            return dict(metadata) if metadata else {}
        except Exception:
            return {}

    def _use_container_mode(self) -> bool:
        """True when git must run inside the resource container.

        Container mode requires (a) an explicit execution mode other than
        'host' AND (b) a registry-derived workspace (id + path). The
        registry requirement keeps deprecated ``workspace_path`` callers and
        direct test invocations on the host path, so tests without a docker
        daemon never enter container mode.
        """
        return (
            self._git_execution_mode() != "host"
            and bool(self._resolved_workspace_path)
            and bool(self._resolved_workspace_id)
        )

    def _ensure_resource_container(self) -> Any:
        """Return (creating if needed) the workspace git resource container."""
        if self._resource_manager is not None:
            return self._resource_manager
        if not self._resolved_workspace_path:
            raise RuntimeError(
                "GitInfoTool: no registry workspace available for container-backed git execution"
            )

        try:
            from security.security_gate import get_expected_container_config

            expected = get_expected_container_config(
                self.session_permissions or {}, None
            )
            network_mode = expected.get("network_mode", "none")
        except Exception:
            network_mode = "none"

        from infra.resource_container_manager import ResourceContainerManager

        manager = ResourceContainerManager(
            workspace_id=self._resolved_workspace_id or self.workspace_id or "default",
            workspace_path=self._resolved_workspace_path,
            network_mode=network_mode,
        )
        manager.ensure_container()
        self._resource_manager = manager
        return manager

    def _to_container_path(self, host_path) -> str:
        """Map a host path inside the resolved workspace to ``/workspace/...``."""
        ws = Path(self._resolved_workspace_path).expanduser().resolve()
        try:
            rel = Path(host_path).expanduser().resolve().relative_to(ws)
        except ValueError:
            raise ValueError(
                f"Git repository {host_path} is outside the workspace {ws}"
            ) from None
        if not rel.parts:
            return "/workspace"
        return "/workspace/" + "/".join(rel.parts)

    def _from_container_path(self, container_path) -> Path:
        """Reverse-map a ``/workspace`` path to a host path."""
        ws = Path(self._resolved_workspace_path).expanduser().resolve()
        container_path = str(container_path).strip()
        if container_path == "/workspace":
            return ws
        if container_path.startswith("/workspace/"):
            return ws / container_path[len("/workspace/"):]
        return ws / container_path.lstrip("/")

    def _resolve_registry_workspace_info(self) -> tuple:
        """Resolve ``(workspace_id, root_path)`` from the session registries.

        Returns ``(None, None)`` when no session is present, no workspace is
        registered, or registry lookup fails (best-effort). Mirrors
        ``ToolBase._resolve_registry_workspace`` but also returns the
        workspace id, which container-backed git execution needs.
        """
        session_id = getattr(self, "session_id", None)
        if not session_id:
            return (None, None)
        try:
            from session.session_registry import SessionRegistry
            from thoughtmachine.workspace_registry import WorkspaceRegistry

            session_info = SessionRegistry.get_default().get(session_id)
            if not session_info:
                return (None, None)
            ws_id = session_info.get("workspace_id") if session_info else None
            if not ws_id:
                return (None, None)
            entry = WorkspaceRegistry.get_default().get_workspace(ws_id)
            if not entry:
                return (None, None)
            return (ws_id, entry.root_path)
        except Exception:
            return (None, None)
    
    def _get_operation_level(self, args: List[str]) -> str:
        """Return the permission level ('read'/'write') for a git invocation.

        Derived from the declared operation: anything that mutates repository
        state (commit/init/clone) requires ``git:write``; everything else is
        ``git:read``. ``args`` is accepted for future operation-level
        granularity (e.g. write detection for internal helper invocations).
        """
        if self.operation in ("commit", "init", "clone"):
            return "write"
        return "read"

    def _run_vault_hooks(self, repo_root: Path, hook_name: str) -> None:
        """Run a vault-managed hook script before a git operation.

        Vault hooks live in ``~/.thoughtmachine/hooks/<workspace_id>/<hook_name>``
        and are the ONLY sanctioned extension point for policy injection:
        repository-local ``.git/hooks/`` scripts are never executed (the
        hardened runner neutralizes them via ``core.hooksPath=/dev/null`` and
        ``--no-verify``).

        Raises:
            RuntimeError: if the hook exists but exits non-zero.
            PermissionError: if session permissions deny ``git:write``
                (fail closed -- hooks are write-side policy).
        """
        workspace_id = getattr(self, "workspace_id", None)
        if not workspace_id:
            if getattr(self, "_logger", None):
                self._logger.debug(
                    "GitInfoTool: no workspace_id, skipping vault %s hook", hook_name
                )
            return

        hook_path = (
            Path.home() / ".thoughtmachine" / "hooks" / str(workspace_id) / hook_name
        )
        if not hook_path.is_file():
            if getattr(self, "_logger", None):
                self._logger.debug("GitInfoTool: vault hook not found: %s", hook_path)
            return

        executor = SandboxedExecution(
            session_permissions=self.session_permissions,
            workspace_id=str(workspace_id),
            logger=getattr(self, "_logger", None) or logging.getLogger(__name__),
        )
        # Enforce git:write only when session permissions are present (the
        # ToolExecutor always injects them; legacy/direct callers without
        # permissions keep the sandbox's hermetic-environment guarantees).
        required_category = "git:write" if self.session_permissions is not None else None
        result = executor.run(
            [str(hook_path)],
            cwd=str(repo_root),
            required_category=required_category,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Vault {hook_name} hook failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    def _git_status(self, repo_root: Path) -> str:
        """Run git status."""
        output = self._run_git(repo_root, ["status", "--porcelain=v1"])
        if output.startswith("Git command failed"):
            # Try human-readable status
            output = self._run_git(repo_root, ["status"])
        return self._truncate_output(output)
    
    def _git_diff(self, repo_root: Path) -> str:
        """Run git diff."""
        # Belt-and-suspenders for Bug A: --no-ext-diff guarantees external
        # diff drivers can never render diffs (hardened_args also clears
        # diff.external).
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if self.commit1:
            args.append(self.commit1)
        if self.commit2:
            args.append(self.commit2)
        else:
            # If only commit1 is specified, compare commit1 to working tree
            pass
        if self.file_path:
            # Validate file paths are within workspace (list-safe)
            try:
                paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
                rels = []
                for p in paths:
                    if not isinstance(p, str):
                        raise ValueError(
                            f"Invalid file path type: {type(p).__name__} (expected str)"
                        )
                    # Compute absolute path relative to repo_root
                    file_abs = (repo_root / p).resolve()
                    validated_abs = self._validate_path(str(file_abs))
                    # Convert to path relative to repo_root for git
                    rels.append(str(Path(validated_abs).relative_to(repo_root)))
                # Single '--' marker, then all paths (not one marker per path)
                if rels:
                    args.append("--")
                    args.extend(rels)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_log(self, repo_root: Path) -> str:
        """Run git log."""
        args = ["log", "--no-ext-diff", "--no-textconv", f"--max-count={self.max_count}", "--oneline"]
        if self.since:
            args.append(f"--since={self.since}")
        if self.until:
            args.append(f"--until={self.until}")
        if self.author:
            args.append(f"--author={self.author}")
        if self.grep:
            args.append(f"--grep={self.grep}")
        if self.file_path:
            # Validate file paths are within workspace (list-safe)
            try:
                paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
                rels = []
                for p in paths:
                    if not isinstance(p, str):
                        raise ValueError(
                            f"Invalid file path type: {type(p).__name__} (expected str)"
                        )
                    # Compute absolute path relative to repo_root
                    file_abs = (repo_root / p).resolve()
                    validated_abs = self._validate_path(str(file_abs))
                    # Convert to path relative to repo_root for git
                    rels.append(str(Path(validated_abs).relative_to(repo_root)))
                # Single '--' marker, then all paths (not one marker per path)
                if rels:
                    args.append("--")
                    args.extend(rels)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_branch(self, repo_root: Path) -> str:
        """Run git branch."""
        args = ["branch"]
        if self.all_branches:
            args.append("-a")
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_show(self, repo_root: Path) -> str:
        """Run git show."""
        args = ["show", "--no-ext-diff", "--no-textconv"]
        if self.format:
            args.append(f"--format={self.format}")
        args.append(self.commit)
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_remote(self, repo_root: Path) -> str:
        """Run git remote."""
        output = self._run_git(repo_root, ["remote", "-v"])
        return self._truncate_output(output)
    
    def _git_blame(self, repo_root: Path) -> str:
        """Run git blame."""
        if not self.file_path:
            return "Error: file_path is required for blame operation"
        # Validate file path is within workspace
        try:
            # Compute absolute path relative to repo_root
            file_abs = (repo_root / self.file_path).resolve()
            validated_abs = self._validate_path(str(file_abs))
            # Convert to path relative to repo_root for git
            file_rel = Path(validated_abs).relative_to(repo_root)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        
        args = ["blame"]
        if self.line_start and self.line_end:
            args.append(f"-L{self.line_start},{self.line_end}")
        elif self.line_start:
            args.append(f"-L{self.line_start},+1")
        args.append("--")
        args.append(str(file_rel))
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_config(self, repo_root: Path) -> str:
        """Run git config."""
        args = ["config", "--list"]
        if self.config_name:
            args = ["config", "--get", self.config_name]
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)

    def _git_add(self, repo_root: Path) -> str:
        """Run git add. Accepts single file path (str) or multiple (list)."""
        args = ["add"]
        if self.file_path:
            # Normalize to list for uniform handling
            paths = self.file_path if isinstance(self.file_path, list) else [self.file_path]
            for path in paths:
                try:
                    file_abs = (repo_root / path).resolve()
                    validated_abs = self._validate_path(str(file_abs))
                    file_rel = Path(validated_abs).relative_to(repo_root)
                    args.append(str(file_rel))
                except ValueError as e:
                    return self._truncate_output(f"Error: {e}")
        else:
            args.append("-A")  # Stage all changes
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)

    def _git_commit(self, repo_root: Path) -> str:
        """Run git commit."""
        # If specific file_paths are given, unstage everything first so that
        # only the explicitly-listed files are included in the commit.
        if self.file_path:
            reset_result = self._run_git(repo_root, ["reset", "HEAD", "--", "."])
            if reset_result.startswith("Git command failed"):
                return reset_result

        # First, stage changes
        add_result = self._git_add(repo_root)
        if add_result.startswith("Git command failed") or add_result.startswith("Error"):
            return add_result

        # Then commit
        if not self.message:
            return "Error: message is required for commit operation"

        # Vault-backed pre-commit hook (write-side policy). Runs after staging
        # (mirroring git semantics) and BEFORE the commit; a non-zero exit
        # aborts the commit. Repository-local .git/hooks are never consulted.
        self._run_vault_hooks(repo_root, "pre-commit")

        args = ["commit", "-m", self.message]
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)

    def _git_init(self, repo_root: Path) -> str:
        """Initialize a new git repository in the target directory."""
        # Ensure the directory exists
        repo_root.mkdir(parents=True, exist_ok=True)
        args = ["init"]
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)

    def _git_clone(self, repo_root: Path) -> str:
        """Clone a remote git repository into the workspace."""
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
        return self._truncate_output(output)
