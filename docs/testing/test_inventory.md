# Test Inventory Report

Generated: Static analysis
Total test files: 52
Total test functions: 752
Errors: 0

## 1. Summary by Type

| Type | Files | Tests |
|------|-------|-------|
| integration | 11 | 92 |
| presenter | 4 | 48 |
| security | 4 | 53 |
| unit | 24 | 391 |
| web_ui | 3 | 29 |
| workspace | 6 | 139 |

## 2. Complete Test File Inventory

| # | File | Type | Tests | Classes | Mocks | Async |
|---|------|------|-------|---------|-------|-------|
| 1 | _trace_config_flow.py | unit | 0 |  | N | N |
| 2 | docker_integration/conftest.py | integration | 0 | _RaiseOnDockerCall, _MakeContainer | Y | N |
| 3 | docker_integration/test_container_integrity.py | integration | 24 | TestDockerUnavailable, TestNoContainer, TestContainerMatches | Y | N |
| 4 | docker_integration/test_error_boundaries.py | integration | 17 | TestResolveWorkspaceId, TestComputeDesiredConfig, TestDockerApiErrorBoundaries | Y | N |
| 5 | docker_integration/test_startup_check.py | integration | 13 | TestServerLifespanScan, TestSessionLoadVerification | Y | N |
| 6 | integration/test_config_roundtrip.py | integration | 4 | TestConfigRoundtrip | N | N |
| 7 | integration/test_preset_enforcement.py | integration | 11 | TestPresetEnforcement | N | N |
| 8 | integration/test_session_lifecycle.py | integration | 1 | TestSessionLifecycle | N | N |
| 9 | integration/test_vault_bootstrap.py | integration | 8 | TestVaultBootstrap | N | N |
| 10 | presenter/test_context_updated_bridge.py | presenter | 14 | FakeMetadata, FakeEvent, TestMapAndEmit | Y | N |
| 11 | presenter/test_state_bridge.py | presenter | 10 | TestSaveConfigSystemPrompt | Y | N |
| 12 | presenter/test_token_warning_duplication.py | presenter | 6 | TestTokenWarningDuplication | Y | N |
| 13 | presenter/test_worker_persistence.py | presenter | 18 | _MockEncoding, TestLoadWorkerContexts, TestResumeWorker | Y | N |
| 14 | security/conftest.py | security | 0 |  | N | N |
| 15 | security/test_null_event_bus.py | security | 12 | TestNullEventBusUnit, TestNullEventBusIntegration | N | N |
| 16 | security/test_security_gate.py | security | 35 | TestWorkspaceCapabilitiesModel, TestGetWorkspaceCapabilities, TestEffectivePermissionsMerge | Y | N |
| 17 | smoke_test_config.py | unit | 0 | SmokeResult | N | N |
| 18 | test_api_key_hygiene.py | unit | 13 | TestAgentConfigSerialization, TestStateBridgeApiKeyHygiene, TestLoaderSaveConfigHygiene | Y | N |
| 19 | test_ask_permission.py | unit | 8 | GitReadTool, GitWriteTool, FakeConfig | N | N |
| 20 | test_bridge_permissions_sync.py | unit | 5 | TestBridgePermissionsRoundtrip, TestPermissionEnforcement, TestPermissionsHotSwap | N | N |
| 21 | test_config_layering.py | unit | 35 | TestLoadFactoryConfig, TestDeepMergeConfig, TestComputeConfigDiff | Y | N |
| 22 | test_config_merging.py | unit | 29 | TestDeepMerge, TestResolveConfig, TestResolveFromProfile | N | N |
| 23 | test_config_snapshot.py | unit | 13 | TestConfigSnapshot | N | N |
| 24 | test_emergency_mode.py | unit | 15 | FakeSession, TestEmergencyModeFlag, TestEmergencyModeReduction | N | N |
| 25 | test_event_logger.py | unit | 7 | TestEventLogger | N | N |
| 26 | test_first_run.py | integration | 1 |  | Y | N |
| 27 | test_health_endpoint.py | integration | 9 | TestHealthEndpoint | Y | N |
| 28 | test_history_pruner.py | unit | 45 | TestFindSummaryIndices, TestIsSystemNotification, TestGroupTurns | N | N |
| 29 | test_notification_pipeline.py | unit | 7 | TestNotificationPipeline | N | N |
| 30 | test_per_worker_eventbus_bridge.py | unit | 1 |  | N | N |
| 31 | test_permissions_roundtrip.py | unit | 25 | FileWriteTool, ContainerTool, NetworkTool | N | N |
| 32 | test_security_defaults.py | security | 6 | TestNetworkDefault, TestDefaultPolicy | N | N |
| 33 | test_session_guardrails.py | unit | 7 | TestSessionUserHistoryGuardrail | N | N |
| 34 | test_session_store.py | unit | 31 | TestInit, TestOpenSessionsPath, TestSaveLoadRoundTrip | Y | N |
| 35 | test_state_timeout.py | unit | 13 | TestGetAllowedTools, TestRestrictionReason, TestTimeoutWarningMessage | Y | N |
| 36 | test_system_prompt.py | unit | 16 | TestMigrateLegacySystemPrompt, TestLoadCustomSystemPrompt, TestMigrateSystemPromptInConfig | N | N |
| 37 | test_tool_executor.py | unit | 17 | PermissiveTool, ContainerTool, NetworkAndFilesystemTool | N | N |
| 38 | test_worker_agent_transplant.py | unit | 32 | ScriptedProvider, TestSmokeMultiTurnTask, TestResumeWorkerContinuesConversation | Y | N |
| 39 | test_worker_definition.py | unit | 11 | TestTemplates, TestValidInstantiation, TestJsonSchema | N | N |
| 40 | test_worker_loop_spike.py | unit | 29 | EchoToolProvider, TestWorkerContextAttributes, TestAgentWithWorkerContext | N | N |
| 41 | tools/test_workspace_tools.py | workspace | 51 | TestCheckSystem, TestWorker, TestEditDockerfileRemoved | Y | N |
| 42 | trust/test_gate_contract.py | unit | 24 | TestEffectivePermissionsEdgeValues, TestContainerConfigEdgeValues, TestGetExpectedContainerConfig | N | N |
| 43 | trust/test_toggle_live.py | unit | 8 | TestNoRecreation, TestAskPermissionRestrictive | Y | N |
| 44 | web_ui/backend/test_websocket_integration.py | integration | 4 | TestWebSocketLifecycle | Y | N |
| 45 | web_ui/backend/test_ws_mock_provider.py | web_ui | 6 | MockProvider, TestWebSocketWithMockProvider | Y | N |
| 46 | web_ui/test_templates_endpoint.py | web_ui | 6 | TestGetTemplates | Y | N |
| 47 | web_ui/test_worker_crud.py | web_ui | 17 | TestCreateWorker, TestUpdateWorker, TestDeleteWorker | Y | N |
| 48 | workspace/test_api.py | workspace | 10 | TestGetDockerfile, TestGetDomainAllowlist, TestPutDomainAllowlist | Y | N |
| 49 | workspace/test_bootstrap.py | workspace | 11 | TestEnsureWorkspaceDirs | Y | N |
| 50 | workspace/test_worker_permissions.py | workspace | 22 | TestRestrictiveMerge, TestWorkerPermissionsMergeIntegration | Y | N |
| 51 | workspace/test_worker_templates.py | workspace | 12 | TestLoadTemplateWorkers, TestBuildDefaultWorkers, TestEnsureWorkspaceDirsMerged | Y | N |
| 52 | workspace/test_workspace_registry.py | workspace | 33 | TestWorkspaceRegistryEntry, TestWorkspaceRegistry, TestGenerateHumanId | Y | N |

