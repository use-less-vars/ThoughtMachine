# TestClient inventory — C76 Spawn 2a (STATIC ONLY)

Scope: every `TestClient(` construction in
`tests/` and `web_ui/backend/tests/` of worktree `c76-trust-harness`.
Method: AST walk of both trees (no tests executed, no test re-runs).
Cross-referenced against spawn-1's `measurement.md`.

## 0. Headline numbers

| metric | value |
|---|---|
| textual `TestClient(` occurrences | 172 |
| **real constructions (AST)** | **170** in **68 files** |
| textual-only (docstring prose) | 2 — `tests/integration/test_health_localhost_binding.py:8`, `tests/integration/test_server_health.py:11` |
| in `tests/` | 137 sites |
| in `web_ui/backend/tests/` | 33 sites |
| **A** (claims a gate decision) | **31** |
| **B** (guarded traffic, no gate claim) | **111** |
| **C** (gate cannot decide) | **28** — 7 exempt-path, 2 explicit-peer, **19 gate-not-mounted** |
| sites provably executed by spawn-1 Stage-3 | 17 |
| sites the Stage-3 measurement could not have covered (**D**) | **153** |

The single most important static finding vs. spawn-1's assumptions: **19 of the 170
sites wrap a locally-built, router-only `FastAPI` app with no `AuthBoundaryMiddleware`
in the file at all.** For those the gate does not merely pass or exempt — it never
runs. The trust-harness flag is inert there, and no `401`/`403` can ever be observed.

## 1. Classification rules (as executed)

Ordered, first match wins:

1. **C-no-gate** — the object passed to `TestClient(...)` is a `FastAPI()` built in
   the same module and the module never registers `AuthBoundaryMiddleware`
   (`add_middleware` absent) and never imports `web_ui.backend.server.app`.
   The middleware stack is mounted at `TestClient.__enter__`/first request; no
   boundary ⇒ no decision.
2. **C-exempt** — every request made through the client targets an `EXEMPT_PATHS`
   entry (`/`, `/health`, `/api/health`, `/api/health/containers`,
   `/api/auth/session`) or `EXEMPT_PREFIXES` (`/assets/`). The gate runs but is
   bypassed by path, so the peer/trust branch is never reached.
3. **A** — the test asserts a *gate decision*: a refusal (401/403/`WebSocketDisconnect`
   code 1008/`denied`/`refused`/`rejected`) or a positive claim about the
   peer `testclient` being *allowed because* it is the harness client.
4. **C-peer** — an explicit `client=(host, port)` peer is supplied **and** the test
   makes a positive (allowed) claim, so `TRUST_TEST_HARNESS` can never fire on that
   traffic.
5. **B** — default `testclient` peer, positive behaviour on a guarded route on a
   gate-bearing app.

Corollary used throughout: a *shared* fixture/helper/module-level client inherits
the class of its consumers; if consumers disagree, the site is classed by its
dominant consumer and any A rider is named explicitly (only 2 such sites).

## 2. Totals per class

| class | n | where |
|---|---|---|
| A | 31 | `web_ui/backend/tests/` 22 (`test_auth_boundary` 12, `test_boundary_layer_ingress` 7, `test_auth_session_route` 2, `test_local_gate_shared` 1), `tests/` 9 (`test_origin_guard_enumeration` 3, `test_system_health_local_only` 3, `test_api_vault_repair` 1, `test_container_user_and_git_ownership` 1, `test_addendum_routes` 1) |
| B | 111 | all gate-bearing files not listed in A or C |
| C-exempt | 7 | `test_health_localhost_binding:225`, `test_server_health:96,125,469,489`, `test_health_endpoint_robustness:48`, `test_structured_health:49` |
| C-peer | 2 | `test_system_health_local_only:200`, `test_local_gate_shared:165` |
| C-no-gate | 19 | see §4 |

Spawn-2a's pre-registered estimate was A≈14 / B≈140 / C≈16; the recount moves
mass from B to both A (auth-boundary and ingress-enumeration rows are all A) and C
(the gate-not-mounted family). 31 + 111 + 28 = 170 ✓.

## 3. Per-file table (all 68 files)

`srv.app` = file imports `web_ui.backend.server.app`; `local` = builds `FastAPI()`;
`mw` = registers `AuthBoundaryMiddleware` itself.

