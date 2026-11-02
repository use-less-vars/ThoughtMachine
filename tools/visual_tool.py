# tools/visual_tool.py
"""Render one workspace file to a PNG in a throwaway container.

One tool, one grain (``visual``), one format (PNG), one deadline
(:data:`RENDER_TIMEOUT_SECONDS`), one artifact per call.  The design is
``docs/design-worker-lifecycle.md`` (section "Proposed design -- one tool, one
grain, one format, one cap, four named outcomes"); what this v1 narrows is
stated here rather than left implicit:

* ``target`` is ``file:///workspace/<path>`` only.  ``http(s)://`` is refused
  with an actionable error: the render container carries the session's network
  envelope (``banned`` by default, i.e. no egress at all), and no verified
  network level exists to hang an egress branch on.
* PNG only -- the intersection of the image types the providers accept.
* One render per call, bounded by the tool's own deadline.
* The artifact is returned **by reference**: path, byte size, MIME type.
  Inline image delivery needs ``ToolResultBlock`` to carry image parts plus two
  provider adapters, neither of which exists yet, so the envelope says what it
  is instead of pretending the model can see the picture.

Container access is uniform across root/worker runs: the tool resolves its
workspace, permission grain and image through the same paths every other
container tool uses (``infra.container_manager`` + the security gate), and its
render container is ephemeral and torn down in a ``finally``.
"""

import base64
import json
import logging
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional
from urllib.parse import unquote

from pydantic import Field

from agent.config.defaults import (
    ADMISSION_IMAGE_ALLOWLIST,
    DEFAULT_CPU_QUOTA,
    DEFAULT_MEM_LIMIT,
    VISUAL_RENDER_IMAGE,
)
from thoughtmachine.container_record import LIFECYCLE_EPHEMERAL

from .base import ToolBase

try:
    from infra.container_manager import ContainerManager
except ImportError:  # pragma: no cover - docker SDK not installed
    ContainerManager = None

# Owner identity of the worker sub-agent that created the container (stdlib-only
# leaf module, so no circular import).  Falls back to None outside a worker turn.
try:
    from agent.core.worker_context import current_worker_name
except ImportError:  # pragma: no cover

    def current_worker_name():  # type: ignore
        return None


logger = logging.getLogger(__name__)


# ── The tool's fixed numbers ────────────────────────────────────────────────
#: Hard deadline for one render, per call, and the container-manager exec
#: timeout for both the render and the read-back.  The design fixes the value
#: (docs/design-worker-lifecycle.md: "The tool enforces RENDER_TIMEOUT_SECONDS =
#: 30 per call"), so a hung renderer costs one container and at most this long.
RENDER_TIMEOUT_SECONDS = 30

#: Directory the renderer writes into *inside* the container.  ``/tmp`` is a
#: tmpfs on every container this manager creates
#: (infra/container_manager.py:2233-2240), so it is writable even when the
#: session's ``filesystem`` grant is ``read`` and the ``/workspace`` bind mount
#: is consequently mounted read-only (infra/container_manager.py:2247-2251).
#: The bytes leave the container over exec stdout, never through the mount.
CONTAINER_RENDER_DIR = "/tmp/visual"

#: Workspace-relative directory the PNG is delivered into (written on the HOST
#: side, so it does not depend on the container's mount mode).
ARTIFACT_DIR = "artifacts/visual"

#: Transport guard, not a delivery cap: the read-back arrives base64-encoded on
#: one exec stdout, so a runaway artifact is refused instead of being held in
#: memory twice.  8 MiB is far above any single-page screenshot.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

#: The PNG signature.  The renderer's output is verified against it rather than
#: trusted, because ``media_type`` is pinned to ``image/png`` on the way out.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: One-line disclosure on every successful envelope: the artifact exists and is
#: addressable, but it is not *shown* to the model yet.
DELIVERY_NOTE = (
    "Image delivery to the model awaits the IR/provider prerequisite "
    "(ToolResultBlock image parts plus adapter support); this call returns the "
    "artifact's path, byte size and MIME type only."
)

