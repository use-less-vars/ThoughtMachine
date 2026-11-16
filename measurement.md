# C76 measurement — suite-wide trust harness (MEASURE ONLY)

Worktree: `.worktrees/c76-trust-harness` (branch c76-trust-harness, base 544bd407).
No fixes applied. No commits. Restore done by reverse edit (no git available in container).

## Stage 1 baseline

### Restore contract (captured BEFORE any edit)

`sha256sum conftest.py` (Stage 1, pre-edit):
```
a8c194c0b2c72e0aff19333f7f8d9a2c07528928f126004b30dbb7f72e84a606  conftest.py
```

Verbatim original text of the lines that will be edited (session-autouse fixture body):
```
    of "a test session was running" rather than of "this process".
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    set_suite_trust_value(True)
    auth_boundary.enable_test_harness_trust(True)
    try:
        yield
    finally:
        auth_boundary.enable_test_harness_trust(previous)
        set_suite_trust_value(previous)
```

Line-numbered:
```
163:    set_suite_trust_value(True)
164:    auth_boundary.enable_test_harness_trust(True)
```

Interpreter: `.venv/bin/python` is present but BROKEN for pytest in-container (`No module named pytest`); used `python3` (Python 3.12.15, pytest 9.2.0) as the briefing allows.

Command: `python3 -m pytest -q -p no:cacheprovider web_ui/backend/tests/test_auth_boundary.py`

NOTE: the brief said `tests/test_auth_boundary.py`; the file actually lives at `web_ui/backend/tests/test_auth_boundary.py` (confirmed by find). That is the path used everywhere below.

Output (warning-summary block elided; every other line verbatim):
```
```

Result: **66 passed**, exit 0, 7.18s.

> Correction: the Stage 1 raw capture was written to `/tmp` inside an ephemeral container and did not survive to the append step, so the code block above came out empty. Stage 1 result (verbatim summary line): `66 passed, 26 warnings in 7.18s`, exit 0.

## Stage 2 probe off

Edit applied to `conftest.py` (session-autouse fixture, lines 163-164): `set_suite_trust_value(True)` -> `(False)` and `auth_boundary.enable_test_harness_trust(True)` -> `(False)`. Nothing else touched. NOT reverted yet at this stage.
```
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    set_suite_trust_value(False)
    auth_boundary.enable_test_harness_trust(False)
    try:
```

Command: `python3 -m pytest -q -p no:cacheprovider web_ui/backend/tests/test_auth_boundary.py`

Summary line:
```
1 failed, 65 passed, 26 warnings in 7.94s
```

Failures:
```
FAILED web_ui/backend/tests/test_auth_boundary.py::test_a_freshly_imported_boundary_carries_the_suite_trust_value
```

Failure detail:
```
________ test_a_freshly_imported_boundary_carries_the_suite_trust_value ________

    def test_a_freshly_imported_boundary_carries_the_suite_trust_value():
        """The conftest import hook re-applies the allowance to the re-imported module.
    
        The trust flag lives *inside* ``web_ui.backend.auth_boundary``, so the
        suite's own purge throws it away: the re-imported module runs its module
        body and gets the shipped default ``False``.  Without the hook in the root
        ``conftest.py`` the fresh app below refuses the harness peer and this test
        fails with a 401 -- which is the whole point: a guard that silently stops
        being applied after a re-import is worse than no guard at all.
        """
        with _a_fresh_backend_from_the_harness() as (fresh_app, fresh_boundary):
            assert fresh_boundary is not auth_boundary, (
                "the purge did not produce a new module object -- this test would "
                "be pinning the module the conftest already fixed up"
            )
>           assert fresh_boundary.TRUST_TEST_HARNESS is True
E           AssertionError: assert False is True
```

### Classification (per failing node id)

`A — <reason>` / `B — <reason>`:

