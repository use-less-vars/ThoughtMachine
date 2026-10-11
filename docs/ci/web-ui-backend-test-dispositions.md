# web-ui backend tests — strand dispositions

- **Strand branch:** `ci/web-ui-backend-tests-outside-testpaths`
- **Base commit:** `f6cd5037` (modifications below are the uncommitted working-tree delta)
- **Scope:** `web_ui/backend/tests/` sits **outside** root `testpaths`
  (`pyproject.toml` → `testpaths = ["tests"]`), so its 125 tests never run in
  gating CI. This strand lands an **advisory** CI job, triages the failing
  tests against the new fail-closed permission contract, and records each
  disposition. `pyproject.toml` is intentionally **NOT** changed here.
- **Fail-closed contract (bulk of the changes):** a workspace whose
  `config.json` is **absent**, has **no `permissions` key**, or has an **empty
  `permissions` map** resolves every grain to **BANNED** — the effective value
  is denied, never passed through raw, and legacy session-record metadata is
  never resurrected as a grant (the sidecar is the sole grants source).

## Dispositions

`assertion changed?` = `no`, or `yes: <old> -> <new>`.

| node id | disposition | what changed | assertion changed? |
|---|---|---|---|
| `test_config_pipeline.py::TestApplyConfig::test_apply_config_updates_bridge_config` | STALE-HARNESS | added `_restore_real_session_types(monkeypatch)` call to repair MagicMock session-class leakage left in `sys.modules` by `test_bridge_dedup.py` | no |
| `test_workspace_routes.py::TestPutPermissionsAllowHostResources::test_explicit_true_persists_and_is_reflected` | STALE-HARNESS | `fake_ws_dir` fixture now also redirects the config **read** seam (`workspace_routes.load_workspace_config`) | no |
| `test_workspace_routes.py::TestPutPermissionsAllowHostResources::test_explicit_false_persists` | STALE-HARNESS | same fixture read-seam redirect | no |
| `test_workspace_routes.py::TestPutPermissionsAllowHostResources::test_omitted_field_leaves_saved_value_untouched` | STALE-HARNESS | same fixture read-seam redirect | no |
| `test_workspace_routes.py::TestPutPermissionsAllowHostResources::test_omitted_field_still_updates_permissions` | STALE-HARNESS | same fixture read-seam redirect | no |
| `test_workspace_routes.py::TestGetPermissionsAllowHostResources::test_get_reflects_persisted_value` | STALE-HARNESS | same fixture read-seam redirect | no |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_put_get_permissions_roundtrip` | STALE-PIN | seeded an explicit workspace ceiling via new `_write_ceiling()` helper | no |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_put_get_permissions_host_bash_ask_roundtrip` | STALE-PIN | seeded ceiling + `allow_host_resources=True` (host_bash hard-gate) | no |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_put_get_permissions_host_bash_allow_roundtrip` | STALE-PIN | seeded ceiling + `allow_host_resources=True` | no |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_put_empty_body_resets_to_safe_defaults` | STALE-PIN | seeded a `SAFE_DEFAULTS` ceiling | no |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_no_ceiling_fails_closed_to_banned` | STALE-PIN | renamed from `test_no_ceiling_lets_raw_stand`; comment records the fail-closed read | yes: effective `git` `"write"` -> `"banned"` |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_put_git_write_on_feature_branch_persists` | STALE-PIN | seeded ceiling (git `write_on_feature_branch`) | no |
| `test_permission_routes.py::TestSessionPermissionsRoutes::test_get_falls_back_to_legacy_record_permissions` | XFAIL | pinned `@pytest.mark.xfail(reason="...bug/legacy-record-read-500s")` | no |
| `test_permission_routes.py::TestWorkspacePermissionsRoutes::test_get_fresh_workspace_returns_safe_defaults` | STALE-PIN | renamed from `test_get_fresh_workspace_returns_empty_permissions`; comment records the safe-default seed | yes: stored/effective map `{}` -> `CATALOG_DEFAULTS` |
| `test_permission_routes.py::TestWorkspaceEffectivePermissionsRoutes::test_effective_reads_sidecar_after_session_put` | STALE-PIN | seeded ceiling (`{"filesystem": "write"}`) | no |
| `test_permission_routes.py::TestWorkspaceEffectivePermissionsRoutes::test_effective_sidecar_supersedes_stale_metadata` | STALE-PIN | seeded ceiling; comment reworded (sidecar wins while present; absent sidecar fail-closes, legacy metadata NOT resurrected) | yes: `filesystem` `"read"` -> `"banned"` |
| `test_permission_routes.py::TestWorkspaceEffectivePermissionsRoutes::test_effective_unknown_session_returns_deny_all` | STALE-PIN | renamed from `test_effective_unknown_session_returns_read_only_default`; comment records the deny-all fail-closed read (unknown session = unreadable grants path) | yes: effective `filesystem` `"read"` -> `"banned"`; `git`/`mcp`/`host_bash` also `-> "banned"` |
| `test_worker_stats_routes.py::TestWorkersEndpointStatsFields::test_time_since_last_query_and_pruned_counter` | XFAIL | pinned `@pytest.mark.xfail(reason="...bug/worker-stats-missing-fields")` | no |
| `test_worker_stats_routes.py::TestWorkersEndpointStatsFields::test_name_filter_includes_new_fields` | XFAIL | same `xfail` pin | no |
| `test_worker_stats_routes.py::TestWorkersEndpointStatsFields::test_unparseable_heartbeat_yields_none` | XFAIL | same `xfail` pin | no |
| `test_worker_stats_routes.py::TestWorkersEndpointStatsFields::test_corrupt_context_json_does_not_crash` | XFAIL | same `xfail` pin | no |
| `test_session_size_routes.py::TestGetSessionSize::test_detail_size_none_for_non_main_session` | STALE-HARNESS | added `session.metadata['agent_type'] = 'worker'` so the fixture is really a non-main session | no |
| `test_session_size_routes.py::TestListSessionsSize::test_list_size_none_for_non_main_session` | STALE-HARNESS | same `agent_type = 'worker'` fixture fix | no |

