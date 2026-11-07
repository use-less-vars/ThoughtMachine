# Ticket: bug/prompt-injection-unauthenticated

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit).
**Severity:** **CRITICAL** — an unauthenticated POST can rewrite the agent's system prompt for every future session, durably, with no build step and no origin check.
**Status:** open; verified by read at `24012a89`; the effect on the agent is inferred from the loader call named below, not observed in a live session — `UNRUN: no live session`.

**Parent pointer:** `ticket-bug-unauthenticated-routes-beyond-health.md` is the generic pointer for "the Web UI API has no authentication at all". It is **not** the owner of this row: it lists `/api/vault/status`, `/api/vault/*`, `/api/containers*`, `/api/browse*` and `/ws`, never names `prompt_routes.py`, and describes itself as `open; pointer, **not** an exhaustive map — Cluster 2 owns the full route inventory.`. This ticket owns the specific row: unauthenticated write of the agent system prompt.

## Finding

`POST /api/prompts/{filename}` (`web_ui/backend/prompt_routes.py`, handler `save_prompt`) takes a caller-supplied filename and body and writes `<name>.txt` at the vault root. The only check is on the **filename's shape** — `_resolve_filename` rejects `os.sep`, `/` and `\`, and appends `.txt`. There is no ownership check, no session binding and no permission gate; and, unlike the vault-repair family, no `_origin_allowed` call.

## Why this is durable

`custom_system_prompt.txt` is read by `load_custom_system_prompt()` in `agent/config/loader.py` and injected into the agent system message. One unauthenticated request therefore persists attacker-chosen instructions into the context of every subsequent session, across every workspace.

Three asymmetries, all raising this above the Dockerfile write as filed:

1. **No build step.** The Dockerfile needs `rebuild_container` (F7) to become execution; this needs only the next session to start.
2. **No origin check.** `_origin_allowed` (`web_ui/backend/origin_policy.py`) is called from exactly two places in the backend — `vault_repair_status` and `vault_repair_apply`, both in `vault_repair_routes.py` — plus one alias call to `websocket_origin_allowed` at the WS handshake. `prompt_routes.py` calls neither. So the vault-repair route carries a gate that this route does not.
3. **Cross-workspace by construction.** The system prompt is global, not per-workspace, so there is no workspace boundary to cross — there is one prompt and it applies everywhere.

## Sibling

`DELETE /api/prompts/{filename}` (`delete_prompt`) applies the same shape-only gate and can remove the system prompt outright and irreversibly. Same row, lower severity; recorded so that any fix covers both verb and both handlers.

## Blast radius

Any client that can route to the port can install or remove the system prompt that every agent session runs under, with no credential and no build. Considered as a persistence primitive it outlasts the process: the file survives restart.

## What this is not

- Not path traversal: the filename is shape-validated against separators before the join.
- Not observed end-to-end: no live session was started to watch the injected text reach the model's context. The claim rests on a named function reading a named path at session start — `load_custom_system_prompt()` → `custom_system_prompt.txt` — not on a runtime trace. `UNRUN: no live session`.
- Not the same row as the Dockerfile write: different handler, different route module, different mechanism, no shared code path.

## Fix shape

Not specified here. At minimum the prompts family needs the same caller binding as any other vault write; see `docs/design-input-auth-layer-prerequisite.md` for why a per-route check is not the whole answer.