| file | n | app source | A | B | C | class note |
|---|---|---|---|---|---|---|
| tests/integration/harness.py | 1 | server app (param) | | 1 | | only indirect site: `make_client(app)` :320 |
| tests/integration/test_apply_config_coverage.py | 18 | server app | | 18 | | |
| tests/integration/test_concurrent_sessions.py | 6 | server app | | 6 | | |
| tests/integration/test_health_localhost_binding.py | 1 | server app | | | 1 | `/api/health` exempt |
| tests/integration/test_origin_guard_enumeration.py | 3 | srv.app | 3 | | | explicit `client=` non-loopback → refusals |
| tests/integration/test_server_health.py | 15 | server app | | 11 | 4 | `/health`, `/api/health/containers` exempt |
| tests/integration/test_session_creation_contract.py | 3 | server app | | 3 | | |
| tests/integration/test_session_loaded_contract.py | 5 | server app | | 5 | | |
| tests/integration/test_state_transitions.py | 5 | server app | | 5 | | |
| tests/integration/test_ws_event_contracts.py | 9 | server app | | 9 | | |
| tests/security/test_git_execution_mode_field.py | 1 | srv.app | | 1 | | |
| tests/test_api_vault_repair.py | 2 | local+mw | 1 | | 1 | :156 gated refusal (A); :138 helper app, no mw (C, medium) |
| tests/test_container_logs_route.py | 1 | srv.app | | 1 | | |
| tests/test_container_machine_smoke.py | 1 | local | | | 1 | router-only app |
| tests/test_container_record_routes.py | 1 | local | | | 1 | router-only app |
| tests/test_container_record_user_action_routes.py | 1 | srv.app | | 1 | | shared client; A rider `test_permission_denied_403_audited` |
| tests/test_container_resources_resize_route.py | 1 | srv.app | | 1 | | |
| tests/test_container_user_and_git_ownership.py | 1 | srv.app | 1 | | | foreign-owner refusal |
| tests/test_created_by_preserved_on_recreate.py | 1 | srv.app | | 1 | | |
| tests/test_credentials_crud.py | 1 | local(helper) | | | 1 | router-only app, no mw (medium) |
| tests/test_first_run.py | 1 | server app | | 1 | | |
| tests/test_global_summary.py | 1 | local(helper) | | | 1 | router-only app, no mw (medium) |
| tests/test_health_endpoint.py | 1 | local | | | 1 | `/api/system/health` guarded BUT app has no mw |
| tests/test_health_endpoint_robustness.py | 1 | srv.app | | | 1 | `/api/health[/containers]` exempt |
| tests/test_host_api_hardening.py | 1 | server app | | 1 | | route-level vault-path 403s, not the ingress gate |
| tests/test_onboarding.py | 1 | server app | | 1 | | |
| tests/test_open_sessions_ws_payload.py | 2 | server app | | 2 | | |
| tests/test_permission_refresh_ws_clobber_guard.py | 1 | srv.app | | 1 | | one of the 401 witnesses (line 108) |
| tests/test_permission_sidecar_clobber_guard.py | 2 | srv.app | | 2 | | 401 witnesses (:82, :202) |
| tests/test_providers_crud.py | 1 | local(helper) | | | 1 | router-only app, no mw (medium) |
| tests/test_r13_step6_vocabulary_closure.py | 1 | srv.app | | 1 | | |
| tests/test_resize_preserves_envelope.py | 1 | srv.app | | 1 | | |
| tests/test_resource_catalog_endpoint.py | 1 | srv.app | | 1 | | |
| tests/test_resource_catalog_grain_projection.py | 1 | srv.app | | 1 | | |
| tests/test_restart_preserves_envelope.py | 1 | srv.app | | 1 | | |
| tests/test_safe_defaults_single_owner.py | 1 | srv.app | | 1 | | 401 witness (:146 `assert 401 == 200`) |
| tests/test_seeding_path_invariants.py | 6 | server app | | 6 | | 401 witness (:302) |
| tests/test_server_logger_registration.py | 1 | srv.app | | 1 | | |
| tests/test_structured_health.py | 1 | srv.app | | | 1 | `/api/health` exempt |
| tests/test_system_health_local_only.py | 7 | local+mw | 3 | 3 | 1 | :151/:165/:187 refusals (A); :200 loopback positive w/ explicit peer (C-peer) |
| tests/test_vault_repair_surface_errors.py | 1 | local(helper) | | | 1 | router-only app, no mw (medium) |
| tests/test_vault_status_actions.py | 1 | srv.app | | 1 | | |
| tests/test_vault_status_endpoint.py | 1 | srv.app | | 1 | | |
| tests/test_worker_blueprint_routes.py | 1 | local | | | 1 | router-only app |
| tests/test_worker_lifecycle_routes.py | 1 | local | | | 1 | router-only app |
| tests/test_workspace_config_root_persisted.py | 1 | local | | | 1 | router-only app |
| tests/test_workspace_container_status_hardening_verdict.py | 1 | srv.app | | 1 | | |
| tests/test_workspace_containers_view.py | 1 | srv.app | | 1 | | 401 witness (:389) |
| tests/test_workspace_permission_vocab_unification.py | 1 | local | | | 1 | router-only app |
| tests/test_workspace_permissions.py | 1 | local | | | 1 | router-only app |
| tests/test_workspace_purpose.py | 1 | local | | | 1 | router-only app |
| tests/test_workspace_routes_runtime_fallback.py | 1 | local | | | 1 | router-only app |
| tests/tools/test_worker_spawn_handle_registration.py | 1 | local | | | 1 | router-only app |
| tests/web_ui/backend/test_websocket_integration.py | 4 | server app | | 4 | | |
| tests/web_ui/backend/test_ws_mock_provider.py | 7 | server app | | 7 | | |
| tests/web_ui/test_templates_endpoint.py | 1 | local | | | 1 | router-only app |
| tests/web_ui/test_worker_crud.py | 1 | local | | | 1 | router-only app |
| tests/workspace/test_api.py | 1 | local | | | 1 | router-only app |
| web_ui/backend/tests/test_addendum_routes.py | 2 | srv.app | 1 | 1 | | :42 explicit loopback peer → origin refusals (A) |
| web_ui/backend/tests/test_auth_boundary.py | 12 | local+mw | 12 | | | the reference gate suite |
| web_ui/backend/tests/test_auth_session_route.py | 2 | srv.app | 2 | | | explicit peers |
| web_ui/backend/tests/test_boundary_layer_ingress.py | 10 | srv.app | 7 | 3 | | ws + origin refusals; :168/:177/:332 positive |
| web_ui/backend/tests/test_config_mode.py | 1 | srv.app | | 1 | | |
| web_ui/backend/tests/test_config_reset_workspace_scope.py | 1 | srv.app | | 1 | | |
| web_ui/backend/tests/test_local_gate_shared.py | 2 | srv.app | 1 | | 1 | :170 remote refusal (A); :165 positive w/ explicit peer (C-peer) |
| web_ui/backend/tests/test_permission_routes.py | 1 | srv.app | | 1 | | |
| web_ui/backend/tests/test_worker_stats_routes.py | 1 | srv.app | | 1 | | |
| web_ui/backend/tests/test_workspace_routes.py | 1 | srv.app | | 1 | | |