```
B — web_ui/backend/tests/test_auth_boundary.py::test_a_freshly_imported_boundary_carries_the_suite_trust_value
```
- It asserts `fresh_boundary.TRUST_TEST_HARNESS is True` and that the harness peer gets `200` from a freshly re-imported app (body at `web_ui/backend/tests/test_auth_boundary.py:780-797`). It is a test OF the conftest import hook re-arming the allowance, i.e. of the harness itself; it pins no shipped-gate refusal. It legitimately needs the harness OPEN, so spawn 2 gives it an explicit opt-in (`_harness_trust(True)` around the block, or a marker), exactly as its sibling `test_a_freshly_imported_boundary_honours_the_suite_value_when_it_is_off` (`:800-812`) wraps itself in `_harness_trust(False)`. It does not belong in the A bucket: nothing here is a closed-gate contract.

Per-test split: **Stage 2: 1 failure (A: 0, B: 1)** — 65 passed of 66.

## Stage 3 broader filter

Probe still OFF (conftest unchanged from Stage 2).

Command: `python3 -m pytest -q -p no:cacheprovider --tb=line -rf tests/ -k "auth or websocket or harness or trust or permission" --ignore=tests/test_auth_boundary.py`

Wall time: 67s (under the 380s timeout wrapper), exit 1.

Summary line:
```
42 failed, 1046 passed, 5 skipped, 6329 deselected, 30 warnings in 62.27s (0:01:02)
```

Note: `--ignore=tests/test_auth_boundary.py` is inert because that file lives at `web_ui/backend/tests/test_auth_boundary.py`, outside the `tests/` root given to pytest. It is therefore not in this run either way.

