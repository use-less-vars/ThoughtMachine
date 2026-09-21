# tools/git_info_tool.py
from typing import Any, ClassVar, Literal, Optional, List, Union
from pydantic import Field
import logging
import subprocess
import unicodedata
from pathlib import Path
from .base import ToolBase
from security.sandboxed_execution import SandboxedExecution
from agent.config.defaults import ALLOWED_GIT_PROTOCOLS
from agent.config.resource_catalog import catalog_entry


logger = logging.getLogger(__name__)

# Read-op hardening: the allowlisted for-each-ref format atoms, the characters
# that may never appear in an agent-supplied ref pattern/prefix/format (a shell
# could re-interpret them), and the allowlisted cat-file object types.
_FOR_EACH_REF_ALLOWED_TOKENS = frozenset({
    "refname", "refname:short", "objectname", "objectname:short",
    "objecttype", "subject",
})
_REF_PATTERN_FORBIDDEN = frozenset({";", "|", "&", "$", "`", ">", "<", "\n", "\r"})
_CAT_FILE_ALLOWED_TYPES = frozenset({"blob", "tree", "commit", "tag"})


class GitUnavailableError(FileNotFoundError):
    """The git executable could not be run (missing binary / unspawnable).

    Raised by ``GitReadTool._git_repo_root`` so that "git is unavailable" is
    never reported as "not a git repository".

    Subclasses ``FileNotFoundError`` deliberately: ``GitWriteTool`` (which
    shares ``_git_repo_root``) only catches
    ``(subprocess.TimeoutExpired, TimeoutError, FileNotFoundError)`` around its
    call, so this keeps that call site fail-closed without modification.
    """


def _git_run_indicates_missing_git(exit_code: int, stderr: Optional[str]) -> bool:
    """True when a failed ``git`` run looks like a missing git executable.

    Distinguishes "git could not be found" (a POSIX shell exits 127, or the
    runtime prints a "command not found"-style message) from "git ran and this
    is not a repository" (e.g. exit 128 with "fatal: not a git repository"), so
    the two failure modes are never reported with the same message.
    """
    if exit_code == 127:
        return True
    text = (stderr or "").strip().lower()
    return "command not found" in text or "not found" in text


def _migrate_legacy_git_execution_mode(agent_config: Optional[dict]) -> None:
    """Pop the retired ``git_execution_mode`` session/agent-config key.

    The git execution mode is now taken from the resource catalog's ``git``
    entry (``execution_mode``); the legacy ``git_execution_mode`` config key can
    no longer influence resolution, so a stale persisted value is popped on
    read.  A legacy ``"host"`` value used to force a host fallback, so it is
    surfaced with a WARNING that names it rather than being silently lost --
    the same retire-tolerant-on-read pattern used for ``use_container_registry``.
    """
    if not isinstance(agent_config, dict):
        return
    stale = agent_config.pop("git_execution_mode", None)
    if stale == "host":
        logger.warning(
            "Ignoring legacy git_execution_mode=%r; the git execution mode is "
            "now taken from the resource catalog 'git' entry's execution_mode "
            "field.",
            stale,
        )


def resolve_git_execution_mode(
    agent_config: Optional[dict],
    workspace_metadata: Optional[dict],
    resolved_workspace_path: Optional[str],
    resolved_workspace_id: Optional[str],
) -> str:
    """Resolve the effective git execution mode for diagnostics.

    Mirrors ``GitReadTool._git_execution_mode`` / ``_use_container_mode`` so
    the decision is observable outside the tool (e.g. CheckSystem).

    The mode is the ``execution_mode`` of the resource catalog's ``git`` entry
    (``"container"`` | ``"host"``).  The retired session/agent-config key
    ``git_execution_mode`` (and the workspace-metadata key of the same name)
    no longer influence resolution -- a stale config value is popped on read by
    ``_migrate_legacy_git_execution_mode`` (logged when it was ``"host"``).

    Returns:
        "containerized": git runs inside the workspace resource container.
        "host": git runs on the host inside the hermetic sandbox.
        "unavailable": no resolvable workspace to run against.
    """
    _migrate_legacy_git_execution_mode(agent_config)

    entry = catalog_entry("git") or {}
    mode = entry.get("execution_mode")
    effective_mode = mode if mode in ("container", "host") else "container"

    if not resolved_workspace_path:
        return "unavailable"
    if effective_mode == "host" or not resolved_workspace_id:
        return "host"
    return "containerized"


def _decode_status_path(token: str) -> bytes:
    """Decode a porcelain-v1 path token (C-quoted OR raw UTF-8) to bytes."""
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        try:
            return token[1:-1].encode("latin-1").decode("unicode_escape").encode("latin-1")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    return token.encode("utf-8")