> Note: `TestPutPermissionsValidation::test_unknown_resource_rejected_with_422`
> and `::test_invalid_level_rejected_with_422` share the same `fake_ws_dir`
> fixture but assert a 422 rejection — they were green before and after the
> read-seam redirect, so they carry no disposition here.

## Tickets filed

- `bug/worker-stats-missing-fields` — the workers-stats endpoint never emits the computed `time_since_last_query` / `pruned_since_last_query` fields; the 4 `TestWorkersEndpointStatsFields` tests are pinned `xfail`.
- `hygiene/conftest-guard-duplication` — `web_ui/backend/tests/conftest.py`'s hermetic-vault guard near-duplicates root `tests/conftest.py`; extract to one shared importable module (left as-is in this strand; report-only).
- `bug/legacy-record-read-500s` — reading a session that has only a legacy record (no permission sidecar) serves a `read` grant and 500s instead of failing closed to the deny-all sentinel; `test_get_falls_back_to_legacy_record_permissions` is pinned `xfail` and the residual half of `test_effective_sidecar_supersedes_stale_metadata` is blocked on it.

Related standing ticket: `ci/promote-web-ui-tests-to-testpaths` (definition-of-done item 4 now requires all advisory-era `xfail`s to go green before `web_ui/backend/tests/` is moved into `testpaths`).

## Known red

`test_permission_routes.py::TestWorkspaceEffectivePermissionsRoutes::test_effective_unknown_session_returns_deny_all` (renamed from `test_effective_unknown_session_returns_read_only_default`) — **RULED (R1) → now GREEN**. The handler deny-alls an unknown session (fail-closed), so the stale pin was corrected to match that contract: the assertion now pins the deny-all sentinel (`filesystem`/`git`/`mcp`/`host_bash`/`network` all `"banned"`, `container` `False`). With this fix the advisory suite is **fully green** (`120 passed, 5 xfailed`; no red).
