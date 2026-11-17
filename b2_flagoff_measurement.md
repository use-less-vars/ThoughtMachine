# C76 2b-ii-b-2a — flag-OFF measurement (conftest trust harness disarmed)

Worktree: `/workspace/.worktrees/c76-trust-harness` · container `c731a155fa49` (`c76-b1c-verify3c`) · python3 3.12.15 · pytest 9.2.0 · integration tests excluded.

## Method

- Target set: 26 `tests/` files, 257 node ids, split into 8 weight-balanced batches (identical in both passes).
- PASS1 = flag ON (pristine `conftest.py`, sha256 `5aeb59e8…8bd`); PASS2 = flag OFF (`conftest.py` mutated inside `_trust_the_in_process_asgi_harness`: line 170 `set_suite_trust_value(False)`, line 171 `auth_boundary.enable_test_harness_trust(False)`; sha256 `93f2eef3…38b7`). Line 250 `boundary.enable_test_harness_trust(True)` belongs to a different fixture and was left untouched.
- Logs: `logs/b2_batch{A..H}_on.log`, `logs/b2_batch{A..H}_off.log` (`-q -rf -p no:randomly`).
- `conftest.py` restored byte-identically afterwards (sha256 re-checked = `5aeb59e88a3c2fac0ca98c007768fabf23b989208385d9bf0b955087a202b8bd`).

## PASS1 (flag ON) — batch summaries

| batch | files | node ids | result |
|---|---|---|---|
| A | 1 | 53 | 53 passed, 26 warnings in 1.25s |
| B | 1 | 36 | 36 passed, 26 warnings in 18.09s |
| C | 1 | 28 | 28 passed, 26 warnings in 1.18s |
| D | 1 | 21 | 21 passed, 26 warnings in 1.30s |
| E | 1 | 19 | 19 passed, 26 warnings in 1.43s |
| F | 5 | 45 | 45 passed, 26 warnings in 12.35s |
| G | 8 | 42 | 42 passed, 26 warnings in 11.43s |
| H | 8 | 13 | 13 passed, 26 warnings in 6.76s |

**PASS1 total: 257 passed, 0 failed, 0 skipped.**

## PASS2 (flag OFF) — batch summaries

| batch | files | node ids | result |
|---|---|---|---|
| A | 1 | 53 | 23 failed, 30 passed, 26 warnings in 1.56s |
| B | 1 | 36 | 32 failed, 4 passed, 26 warnings in 18.06s |
| C | 1 | 28 | 25 failed, 3 passed, 26 warnings in 1.29s |
| D | 1 | 21 | 17 failed, 4 passed, 26 warnings in 1.13s |
| E | 1 | 19 | 19 failed, 26 warnings in 1.13s |
| F | 5 | 45 | 34 failed, 11 passed, 26 warnings in 6.44s |
| G | 8 | 42 | 29 failed, 13 passed, 26 warnings in 9.14s |
| H | 8 | 13 | 11 failed, 2 passed, 26 warnings in 5.56s |

**PASS2 total: 67 passed, 190 failed, 0 skipped, 0 errors.**
Every PASS2 failure is a flag-dependent node: it passed with the flag ON (PASS1) and failed with it OFF. **Flag-ON-only failures: none.**

## Per-file measured counts