## 4. The C-no-gate family (19 sites, the actionable finding)

Files whose client wraps a `FastAPI()` built in-module with **no**
`AuthBoundaryMiddleware` and no `server.app` import:

`test_container_machine_smoke:441`, `test_container_record_routes:36`,
`test_credentials_crud:34`*, `test_global_summary:36`*, `test_health_endpoint:32`,
`test_providers_crud:34`*, `test_vault_repair_surface_errors:158`*,
`test_worker_blueprint_routes:79`, `test_worker_lifecycle_routes:79`,
`test_workspace_config_root_persisted:44`, `test_workspace_permission_vocab_unification:269`,
`test_workspace_permissions:98`, `test_workspace_purpose:59`,
`test_workspace_routes_runtime_fallback:214`,
`tests/tools/test_worker_spawn_handle_registration:376`,
`tests/web_ui/test_templates_endpoint:37`, `tests/web_ui/test_worker_crud:40`,
`tests/workspace/test_api:51`, `test_api_vault_repair:138`*.

`*` = the client wraps a locally *built* app returned by a helper function, not a
module-level `FastAPI()`. 14 of the 19 are certain (the `FastAPI()` is at module
scope and `add_middleware` is absent from the file); the 5 starred ones are
**medium confidence** — the helper was not read line-by-line. If the goal is an
exact A/B/C partition, read those five helper functions.

