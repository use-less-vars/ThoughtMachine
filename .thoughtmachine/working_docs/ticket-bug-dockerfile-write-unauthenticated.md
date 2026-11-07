# Ticket: bug/dockerfile-write-unauthenticated

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit; handler read in full).
**Severity:** **CRITICAL** — an unauthenticated caller can replace the Dockerfile of any workspace, and the Dockerfile is the recipe for everything that executes inside that workspace.
**Status:** open; verified by read at `24012a89`, not exercised at runtime — `UNRUN: no docker` in the auditing environment. Not to be fixed in this wave.

**Parent pointer:** `ticket-bug-unauthenticated-routes-beyond-health.md` is the generic pointer for "the Web UI API has no authentication at all". It is **not** the owner of this row: it names no workspace route, never uses the word `workspace`, does not mention Dockerfiles, and describes itself as `open; pointer, **not** an exhaustive map — Cluster 2 owns the full route inventory.` This ticket owns the specific row: unauthenticated write of an attacker-chosen Dockerfile into any workspace.

## Finding

`PUT /api/workspace/{ws_id}/dockerfile` (`web_ui/backend/workspace_routes.py`, handler `put_dockerfile`) is five statements with no gate on the caller's relationship to the target workspace. The complete executable body (docstring omitted):

```python
@router.put("/{ws_id}/dockerfile")
async def put_dockerfile(ws_id: str, request: Request):
    body = await request.body()
    text = body.decode("utf-8")
    ensure_workspace_dirs(ws_id)
    path = _workspace_dir(ws_id) / "Dockerfile"
    _atomic_write_text(text, path)
    return {"status": "ok", "workspace_id": ws_id}
```

No ownership predicate (`_ownership_refusal` is not consulted), no session or socket binding, no permission lookup, no `_origin_allowed`, no CSRF token, no `confirmed` flag, no `_confine_to_home`. The only validation reachable is workspace-id **shape**, via `_workspace_dir` → `_validate_workspace_id` → `validate_single_path_segment(..., kind="workspace id")`, which rejects separator characters, `.`/`..` and reserved ids — and says nothing about who may write to `ws_id`.

## Bearing — the write is the primitive, F7 is the trigger

The write alone changes a file on disk. What makes it a code-execution primitive is WS `rebuild_container` (`web_ui/backend/server.py`, F7), which re-runs the container build from whatever Dockerfile is on disk. The chain is: unauthenticated `PUT` (this ticket) → attacker-controlled build recipe on disk → `rebuild_container` (F7) → attacker-controlled code inside every container of that workspace, with that workspace's mounts. Neither link requires a credential. The two fixes are separable: this ticket removes the unauthorised write; F7 governs whether an authorised write may be re-executed without further consent.

## Blast radius

Any client that can route to the port can replace any workspace's image recipe, for any workspace id it can name, with no credential. What the recipe can then reach inside the container depends on that workspace's mounts and capability ceiling; the write itself is unconditional.

## Why it matters now

`rebuild_container` is an ordinary user action, not an unusual one, so a poisoned recipe is consumed by normal use. And nothing pins the caller-binding property of this route: `NONE FOUND` in the test suite.

## What this is not

- Not a path traversal: `ws_id` is shape-validated before the path join, so `../B` cannot reach the write.
- Not a demonstrated remote-code-execution: the execution leg is inferred from Dockerfile semantics, not observed. `UNRUN: no docker` in the auditing environment; no container was built, no recipe was executed.
- Not a duplicate of `ticket-bug-unauthenticated-routes-beyond-health.md` — see parent pointer above.

## Fix shape

Not specified here. Any fix must bind the caller to the target workspace through the single authority the rest of the server should already be using; see `docs/design-input-auth-layer-prerequisite.md` for why a per-route check is not sufficient on its own.
