# Ticket: hygiene/legacy-docker-executor-path-orphaned

**Filed:** Gen 44, 2026-10-16, from T1b worker report §3 (Claim B, verified).
**Severity:** HIGH structural — reframes T1b's value and the container lifecycle scope.
**Status:** filed, not dispatched. Awaits a read, then Main rules the shape.

## Finding

`DockerExecutor` is constructed in production only by
`thoughtmachine/security.py:696::setup_docker_sandbox`. That function has **no
production caller**:

- Only user: `tests/docker/test_pool_envelope_keying.py:422`.
- `tools/docker_code_runner.py:25` imports it (`security_setup_docker`) but
  never calls it.
- The only other construction is `docker_executor.py:1393::_run_build` —
  build-only.

The **live code-execution path is `ContainerManager`**
(`tools/docker_code_runner.py:393`, `tools/workspace/check_system.py:614`).

Consequence: `_ensure_container`, the entire `DockerExecutor.execute()` path,
and the trust-suite contract at `tests/trust/test_toggle_live.py` all describe
code that nothing in production calls.

## The two paths

| Path | Status | Test surface |
|---|---|---|
| `DockerExecutor.execute()` / `_ensure_container` | **PRODUCTION-DEAD** | `tests/trust/test_toggle_live.py`, `tests/docker/` |
| `ContainerManager` | LIVE | `tests/test_container_manager_lifecycle.py`, `tests/docker_integration/` |

## The question

Three possibilities:

- **(a) Retire it.** Delete `DockerExecutor` and `setup_docker_sandbox` if
  truly unused. Blast radius: many tests test it. Big.
- **(b) Redirect it.** Make `setup_docker_sandbox` a thin shim that constructs
  `ContainerManager`. Preserves the API, closes the "dormant path" gap.
- **(c) Document it.** It exists for a reason we haven't found (e.g. an
  external entry point). Write that reason down.

**Main rules.** This is a structural decision, not a fix.

## Why now

- T1b was scoped under the assumption that `_ensure_container` was the live
  use-path. It isn't. T1b still ships (correct on a dormant path), but the
  live path (T1c, `ContainerManager` recreate ladder) is where the
  trust-suite contract actually matters.
- Every container-lifecycle wave has touched this path. The frame is clearer
  with it named.

## MUST NOT TOUCH
Standard list.

## Bearing

Arc 3 (Secure Execution), Fact 1 (Existence) — "what IS this code path, and
is it alive?" Layer 3 (Subsystems) hygiene.