## 3. Coverage Gaps

| Module | Pattern | Test Files | Status |
|--------|---------|------------|--------|
| Main Agent loop | `agent.core.agent` | test_worker_agent_transplant.py, test_worker_loop_spike.py | Covered |
| Tool execution engine | `agent.core.tool_executor` | test_ask_permission.py, test_bridge_permissions_sync.py, test_permissions_roundtrip.py, test_tool_executor.py | Covered |
| Worker sub-agent | `tools.workspace.worker` | test_per_worker_eventbus_bridge.py, test_worker_agent_transplant.py, tools/test_workspace_tools.py, workspace/test_worker_permissions.py | Covered |
| Respond tool | `tools.respond` | — | NOT COVERED |
| Web UI bridge | `WebAgentBridge` | _trace_config_flow.py, presenter/test_context_updated_bridge.py, presenter/test_worker_persistence.py, test_bridge_permissions_sync.py, test_per_worker_eventbus_bridge.py | Covered |
| Event bus | `EventBus` | security/test_null_event_bus.py, test_event_logger.py, test_per_worker_eventbus_bridge.py | Covered |
| Session config | `SessionConfig` | integration/test_config_roundtrip.py, integration/test_preset_enforcement.py, integration/test_session_lifecycle.py, integration/test_vault_bootstrap.py | Covered |
| Tool presets | `ToolPreset` | — | NOT COVERED |
| Vault | `Vault` | — | NOT COVERED |
| Permissions | `Permission` | security/test_security_gate.py, test_ask_permission.py, test_bridge_permissions_sync.py, test_permissions_roundtrip.py, test_security_defaults.py | Covered |