#: Fail-closed refusal, returned verbatim.  Both sub-conditions collapse into
#: this one string on purpose: the model cannot act on the difference between
#: "nothing provisioned" and "provisioned but not admitted", and naming the
#: configured reference would leak an operator-side detail.
RENDER_IMAGE_UNAVAILABLE = (
    "visual: render image unavailable - no render image is provisioned in this "
    "build, or the configured one is not in the admission allow-list; refusing "
    "to start a render container (fail-closed). No container was created."
)

_SCHEME_FILE = "file://"
_ENCODED_ESCAPES = ("%2e", "%2f", "%5c", "%00")


def _render_argv(container_target: str, container_output: str,
                 full_page: bool) -> List[str]:
    """Return the argv the render image must accept for one render.

    THE RENDER-IMAGE CONTRACT, isolated here in one function.  No render image
    exists in this build (``agent.config.defaults.VISUAL_RENDER_IMAGE`` is
    ``None``), so this argv is the contract the image has to satisfy rather
    than a command verified against a real binary: read the file named by
    ``--target``, write a PNG at ``--output``, add ``--full-page`` for a
    full-scroll render, exit non-zero on failure, and make no network calls.
    When the image is admitted, this function -- and only this function --
    changes.
    """
    argv = [
        "visual-render",
        "--target", container_target,
        "--output", container_output,
    ]
    if full_page:
        argv.append("--full-page")
    return argv


def _render_image_refusal() -> Optional[str]:
    """Return the fail-closed refusal, or ``None`` when a render may proceed.

    Two conditions, both fail-closed:

    1. no render image is provisioned in this build (``VISUAL_RENDER_IMAGE`` is
       falsy), and
    2. the configured reference is not in the admission allow-list
       (``ADMISSION_IMAGE_ALLOWLIST``).

    ``ADMISSION_IMAGE_ALLOWLIST`` stays the single authority for what may run:
    this tool hard-codes exactly one reference and never accepts an image from
    the model (there is no ``image`` parameter), so the check here can only
    ever *narrow* what the gate would allow -- it never widens it, and it is
    deliberately not implemented as a second, private allow-list, because two
    authorities is exactly the drift this avoids.  Residual, stated so it is
    reviewed rather than discovered: once the render image is on the GLOBAL
    allow-list, any other caller that passes ``image=`` to the container
    manager may use it too, so adding the reference is a workspace-wide
    widening to review as one, not a private grant to this tool.
    """
    if not VISUAL_RENDER_IMAGE:
        return RENDER_IMAGE_UNAVAILABLE
    # Imported HERE, not at module scope, because it closes a cycle:
    # security.admission_gate imports ContainerConfig from
    # security.security_gate, which transitively imports ``tools``, which
    # imports this module -- so a module-level import here resolved against a
    # *partially initialized* security_gate whenever the gate was imported
    # first, the ImportError was swallowed by tools/__init__.py, and VisualTool
    # silently vanished from TOOL_CLASSES.
    # Fail closed: an import error only ever means "the reference cannot be
    # proven admitted", which must refuse -- never fall through to None, which
    # means "proceed".
    try:
        from security.admission_gate import _image_repo_name
    except Exception:  # noqa: BLE001 - fail closed, never proceed unverified
        return RENDER_IMAGE_UNAVAILABLE
    # Same normaliser the gate itself uses (security/admission_gate.py:399-410,
    # compared at :438), so tag/registry variants of an allow-listed reference
    # behave identically in both places.
    if _image_repo_name(VISUAL_RENDER_IMAGE) not in ADMISSION_IMAGE_ALLOWLIST:
        return RENDER_IMAGE_UNAVAILABLE
    return None


class VisualTargetError(ValueError):
    """A ``target`` this tool refuses to render, with an actionable message."""