| file | collected | PASS1 pass | PASS2 pass | PASS2 fail (flag-dependent) |
|---|---|---|---|---|
| `tests/test_container_resources_resize_route.py` | 53 | 53 | 30 | 23 |
| `tests/test_host_api_hardening.py` | 36 | 36 | 4 | 32 |
| `tests/test_workspace_containers_view.py` | 28 | 28 | 3 | 25 |
| `tests/test_container_record_user_action_routes.py` | 21 | 21 | 4 | 17 |
| `tests/test_restart_preserves_envelope.py` | 19 | 19 | 0 | 19 |
| `tests/test_resize_preserves_envelope.py` | 12 | 12 | 5 | 7 |
| `tests/test_onboarding.py` | 11 | 11 | 0 | 11 |
| `tests/test_container_logs_route.py` | 8 | 8 | 0 | 8 |
| `tests/web_ui/backend/test_ws_mock_provider.py` | 7 | 7 | 1 | 6 |
| `tests/test_system_health_local_only.py` | 7 | 7 | 5 | 2 |
| `tests/test_safe_defaults_single_owner.py` | 7 | 7 | 6 | 1 |
| `tests/test_created_by_preserved_on_recreate.py` | 7 | 7 | 2 | 5 |
| `tests/test_vault_status_endpoint.py` | 6 | 6 | 0 | 6 |
| `tests/test_seeding_path_invariants.py` | 6 | 6 | 1 | 5 |
| `tests/security/test_git_execution_mode_field.py` | 5 | 5 | 4 | 1 |
| `tests/web_ui/backend/test_websocket_integration.py` | 4 | 4 | 0 | 4 |
| `tests/test_workspace_container_status_hardening_verdict.py` | 4 | 4 | 0 | 4 |
| `tests/test_resource_catalog_endpoint.py` | 3 | 3 | 0 | 3 |
| `tests/test_vault_status_actions.py` | 2 | 2 | 0 | 2 |
| `tests/test_server_logger_registration.py` | 2 | 2 | 2 | 0 |
| `tests/test_r13_step6_vocabulary_closure.py` | 2 | 2 | 0 | 2 |
| `tests/test_permission_sidecar_clobber_guard.py` | 2 | 2 | 0 | 2 |
| `tests/test_open_sessions_ws_payload.py` | 2 | 2 | 0 | 2 |
| `tests/test_resource_catalog_grain_projection.py` | 1 | 1 | 0 | 1 |
| `tests/test_permission_refresh_ws_clobber_guard.py` | 1 | 1 | 0 | 1 |
| `tests/test_first_run.py` | 1 | 1 | 0 | 1 |
| **total (26 files)** | **257** | **257** | **67** | **190** |

## Proposed chunks (marker budget ≤ 60 node ids per chunk)

Rules applied: a file never straddles two chunks; `test_restart_preserves_envelope.py` (19) and `test_resize_preserves_envelope.py` (12) stay intact as all-or-nothing units; `test_system_health_local_only.py` is leaf-selective (only 2 of its 7 leaves are flag-dependent).

| chunk | markers | files (markers) |
|---|---|---|
| chunk 1 | 58 | `tests/test_container_resources_resize_route.py` (23), `tests/test_host_api_hardening.py` (32), `tests/test_resource_catalog_endpoint.py` (3) |
| chunk 2 | 60 | `tests/test_workspace_containers_view.py` (25), `tests/test_restart_preserves_envelope.py` (19), `tests/test_resize_preserves_envelope.py` (7), `tests/test_created_by_preserved_on_recreate.py` (5), `tests/web_ui/backend/test_websocket_integration.py` (4) |
| chunk 3 | 59 | `tests/test_container_record_user_action_routes.py` (17), `tests/test_onboarding.py` (11), `tests/test_container_logs_route.py` (8), `tests/web_ui/backend/test_ws_mock_provider.py` (6), `tests/test_vault_status_endpoint.py` (6), `tests/test_seeding_path_invariants.py` (5), `tests/test_workspace_container_status_hardening_verdict.py` (4), `tests/test_system_health_local_only.py (leaf subset)` (2) |
| chunk 4 | 13 | `tests/security/test_git_execution_mode_field.py` (1), `tests/test_safe_defaults_single_owner.py` (1), `tests/test_vault_status_actions.py` (2), `tests/test_resource_catalog_grain_projection.py` (1), `tests/test_permission_refresh_ws_clobber_guard.py` (1), `tests/test_permission_sidecar_clobber_guard.py` (2), `tests/test_open_sessions_ws_payload.py` (2), `tests/test_r13_step6_vocabulary_closure.py` (2), `tests/test_first_run.py` (1) |

Sum = 190 markers = all 190 flag-dependent nodes. `tests/test_server_logger_registration.py` (2 node ids) never failed → flag-independent, stays out of the gated chunks.

## Flag-dependent node ids (PASS2 `-rf` failures)

### `tests/security/test_git_execution_mode_field.py` — 1 of 5

