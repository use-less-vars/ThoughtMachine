"""
PaginateTool - Add pagination to any ToolBase that returns list results.

This meta-tool wraps another tool's execution and adds pagination to large results.
It's designed to work with tools that return newline-delimited or list-based output.
"""
from .base import ToolBase
from typing import ClassVar, List, Any, Literal
from pydantic import Field

class PaginateTool(ToolBase):
    required_categories: ClassVar[List[str]] = ["filesystem:read"]
    """Wrap another tool's execution with pagination.

    Use this to page through large results from any file listing or search tool.

    Example workflow:
    1. First call: target_tool_name='GlobTool', page=1, per_page=50, directory='.', pattern='*.py'
    2. If result says "Page 1 of 5", call again with page=2

    Attributes:
        target_tool_name: Name of the tool class to paginate (e.g., 'GlobTool', 'DirectoryTreeTool')
        tool_params: Dictionary of parameters for that tool
        page: Page number (1-indexed)
        per_page: Results per page (0 = all)
    """
    tool: Literal["PaginateTool"] = "PaginateTool"


    target_tool_name: str = Field(
        description="Name of the tool class to paginate (e.g., 'GlobTool', 'DirectoryTreeTool')"
    )
    tool_params: dict = Field(
        default_factory=dict,
        description="Parameters to pass to the wrapped tool (as a dictionary)"
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-indexed)"
    )
    per_page: int = Field(
        default=100,
        ge=0,
        description="Number of results per page (0 = all results, no pagination)"
    )

    # Fields the executor owns and that a paginated call can therefore never
    # legitimately supply.  ``workspace_id`` is overwritten by the executor on
    # the direct path (agent/core/tool_executor.py) *after* the model is
    # built, so a caller may never provide it; a paginated call cannot stand
    # in for the executor, so the field is dropped instead of forwarded.
    _NEVER_FORWARD: ClassVar[tuple] = ("workspace_id",)

    # Control-plane / meta tools that pagination may never reach, no matter
    # what the caller's permission profile says.  Their authority does not
    # reduce to ``required_categories``: several deliberately declare none
    # (``Worker``, ``CheckSystem``, ``SummarizeTool``, ``Respond`` without a
    # ``report_body``, ``KnowledgeBaseTool`` in status/summary mode), so the
    # categories re-gate below cannot see them.  The worker blocklist is a
    # construction/spawn-time filter only (tools/workspace/worker_thread.py)
    # and is never consulted at call time, and paginate resolves its target
    # from the global ``tools.TOOL_CLASSES`` registry rather than the
    # caller's enabled tool set -- so categories alone leave a live route to
    # the blocklisted tools for any caller holding ``filesystem:read``.
    # Pagination exists to page bulky *data* output; wrapping a control-plane
    # tool is never legitimate.  ``PaginateTool`` lists itself to stop
    # recursion.
    _NEVER_PAGINATE: ClassVar[frozenset] = frozenset({
        "Worker",
        "CheckSystem",
        "KnowledgeBaseTool",
        "PaginateTool",
        "Respond",
        "SummarizeTool",
    })

    # Context fields forwarded to the wrapped tool when it declares them.
    #
    # This list is the injection allowlist for containment (paths) AND
    # identity (session, workspace, credentials). A field missing from this
    # list is a field an attacker can forge through tool_params.
    # (PR #175 added only the paths; the identity fields fell through and
    # workspace_id had to be handled separately via _NEVER_FORWARD.)
    # ToolBase sets ``extra="forbid"``, so a field the wrapped class does not
    # declare must never be passed through.
    _CONTEXT_FIELDS: ClassVar[tuple] = (
        "workspace_path",
        "token_limit",
        "is_docker",
        "container_workspace_path",
        "session_permissions",
        "effective_permissions",
        "agent_config",
        "session_id",
        "is_worker_context",
        "worker_name",
    )

    def _build_params(self, tool_class: type) -> dict:
        """Build the wrapped tool's kwargs from tool_params + injected context.

        Pagination (``page``/``per_page``) is added only for tools that
        actually declare those fields; injecting them unconditionally made
        every non-paginating tool fail with ``extra_forbidden``.
        """
        field_names = set(tool_class.model_fields)

        # Injected context is authoritative: a caller must not be able to forge
        # it through ``tool_params`` (e.g. ``workspace_path``) and thereby
        # re-open the containment the wrapped tool applies.  Colliding caller
        # keys are dropped silently, mirroring the executor, which also strips
        # infrastructure keys instead of rejecting the call.
        #
        # The same filter drops ``_NEVER_FORWARD`` fields (executor-owned
        # identity), which a paginated call may not supply at all.
        dropped = self._CONTEXT_FIELDS + self._NEVER_FORWARD
        params = {
            name: value
            for name, value in self.tool_params.items()
            if name not in dropped
        }
        for name in self._CONTEXT_FIELDS:
            if name not in field_names:
                continue
            value = getattr(self, name, None)
            if value is not None:
                params[name] = value

        if "page" in field_names:
            params["page"] = self.page
        if "per_page" in field_names:
            params["per_page"] = self.per_page
        return params

    def execute(self) -> str:
        """Execute the wrapped tool with pagination applied."""
        # Control-plane refusal, checked before the registry scan so it holds
        # even for names that are not resolvable there.  These tools cannot be
        # gated by categories (see ``_NEVER_PAGINATE``), so they are refused
        # structurally.  Never instantiated.
        if self.target_tool_name in self._NEVER_PAGINATE:
            return (
                f"Error: not permitted to paginate '{self.target_tool_name}': "
                f"it is a control-plane/meta tool and cannot be invoked "
                f"through pagination."
            )

        from tools import TOOL_CLASSES

        # Find the tool class
        tool_class = None
        for cls in TOOL_CLASSES:
            if cls.__name__ == self.target_tool_name:
                tool_class = cls
                break

        if tool_class is None:
            # Deliberately does not enumerate the registry: a full tool list
            # next to a refusal set is an enumeration oracle.
            return (
                f"Error: Tool '{self.target_tool_name}' not found."
            )

        # Execute the wrapped tool with merged pagination params
        params = self._build_params(tool_class)

        # ── Authority re-gate (fail closed) ──────────────────────────────
        # The executor gated *this* tool (``PaginateTool``), not the tool it
        # is about to construct on the caller's behalf.  Without this second
        # check a session holding only ``filesystem:read`` could wrap any
        # registered class (container control, host shell, ...) and run it
        # with its own ``effective_permissions`` -- an authority-laundering
        # hop.  The wrapped class's own declared requirements are re-checked
        # against the caller's effective profile with the same predicate the
        # executor uses, *before* the wrapped tool is instantiated.
        #
        # ``permission_footprint`` is unavailable to a tool (the executor
        # never injects it), so the workspace-permission ceiling cap is not
        # re-applied here; the effective profile already reflects the capped
        # merge the executor produced.  Known, documented residual.
        #
        # A tool also has no event bus, so an ``ask``-policy grain cannot be
        # prompted from here: ``check_required_categories`` treats it as a
        # deny in that case.  Pagination under an ask policy is therefore
        # refused rather than prompted -- fail closed, never fail open.
        try:
            from security.security_gate import check_required_categories
        except Exception as gate_import_error:  # pragma: no cover - defensive
            # Fail closed: no gate means no way to prove the wrapped call is
            # allowed, so it must not run.
            return (
                f"Error: not permitted to paginate '{self.target_tool_name}': "
                f"security gate unavailable ({gate_import_error})"
            )

        required = tool_class.get_required_categories(params)
        if required:
            allowed, err = check_required_categories(
                required=required,
                effective=self.effective_permissions or {},
                tool_name=self.target_tool_name,
                tool_args=params,
                description=(
                    f"PaginateTool wrapped call to {self.target_tool_name}"
                ),
                event_bus=None,
                session_id=self.session_id or "",
                permission_footprint=None,
                is_worker_context=bool(self.is_worker_context),
            )
            if not allowed:
                return (
                    f"Error: not permitted to paginate '{self.target_tool_name}': "
                    f"{err}"
                )

        try:
            # Instantiate and run
            tool_instance = tool_class(**params)
            return tool_instance.execute()
        except Exception as e:
            return f"Error executing {self.target_tool_name}: {e}"