def _resolve_target(target: str, workspace: str) -> tuple:
    """Resolve ``target`` to ``(host_path, container_path)``.

    v1 accepts ``file:///workspace/<path>`` only.  The resolved *real* path must
    stay inside ``workspace``; each escape route gets its own message so the
    model is told what to fix instead of retrying blindly.

    No shared path-guard helper exists in this codebase -- the precedents are
    private per-tool resolvers (``tools/workspace/working_document.py:111-129``
    ``_resolve_doc_path``, which uses ``Path.resolve()`` plus
    ``Path.is_relative_to()``).  This follows the same shape.

    Args:
        target: the model-supplied target string.
        workspace: host path of the workspace root.

    Returns:
        ``(host_path, container_path)`` -- the absolute host path of the file
        and its ``/workspace``-relative sibling inside the container.

    Raises:
        VisualTargetError: empty target, an ``http(s)://`` URL, any other
            scheme, a path outside ``/workspace``, an escape attempt (``..``
            segments or their percent-encoded forms), or a target that is not
            an existing regular file.
    """
    raw = (target or "").strip()
    if not raw:
        raise VisualTargetError(
            "visual: 'target' is required and must be file:///workspace/<path>."
        )

    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        scheme = lowered.split(":", 1)[0]
        raise VisualTargetError(
            f"visual: {scheme}:// targets are not supported -- the render "
            "container runs with the session's network envelope (banned by "
            "default), so it could not fetch a remote page. Render a file that "
            "is already in the workspace instead: file:///workspace/<path>."
        )
    if not lowered.startswith(_SCHEME_FILE):
        raise VisualTargetError(
            f"visual: unsupported target {raw!r} -- the only accepted form is "
            "file:///workspace/<path>."
        )

    # Percent-encoded dots/separators/NUL are refused outright: a decoder later
    # in the chain (shell, renderer, OS) could otherwise turn one back into a
    # traversal that the lexical checks below already rejected.
    for token in _ENCODED_ESCAPES:
        if token in lowered:
            raise VisualTargetError(
                f"visual: refusing target {raw!r} -- percent-encoded characters "
                f"({token}) are not accepted in a target path."
            )

    # "file:///workspace/x" -> "/workspace/x"; the boundary test keeps
    # "/workspace-elsewhere/x" out.
    after_scheme = raw[len(_SCHEME_FILE):]
    if after_scheme != "/workspace" and not after_scheme.startswith("/workspace/"):
        raise VisualTargetError(
            f"visual: target must be inside the workspace -- expected "
            f"file:///workspace/<path>, got {raw!r}."
        )

    relative = unquote(after_scheme[len("/workspace"):]).lstrip("/")
    if ".." in Path(relative).parts or "\\" in relative:
        raise VisualTargetError(
            f"visual: refusing target {raw!r} -- the path must stay inside the "
            "workspace (no '..' segments, no absolute paths, no backslashes)."
        )

    root = Path(workspace).resolve()
    resolved = (root / relative).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise VisualTargetError(
            f"visual: refusing target {raw!r} -- it resolves outside the "
            f"workspace ({resolved})."
        )
    if not resolved.is_file():
        raise VisualTargetError(
            f"visual: no regular file at {raw!r} in the workspace."
        )
    return str(resolved), "/workspace/" + relative


