# Ticket: bug/build-tool-chain-untested

**Filed:** Gen 43, 2026-10-15 at `eadd2ca2`
**Source:** p0-buildtool-egress read + Q1 check (ii) by orchestrator
**Severity:** Low — coverage gap, not a functional gap
**Status:** filed, not dispatched.

## Defect

No test exercises the full build-tool chain end-to-end:

  ContainerBuildTool.execute
  → ContainerManager.build_image
  → _resolve_container_config_via_gate (network resolution)
  → _run_image_build
  → client.api.build(network_mode=...)

The prior egress fix (#95, c3be9332) tests `_run_image_build` **directly**
at `tests/docker/test_build_network_isolation.py`. It drives a real
`docker.APIClient` through `_run_image_build(networkmode="none")` — no
monkeypatching, which is excellent for that helper. But it does not go
through `ContainerBuildTool` or `ContainerManager.build_image`.

## Why it matters

The gate at HEAD is correct — `build_image` resolves network from the
adapter and passes `networkmode="none"` when restricted. But nothing
asserts that the resolution at `build_image` uses the canonical source.
A future refactor of `build_image` (e.g., moving the resolution, or
passing a different argument name) would silently bypass the gate, and
#95's test would stay green.

## Fix shape (not written)

Add a test that drives `ContainerBuildTool.execute` under a session where
the canonical source resolves to `network:none`, and asserts:
- the build raises with a network failure
- it does NOT reach package installation
- the resolved `network_mode` was passed to `_run_image_build`

May be possible without a real Docker daemon by mocking `client.api.build`
at the daemon boundary and asserting the `network_mode` kwarg.

## Related

- `bug/build-tool-filesystem-grain-unchecked` (G1) — separate gap
- `bug/git-write-not-bound-to-filesystem-grain` — same family
- `displayed ≠ enforced` cluster
