# Ticket: bug/build-tool-filesystem-grain-unchecked

**Filed:** Gen 43, 2026-10-15 at `eadd2ca2`
**Source:** p0-buildtool-egress read; ruled by Main
**Severity:** Medium — trust-layer gap, same shape as git:write finding
**Status:** filed, not dispatched. Disjoint from 9a's funnel.

## Defect

`ContainerBuildTool.execute` (`tools/container_control.py`) requires only
the `container:true` grain. The build writes to Docker daemon-side storage
(outside the workspace), and no `filesystem` gate fires on this path.

The build context is a tempdir (Dockerfile + requirements.txt) — the
workspace itself is not touched. But the daemon's image store is
host-side disk, and the tool does not require a filesystem write grant
to trigger that write.

## What is already fixed (do not re-fix)

The **network** gate on this path is live at dev HEAD:
- #90 (`9890173e`) added `network_mode=` kwarg to `client.api.build(...)`,
  resolved via `_resolve_container_config_via_gate`
- #95 (`c3be9332`) corrected the kwarg spelling (`network_mode` vs the
  old `networkmode` which was silently ignored by docker-py), with a
  deliberate no-fallback policy plus a live-fire egress test

The filesystem grain is the remaining gap.

## Fix shape (not written)

Gate the tool surface on `filesystem` + `network` grains. **Grant, not
blanket ban** — the read confirmed a legitimate deps-fetch path exists
(Q8). If build-time network is genuinely needed for a workspace, the
correct shape is an operator-explicit grant, not a ban.

Do NOT touch:
- `thoughtmachine/security.py::SAFE_DEFAULTS`
- `security/resource_catalog.py`, `agent/config/resource_catalog.json`
- `security_gate.apply_workspace_ceiling` + call sites
- parked `.thoughtmachine/knowledge/project/architecture_audit_2026-09.md`
- `scratch/_exp2.py`

## Bearing

Same class as `bug/git-write-not-bound-to-filesystem-grain` and the
`displayed ≠ enforced` cluster. Distinct mechanism, same family.