One-line failure signatures (verbatim, `--tb=line`):
```
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:333: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/harness.py:373: AssertionError: workspace summary failed: 401 {"error": "unauthorized"}
tests/integration/test_ui_permission_persistence_flow.py:70: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/integration/test_ui_permission_persistence_flow.py:70: AssertionError: create workspace failed: 401 {"error": "unauthorized"}
tests/test_container_record_user_action_routes.py:399: AssertionError: {"error": "unauthorized"}
tests/test_permission_refresh_ws_clobber_guard.py:108: AssertionError: {"error": "unauthorized"}
tests/test_permission_sidecar_clobber_guard.py:82: AssertionError: {"error": "unauthorized"}
tests/test_permission_sidecar_clobber_guard.py:202: AssertionError: {"error": "unauthorized"}
tests/test_safe_defaults_single_owner.py:146: assert 401 == 200
tests/test_seeding_path_invariants.py:302: AssertionError: {"error": "unauthorized"}
tests/test_workspace_containers_view.py:389: assert 401 == 200
FAILED tests/integration/test_apply_config_coverage.py::test_case1_permissions_change[default]
FAILED tests/integration/test_apply_config_coverage.py::test_case1_permissions_change[custom]
FAILED tests/integration/test_apply_config_coverage.py::test_new_session_handshake_default_permissions[default]
FAILED tests/integration/test_apply_config_coverage.py::test_new_session_handshake_default_permissions[custom]
FAILED tests/integration/test_apply_config_coverage.py::test_case7_permission_only_apply_keeps_session[default]
FAILED tests/integration/test_apply_config_coverage.py::test_case7_permission_only_apply_keeps_session[custom]
FAILED tests/integration/test_bind_workspace_authority.py::test_handshake_with_project_binds_connection
FAILED tests/integration/test_bind_workspace_authority.py::test_handshake_without_project_arrives_unbound
FAILED tests/integration/test_bind_workspace_authority.py::test_register_by_root_binds_connection
FAILED tests/integration/test_bind_workspace_authority.py::test_cross_workspace_load_refused
FAILED tests/integration/test_bind_workspace_authority.py::test_site1_refused_bind_does_not_leak_path
FAILED tests/integration/test_config_control_matrix.py::test_persistence_workspace_permission_ceiling_survives_restart
FAILED tests/integration/test_config_control_matrix.py::test_persistence_session_permission_grants_survive_restart_capped_by_ceiling
FAILED tests/integration/test_config_control_matrix.py::test_workspace_ceiling_caps_session_effective_permission
FAILED tests/integration/test_layer_precedence_and_ui_truth.py::test_config_changed_event_carries_effective_permission_from_canonical_store
FAILED tests/integration/test_layer_precedence_and_ui_truth.py::test_restart_reapplies_workspace_permission_ceiling_on_session_load
FAILED tests/integration/test_layer_precedence_and_ui_truth.py::test_rest_effective_permissions_serve_defaults_then_ceiled_grant
FAILED tests/integration/test_layer_precedence_and_ui_truth.py::test_workspace_summary_reflects_ui_permission_state
FAILED tests/integration/test_permission_reset_and_ceiling.py::test_effective_permissions_without_session_reflects_workspace_ceiling
FAILED tests/integration/test_permission_reset_and_ceiling.py::test_effective_permissions_caps_session_grant_by_ceiling
FAILED tests/integration/test_permission_reset_and_ceiling.py::test_effective_permissions_capped_after_restart
FAILED tests/integration/test_permission_reset_and_ceiling.py::test_same_process_ws_reconnect_live_state_is_ceiled
FAILED tests/integration/test_permission_reset_and_ceiling.py::test_workspace_permission_persists_across_full_process_restart
FAILED tests/integration/test_ui_permission_persistence_flow.py::test_ui_flow_apply_restart_reload_persists
FAILED tests/integration/test_ui_permission_persistence_flow.py::test_session_grants_capped_after_restart
FAILED tests/test_container_record_user_action_routes.py::test_permission_denied_403_audited
FAILED tests/test_permission_refresh_ws_clobber_guard.py::test_ws_load_session_refresh_does_not_clobber_rest_written_sidecar
FAILED tests/test_permission_sidecar_clobber_guard.py::test_config_save_does_not_clobber_rest_written_sidecar
FAILED tests/test_permission_sidecar_clobber_guard.py::test_stale_nonempty_p2_does_not_clobber_rest_written_sidecar
FAILED tests/test_safe_defaults_single_owner.py::test_permission_defaults_endpoint_serves_safe_defaults
FAILED tests/test_seeding_path_invariants.py::test_ws_apply_config_workspace_switch_sends_permission_reset_notice
FAILED tests/test_workspace_containers_view.py::test_unwired_record_permissions_and_drift_null
FAILED tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_new_session_emits_five_events
FAILED tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_continue_session_without_key_emits_error
FAILED tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_new_session_resolves_workspace_id
FAILED tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_apply_config_api_key_not_configured
FAILED tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_new_session_emits_five_events
FAILED tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_continue_session_mock_provider_not_used
FAILED tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_apply_config_permissions_payload_is_ignored
FAILED tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_model_override_ignored
FAILED tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_api_key_stripped_from_config_dump
FAILED tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_save_and_load_session_roundtrip_strips_grants
```

Complete `--tb=line` signatures for the 42 failures (no path filter; `=+`/`+ where` continuations kept):
```
=========================== short test summary info ============================
FAILED tests/integration/test_apply_config_coverage.py::test_case1_permissions_change[default]
FAILED tests/integration/test_apply_config_coverage.py::test_case1_permissions_change[custom]
FAILED tests/integration/test_apply_config_coverage.py::test_new_session_handshake_default_permissions[default]
FAILED tests/integration/test_apply_config_coverage.py::test_new_session_handshake_default_permissions[custom]
     16 /usr/local/lib/python3.12/site-packages/starlette/testclient.py:143: starlette.websockets.WebSocketDisconnect
```

### Stage 3 classification (all 42 failures)

Evidence: every one of the 42 failures is the trust gate refusing a `TestClient` peer whose host is `testclient` (that peer name is only exempt while `TRUST_TEST_HARNESS` is True). Signatures group into exactly two kinds:

