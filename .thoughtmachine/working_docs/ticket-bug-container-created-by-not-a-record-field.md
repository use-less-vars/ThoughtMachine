# Ticket: bug/container-created-by-not-a-record-field

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run (F7, the resize/recreate sequence); code verified at `124477b7`. Subject is now `created_by`: the record-field gap is the defect, the F7 sequence is its evidence path.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F7 under `## Findings` (line 353); run evidence `### Step 7a` (line 155) and `### Step 7a'` (line 193).
**Status:** filed, not dispatched.

## Defect

The primary defect is that `created_by` has no record home: it is not a field in `SCHEMA_FIELD_NAMES` (`thoughtmachine/container_record/models.py:78-107`) and exists only as the `thoughtmachine.created_by` Docker label (`thoughtmachine/container_record/hook.py:68`). Every other resource attribute lives on the record; this one lives in the live container, so it is destroyed by the operation that most needs it to survive. The resize sequence below is not a second bug — it is the evidence path, kept because it is the only place the fragility is presently survivable.

Resize is not an in-place limit change; it is a destroy-and-recreate that keeps the record. The sequence is fixed and ordered, at `web_ui/backend/server.py:4746-4943`:

1. validate the requested limits before anything is persisted or destroyed (`:4843-4870`);
2. recover `original_created_by` from the live label BEFORE the destroy (`:4879-4884`, comment `:4876-4878`) — after `remove()` the handle is gone and the label unreadable;
3. persist `intent_snapshot` first (`:4913`), so the container started below reads the new limits from its record;
4. `manager.remove(container_id, keep_record=True, audit_reason="resize_recreate", audit_source="server.workspace_container_resources")` (`:4919-4923`);
5. `manager.start(name=record.name, note=record.notes, allow_fresh=True, created_by=original_created_by, **_recorded_envelope(snapshot))` (`:4920-4924`);
6. rebind `docker_id` / `state="running"` (`:4927-4928`).

There is no `container.update()` call anywhere in the route or the manager. The new limits take effect only because `start()` re-reads the just-persisted snapshot: `infra/container_manager.py:1597-1604` (`_reuse_mem_limit` / `_reuse_cpu_quota`, gated by `snapshot_has_evidence`) consumed at `:1626-1640` and `:1756-1767`.

What survives, and why it is fragile:

- record `id` survives — `keep_record=True` (`:4919-4923`); `_recorded_envelope` (`:3926-3944`) forwards only the isolation axes (`network_mode`, `workspace_mode`), never the limits.
- `notes` survives — re-passed as `note=record.notes` (`:4861`, `:4920`).
- `created_by` survives ONLY because it is not a record field: it is absent from `SCHEMA_FIELD_NAMES` (`thoughtmachine/container_record/models.py:78-107`) and exists as the `thoughtmachine.created_by` Docker label (`thoughtmachine/container_record/hook.py:68`). Durability therefore depends on this one call site remembering `_original_created_by(...)`; any new recreate caller that forgets it stamps the acting operator onto an agent-created container irreversibly.
- The Docker container id is NEW after every resize, and so is `uptime_seconds` (`infra/container_manager.py:3749-3756`).

Failure path: `_resize_failed` restores `prev_snapshot`, attempts one rebuild with the previous limits, and either rebinds `docker_id` or unbinds the record (`docker_id=None, state="error"`) so the orphan sweeper treats it kindly rather than as a vanished container (`web_ui/backend/server.py:4885-4950`); audit code `CONTAINER_RESIZE_FAILED`.

## Bearing

Track containers by record uuid, not Docker id — the id changes on resize, restart-recreate, and any heal (`infra/container_manager.py:1690-1706`). Surface `created_by` as a record field so recreate durability stops depending on caller discipline. Any second recreate caller must copy the `:4879-4924` preamble verbatim.
