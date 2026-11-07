# Ticket: bug/container-remove-ui-no-op

**Filed:** Gen 44, 2026-10-16, operator observation.
**Severity:** Medium -- UI action accepted but no effect.
**Status:** filed, folds into Wave 10a.

## Finding

Operator confirmed removal of
`agent-exec-ephemeral-0acf09775137-745bd7d3-b643-4b-6509e183` via the panel
dialog ("Are you sure you want to remove...?"). After confirmation, the
container is still present.

## Candidates

1. The UI remove endpoint is not wired to `ContainerRemoveTool`.
2. The remove succeeds but the panel does not refresh (same family as
   `panel-polls-no-subscribe`).
3. The remove silently refuses (grain check on an ephemeral under some path).

## Read first

- `web_ui/backend/container_routes.py` (or equivalent) -- what does the
  remove-confirm POST call?
- `web_ui/frontend/src/components/...ContainerPanel*` -- does it optimistically
  remove from local state, or wait for a refetch?
- Does the response from the remove endpoint say success? Does the container
  actually disappear from `docker ps`?

## Member of Wave 10a

10a gets one member to verify the UI remove path end-to-end (start ephemeral
-> remove via panel -> verify in docker AND in panel).
