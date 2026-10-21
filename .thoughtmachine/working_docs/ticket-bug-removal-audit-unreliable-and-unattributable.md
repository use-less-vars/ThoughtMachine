# Ticket: bug/removal-audit-unreliable-and-unattributable

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run (F8); code verified at `124477b7`. Renamed from bug/container-removal-audit-not-attributable — the swallowed publish failure is the sharper half and the title must carry both.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F8 under `## Findings` (line 361); run evidence `### Step 9` (line 253).
**Status:** filed, not dispatched.

## Defect

Two defects share one event. The audit trail is *unattributable* — no actor field, and `session_id` is `None` on every site that is not the manager's own remove. It is also *unreliable* — publish is wrapped in a bare `try/except: pass` (`infra/container_manager.py:4937-4945`), so an event that cannot be attributed is also an event that can vanish without trace. Either alone would be a completeness gap; together they mean the removal of a shared container can leave no durable evidence at all.

The `container_removed` event cannot attribute who removed a container, and frequently cannot even name the session.

- Event name: `agent/events.py:95` `CONTAINER_REMOVED = 'container_removed'`.
- Emitter: `infra/container_manager.py:4923` `def _emit_container_removed(workspace_id, session_id, container, reason="removed")`.
- Payload, verbatim (`:4930-4936`): `{container_id, name, workspace_id, session_id, reason}` — no `actor`, no `created_by`, no labels, no limits/envelope. `source="infra.container_manager.remove"`, `session_id=session_id` (`:4939-4942`).
- Publish is best-effort inside a bare `try/except: pass` (`:4937-4945`), so a failed publish leaves no trace at all.

`session_id` is `None` on every site that is not the manager's own remove:

- workspace cleanup — `infra/container_manager.py:5017` `_emit_container_removed(wid, None, container, reason="workspace_cleanup")`;
- executor teardown — `infra/containers/executor.py:619` `_cm_emit_container_removed(workspace_id, None, container, reason="removed")`;
- resource containers — `infra/resource_container_manager.py:1703` and `:2137` (both pass `None`).

Only the manager wrapper supplies a real session: `infra/container_manager.py:3711-3719` passes `self.workspace_id, self.session_id`. Remove itself emits `reason="removed"` at `:4864` (docstring `:4841`); the recreate path tags `reason="recreate"` (`:3625`).

Consequence: for an operator-only, irreversible action the event stream records WHAT was destroyed and why, never WHO. Correlation with the audit log is the only route, and the audit line is only written when the caller supplies one — resize for instance writes its own into the route (`web_ui/backend/server.py:4917`, `audit_source="server.workspace_container_resources"`).

Fix shape: add an `actor` field to the payload and thread a real `session_id` through the cleanup/executor/resource sites; stop swallowing publish failures silently (log at WARNING minimum).

## Bearing

Any post-hoc attribution question about a removed container (F8 itself, and the protocol's removal step) cannot be answered from the event stream alone. Also bears on `ticket-bug-container-created-by-not-a-record-field.md`: `remove()` takes `audit_reason`/`audit_source` (`infra/container_manager.py:3613` onward) separately from the event's own `reason` argument (`:4923`), so the resize attribution lives in the audit log, not in the event stream.
