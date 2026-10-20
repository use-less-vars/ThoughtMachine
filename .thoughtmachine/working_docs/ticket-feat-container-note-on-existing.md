# Ticket: feat/container-note-on-existing

**Filed:** Gen 45, 2026-10-17, from the container-remove-grain wave.
**Severity:** P1 — prerequisite for agent-side persistent removal.
**Status:** filed, not dispatched. **P1 since 2026-10-17** (Main ruling).

## Finding

The operator ruling for container Control (§5) relies on the `note` field
on the container record as the coordination mechanism: one session leaves
a note, another reads it and acts in good faith.

But the note can **only be set at container start**:
- `POST /api/workspace/{ws}/containers` accepts `note` in the body
- `ContainerStartTool` accepts `note` as a parameter

There is **no** way to set or change a note on an existing container —
no route, no UI, no tool.

## Why it matters

The scenario the ruling depends on: a session finishes work with a
persistent container it started but doesn't want to keep. It wants to
write "safe to remove now — work finished". A different session later
sees the note and removes it in good faith.

Without note-on-existing, the finished session cannot signal this. The
coordination mechanism only fires at creation time — which is exactly
when there's nothing to coordinate yet.

## Shape

- Endpoint: `POST /api/workspace/{ws}/containers/{name}/note` (or `PATCH`)
- Tool: extend `ContainerRemoveTool` (or new `ContainerNoteTool`)
- Panel: inline edit affordance on the note line in the row
- Permission: same envelope as start/stop (`container: true`)

## Reference

Operator ruling text: `.thoughtmachine/working_docs/wave-container-remove-grain-removal.md`
Container contract §5.

## Amendment 2026-10-20 (gen-47 run)

**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F4 (line 340); run evidence §Step 3 (line 84), §Step 7a (line 183).

The "creation-time only" premise above is **STALE**. The note lives on the container **record** (`infra/container_manager.py:1146`, which returns `record.notes` at `:1171`) and is mutable on the start **reuse** path (`infra/container_manager.py:1772-1773`), reachable by the agent through `ContainerStartTool.note` (`tools/container_control.py:218`, forwarded `:264`). It also survives a re-create, because the record survives it (`keep_record=True` at `web_ui/backend/server.py:4919-4923`).

Critically, the primitive already exists: `set_note` (`infra/container_manager.py:4576-4617`) is fully implemented, fail-closed on a missing record (`:4610-4614`) and tested, but has **zero production callers** — its only caller is `live_smoke_docker.py:650`. So this ticket should be scoped as "expose the primitive that already exists with a real verb/route" (write path + tool verb + panel affordance + grain), not "make notes mutable". The residue after the premise correction is surface, not mechanism.
