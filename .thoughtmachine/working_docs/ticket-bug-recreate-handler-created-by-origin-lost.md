# bug/recreate-handler-created-by-origin-lost

Same defect as `bug/created-by-tracks-last-create` (fixed by #XXX `83139d41`), but at a **different site**.

`web_ui/backend/server.py:4365-4376` — the record-user-action `recreate` handler has the same destroy-then-start shape and will also overwrite the `thoughtmachine.created_by` label with the operator's actor, losing the origin.

**Fix:** hoist the origin-recovery helper (currently `_original_created_by()`) to be shared near `_view_action_container_id`, call from both resize and recreate handlers.

Not fixed in `wave/2026-10-19-container-fixes` to keep the two-commit shape.
