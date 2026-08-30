# tools/git_info_tool.py
import json
from typing import Any, ClassVar, Literal, Optional, List, Union
from pydantic import Field
import logging
import subprocess
from pathlib import Path
from .base import ToolBase
from security.sandboxed_execution import SandboxedExecution
from agent.config.defaults import ALLOWED_GIT_PROTOCOLS


logger = logging.getLogger(__name__)


def resolve_git_execution_mode(
    agent_config: Optional[dict],
    workspace_metadata: Optional[dict],
    resolved_workspace_path: Optional[str],
    resolved_workspace_id: Optional[str],
) -> str:
    """Resolve the effective git execution mode for diagnostics.

    Mirrors ``GitReadTool._git_execution_mode`` / ``_use_container_mode`` so
    the decision is observable outside the tool (e.g. CheckSystem).

    Returns:
        "containerized": git runs inside the workspace resource container.
        "host_fallback": git runs on the host inside the hermetic sandbox.
        "unavailable": no resolvable workspace to run against.
    """
    config = agent_config or {}
    mode = config.get("git_execution_mode")
    if mode not in ("host", "container"):
        metadata = workspace_metadata or {}
        mode = metadata.get("git_execution_mode")
    effective_mode = mode if mode in ("host", "container") else "container"

    if not resolved_workspace_path:
        return "unavailable"
    if effective_mode == "host" or not resolved_workspace_id:
        return "host_fallback"
    return "containerized"


