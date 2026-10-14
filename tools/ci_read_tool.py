# tools/ci_read_tool.py
"""Read-only CI (GitHub Actions) inspection tool.

``CiReadTool`` answers "what is the CI state for <branch|PR>?" for the
workspace repository by performing a SINGLE in-process HTTPS GET against the
GitHub Actions runs endpoint (``GET /repos/{owner}/{repo}/actions/runs``).

Containment / egress posture
-----------------------------
* Exactly ONE network request per call, made in-process via
  ``urllib.request`` (``_http_get`` -- the sole egress seam, monkeypatchable).
  No shell, no ``subprocess`` for the HTTP call.
* NO credentials are ever sent: the request carries NO ``Authorization``
  header and NO token.  The only headers are a non-credential ``Accept``
  (``application/vnd.github+json``) and a non-credential ``User-Agent``
  (``thoughtmachine-ci-read``); the public API returns public-repo data
  anonymously (unauthenticated calls are rate-limited by GitHub).
* The repository owner/name are derived from the workspace's ``origin`` remote
  URL via a read-only ``git remote get-url origin`` (``_resolve_owner_repo``,
  monkeypatchable) -- a bare in-process host ``git remote get-url origin``,
  NOT routed through ``GitReadTool``'s container/host execution-mode
  backends nor its host-resource (``allow_host_resources``) ceiling.
  The git lookup never contacts a remote and runs no hooks.

Permission model
----------------
The tool does NOT declare ``requires_resource`` (setting it to a resource name
absent from ``agent/config/defaults.RESOURCE_REGISTRY`` -- which contains only
``git`` -- would make ``security_gate.check_requires_resource`` deny every
call fail-closed).  Instead the read is gated through the permission category
gate: ``get_required_categories`` returns ``["ci:read"]``, checked by
``security_gate.check_required_categories`` against the effective session
permissions (session grant ``ci: read`` allows it; the default ``ci: banned``
denies it).
"""

from typing import ClassVar, List, Literal, Optional, Tuple
from pydantic import Field

import json
import logging
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import ToolBase


logger = logging.getLogger(__name__)

#: GitHub REST API base (single, fixed egress host).
_API_ROOT = "https://api.github.com"

#: Non-credential User-Agent identifying this tool to the API.
_USER_AGENT = "thoughtmachine-ci-read"

#: Default / maximum ``per_page`` for the runs listing.
_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100

#: Timeout (seconds) for the single HTTPS GET.
_HTTP_TIMEOUT = 30


def _http_get(url: str, headers: dict) -> dict:
    """Perform a SINGLE in-process HTTPS GET and return the parsed JSON body.

    This is the tool's ONLY egress seam and is deliberately a module-level
    function so tests can monkeypatch it, assert exactly one call, and capture
    the URL and headers (to prove no ``Authorization``/token is ever sent).

    Raises ``urllib.error.HTTPError`` / ``urllib.error.URLError`` (and
    ``json.JSONDecodeError``) on failure -- the caller turns those into a
    returned error string.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310 - fixed https host
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def parse_origin_owner_repo(url: str) -> Optional[Tuple[str, str]]:
    """Parse ``(owner, repo)`` out of a git ``origin`` URL, or None.

    Supports the common remote spellings:

    * ``https://github.com/owner/repo`` / ``...repo.git``
    * ``http://host/owner/repo.git``
    * ``ssh://git@host/owner/repo.git`` (scheme + optional ``user@``)
    * ``git@github.com:owner/repo.git`` (scp-like ``host:path``)

    A trailing ``.git`` is stripped; extra leading path segments (e.g. a GitLab
    subgroup) collapse to the last two components, which is what the GitHub
    REST API's ``owner/repo`` pair expects.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None

    if "://" not in text:
        # scp-like ``user@host:path`` or a bare ``host:path``.
        if ":" in text:
            path = text.split(":", 1)[1]
        else:
            path = text
    else:
        rest = text.split("://", 1)[1]
        # Drop ``user@`` userinfo when present.
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        # Drop the host segment.
        if "/" not in rest:
            return None
        path = rest.split("/", 1)[1]

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    if not owner or not repo:
        return None
    return owner, repo


