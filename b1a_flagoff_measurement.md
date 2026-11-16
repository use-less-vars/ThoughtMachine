# b-1a — flag-off measurement of `web_ui/backend/tests/` (MEASURE ONLY)

Worktree `.worktrees/c76-trust-harness`. No test, harness or shipped-code edit was kept.
conftest.py: pre-edit sha256 `5aeb59e8…` == post-restore sha256 `5aeb59e8…` (MATCH).
Interpreter: python3 3.12.15 / pytest 9.2.0 (`-p no:cacheprovider`, `PYTHONDONTWRITEBYTECODE=1`).

## 1. Flag-off mechanism used (and where it was read)

Source of the mechanism: `measurement.md` § "Stage 2 probe off" / § "Stage 4 restore"
(session-autouse fixture, two lines flipped `True`→`False`, restored by reverse edit).

`measurement.md` records the lines as `conftest.py:163-164` (sha `a8c194c0…`, 232-line file).
The current conftest.py is the 253-line version (sha `5aeb59e8…`), so the same two lines have
MOVED to **`conftest.py:170-171`** inside `_trust_the_in_process_asgi_harness`:

    169:    previous = auth_boundary.TRUST_TEST_HARNESS
    170:    set_suite_trust_value(True)                    -> (False)
    171:    auth_boundary.enable_test_harness_trust(True)  -> (False)
    172:    try:

Mechanism applied: exactly those two edits, nothing else (`sha256` after edit `93f2eef3…`).
Restore: reverse edit, `sha256` back to `5aeb59e8…` — MATCH.

No `-o` override, no monkeypatch, no marker, no env var was invented: `auth_boundary.py:229`
states `TRUST_TEST_HARNESS = False` "is never read from the environment" (line 228-229; doc line 101),
so there is no env-var off-switch to use.

The new function-scoped autouse `_harness_trust_for_marked_tests` (conftest.py:243-253, its own
`enable_test_harness_trust(True)` at :250) is a STRICT NO-OP for unmarked tests, and `grep` shows the
12 `@pytest.mark.trust_harness` applications live only under `tests/` — **zero** in
`web_ui/backend/tests/`. So flag-off is not partially re-armed anywhere inside the measured dir.
(`test_auth_boundary.py`'s local `_harness_trust(...)` context manager at :160 is self-contained and
still works under flag-off, which is why only 1 test in that file moves.)

## 2. Baseline (flag ON, pristine conftest)

`python3 -m pytest -q -p no:cacheprovider -rf --tb=line web_ui/backend/tests/`
→ **305 collected — 300 passed, 5 xfailed, 0 failed, 0 skipped/errors, exit 0, 10.96s**
Log: `logs/b1a_baseline_flagON_tee.log`.

## 3. Flag OFF

Same command → **305 collected — 76 failed, 224 passed, 5 xfailed, 0 skipped/errors, exit 1, 9.38s**
Rerun: 76 failed / 224 passed / 5 xfailed / 9.69s, failure set byte-identical (`diff` clean).
Logs: `logs/b1a_flagoff_flagOFF.log`, `logs/b1a_flagoff_flagOFF_rerun.log`.

## 4. Node ids that the flag-off mechanism measures (76)

Full list: `logs/b1a_flagoff_failures.txt`. Per file:
* addendum_routes (8): a_get_fresh_addendum_defaults, b_post_accepts_and_persists_entry,
  c_written_entry_reaches_composed_system_prompt, d_post_over_budget_refused,
  e_post_at_max_entries_refused, f_recovery_after_clear, g_unknown_session_404_and_foreign_origin_403,
  h_client_cannot_smuggle_source
* auth_boundary (1): test_a_freshly_imported_boundary_carries_the_suite_trust_value
* boundary_layer_ingress (7): apply_config_rejects_workspace_path_inside_vault,
  security_response_from_other_connection_is_refused, security_response_from_the_asking_connection_is_applied,
  security_response_with_unknown_request_id_is_default_denied, set_project_rejects_path_inside_vault,
  ws_handshake_accepts_absent_origin, ws_handshake_accepts_loopback_origin