- `tests/security/test_git_execution_mode_field.py::test_endpoint_entries_carry_execution_mode_only`

### `tests/test_container_logs_route.py` — 8 of 8

- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_happy_path_matches_tool_shape`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_manager_construction_failure_is_503`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_negative_tail_rejected`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_not_found_runtime_error_is_404`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_other_runtime_error_is_503`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_tail_clamped_to_max`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_unknown_container_404`
- `tests/test_container_logs_route.py::TestContainerLogsRoute::test_unresolvable_workspace_is_404`

### `tests/test_container_record_user_action_routes.py` — 17 of 21

- `tests/test_container_record_user_action_routes.py::test_actor_malformed_400`
- `tests/test_container_record_user_action_routes.py::test_body_actor_overrides_header`
- `tests/test_container_record_user_action_routes.py::test_decisions_only_canonical_vocabulary`
- `tests/test_container_record_user_action_routes.py::test_fail_closed_unknown_workspace_denied`
- `tests/test_container_record_user_action_routes.py::test_kill_audits_both_channels`
- `tests/test_container_record_user_action_routes.py::test_kill_success`
- `tests/test_container_record_user_action_routes.py::test_manager_status_error_maps_to_503`
- `tests/test_container_record_user_action_routes.py::test_missing_workspace_id_400`
- `tests/test_container_record_user_action_routes.py::test_permission_denied_403_audited`
- `tests/test_container_record_user_action_routes.py::test_record_without_container_409`
- `tests/test_container_record_user_action_routes.py::test_record_without_name_409`
- `tests/test_container_record_user_action_routes.py::test_recreate_route_tells_manager_to_keep_record`
- `tests/test_container_record_user_action_routes.py::test_recreate_success_real_ladder`
- `tests/test_container_record_user_action_routes.py::test_restart_success_stop_then_start`
- `tests/test_container_record_user_action_routes.py::test_start_error_maps_to_409_conflict`
- `tests/test_container_record_user_action_routes.py::test_unknown_record_404`
- `tests/test_container_record_user_action_routes.py::test_wrong_workspace_403`

### `tests/test_container_resources_resize_route.py` — 23 of 53

- `tests/test_container_resources_resize_route.py::test_invalid_cpu_quota_is_400_and_leaves_everything_untouched[-1]`
- `tests/test_container_resources_resize_route.py::test_invalid_cpu_quota_is_400_and_leaves_everything_untouched[1.5]`
- `tests/test_container_resources_resize_route.py::test_invalid_cpu_quota_is_400_and_leaves_everything_untouched[1000]`
- `tests/test_container_resources_resize_route.py::test_invalid_cpu_quota_is_400_and_leaves_everything_untouched[True]`
- `tests/test_container_resources_resize_route.py::test_invalid_mem_limit_is_400_and_leaves_everything_untouched[-1]`
- `tests/test_container_resources_resize_route.py::test_invalid_mem_limit_is_400_and_leaves_everything_untouched[1.5]`
- `tests/test_container_resources_resize_route.py::test_invalid_mem_limit_is_400_and_leaves_everything_untouched[1g extra]`
- `tests/test_container_resources_resize_route.py::test_invalid_mem_limit_is_400_and_leaves_everything_untouched[None]`
- `tests/test_container_resources_resize_route.py::test_invalid_mem_limit_is_400_and_leaves_everything_untouched[]`
- `tests/test_container_resources_resize_route.py::test_invalid_mem_limit_is_400_and_leaves_everything_untouched[banana]`
- `tests/test_container_resources_resize_route.py::test_mem_limit_at_the_docker_floor_still_resizes[6291456]`
- `tests/test_container_resources_resize_route.py::test_mem_limit_at_the_docker_floor_still_resizes[6m]`
- `tests/test_container_resources_resize_route.py::test_mem_limit_below_docker_floor_is_400_and_destroys_nothing[0]`
- `tests/test_container_resources_resize_route.py::test_mem_limit_below_docker_floor_is_400_and_destroys_nothing[1m]`
- `tests/test_container_resources_resize_route.py::test_mem_limit_below_docker_floor_is_400_and_destroys_nothing[5m]`
- `tests/test_container_resources_resize_route.py::test_mem_limit_below_docker_floor_is_400_and_destroys_nothing[6291455]`
- `tests/test_container_resources_resize_route.py::test_rebuild_failure_unbinds_the_record_docker_id`
- `tests/test_container_resources_resize_route.py::test_recovery_rebuild_runs_with_the_previous_limits`
- `tests/test_container_resources_resize_route.py::test_start_error_dict_is_400_and_restores_previous_limits`
- `tests/test_container_resources_resize_route.py::test_start_exception_status_code_maps_to_http_status[400-400]`
- `tests/test_container_resources_resize_route.py::test_start_exception_status_code_maps_to_http_status[500-503]`
- `tests/test_container_resources_resize_route.py::test_success_labels_the_destroy_as_a_resize_and_rebinds`
- `tests/test_container_resources_resize_route.py::test_valid_resize_keeps_record_id_and_remove_keeps_record`

### `tests/test_created_by_preserved_on_recreate.py` — 5 of 7

- `tests/test_created_by_preserved_on_recreate.py::test_fallback_to_the_acting_operator_when_the_label_is_unavailable[blank]`
- `tests/test_created_by_preserved_on_recreate.py::test_fallback_to_the_acting_operator_when_the_label_is_unavailable[missing]`
- `tests/test_created_by_preserved_on_recreate.py::test_fallback_to_the_acting_operator_when_the_label_is_unavailable[unreadable]`
- `tests/test_created_by_preserved_on_recreate.py::test_origin_is_stamped_on_the_resize_failed_recovery_too`
- `tests/test_created_by_preserved_on_recreate.py::test_recreate_stamps_the_origin_not_the_acting_operator`

### `tests/test_first_run.py` — 1 of 1

- `tests/test_first_run.py::test_full_first_run_lifecycle`

### `tests/test_host_api_hardening.py` — 32 of 36

- `tests/test_host_api_hardening.py::test_browse_home_ok`
- `tests/test_host_api_hardening.py::test_browse_nonexistent_dir`
- `tests/test_host_api_hardening.py::test_browse_outside_home_dir_allowed`
- `tests/test_host_api_hardening.py::test_browse_rejects_symlink_into_vault`
- `tests/test_host_api_hardening.py::test_browse_rejects_vault`
- `tests/test_host_api_hardening.py::test_browse_root_allowed`
- `tests/test_host_api_hardening.py::test_container_list_rejects_vault_path`
- `tests/test_host_api_hardening.py::test_container_start_empty_path_falls_back_to_registry`
- `tests/test_host_api_hardening.py::test_container_start_rejects_etc_outside_root_403`
- `tests/test_host_api_hardening.py::test_container_start_rejects_nonexistent`
- `tests/test_host_api_hardening.py::test_container_start_rejects_sibling_dir_403`
- `tests/test_host_api_hardening.py::test_container_start_rejects_unregistered_ws_root_400`
- `tests/test_host_api_hardening.py::test_container_start_rejects_vault_path`
- `tests/test_host_api_hardening.py::test_container_start_unknown_ws_explicit_path_400`
- `tests/test_host_api_hardening.py::test_create_absolute_name_outside_home_allowed`
- `tests/test_host_api_hardening.py::test_create_directory_ok`
- `tests/test_host_api_hardening.py::test_create_outside_home_parent_allowed`
- `tests/test_host_api_hardening.py::test_create_rejects_absolute_name_in_vault`
- `tests/test_host_api_hardening.py::test_create_rejects_traversal`
- `tests/test_host_api_hardening.py::test_create_rejects_vault_parent`
- `tests/test_host_api_hardening.py::test_record_action_body_actor_passes_attribution_guard`
- `tests/test_host_api_hardening.py::test_record_action_rejects_malformed_actor_400`
- `tests/test_host_api_hardening.py::test_record_action_rejects_overlong_actor_400`
- `tests/test_host_api_hardening.py::test_record_action_requires_workspace_id_400`
- `tests/test_host_api_hardening.py::test_resolve_pre_registered_outside_home_still_resolves`
- `tests/test_host_api_hardening.py::test_resolve_registered_under_home_returns_existing`
- `tests/test_host_api_hardening.py::test_resolve_registers_new_path_under_home`
- `tests/test_host_api_hardening.py::test_resolve_registers_outside_home_absolute_200`
- `tests/test_host_api_hardening.py::test_resolve_rejects_empty_path_400`
- `tests/test_host_api_hardening.py::test_resolve_rejects_nonexistent_400`
- `tests/test_host_api_hardening.py::test_resolve_rejects_symlink_into_vault_403`
- `tests/test_host_api_hardening.py::test_resolve_rejects_vault_403`

### `tests/test_onboarding.py` — 11 of 11

- `tests/test_onboarding.py::TestConnection::test_connection_failure_chat_completion_raises`
- `tests/test_onboarding.py::TestConnection::test_connection_failure_no_key_leak`
- `tests/test_onboarding.py::TestConnection::test_connection_invalid_provider`
- `tests/test_onboarding.py::TestConnection::test_connection_ok`
- `tests/test_onboarding.py::TestOnboardingComplete::test_complete_and_status_true`
- `tests/test_onboarding.py::TestOnboardingComplete::test_complete_idempotent`
- `tests/test_onboarding.py::TestOnboardingComplete::test_complete_repairs_malformed_marker`
- `tests/test_onboarding.py::TestOnboardingStatus::test_status_false_when_empty`
- `tests/test_onboarding.py::TestOnboardingStatus::test_status_false_with_malformed_marker`
- `tests/test_onboarding.py::TestOnboardingStatus::test_status_true_with_provider_profile`
- `tests/test_onboarding.py::TestOnboardingStatus::test_status_true_with_workspace`

### `tests/test_open_sessions_ws_payload.py` — 2 of 2

- `tests/test_open_sessions_ws_payload.py::test_open_sessions_payload_carries_workspace_id_per_session`
- `tests/test_open_sessions_ws_payload.py::test_open_sessions_reports_unknown_workspace_as_empty_string`

### `tests/test_permission_refresh_ws_clobber_guard.py` — 1 of 1

- `tests/test_permission_refresh_ws_clobber_guard.py::test_ws_load_session_refresh_does_not_clobber_rest_written_sidecar`

### `tests/test_permission_sidecar_clobber_guard.py` — 2 of 2

- `tests/test_permission_sidecar_clobber_guard.py::test_config_save_does_not_clobber_rest_written_sidecar`
- `tests/test_permission_sidecar_clobber_guard.py::test_stale_nonempty_p2_does_not_clobber_rest_written_sidecar`

### `tests/test_r13_step6_vocabulary_closure.py` — 2 of 2

- `tests/test_r13_step6_vocabulary_closure.py::test_a_session_vocabulary_is_subset_of_served_b_names`
- `tests/test_r13_step6_vocabulary_closure.py::test_served_b_minus_a_is_display_only_with_empty_grains`

### `tests/test_resize_preserves_envelope.py` — 7 of 12

- `tests/test_resize_preserves_envelope.py::test_both_recorded_axes_and_the_new_limits_coexist`
- `tests/test_resize_preserves_envelope.py::test_failed_recreate_rebuild_restores_the_recorded_envelope`
- `tests/test_resize_preserves_envelope.py::test_no_recorded_envelope_passes_no_override[snapshot0]`
- `tests/test_resize_preserves_envelope.py::test_no_recorded_envelope_passes_no_override[snapshot1]`
- `tests/test_resize_preserves_envelope.py::test_no_recorded_envelope_passes_no_override[snapshot2]`
- `tests/test_resize_preserves_envelope.py::test_recorded_network_mode_is_restored_on_recreate`
- `tests/test_resize_preserves_envelope.py::test_recorded_workspace_mode_is_restored_on_recreate`

### `tests/test_resource_catalog_endpoint.py` — 3 of 3

- `tests/test_resource_catalog_endpoint.py::test_resource_catalog_git_entry_exact_fields`
- `tests/test_resource_catalog_endpoint.py::test_resource_catalog_nine_resources_with_exact_keys`
- `tests/test_resource_catalog_endpoint.py::test_resource_catalog_returns_bare_array`

### `tests/test_resource_catalog_grain_projection.py` — 1 of 1

- `tests/test_resource_catalog_grain_projection.py::test_served_grain_set_projection_from_canonical`

### `tests/test_restart_preserves_envelope.py` — 19 of 19

- `tests/test_restart_preserves_envelope.py::test_real_manager_recreate_proves_the_downgrade_without_the_envelope`
- `tests/test_restart_preserves_envelope.py::test_real_manager_recreate_rebuilds_with_both_recorded_axes`
- `tests/test_restart_preserves_envelope.py::test_real_manager_recreate_stamps_the_origin_created_by`
- `tests/test_restart_preserves_envelope.py::test_recreate_network_mode_only`
- `tests/test_restart_preserves_envelope.py::test_recreate_rebind_payload_carries_no_creator_field`
- `tests/test_restart_preserves_envelope.py::test_recreate_rebinds_record_to_the_rebuilt_container`
- `tests/test_restart_preserves_envelope.py::test_recreate_rebuilds_with_the_origin_created_by`
- `tests/test_restart_preserves_envelope.py::test_recreate_restores_both_recorded_axes`
- `tests/test_restart_preserves_envelope.py::test_recreate_with_the_container_already_gone_falls_back_to_the_operator[gone]`
- `tests/test_restart_preserves_envelope.py::test_recreate_with_the_container_already_gone_falls_back_to_the_operator[unreadable]`
- `tests/test_restart_preserves_envelope.py::test_recreate_without_an_origin_label_falls_back_to_the_operator`
- `tests/test_restart_preserves_envelope.py::test_recreate_without_rebuild_id_errors_and_keeps_old_handle`
- `tests/test_restart_preserves_envelope.py::test_recreate_without_recorded_envelope_passes_no_override`
- `tests/test_restart_preserves_envelope.py::test_recreate_workspace_mode_only`
- `tests/test_restart_preserves_envelope.py::test_restart_forwards_recorded_envelope`
- `tests/test_restart_preserves_envelope.py::test_restart_forwards_the_origin_created_by`
- `tests/test_restart_preserves_envelope.py::test_restart_only_stops_and_never_removes`
- `tests/test_restart_preserves_envelope.py::test_restart_without_rebuild_id_errors_and_keeps_old_handle`
- `tests/test_restart_preserves_envelope.py::test_restart_without_recorded_envelope_passes_no_override`

### `tests/test_safe_defaults_single_owner.py` — 1 of 7

- `tests/test_safe_defaults_single_owner.py::test_permission_defaults_endpoint_serves_safe_defaults`

### `tests/test_seeding_path_invariants.py` — 5 of 6

- `tests/test_seeding_path_invariants.py::test_rest_create_with_workspace_path_seeds_empty_sidecar`
- `tests/test_seeding_path_invariants.py::test_transport_roundtrip_ws_notice_rest_read_and_refresh_agree`
- `tests/test_seeding_path_invariants.py::test_ws_apply_config_workspace_switch_seeds_empty_sidecar`
- `tests/test_seeding_path_invariants.py::test_ws_apply_config_workspace_switch_sends_permission_reset_notice`
- `tests/test_seeding_path_invariants.py::test_ws_set_project_seeds_empty_sidecar`

### `tests/test_system_health_local_only.py` — 2 of 7

- `tests/test_system_health_local_only.py::test_payload_has_no_event_log_content`
- `tests/test_system_health_local_only.py::test_workspace_filter_still_applies`

### `tests/test_vault_status_actions.py` — 2 of 2

- `tests/test_vault_status_actions.py::test_vault_status_aborts_on_invalid_json`
- `tests/test_vault_status_actions.py::test_vault_status_warnings_with_actions_and_no_secrets`

### `tests/test_vault_status_endpoint.py` — 6 of 6

- `tests/test_vault_status_endpoint.py::test_vault_status_endpoint_abort_drifts`
- `tests/test_vault_status_endpoint.py::test_vault_status_endpoint_no_secrets`
- `tests/test_vault_status_endpoint.py::test_vault_status_endpoint_only_info_is_ok`
- `tests/test_vault_status_endpoint.py::test_vault_status_endpoint_returns_issues`
- `tests/test_vault_status_endpoint.py::test_vault_status_endpoint_unexpected_error`
- `tests/test_vault_status_endpoint.py::test_vault_status_endpoint_unknown_file_is_warning`

### `tests/test_workspace_container_status_hardening_verdict.py` — 4 of 4

- `tests/test_workspace_container_status_hardening_verdict.py::test_conformant_container_reports_conformant`
- `tests/test_workspace_container_status_hardening_verdict.py::test_unobtainable_container_reports_unverified`
- `tests/test_workspace_container_status_hardening_verdict.py::test_unreadable_attrs_reports_unverified`
- `tests/test_workspace_container_status_hardening_verdict.py::test_weak_cap_drop_reports_drifted`

### `tests/test_workspace_containers_view.py` — 25 of 28

- `tests/test_workspace_containers_view.py::test_action_remove_ephemeral`
- `tests/test_workspace_containers_view.py::test_action_remove_persistent_allowed`
- `tests/test_workspace_containers_view.py::test_action_remove_persistent_identity_is_attribution_only`
- `tests/test_workspace_containers_view.py::test_action_remove_persistent_reaped_by_manager`
- `tests/test_workspace_containers_view.py::test_action_remove_resource_refused`
- `tests/test_workspace_containers_view.py::test_action_remove_service_refused`
- `tests/test_workspace_containers_view.py::test_action_remove_unknown_class_refused`
- `tests/test_workspace_containers_view.py::test_action_restart_resource_recreates`
- `tests/test_workspace_containers_view.py::test_action_stop_ephemeral`
- `tests/test_workspace_containers_view.py::test_empty_workspace_yields_empty_container_list`
- `tests/test_workspace_containers_view.py::test_entries_pin_live_and_unrecorded_keys`
- `tests/test_workspace_containers_view.py::test_entry_carries_record_note_and_recordless_empty`
- `tests/test_workspace_containers_view.py::test_entry_live_envelope_derived_from_running_container`
- `tests/test_workspace_containers_view.py::test_get_entry_state_oom`
- `tests/test_workspace_containers_view.py::test_get_groups_and_entry_fields`
- `tests/test_workspace_containers_view.py::test_get_legacy_contract_and_store_failure`
- `tests/test_workspace_containers_view.py::test_listed_count_includes_live_resource_container`
- `tests/test_workspace_containers_view.py::test_live_container_with_record_shown_live`
- `tests/test_workspace_containers_view.py::test_live_container_without_record_appears`
- `tests/test_workspace_containers_view.py::test_patch_ephemeral_resources_not_refused`
- `tests/test_workspace_containers_view.py::test_patch_persists_intent_before_recreate`
- `tests/test_workspace_containers_view.py::test_patch_resource_resources_recreates`
- `tests/test_workspace_containers_view.py::test_record_without_live_container_marked_not_live`
- `tests/test_workspace_containers_view.py::test_recordless_live_container_carries_live_envelope`
- `tests/test_workspace_containers_view.py::test_unwired_record_permissions_and_drift_null`

### `tests/web_ui/backend/test_websocket_integration.py` — 4 of 4

- `tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_apply_config_api_key_not_configured`
- `tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_continue_session_without_key_emits_error`
- `tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_new_session_emits_five_events`
- `tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_new_session_resolves_workspace_id`

### `tests/web_ui/backend/test_ws_mock_provider.py` — 6 of 7

- `tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_api_key_stripped_from_config_dump`
- `tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_apply_config_permissions_payload_is_ignored`
- `tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_continue_session_mock_provider_not_used`
- `tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_model_override_ignored`
- `tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_new_session_emits_five_events`
- `tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_save_and_load_session_roundtrip_strips_grants`

## Notes / caveats

- No node was killed, skipped, errored or retried in either pass; no test was deselected.
- Both passes ran on the same batch composition, same interpreter and same `-q -rf -p no:randomly` invocation, so the only delta between the passes is the two trust-harness toggles.
- `test_first_run.py::test_full_first_run_lifecycle` fails only under flag OFF (starlette WebSocket path), confirming it also rides the harness trust seam.