class VisualTool(ToolBase):
    """Screenshot one workspace file inside an ephemeral, network-isolated container.

    Renders ``target`` (``file:///workspace/<path>``) to a PNG in a throwaway
    container built from the provisioned render image, writes the PNG into the
    workspace under ``artifacts/visual/`` and returns its path, byte size and
    MIME type.  Exactly one render per call; the container is destroyed on
    every path -- success, failure, timeout and exception alike.

    Refuses (fail-closed) when no render image is provisioned or the configured
    one is not in the admission allow-list, before any container is created.
    """

    # Both requirements carry their level on purpose: the gate skips an entry
    # without a ':' ("if ':' not in req: continue  # malformed, skip",
    # security/security_gate.py:1568-1570), so a bare "container" would silently
    # grant nothing.  ``visual:view`` is the grain added for this capability
    # (security/resource_catalog.py:133); ``container:true`` is the same
    # container-execution grant tools/docker_code_runner.py:61 requires.
    required_categories: ClassVar[List[str]] = ["visual:view", "container:true"]

    # No ``name`` ClassVar override: ``tool_name()`` then falls back to the
    # class name (tools/base.py:99-101), which is the only form that resolves in
    # all three consumers -- the executor (agent/core/tool_executor.py:302-308,
    # which also accepts ``tool_name()``), the worker tool registry
    # (tools/workspace/worker_thread.py:3739-3741 keys by ``cls.__name__``) and
    # the worker's enabled-tools list (worker_thread.py:2030-2037).
    tool: Literal["VisualTool"] = "VisualTool"

    target: str = Field(
        ...,
        description=(
            "file:///workspace/<path> -- the workspace file to render. Only "
            "file:// targets inside /workspace are accepted; an http(s):// URL "
            "is rejected because the render container has no network access."
        ),
    )
    full_page: bool = Field(
        default=False,
        description=(
            "Render the full scrollable page instead of just the viewport "
            "(default: false)."
        ),
    )
    worker_name: Optional[str] = Field(
        default=None,
        description=(
            "[internal] Owner identity of the worker sub-agent that created "
            "this container (injected by ToolExecutor from the worker context "
            "var; stamped VERBATIM as the ``thoughtmachine.worker`` docker "
            "label on fresh creates so worker teardown can reclaim it)."
        ),
    )

    # ── result envelope ────────────────────────────────────────────────────
    def _envelope(self, *, success: bool, duration: float,
                  artifact_path: Optional[str] = None, size_bytes: int = 0,
                  mime_type: Optional[str] = None,
                  error: Optional[str] = None) -> str:
        """Build the tool's JSON result (by reference; never inline image data)."""
        response: Dict[str, Any] = {
            "success": success,
            "tool": self.tool_name(),
            "target": self.target,
        }
        if success:
            response["artifact_path"] = artifact_path
            response["bytes"] = size_bytes
            response["mime_type"] = mime_type
            response["note"] = DELIVERY_NOTE
        else:
            response["error"] = error
        response["duration"] = round(duration, 3)
        return json.dumps(response, indent=2)

    def _fail(self, error: str, started: float) -> str:
        return self._truncate_output(
            self._envelope(success=False, error=error,
                           duration=time.time() - started)
        )

    # ── execution ──────────────────────────────────────────────────────────
    def execute(self) -> str:
        started = time.time()

        # 1. Image admission, BEFORE any container work (fail-closed).
        refusal = _render_image_refusal()
        if refusal is not None:
            return self._fail(refusal, started)

        # 2. Workspace, resolved through the registries like every other tool.
        workspace = self._resolve_registry_workspace()
        if not workspace:
            return self._fail(
                "visual: no workspace path could be resolved for this session, "
                "so there is nothing to render.", started)

        # 3. Target: file:///workspace/<path> only, confined to the workspace.
        try:
            host_target, container_target = _resolve_target(self.target, workspace)
        except VisualTargetError as exc:
            return self._fail(str(exc), started)

        # 4. Artifact destination (host side).
        artifact_stem = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        artifact_name = f"{artifact_stem}.png"
        host_dir = Path(workspace) / ARTIFACT_DIR
        try:
            host_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._fail(
                f"visual: cannot create the artifact directory {host_dir}: {exc}",
                started)
        host_artifact = host_dir / artifact_name
        container_output = f"{CONTAINER_RENDER_DIR}/{artifact_name}"

        if ContainerManager is None:
            return self._fail(
                "visual: the container manager is unavailable in this build, so "
                "the render container cannot be created.", started)

        # 5. Render container: ephemeral, no heal, destroyed in the finally.
        manager = ContainerManager(
            workspace_path=workspace,
            session_id=getattr(self, "session_id", None),
            workspace_id=_resolve_workspace_id(workspace),
            session_permissions=getattr(self, "session_permissions", None),
            image=VISUAL_RENDER_IMAGE,
            # Explicit, not inherited from the manager default: the operator
            # mandate caps the untrusted renderer at 512 MB, so this call
            # site must not drift if the global default changes.
            mem_limit=DEFAULT_MEM_LIMIT,
            cpu_quota=DEFAULT_CPU_QUOTA,
        )
        try:
            info = manager.start(
                image=VISUAL_RENDER_IMAGE,
                worker_name=getattr(self, "worker_name", None) or current_worker_name(),
                lifecycle_class=LIFECYCLE_EPHEMERAL,
                heal_missing=False,
            )
        except Exception as exc:  # noqa: BLE001 - surface, never mask
            return self._fail(
                f"visual: could not start the render container: {exc}", started)

        container_id = (info or {}).get("id")
        if not container_id:
            reason = (info or {}).get("error") or "no container id was returned"
            return self._fail(
                f"visual: could not start the render container: {reason}", started)

        try:
            # 5a. The render itself.
            argv = _render_argv(container_target, container_output, self.full_page)
            try:
                render = manager.exec(
                    container_id,
                    command=" ".join(shlex.quote(part) for part in argv),
                    timeout=RENDER_TIMEOUT_SECONDS,
                    workdir=CONTAINER_RENDER_DIR,
                )
            except TimeoutError:
                return self._fail(
                    f"visual: the render exceeded RENDER_TIMEOUT_SECONDS "
                    f"({RENDER_TIMEOUT_SECONDS}s) and the container was killed. "
                    "Try a smaller or simpler target.", started)
            except Exception as exc:  # noqa: BLE001 - surface, never mask
                return self._fail(f"visual: the render failed: {exc}", started)

            exit_code = render.get("exit_code")
            if exit_code != 0:
                detail = (render.get("stderr") or render.get("stdout") or "").strip()
                return self._fail(
                    f"visual: the renderer exited with code {exit_code}"
                    + (f": {detail[:1000]}" if detail else "."), started)

            # 5b. Read the artifact back.  The /workspace bind mount is
            # read-only unless the session's ``filesystem`` grant is write
            # (infra/container_manager.py:2247-2251), so the tmpfs output path
            # is the only location guaranteed writable for a render session;
            # the bytes are transported base64-encoded over exec stdout and
            # written into the workspace from the HOST side, which needs no
            # container mount permission at all.
            try:
                fetch = manager.exec(
                    container_id,
                    command=f"base64 {shlex.quote(container_output)}",
                    timeout=RENDER_TIMEOUT_SECONDS,
                    workdir=CONTAINER_RENDER_DIR,
                )
            except TimeoutError:
                return self._fail(
                    f"visual: reading the artifact back exceeded "
                    f"{RENDER_TIMEOUT_SECONDS}s.", started)
            except Exception as exc:  # noqa: BLE001 - surface, never mask
                return self._fail(
                    f"visual: the renderer produced no readable artifact: {exc}",
                    started)

            if fetch.get("exit_code") != 0:
                detail = (fetch.get("stderr") or "").strip()
                return self._fail(
                    "visual: the render produced no artifact at the expected "
                    "output path"
                    + (f": {detail[:1000]}" if detail else "."), started)

            try:
                data = base64.b64decode(fetch.get("stdout") or "", validate=False)
            except Exception as exc:  # noqa: BLE001 - surface, never mask
                return self._fail(
                    f"visual: the artifact could not be decoded: {exc}", started)

            if not data:
                return self._fail(
                    "visual: the renderer produced an empty artifact.", started)
            if len(data) > MAX_ARTIFACT_BYTES:
                return self._fail(
                    f"visual: the artifact is {len(data)} bytes, over the "
                    f"{MAX_ARTIFACT_BYTES}-byte transport limit.", started)
            if not data.startswith(_PNG_MAGIC):
                return self._fail(
                    "visual: the renderer did not produce a PNG (bad signature).",
                    started)

            try:
                host_artifact.write_bytes(data)
            except OSError as exc:
                return self._fail(
                    f"visual: cannot write the artifact to {host_artifact}: {exc}",
                    started)

            return self._truncate_output(self._envelope(
                success=True,
                artifact_path=f"{ARTIFACT_DIR}/{artifact_name}",
                size_bytes=len(data),
                mime_type="image/png",
                duration=time.time() - started,
            ))
        finally:
            # Teardown on every path: success, timeout, exception, cancellation.
            # ``remove`` is already idempotent and documented never to raise
            # (infra/container_manager.py:3679-3686), so this nested guard is
            # belt-and-braces -- and it must never mask the result being
            # returned, so it only logs.
            try:
                manager.remove(container_id)
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                logger.warning(
                    "visual: could not remove render container %s: %s",
                    container_id, exc,
                )


def _resolve_workspace_id(workspace: str) -> Optional[str]:
    """Resolve the workspace id for the security gate (best-effort).

    Mirrors tools/docker_code_runner.py:375-381: the id is what the gate keys
    its per-workspace checks on, and no id simply means those checks see none.
    """
    try:
        from thoughtmachine.workspace_capabilities import resolve_workspace_id
        return resolve_workspace_id(workspace)
    except Exception:  # noqa: BLE001 - never block a render on id resolution
        return None
