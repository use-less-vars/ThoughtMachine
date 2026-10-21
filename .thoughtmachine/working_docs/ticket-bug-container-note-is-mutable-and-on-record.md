# Ticket: bug/container-note-is-mutable-and-on-record

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run (F4); code verified at `124477b7`
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F4 under `## Findings` (line 340); run evidence `### Step 3` (line 84) and `### Step 7a` (line 183).
**Status:** filed, not dispatched.

## Defect

`ticket-feat-container-note-on-existing.md` is written on the premise that the sticky note is creation-time only. That premise is stale: the note lives on the container RECORD and is mutated after creation.

- Storage: `_read_note` (`infra/container_manager.py:1146`) prefers the record and returns `record.notes` at `:1171`; the legacy `container_notes.json` sidecar is a read-only fallback (`:1175-1185`), migrated once by `_migrate_legacy_notes_once` (`:1214`).
- Write: `_write_note` → `update_record(..., notes=note)` at `infra/container_manager.py:1188,1207`, refusing when the container carries no record id (`:1199`).
- Live mutation path: the start REUSE path rewrites the note when `note is not None` — `infra/container_manager.py:1772-1773`. It is reachable from the agent because `ContainerStartTool.note` is an ordinary field (`tools/container_control.py:218`) forwarded as `note=self.note` (`:264`).
- Idempotent rewrite by the panel: `web_ui/backend/server.py:4439` (record restart/recreate), `:4861` and `:4920` (resize and its rollback) all pass `note=record.notes`.

What is genuinely missing is a VERB, not mutability. `set_note(container_id, note)` at `infra/container_manager.py:4576-4617` is fully implemented and fail-closed (`error="no container record"` at `:4610-4614`) — and has NO production caller: the only non-test reference in the repo is `live_smoke_docker.py:650`.

Consequence: an agent can change a note only by "starting" a container that already exists — a destructive-looking call used for a cosmetic write — and the response (`status: "reused"`, `infra/container_manager.py:1782-1790`) is indistinguishable from a genuine start.

Fix shape: keep the record storage, expose an explicit note verb (tool + route) delegating to `set_note`, and correct the premise in the feature ticket rather than re-specifying storage.

## Bearing

`ticket-feat-container-note-on-existing.md` (premise stale — amend, do not dispatch as written). Protocol steps that compare a note before and after a lifecycle action depend on the record surviving the destroy; that holds only because resize passes `keep_record=True` (`web_ui/backend/server.py:4919-4923`) and the record-recreate path does the same (`:4431-4434`).