- **26 failures = HTTP `401 {"error": "unauthorized"}`** — 16 at `tests/integration/harness.py:333` (`create workspace failed: 401`), 1 at `tests/integration/harness.py:373` (`workspace summary failed: 401`), 2 at `tests/integration/test_ui_permission_persistence_flow.py:70` (`create workspace failed: 401`), and single ones at `tests/test_container_record_user_action_routes.py:399`, `tests/test_permission_refresh_ws_clobber_guard.py:108`, `tests/test_permission_sidecar_clobber_guard.py:82` and `:202`, `tests/test_seeding_path_invariants.py:302` (`{"error": "unauthorized"}`), plus `tests/test_safe_defaults_single_owner.py:146` and `tests/test_workspace_containers_view.py:389` (`assert 401 == 200`).
- **16 failures = `starlette.websockets.WebSocketDisconnect`** at `site-packages/starlette/testclient.py:143` — the websocket handshake is closed by the boundary before the WS code under test runs.

Rule used for A vs B (stated so it can be argued with): **A** = the test own subject is an authorization / permission-denial decision on the shipped boundary, so it must be re-pointed at the CLOSED gate (it needs a real credential, not an open gate). **B** = the 401/disconnect is incidental setup (create workspace, REST read, WS handshake) and the test subject is something else (permission-ceiling arithmetic, config persistence, WS event ordering, provider config, view shape), so it needs an explicit opt-in.

```
A — tests/integration/test_bind_workspace_authority.py::test_handshake_with_project_binds_connection  (:156) docstring: "?project=A binds the connection to A, so a load that would adopt B is refused" — an authorization refusal is the subject.
A — tests/integration/test_bind_workspace_authority.py::test_handshake_without_project_arrives_unbound  (:201) subject is when the connection is NOT bound; asserts the unbound state that decides later refusals.
A — tests/integration/test_bind_workspace_authority.py::test_register_by_root_binds_connection  (:238) subject is which workspace a connection gains authority over.
A — tests/integration/test_bind_workspace_authority.py::test_cross_workspace_load_refused  (:362) docstring: "An explicit load into another workspace cannot re-scope a handshake-bound socket" — pure refusal contract.
A — tests/integration/test_bind_workspace_authority.py::test_site1_refused_bind_does_not_leak_path  (:405) docstring names the refusal site and asserts the refusal leaks no path.
A — tests/test_container_record_user_action_routes.py::test_permission_denied_403_audited  (:391) asserts 403 + {"code": "permission_denied"} + the denied audit record; currently gets 401 because the auth boundary answers first, so it cannot distinguish "no credential" from "no permission" while the gate is open.
```