def _nfc_key(path_bytes: bytes) -> bytes:
    """NFC-normalize a path; BOTH comparison sides use this (symmetric)."""
    try:
        return unicodedata.normalize("NFC", path_bytes.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        return path_bytes


def _reconcile_status_paths(output: str) -> str:
    """Collapse Unicode-equivalent delete/untracked pairs in porcelain v1.

    When ONE logical path is spelled Unicode-equivalently but byte-distinct in
    the index and the worktree, git reports it twice (a `` D`` for the index
    spelling plus a ``??`` for the worktree spelling); the two rows are dropped
    together. Pairing is keyed on git's own index-derived deletion report, never
    a platform setting, so an unpaired ``??`` always survives.
    """
    parsed, deletions, untracked = [], set(), set()
    for line in output.splitlines():
        if len(line) < 4 or line[2] != " ":
            parsed.append(("", b"", line))
            continue
        status = line[:2]
        path_bytes = _decode_status_path(line[3:])
        if status == "??":
            key = _nfc_key(path_bytes)
            untracked.add(key)
        elif status.strip() == "D":
            key = _nfc_key(path_bytes)
            deletions.add(key)
        else:
            key = b""
        parsed.append((status, key, line))
    paired = deletions & untracked
    if not paired:
        return output
    return "\n".join(
        line
        for status, key, line in parsed
        if not (key in paired and (status == "??" or status.strip() == "D"))
    )


class GitReadTool(ToolBase):
    """
    Read-only git repository inspection tool.

    Operations: status, diff, diff_cached, log, branch, branch_list, show,
    remote, blame, config, rev_parse, show_ref, for_each_ref, ls_tree,
    cat_file. Write operations (commit, init, clone,
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
    name: ClassVar[str] = "git_read"

    # Bound resource: git operations run inside the hidden git resource
    # container, so execution requires the ``git:read`` grain (checked by
    # security_gate.check_requires_resource against RESOURCE_REGISTRY).
    requires_resource: ClassVar[Optional[str]] = "git"

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

    def _host_execution_denied_reason(self) -> Optional[str]:
        """Return a deny reason when the workspace ceiling blocks host git.

        Host-side git execution remains gated on the workspace top-level
        ``allow_host_resources`` key (see ``tools.host_resource_policy``).
        With no resolvable workspace id there is no workspace ceiling to
        consult, so the legacy host path is DENIED fail-closed: an unbound
        workspace id is a production-reachable state, and allowing host git
        there would bypass the workspace policy entirely.  Any policy-lookup
        failure is likewise fail-CLOSED: a helper import/read error DENIES
        host execution rather than silently allowing it, matching the rest
        of the permission layer
        (``tools.host_resource_policy.workspace_allows_host_resources`` whose
        reader returns ``False`` on error, and the
        ``security_gate.get_effective_permissions`` host_bash override which
        bans on any reader error).
        """
        ws_id = self._resolved_workspace_id or getattr(self, "workspace_id", None)
        if not ws_id:
            return (
                "GitReadTool: host-side git execution denied; no workspace id "
                "is bound to this session, so no host-resource policy can be "
                "resolved for host-side git"
            )
        try:
            from tools.host_resource_policy import workspace_allows_host_resources

            allowed = workspace_allows_host_resources(ws_id)
        except Exception:
            return (
                "GitReadTool: host-side git execution denied; the "
                "allow_host_resources policy could not be resolved for "
                f"workspace {ws_id} (workspaces/{ws_id}/config.json)"
            )
        if allowed:
            return None
        return (
            "GitReadTool: host-side git execution requires "
            "allow_host_resources: true in the workspace config "
            f"(workspaces/{ws_id}/config.json)"
        )

    def _kill_switch_state(self) -> str:
        """Return the workspace host-resource kill-switch state ("on"/"off").

        "on"  -> the workspace ceiling allows host-side git execution.
        "off" -> host execution is denied (fail-closed) for this workspace.
        """
        return "off" if self._host_execution_denied_reason() else "on"

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return dynamic permission categories based on the git operation.

        This tool is read-only: every operation, including ``remote``, requires
        only ``git:read``. ``remote`` runs ``git remote -v`` and never contacts
        a remote, so it needs no network egress.
        """
        return ["git:read"]

    tool: Literal["GitInfoTool"] = "GitInfoTool"

    
    operation: Literal[
        "status", "diff", "diff_cached", "log", "branch", "branch_list",
        "show", "remote", "blame", "config",
        "rev_parse", "show_ref", "for_each_ref", "ls_tree", "cat_file",
        "merge_base", "stash_list", "reflog", "show_file",
    ] = Field(
        description="Git read operation to perform: status, diff, diff_cached, log, "
        "branch, branch_list, show, remote, blame, config, rev_parse, "
        "show_ref, for_each_ref, ls_tree, cat_file, merge_base, stash_list, "
        "reflog, show_file"
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

    # rev_parse parameters
    ref: Optional[str] = Field(
        default="HEAD",
        description="Ref for rev_parse (default HEAD); a commit SHA or ref name, "
        "never '-...'."
    )
    abbrev: bool = Field(
        default=False,
        description="Use --abbrev-ref for rev_parse."
    )

    # show_ref parameters
    pattern: Optional[str] = Field(
        default=None,
        description="Ref pattern for show_ref (optional)."
    )

    # for_each_ref parameters
    prefix: Optional[str] = Field(
        default=None,
        description="Ref prefix for for_each_ref (optional)."
    )

    # ls_tree parameters
    treeish: Optional[str] = Field(
        default="HEAD",
        description="Tree-ish for ls_tree (default HEAD)."
    )
    recursive: bool = Field(
        default=False,
        description="Recurse subtrees for ls_tree (-r)."
    )
    path: Optional[str] = Field(
        default=None,
        description="Single path to scope ls_tree."
    )

    # cat_file parameters
    object: Optional[str] = Field(
        default=None,
        description="Object name/ref for cat_file."
    )
    type: Optional[str] = Field(
        default=None,
        description="Object type for cat_file: blob|tree|commit|tag "
        "(default pretty-print -p)."
    )

    # merge_base parameters
    a: Optional[str] = Field(
        default=None,
        description="First ref for merge_base operation."
    )
    b: Optional[str] = Field(
        default=None,
        description="Second ref for merge_base operation."
    )
    is_ancestor: bool = Field(
        default=False,
        description="Use --is-ancestor for merge_base (exit 1 = not an "
        "ancestor)."
    )

    # reflog parameters (reuses the ``ref`` field)
    limit: Optional[int] = Field(
        default=20,
        description="Maximum number of reflog entries (clamped to 1..1000)."
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

    def _normalise_working_dir(self, working_dir: str) -> str:
        """Accept the workspace's OWN container mount as a valid working_dir.

        An agent that speaks container paths (``/workspace`` and anything
        below it) must be able to target the very workspace it is bound to;
        the ``working_dir`` param is otherwise host-pathed, so ``/workspace``
        was rejected as "outside workspace". A clean mount path is mapped back
        to the canonical host workspace root through the existing
        ``_from_container_path`` helper, so behaviour is identical to passing
        that host path.

        Anything that is not an unambiguous mount path is returned UNCHANGED
        so the existing validator still rejects it with its byte-for-byte
        message: host paths, ``/workspace``-lookalikes (e.g. ``/workspaceX``)
        and -- critically -- any ``..`` escape such as
        ``/workspace/../outside``.
        """
        raw = str(working_dir).strip()
        mount = (getattr(self, "container_workspace_path", None) or "/workspace")
        mount = mount.rstrip("/") or "/workspace"
        if not raw.startswith("/"):
            return working_dir
        # Classify lexically: reject any '..' and any boundary that is not the
        # mount itself nor a descendant of it (drops redundant separators and
        # a trailing slash in the process).
        segs = [s for s in raw.split("/") if s not in ("", ".")]
        if any(s == ".." for s in segs):
            return working_dir
        norm = "/" + "/".join(segs)
        if norm != mount and not norm.startswith(mount + "/"):
            return working_dir
        ws_path = self._resolved_workspace_path or self._resolve_registry_workspace()
        if not ws_path:
            return working_dir
        self._resolved_workspace_path = ws_path
        try:
            return str(self._from_container_path(norm))
        except (ValueError, TypeError):
            return working_dir

    def execute(self) -> str:
        # Reset per-call runtime state (tool instances may be reused).
        self._resource_manager = None
        self._resolved_workspace_path = None
        self._resolved_workspace_id = None
        self._last_execution_mode = None
        self._last_failure_reason = None

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
            except GitUnavailableError as e:
                # The git binary is missing / could not be spawned: an
                # availability failure, NOT a "not a repository" condition.
                return self._truncate_output(
                    f"git executable not available: {e}"
                )
            except (subprocess.TimeoutExpired, TimeoutError) as e:
                # A hung git binary is also an availability failure; never
                # report it as "not a git repository".
                return self._truncate_output(
                    f"git executable not available (timed out): {e}"
                )
            except FileNotFoundError as e:
                # Defensive parity with the previous spawn-error handling: an
                # unwrapped "git not found" is still an availability failure.
                return self._truncate_output(
                    f"git executable not available: {e}"
                )
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
            elif self.operation == "rev_parse":
                return self._git_rev_parse(repo_root)
            elif self.operation == "show_ref":
                return self._git_show_ref(repo_root)
            elif self.operation == "for_each_ref":
                return self._git_for_each_ref(repo_root)
            elif self.operation == "ls_tree":
                return self._git_ls_tree(repo_root)
            elif self.operation == "cat_file":
                return self._git_cat_file(repo_root)
            elif self.operation == "merge_base":
                return self._git_merge_base(repo_root)
            elif self.operation == "stash_list":
                return self._git_stash_list(repo_root)
            elif self.operation == "reflog":
                return self._git_reflog(repo_root)
            elif self.operation == "show_file":
                return self._git_show_file(repo_root)
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
    ) -> str:
        """Run git command and return output.

        ``_run_git_raw`` selects the backend: the workspace resource container
        when container mode is active and the git resource is available,
        otherwise the hardened host backend (config/catalog host mode). There
        is NO silent fallback: an unavailable OR degraded container resource
        raises RuntimeError, so the host backend (which injects ``--no-verify``
        / ``core.hooksPath=/dev/null``) can never silently bypass the QA gate.
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
            )
            if exit_code != 0:
                return f"Git command failed (exit code {exit_code}):\n{stderr}"
            return stdout
        except subprocess.TimeoutExpired:
            # Fail closed: a timeout must surface as an exception, never as a
            # returned string. Callers parse return values as branch names or
            # refs; a returned "timed out" string would be treated as a valid
            # branch and open a security gate. Raising escapes both gates.
            raise
        except TimeoutError:
            # Fail closed, same reason as TimeoutExpired above.
            raise
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
    ) -> tuple:
        """Execute git in the active execution mode.

        Returns ``(exit_code, stdout, stderr)``. Dispatches between the host
        hermetic sandbox and the workspace resource container based on
        ``_use_container_mode()``. No path validation is performed here --
        that lives in ``_run_git`` so internal callers (e.g.
        ``_git_repo_root``) do not re-validate.

        Host mode (``_use_container_mode()`` is False) is a first-class,
        config/catalog-selected mode: ``_run_git_raw`` runs the hardened host
        backend directly, gated by the host-resource kill switch
        (``_host_execution_denied_reason``).

        Container mode is self-healing: ``_resolve_resource_execution()``
        consults ``ensure_resource("git")`` at execution time and honors the
        ACTUAL resource mode. There is NO silent fallback: an unavailable
        resource OR a resource that degraded (docker/image outage) raises a
        LOUD RuntimeError naming the resource and the reason, so a broken
        container can never silently bypass the container QA gate / hooks.
        """
        if not self._use_container_mode():
            # Host mode: config/catalog selects host execution. Fail closed on
            # the workspace host-resource kill switch.
            denied = self._host_execution_denied_reason()
            if denied:
                self._last_execution_mode = "unavailable"
                self._last_failure_reason = denied
                raise RuntimeError(denied)
            self._last_execution_mode = "host"
            self._last_failure_reason = None
            logger.info(
                "GitReadTool effective git execution mode: host "
                "(operation=%s, workspace_id=%s)",
                self.operation,
                self._resolved_workspace_id or "none",
            )
            return self._exec_host_raw(repo_root, args, timeout=timeout)

        mode, manager = self._resolve_resource_execution()
        effective = mode.get("mode")
        detail = mode.get("detail", "")
        failure_reason = mode.get("failure_reason")
        if effective == "containerized" and manager is not None:
            logger.info(
                "GitReadTool effective git execution mode: containerized "
                "(operation=%s, workspace_id=%s)",
                self.operation,
                self._resolved_workspace_id or "none",
            )
            self._last_execution_mode = "containerized"
            self._last_failure_reason = failure_reason
            return self._exec_container_raw(
                repo_root, args, timeout=timeout, manager=manager
            )
        # "unavailable" OR degraded ("host_fallback"): FAIL LOUD. There is no
        # host fallback any more -- a container-mode call whose resource is
        # missing or degraded must not degrade to the host backend.
        logger.error(
            "GitReadTool git resource unavailable: %s (operation=%s)",
            detail,
            self.operation,
        )
        self._last_execution_mode = "unavailable"
        self._last_failure_reason = failure_reason
        if failure_reason:
            raise RuntimeError(
                f"GitReadTool: containerized git execution unavailable: "
                f"{detail} (failure_reason: {failure_reason})"
            )
        raise RuntimeError(
            f"GitReadTool: containerized git execution unavailable: {detail}"
        )

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
        if self.session_permissions is None:
            # Fail closed: an unresolved (None) session must never reach the
            # host sandbox. Authorising on an unknown session is a bypass, and
            # no git subprocess may be spawned for such a call.
            logger.warning(
                "GitReadTool host-side git execution refused: "
                "session_permissions_unresolved (operation=%s)",
                self.operation,
            )
            raise RuntimeError(
                "GitReadTool: session_permissions_unresolved - refusing "
                "host-side git execution without resolved session permissions"
            )
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
        if self.session_permissions is None:
            # Fail closed: an unresolved (None) session must never reach the
            # resource container. Authorising on an unknown session is a bypass.
            logger.warning(
                "GitReadTool containerized git execution refused: "
                "session_permissions_unresolved (operation=%s)",
                self.operation,
            )
            raise PermissionError(
                "GitReadTool: session_permissions_unresolved - refusing "
                "containerized git execution without resolved session permissions"
            )
        level = self._get_operation_level(args)
        effective = self.effective_permissions or {}
        if effective.get("git") != "ask":
            from security.security_gate import (
                _ceiling_denial_note,
                check_atomic_operation,
            )

            if not check_atomic_operation(
                f"git:{level}",
                effective,
                "GitReadTool",
                f"git {' '.join(args)}",
            ):
                note = _ceiling_denial_note("git", level, effective)
                raise PermissionError(
                    f"Permission denied: git:{level} required for this operation"
                    + (f" (workspace ceiling: {note})" if note else "")
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

        Returns ``None`` ONLY when git ran and reported that ``repo_root`` is
        not inside a repository. When the git executable itself cannot be run
        (missing binary, exit 127, or an unspawnable process) this raises
        ``GitUnavailableError`` instead, so an unavailable git is never
        conflated with a genuine "not a git repository" result.
        """
        dot_git = repo_root / ".git"
        if dot_git.exists() and dot_git.is_dir():
            return repo_root

        try:
            exit_code, stdout, stderr = self._run_git_raw(
                repo_root, ["rev-parse", "--show-toplevel"], timeout=10
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            # The git binary could not be spawned at all -- not a repository
            # problem. Surface it as unavailability so the caller never emits
            # the misleading "Not a git repository" message.
            raise GitUnavailableError(
                f"git command could not be executed: {e}"
            ) from e
        if exit_code != 0 or not stdout.strip():
            if _git_run_indicates_missing_git(exit_code, stderr):
                raise GitUnavailableError(
                    f"git command failed (exit code {exit_code}): "
                    f"{(stderr or '').strip() or 'no output'}"
                )
            return None
        if self._use_container_mode():
            return self._from_container_path(stdout.strip())
        return Path(stdout.strip())

    def _git_execution_mode(self) -> str:
        """Return 'host' or 'container' for git execution.

        The mode is the ``execution_mode`` of the resource catalog's ``git``
        entry.  The retired session/agent-config key ``git_execution_mode`` and
        the workspace-metadata key of the same name no longer influence
        resolution (any stale config value is popped on read by
        ``_migrate_legacy_git_execution_mode``). Container mode additionally
        requires a registry-derived workspace (enforced by
        ``_use_container_mode()``).
        """
        _migrate_legacy_git_execution_mode(getattr(self, "agent_config", None))

        entry = catalog_entry("git") or {}
        mode = entry.get("execution_mode")
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

        Container mode requires (a) the resource catalog ``git`` entry's
        ``execution_mode`` to be ``"container"`` AND (b) a registry-derived
        workspace (id + path). The registry requirement keeps deprecated
        ``workspace_path`` callers and direct test invocations on the host
        path, so tests without a docker daemon never enter container mode.
        """
        return (
            self._git_execution_mode() == "container"
            and bool(self._resolved_workspace_path)
            and bool(self._resolved_workspace_id)
        )

    def _resolve_resource_execution(self) -> tuple:
        """Resolve the ACTUAL git resource execution mode at runtime.

        Returns ``(mode_dict, manager_or_None)``. ``mode_dict`` carries
        ``mode`` (see the two vocabularies below) and ``detail``
        (human-readable reason). ``manager_or_None`` is the live
        ``ResourceContainerManager`` when the resolved mode is
        ``containerized``, else ``None``.

        Two vocabularies meet here. The config-selected (host) branch returns
        the tool-side vocabulary used by ``resolve_git_execution_mode``:
        ``"containerized" | "host" | "unavailable"``. The container-mode
        branch returns the infra/resource-manager vocabulary passed through
        UNCHANGED: ``"containerized" | "host_fallback" | "unavailable"`` -- so
        a docker/image outage surfaces as ``host_fallback`` (NOT ``host``),
        while a policy denial or unknown resource surfaces as ``unavailable``.

        Config-level ``_use_container_mode()`` decides whether the container
        path is *desired*; ``ensure_resource("git")`` then self-heals
        (auto-build image, recreate stale containers) and reports the mode
        that is actually achievable. ``_run_git_raw`` treats only
        ``containerized`` as usable and raises loudly on anything else.
        Never raises.
        """
        if not self._use_container_mode():
            return (
                {
                    "mode": "host",
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
                from security.security_gate import (
                    ContainerConfig,
                    get_workspace_capabilities,
                    resolve_container_config,
                )
                from thoughtmachine.container_record import LIFECYCLE_RESOURCE

                # Never fabricate a "default" workspace id (loading caps for a
                # made-up workspace is misleading).  With no real id we pass
                # ``capabilities=None`` so the resolver returns
                # ``ContainerConfigError("capabilities_required")`` and the
                # ``network_mode`` below stays the locked-down "none".
                workspace_id = (
                    self._resolved_workspace_id or self.workspace_id or None
                )
                capabilities = (
                    get_workspace_capabilities(workspace_id)
                    if workspace_id is not None
                    else None
                )
                expected = resolve_container_config(
                    self.session_permissions or {}, capabilities, LIFECYCLE_RESOURCE
                )
                network_mode = (
                    expected.network_mode
                    if isinstance(expected, ContainerConfig)
                    else "none"
                )
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
                session_id=getattr(self, "session_id", None),
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
        """Run git status, reconciling Unicode-equivalent path spellings."""
        output = self._run_git(
            repo_root,
            ["status", "--porcelain=v1"],
        )
        if output.startswith("Git command failed"):
            # Try human-readable status
            output = self._run_git(repo_root, ["status"])
        else:
            output = _reconcile_status_paths(output)
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
        try:
            raw_max = self.max_count if self.max_count is not None else 50
            max_count = max(1, min(1000, int(raw_max)))
        except (TypeError, ValueError):
            return self._truncate_output("Error: max_count must be an integer")
        args = ["log", "--no-ext-diff", "--no-textconv", f"--max-count={max_count}", "--oneline"]
        if self.since:
            args.append(f"--since={self.since}")
        if self.until:
            args.append(f"--until={self.until}")
        if self.author:
            args.append(f"--author={self.author}")
        if self.grep:
            args.append(f"--grep={self.grep}")
        if self.branch:
            # The revision is now HONORED: a single validated argv element
            # AFTER the option flags and BEFORE the '--' path separator. An
            # unknown revision yields a non-zero exit surfaced as the standard
            # error form, never silently ignored.
            try:
                args.append(self._validate_git_ref(self.branch, field="branch"))
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
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

        Appends two trailing lines — ``execution_mode`` (containerized |
        host | unavailable) and ``failure_reason`` (why a containerized
        resource could not be used, or ``none``) — so every operation
        reports how it actually executed.
        """
        return (
            f"{output}\nexecution_mode: {self._last_execution_mode or 'unavailable'}"
            f"\nfailure_reason: {self._last_failure_reason or 'none'}"
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
        """Run git show, honouring file scope and an optional line range.

        ``git show`` has no line-range selector, so a requested line range is
        honoured by reading the file's blob at ``<commit>:<path>`` and slicing
        the requested lines. Any ``file_path``/``line_start``/``line_end``
        argument that cannot be honoured is reported as an explicit error --
        never silently dropped (defect A1).
        """
        range_requested = self.line_start is not None or self.line_end is not None
        file_path = self.file_path

        # Any range/file argument that cannot be honoured must fail loudly
        # instead of being silently dropped (defect A1).
        if range_requested:
            if file_path is None:
                return self._truncate_output(
                    "Error: show: line_start/line_end require file_path "
                    "(a line range is only honoured for a single scoped file)."
                )
            if self.line_start is None or self.line_end is None:
                return self._truncate_output(
                    "Error: show: both line_start and line_end are required "
                    "together for a line range."
                )
            if (
                self.line_start < 1
                or self.line_end < 1
                or self.line_start > self.line_end
            ):
                return self._truncate_output(
                    "Error: show: invalid line range; require "
                    "1 <= line_start <= line_end."
                )
            if isinstance(file_path, list):
                return self._truncate_output(
                    "Error: show: a line range requires a single file_path "
                    "(got a list)."
                )

        rel_paths: List[str] = []
        if file_path is not None:
            try:
                rel_paths = self._validated_rel_paths(repo_root, file_path)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")

        if range_requested:
            # git show cannot select a line range; read the scoped blob at
            # <commit>:<path> and slice the requested lines.
            args = ["show", "--no-ext-diff", "--no-textconv"]
            if self.format:
                args.append(f"--format={self.format}")
            args.append(f"{self.commit}:{rel_paths[0]}")
            exit_code, stdout, stderr = self._run_git_raw(
                repo_root, args, timeout=30
            )
            if exit_code != 0:
                # Same git-error convention as _run_git / the other handlers;
                # never slice an error string as if it were file content.
                return self._with_mode(self._truncate_output(
                    f"Git command failed (exit code {exit_code}):\n{stderr}"
                ))
            lines = stdout.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            sliced = "\n".join(lines[self.line_start - 1:self.line_end])
            return self._with_mode(self._truncate_output(sliced))

        args = ["show", "--no-ext-diff", "--no-textconv"]
        if self.format:
            args.append(f"--format={self.format}")
        args.append(self.commit)
        if rel_paths:
            args.append("--")
            args.extend(rel_paths)
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

    # ------------------------------------------------------------------
    # Additional read operations: rev_parse, show_ref, for_each_ref,
    # ls_tree, cat_file. Every one obeys the fixed-argv contract: no
    # agent-supplied flags, a leading '-' is rejected, ref/pattern/format
    # tokens are validated BEFORE any git call, and non-zero exits are
    # surfaced via the standard "Git command failed (exit code N):" form.
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_git_ref(value: str, *, field: str) -> str:
        """Validate a ref/object/tree-ish token used as one fixed argv element.

        Rejects non-strings, empty or surrounding-whitespace values, any value
        beginning with '-' (which git would parse as a flag) and any value
        containing whitespace or a control character. Raises ``ValueError``;
        callers convert it to an ``Error:`` string without spawning git.
        """
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        if value == "" or value.strip() != value:
            raise ValueError(f"invalid {field}: {value!r}")
        if value.startswith("-"):
            raise ValueError(
                f"invalid {field}: {value!r} (must not start with '-')"
            )
        for ch in value:
            if ch.isspace() or ord(ch) < 0x20:
                raise ValueError(
                    f"invalid {field}: {value!r} (must not contain whitespace "
                    "or control characters)"
                )
        return value

    @staticmethod
    def _validate_ref_pattern(value: str, *, field: str) -> str:
        """Validate a ref pattern/prefix (show_ref, for_each-ref).

        Applies the ``_validate_git_ref`` rules and additionally rejects any
        character in ``_REF_PATTERN_FORBIDDEN`` (shell metacharacters) so the
        token can never be re-interpreted outside argv. Raises ``ValueError``.
        """
        validated = GitReadTool._validate_git_ref(value, field=field)
        if any(ch in _REF_PATTERN_FORBIDDEN for ch in validated):
            raise ValueError(
                f"invalid {field}: {value!r} (must not contain shell "
                "metacharacters)"
            )
        return validated

    @staticmethod
    def _validate_for_each_ref_format(fmt: str) -> str:
        """Validate a ``for-each-ref --format`` string.

        The format travels as a single ``--format=<fmt>`` argv element (never
        shell-interpreted). Structural rules: non-empty string, no newlines and
        no ``_REF_PATTERN_FORBIDDEN`` characters; then every '%' must open a
        ``%(...)`` token whose atom is in ``_FOR_EACH_REF_ALLOWED_TOKENS``.
        Literal text (without metacharacters or a bare '%') is allowed. Raises
        ``ValueError`` naming the allowed atoms otherwise.
        """
        if not isinstance(fmt, str) or fmt == "":
            raise ValueError("format must be a non-empty string")
        if "\n" in fmt or "\r" in fmt:
            raise ValueError("invalid format: must not contain newlines")
        if any(ch in _REF_PATTERN_FORBIDDEN for ch in fmt):
            raise ValueError(
                f"invalid format: {fmt!r} (must not contain shell "
                "metacharacters)"
            )
        allowed = ", ".join(sorted(_FOR_EACH_REF_ALLOWED_TOKENS))
        i, n = 0, len(fmt)
        while i < n:
            if fmt[i] != "%":
                i += 1
                continue
            if i + 1 >= n or fmt[i + 1] != "(":
                raise ValueError(
                    f"invalid format: bare '%' at position {i}; every '%' must "
                    "begin a '%(token)' atom"
                )
            close = fmt.find(")", i + 2)
            if close == -1:
                raise ValueError(
                    f"invalid format: unterminated '%(' token at position {i}"
                )
            token = fmt[i + 2:close]
            if token not in _FOR_EACH_REF_ALLOWED_TOKENS:
                raise ValueError(
                    f"invalid format: unsupported atom '%({token})'; allowed "
                    f"atoms are: {allowed}"
                )
            i = close + 1
        return fmt

    def _git_rev_parse(self, repo_root: Path) -> str:
        """Run git rev-parse, optionally --abbrev-ref, on a ref/SHA."""
        try:
            ref = self._validate_git_ref(self.ref, field="ref")
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        args = ["rev-parse"]
        if self.abbrev:
            args.append("--abbrev-ref")
        args.append(ref)
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_show_ref(self, repo_root: Path) -> str:
        """Run git show-ref; an empty match (exit 1) is benign, not an error."""
        args = ["show-ref"]
        if self.pattern is not None:
            try:
                args.append(
                    self._validate_ref_pattern(self.pattern, field="pattern")
                )
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
        exit_code, stdout, stderr = self._run_git_raw(repo_root, args)
        if exit_code == 1 and not stdout.strip():
            return self._with_mode(self._truncate_output("No matching refs."))
        if exit_code != 0:
            return self._with_mode(
                self._truncate_output(
                    f"Git command failed (exit code {exit_code}):\n{stderr}"
                )
            )
        return self._with_mode(self._truncate_output(stdout))

    def _git_for_each_ref(self, repo_root: Path) -> str:
        """Run git for-each-ref with an optional validated format/prefix."""
        args = ["for-each-ref"]
        if self.format is not None:
            try:
                fmt = self._validate_for_each_ref_format(self.format)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            args.append(f"--format={fmt}")
        if self.prefix is not None:
            try:
                prefix = self._validate_ref_pattern(self.prefix, field="prefix")
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            args.append(prefix)
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_ls_tree(self, repo_root: Path) -> str:
        """Run git ls-tree with optional recursion and path scope."""
        try:
            treeish = self._validate_git_ref(self.treeish, field="treeish")
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        args = ["ls-tree"]
        if self.recursive:
            args.append("-r")
        args.append(treeish)
        if self.path:
            try:
                rels = self._validated_rel_paths(repo_root, self.path)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            if rels:
                args.append("--")
                args.extend(rels)
        output = self._run_git(repo_root, args)
        return self._with_mode(self._truncate_output(output))

    def _git_merge_base(self, repo_root: Path) -> str:
        """Run git merge-base on two validated refs.

        Exit code 1 is MEANINGFUL and is never mapped to empty/None: for the
        plain form it means no common ancestor; for ``--is-ancestor`` it means
        the first ref is NOT an ancestor of the second. Any other non-zero
        exit is the standard error form.
        """
        if not self.a or not self.b:
            return self._truncate_output(
                "Error: a and b are required for merge_base operation"
            )
        try:
            a = self._validate_git_ref(self.a, field="a")
            b = self._validate_git_ref(self.b, field="b")
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        args = ["merge-base"] + (
            ["--is-ancestor"] if self.is_ancestor else []
        ) + [a, b]
        exit_code, stdout, stderr = self._run_git_raw(repo_root, args)
        if self.is_ancestor:
            if exit_code == 0:
                return self._with_mode(
                    self._truncate_output(f"{a!r} is an ancestor of {b!r}.")
                )
            if exit_code == 1:
                return self._with_mode(
                    self._truncate_output(f"{a!r} is not an ancestor of {b!r}.")
                )
        else:
            if exit_code == 1:
                return self._with_mode(
                    self._truncate_output("No common ancestor (no merge base).")
                )
        if exit_code != 0:
            return self._with_mode(
                self._truncate_output(
                    f"Git command failed (exit code {exit_code}):\n{stderr}"
                )
            )
        return self._with_mode(self._truncate_output(stdout))

    def _git_stash_list(self, repo_root: Path) -> str:
        """Run git stash list; empty output is benign, non-zero is an error."""
        exit_code, stdout, stderr = self._run_git_raw(repo_root, ["stash", "list"])
        if exit_code != 0:
            return self._with_mode(
                self._truncate_output(
                    f"Git command failed (exit code {exit_code}):\n{stderr}"
                )
            )
        if not stdout.strip():
            return self._with_mode(self._truncate_output("No stashes."))
        return self._with_mode(self._truncate_output(stdout))

    def _git_reflog(self, repo_root: Path) -> str:
        """Run git reflog on a validated ref with a clamped entry limit."""
        try:
            ref = self._validate_git_ref(self.ref or "HEAD", field="ref")
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        try:
            limit = int(self.limit) if self.limit is not None else 20
        except (TypeError, ValueError):
            return self._truncate_output("Error: limit must be an integer")
        limit = max(1, min(1000, limit))
        args = ["reflog", f"-n{limit}", ref]
        exit_code, stdout, stderr = self._run_git_raw(repo_root, args)
        if exit_code != 0:
            return self._with_mode(
                self._truncate_output(
                    f"Git command failed (exit code {exit_code}):\n{stderr}"
                )
            )
        if not stdout.strip():
            return self._with_mode(self._truncate_output("No reflog entries."))
        return self._with_mode(self._truncate_output(stdout))

    def _git_show_file(self, repo_root: Path) -> str:
        """Show a single tracked file at a ref (``git show <ref>:<path>``).

        A non-zero exit is the explicit "path not tracked at ref" signal and is
        surfaced as the standard error form; exit 0 with empty stdout is a
        legitimately empty file and is returned as empty content, not an error.
        """
        if not self.ref:
            return self._truncate_output(
                "Error: ref is required for show_file operation"
            )
        if not self.path:
            return self._truncate_output(
                "Error: path is required for show_file operation"
            )
        try:
            ref = self._validate_git_ref(self.ref, field="ref")
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        try:
            rels = self._validated_rel_paths(repo_root, self.path)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        rel = rels[0]
        exit_code, stdout, stderr = self._run_git_raw(
            repo_root,
            ["show", "--no-ext-diff", "--no-textconv", f"{ref}:{rel}"],
        )
        if exit_code != 0:
            return self._with_mode(
                self._truncate_output(
                    f"Git command failed (exit code {exit_code}):\n{stderr}"
                )
            )
        return self._with_mode(self._truncate_output(stdout))

    def _git_cat_file(self, repo_root: Path) -> str:
        """Run git cat-file (-p by default, or a validated object type)."""
        if not self.object:
            return self._truncate_output(
                "Error: object is required for cat_file operation"
            )
        try:
            obj = self._validate_git_ref(self.object, field="object")
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        mode = "-p"
        if self.type is not None:
            if self.type not in _CAT_FILE_ALLOWED_TYPES:
                allowed = ", ".join(sorted(_CAT_FILE_ALLOWED_TYPES))
                return self._truncate_output(
                    f"Error: invalid type {self.type!r}; allowed types are: "
                    f"{allowed}"
                )
            mode = self.type
        output = self._run_git(repo_root, ["cat-file", mode, obj])
        return self._with_mode(self._truncate_output(output))

# Backward-compatible alias: legacy code, imports and tests reference the old
# class name; the canonical identifier is now ``GitReadTool`` with the stable
# tool name ``git_read``.
GitInfoTool = GitReadTool