## 4. Errors / Files that couldn't be parsed

None — all files parsed successfully.

## 5. Known Issues (from earlier pytest run)

### Failing test: `test_token_warning_duplication.py`
- **Root cause**: Test expects `token_warning` event at index 4 (tokens=64000) but gets `token_recovery` instead.
  The `update_token_state()` method emits `token_recovery` when tokens drop below the warning threshold.
  The test's oscillating sequence [50000, 68000, 72000, 85000, 64000, 68000] triggers recovery at index 4.
  The assertion `events[0]["type"] == "token_warning"` fails.
- **Recommendation**: Update the test to accept `token_recovery` events in the recovery phase of the sequence.

### Missing dependencies blocking test collection:
- `tiktoken` — needed by `agent/core/agent.py` → `session/context_builder.py`. Missing from requirements.txt.
- `httpx2` — needed by starlette TestClient. Not installed in base environment.
- `fast-json-repair` — needed at import time by agent modules. Not in requirements.txt.

All 23 collection errors stem from these three missing deps.

## 6. Test Configuration

- `pyproject.toml`: No `[tool.pytest.ini_options]` section
- Root `conftest.py`: Not present
- Subdirectory conftest files: `tests/docker_integration/conftest.py`, `tests/security/conftest.py`
- Pytest markers: None defined
- Asyncio mode: Not configured

## 7. Priority Action List

### Immediate (unblocks suite):
1. Add `tiktoken`, `fast-json-repair`, `httpx2` to requirements.txt
2. Add `[tool.pytest.ini_options]` to pyproject.toml with `testpaths = ["tests"]`, `asyncio_mode = "auto"`, and marker definitions
3. Create root `conftest.py` for shared fixtures

### Quick wins:
4. Fix `test_token_warning_duplication.py` — update assertion to accept `token_recovery`
5. Add basic tests for `tools.respond` (the only tool module with zero coverage)
6. Add tests for `tools.workspace.worker` telemetry/metadata fields

### Medium term:
7. Add `agent.core.agent` integration test for the main agent loop
8. Add `agent.core.tool_executor` unit tests for edge cases
9. Migrate Pydantic V1 `@validator` to V2 `@field_validator`
10. Set up CI pipeline with test categorization (unit/integration/slow)