class CiReadTool(ToolBase):
    """
    Read-only CI (GitHub Actions) monitoring tool.

    Reports the workflow runs for the workspace repository, scoped to EITHER a
    branch OR a pull request (exactly one must be supplied).

    Parameters:
        branch: branch name to scope by (mutually exclusive with ``pr``).
        pr: pull-request number to scope by (mutually exclusive with ``branch``).
        per_page: max number of runs requested from the API (1..100).
        working_dir: repository root override (defaults to the workspace root).

    Containment: exactly one in-process ``GET`` (no shell/subprocess egress),
    no credentials sent; see the module docstring.
    """

    # Stable tool identifier: used in LLM schemas, preset lists and the
    # /api/tools endpoint. The internal ``tool`` Literal field below is
    # excluded from the LLM schema and kept for the class-name convention.
    name: ClassVar[str] = "ci_read"

    # This tool runs no hidden resource container. ``requires_resource`` stays
    # None: RESOURCE_REGISTRY (agent/config/defaults.py) contains only ``git``,
    # so any other value would be denied fail-closed by
    # security_gate.check_requires_resource. The read is gated by the
    # category gate instead (``ci:read``, see get_required_categories).
    requires_resource: ClassVar[Optional[str]] = None

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return the permission category this tool requires: ``ci:read``.

        Enforced by ``security_gate.check_required_categories`` against the
        effective session permissions: a session grant of ``ci: read`` allows
        the call, the default ``ci: banned`` denies it.
        """
        return ["ci:read"]

    tool: Literal["CiReadTool"] = "CiReadTool"

    branch: Optional[str] = Field(
        default=None,
        description="Branch name to scope CI runs by. Exactly one of "
        "'branch' or 'pr' must be supplied.",
    )
    pr: Optional[int] = Field(
        default=None,
        description="Pull-request number to scope CI runs by. Exactly one of "
        "'branch' or 'pr' must be supplied.",
    )
    per_page: Optional[int] = Field(
        default=_DEFAULT_PER_PAGE,
        description=f"Maximum number of workflow runs to request (1..{_MAX_PER_PAGE}).",
    )
    working_dir: Optional[str] = Field(
        default=None,
        description="Path to the git repository root (defaults to workspace root).",
    )

    # ------------------------------------------------------------------
    # Helpers (monkeypatch seams for tests)
    # ------------------------------------------------------------------

    def _resolve_repo_root(self) -> Path:
        """Resolve the repository root to read the ``origin`` remote from.

        Prefers an explicit ``working_dir`` (relative paths resolve against the
        registry workspace); otherwise the registry workspace; otherwise the
        process working directory.
        """
        ws_path = self._resolve_registry_workspace()
        if self.working_dir:
            candidate = Path(self.working_dir).expanduser()
            if not candidate.is_absolute() and ws_path:
                candidate = Path(ws_path) / candidate
        elif ws_path:
            candidate = Path(ws_path)
        else:
            candidate = Path.cwd()
        return candidate.resolve()

    def _resolve_origin_url(self, repo_root: Path) -> Optional[str]:
        """Return the ``origin`` remote URL for *repo_root*, or None.

        Read-only ``git remote get-url origin`` (no network, no hooks). Returns
        None when git is missing, the directory is not a repository, or no
        ``origin`` remote is configured.
        """
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        url = (proc.stdout or "").strip()
        return url or None

    def _resolve_owner_repo(self) -> Tuple[str, str]:
        """Resolve ``(owner, repo)`` from the workspace ``origin`` remote.

        Raises ``ValueError`` (turned into an error string by ``execute``) when
        the repository root, the ``origin`` remote, or its owner/repo cannot be
        determined. Overridable/monkeypatchable seam for tests.
        """
        repo_root = self._resolve_repo_root()
        origin = self._resolve_origin_url(repo_root)
        if not origin:
            raise ValueError(
                "could not determine the 'origin' remote URL for the repository "
                f"at {repo_root}"
            )
        parsed = parse_origin_owner_repo(origin)
        if parsed is None:
            raise ValueError(f"could not parse owner/repo from origin URL: {origin}")
        return parsed

    @staticmethod
    def _clamp_per_page(value: object) -> int:
        """Coerce/clamp ``per_page`` to ``1.._MAX_PER_PAGE``."""
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _DEFAULT_PER_PAGE
        return max(1, min(_MAX_PER_PAGE, number))

    @staticmethod
    def _build_runs_url(
        owner: str, repo: str, per_page: int, branch: Optional[str] = None
    ) -> str:
        """Build the actions-runs URL, URL-encoding owner/repo/branch."""
        base = (
            f"{_API_ROOT}/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(repo, safe='')}/actions/runs"
        )
        params: dict = {}
        if branch:
            params["branch"] = branch
        params["per_page"] = per_page
        return f"{base}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def _run_matches_pr(run: dict, pr: int) -> bool:
        """True when *run* is associated with pull request number *pr*."""
        for ref in run.get("pull_requests") or []:
            if isinstance(ref, dict) and ref.get("number") == pr:
                return True
        return False

    @staticmethod
    def _format_runs(
        owner: str,
        repo: str,
        branch: Optional[str],
        pr: Optional[int],
        runs: List[dict],
    ) -> str:
        """Render the (already filtered) runs as a compact text report."""
        scope = f"branch '{branch}'" if branch else f"PR #{pr}"
        if not runs:
            return f"No CI workflow runs found for {owner}/{repo} ({scope})."
        lines = [
            f"CI workflow runs for {owner}/{repo} ({scope}) - {len(runs)} run(s):"
        ]
        for run in runs:
            name = run.get("name") or "(unnamed workflow)"
            number = run.get("run_number", "?")
            status = run.get("status", "unknown")
            conclusion = run.get("conclusion") or "-"
            sha = str(run.get("head_sha") or "")[:7]
            url = run.get("html_url", "")
            lines.append(
                f"  #{number} {name}: {status}/{conclusion} (sha {sha})\n    {url}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def execute(self) -> str:
        try:
            branch = self.branch.strip() if isinstance(self.branch, str) else None
            pr = self.pr

            # Exactly one of branch / pr is required (XOR).
            if branch and pr is not None:
                return self._truncate_output(
                    "Error: specify exactly one of 'branch' or 'pr', not both."
                )
            if not branch and pr is None:
                return self._truncate_output(
                    "Error: specify exactly one of 'branch' or 'pr'."
                )

            try:
                owner, repo = self._resolve_owner_repo()
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")

            per_page = self._clamp_per_page(self.per_page)
            url = self._build_runs_url(owner, repo, per_page, branch if branch else None)

            # Non-credential headers only: never send Authorization/token.
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": _USER_AGENT,
            }

            data = _http_get(url, headers)
            runs = data.get("workflow_runs") or []
            if pr is not None:
                runs = [run for run in runs if self._run_matches_pr(run, pr)]

            return self._truncate_output(
                self._format_runs(owner, repo, branch or None, pr, runs)
            )
        except Exception as e:
            if isinstance(e, (RuntimeError, PermissionError)):
                # Hard security errors surface instead of being swallowed.
                raise
            return self._truncate_output(f"Error executing ci_read: {e}")
