# bug/git-resource-container-ignores-workspace-container-limit

The git tool's admission check always uses the DEFAULT cap, so a workspace's
`container_limits.max_containers` override is invisible to it.

`tools/git_info_tool.py:1262-1269` builds the resource manager without
`session_config`:

    ResourceContainerManager(workspace_id=..., workspace_path=...,
                             network_mode=..., session_permissions=...,
                             session_id=...)

`security/admission_gate.py:674-702 _container_limit()` returns
`DEFAULT_MAX_CONTAINERS` whenever `session_config is None`, and
`agent/config/defaults.py:137 DEFAULT_MAX_CONTAINERS = 6`.

Consequences, both wrong:
- a workspace that legitimately RAISED its limit still deadlocks the git tool at
  6 — the operator's override cannot help it;
- a workspace that LOWERED its limit gets no protection on the git path.

**Fix:** pass `session_config` through to the manager so the git resource
container is admitted under the same limit as everything else. One argument.

**Found during:** the read for `bug/resource-containers-compete-with-workspace-cap`.
**Filed by:** Gen 46, 2026-10-20.
