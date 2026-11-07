# Bug 4 — git execution fallback under containerized mode (investigation)

## Question
Does git silently run on the **HOST** when `git_execution_mode=containerized` and the
container has **no network**?

## Evidence
**Reproducer (`section_bug4`, `.thoughtmachine/working_docs/reproduce_p0.py` L598–660).**
This section is explicitly headed *"INVESTIGATION ONLY - NO CONTAINERS"*. It **starts no
container**: it only prints the decision surface — `resolve_git_execution_mode(...)` for 6
config/meta combos, then `tool._git_execution_mode()`, `tool._use_container_mode()`, and (if
`_with_mode` exists) a dummy trailer — and records
`record("BUG4","INVESTIGATION","git fallback decision surface printed; NO container started")`.
So it **observes nothing about a live container**; it is a static dump of the resolver +
trailer. (README §7 treats `BUG4` as `INVESTIGATION`/informational, not a pass/fail bug.)

**Decisive code (static reading):**
- `tools/git_info_tool.py:16-43` `resolve_git_execution_mode()` → `"unavailable"` if no
  workspace path; `"host_fallback"` if `mode=='host'` **or no workspace id**; else
  `"containerized"`. Default effective mode is `"container"`.
- `tools/git_info_tool.py:787-795` `_use_container_mode()` → True iff mode `!= "host"` **and**
  resolved workspace **path** **and** **id**.
- `tools/git_info_tool.py:799-859` `_resolve_resource_execution()` → when container is
  desired, calls `ResourceContainerManager.ensure_resource("git")`; returns `"containerized"`
  (+ manager) only if `result["mode"]=="containerized"`, else `(result, None)` → degrade.
- `tools/git_info_tool.py:483-579` `_run_git_raw()` → `containerized` ⇒
  `_exec_container_raw()` which execs `git` **inside the container** (`manager.exec(["git"]+args, …)`,
  L723-729); `host_fallback` ⇒ logs WARNING *"degraded containerized git execution to hardened
  host git"* and calls `_exec_host_raw()`; `unavailable` ⇒ raises `RuntimeError`.
- `infra/resource_container_manager.py:1222-1350` `ensure_resource()` → `"host_fallback"` is
  returned **only** for image-unavailable/`build_failed`, Docker unreachable, or
  create/remove/start failure. `network_mode` (default `'none'`) is a container-create
  attribute (L1102/1121); **no branch returns `host_fallback` because of missing network.**

**OBSERVED vs INFERRED.** All of the above is **INFERRED** from source (no live docker probe
is possible in this sandbox). Nothing was OBSERVED against a running container.

## Finding — MIXED (NO for the network-specific case; bounded YES for infra outage)
Precise path:
1. `git_execution_mode=containerized` + registry workspace ⇒ `_use_container_mode()` True ⇒
   `_resolve_resource_execution()` ⇒ `ensure_resource("git")`.
2. Container **running** ⇒ `mode=="containerized"` ⇒ `manager.exec(["git", …])` runs git
   **inside** the container. A network-requiring op (fetch/push/ls-remote) under
   `network_mode='none'` returns a **non-zero exit**; `_run_git` surfaces
   `"Git command failed (exit code N)"`. **No host fallback; git does NOT run on the host.**
3. Container **cannot be ensured** (no image / Docker unreachable / create-start-remove error)
   ⇒ `ensure_resource` returns `host_fallback` ⇒ git **does** run on the host — but **not
   silently**: a WARNING is logged and the per-call trailer reports
   `execution_mode: host_fallback` / `fallback_used: true`, gated by
   `_host_execution_denied_reason()` (`allow_host_resources`, fail-closed).

⇒ Git does **not** silently run on the host merely because the container lacks network. Host
execution happens only on container-infra outage, and even then it is logged + trailer-flagged.

## Recommended follow-up
**Pick (b) — with a correction.** The network case is *not* "git silently runs on host"; it is
"git runs in the sandbox and fails on network". Option (a) cannot fix this by itself because
the container is intentionally `network_mode='none'` — making network git succeed in-container
is a **policy change (grant network)**, not a Bug-2 correctness fix. So the UI must state
honestly: (i) network-requiring git ops **fail inside the no-network sandbox**; (ii) host
fallback occurs **only on container-infra outage** and is surfaced via the
`execution_mode`/`fallback_used` trailer. Chosen because it matches the observed behaviour and
avoids the false claim that "network ⇒ host".

## Inconclusive / confirmation probe
Confidence is **INFERRED (static only)**. The specific claim that a **non-zero container-git
exit never re-dispatches to host** is supported by the absence of any such branch, but only a
live run proves it. Exact probe that would settle it (needs host docker — unavailable here):
- `docker ps -a --filter label=thoughtmachine.resource` (is a resource container up?).
- Ensure the git resource container with `network_mode='none'`, then
  `manager.exec(["git","ls-remote","https://github.com/git/git"])` → **look for**: non-zero
  exit **AND** absence of the `"degraded … to hardened host git"` WARNING **AND**
  `fallback_used:false`. If instead a WARNING + `fallback_used:true` appears, the finding
  flips to YES for the network case.
- Re-run `reproduce_p0.py` section 4 to capture the resolver/trailer surface alongside.