```
B — tests/integration/test_apply_config_coverage.py::test_case1_permissions_change[default]  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_apply_config_coverage.py::test_case1_permissions_change[custom]  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_apply_config_coverage.py::test_new_session_handshake_default_permissions[default]  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_apply_config_coverage.py::test_new_session_handshake_default_permissions[custom]  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_apply_config_coverage.py::test_case7_permission_only_apply_keeps_session[default]  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_apply_config_coverage.py::test_case7_permission_only_apply_keeps_session[custom]  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_config_control_matrix.py::test_persistence_workspace_permission_ceiling_survives_restart  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_config_control_matrix.py::test_persistence_session_permission_grants_survive_restart_capped_by_ceiling  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_config_control_matrix.py::test_workspace_ceiling_caps_session_effective_permission  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_layer_precedence_and_ui_truth.py::test_config_changed_event_carries_effective_permission_from_canonical_store  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_layer_precedence_and_ui_truth.py::test_restart_reapplies_workspace_permission_ceiling_on_session_load  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_layer_precedence_and_ui_truth.py::test_rest_effective_permissions_serve_defaults_then_ceiled_grant  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_layer_precedence_and_ui_truth.py::test_workspace_summary_reflects_ui_permission_state  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_permission_reset_and_ceiling.py::test_effective_permissions_without_session_reflects_workspace_ceiling  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_permission_reset_and_ceiling.py::test_effective_permissions_caps_session_grant_by_ceiling  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_permission_reset_and_ceiling.py::test_effective_permissions_capped_after_restart  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_permission_reset_and_ceiling.py::test_same_process_ws_reconnect_live_state_is_ceiled  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_permission_reset_and_ceiling.py::test_workspace_permission_persists_across_full_process_restart  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_ui_permission_persistence_flow.py::test_ui_flow_apply_restart_reload_persists  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/integration/test_ui_permission_persistence_flow.py::test_session_grants_capped_after_restart  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/test_permission_refresh_ws_clobber_guard.py::test_ws_load_session_refresh_does_not_clobber_rest_written_sidecar  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/test_permission_sidecar_clobber_guard.py::test_config_save_does_not_clobber_rest_written_sidecar  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/test_permission_sidecar_clobber_guard.py::test_stale_nonempty_p2_does_not_clobber_rest_written_sidecar  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/test_safe_defaults_single_owner.py::test_permission_defaults_endpoint_serves_safe_defaults  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/test_seeding_path_invariants.py::test_ws_apply_config_workspace_switch_sends_permission_reset_notice  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/test_workspace_containers_view.py::test_unwired_record_permissions_and_drift_null  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_new_session_emits_five_events  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_continue_session_without_key_emits_error  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_new_session_resolves_workspace_id  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_websocket_integration.py::TestWebSocketLifecycle::test_apply_config_api_key_not_configured  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_new_session_emits_five_events  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_continue_session_mock_provider_not_used  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_apply_config_permissions_payload_is_ignored  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_model_override_ignored  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_api_key_stripped_from_config_dump  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
B — tests/web_ui/backend/test_ws_mock_provider.py::TestWebSocketWithMockProvider::test_save_and_load_session_roundtrip_strips_grants  -> 401/disconnect on harness setup (create workspace / REST read / WS handshake); subject is not an ingress-gate refusal.
```

Per-test split: **Stage 3: 42 failures (A: 6, B: 36)** — 1046 passed, 5 skipped, 6329 deselected.

## Stage 4 restore

Restored by REVERSE EDIT (no git in container): lines 163-164 changed from `False` back to `True`. No backup/copy file was created in the tree; the pre-edit snapshot was compared in `/tmp` (ephemeral, does not persist).

Restored text (`conftest.py:160-171`):
```
    of "a test session was running" rather than of "this process".
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    set_suite_trust_value(True)
    auth_boundary.enable_test_harness_trust(True)
    try:
        yield
    finally:
        auth_boundary.enable_test_harness_trust(previous)
        set_suite_trust_value(previous)
```

Proof:
```
Stage 1 (pre-edit) sha256: a8c194c0b2c72e0aff19333f7f8d9a2c07528928f126004b30dbb7f72e84a606
Stage 4 (post-restore) sha256: a8c194c0b2c72e0aff19333f7f8d9a2c07528928f126004b30dbb7f72e84a606
Result: MATCH
```

Stray-file check in the worktree: no `*.bak`, no `*.orig`, no `.pytest_cache` (every run used `-p no:cacheprovider`). The only file this pass added to the worktree is `measurement.md` itself.

## Summary

| stage | what | result |
|---|---|---|
| 1 | baseline, harness ON, `web_ui/backend/tests/test_auth_boundary.py` | 66 passed, exit 0 |
| 2 | harness defaulted OFF (same file) | 1 failed, 65 passed — A: 0, B: 1 |
| 3 | harness OFF, `tests/ -k "auth or websocket or harness or trust or permission"` | 42 failed, 1046 passed, 5 skipped, 6329 deselected — A: 6, B: 36 |
| 4 | restore | sha256 MATCH (see above) |

**Total across stages 2+3: 43 failures (A: 6, B: 37).** No test was fixed. No commit, no push, no branch.
