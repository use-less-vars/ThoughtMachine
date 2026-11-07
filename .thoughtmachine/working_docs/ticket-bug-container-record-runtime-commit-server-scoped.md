# Ticket: bug/container-record-runtime-commit-server-scoped

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run (F1); code verified at `124477b7`
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F1 under `## Findings` (line 320); run evidence `### Step 1` (line 51).
**Status:** filed, not dispatched.

## Defect

`runtime_commit` is not a container fact and is not a record field.

- The record schema has no such key: `SCHEMA_FIELD_NAMES` at `thoughtmachine/container_record/models.py:78-107` lists `id, docker_id, lifecycle_class, owner, purpose, intent_snapshot, notes, event_log, schema_version, inferred, state, created_at, updated_at, restart_policy, name, retention_days, user, permissions` and nothing else; `grep runtime_commit thoughtmachine/container_record/*.py` is 0 hits.
- The value is resolved lazily at serialisation time from the LIVE server process: `tools/container_control.py:55` `def _runtime_commit()` imports `web_ui.backend.server._SERVER_REVISION` inside the function body and returns `"unknown"` on any exception (`:55-67`). Wired into the status payload at `tools/container_control.py:688`; forced to `None` for a missing container at `:739`.
- The panel list path aliases the same server revision onto every entry: `tools/workspace/check_system.py:473-476` `result.setdefault("runtime_commit", _server_revision())`.

Consequence: two containers built from different images thirteen hours apart both report the CURRENT server revision. The field cannot date a container, cannot identify its image, and cannot be compared across a panel restart.

The per-image identity the field is being mistaken for already exists as a Docker label: `EXECUTOR_BUILD_HASH_LABEL = "thoughtmachine.build_hash"` at `infra/containers/executor.py:83`, applied as an image label at `infra/containers/executor.py:1343`, `infra/container_manager.py:4312`, `infra/resource_container_manager.py:656,689`, and forwarded onto the created container at `infra/container_create.py:351` (`create_hardened_container`, labels at `:423,442,453`).

Fix shape: rename the field so its scoping is explicit (server revision, resolved live), and surface `build_hash` as a first-class status/record field read from `container.labels`.

## Bearing

Every consumer that reads `runtime_commit` as a container property is reading the panel's own revision: `ContainerStatusTool` (`tools/container_control.py:688`) and the panel container list (`tools/workspace/check_system.py:473-476`). Any provenance or drift conclusion drawn from it is unsound. Tests already pin the current shape (`tests/test_wave10a_runtime_commit_and_version.py`), so the rename needs a coordinated test update.