* config_mode (11): TestGetMode[get_mode_returns_agent_after_clearing, get_mode_returns_agent_by_default,
  get_mode_returns_engineer_after_set, get_mode_with_empty_custom_prompt],
  TestSetMode[empty_mode_returns_400, invalid_mode_returns_400, legacy_full_maps_to_agent,
  mode_is_case_insensitive, set_agent_mode_clears_custom_prompt, set_agent_mode_when_no_custom_prompt,
  set_engineer_mode]
* config_reset_workspace_scope (3): bridge_of_another_workspace_is_not_updated,
  same_workspace_bridge_is_still_updated, sessionaless_cached_bridge_is_not_updated
* permission_routes (33): TestSessionPermissionsRoutes(20), TestSessionPermissionsNotice(3),
  TestWorkspacePermissionsRoutes(7), TestWorkspacePermissionsNotice(2),
  TestWorkspaceEffectivePermissionsRoutes(1, effective_unknown_session_returns_deny_all) — ids in
  `logs/b1a_flagoff_failures.txt`
* worker_stats_routes (6): TestContainersEndpointStatsFields[available_clamped_at_zero,
  full_cap_yields_zero_available, in_use_and_available, list_failure_503_preserved,
  missing_manager_404_preserved, no_containers]
* workspace_routes (7): TestPutPermissionsAllowHostResources(5), TestGetPermissionsAllowHostResources(1),
  TestPutPermissionsValidation(2)

Failure signatures: 69 = `401 {"error":"unauthorized"}` /
`assert 401 == 200|422|404|503` / `KeyError: 'containers_in_use'` (401 body lacks the key),
7 = `starlette.websockets.WebSocketDisconnect` at `starlette/testclient.py:143`
(boundary_layer_ingress ws_handshake_* 2 + others). Every one is the trust gate refusing a
`host: testclient` peer, i.e. the peer exemption that only exists while the flag is True.

## 5. Newly measured = 76

## 6. Reverse direction = 0

Baseline failure set is empty (300 passed / 5 xfailed), so no test fails with the flag ON and passes
with it OFF. The 5 xfailed are identical in both runs.

## 7. Per-file split

| file | flag ON | flag OFF | newly failing |
|---|---|---|---|
| addendum_routes | 8 passed | 0 passed / 8 failed | 8 |
| boundary_layer_ingress | 25 passed | 18 / 7 | 7 |
| config_mode | 11 passed | 0 / 11 | 11 |
| config_reset_workspace_scope | 3 passed | 0 / 3 | 3 |
| permission_routes | 33 passed | 0 / 33 | 33 |
| worker_stats_routes | ? (baseline run killed by container, see §8) | 6 failed | 6 |
| workspace_routes | 7 passed | 0 / 7 | 7 |
| auth_boundary | 66 tests, 0 failed | 65 passed / 1 failed | 1 |
| other 12 files | all passed | all passed | 0 |

## 8. Environment caveat (why baseline was only obtained once as a whole-dir run)

The container cgroup is `memory.max = 536870912` (512 MB) regardless of the requested `mem_limit`,
and the host was swapping with ~360 MB free. 4 of 5 later whole-dir BASELINE attempts (and 8 of 20
per-file baseline runs) were killed by SIGTERM mid-run (`rc=143`, log tail `Terminated`, no summary
line) — the memory-heavy TestClient files (addendum_live_chain, addendum_routes,
addendum_write_through, auth_boundary, origin_check_request, permission_routes, security_coercion,
worker_stats_routes) are the ones that die. No flag-OFF run was ever killed: all four completed with a
real summary line and two produced byte-identical failure sets, so the 76 is not kill-contamination.
The BASELINE 300 passed / 5 xfailed figure rests on ONE complete whole-dir run plus 12/20 per-file
runs (all `0 failed`); see `logs/b1a_baseline_perfile_summary.log`.

## Logs (all under `.worktrees/c76-trust-harness/logs/`)

b1a_baseline_flagON_tee.log · b1a_flagoff_flagOFF.log · b1a_flagoff_flagOFF_rerun.log ·
b1a_flagoff_failures.txt · b1a_flagoff_failures_rerun.txt · b1a_flagoff_flagOFF_attempt1_TRUNCATED.log ·
b1a_collected_nodeids.txt · b1a_baseline_perfile_summary.log · b1a_perfile_*.log ·
b1a_baseline_attempt1..3.log · b1a_baseline_verbose_probe.log