Note `test_health_endpoint:32` is the one case where the *route* is guarded
(`/api/system/health`, deliberately not exempt) but the *middleware* is absent from
the app under test — so the route-level assertion can pass while the ingress gate
was never exercised.

## 5. Cross-reference against spawn-1 `measurement.md`

Spawn-1 Stage 3 (`tests/ -k "auth or websocket or harness or trust or permission"`):
42 failed / 1046 passed / 5 skipped / 6329 deselected → 6 A + 36 B node ids.

* **E (failing node ids with no `TestClient` construction behind them) = 0.**
  23 of the 42 have an in-file construction; the other 19 are explained by the
  single shared site `tests/integration/harness.py:320 make_client(app)`
  (`test_bind_workspace_authority` 5, `test_config_control_matrix` 3,
  `test_layer_precedence_and_ui_truth` 4, `test_permission_reset_and_ceiling` 5,
  `test_ui_permission_persistence_flow` 2). Nothing in the failure set is
  client-less.
* **M (overlap) = 42/42.**
* **Construction-site class ≠ observed-failure class, in both directions.** The 5
  `test_bind_workspace_authority` failures are A-behaving (they hit the gate) yet
  their only construction is the shared, default-peer `harness.py:320` site, which
  classifies B. Conversely `test_container_record_user_action_routes:40` is a B
  shared site whose single A rider is the failing test. This is why a per-site
  table alone can never reproduce the failure set — the fixture/helper fan-out is
  the missing dimension.
* **D (sites the measurement could not cover) = 153.** Stage 3 ran `tests/` only
  ⇒ neither 33 sites in `web_ui/backend/tests/` nor the 137 `tests/` sites outside
  the `-k` selection were exercised. Static reconstruction of the `-k` selection
  (match on module/class/function names) finds 17 `tests/` sites inside it and 16
  `web_ui/backend/tests/` sites that *would* have matched but were never collected:
  170 − 17 = 153.
  *Correction to the number carried in the task brief:* the earlier "D = 120"
  came from textual grep counts (172/139) minus a 40-row overlap and mixes
  node-ids with sites. On AST counts with an explicit `-k` reconstruction,
  D = 153. Either way the qualitative conclusion stands: **the overwhelming
  majority of the 170 sites were not observed under the closed gate**, and ~1/5 of
  the whole inventory (33 sites) lives in a directory Stage 3 never collected.
* Failure surface consistency: the 26 HTTP 401s are all in gate-bearing files
  (harness 16, `get_summary` 1, `test_ui_permission_persistence_flow` 2, and the
  7 singles), and the 16 `WebSocketDisconnect`s are all on gate-bearing apps —
  not one of them lands in the C-no-gate family, which is the expected result and
  supports that family's classification.

## 6. Argue-with-me set (2 genuinely ambiguous sites)

1. `tests/integration/test_health_localhost_binding.py:225` — `test_api_health_allows_test_client…`
   asserts `/api/health` is served to the harness client. Both readings hold
   ("exempt path" and "trust flag allowed it"); it is classed **C-exempt** because
   `/api/health` appears in `EXEMPT_PATHS`, so the trust branch is unreachable and
   the flag cannot change the outcome.
2. `web_ui/backend/tests/test_local_gate_shared.py:165` —
   `test_health_route_allows_local_callers` is parametrised over peer
   ∈ {`127.0.0.1`, `testclient`}. The `testclient` parameter *looks* like a
   trust-flag claim, but the parametrisation supplies an explicit `client=` peer
   and the assertion is positive-only ⇒ **C-peer**. Demoting it to B would be
   defensible; it does not change any total if the rule (§1.4) is applied
   consistently.

## 7. Honesty / limits

* Nothing was executed. Every claim above is from source text; the `-k`
  reconstruction (§5) is a static proxy for pytest's own selection, not a
  collection run, so 153 is a *bound*, not a measurement.
* The 5 starred sites in §4 need their helper functions read before the
  C-no-gate count of 19 can be called exact.
* Class C is not one thing: "exempt path" (7), "explicit peer, positive claim" (2)
  and "gate not mounted" (19) fail for different reasons and should not be
  reported as a single bucket downstream.
* Only file created by this spawn: this document.