class GitReadTool(ToolBase):
    """
    Read-only git repository inspection tool.

    Operations: status, diff, diff_cached, log, branch, branch_list, show,
    remote, blame, config. Write operations (commit, init, clone,
    branch_create, checkout, stage, unstage) live in ``GitWriteTool``
    (tools/git_write_tool.py), which gates every write on the session
    ``git_write`` permission (``session_permissions['git_write']`` / the
    effective ``git_write`` grain) and the agent's ask policy; this tool
    intentionally exposes no write surface.

    Parameters:
        working_dir: repository root (defaults to workspace root).
        file_path: single path or list of paths, used by diff, diff_cached,
            log, and blame.
        all_branches: include remote branches for branch / branch_list.

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

    # Stable tool identifier: used in LLM schemas, preset lists and the
    # /api/tools endpoint. The internal ``tool`` Literal field below is
    # excluded from the LLM schema and kept unchanged for backward
    # compatibility with legacy callers.
    name: ClassVar[str] = "git_read"

    # ------------------------------------------------------------------
    # Private runtime state. Leading-underscore attributes are assignable in
    # Pydantic v2 (same pattern as ToolBase._logger): they are not validated
    # fields, so they never conflict with extra="forbid".
    # ------------------------------------------------------------------
    _resource_manager: Optional[Any] = None
    _resolved_workspace_path: Optional[str] = None
    _resolved_workspace_id: Optional[str] = None
    _last_mode: Optional[str] = None
    _last_execution_mode: Optional[str] = None
    _last_failure_reason: Optional[str] = None
    _last_fallback_used: bool = False

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return dynamic permission categories based on the git operation.

        This tool is read-only: every operation requires ``git:read``;
        ``remote`` additionally needs network egress to query remotes.
        """
        if params:
            op = params.get("operation", "")
            if op == "remote":
                return ["git:read", "network:outbound"]
        return ["git:read"]

    tool: Literal["GitInfoTool"] = "GitInfoTool"

    
    operation: Literal[
        "status", "diff", "diff_cached", "log", "branch", "branch_list",
        "show", "remote", "blame", "config",
    ] = Field(
        description="Git read operation to perform: status, diff, diff_cached, log, "
        "branch, branch_list, show, remote, blame, config"
    )
    
    # Common parameters
    working_dir: Optional[str] = Field(
        default=None,
        description="Path to git repository root (defaults to workspace root)"
    )

    workspace_id: Optional[str] = Field(
        default=None,
        description="Workspace identifier resolved by the ToolExecutor "
        "(used for container-backed git execution)"
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
        description="File path(s) for diff, diff_cached, log, blame, stage, unstage, or "
        "commit (selective commit) operations. Accepts a single path (string) or "
        "multiple paths (list of strings)."
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
    branch: Optional[str] = Field(
        default=None,
        description="Branch name for branch_create and checkout operations"
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
        self._last_execution_mode = None
        self._last_failure_reason = None
        self._last_fallback_used = False

        # Atomic permission re-check for network operations. An 'ask' level
        # is NOT re-checked here: it defers to the ToolExecutor's outer gate,
        # which already prompted the user and approved this call, so effective
        # permissions still read 'ask'. Missing/banned/False stay fail-closed
        # (the atomic check runs and denies).
        operation = self.operation
        network_ops = {"remote"}
        if operation in network_ops:
            effective = self.effective_permissions or {}
            if effective.get("network") != "ask":
                from security.security_gate import check_atomic_operation
                if not check_atomic_operation(
                    "network:outbound",
                    effective,
                    "GitReadTool",
                    f"{operation} on remote"
                ):
                    return json.dumps({"error": f"Atomic permission check failed: network:outbound required for {operation}"})

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
                            "GitReadTool falling back to deprecated AgentConfig.workspace_path")

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
                    "GitReadTool git execution mode: %s (operation=%s, workspace_id=%s)",
                    mode,
                    self.operation,
                    self._resolved_workspace_id or "none",
                )
                self._last_mode = mode
            # Record the effective execution mode for per-call reporting
            # (surfaced via _with_mode() on the new operation outputs).
            self._last_execution_mode = mode

            # Execute operation
            if self.operation == "status":
                return self._git_status(repo_root)
            elif self.operation == "diff":
                return self._git_diff(repo_root)
            elif self.operation == "log":
                return self._git_log(repo_root)
            elif self.operation == "branch":
                return self._git_branch(repo_root)
            elif self.operation == "diff_cached":
                return self._git_diff_cached(repo_root)
            elif self.operation == "branch_list":
                return self._git_branch_list(repo_root)
            elif self.operation == "show":
                return self._git_show(repo_root)
            elif self.operation == "remote":
                return self._git_remote(repo_root)
            elif self.operation == "blame":
                return self._git_blame(repo_root)
            elif self.operation == "config":
                return self._git_config(repo_root)
            else:
                return self._truncate_output(f"Unknown operation: {self.operation}")
        
        except Exception as e:
            if isinstance(e, (RuntimeError, PermissionError)):
                # Hard security errors (permission denials) are re-raised
                # instead of swallowing into a generic string.
                raise
            return self._truncate_output(f"Error executing git operation: {e}")
    
    def _run_git(
        self,
        repo_root: Path,
        args: List[str],
        timeout: int = 30,
        allow_host_fallback: bool = True,
    ) -> str:
        """Run git command and return output.

        ``allow_host_fallback=False`` makes container execution mandatory: if
        container mode is required but unavailable (no container, degraded
        host_fallback, policy denial), ``_run_git_raw`` raises RuntimeError
        instead of degrading to the host backend. The host backend injects
        ``--no-verify`` and ``core.hooksPath=/dev/null``, which would bypass
        the QA gate -- forbidden for policy-allowed agent commits.
        """
        # Defense-in-depth: never run git with a cwd outside the workspace.
        # Raises ValueError (handled by execute()'s caller) if repo_root
        # escapes the workspace. execute() already validates before calling
        # _run_git, so this only fires for direct callers.
        self._validate_repo_root(repo_root)

        try:
            exit_code, stdout, stderr = self._run_git_raw(
                repo_root,
                args,
                timeout=timeout,
                allow_host_fallback=allow_host_fallback,
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
        except RuntimeError:
            # Fail closed: container-mandatory execution must surface as a
            # hard error rather than degrade to the host backend (which
            # injects --no-verify / core.hooksPath=/dev/null).
            raise
        except Exception as e:
            return f"Error running git command: {e}"

    def _run_git_raw(
        self,
        repo_root: Path,
        args: List[str],
        timeout: int = 30,
        allow_host_fallback: bool = True,
    ) -> tuple:
        """Execute git in the active execution mode.

        Returns ``(exit_code, stdout, stderr)``. Dispatches between the host
        hermetic sandbox and the workspace resource container based on
        ``_use_container_mode()``. No path validation is performed here --
        that lives in ``_run_git`` so internal callers (e.g.
        ``_git_repo_root``) do not re-validate.

        The container path is self-healing: ``_resolve_resource_execution()``
        consults ``ensure_resource("git")`` at execution time and honors the
        ACTUAL resource mode. A docker/image outage degrades to the hardened
        host path (host_fallback, logged); a policy denial or unknown
        resource surfaces as a clear RuntimeError (unavailable) instead of a
        generic failure.
        """
        if not self._use_container_mode():
            if not allow_host_fallback:
                raise RuntimeError(
                    "GitReadTool: containerized git execution is mandatory "
                    "for this operation but container mode is not active "
                    "(host backend would bypass the commit QA gate)"
                )
            self._last_execution_mode = "host_fallback"
            self._last_failure_reason = None
            self._last_fallback_used = False
            return self._exec_host_raw(repo_root, args, timeout=timeout)

        mode, manager = self._resolve_resource_execution()
        effective = mode.get("mode")
        detail = mode.get("detail", "")
        failure_reason = mode.get("failure_reason")
        fallback_used = bool(mode.get("fallback_used", False))
        if effective == "containerized" and manager is not None:
            logger.info(
                "GitReadTool effective git execution mode: containerized "
                "(operation=%s, workspace_id=%s)",
                self.operation,
                self._resolved_workspace_id or "none",
            )
            self._last_execution_mode = "containerized"
            self._last_failure_reason = failure_reason
            self._last_fallback_used = fallback_used
            return self._exec_container_raw(
                repo_root, args, timeout=timeout, manager=manager
            )
        if effective == "unavailable":
            logger.error(
                "GitReadTool containerized git execution unavailable: %s "
                "(operation=%s)",
                detail,
                self.operation,
            )
            self._last_execution_mode = "unavailable"
            self._last_failure_reason = failure_reason
            self._last_fallback_used = fallback_used
            if failure_reason:
                raise RuntimeError(
                    f"GitReadTool: containerized git execution unavailable: "
                    f"{detail} (failure_reason: {failure_reason})"
                )
            raise RuntimeError(
                f"GitReadTool: containerized git execution unavailable: {detail}"
            )
        # host_fallback: graceful degradation to the hardened host path --
        # unless container execution is mandatory for this call.
        if not allow_host_fallback:
            raise RuntimeError(
                "GitReadTool: containerized git execution is mandatory for "
                f"this operation but execution degraded to host: {detail}"
            )
        logger.warning(
            "GitReadTool degraded containerized git execution to hardened "
            "host git: %s (operation=%s)",
            detail,
            self.operation,
        )
        self._last_execution_mode = "host_fallback"
        self._last_failure_reason = failure_reason
        self._last_fallback_used = fallback_used
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
        # guarantees but skip the gate). An 'ask' level defers outward to
        # the ToolExecutor's outer ask/prompt flow -- SandboxedExecution
        # treats 'ASK' as denied, so the category must be left unset here
        # or the host path would hard-deny before the user is ever asked.
        required_category = None
        if self.session_permissions is not None:
            if (self.effective_permissions or {}).get("git") != "ask":
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
        self,
        repo_root: Path,
        args: List[str],
        timeout: int = 30,
        manager: Any = None,
    ) -> tuple:
        """Run git inside the workspace resource container.

        Only reached when a registry-derived workspace is present
        (``_use_container_mode()``), so host paths are mapped to
        ``/workspace/...`` before dispatch. The same git:read/git:write
        permission gate as the host path is enforced here (fail closed).
        ``manager`` may be supplied by the caller when it was already
        resolved via ``ensure_resource("git")``; otherwise it is obtained
        through ``_ensure_resource_container()`` (which raises a clear
        RuntimeError when the resource is unavailable).
        """
        # Containerized commits run workspace-local hooks from the policy-owned
        # .githooks directory (mounted at /workspace/.githooks). The explicit
        # core.hooksPath override uses the container-mapped ABSOLUTE path so a
        # nested repository (whose root is not the workspace root) cannot
        # resolve the relative ".githooks" to some other directory; repo-local
        # .git/hooks is never consulted. No --no-verify here: the resource
        # container IS the security boundary, but hooks may only originate
        # from .githooks.
        if args and args[0] == "commit":
            hooks_dir = Path(self._resolved_workspace_path) / ".githooks"
            try:
                hooks_path = self._to_container_path(hooks_dir)
            except ValueError:
                # Defensive fallback: workspace path unresolvable → relative.
                hooks_path = ".githooks"
            args = ["-c", f"core.hooksPath={hooks_path}"] + args

        if manager is None:
            manager = self._ensure_resource_container()

        # Permission gate: enforce git:read/git:write ONLY when session
        # permissions are present (mirrors the host path). The gate hard-denies
        # only definitively-denied levels ('banned'/False/missing category -
        # the missing-category case stays fail-closed for legacy/direct
        # callers without effective_permissions). An 'ask' level is NOT
        # denied here: it defers to the ToolExecutor's outer gate, which owns
        # the interactive user-prompt flow.
        if self.session_permissions is not None:
            level = self._get_operation_level(args)
            effective = self.effective_permissions or {}
            if effective.get("git") != "ask":
                from security.security_gate import check_atomic_operation

                if not check_atomic_operation(
                    f"git:{level}",
                    effective,
                    "GitReadTool",
                    f"git {' '.join(args)}",
                ):
                    raise PermissionError(
                        f"Permission denied: git:{level} required for this operation"
                    )

        # NOTE: no --no-verify here. The resource container IS the security
        # boundary; hooks are restricted to the workspace .githooks dir via
        # the core.hooksPath override above (unlike the host path, which
        # neutralizes hooks entirely).
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

    def _resolve_resource_execution(self) -> tuple:
        """Resolve the ACTUAL git resource execution mode at runtime.

        Returns ``(mode_dict, manager_or_None)``. ``mode_dict`` carries
        ``mode`` ("containerized" | "host_fallback" | "unavailable") and
        ``detail`` (human-readable reason). ``manager_or_None`` is the live
        ``ResourceContainerManager`` when ``mode == "containerized"``, else
        ``None``.

        Config-level ``_use_container_mode()`` decides whether the container
        path is *desired*; ``ensure_resource("git")`` then self-heals
        (auto-build image, recreate stale containers) and reports the mode
        that is actually achievable: a docker/image outage degrades to
        ``host_fallback``, while a policy denial or unknown resource
        surfaces as ``unavailable``. Never raises.
        """
        if not self._use_container_mode():
            return (
                {
                    "mode": "host_fallback",
                    "detail": "config selects host mode or no registry workspace",
                },
                None,
            )

        if self._resource_manager is None:
            if not self._resolved_workspace_path:
                return (
                    {
                        "mode": "unavailable",
                        "detail": (
                            "no registry workspace available for "
                            "container-backed git execution"
                        ),
                    },
                    None,
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

            self._resource_manager = ResourceContainerManager(
                workspace_id=self._resolved_workspace_id
                or self.workspace_id
                or "default",
                workspace_path=self._resolved_workspace_path,
                network_mode=network_mode,
                session_permissions=self.session_permissions,
            )

        try:
            result = self._resource_manager.ensure_resource("git")
        except Exception as e:
            # ensure_resource never raises by contract, but stay defensive:
            # an unexpected exception is an unavailable resource.
            return ({"mode": "unavailable", "detail": str(e)}, None)

        result = result or {}
        if result.get("mode") == "containerized":
            return (result, self._resource_manager)
        # host_fallback or unavailable (or unknown) -> no container to use.
        return (result, None)

    def _ensure_resource_container(self) -> Any:
        """Return the workspace git resource container.

        Thin wrapper over ``_resolve_resource_execution()`` for callers that
        need a live manager; raises ``RuntimeError`` with the detail when the
        resource is not containerized (policy denial, unknown resource,
        docker outage).
        """
        mode, manager = self._resolve_resource_execution()
        if mode.get("mode") != "containerized" or manager is None:
            raise RuntimeError(
                "GitReadTool: git resource container unavailable: "
                f"{mode.get('detail', 'unknown reason')}"
            )
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
        state (commit/init/clone/branch_create/checkout/stage/unstage) requires
        ``git:write``; everything else is ``git:read``. ``args`` is accepted for
        future operation-level granularity (e.g. write detection for internal
        helper invocations).
        """
        if self.operation in (
            "commit", "init", "clone", "branch_create", "checkout", "stage", "unstage",
        ):
            return "write"
        return "read"

    def _git_status(self, repo_root: Path) -> str:
        """Run git status."""
        output = self._run_git(repo_root, ["status", "--porcelain=v1"])
        if output.startswith("Git command failed"):
            # Try human-readable status
            output = self._run_git(repo_root, ["status"])
        return self._with_mode(self._truncate_output(output))
    
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
        return self._with_mode(self._truncate_output(output))
    
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
        return self._with_mode(self._truncate_output(output))
    
    def _git_branch(self, repo_root: Path) -> str:
        """Run git branch."""
        args = ["branch"]
        if self.all_branches:
            args.append("-a")
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _validated_rel_paths(self, repo_root: Path, paths) -> List[str]:
        """Normalize and workspace-validate file path(s) into repo-relative paths.

        Accepts a single path (str) or multiple paths (list of str). Each
        path must be a string and must resolve inside the workspace; returns
        paths relative to ``repo_root`` for use as git path arguments. Raises
        ``ValueError`` for invalid path types, empty paths, paths outside
        the workspace, and paths that would make git touch more than the
        explicitly named file(s): ``.`` / ``..`` (whole-tree sweeps,
        equivalent to ``git add -A``) and git pathspec wildcards / magic
        characters (``* ? [ ] : \\``) and leading ``-`` are rejected up
        front so an agent can only stage paths it actually names.
        """
        path_list = paths if isinstance(paths, list) else [paths]
        rels = []
        for p in path_list:
            if not isinstance(p, str):
                raise ValueError(
                    f"Invalid file path type: {type(p).__name__} (expected str)"
                )
            if not p:
                raise ValueError("Invalid empty file path")
            if p in (".", ".."):
                raise ValueError(
                    f"Invalid file path {p!r}: whole-tree paths are not "
                    "allowed; stage named files only"
                )
            if any(c in p for c in "*?[]:\\"):
                raise ValueError(
                    f"Invalid file path {p!r}: git pathspec wildcards and "
                    "magic characters are not allowed; stage named files only"
                )
            if p.startswith("-"):
                raise ValueError(
                    f"Invalid file path {p!r}: paths may not start with '-' "
                    "(option smuggling); stage named files only"
                )
            file_abs = (repo_root / p).resolve()
            validated_abs = self._validate_path(str(file_abs))
            rel = str(Path(validated_abs).relative_to(repo_root))
            if rel in (".", ".."):
                raise ValueError(
                    f"Invalid file path {p!r}: resolves to {rel!r}, which "
                    "would stage the whole tree; stage named files only"
                )
            rels.append(rel)
        return rels

    def _with_mode(self, output: str) -> str:
        """Append effective execution mode + failure diagnostics.

        Appends three trailing lines — ``execution_mode`` (containerized |
        host_fallback | unavailable), ``failure_reason`` (why a
        containerized resource could not be used, or ``none``) and
        ``fallback_used`` (True when the call degraded to a host-side
        operation) — so every operation reports how it actually executed.
        """
        return (
            f"{output}\nexecution_mode: {self._last_execution_mode or 'unavailable'}"
            f"\nfailure_reason: {self._last_failure_reason or 'none'}"
            f"\nfallback_used: {str(bool(self._last_fallback_used)).lower()}"
        )

    def _git_diff_cached(self, repo_root: Path) -> str:
        """Run git diff --cached (staged changes)."""
        # Same belt-and-suspenders as _git_diff: --no-ext-diff guarantees
        # external diff drivers can never render diffs.
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv"]
        if self.file_path:
            try:
                rels = self._validated_rel_paths(repo_root, self.file_path)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            if rels:
                args.append("--")
                args.extend(rels)
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_branch_list(self, repo_root: Path) -> str:
        """Run git branch --list (explicit list operation)."""
        args = ["branch", "--list"]
        if self.all_branches:
            args.append("--all")
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_show(self, repo_root: Path) -> str:
        """Run git show."""
        args = ["show", "--no-ext-diff", "--no-textconv"]
        if self.format:
            args.append(f"--format={self.format}")
        args.append(self.commit)
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))
    
    def _git_remote(self, repo_root: Path) -> str:
        """Run git remote."""
        output = self._run_git(repo_root, ["remote", "-v"])
        return self._with_mode(self._truncate_output(output))
    
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
        return self._with_mode(self._truncate_output(output))
    
    def _git_config(self, repo_root: Path) -> str:
        """Run git config."""
        args = ["config", "--list"]
        if self.config_name:
            args = ["config", "--get", self.config_name]
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

# Backward-compatible alias: legacy code, imports and tests reference the old
# class name; the canonical identifier is now ``GitReadTool`` with the stable
# tool name ``git_read``.
GitInfoTool = GitReadTool
