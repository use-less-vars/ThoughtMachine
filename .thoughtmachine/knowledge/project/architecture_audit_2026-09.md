# Architecture Audit — 2026-09

**Status:** Phases 0–10 complete (Index + Sections 0b–10 + metadata). No BLOCKER.
**Date:** 2026-09-17
**Authority:** `v3_roadmap.md` is the source of truth for branch sequence, ownership and phase gates. This audit is an *input*, never an authority; it does not authorise work.
**Method:** anchor docs read in full; index entries skimmed (head/anchors) unless marked FULL.

---

## Table of contents

*(start lines are pre-TOC-insertion — current at time of writing)*

- Section 0 — Index — L10
- Section 0b — Documentation sweep (listing only) — L37
- Section 1 — The map — L124
- Section 2 — Container subsystem deep dive — L280
- Section 3 — Duplication survey beyond containers — L380
- Section 4 — God objects and growth — L478
- Section 5 — Boundary map — L548
- Section 6 — Test suite shape — L599
- Section 7 — Doc / code drift — L631
- Section 8 — Risk register — L653
- Section 9 — Migration plan — (new; appended)
- Section 10 — Open questions — (new; appended)
- Section 11 — Audit metadata — (new; appended)

## Section 0 — Index

Legend — **Depth:** FULL = read end-to-end; SKIM = head + anchors. **Key claims** are the load-bearing, falsifiable assertions the doc makes (not a summary).

| # | Path | State | Purpose | Depth | Key claims |
|---|------|-------|---------|-------|------------|
| 1 | `.thoughtmachine/knowledge/project/v3_roadmap.md` | EXISTS | v3 phase plan; SSOT for branch order/ownership/phase gates; supersedes `battle_plan.md` | FULL (214 L) | Phase A branch order L48-56; §9 of target-arch doc is the build order (L59-60); record API = `GET /containers[/{id}]` (L63); infra-touching branches get HOST gate not sandbox (L67-71); first 3 Claim-Ledger claims (L89); guardrails L171-184 (record=state-only, no ORM/bus/scheduler/db, never stage `knowledge/`, `--no-ff`, no `--no-verify`); every PR needs `Claim added/changed:` (L196) |
| 2 | `.thoughtmachine/knowledge/project/container_controls_findings.md` | EXISTS | Pre-brief findings for per-container start/stop/restart/delete controls; append-only session log to 2026-09-17. STATUS: COLLECTED, NOT SCHEDULED | FULL (577 L) | Container-record API UNPROTECTED (`server.py:2682`, no `dependencies=`) §1; start/stop/delete exist, gap is frontend-only §2; B-04 CONFIRMED (UID/tmpfs/`.git` shadow) ; B-05 FIXED by `abd7522`; B-06 CONFIRMED (four status surfaces, two truths); B-07 CONFIRMED highest-risk (reuse ladder never re-applies isolation); fail-open sites incl. Site C `worker_execution.py:338-366` no ownership check; named follow-ons L446-448 |
| 3 | `.thoughtmachine/knowledge/project/v3_vision_full.md` | EXISTS | Compass for post-Phase-B; supersedes the "Beyond V3" list in v3_roadmap | FULL (7,582 B) | Six threads (perf; worker timeouts/async/join; import/resource-catalog scoping; Comm Gate — `domain_allowlist.json` shown/never enforced; credentials as first-class resource; modes as named preset). Shared shape: define global / import per workspace / scope per session / enforce at chokepoint / log / surface in UI. Sequencing only, no dates |
| 4 | `.thoughtmachine/knowledge/project/subsystem_audit_template.md` | EXISTS | Template to apply *before* building on any subsystem | FULL (2,011 B) | §1 five questions (durable SSOT? competing SSOTs? silent failure modes? fail-closed? state survives restart?); §2 ten growth/retention questions. The template a future audit must follow |
| 5 | `docs/container_subsystem_target_architecture.md` | EXISTS | Canonical target design ("where we are going"); companion to `container_subsystem_audit.md` ("where we are") | SKIM (215 L) | Target model = one durable Container Record + one pure fail-closed `resolve_container_config()` + one admission gate; six moves (record; pure resolve; drift-as-events; admission gate; four lifecycle classes; event log); two standing constraints (fail closed; do not lose data). §9 gives the build order |
| 6 | `docs/container_record_design.md` | EXISTS | Design draft (NOT implemented) for the Container Record subsystem: one JSON file per container, state-only, replaces name-keyed registry + `container_notes.json` | SKIM (424 L) | Schema sourced from target-arch §2 (13 fields, each with a named reader); `id`=UUID4 stable across recreate; `schema_version`/`inferred` for synthesised legacy records; `intent_snapshot` drives boot-drift; §9 Questions Resolved (one-label rule scoped to record-owned labels; session derived from `thoughtmachine.worker`). `container_registry_design.md` is the superseded sibling |
| 7 | `docs/CAPABILITIES.md` | EXISTS | Single reference for what TM can do, where it runs, what it does not yet do (no roadmap/vision) | SKIM (303 L) | Platform matrix: Docker code sandbox ❌ on Windows (fails gracefully, degrades via `DockerCodeRunner`); Python ≥3.11, Node ≥18; sandbox config resolved by single SSOT then enforced at create; §delegates Windows boundary to `windows_stability_contract.md` (authoritative, not restated) |
| 8 | `docs/windows_stability_contract.md` | EXISTS | Windows stability guarantees (last updated 2026-06-02) | FULL (5,798 B) | Docker executor NOT available on Windows; PyQt6 GUI excluded; all I/O explicit utf-8; no `shell=True`/`os.system`; config falls back to defaults on corrupt/empty/missing; PyInstaller build. **NOTE:** findings doc flags stale Qt-GUI refs here + `PACKAGING.md` |
| 9 | `docs/testing/ci_process.md` | EXISTS | CI / branch merge rule | FULL (403 B) | "Branches push to origin before merge. Merge to `dev` only after CI is green on the pushed branch." |
| 10 | `pyproject.toml` | EXISTS | Project/build config | FULL (1,796 B) | `requires-python >=3.11`; packages `thoughtmachine/agent/tools/security/web_ui/llm_providers/infra`; `[tool.pytest.ini_options]` `testpaths=["tests"]` (~L51); markers slow/integration/docker/e2e, `docker` registered (~L54). **NOTE:** findings flag the `docker` marker as applied to ZERO nodes |
| 11 | `requirements.txt` | EXISTS | Runtime dependency pins | FULL (469 B) | 25 deps, lower-bounds only (`uvicorn[standard]>=0.27.0`, `docker>=7.0.0`, `websockets>=10.0`; `pytest>=8.0.0` L25, `pytest-timeout>=2.0.0` L26). No upper bounds / no lockfile → `fix/deps-lockfile` follow-on. `requirements-dev.txt:1` = `-r requirements.txt` |

### Index gaps / UNKNOWNs

- **Scope caveat:** this index covers the 11 named paths only. **Measured (Phase 1):** `docs/` holds **50** files and `.thoughtmachine/knowledge/project/` **21** (20 pre-existing + this audit) — see `## Section 0b`, which is the authoritative full listing. The large adjacent set (e.g. `container_subsystem_audit.md`, `container_registry_design.md`, `machine_model.md`, `claim_ledger_design.md`, `docs/decisions/logging-architecture.md`, `docs/architecture/*`) is **unindexed** — read on demand in Phase 1+.
- **Stale-doc signals (unverified here):** findings doc asserts `container_subsystem_target_architecture.md` §9 status column marks items 3-10 "planned" though code ships (`docs/container-subsystem-target-arch-stale-status` follow-on); Qt-GUI refs stale in `windows_stability_contract.md` + `PACKAGING.md`. Not independently confirmed in Phase 0.
- **Anchor-doc line drift:** `container_controls_findings.md` L46-47 marks the end line of `workspace_container_delete` as UNCONFIRMED (`~server.py:3316` approx); all `file:line` anchors are current-truth at dev@2d9c009 and drift as branches land.
- **Not yet read in full:** items 5-7 (target-arch, record-design, CAPABILITIES) were skimmed (head + key anchors + tail); a Phase-1 deep read may surface further load-bearing claims.

---

## Section 0b — Documentation sweep (listing only)

Method: `docs/` walked recursively + `.thoughtmachine/knowledge/project/` listed; for each file the first `#` heading was taken from the first 5 lines (`head -n5 | grep -m1 '^#'`). **Listing only — no reading, no claims.** Flags: `NO_HEADING` = no `#` in first 5 lines; `title insufficient` = first heading is not a document title (code fragment / mid-document heading). Counts below **agree with** Section 0's `### Index gaps / UNKNOWNs` scope caveat (measured `docs/` = **50**, project = **21**); there is no separate "~58 / 20" estimate.

**Totals:** `docs/` = 50 files (incl. 1 `.html`, 1 filename with a space); `.thoughtmachine/knowledge/project/` = 21 files (20 pre-existing + this audit).

### `docs/` (50)

- `docs/CAPABILITIES.md` — # Capabilities Reference
- `docs/LLM_quality insurance.md` — NO_HEADING (17 L) → title insufficient
- `docs/architecture-map.html` — NO_HEADING (HTML; `<title>` = "ThoughtMachine — Architecture Map")
- `docs/architecture-map.md` — # ThoughtMachine — Architecture Map
- `docs/architecture/config_ownership.md` — # Config Ownership Model
- `docs/architecture/session_history_bounding.md` — # Session History Bounding (Phase 1)
- `docs/architecture/vault-restructuring.md` — # Vault Restructuring — Design Document
- `docs/architecture/wlm_completion.md` — # WLM Completion — Architecture Notes
- `docs/audit-worker-core.md` — # Audit: Worker Core (CheckSystem + async/timeout desync)
- `docs/audit/sprint_audit.md` — # Sprint Audit — Backend Config-Change Flow (Chunk 1) (966 L)
- `docs/audit_logging_testing_introspection_2026_08_13.md` — # Audit: Logging, Testing Infra, CheckSystem Introspection, Container Architecture
- `docs/audit_resource_container_isolation_2026_08_13.md` — # Security Audit: Resource Container Isolation (Worker Access)
- `docs/backend-audit.md` — # Backend Audit — Data Models & WS Event Payloads Feeding the Frontend
- `docs/container_p0_bug4_git_fallback.md` — # Bug 4 — git execution fallback under containerized mode (investigation)
- `docs/container_record_design.md` — # Container Record — Design Document
- `docs/container_registry_design.md` — # Container Registry — Design Document (966 L)
- `docs/container_subsystem_audit.md` — # Container Subsystem Audit
- `docs/container_subsystem_target_architecture.md` — # Container Subsystem — Target Architecture
- `docs/decisions/logging-architecture.md` — # ADR: Structured Logging Architecture
- `docs/docker_usage.md` — # First call – install → title insufficient
- `docs/event_pipeline_audit.md` — # Event Pipeline Architecture: Main Agent vs Worker Agent
- `docs/frontend-audit.md` — # Frontend Architectural Audit — web_ui/frontend/src
- `docs/frontend-state-qa.md` — # Frontend State Audit — Q&A (Zustand Consolidation Verification)
- `docs/git_resource_execution_audit.md` — # Git Resource Execution Audit
- `docs/infrastructure/docker-pipeline-trace.md` — # Docker Pipeline Trace
- `docs/installation_guide.md` — # ThoughtMachine Installation Guide
- `docs/logging.md` — # Structured Logging Guide
- `docs/logging_manual.md` — # Returns dict with: log_level, log_tags, truncation, env_vars (171 L) → title insufficient
- `docs/message_metadata.md` — NO_HEADING (104 L) → title insufficient
- `docs/operational_unknowns_2026_08_15.md` — # Operational Unknowns — Investigation Report
- `docs/param_ownership_map.md` — # Parameter Ownership Map
- `docs/pruning_system.md` — # Before (buggy) (229 L) → title insufficient
- `docs/rag_for_code.md` — # ThoughtMachine RAG System – Technical Report
- `docs/research/r3-bootstrap.md` — # Research: Phase 7 — Bootstrap & Agent Initialization (r3)
- `docs/research/r4-checksystem.md` — # Research: Phase 7 — CheckSystem Tool (r4)
- `docs/research/r6-worker-config.md` — # Research: Phase 7 — Worker Configuration (r6)
- `docs/research/synthesis-and-recommendations.md` — # Research: Phase 7 — Synthesis and Recommendations
- `docs/resource_execution_contract.md` — # Resource Execution Contract
- `docs/security_layer.md` — # ⚠️ OBSOLETE — pre-migration Qt architecture
- `docs/system-map.md` — # ThoughtMachine V3 — System Map
- `docs/system_notifications.md` — NO_HEADING (77 L) → title insufficient
- `docs/testing/ci_process.md` — # CI and Branch Process
- `docs/testing/test_inventory.md` — # Test Inventory Report
- `docs/token_pipeline.md` — # Token Pipeline — Debug Trace Standard
- `docs/windows_installation_saga.md` — # The Windows Installation & Running Saga
- `docs/windows_stability_contract.md` — # Windows Stability Contract
- `docs/worker_smoke_checklist.md` — # Worker Panel Smoke Test Checklist
- `docs/workspace_office_operational_analysis_2026_08_15.md` — # Workspace Office — Operational Analysis
- `docs/xray/backend_runtime.md` — # Backend + Vault + Runtime Map — ThoughtMachine (three-layer-ui)
- `docs/xray/frontend.md` — # Frontend X-Ray Map — ThoughtMachine (three-layer-ui)

**0b sweep notes (observations only, unverified claims):** 6 of 50 `docs/` files lack a usable title (`LLM_quality insurance.md`, `docker_usage.md`, `logging_manual.md`, `message_metadata.md`, `pruning_system.md`, `system_notifications.md`); `architecture-map.html` is an HTML twin of `architecture-map.md`. Several docs self-declare stale/obsolete in their own H1 (e.g. `security_layer.md`). Drift-prone: `container_registry_design.md` is the superseded sibling of `container_record_design.md` (already noted in Section 0 item 6).

### `.thoughtmachine/knowledge/project/` (21)

- `architecture_audit_2026-09.md` — # Architecture Audit — 2026-09 (this file)
- `archive_arch_a.md` — # Archive — Architecture Logs A (system_architecture ≤ 2026-07-15)
- `archive_arch_b.md` — # Archive — Architecture & Guides B (system_architecture 2026-07-16 → 2026-07-31; development_guides stale; roadmap completed)
- `battle_plan.md` — # ThoughtMachine — Battle Plan
- `claim_ledger_design.md` — # The Claim Ledger — Design
- `container_controls_findings.md` — # Global container controls — collected findings (pre-brief)
- `development_guides.md` — # Development Guides
- `epoch_plan.md` — # ThoughtMachine Mid-Term Design Document (Epoch Plan)
- `gui_container_panel_brief.md` — # GUI Container Panel — Discovery Brief
- `ideas_and_brainstorming.md` — # Ideas And Brainstorming
- `kb_upgrade_plan.md` — # Kb Upgrade Plan
- `machine_model.md` — # The Machine Model — ThoughtMachine as a Stack of Promises
- `roadmap.md` — # Roadmap
- `security_architecture.md` — # ThoughtMachine Security Architecture — Lay of the Land
- `subsystem_audit_template.md` — # Subsystem Audit Template
- `system_architecture.md` — # System Architecture
- `test_protocols.md` — # Test Protocols
- `v3_roadmap.md` — # ThoughtMachine — V3 Roadmap
- `v3_vision_full.md` — # ThoughtMachine — V3 Vision (Full)
- `working_agreements.md` — # Working agreements — how this project is run right now
- `worktree_commit_policy_findings.md` — # Worktree commit refusal — READ-ONLY archaeology (2026-09-17)

---

## Section 1 — The map

Status: 1a–1d complete. All anchors are current-truth at `dev@2d9c009` (git unavailable in-container → tree walked with `find` + `wc -l`; `grep -n` for anchors).

### 1a — Tree inventory + entry points

**LOC by top-level dir** (command: `find` over `*.py *.ts *.tsx *.jsx *.sh *.css`, excluding `node_modules __pycache__ .venv .tm-fix-scratch .tmp-test temp .tm-node .deb-cache .ms-playwright logs tmp`, then `xargs wc -l`):

| Dir | Files | LOC |
|---|---:|---:|
| `tests/` | 272 | 88,260 |
| `web_ui/` | 122 | 48,018 |
| `tools/` | 50 | 22,295 |
| `agent/` | 62 | 16,717 |
| `thoughtmachine/` | 22 | 9,432 |
| `infra/` | 7 | 8,299 |
| `session/` | 12 | 3,590 |
| `scripts/` | 10 | 2,669 |
| `security/` | 6 | 2,600 |
| `working_docs/` | 2 | 2,438 |
| `llm_providers/` | 7 | 1,273 |
| `mcp_examples/` | 2 | 291 |

Root-level source (`.py`/`.sh`/`.bat`, maxdepth 1): 5,641 LOC. Excluded as vendored/scratch (not counted): `.venv` (~1.27M), `temp/` (~179k), `.tmp-test/` (~76k), `.tm-fix-scratch`, `node-bin`, `chrome-libs`, `resources`, `.git`.

Top-level dirs present on disk: `agent infra llm_providers mcp_examples docs security session tools web_ui thoughtmachine` + non-source (`__pycache__ chrome-libs demo_output logs node-bin presets reports resources scratch temp tests thoughtmachine.egg-info tmp working_docs`). Tests are the single largest body of code (≈1.8× the whole product tree).

**Per-dir module inventory (LOC):**
- `agent/`: `core/agent.py` 1628, `logging/__init__.py` 736, `controller/__init__.py` 745, `events.py` 640, `logging/unified.py` 612, `core/tool_executor.py` 601, `config/vault_drift.py` 836, `config/loader.py` 507, `presenter/session_lifecycle.py` 563, `presenter/agent_presenter.py` 416, `core/state.py` 413, `config/models.py` 378, `presenter/state_bridge.py` 368, `config/session_config.py` 346, `startup_health_check.py` 339, `presenter/event_processor.py` 337, `core/llm_client.py` 291, `config/service.py` 283, `config/resource_catalog.py` 281, `config/defaults.py` 264, `core/worker_context.py` 238, `core/turn_transaction.py` 196, `credentials/injector.py` 165, `knowledge/codebase_indexer.py` 1278.
- `tools/`: `workspace/worker_thread.py` 5173, `code_modifier.py` 1329, `knowledge_base.py` 1297, `git_info_tool.py` 1265, `workspace/check_system.py` 1106, `git_write_tool.py` 684, `workspace/worker_manager.py` 676, `container_control.py` 659, `file_editor.py` 530, `docker_code_runner.py` 519, `mcp_*` (~492+465+374+370+130), `workspace/worker_execution.py` 440, `workspace/worker_query.py` 436, `base.py` 395, `host_bash_tool.py` 331, `field_viewer.py` 322, `workspace/job_registry.py` 290, `workspace/worker_lifecycle.py` 283, `__init__.py` 278, `workspace/working_document.py` 227, `workspace/worker_timeout.py` 212, `workspace/worker_registry.py` 206, `workspace/worker_container.py` 135, `workspace/worker.py` 112.
- `security/`: `security_gate.py` 1388, `admission_gate.py` 643, `resource_catalog.py` 231, `sandboxed_execution.py` 231, `gate_helpers.py` 106.
- `infra/`: `container_manager.py` 4212, `resource_container_manager.py` 2249, `container_registry.py` 988, `workspace_lifecycle_manager.py` 726, `registry_wiring.py` 65, `container_env.py` 59.
- `thoughtmachine/`: `vault_repair.py` 1798, `security.py` 1357, `vault_gc.py` 851, `container_record/{drift 507, api 441, migration 354, storage 322, models 275, hook 265, lifecycle_policy 243, snapshot 147}`, `vault.py` 478, `workspace_capabilities.py` 469, `permission_store.py` 433, `workspace_registry.py` 393, `workspace_lifecycle.py` 330, `bootstrap.py` 292, `doctor.py` 224, `audit_logger.py` 101, `timeout_constants.py` 30.
- `llm_providers/`: `openai_compatible.py` 552, `anthropic_provider.py` 211, `tool_converter.py` 168, `factory.py` 135, `base.py` 128, `exceptions.py` 42.
- `session/`: `store.py` 950, `models.py` 491, `event_schema.py` 449, `context_builder.py` 395, `history_pruner.py` 342, `history_provider.py` 261, `size_bounding.py` 179, `lock.py` 175, `session_registry.py` 163, `tool_presets.py` 136.
- `web_ui/backend/`: `server.py` 3995, `bridge.py` 2612, `workspace_routes.py` 1809, `config_manager.py` 1347, `session_manager.py` 755, `session_routes.py` 580, `global_routes.py` 359, `health_routes.py` 232, `container_record_routes.py` 199, `config_routes.py` 195, `vault_repair_routes.py` 153, `onboarding_routes.py` 139, `prompt_routes.py` 128, `provider_routes.py` 122, `event_forwarder.py` 119, `logging_routes.py` 101 (+ `tests/`).
- `web_ui/frontend/src/`: **JSX, not TS** — `App.jsx`, `main.jsx`, `router.js`, `globalApi.js`, `sessionTabsStore.js`, `styles.css`, `components/`, `data/`, `store/`, `types/`; exactly one `.ts*` file in the tree.

**Entry points (all confirmed to exist):**
| Entry | Anchor |
|---|---|
| Backend ASGI app | `web_ui/backend/server.py:830 app = FastAPI(`; `:3986 uvicorn.run`; `:3994 if __name__ == "__main__"` |
| `serve` launcher (POSIX) | `start_thoughtmachine.sh:404` → `.venv/bin/python -m web_ui.backend.server --port $TM_BACKEND_PORT`; `:164 start_backend()`; `:363 python -m thoughtmachine.bootstrap`; `:125 wait_for_backend` probes `GET /api/health`; `:95/:211` run `python3 doctor`; `:77 TM_REQUIRE_FRONTEND` opt-in; header L7-24 = dev mode (Vite 5173 + backend) |
| Launcher (Windows) | `start_windows.py:245 def main()` (launches FastAPI backend + Vite dev server) |
| Vault bootstrap | `thoughtmachine/bootstrap.py:251 main`; `:74 get_manifest`; `:140 ensure_user_defaults` |
| Vault health/repair | `thoughtmachine/doctor.py` (224); `thoughtmachine/vault_repair.py:151 classify` |
| Agent core | `agent/core/agent.py:96 class Agent` / `:885 process_query` |
| Tool registry | `tools/__init__.py:62 register_tool` / `:279 __all__ TOOL_CLASSES` (+`SIMPLIFIED_TOOL_CLASSES`); MCP tools lazy-registered (L272-273) |
| Docker exec (standalone) | `docker_executor.py:469 class DockerExecutor` / `:818 execute` |

### 1b — Runtime flow (ASCII)

```
 Browser — React/JSX (web_ui/frontend/src, Vite dev 5173)
   │  WS  /ws                                          HTTP  /api/*  (12 routers)
   ▼                                                    ▼
 web_ui/backend/server.py  (FastAPI, 3995 L; app@830; add_middleware@869)
   ├─ @app.websocket("/ws")@888 ──▶ bridge.py  WebAgentBridge:275 ─┐
   ├─ include_router @2671-2683 (workspace, onboarding, config, health, logging, session,
   │                             prompt, global, provider, vault_repair, container_record)
   ├─ GET /api/health@2875        GET /api/health/containers@3695
   └─ event_forwarder.py (119)                                    │
                                                                   ▼
                                        agent/presenter/state_bridge.py  StateBridge:23
                                                                   │
                                                                   ▼
                             agent/core/agent.py  Agent:96 ──▶ process_query:885   (the turn loop)
                               ├─ LLM:  agent/core/llm_client.py LLMClient:42 ──▶ llm_providers/factory.py
                               │        ProviderFactory:11 ──▶ openai_compatible.py / anthropic_provider.py
                               ├─ CONFIG: agent/config/{loader,service,session_config}  ⟵ web_ui/backend/config_manager.py
                               ├─ SESSION: session/store.py, context_builder, history_pruner/size_bounding
                               └─ TOOLS: agent/core/tool_executor.py ToolExecutor:119
                                          execute_tool_calls:145 ──▶ _execute_single_tool:282
                                                                   │
                                                                   ▼
                                    tools/__init__.py register_tool:62 — host tools
                                      (file_editor, git_*, knowledge_base, code_modifier, host_bash…)
                                    ┌──────────────┬───────────────────────┬──────────────────────┐
                                    ▼              ▼                       ▼                      ▼
                       docker_code_runner    container_control      workspace/worker_thread    checkout/consult tools
                       DockerCodeRunner ─▶   ─▶ security/* + infra/*  WorkerThread:807 (sub-agents)
                       docker_executor.py                                 │
                       DockerExecutor:469                                 │ (worker event bus)
                                                                          ▼
 ── SECURITY CHOKEPOINT ──────────────────────────────────────────────────────────────
 security/security_gate.py  get_effective_permissions:763 / resolve_container_config:1001
 security/admission_gate.py ContainerSpec:122 → Allow:151 | Deny:159 | Transform:168 (Probes:198)
 thoughtmachine/security.py validate_path:329 / is_allowed:1000 ; permission_store.py:230/272/313
                                                                          │
 ── CONTAINER SUBSYSTEM ───────────────────────────────────────────────────▼─────────
 infra/container_registry.py ContainerRegistry:266 → register:308 / request_container:351
 infra/container_manager.py  ContainerManager:554 → start:1089 / exec:1746 / stop:2610 / remove:2664 / _reuse_container:3272
 infra/resource_container_manager.py ResourceContainerManager:846 → ensure_container:971 / ensure_resource:1323 / exec:1476
                                                                          │
 ── PERSISTENT STATE ───────────────────────────────────────────────────────▼─────────
 thoughtmachine/container_record/*  (api:141 begin:174 attach:205 ; drift:326 ; hook:206 ; lifecycle_policy:196)
 thoughtmachine/vault.py vault_root:29 / ensure_* ; vault_repair.py:151 ; vault_gc.py ; workspace_registry.py:164
 infrastructure/workspace_lifecycle_manager.py  WorkerSupervisor:201 (WorkerState:100)
```

### 1c — Layer owners

| Layer | Owner module(s) | Key anchor | Responsibility |
|---|---|---|---|
| **Agent loop** | `agent/core/agent.py`; `core/turn_transaction.py`, `core/state.py`; `agent/events.py` | `Agent:96`, `process_query:885` | Turn lifecycle, tool-call sequencing, agent events |
| **Tool layer** | `tools/__init__.py` + `tools/*.py`, `tools/workspace/*` | `register_tool:62`, `__all__:279` | Tool registry, JSON schemas, per-tool execution; host vs sandboxed tools |
| **Security (admission + exec gate + permissions)** | `security/security_gate.py`, `security/admission_gate.py`, `security/gate_helpers.py`; `thoughtmachine/security.py`, `permission_store.py` | `get_effective_permissions:763`, `resolve_container_config:1001`; `ContainerSpec:122`; `validate_path:329`, `is_allowed:1000`; `read/write_session_permissions:230/313` | Single fail-closed chokepoint: effective perms, container config resolution, path validation, admission decision |
| **Container subsystem** | `infra/container_manager.py`, `container_registry.py`, `resource_container_manager.py`, `registry_wiring.py`, `container_env.py`; `docker_executor.py` (root); `tools/docker_code_runner.py` | `ContainerManager:554`; `ContainerRegistry:266`; `ResourceContainerManager:846`; `DockerExecutor:469`, `execute:818` | Container create/reuse/exec/stop/remove + resource containers; three executable paths (see 1d) |
| **Vault (schema / seeding / integrity / repair)** | `thoughtmachine/vault.py`, `bootstrap.py`, `vault_repair.py`, `vault_gc.py`, `workspace_registry.py`, `workspace_lifecycle.py`, `audit_logger.py` | `vault_root:29`, `ensure_vault_structure:56`, `verify_allowlist_integrity:362`; `bootstrap main:251`; `vault_repair classify:151`; `vault_gc _thresholds:156` | Vault layout + defaults, allowlist integrity, repair/gc, workspace registry & teardown |
| **Session config resolution** | `agent/config/{loader,service,session_config,models,defaults,vault_drift,resource_catalog}.py`; `web_ui/backend/config_manager.py`; `session/store.py`, `session_manager.py` | `load_config:287`, `create_agent_config_service:263`, `SessionConfig:56`; `session_manager create_session:105` | Layered config (factory/user/session) + drift detection; persisted session records |
| **LLM providers** | `llm_providers/*`; `agent/core/llm_client.py` | `ProviderFactory:11`, `LLMProvider:50`; `LLMClient:42`, `chat_completion:130` | Provider abstraction + tool-schema conversion; request/response normalisation |
| **WebSocket bridge** | `web_ui/backend/bridge.py`, `event_forwarder.py`, `session_manager.py`; `agent/presenter/*` | `WebAgentBridge:275`, `create_bridge:2610`; `StateBridge:23`; `@app.websocket("/ws")@888` | Client↔agent transport, event fan-out, presenter/state bridging |
| **Worker infrastructure** | `tools/workspace/worker_thread.py` + `worker_{manager,lifecycle,registry,execution,query,timeout,container}.py`, `job_registry.py`, `check_system.py`; `agent/core/worker_context.py`; `infra/workspace_lifecycle_manager.py` | `WorkerThread:807`, `_build_tool_registry:3238`, `shutdown_workers:3257`; `WorkerManager:225`, `get_manager:669`; `WorkerSupervisor:201`, `process_query:385`; `check_system.py` (1106) | Sub-agent spawning, tool registry per worker, lifecycle/stale checks, timeouts, container ownership |

### 1d — Misfits (inventory-level observations)

1. **Three container/exec paths, four large files.** `infra/container_manager.py` (4212) + `infra/resource_container_manager.py` (2249) + root `docker_executor.py` (DockerExecutor) + `tools/docker_code_runner.py` all provision/execute. Ownership of "how a container is made and reused" is split across `infra/` and a root-level module that sits outside both `infra/` and `tools/`.
2. **Worker lifecycle modelled twice.** `tools/workspace/worker_thread.py` (5173 L — the single largest source file) and `infra/workspace_lifecycle_manager.py` (`WorkerState`/`WorkerSupervisor`) are separate lifecycle notions for the same concept; `agent/core/worker_context.py` is a third participant.
3. **Config resolution spans three packages.** `agent/config/*` + `web_ui/backend/config_manager.py` + `session/store.py` all read/write session-vs-workspace-vs-factory config; `config/vault_drift.py` (836) adds a drift detector on top.
4. **Security is two-package.** `security/` (gate + admission) and `thoughtmachine/security.py` + `permission_store.py` both own permission logic; `is_allowed` (thoughtmachine) vs `get_effective_permissions` (security_gate) are parallel authorities to check for overlap.
5. **Test volume dominates.** `tests/` = 88,260 LOC / 272 files vs product (excluding tests) ≈ 90k LOC. Any refactor's blast radius is dominated by tests.
6. **Frontend is JSX, not TS**, despite `web_ui/frontend/src/types/` and `docs/xray/frontend.md` framing — a docs-vs-code type mismatch worth noting when reading the frontend X-Ray.
7. **Docs hygiene:** 6/50 `docs/` files have no usable title; at least one (`security_layer.md`) is self-declared OBSOLETE; `docker_usage.md` and `pruning_system.md` open on mid-document fragments. `docs/` (50) + project KB (21) is a large unindexed corpus (see Section 0 scope caveat).
8. **`docs/` doc-pair duplication:** `architecture-map.md` ↔ `architecture-map.html`; `container_record_design.md` ↔ (superseded) `container_registry_design.md`.

### Phase 7 drift candidate (not chased here)

- `container_controls_findings.md` L46-47 leaves the end line of `workspace_container_delete` as UNCONFIRMED (`~server.py:3316`). Flagged in Section 0 (item 4/§gaps) already; **carry forward to Phase 7** as a drift candidate — do not resolve in this audit.
- **New drift found in Phase 1:** findings doc cites the container-record API router at `server.py:2682` with no `dependencies=`. Current tree shows `container_record_router` registered at **`server.py:2683`** (`vault_repair_router` is at 2682) — a one-line off-by-one. The *substance* of the claim (unprotected router) is unaffected; anchor needs correcting when Phase 7 touches it.

### Open questions (deferred)

1. Do `thoughtmachine/security.py::is_allowed` and `security/security_gate.py::get_effective_permissions` overlap, or is one a strict superset? (1c item 4) — **Phase 2/3**.
2. Which of the three exec paths (`ContainerManager.exec`, `ResourceContainerManager.exec`, `DockerExecutor.execute`) is authoritative for a given tool call? (1d item 1) — **Phase 3**.
3. Are `WorkerState`/`WorkerSupervisor` (infra) and `WorkerThread`/`WorkerManager` (tools) two views of one lifecycle or genuinely two mechanisms? (1d item 2) — **Phase 4**.
4. Is `agent/presenter/state_bridge.py::StateBridge` the same object graph as `web_ui/backend/bridge.py::WebAgentBridge`, or two bridges? — **Phase 3**.
5. Does any CI config actually run the out-of-tree `web_ui/backend/tests/` package, or is it dead in CI too? (Section 6 blind spot 1) — **Phase 7**. `UNKNOWN: reading the CI workflow / scripts that invoke pytest would resolve it`.
6. Is `tests/docker/` (143 nodes) *intended* to carry the `docker` marker, or is the name a legacy misnomer? (Section 6 blind spot 2) — **Phase 7**. `UNKNOWN: git history of the marker + the suite's intent would resolve it`.
7. Which module owns converging the four create sites onto `create_hardened_container` (F1)? — **Phase 9**. `UNKNOWN: the v3_roadmap branch touching infra/container_registry.py would name the owner`.
8. Does the reuse path ever re-apply isolation, or does B-07 stand (R1)? — **Phase 9**. `UNKNOWN: a live-daemon reuse test (a docker-marked node would gate one, but no node carries the marker)`.
9. Are the 10 undeclared `AgentConfig` fields added to `schema_manifest.json` or dropped from the model (F3)? — **Phase 9**.
10. Are the out-of-tree `web_ui/backend/tests/` folded into `testpaths`, or retired (ties item 5)? — **Phase 9**.
11. Is the legacy `agent-exec-<hash>` create path (`docker_executor.py:292,374`) still reachable in shipped configs, or dead (F2)? — **Phase 9**. `UNKNOWN: the vault config values that disable the registry`.
**Resolved after Phases 3+4:**

- **Q1 — partially overlapping; not a strict superset.** `thoughtmachine/security.py::is_allowed:1000` *evaluates* one permission/path against an already-resolved set, while `security/security_gate.py::get_effective_permissions:763` *computes* that set (min over grains `_min_permission:116` + `apply_workspace_ceiling:223`). `security_gate.py:33` imports `thoughtmachine.security`, so the app package is upstream of the gate. The duplication is at the **package scope** (two packages + `web_ui/backend/workspace_routes.py:786` third wrap + duplicated resource catalog), not between these two functions. (Section 3.6.)
- **Q2 — authority is per container-kind, not per tool call.** `container_manager.py:1788` executes in session/agent containers; `resource_container_manager.py:1504` in resource-git containers; `docker_executor.py:866` is the hardened path git/tool execution uses (`GitReadTool` resolves its own exec-mode at `git_info_tool.py:806-993` and delegates). They share the `exec_run` idiom but diverge on env-injection and cmd form. **Not one authority — three by container class** (Section 3.4/3.10). Exact precedence for an arbitrary tool call not fully proven → carry to Phase 5.
- **Q3 — two genuinely parallel mechanisms, not two views.** 2 registries (`worker_manager.py:253` vs `worker_registry.py:76`), 3 staleness helpers (`worker_manager`/`worker_lifecycle`/`worker_query`), 2 state machines (`WorkerSupervisor:201` vs `WorkerThread`/`WorkerSessionLifecycle:569`). (Section 3.8.)
- **Q4 — two different objects.** `agent/presenter/state_bridge.py:23` `StateBridge` is the presenter/config/session/token bridge (used by `RefactoredAgentPresenter:37` and lazily by `WorkerThread:1699/1712` for `token_update`/`context_length`); `web_ui/backend/bridge.py` `WebAgentBridge` is the web/WebSocket layer. Same noun, different object graphs. **Distinct.** (Section 3.5.)





---

## Section 2 — Container subsystem deep dive

Scope: the container *creation* surface (who calls the daemon), the feature-flag surface that switches create stacks, the hardening kwargs each site passes, and the lifecycle/drift/GC surfaces around them. Method: `grep -rnE` over `--include=*.py`, excluding `.venv .git __pycache__ node_modules tests .tm-fix-scratch temp .tmp-test`, plus targeted reads to classify. Anchors are `file:line` at this audit's tree state.

### 2a — Create-site census

Daemon create calls are `client.containers.run(...)`. `.containers.create(` and `create_container(` have **zero** product hits — `create_container(` exists only as the *method name* `create_resource_container` (a registry factory, not a daemon call). `podman` has zero hits (docker-py only).

Product `containers.run(` sites — exactly **four terminal create sites**, matching the code's own numbering (`resource_container_manager.py:1108`: "site 4/4 of the terminal container-create sites"):

| # | Site | `run()` line | Notes |
|---|---|---|---|
| 1 (shared) | `infra/container_registry.py` | `:239` | one `run()` shared by BOTH registry entry points: free-use `request_container:351` and resource `create_resource_container:521`. |
| 2 | `infra/container_manager.py` | `:1463` | inside `_run_container:1456` (docstring: "the single ``run`` call site"). Called by the reuse/recreate path and the legacy fresh-create path inside `_fresh_start:1499`. |
| 3 | `infra/resource_container_manager.py` | `:1213` | inside `_create_resource_container:1037` (legacy branch only; registry-active delegates to site 1 at `:1174`). |
| 4 | `docker_executor.py` (root) | `:790` | inside `DockerExecutor._ensure_container`. |

**No create site exists beyond these four in product code.** Two out-of-product `.run()` sites exist and are *not* part of the product surface:
- `live_smoke_docker.py:233` and `:244` — a standalone host-runnable smoke harness at repo **root** (`live_smoke_docker.py:1-8`); hard-codes `user="1000:1000"`, `network_mode="none"`, no `cap_drop`/`security_opt`. A **5th/6th `.run()` site, out-of-product.**
- tests (`tests/test_worker_sync_query_timeout_containment.py:548/555/704`, `tests/docker/test_persistence.py:135`, `tests/docker/test_container_lifecycle.py:250`) — excluded from the census by scope; they are the only create sites outside the five files above.

`privileged` has **no** product use (only a docstring mention of a "privileged counterpart" at `container_registry.py:526`); `pids_limit` has **zero** hits anywhere. Neither is set at any site → **UNKNOWN:** whether either is a *required* hardening omission (would need the security design doc's threat model to resolve). *not investigated — scope boundary.*

### 2b — `use_*` feature-flag map

Two hot-swappable booleans select between create stacks. Both default **False**, both are `HOT_SWAPPABLE` (`agent/config/models.py:68-69`), and both are declared on both config layers.

| Flag | Default | Declared | Plumbed to agent | Read at |
|---|---|---|---|---|
| `use_container_registry` | `False` | `agent/config/models.py:141-144`; `agent/config/session_config.py:190-193` | `session_config.py:326`; `agent/core/tool_executor.py:456` | `infra/container_registry.py:974-976` (`is_container_registry_enabled`); `infra/registry_wiring.py:5,57-58`; `infra/resource_container_manager.py:180,875`; `tools/workspace/worker_thread.py:3595-3598` |
| `use_workspace_lifecycle_manager` | `False` | `agent/config/models.py:137-140`; `agent/config/session_config.py:186-189` | `session_config.py:325`; `agent/core/tool_executor.py:455` | `infra/workspace_lifecycle_manager.py:195-197,231,268`; `tools/workspace/worker_thread.py:1319-1331,3586-3589` |

Registry activation is additionally gated on a live daemon: `is_registry_active` returns False when the flag is on but docker is unreachable (`infra/registry_wiring.py:57-61`), and callers fall back to the legacy manager path (`container_manager.py:1580`; `resource_container_manager.py:1174` delegate only when `is_registry_active`). **Branch parity:** registry ON → sites 2/3 delegate to site 1; registry OFF → sites 2/3 run their own `run()`. The WLM flag gates a *worker-supervisor* path (`infra/workspace_lifecycle_manager.py`), which itself requests registry containers (`request_container` docstring "Mirrors WorkerSupervisor.request_container (workspace_lifecycle_manager.py L532-563)", `container_registry.py:352-353`). **UNKNOWN:** whether the WLM path can ever reach a `run()` outside the four (it should always go through `request_container` → site 1) — resolving needs a call-graph trace of `WorkerSupervisor.process_query`. *not investigated — scope boundary.*

### 2c — Hardened-kwargs matrix

Constants: `HARDENED_CAP_DROP=["ALL"]`, `HARDENED_SECURITY_OPT=["no-new-privileges:true"]`, `HARDENED_READ_ONLY=True`, `DEFAULT_USER_OOM_SCORE_ADJ=1000`, `DEFAULT_RESOURCE_OOM_SCORE_ADJ=500` (`agent/config/defaults.py:161-163,144-145`); `DEFAULT_MEM_LIMIT="1g"` / `DEFAULT_CPU_QUOTA=100000` (`defaults.py:226-227`); `RESOURCE_MEM_LIMIT="512m"` / `RESOURCE_CPU_QUOTA=50000` (`defaults.py:132-133`).

| kwarg | registry `:239` | container_manager `:1463` | resource `:1213` | docker_executor `:790` |
|---|---|---|---|---|
| `mem_limit` | `profile.mem_limit` (1g / 512m) `:254` | `self.mem_limit` (1g) `:1479` | `self.mem_limit` (512m) `:1230` | `self.mem_limit` (1g) `:804` |
| `cpu_quota` | `profile.cpu_quota` `:255` | `self.cpu_quota` `:1480` | `self.cpu_quota` `:1231` | `self.cpu_quota` `:805` |
| `cap_drop` | `list(HARDENED_CAP_DROP)` `:246` | `["ALL"]` literal `:1470` | `["ALL"]` literal `:1219` | `["ALL"]` literal `:796` |
| `security_opt` | `list(HARDENED_SECURITY_OPT)` `:247` | `["no-new-privileges:true"]` `:1471` | `["no-new-privileges:true"]` `:1220` | `["no-new-privileges:true"]` `:797` |
| `read_only` | `HARDENED_READ_ONLY` `:248` | `True` `:1473` | `True` `:1222` (git *mount* rw — see D3) | `True` `:798` |
| `user` | `(host_user() or "0:0")` `:251` | **`host_user()`** `:1474` | `(host_user() or "0:0")` `:1225` | **`host_user()`** `:799` |
| `oom_score_adj` | `profile.oom_score_adj` (1000/500) `:252` | `1000` `:1472` | `500` `:1221` | **absent** (not set) |
| `network` | `profile.network_mode` `:253` | `network_mode` `:1469` | `network_mode` `:1218` | `network_mode` `:795` |
| `restart_policy` | `docker_restart_policy(lifecycle)` `:262` | `…(lifecycle_class)` `:1486` | `…(LIFECYCLE_RESOURCE)` `:1234` | `…(LIFECYCLE_PERSISTENT)` `:813` |
| `tmpfs` | `dict(profile.tmpfs)` `:256` | recipe `:1519-1527` (+ `/workspace/.git` shadow) | recipe `:1101-1106` (no `.git` shadow) | recipe `:693-702` (+ `.git` shadow) |
| `labels` | `dict(profile.labels)` `:257` | dict `:1542-1560` (+ worker) | `_labels()` `:1207` | literal dict `:807-811` |
| `environment` | `dict(profile.environment)` `:258` | `merge_container_identity_env` `:1481` | `identity_env` `:1232` | `container_env` `:806` |
| `mounts`/`volumes` | `profile.mounts`/`volumes` `:260-261` | workspace rw/ro + `tm-packages-<ws>` `:1529-1541` | workspace **rw** + optional main-repo `:1072-1096` | workspace bind + pkg volume `:716,722-724` |
| `extra_hosts` | `dict(profile.extra_hosts)` `:259` | absent | absent | absent |
| `detach`/`tty`/`stdin_open` | ✓ `:242-244` | ✓ `:1475-1477` | ✓ `:1226-1228` | ✓ `:800-802` |
| `command` | `profile.command` (default `["tail","-f","/dev/null"]`) | literal same `:1478` | literal same `:1229` | literal same `:803` |

`user` Windows policy derives from `host_user()` returning `None` on Windows (`defaults.py:180-193`; `_host_ids:166-178`): docker-py omits `User` when the value is None. **Divergence D1:** two sites net to RUN AS IMAGE DEFAULT on Windows (`container_manager.py:1474`, `docker_executor.py:799`) while two net to RUN AS ROOT:ROOT (`container_registry.py:251`, `resource_container_manager.py:1225` via `or "0:0"`). Both carry an identical "Windows: match the mount owner" comment, so the two behaviours look deliberate but are inconsistent. **This confirms the two sites the briefing named, exactly.** **D2:** `docker_executor.py` never sets `oom_score_adj` → its containers get the daemon default (0), whereas user containers elsewhere are deliberately first OOM victims (1000, `container_manager.py:1472`, comment to that effect) or resource 500. **D4:** `cap_drop`/`security_opt` are value-equal at all sites today, but only the registry reads the named `HARDENED_*` constants; the other three hard-code literals → drift-prone.

### 2d — Lifecycle, drift, restart_policy

**restart_policy** is derived at **every** create site (2c row) via `docker_restart_policy(lifecycle)` (`thoughtmachine/container_record/lifecycle_policy.py:228-243` → `{"Name": ..., "MaximumRetryCount": 0}`), so all four agree structurally. The class table (`lifecycle_policy.py:126-243`; `LifecyclePolicy:114`, `policy_for:196`) maps persistent → `unless-stopped` (`:163,179,187`) and ephemeral → `"no"` (`:171`). Resource containers use `LIFECYCLE_RESOURCE`; the registry picks `LIFECYCLE_RESOURCE` for `container_type == "resource"` else `LIFECYCLE_PERSISTENT` (`container_registry.py:194-198`).

**Drift detection** is centralized in `thoughtmachine/container_record/drift.py`: axes are `_POLICY_AXES = ("network_mode","workspace_mode")` (`:99`) and `_RUNTIME_AXES = (...,"oom_score_adj")` (`:100`); `CLASS_RESTART_POLICY:79`, `EVENT_RESTART_POLICY_MISMATCH="drift.restart_policy_mismatch":88`, `classify_drift:173`, expected `:284-297`, `detect_record_drift:326`. **Observation D5:** `restart_policy` is *not* a member of `_POLICY_AXES`/`_RUNTIME_AXES`; it is classified by a **separate** code path in `container_manager.py:2162-2222` (`_expected_restart_policy:2131`; deny `restart_policy_more_permissive` `:2169`; warn `:2171`; surfaced as `drift["restart_policy"]` `:2473`). So restart_policy *is* drift-checked, but via a different mechanism than the other four axes.

**Orphan / GC sweeps** (all "never raises"; the user-container sweeps always skip resource containers):
- `infra/container_manager.py`: `cleanup_workspace:3557` (stop+force-remove by `workspace_id` label); `cleanup_stale_worker_containers:3591` (worker label exact-match, `created/exited/dead` only); `sweep_exited_workspace_containers:3721` (exited, idle `max_age_s=86400`); `sweep_orphan_container_records:3864` (records whose bound container is gone; conservative no-op when the registry is absent).
- `infra/resource_container_manager.py`: `cleanup_workspace_resources:1931`; `sweep_stale_resource_containers:2039` (resource containers of unregistered workspaces).
- `thoughtmachine/vault_gc.py`: `run_gc:757`; category sweep `_gc_orphan_resource_containers:627` (default 24 h, `TM_GC_ORPHAN_RESOURCE_CONTAINER_HOURS`; docstring table `:26`).
- `thoughtmachine/workspace_lifecycle.py`: teardown orchestrator, steps at `:305-312` (`_cleanup_user_containers:150`, `_cleanup_resource_containers_and_image:163`, `_cleanup_package_volume:176`).
- `tools/workspace/worker_manager.py:103` `PeriodicStaleCheck` is a **heartbeat** watchdog (interrupts hung workers via `_terminate_tracked_executions`), **not** a container GC — flagged in case the name implies otherwise.

**Restart/recreate paths.** All fresh containers flow through `ContainerManager._fresh_start:1499`: the registry-delegation branch (`:1580-1616`, `request_container` → site 1) and the two direct `_run_container` callers — the operator-only recreate (`reuse_record_id` set, `:1660`) and the legacy fresh create under `record_creation` (`:1723`). A crashed worker's stale containers are reaped before respawn (`cleanup_stale_worker_containers`, `:1553`). Docker-side restart behaviour is whatever the daemon `restart_policy` implies; there is **no** in-process re-launch loop. **UNKNOWN:** whether any drift-driven *automatic* recreate exists (vs operator-only) — the recreate path here is explicitly operator-only ("the operator-only recreate path", `:1653`); resolving needs the control-plane caller. *not investigated — scope boundary.*

### 2e — Proposed shared primitive (design sketch only — NO code)

The four sites re-implement one concept with five divergent details (D1 `user`, D2 `oom_score_adj`, D3 resource `read_only`/rw-git-mount, D4 literal-vs-constant hardening; plus the 5th `.run()` site in the smoke harness). Sketch a single primitive, e.g. a new `infra/hardened_container.py`:

1. **`HardenedContainerSpec` (frozen dataclass)** — the union of the per-site fields already enumerated in `container_registry.py:130-153`: `image, command, container_type, lifecycle_class, workspace_id, session_id, mem_limit, cpu_quota, oom_score_adj, network_mode, workspace_mode, labels, environment, mounts, tmpfs, extra_hosts, volumes, name`.
2. **`run_hardened_container(spec, client) -> Container`** — applies the *union* hardening from the one constant set in `agent/config/defaults.py:161-163`: `cap_drop=HARDENED_CAP_DROP`, `security_opt=HARDENED_SECURITY_OPT`, `read_only=HARDENED_READ_ONLY`, `oom_score_adj`, `restart_policy=docker_restart_policy(spec.lifecycle_class)`, the canonical tmpfs recipe, and **one** `user` policy (recommend adopting the `host_user() or "0:0"` form so all sites agree on Windows behaviour; resolves D1).
3. **The four sites delegate**: the registry keeps `request_container`/`create_resource_container` as public entry points but calls the primitive; `container_manager._run_container`, `resource_container_manager._create_resource_container` (legacy branch) and `DockerExecutor._ensure_container` each reduce to "build spec → primitive".
4. **Escape hatches as spec fields, not code forks**: the resource container's rw git mount becomes `mounts=[{… "mode":"rw"}]` on the spec (D3); `docker_executor`'s currently-missing `oom_score_adj` becomes an explicit spec value (D2); the `/workspace/.git` tmpfs shadow becomes a derived field from `workspace_mode`.
5. **Smoke harness** (`live_smoke_docker.py`) should call the primitive too, or be explicitly declared out-of-surface.

Benefits: a single place to audit hardening; the drift matrix collapses to one row; D1/D2/D4 disappear. Risks: 4 call sites + tests; the resource rw-mount is a genuine policy exception that must stay visible, not silently defaulted. **Design sketch only — no code proposed for landing in this audit.**

---

## Checkpoint — after Phase 2

- **Status:** Section 2 complete (2a create census, 2b flag map, 2c kwargs matrix, 2d lifecycle/drift/GC, 2e primitive sketch).
- **2a:** exactly **4** product `containers.run()` sites — `container_registry.py:239` (one call shared by free-use + resource), `container_manager.py:1463`, `resource_container_manager.py:1213`, `docker_executor.py:790` — matching the code's own "site 4/4". No 5th *product* site. Out-of-product: `live_smoke_docker.py:233/244` (root harness) + tests.
- **2b:** `use_container_registry` and `use_workspace_lifecycle_manager`; both `False`, `HOT_SWAPPABLE`, declared on both config layers; registry callers fail closed when no daemon.
- **2c:** matrix complete. Divergences: **D1** `user` fallback (2 sites `or "0:0"`, 2 sites bare `host_user()` — confirms briefing), **D2** `docker_executor` omits `oom_score_adj`, **D3** resource git mount rw / `read_only=False` (documented at `:1069-1077`), **D4** literals vs `HARDENED_*` constants.
- **2d:** `restart_policy` derived at all 4 sites via `docker_restart_policy`; drift axes exclude restart_policy (handled by a separate path, `container_manager.py:2162-2222`); orphan GC spans `container_manager`/`resource_container_manager`/`vault_gc`/`workspace_lifecycle`; recreate is operator-only.
- **Gaps left (scope boundaries, not blockers):** call-graph proof that no WLM path reaches a 5th `run()`; whether `privileged`/`pids_limit` are required hardening; whether drift ever triggers automatic recreate.
- **Edits this turn:** ZERO outside the audit doc.
- **ready for Phase 3**


## Section 3 — Duplication survey beyond containers

Method: for each of 9 ownership domains, locate every function/class that *resolves*,
*computes* or *mutates* the same concept and record whether the domain has one owner
(single) or many (multiple). Verdict = SINGLE OWNER when one module/class is the sole
authority and all others consume it; MULTIPLE when ≥2 sites independently carry the logic
(so a change to the concept must be applied in ≥2 places). Line refs are `file:line`.

- **3.1 Vault lifecycle — MULTIPLE (seed / detect / repair / GC spread across 4 modules + a 5th HTTP forwarder).**
  Seed: `thoughtmachine/vault.py` (`ensure_vault_structure:56`, `_seed_manifest_safe_default:126`,
  `ensure_vault_defaults:157`, `load_factory_defaults:347`). Detect: `agent/config/vault_drift.py`
  (`VaultDriftChecker:101`, `check:129`). Repair: `thoughtmachine/vault_repair.py`
  (`classify:151`, `locate:187`, `run_repair:1507`). GC: `thoughtmachine/vault_gc.py` (`run_gc:757`).
  HTTP forwarder: `web_ui/backend/vault_repair_routes.py` (`status:54`, `apply:96`).
  **Confirmed drift:** `vault_repair.classify:151` + `locate:187` are duplicated *verbatim* in
  `scripts/vault_permission_cleanup_dryrun.py:148` / `:198`; `scripts/migrate_vault.py:17` defines a
  local `vault_root()` shadowing `vault.py:29`. The dry-run script is a stale copy of the repair logic —
  any change to `vault_repair.classify` silently desyncs the dry-run.

- **3.2 Session/config defaults — MULTIPLE (8 default-resolving functions in 4 modules across 3 packages).**
  `agent/config/loader.py` (`load_factory_config:87`, `load_default_config:278`, `load_config:287`);
  `agent/config/config_manager.py` (`resolve_config_defaults:37`); `agent/config/defaults.py` (literal
  table: `RESOURCE_MEM_LIMIT="512m":132`, `DEFAULT_MEM_LIMIT="1g":226`, `DEFAULT_CPU_QUOTA:227`,
  `HARDENED_CAP_DROP:161`, `DEFAULT_SESSION_PERMISSIONS:256`, `MAX_WORKERS_PER_SESSION:39`,
  `HEARTBEAT_STALE_AFTER_S:43`); `web_ui/backend/config_manager.py` (`load_global_defaults:161`,
  `_load_factory_defaults:520`, `_load_global_defaults_layer:536`, `resolve_full_config:803`);
  `thoughtmachine/vault.py` (`load_factory_defaults:347`). `defaults.py` is the intended consolidated
  table, but the web layer re-implements its own layered resolver (`_load_factory_defaults` +
  `_load_global_defaults_layer` + `resolve_full_config`) rather than calling `agent/config/loader`.
  Permission coercion is likewise centralised in `thoughtmachine/security.py:209`
  (`coerce_session_permissions`) + `SessionPermissions:105` and is consumed (not re-implemented) at
  `session/models.py:349/385/410/445` and `session_routes.py:431/528/563` → that sub-concept is SINGLE OWNER.

- **3.3 LLM providers — SINGLE OWNER.**
  `llm_providers/factory.py:11` `ProviderFactory` (`create_provider:51`, `create_from_dict:119`) is the
  sole constructor; `base.py:50` `LLMProvider` is the ABC; `AnthropicProvider:22` and
  `OpenAICompatibleProvider:20` implement it. `agent/core/llm_client.py:42` (`LLMClient:60`) is a
  *consumer* that calls `create_provider` — a wrapper, not a second owner.
  **Minor inconsistency:** `ToolFormatConverter` is used only by `AnthropicProvider`
  (`anthropic_provider.py:18/32`); the OpenAI provider inlines tool-format handling → small format
  divergence *inside* one package (not a cross-package duplication).

- **3.4 Git tools — SINGLE OWNER via inheritance.**
  `GitReadTool` (`git_info_tool.py:100`, `execute:344`) is the base; `GitWriteTool(GitReadTool)`
  (`git_write_tool.py:19`, `execute:143`) overrides. **Watch item:** `git_info_tool` carries a *parallel*
  exec-mode/resource resolver (`_git_execution_mode:806`, `_use_container_mode:845`,
  `_resolve_resource_execution:860`, `_ensure_resource_container:954`, `_resolve_registry_workspace_info:993`)
  that mirrors `infra/registry_wiring` — same *decision* computed twice in two trees.

- **3.5 WebSocket bridge — SINGLE OWNER with forwarder overlap.**
  `bridge.py` `WebAgentBridge` is the single owner, but `bridge.py` `broadcast_rename:971` /
  `broadcast_logging_config:976` duplicate `event_forwarder.py` `broadcast_rename:101` /
  `broadcast_logging_config:113` — two broadcast paths for the same two events.

- **3.6 Security / permissions — MULTIPLE (two packages + a third HTTP wrap + a duplicated resource catalog).**
  Package `security/`: `security_gate.py` (`get_effective_permissions:763`, `_min_permission:116`,
  `apply_workspace_ceiling:223`, `resolve_container_config:1001`, `check_required_categories:1197`) +
  `admission_gate.py` (`admit:321`, `_narrow:555`) + `gate_helpers.py` (`resolve_network_mode:90`) +
  `resource_catalog.py` (`coerce_resource_permissions:205`) + `sandboxed_execution.py:48`. Package
  `thoughtmachine/security.py` (`SessionPermissions:105`, `coerce_session_permissions:209`,
  `validate_path:329`, `is_allowed:1000`, `get_default_security_config:1192`, `merge_security_config:1207`,
  `get_security_profile:1224`). Note `security_gate.py:33` *imports from* `thoughtmachine.security`, so the
  app package is upstream of the gate — but both carry permission-resolution logic. Third parallel wrap:
  `web_ui/backend/workspace_routes.py:786` `async get_effective_permissions`. **Resource catalog is
  literally duplicated:** `security/resource_catalog.py` vs `agent/config/resource_catalog.py` (+ a `.json`).
  `gate_helpers.py:8` documents a known import cycle.

- **3.7 Tool dispatch — MULTIPLE (two registries + a separate executor).**
  Registry #1: `tools/__init__.py` (`register_tool:62`, `__all__:279`). Registry #2:
  `tools/workspace/worker_thread.py` (`_build_tool_registry:3238`, `_get_tool_registry:3243`) — a second,
  independent registry. Executor: `agent/core/tool_executor.py` `ToolExecutor:119`
  (`execute_tool_calls:145`, `_execute_single_tool:282`).

- **3.8 Worker infrastructure — MULTIPLE (2 registries + 3 staleness helpers + 2 state machines).**
  Registries duplicated: `worker_manager.py` (`register_worker:253`, `unregister_worker:265`,
  `get_worker:277`, `find_workers_by_name:288`) vs `worker_registry.py` (`:76/:84/:91/:107`). Staleness
  triplicated: `worker_manager.py` (`_is_alive:156`, `_heartbeat_age:166`, `_is_alive:642`,
  `PeriodicStaleCheck:103`) vs `worker_lifecycle.py` (`_heartbeat_age:130`, `_is_stale:143`, `_is_hung:148`,
  `check_stale_transitions:199`) vs `worker_query.py` (`_is_alive:237`). Lifecycle state machines:
  `infra/workspace_lifecycle_manager.py` `WorkerSupervisor:201` (`transition_*`, `process_query:385`,
  `request_container:582`) vs `worker_thread.py` `WorkerSessionLifecycle:569` / `WorkerThread:807`.

- **3.9 Launcher scripts — MULTIPLE (shell duplication + a parallel Python re-implementation).**
  `start_thoughtmachine.sh` (499 L) and `install.sh` (345 L) share **byte-identical** `doctor()`
  (`start:94` / `install:26`) and `json_get()` (`start:99` / `install:31`). `start_windows.py` (391 L)
  re-implements the same start sequence in Python (venv `:57/:183`, port `:97`, docker `:159`, health `:143`).

- **3.10 Focus: the three container-exec call paths (highest-value duplication).**
  `docker_executor.py:866`, `container_manager.py:1788` and `resource_container_manager.py:1504` each
  build the identical kwargs dict (`{"cmd": …, "demux": True, "workdir": …}`) and call
  `container.exec_run(**exec_kwargs)` inside a copy-pasted `threading.Thread` + `queue.Queue` streaming
  wrapper (×3). They **converge** on the exec call shape but **diverge** on two axes:
  (i) env injection — `container_manager` and `resource_container_manager` call
  `merge_container_identity_env(...)`, whereas `docker_executor` does **not** (injects env only when the
  dict is truthy) ⇒ behavioural drift; (ii) command form — `resource_container_manager` accepts
  `list(cmd)` argv while the other two force `["/bin/sh", "-c", command]`. This is the clearest
  "same operation, three owners, disagree on details" case in the codebase.

## Section 4 — God objects and growth

Census scope: product `.py` only (excluded `.venv`, `.git`, `__pycache__`, `node_modules`, `tests`,
`.tm-fix-scratch`, `temp`, `.tmp-test`, `web_ui/frontend`, `docs`; `temp/baseline` and
`.tm-fix-scratch/dab` are stale copies, not counted). "method" = `def` at class-body indent;
"extRefs" = occurrences of `\bClassName\b` in product files **excluding** the defining file.

- **4a Population shape (context for thresholds).** 252 classes total. The **median class = 1 method /
  28.5 LOC**; the **p90 for methods = 14**. So the distribution is heavily skewed toward tiny classes
  with a handful of large ones — a fixed absolute cutoff is the only defensible rule.

- **4b Threshold.** Three axes: **methods ≥ 25**, **class LOC ≥ 800**, **extRefs ≥ 50**. Rationale:
  25 methods ≈ 1.8× p90 (14) and ≈ 25× median (1); 800 LOC ≈ 28× median class LOC (28.5). Rule:
  **any single axis ⇒ "flagged"; ≥2 axes ⇒ "CONFIRMED god object".** At this cutoff the axes hit
  11 / 10 / 14 classes respectively and the ≥2-axis rule yields **7 confirmed** = **2.8 %** of 252 classes.
  (A softer cutoff m≥15 / loc≥500 / ref≥30 would hit 25/22/22 and 18 confirmed — kept as a sensitivity band.)

- **4c Top files by LOC** (product, excl. tests): `worker_thread.py` **5174** (5 cls, 78 meth),
  `container_manager.py` **4213** (1 cls, 72 meth), `web_ui/backend/server.py` **3996** (2 cls but 51
  module-level `def`/`async def`, 21 routes, 4 container-sweep fns `:333/388/440/482` + loop `:499`),
  `bridge.py` **2613** (1 cls, 60 meth), `resource_container_manager.py` **2250** (2 cls, 18 meth),
  `workspace_routes.py` 1810, `vault_repair.py` 1799, `agent/core/agent.py` 1629 (`Agent:40`),
  `docker_executor.py` 1398, `security/security_gate.py` 1389, `thoughtmachine/security.py` 1358,
  `web_ui/backend/config_manager.py` 1348 (`ConfigManager:18`), `tools/code_modifier.py` 1330,
  `tools/knowledge_base.py` 1298, `codebase_indexer.py` 1279, `git_info_tool.py` 1266 (`GitReadTool:30`),
  `check_system.py` 1107, `container_registry.py` 989, `session/store.py` 951 (`FileSystemSessionStore:30`),
  `vault_gc.py` 852, `vault_drift.py` 837 (`VaultDriftChecker:21`), `scripts/doctor_checks.py` 763,
  `session_manager.py` 756, `agent/controller/__init__.py` 746 (`AgentController:23`).

- **4d Top classes by method count** (cLOC, extRefs): `ContainerManager` 72 (3000, 78);
  `WebAgentBridge` 60 (2329, 17); `_AgentLogger` 51; `RefactoredAgentPresenter` 47 (0);
  `Agent` 40 (1534, 94); `WorkerThread` 37 (2358, 10); `GitReadTool` 30 (1161, 7);
  `FileSystemSessionStore` 30 (863, 37); `WorkerSupervisor` 27 (526, 5); `Worker` 25 (1894, 69);
  `WorkerManager` 25 (436, 5); `AgentController` 23 (733, 24); `CheckSystem` 22 (925, 13);
  `VaultDriftChecker` 21 (736, 12); `SessionManager` 20 (669, 25); `Session` 19 (352, 219);
  `ConfigManager` 18 (437, 16); `GitWriteTool` 18 (666, 6).
  **Top by external refs:** `Session` 219; `AgentConfig` 188; `Agent` 94; `LogLevel` 91; `ToolBase` 87;
  `EventType` 81; `ContainerManager` 78; `Worker` 69; `WorkspaceRegistry` 68; `ExecutionState` 62;
  `SessionRegistry` 61; `SessionConfig` 60; `EventBus` 52; `SessionPermissions` 50.

- **4e CONFIRMED god objects (≥2 axes) and the two suspect files called out for review.**
  - `bridge.py` — **CONFIRMED.** 2613 LOC; `WebAgentBridge` 60 meth / 2329 cLOC; 13 commits/30 d.
    One class does worker-event subscriptions + security subscriptions + session persistence + config
    apply + controller lifecycle + event mapping.
  - `server.py` — **CONFIRMED.** 3996 LOC; 51 module fns, 21 routes, 4 container-sweep fns; 27 commits/30 d.
    Web transport + container sweeps + record-user-actions + frontend hosting + argument parsing in one module.
  - `resource_container_manager.py` — **NOT confirmed (large-but-coherent).** 2250 LOC but
    `ResourceContainerManager` has only 17 methods (< 25); the bulk is module-level helpers around a single
    resource-git lifecycle. Flagged for review (11 commits/30 d) but not a confirmed god object.
  - `container_manager.py` / `ContainerManager` — **CONFIRMED** on methods (72) + LOC axes.

- **4f Growth signal (30 d churn).** `git_read since="30 days ago"` returns ≥200 commits (hits the
  `max_count=200` cap) ≈ **6.7 commits/day** repo-wide. Per-file hotspots: `server.py` 27,
  `bridge.py` 13, `resource_container_manager.py` 11, `worker_thread.py` 6. The two CONFIRMED god
  objects (`server.py`, `bridge.py`) are also the two hottest files — high coupling + high churn =
  highest-value refactor targets.

## Checkpoint — after Phases 3+4

- **Phase 3 done:** 9 ownership domains classified (6 MULTIPLE, 3 SINGLE-OWNER), plus the 3-exec-path
  focus. Verdicts and line refs in Section 3; all MULTIPLE verdicts are actionable (dedupe) targets.
- **Phase 4 done:** census over 252 product classes; threshold defined and justified (4b); 7 CONFIRMED
  god objects (2.8 %); churn correlated (`server.py`, `bridge.py` are both god objects *and* hot files).
- **Strongest cross-cutting findings:** (i) `vault_repair.classify/locate` duplicated verbatim in a dry-run
  script (silent desync risk); (ii) 3 container-exec paths that agree on shape but disagree on env-injection
  and cmd form; (iii) security split across two packages with a duplicated resource catalog + a third HTTP wrap.
- **Edits this turn:** ZERO outside this audit doc — two appends (Sections 3–4 + this checkpoint) after the Phase-2 checkpoint, plus one insert at L260 resolving the deferred Open questions. No pre-existing line altered.
- **ready for Phase 5**


## Section 5 — Boundary map

Method: each of the 7 required boundary classes is mapped to its enforcing seam(s). Every row records **(a)** enforcing `file:line`, **(b)** one-sentence what, **(c)** verdict **HARDENED / ASSUMED / MIXED** + the conftest/fixture/test that decides it. **14 boundaries across 7 classes: 8 HARDENED, 5 ASSUMED, 1 MIXED.** Definitions used: **HARDENED** = enforced fail-closed at a single chokepoint **and** pinned by a dedicated test under `tests/`; **ASSUMED** = the seam exists but nothing in the selected suite exercises it; **MIXED** = gate hardened, surface assumed. **All paths are TOP-LEVEL package paths** (`agent/ tools/ security/ infra/ web_ui/ llm_providers/ session/` + root `docker_executor.py`); the repo also contains a `thoughtmachine/` shim dir, but product code lives in the top-level packages.

### 5.1 host ⇄ container

- **B1 — create-time mount/tmpfs seam.** `infra/container_manager.py:_run_container:1456` (`volumes=None` L1466; `/home/agent` + `/workspace/.git`-shadow tmpfs L1521-1527; named `tm-packages-<ws>:/home/agent/.local` L1596). What: no host workspace bind-mounts — the workspace is in-container, plus two tmpfs and one named package volume. Verdict **ASSUMED** — isolation is applied at CREATE only; `container_controls_findings.md` **B-07 (CONFIRMED** per Section 0 item 2) reports the reuse ladder never re-applies isolation, so a *reused* container's mount/user/tmpfs state is not re-proven. `tests/docker_integration/conftest.py` **fakes** the client (`patch.object(docker,"from_env")` + `make_container`) so mount assertions run against a fake whose `Mounts` the test itself builds.
- **B2 — identity-env seam.** `infra/container_env.py:merge_container_identity_env:22` (leaf, stdlib-only) consumed at `container_manager.py:1481/1586/1793` and `resource_container_manager.py:1178/1509`. What: injects `THOUGHTMACHINE_SESSION_ID`/`_WORKSPACE_ID`, never clobbers caller keys, accepts docker dict/list env shapes. Verdict **ASSUMED** — pure helper, but no test in `tests/` pins the merge/no-clobber contract; correctness is trusted from the leaf design.
- **B3 — host-shell execution.** `tools/host_bash_tool.py:HostBashTool.execute:214`; the exec is `subprocess.run(cmd, shell=True, ...)` at **L290**. What: optional host-shell tool, double-gated by the workspace `allow_host_resources` policy (checked first) + the `host_bash` grain (default banned; `ask`/`allow` required; `ask` denied in worker context), then audit-logged to `host_bash_audit.jsonl`. Verdict **MIXED** — the *gate* is HARDENED (`tests/test_host_bash_tool.py`, 438 L); the *execution surface* is **ASSUMED** (see 5.8 #1).

### 5.2 sync ⇄ async

- **B4 — worker/callback thread → asyncio loop.** `web_ui/backend/server.py:939` `asyncio.run_coroutine_threadsafe(send_event(event), _loop)`; broadcast scheduling seam `server.py:521-533`. What: cross-thread event send onto the server loop, guarded by `_shutting_down` + `_loop.is_closed()`. Verdict **HARDENED** — `tests/integration/test_session_loaded_contract.py` (docstring L18-26 names server.py:521-533) + `tests/test_container_sweep_scheduler.py`.
- **B5 — sync `close()` → async provider `aclose()`.** `agent/core/llm_client.py:255-278` (`LLMClient.close()`; L268 `asyncio.get_event_loop()` → `create_task`/`run_until_complete(provider.aclose())`). What: shutdown of an async provider from sync code. Verdict **ASSUMED** — deprecated-loop path; no test drives the async-provider branch.
- **B6 — loop → thread offload.** `web_ui/backend/server.py:511` `await asyncio.to_thread(_run_container_sweeps)`. What: periodic container sweep run off the event loop. Verdict **HARDENED** — `tests/test_container_sweep_scheduler.py` drives `server.lifespan`.

### 5.3 trusted ⇄ untrusted

- **B7 — subprocess sandbox chokepoint.** `security/sandboxed_execution.py:SandboxedExecution.run:67` (fail-closed `required_category` via `security/gate_helpers._value_satisfies`; shell-metachar reject `_SHELL_METACHARS`; hermetic env `HOME=/dev/null` + fixed PATH + git hardening; **always `shell=False`**). What: the one runner that external commands should pass through. Verdict **HARDENED** — `tests/security/test_sandboxed_execution.py` (79 L) + `tests/security/test_git_container_sandbox.py` (248 L).
- **B8 — host-path confinement (HTTP surface).** `web_ui/backend/server.py:2690-2706` (`_confine_to_home:2706`, used at 2749/2835/3062; endpoints — path browser / create-dir / container lifecycle — must resolve to an ABSOLUTE path OUTSIDE the vault root `~/.thoughtmachine`). What: blocks HTTP callers from addressing vault internals. Verdict **HARDENED** — `tests/test_host_api_hardening.py` (577 L) + `tests/security/test_validate_path.py`.

### 5.4 network (in / out)

- **B9 — OUT (container egress).** `security/gate_helpers.py:resolve_network_mode:90` (`True`/`"write"`/`"outbound"` → `"bridge"`, everything else → `"none"`); consumed by `security/security_gate.py:resolve_container_config:1001`, `docker_executor._resolve_container_config_via_gate:176`, `infra/container_registry._resolve_network_mode_via_gate`. What: single source of truth mapping a network grant to the docker `network_mode`. Verdict **HARDENED** — `tests/security/test_network_mode_gate.py` (142 L).
- **B10 — IN (server ingress).** `web_ui/backend/server.py:3940` default bind `127.0.0.1` (`HOST` env override) + `uvicorn.run` L3986; CORS `allow_origins=["*"]` L869-871; routers registered with **no `dependencies=`** (L2671-2683, incl. `container_record_router` L2683). What: the process's inbound posture. Verdict **ASSUMED** — no test pins the bind address or narrows origins; the wildcard CORS + dependency-free routers are code-only facts.

### 5.5 credentials

- **B11 — credential injection.** `agent/credentials/injector.py:CredentialInjector:52` (`resolve:66` — key validation `_validate_key:139` rejects separators / `..` / `~` / null / absolute; realpath-prefix symlink-traversal check; returns `Secret`, a `str` subclass redacting `repr`/`str`/`format`; `inject:110` resolves `{{credential:<key>}}` in flat tool args). What: `{{credential:key}}` → vault-file secret, never logged. Verdict **HARDENED** — `tests/integration/test_credential_injector.py`, `test_credential_leakage.py`, `test_credential_provider_injection.py`, `test_credential_e2e_trust.py`, `tests/test_credentials_crud.py`.
- **B12 — api_key hygiene.** `agent/config/models.py:AgentConfig` `model_dump(exclude={"api_key"})` path (loader / `StateBridge` / `ConfigService` / `server._config_to_dict`). What: `api_key` must never enter serialised output or persisted config. Verdict **HARDENED** — `tests/test_api_key_hygiene.py` (239 L).

### 5.6 LLM-provider ⇄ agent-loop

- **B13 — provider construction/call.** `agent/core/llm_client.py:LLMClient:42` (`create_provider:60` via `llm_providers.factory.ProviderFactory`; `chat_completion:130`; provider-error mapping L154); impls `llm_providers/anthropic_provider.py`, `llm_providers/openai_compatible.py`. What: the agent loop's only egress to a model. Verdict **ASSUMED** — providers are constructed only through mocks/patches; no test in `tests/` makes a live or recorded provider call, so streaming / tool-call format / error taxonomy at the boundary is unproven.

### 5.7 MCP

- **B14 — MCP connect.** `tools/mcp_server_connect.py:MCPServerConnect:54` (`required_categories=["mcp:connect"]`; reads vault `~/.thoughtmachine/mcp_servers.json`, always read-only; runs the registry `command` via `SandboxedExecution.run(required_category="mcp:connect", extra_env=env)` L98-105). What: stdio JSON-RPC `initialize` handshake to a registered server. Verdict **HARDENED** — `tests/security/test_mcp_server_connect.py` (160 L). **Caveat:** the registry-supplied `env` is passed through verbatim (`extra_env=env`) — registry integrity is the operator's, not enforced here.

### 5.8 Highest-blast ASSUMED boundaries (3)

1. **host-shell execution surface** — `tools/host_bash_tool.py:290` `subprocess.run(cmd, shell=True, ...)`. This is the **only `shell=True` in product code** (`grep shell=True` over `agent/ tools/ security/ infra/ web_ui/ llm_providers/ session/` returns this one line + two *comment* lines in `sandboxed_execution.py:15/144`). It **contradicts** `security/sandboxed_execution.py:15` ("`shell=True` is NEVER used") and `docs/windows_stability_contract.md` ("no `shell=True`/`os.system`"). Blast radius: once both gates pass, arbitrary shell runs on the host with the **full ambient environment** (no `_STRIPPED_ENV`, no metachar scan); audit-logged but not sandboxed. Decided by: `tests/test_host_bash_tool.py` (gate only — no surface/env assertion).
2. **LLM-provider live contract** — `agent/core/llm_client.py:130` ↔ `llm_providers/*`. Blast radius: the whole agent loop rides this seam. Decided by: **absence** — no test exercises a real provider path.
3. **network-ingress posture** — `web_ui/backend/server.py:871` CORS `allow_origins=["*"]` + L3940 loopback default bind (explicitly overridable via `HOST`) + dependency-free routers (L2671-2683). Blast radius: pointed beyond loopback, any origin can drive an unauthenticated API. Decided by: **absence** — no test pins bind/origins.

*(Honourable mention, already CONFIRMED not assumed: B1 reuse-ladder isolation non-re-application — `container_controls_findings.md` B-07.)*

**UNKNOWNs (Section 5):** (U1) the exact auth model of every registered router — `UNKNOWN: read each `web_ui/backend/*_routes.py` + the FastAPI dependency graph would resolve it`. (U2) whether the current tree's reuse path re-applies isolation — `UNKNOWN: reading the reuse branch of infra/container_manager.py` (findings doc B-07 asserts it does not). (U3) live provider wire behaviour — `UNKNOWN: a recorded-cassette or live provider test`. (U4) MCP transports other than stdio (registry declares `"transport"`, only `"stdio"` is exercised) — `UNKNOWN: reading the registry schema + transport dispatch`.

**not investigated — scope boundary:** per-router auth model; MCP transports ≠ stdio; credential *storage write* path (only read/inject inspected); provider streaming/tool-call wire format; the `thoughtmachine/` shim package internals.

## Section 6 — Test suite shape

All numbers **MEASURED** (Phase 6a) on **Python 3.11.16 / pytest 9.1.1**.

**Layout.** `pyproject.toml` `testpaths=["tests"]` (L51); markers `slow/integration/docker/e2e` (L54). Four conftests live under `tests/`: root `tests/conftest.py` (15.5 KB), `tests/docker_integration/conftest.py`, `tests/e2e/conftest.py`, `tests/security/conftest.py` (42 L — sys.path fix so `security` resolves to the repo root, not the `tests/security/` shadow). **A fifth conftest lives OUT of tree** at `web_ui/backend/tests/conftest.py` — that whole package is **not collected by a bare `pytest`** because `testpaths` restricts collection to `tests/`.

**Hermeticity (root conftest).** At IMPORT time: `HOME` is redirected to a tempfile base (Windows `USERPROFILE`/`HOMEDRIVE`/`HOMEPATH` too) and `pathlib.Path.home` + `os.path.expanduser` are patched. Autouse session fixture `hermetic_fs_guard` wraps `os.makedirs/mkdir/remove/unlink/rmdir/rename/replace/open/utime/chmod`, `shutil.rmtree/move`, and `builtins.open`/`io.open` to **raise on writes into the REAL vault** (escape hatch `THOUGHTMACHINE_HERMETIC_GUARD_DISABLED=1`). Autouse `_reset_container_manager_memos` clears `infra.container_manager` module-memo dicts (`_NAME_INDEX` etc.). `tests/docker_integration/conftest.py` **fakes Docker** (`patch.object(docker,"from_env")` + `make_container` factory with fake `Mounts`/`HostConfig`) → **docker is FAKED, never live**. `tests/e2e/conftest.py` is INERT without `--run-e2e` (`pytest_collection_modifyitems` skips every `e2e`-marked test); with the flag it boots a REAL backend subprocess (`web_ui.backend.server`) + Vite.

**Measured totals.** `python3 -m pytest --collect-only -q` → **3580 tests collected** (raw last line "3580 tests collected in 3.53s"; node-id count 3580). `-m "not docker and not e2e"` → **3576 collected / 4 deselected**. Marker counts: `docker` = **0**, `e2e` = **4**, `slow` = **0**, `integration` = **103**. So `docker` and `slow` are **defined but unused**; the 4 deselected are exactly the 4 `e2e` tests.

**Plugins.** Present: pytest-playwright 0.9.0, pytest-base-url 2.1.0, pytest-timeout 2.4.0 (installed libs: docker 7.2.0, playwright 1.62.0). **Absent:** pytest-mock, pytest-xdist, pytest-asyncio, responses, freezegun.

**Per-directory node-id counts** (all = selected except `tests/e2e`): `tests/` root-flat **2352**; `tests/security` **411**; `tests/integration` **240**; `tests/docker` **143** (NOT `docker`-marked → runs by default); `tests/tools` **122**; `tests/workspace` **99**; `tests/docker_integration` **61**; `tests/trust` **51**; `tests/presenter` **48**; `tests/web_ui` **23**; `tests/session` **15**; `tests/web_ui/backend` **11**; `tests/e2e` **4** (→0 selected).

**Untracked test file.** `git_read status` shows exactly one untracked test file: `tests/test_container_machine_smoke.py` (`??`). Contribution **+9** node ids; carries **no** `docker`/`e2e` markers (`-m "docker or e2e"` = 0, `-m "not docker and not e2e"` = 9) → fully in the selected set.

**Blind spots.**
1. **Out-of-tree tests invisible to the default run.** `testpaths=["tests"]` ⇒ a bare `pytest` collects none of: `web_ui/backend/tests/` (10 test files + its own conftest), `tools/test_audit.py`, `mcp_examples/test_client.py`, `mcp_examples/test_with_agent_mcp_client.py` — **13 real test files** — plus 3 scratch files under `.tm-fix-scratch/**`. They pass only when invoked by explicit path, so a green `pytest` does not cover the `web_ui` backend test package (where router/permission tests live).
2. **`docker` + `slow` markers are dead** (0 nodes), while `tests/docker/` (143 nodes) — by name a Docker suite — is **unmarked** and runs in every default run (against a faked client only in the `docker_integration` subset).
3. **Docker isolation is faked at the unit layer.** Only `tests/docker_integration` (61, faked client) touches the container path; there is **no live-daemon test** in the selected set (the `docker` marker would gate one, but no node carries it). → ties to Section 5 B1.

## Checkpoint — after Phases 5+6

- **Phase 5 done:** 7 boundary classes mapped; **14 boundaries — 8 HARDENED, 5 ASSUMED, 1 MIXED**. Highest-blast ASSUMED: host-shell surface (`host_bash_tool.py:290`, only `shell=True` in product code), LLM-provider live contract, network-ingress posture (CORS `*` + dependency-free routers).
- **Phase 6 done:** 3580 collected / 3576 selected / 4 deselected; `docker`+`slow` markers unused; one untracked test file (`tests/test_container_machine_smoke.py`, +9); 13 real test files sit OUTSIDE `testpaths`.
- **Most surprising blind spot:** the `web_ui/backend/tests/` package (router/permission tests + a second conftest) is never collected by a default `pytest`, so "suite green" ≠ "backend tested".
- **Edits this turn:** ZERO outside this audit doc — one append (Sections 5-6 + this checkpoint) at the document tail, plus two deferred questions parked in `### Open questions (deferred)`. No pre-existing line altered.
- **ready for Phase 7**


---

## Section 7 — Doc / code drift

Method: each load-bearing doc claim re-checked against the current tree (`grep -n` / `sed -n`), plus the two Phase-1 carried anchors (`### Phase 7 drift candidate`, L250-252). Verdicts: **WRONG** = anchor points at the wrong line; **IMPRECISE** = right area, wrong span; **STALE** = true when written, false now; **CONTRADICTED** = doc says X, code says not-X; **SELF-DECLARED OBSOLETE**.

**9 findings — 3 HIGH, 2 MED, 4 LOW.**

| # | Doc claim (file:line) | Code truth | Verdict | Sev |
|---|---|---|---|---|
| D1 | `container_controls_findings.md:16` — container-record router at `server.py:2682`, no `dependencies=` (carried anchor) | `web_ui/backend/server.py:2682` = `vault_repair_router`; `container_record_router` is at **2683**; all 12 `include_router` calls L2671-2683 carry no `dependencies=` (substance stands) | **WRONG** (off-by-one) | MED |
| D2 | `container_controls_findings.md:46` — `workspace_container_delete` ends ≈ `server.py:3316`; fallback anchor `# Container logs:` @3319 | `def workspace_container_delete` @ **3289**, body ends **≈3333** (≈17 L off); `# Container logs:` is @ **3631** (**312 L off**) | **IMPRECISE** (end) + **WRONG** (fallback) | LOW |
| D3 | `docs/container_subsystem_target_architecture.md:181-192` §9 marks items 3-10 "planned" | items **3-9 shipped**: #3 `thoughtmachine/container_record/`; #4 `security/security_gate.py:resolve_container_config:1001`; #5 `security/admission_gate.py:321`; #6 `container_record/drift.py`; #7 `container_record/models.py:183`; #8 `container_record/lifecycle_policy.py:114`; #9 `infra/container_manager.py:302`; **only #10** (registry dual-write/retire-legacy) unimplemented | **STALE** | **HIGH** |
| D4 | `docs/security_layer.md` H1 "# ⚠️ OBSOLETE — pre-migration Qt architecture" | file present, listed in Section 0b | **SELF-DECLARED OBSOLETE** | LOW |
| D5 | ~~Section 0b intro: counts "supersede the '~58 / 20' estimates in Section 0's scope caveat"~~ **RESOLVED — Section 0b intro reworded** | Section 0 `### Index gaps / UNKNOWNs` scope caveat states **50** docs / **21** project dirs; the "~58/20" figure never existed there. Section 0b now reads "agree with" Section 0's 50 / 21 | **RESOLVED** (internal) | LOW |
| D6 | `docs/testing/test_inventory.md` "Total test files: 52, Total test functions: 752"; 0 refs to container_record/admission/network_mode/container_machine | Phase 6a **MEASURED 3580 collected / 3576 selected**; `grep -c` = 0 for all four subsystem terms | **STALE** | **HIGH** |
| D7 | `docs/CAPABILITIES.md:126` — split grains `git_read`/`git_write` **removed** and `allow_host_resources` **removed** | `agent/config/schema_manifest.json` STILL declares `git_read`,`git_write`,`allow_host_resources` for `sessions/*/config.json`; `agent/config/session_config.py:105-108 drop_legacy_allow_host_resources` drops the flag only at load | **CONTRADICTED** | **HIGH** |
| D8 | `docs/CAPABILITIES.md:49-50` — sandbox config "resolved by a single source of truth, then enforced at container creation" | true at **create**; the **reuse** ladder never re-applies isolation (findings B-07; R1) | **IMPRECISE** | MED |
| D9 | `docs/CAPABILITIES.md` cites `tools/docker_executor.py` | `docker_executor.py` lives at repo **root**, not under `tools/` | **WRONG** (path) | LOW |

**Carried anchors resolved (Phase-1 `### Phase 7 drift candidate`, L250-252):** the `server.py:2682` router anchor is confirmed **WRONG** (D1); the `workspace_container_delete` end/anchor is **IMPRECISE/WRONG** (D2). Both belong in `container_controls_findings.md`, which is an append-only session log — correcting that source is a future edit pass, not this audit.

---

## Section 8 — Risk register

Method: ranked by **impact × likelihood** (High=3 / Med=2 / Low=1); the `#` column shows the raw score. Each row is a falsifiable claim with a `file:line`/test mitigation cite. **F1-F4 (Phase 8 structural findings) map as: F1=R3, F2=R4, F3=R2, F4=R7.** Ordering rule: a **silent permissive default** (raises no error) outranks an equivalent **verbose** failure — an invisible gap outranks a loud one.

| # (score) | Risk | Description | Why it matters | Current mitigation (file:line / test) | What moves it off | Impact | Likelihood |
|---|---|---|---|---|---|---|---|
| R1 (9) | **Reuse ladder never re-applies isolation** (B-07; Section 5) | `infra/container_manager.py` reuse path `_reuse_container:3272` returns an existing container without reconciling hardening to `resolve_container_config` | "enforced at create" is false for every reuse; a weakly-hardened container keeps that posture for life | admission gate at **create** only (`security/admission_gate.py:321`); no reuse-time admission; no reuse-isolation test in `tests/` | re-run admission / `resolve_container_config` on every reuse (or delete reuse) | High | High |
| R2 (9) | **F3 — vault schema-manifest gap** | `agent/config/schema_manifest.json` = 99 fields / 33 entries; `AgentConfig` (`agent/config/models.py`) = 47 fields → **10 undeclared**, incl security-relevant **`use_container_registry`** (`:141`) and **`use_workspace_lifecycle_manager`** (`:137`) (both default False) | the two fields that select the *entire* container subsystem are unvalidated → a typo silently leaves the legacy path on | undeclared vault fields get **WARNING only**, never validated/repaired (`agent/config/vault_drift.py:344-362`) | declare the 10 fields; validate manifest ⊆ model | High | High |
| R3 (6) | **F1 — four container-create sites, no shared primitive** | `infra/container_registry.py:156 create_hardened_container` is documented "THE single hardened create path" (`run()` @239) but 3 raw `client.containers.run` sites bypass it: `infra/container_manager.py:1463`, `infra/resource_container_manager.py:1213`, root `docker_executor.py:790` | a hardening change to the "single path" reaches 1 of 4 sites | branch parity (registry ON → sites 2/3 delegate to site 1), documented Section 5 L307 | converge all four `run()` calls onto `create_hardened_container` | High | Med |
| R4 (6) | **F2 — invisible legacy/new branch, same type** | resource containers are created via the registry facade OR a legacy RAW create (`resource_container_manager.py:932 _registry_active` fallback); legacy ones "never swept or integrity-checked by the legacy executor paths" (`:954`); `docker_executor.py:292,374` legacy `agent-exec-<hash>` → `container_type "user"` | two containers of the *same declared type* have different integrity guarantees; the branch is invisible to the operator | registry facade registers when `_registry_active`; boot drift scan (`web_ui/backend/server.py _BOOT_DRIFT_SCAN_LIMIT=100`) | retire the legacy raw create (or give it a distinct type + sweep it) | High | Med |
| R5 (6) | host-shell surface | `tools/host_bash_tool.py:290 subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)` with full ambient env — the only `shell=True` in product code | a host shell with the operator's env is the widest blast-radius I/O in the tree | HOST gate (infra-touching tools get the host gate, not the sandbox) per `v3_roadmap.md:67-71` | drop `shell=True` (argv list) and/or scrub env | High | Med |
| R6 (6) | network ingress: wildcard CORS + dependency-free routers | `web_ui/backend/server.py:871 allow_origins=["*"]` (CORSMiddleware import `:867`); 12 routers registered L2671-2683 with no `dependencies=` | any origin may reach a localhost backend; routers carry no per-router auth dependency | none at middleware/router layer (any auth is in-handler) | restrict origins; add router-level dependencies | Med | High |
| R7 (6) | **F4 — one worktree-refusal message for ≥3 causes** | `tools/git_write_tool.py:590-597` emits a single "operator-managed git worktree" message whenever `_is_operator_managed_worktree()` and `_unprotected_branch_agent_commit_allowed()` (`:293-330`) is false — but the helper is False for ≥3 distinct reasons (git perm not write-capable; container git exec inactive; container-mandatory resolution failed) + protected-branch | accurate only for the protected-branch case; misdirects operators debugging permission/container failures | message text only | distinct messages per cause | Med | High |
| R8 (6) | target-arch §9 stale (D3) | `docs/container_subsystem_target_architecture.md:181-192` marks items 3-10 "planned"; 7 of 8 are shipped | the "build order" doc understates what exists → duplicate/over-planned work | this audit Section 7 D3 | update the §9 status column | Med | High |
| R9 (6) | out-of-tree backend tests never collected | `pyproject.toml:51 testpaths=["tests"]` excludes `web_ui/backend/tests/` (10 test files + conftest) | "suite green" ≠ "backend tested"; router/permission tests silently skipped | explicit-path invocation only; Section 6 blind spot 1 | add the package to `testpaths` (or fold into `tests/`) | Med | High |
| R10 (6) | fail-open Site C — no ownership check | `tools/workspace/worker_execution.py:338-366` executes without an ownership check (findings §fail-open) | a worker/container path that skips the ownership gate | findings-doc only | add the ownership check at Site C | High | Med |
| R11 (4) | four status surfaces, two truths (B-06) | container state reported from four surfaces that can disagree | operator acts on stale/wrong state | partial UI/anchor only (findings B-06) | one status source (the record) | Med | Med |
| R12 (4) | Windows `user=` fallback missing at 2 of 4 sites | Windows-safe `user=(host_user() or "0:0")` present at `container_registry.py:251` and `resource_container_manager.py:1225`, MISSING at `infra/container_manager.py:1474` and `docker_executor.py:799` (`user=host_user()`) | on Windows `host_user()` may be None → create fails at exactly 2 sites | two of four sites guard it | add the `or "0:0"` fallback at both missing sites | Med | Med |
| R13 (4) | no lockfile / no upper bounds | `requirements.txt` = 25 lower-bounds only; no lockfile (Section 0 item 11) | supply-chain drift; a transitive bump can break a pinned-range install | `requirements-dev.txt:1` = `-r requirements.txt` | add a lockfile / upper bounds | Med | Med |
| R14 (4) | two permission authorities | `security/` (gate+admission) and `thoughtmachine/security.py`+`permission_store.py` both own permission logic; `is_allowed:1000` vs `get_effective_permissions:763` (Q1 resolved: package-scope duplication) | two places to reason about for any permission change | Q1 (Section 3.6) documents the split | fold to one authority | Med | Med |
| R15 (4) | UID / tmpfs / `.git` shadow (B-04) | resource-container UID/tmpfs/`.git` shadowing (findings, CONFIRMED) | a worker can shadow `.git` / run as the wrong UID inside a resource container | findings-doc B-04 only | mirror `.git` protection into resource containers | Med | Med |
| R16 (3) | `docker`+`slow` markers dead while `tests/docker/` runs | `docker` = 0 nodes, `slow` = 0 nodes; `tests/docker/` (143 nodes) is unmarked → runs in every default run (Section 6 blind spot 2) | a by-name "Docker suite" gives false confidence / accidental load | pyproject markers `:54` | mark `tests/docker/` or rename | Low | High |
| R17 (3) | frontend JSX vs docs "TS" | `web_ui/frontend/src` is JSX; docs/xray/`types/` frame it as TS (Section 1d item 6) | misleads anyone reading docs before code | Section 1d item 6 records it | correct the docs | Low | High |
| R18 (2) | note write fail-closed drops operator notes | `infra/container_manager.py:857-868 _write_note` refuses if no record id (sidecar NEVER written) | an operator note can be silently dropped when the record id is missing | fail-closed (by design) | backfill record id / surface the refusal in UI | Med | Low |
| R19 (2) | boot drift scan capped at 100 | `web_ui/backend/server.py _BOOT_DRIFT_SCAN_LIMIT=100` | >100 containers → drift beyond the cap goes unscanned at boot | constant present | raise/paginate the scan | Med | Low |
| R20 (2) | orphan manifest field | `schema_manifest.json` declares `agent_soft_budget_seconds` for agent_config.json but it is not on the model (F3) | a validated-but-unused field; a drift signal | none | drop it or add it to the model | Low | Med |

**Operator weighting.** The operator will **under-weight** the risks that raise *no error* — precisely the ones ranked highest here: **R1** (reuse silently keeps weak isolation), **R2/F3** (an undeclared field is a warning, not a failure), **R4/F2** (the legacy branch is invisible), **R6** (dependency-free routers fail silently open), **R18** (a dropped note raises nothing). They will **over-weight** the *loud but cosmetic* ones — **R7/F4** (a single misleading message: annoying, not dangerous) and the pure doc-drift rows **R8/R17**. Per the ordering rule, the permissive silent default outranks the verbose error: fix **R1 / R2 / R4** before **R7**.

**F1-F4 (Phase 8 structural) coverage.** All four appear (F1=R3, F2=R4, F3=R2, F4=R7); **none is retired by current code.**


---

## Checkpoint — after Phases 7+8

- **Sections written (append-only):** `## Section 7 — Doc / code drift` (9 findings) and `## Section 8 — Risk register` (20 risks). Doc 622 → 697 L; no pre-existing line altered (one insert added deferred items 7-11).
- **Drift: 9 findings (3 HIGH, 2 MED, 4 LOW).** HIGH = D3 (target-arch §9 stale), D6 (`docs/testing/test_inventory.md` stale), D7 (`docs/CAPABILITIES.md:126` removal claims contradicted by `schema_manifest.json`).
- **Carried anchors:** `server.py:2682` container-record router = **WRONG** (actual 2683); `workspace_container_delete` end/anchor = **IMPRECISE/WRONG** (ends ≈3333; `# Container logs:` @3631).
- **§9 verdict:** 7 of 8 "planned" items (3-9) are shipped; only #10 (registry dual-write / retire-legacy) unimplemented — the table is stale by ~2 waves.
- **Top-5 risks (impact×likelihood):** R1 reuse never re-applies isolation; R2/F3 manifest gap; R3/F1 four create sites; R4/F2 invisible legacy branch; R5 host_bash `shell=True`.
- **F3 gap:** **10** undeclared `AgentConfig` fields; security-relevant = `use_container_registry`, `use_workspace_lifecycle_manager`.
- **UNKNOWNs = 7 (unchanged);** deferred `### Open questions` items **6 → 11**.
- **ready for Phase 9** — no BLOCKER.

---

## Section 9 — Migration plan

*Plan sketch only — no dates, no estimates, no code.* This section gives an **ordering + checkability contract** for the Section-8 risks that map onto the Section 2e / Section 3 deduplication opportunities. Each opportunity records: **(a)** first adopter + caller order; **(b)** revert points between callers; **(c)** the legacy-retirement criterion (a command you can run); **(d)** the byte-identical-behaviour claim + the test `file:line` that catches a regression.

### 9.0 Prioritisation (from Section 8 scores)

| Order | Opportunity (Section 2e / 3) | Section-8 risks moved | Score(s) |
|---|---|---|---|
| **M1** | container create + exec unification | R3/F1 (four create sites), R1 (reuse), R4/F2 (invisible legacy branch) | 6 / 9 / 6 |
| **M2** | security / permissions — two authorities | R14 | 4 |
| **M3** | session / config defaults | R2/F3 | 9 |
| **M4** | vault lifecycle | ~none ranked High | – |
| **M5** | WS-bridge overlap | R11 (four status surfaces) | 4 |
| **M6** | tool dispatch registries | – | – |
| **M7** | worker infra | Q3 (2 registries / 3 staleness checks / 2 state machines) | – |
| **M8** | launcher scripts | – | – |

M1 ranks first not because its raw score is highest but because it is the only opportunity that closes **three** top-10 risks at once (and it is a precondition for retiring the `use_container_registry` flag). M3 carries the single highest score (9) but closes exactly one risk family.

### M1 — container create + exec unification (highest)

**(a) First adopter + caller order.** First adopter = `infra/container_registry.py:156 create_hardened_container` / `run()@239` — already the documented "single path". Converge callers in blast-radius order:
1. **site 2** — `infra/container_manager.py:1463` (inside `_run_container:1456`; the 72-method class — largest blast).
2. **site 3** — `infra/resource_container_manager.py:1213` (`_create_resource_container:1037`; registry-ON already delegates to site 1 at `:1174`).
3. **site 4** — root `docker_executor.py:790` (`DockerExecutor._ensure_container`).

Then unify the **three exec paths** (`docker_executor.py:866`, `container_manager.py:1788`, `resource_container_manager.py:1504`) onto one Thread/Queue wrapper (Section 3.10).

**(b) Revert points.** Each site is one `client.containers.run(...)` call inside one method → revert = restore that single call. The registry-ON delegation branch (`container_manager.py:1580-1616`; `resource_container_manager.py:1174`) is the natural per-site toggle.

**(c) Legacy-retirement criterion (checkable).** `grep -n "containers.run(" --include=*.py` over the product returns **exactly ONE hit** (in `create_hardened_container`); all four legacy sites gone. Exclusions: the smoke harness `live_smoke_docker.py:233/244` (out-of-product) and the `tests/docker/test_*.py` create sites.

**(d) Byte-identical behaviour.** The four sites already share the hardening constants `HARDENED_CAP_DROP=["ALL"]` / `HARDENED_SECURITY_OPT=["no-new-privileges:true"]` / `HARDENED_READ_ONLY=True` (`agent/config/defaults.py:161-163`). The divergent values that must be **parameterised, not dropped**: **D1** `user`, **D2** `oom_score_adj`, **D3** resource `read_only` / rw-git-mount, **D4** literal-vs-constant. Regression tests: `tests/docker_integration/conftest.py` (faked docker — `patch.object(docker,"from_env")` + `make_container`), `tests/test_container_user_and_git_ownership.py`, `tests/test_container_ownership_user_fallback.py` (3 tests: windows None host_user→0:0, posix pass-through, registry None→0:0), `tests/test_container_exec_drift.py`, `tests/test_container_machine_smoke.py` (+9), `tests/test_g9_real_container_hardening` (self-skips via `require_docker`).

### M2 — security / permissions two authorities (R14)

**(a)** First adopter = `security/security_gate.py::get_effective_permissions:763` (computes the set) **stays**; fold `thoughtmachine/security.py::is_allowed:1000` semantics behind it; dedupe the LITERAL resource-catalog copy `security/resource_catalog.py` vs `agent/config/resource_catalog.py`; drop the third wrap `web_ui/backend/workspace_routes.py:786`.
**(b)** Revert point: `security_gate.py:33` imports `thoughtmachine.security` (upstream) — cut/restore there.
**(c)** Retirement: one resource-catalog file; no `is_allowed` callers outside the gate.
**(d)** Regression: `tests/security/test_*` (411 nodes), `tests/test_api_key_hygiene.py`.

### M3 — session / config defaults (R2/F3)

**(a)** First adopter = `agent/config/defaults.py` (single constant table: `RESOURCE_MEM_LIMIT:132`, `DEFAULT_MEM_LIMIT:226`, `CPU_QUOTA:227`, `HARDENED_CAP_DROP:161`, `SESSION_PERMS:256`); converge 8 resolvers (`loader.py:87/278/287`, `config_manager.py:37` (agent) & web_ui `config_manager.py:161/520/536/803`, `vault.py:347`).
**(b)** Revert point: per-resolver shim delegating to the table.
**(c)** Retirement criterion: `schema_manifest.json` ⊆ `AgentConfig` (47 fields) → fixes the 10 undeclared, incl `use_container_registry:141` and `use_workspace_lifecycle_manager:137`.
**(d)** Regression: `agent/config/vault_drift.py:344-362` warns; config tests.

### M4 — vault lifecycle (~no ranked risk)

**(a)** Owners: seed `vault.py:56/126/157/347`, detect `vault_drift.py:101/129`, repair `vault_repair.py:151/187/1507`, GC `vault_gc.py:757`, HTTP `vault_repair_routes.py:54/96`. Drift classify/locate duplicated in `scripts/vault_permission_cleanup_dryrun.py:148/198`; `scripts/migrate_vault.py:17` shadows `vault_root()`.
**(b)** Revert: scripts keep their local copies.
**(c)** Retirement: scripts import from the library, no redefinition.
**(d)** Regression: `tests/test_vault_gc.py`.

### M5 — WS-bridge overlap (R11)

**(a)** `bridge.py broadcast_rename:971` / `broadcast_logging_config:976` duplicate `event_forwarder.py:101/:113`.
**(c)** Retirement: one broadcast helper. **(d)** Regression: `tests/web_ui/*`, `tests/test_per_worker_eventbus_bridge.py`, `tests/integration/test_session_loaded_contract.py`.

### M6 — tool dispatch registries

Three registries: `tools/__init__.py:62/279`, `worker_thread.py:3238/3243`, `tool_executor.py:119/145/282`.

### M7 — worker infra (Q3 — two parallel mechanisms)

Two registries (`worker_manager.py:253/265/277/288` vs `worker_registry.py:76/84/91/107`); three staleness checks (`worker_manager.py:156/166/642/103` vs `worker_lifecycle.py:130/143/148/199` vs `worker_query.py:237`); two state machines (`workspace_lifecycle_manager.py:201/385/582` vs `worker_thread.py:569/807`). Regression: `tests/tools/test_worker_registry.py`, `tests/test_worker_timeout_audit.py`.

### M8 — launcher scripts

`start_thoughtmachine.sh:94/99` byte-identical `doctor()` / `json_get()` with `install.sh:26/31`; `start_windows.py` re-implements.

**Not opportunities (no-op).** Section 3.3 (LLM providers) and Section 3.4 (git tools) are **single-owner** in the current tree → no unification needed; record as no-op.

### Container flag-removal preconditions

For `use_container_registry=true` to become the ONLY path (and the flag removed):

**Already satisfied:** (1) registry-ON branch parity — `is_registry_active` (`registry_wiring.py:5,57-58`) makes sites 2/3 delegate to site 1 (`container_manager.py:1580-1616`; `resource_container_manager.py:1174`); (2) `create_hardened_container` exists as the documented single path (`container_registry.py:156`); (3) 4 sites value-equal on cap_drop / security_opt / read_only constants (`defaults.py:161-163`); (4) registry activation already gated on a live daemon (fail-closed-ish: `is_registry_active` returns False when docker unreachable, `registry_wiring.py:57-61`).

**Open:** **(O1)** legacy RAW resource create must be retired — `resource_container_manager.py:932 _registry_active` fallback + `:954` (legacy containers "never swept or integrity-checked"); **(O2)** legacy `agent-exec-<hash>` path `docker_executor.py:292,374` → `container_type "user"` must converge; **(O3)** the reuse ladder never re-applies isolation (R1, `container_manager.py:_reuse_container:3272`) — must re-run admission / `resolve_container_config` on every reuse; **(O4)** no test pins branch parity / no live-daemon test (docker faked; `docker` marker = 0 nodes); **(O5)** `user=` Windows fallback missing at 2 sites (`container_manager.py:1474`, `docker_executor.py:799`) vs guarded at `container_registry.py:251` / `resource_container_manager.py:1225` (R12); **(O6)** exec-path divergence to reconcile — env injection `merge_container_identity_env` present in container_manager + resource, NOT docker_executor (`container_env.py:22`); cmd form `list(cmd)` argv (resource) vs `["/bin/sh","-c",command]` elsewhere; **(O7)** decide no-daemon semantics after flag removal (currently silent fallback → must become fail-closed).

**UNKNOWN:** **(U-a)** whether the WLM path can ever reach a `run()` outside the four (needs a call-graph trace of `WorkerSupervisor.process_query`); **(U-b)** whether shipped vault configs still set the flag off / use legacy (deferred Q11 — the vault config values that disable the registry).

### The one subsystem to fix first

If the migration is to be staged, M1 — **container create/exec unification** — is the choice: it is the only opportunity that closes three of the top risks at once (**R3/F1** four create sites, **R1** reuse never re-applies isolation, **R4/F2** invisible legacy branch; scores 6 / 9 / 6) and is a precondition for retiring the `use_container_registry` flag. Every other unification (**security R14**, **config R2/F3**, **bridge R11**) closes at most one risk, and none is a silent-permissive default of equal rank. Per the Section-8 operator-weighting rule: **fix R1 / R2 / R4 before R7.**

---

## Section 10 — Open questions

Three buckets, then the requester-question answers, then the questions this audit itself adds.

### (a) BLOCKING for Phase C

**None outright.** The nearest-to-blocking family is the **F2/F4 invisible-branch + reuse-isolation** cluster (**R1 / R4**) — Phase C container work builds directly on the create path, so this should be parked as **"resolve-first"** rather than assumed. No single open question stops Phase C from starting.

### (b) Infra-engineer questions (need ops / a live daemon)

1. **Live-daemon reuse-isolation test** — is the reuse ladder (`container_manager.py:_reuse_container:3272`) ever re-applying hardening, or must admission be re-run on every reuse? (R1; needs a real docker daemon.)
2. **No-daemon semantics after flag removal** — once the legacy path is retired, what should happen when docker is unreachable: fail-closed, or an explicit degraded mode? (O7.)
3. **Deployed vault flag values** — does any deployed vault set `use_container_registry` / `use_workspace_lifecycle_manager` (the two security-relevant undeclared `AgentConfig` fields, R2/F3)? This decides whether the flag can be removed safely (deferred Q11).
4. **Network-ingress posture** — decide the intended bind + allowed origins: CORS is `allow_origins=["*"]` (`server.py:869-871`) with dependency-free routers (`server.py:2671-2683`).
5. **Firewall / egress posture** — there is **no** in-repo firewall (grep `firewall|iptables|ufw|nftables` = **0 hits**, Q6); egress relies on the docker `network_mode` mapping (B9) plus the external host. Confirm the host is the intended control point.

### (c) Known unknowns for the next engineer (Q7 — friend-bug classes invisible to the unit suite)

1. **Reuse-ladder isolation never re-applied** — R1/B1; only a live run reveals it.
2. **Legacy-vs-registry branch divergence** — R4/F2; the branch is invisible and docker is faked.
3. **Windows / macOS host-identity & path branches** — no test monkeypatches `os.name='nt'` / `platform.system`→Windows (Q3); Windows paths are ASSUMED and unexercised.
4. **Live LLM-provider wire** — B13; providers are mocked, so streaming / tool-call format / error taxonomy are unproven.
5. **Host-shell ambient-env exec** — B3; the gate is tested but the execution surface (full ambient env, no metachar scan) is not.
6. **CORS / router auth posture** — B10/R6.
7. **Out-of-tree `web_ui/backend/tests/`** — never collected by a default `pytest` (R9).
8. **Multi-cause diagnostic conflation** — F4 family / Q2: predicate functions that collapse ≥3 causes into one static message.
9. **Drift-driven automatic recreate & GC timing** — Section 2d UNKNOWN.
10. **`docker` / `slow` markers dead** — so the container suite runs unmarked in every default run (R16).

### Requester questions (answer-or-park)

The executing worker retained search findings for **five** of the requester's questions (labelled **Q2, Q3, Q5, Q6, Q7**). The exact text of **Q1** and **Q4** was not retained by the executing worker; both have since been **reconstructed and answered** below (Q1 from the Section 4/4e god-object evidence + the NFC/NFD git-tool fix; Q4 from the Section 8/R2 manifest-vs-model counts).

- **Q2 — F4-class: a bool predicate with ≥3 causes collapsing to one static message.** AST scan for functions with ≥3 `return False` (findings): `logs.py:127 _matches` (8), `resource_container_manager.py:531 _ensure_resource_image` (7), `agent.py:360 _can_hot_swap` (7), `worker_execution.py:101 _is_worker_owned_container` (6), `workspace_lifecycle_manager.py:154 _is_worker_owned_container` (6), `server.py:3859 _build_frontend` (5), `server.py:3834 _session_has_conversation` (4), `git_write_tool.py:293 _unprotected_branch_agent_commit_allowed` (4 = THE F4/R7), `container_manager.py:2228 _heal_eligible` (4), `vault_drift.py:466 _check_field` (4), `worker_query.py:114 is_timeout_envelope` (3), `security_gate.py:1096 check_atomic_operation` (3), `container_manager.py:2589 _exceeds_disk_quota` (3), `controller/__init__.py:425 restart_agent` (3). **Two CONFIRMED F4-shaped:** (1) `_is_worker_owned_container` (`worker_execution.py:101` & `workspace_lifecycle_manager.py:154`) — 6 causes → one message "container X is not worker-owned — skipping" (`worker_execution.py:320-324`); (2) `_ensure_resource_image` (`resource_container_manager.py:531`) — 7 causes → a single `raise RuntimeError("Resource image '...' is not available (auto-build failed or Docker unreachable)")` (`:988`/`:1369`). **Counter-examples (per-cause messages, NOT F4):** `check_atomic_operation`, `vault_drift._check_field`. `_flag_gate_error()` (`git_write_tool.py:89`) is a single-cause gate.
- **Q3 — host-identity.** `host_user()` defined at `agent/config/defaults.py:180` (`os.getuid`/`os.getgid` via `_host_ids:166`; `os.name=="nt"`→None at `:175`). Consumers: the 4 create sites (`container_registry.py:251 or "0:0"`, `container_manager.py:1474` bare, `resource_container_manager.py:1225 or "0:0"`, `docker_executor.py:799` bare) + a **5th**, `thoughtmachine/container_record/hook.py:150` (`Config User or host_user()`), and `HARDENED_USER=host_user()` (`defaults.py:215`); drift comparators `container_manager.py:1872/2425 _user_drift_axis`. HOME: ~40 `Path.home()`/`expanduser` sites, POSIX-shaped (`loader.py:15`, `injector.py:62`, `state_bridge.py:27`, `container_manager.py:705/710`, `resource_container_manager.py:151/152/2155`, `web_ui server.py:300`, `bridge.py:1169`) — tests redirect `HOME`/`USERPROFILE`/`HOMEDRIVE`/`HOMEPATH` via the root conftest. Case: `workspace_registry.py:80/:381`, `workspace_capabilities.py:210`. `os.sep`: `admission_gate.py:317`. Drive letters: `server.py:2696/2712/2746`, `workspace_routes.py:91/123`, `tools/base.py:389`, `thoughtmachine/security.py:424/528`. Locking: `session/lock.py:31` (Windows `msvcrt`), `container_record/storage.py:31-33/234` (`fcntl`, POSIX-only). Atomic-write retry: `workspace_routes.py:266/307`, `config_manager.py:473/988`. **TEST GAP:** NO test monkeypatches `os.name='nt'` / `platform.system`→Windows → Windows paths are ASSUMED/unexercised.
- **Q5 — invisible flags.** `AgentConfig` bool-ish fields (`models.py`): `worker_mode:74`, `stop_check:102`, `turn_monitor_enabled:107`, `enable_logging:109`, `enable_file_logging:112`, `jsonl_format:113`, `rag_enabled:118`, `kb_enabled:125`, `time_monitor_enabled:131`, `use_workspace_lifecycle_manager:137`, `use_container_registry:141`. `SessionConfig` booleans: ONLY `use_workspace_lifecycle_manager:186`, `use_container_registry:190`. Flags at **create dispatch = exactly two**: `is_container_registry_enabled` (`container_registry.py:972-976` reads `use_container_registry`; `registry_wiring.py:38/55`) + `_wlm_flag_enabled` (`workspace_lifecycle_manager.py:195-197/231`; registry `_feature_flag_check` `container_registry.py:302-304`). Extra: `container_manager.py:1375` max-container limit is **also** gated on `is_registry_active` (the flag changes the capacity ceiling). Both create-dispatch flags are UNDOCUMENTED (2 of the 10 undeclared manifest fields, F3). **No third hidden create-dispatch flag.**
- **Q6 — host_bash / host git / firewall.** host_bash: `HostBashTool` registered `tools/__init__.py:205-208`; gated by `host_resource_policy.py` + the `host_bash` grain (default `"banned"` `defaults.py:262`; catalog `resource_catalog.py:82/107/258`); exec `host_bash_tool.py:290 subprocess.run(cmd, shell=True, ...)` = the ONLY `shell=True` in product. The HARDENED runner `security/sandboxed_execution.py` consumers = ONLY `git_info_tool.py` + `mcp_server_connect.py` (+itself). **BYPASS sites** (subprocess not via `SandboxedExecution`): `check_system.py:939`, `worker_execution.py:366`, `web_ui/backend/server.py:224`, `server.py:3872` (npm run build), `docker_executor.py:1008`, `doctor.py:47`, `mcp_client.py:226` + `mcp_client_new.py:219` — all argv lists, none `shell=True`. **FIREWALL: ZERO in-repo** (see (b)5).
- **Q7 —** see bucket **(c)** above.
- **Q1 — `web_ui/backend/bridge.py`: god-object verdict + the "the NFC/NFD fix touched it" premise.** **Verdict: CONFIRMED god object** (Section 4/4e) — `bridge.py` = **2612 LOC** (`wc -l`; Section 4/4e cites 2613), one class `WebAgentBridge` (L275) with **60 methods / 2329 cLOC**, exceeding ≥2 axes (methods ≥25 + LOC ≥800); 13 commits/30d. **Sub-responsibilities (spans, all `web_ui/backend/bridge.py`):** (1) module helpers L159-273 — worker-instance labels `_worker_instance_label:159`/`_worker_instance_parts:169`; workspace-id resolve cache `_build_workspace_id_cache:182`/`_resolve_workspace_id:202`/`_validate_workspace_id:234`; async-job probe `_worker_is_running_async_job:252`. (2) security-event subscription L374-423 (`_subscribe_to_security_events:374`, `_unsubscribe_security_events:573`). (3) worker-event subscriptions + per-worker bus L425-958 (`_subscribe_to_worker_events:425`, `_discover_existing_workers:506`, `_on_worker_token_warning:533`, `_unsubscribe_worker_events:585`, `_on_worker_spawned:613`, `_subscribe_to_worker_bus:695`, `_unsubscribe_worker_bus:871`, `_on_worker_completed:896`, `_on_worker_error:909`, `_forward_worker_event:922`). (4) global tab registry + broadcast L962-978 (`register:962`, `unregister:966`, `broadcast_rename:971`, `broadcast_logging_config:976`). (5) public state/query API + event buffer/replay L983-1078 (properties `session:984`…`is_processing:1015`; `set_event_callback:1020`, `remove_event_callback:1030`, `_buffer_worker_event:1034`, `_flush_worker_event_buffer:1046`, `set_controller:1070`). (6) lifecycle L1081-1362 (`_build_global_agent_config:1081`, `start:1104`, `continue_session:1241`, `pause:1296`, `resume:1325`, `stop:1350`). (7) query + controller restart + container re-sync L1366-1446 (`get_conversation:1366`, `get_config:1373`, `_restart_controller:1381`, `_maybe_re_sync_container:1417`). (8) config apply L1447-1599 (`apply_config:1447`, `apply_config_queued:1581`). (9) session persistence L1602-2193 (`list_sessions:1602`, `_refresh_persistence_dump:1612`, `_config_dump_for_persistence:1649`, `save_session:1662`, `_normalize_for_frontend:1732`, `create_session:1844`, `load_session:1873`, `load_more_messages:2087`, `delete_session:2092`, `rename_session:2096`, `get_open_sessions:2110`, `save_open_session:2114`, `remove_open_session:2130`, `close_session:2143`). (10) worker-context persistence + controller event mapping + run loop L2196-2612 (`_load_worker_contexts:2196`, `resume_worker:2244`, `clear_loaded_session:2257`, `_on_controller_event:2266`, `_map_and_emit:2365`, `_run_loop:2529`). **Premise correction — the "NFC/NFD fix" did NOT touch `bridge.py`:** it lives in `tools/git_info_tool.py:7` (`import unicodedata`), `:58` `_nfc()` ("NFC-normalize a path; **BOTH** comparison sides use this (symmetric)"), `:60` impl, with the RED test `tests/security/test_git_status_unicode_reconciliation.py` and harness `scratch/mutate_git_info.py`. A repo-wide scan for `NFC|NFD|unicodedata` yields only git-tool/test/report hits; `bridge.py` has none (its only "normalize" is message-role normalization, `_normalize_for_frontend:1732`). So the premise is **mistaken** — this is an **OUTLIER**, not a bridge concern.
- **Q4 — schema manifest declared-field count + gap against the vault.** Declared = **99 fields across 33 file entries** (`agent/config/schema_manifest.json`); the vault-side reference model `AgentConfig` (`agent/config/models.py`) = **47 fields**; **gap = 10 undeclared `AgentConfig` fields** — incl the security-relevant `use_container_registry` (`models.py:141`) and `use_workspace_lifecycle_manager` (`models.py:137`), both default `False`. Reverse orphan: the manifest declares `agent_soft_budget_seconds`, which is not on the model (R20). **Method:** differential count of `schema_manifest.json` `files[*].fields` keys vs `AgentConfig.model_fields` keys (Section 8/R2, F3).

### Added by this audit

Questions this audit itself raises (net-new):
1. **F4 has two more instances than R7 named** — `_is_worker_owned_container` and `_ensure_resource_image` (Q2 above).
2. **`host_user()` has a 5th consumer** in `container_record/hook.py:150` (Q3 above).
3. **CORS / ingress posture is undecided** (B10/R6).
4. **No Windows-branch tests** — Windows host-identity / path behaviour is ASSUMED.
5. **`docker` / `slow` markers are dead** — the container suite runs unmarked (R16).
6. **Reuse isolation is unproven** — R1/B1; needs a live-daemon test.

---

## Section 11 — Audit metadata

Per-phase record: what was investigated, the scope boundary, and the `file:line` / span footprint.

| Phase | Investigated | Scope boundary | Span |
|---|---|---|---|
| **0** | Index build | 11 agent/doc paths; index entries skimmed unless FULL | Section 0 (L10-36) |
| **0b** | Documentation sweep | listing only (verdicts, not deep reads) | Section 0b (L37-123) |
| **1** | The map (directory / LOC tree) | top-level packages + root `docker_executor.py` | Section 1 (L124-…) |
| **2** | Container subsystem deep dive | container create / exec / reuse (`2a`-`2e`) | Section 2 (L280-…) + Checkpoint2 (L368) |
| **3** | Duplication survey | 10 domains beyond containers (3.3 LLM providers / 3.4 git tools = single-owner, no-op) | Section 3 (L380-477) |
| **4** | God objects and growth | 252 classes; 7 confirmed god objects | Section 4 (L478-534) |
| **5** | Boundary map | 14 boundaries / 7 classes (8 HARDENED, 5 ASSUMED, 1 MIXED) | Section 5 (L548-597) |
| **6** | Test suite shape | 3580 collected / 3576 selected / 4 deselected; 1 untracked file; 13 files outside `testpaths` | Section 6 (L599-619) |
| **7** | Doc / code drift | 9 findings (D1-D9); 3 HIGH, 2 MED, 4 LOW | Section 7 (L631-649) |
| **8** | Risk register | 20 risks (R1-R20); F1-F4 mapped (F1=R3, F2=R4, F3=R2, F4=R7) | Section 8 (L653-682) |
| **9** | Migration plan | ordering + checkability sketch only — no code, no dates | Section 9 (this append) |
| **10** | Open questions | 3 buckets + 5 retained requester Qs; **Q1/Q4 reconstructed + answered** (close-out) | Section 10 (this append) |

### Self-consistency check

- **Final line count:** **897 L** (re-measured after the Q1/Q4 + D5 close-out edit; 697 → 897).
- **`UNKNOWN:` total.** The doc self-reports **"UNKNOWNs = 7"**, which by design equals Section 2 scope-boundary **(3)** + Section 5 **(4)**. Section 9 adds **(U-a)/(U-b)** → **+2** `UNKNOWN:` markers → **9**. Section 10's **Q1/Q4** were once a park, but are now **answered** (see Q1/Q4 above) → they add **0** `UNKNOWN:` markers. (Separately, Section 10(c) enumerates **10** known-unknown *classes* and the requester-Qs bucket holds **5** answered items; these are not counted as `UNKNOWN:` markers, to keep the convention identical to Sections 2/5.)
- **Deferred `### Open questions` items:** **6 → 11** (unchanged by this append; the 11 were already recorded in the Phases 7+8 checkpoint).
- **Contradiction resolved (internal):** **D5** (Section 7, LOW) **was** the **only** internal contradiction — the Section 0b intro had cited a "~58 / 20" superseded estimate that does not exist in Section 0 (whose scope caveat states **50** docs / **21** project dirs). Fixed: the Section 0b intro now states it **agrees with** Section 0's 50 / 21 → the doc is self-consistent. No other internal contradiction was found.

---

## Checkpoint — after Phases 9+10

- **Sections written (append-only):** `## Section 9 — Migration plan`, `## Section 10 — Open questions`, `## Section 11 — Audit metadata`, + this checkpoint. Doc **697 → 897 L** (the +18 L TOC / status insert near the head, then this 182-L tail append; the close-out reworded D5 (Section 0b intro + Section 7 table) and replaced the Q1/Q4 park with two answers, altering no other pre-existing line).
- **Phase 9 — one-subsystem choice:** **container create/exec unification (M1)** — the only opportunity that closes three top risks at once (R3/F1 + R1 + R4/F2) and a precondition for retiring the `use_container_registry` flag. **7 OPEN preconditions (O1-O7)** for flag removal.
- **Phase 10 — question buckets:** blocking = **0**; infra-engineer = **5**; known-unknowns = **10**.
- **Metadata:** TOC + status-string + Section 11 done. **AUDIT COMPLETE.**
- **Contradiction resolved:** **D5** (internal, LOW) — the "~58 / 20" estimate once cited in Section 0b is not stated in Section 0 (actual: 50 docs / 21 dirs); Section 0b reworded to agree. Only internal contradiction.
- **`UNKNOWN:` total:** 7 → **9**; deferred `### Open questions` items = **11**.

---

## Annex - Verified anchors (Gen 16, 2026-09-18)

Gen-16 pass re-checking every load-bearing anchor cited in Sections 2, 3, 5, 6, 7, 8, 9 against the tree at **HEAD `f534d48`** (branch `fix/host-user-fallback-all-sites`). Columns: **cite** = citation as written in the audit; **claim** = the load-bearing claim; **verdict** = `verified` (anchor/claim resolves as cited) | `drift` (claim holds but the anchor moved or the value changed) | `not-found` (anchor absent) | `deferred` (not re-measured this pass); **current** = file:line at HEAD. Only `file:line` anchors were re-checked; pre-existing text is unaltered — this section is appended only.

### Section 2 — Container subsystem deep dive

| cite | claim | verdict | current |
|---|---|---|---|
| §2a `container_registry.py:239`, `container_manager.py:1463`, `resource_container_manager.py:1213`, `docker_executor.py:790` | exactly four product `containers.run()` create sites ("site 4/4") | verified | `container_registry.py:239`; `container_manager.py:1463`; `resource_container_manager.py:1213`; `docker_executor.py:790` |
| §2a `live_smoke_docker.py:233/244` | out-of-product root smoke harness = 5th/6th `.run()` site; hardcodes `user="1000:1000"`, `network_mode="none"` | verified | `live_smoke_docker.py:233/244` (root, 43197 B) |
| §2a `container_registry.py:526` ("privileged counterpart") | no product `privileged`; `pids_limit` zero hits | verified | `container_registry.py:526` (docstring only) |
| §2b `models.py:68-69`; `:137-144`; `session_config.py:186-193,325-326` | two `HOT_SWAPPABLE` flags, default `False`, on both config layers | verified | `models.py:68-69,137-141`; `session_config.py:186-190,325-326` |
| §2b `container_registry.py:974-976`; `registry_wiring.py:5,57-58`; `resource_container_manager.py:180,875`; `worker_thread.py:3595-3598` | flag read sites; `is_registry_active` False when daemon unreachable | verified | `container_registry.py:972` (`is_container_registry_enabled`); `registry_wiring.py:5,55-61`; `resource_container_manager.py:180,875`; `worker_thread.py:3595-3598` |
| §2b `container_manager.py:1580`; `resource_container_manager.py:1174` | callers fall back to legacy path; delegate only when `is_registry_active` | drift | `container_manager.py:1572-1616` (guard `:1572-1573`); `resource_container_manager.py:1183` (call `:1190`) |
| §2c `defaults.py:161-163,144-145,226-227,132-133` | hardening/limit constant set | verified | `defaults.py:161-163,144-145,226-227,132-133` |
| §2c kwarg matrix (4 create sites) | per-site hardening kwargs; D1 `user`, D2 `oom_score_adj`, D4 literals | drift | `registry:246-262`; `container_manager:1469-1486`; `resource:1218-1234`; `docker_executor:795-813` — D1 `user` now guarded at **all four** sites |
| §2d `lifecycle_policy.py:228-243,114,196`; `container_registry.py:194-198` | `restart_policy` derived at every site; resource vs persistent pick | verified | `lifecycle_policy.py:228-243,114,196`; `container_registry.py:194-198` |
| §2d `drift.py:99-100,173,326`; `container_manager.py:2162-2222,2131,2169,2473` | drift axes; `restart_policy` checked via separate path | verified | `drift.py:99-100,173,326`; `container_manager.py:2162-2222,2131,2169,2473` |
| §2d GC sweeps `container_manager.py:3557/3591/3721/3864`; `resource_container_manager.py:1931/2039`; `vault_gc.py:757/627`; `workspace_lifecycle.py:305-312,150,163,176` | orphan/GC sweep inventory | verified | same (all resolve) |
| §2d `container_manager.py:_fresh_start:1499`, `:1580-1616`, `:1660/1723/1553/1653` | recreate paths (operator-only) | drift | `_fresh_start:1499`; delegation `1572-1616`; `reuse_record_id` `:1658` |
| §2d `resource_container_manager.py:932 _registry_active` | `_registry_active` property | drift | `resource_container_manager.py:929` (off 3) |

Note: the only live Section-2 drift is D1 (`user=`), now closed at both formerly-bare sites; the two `or "0:0"` sites are unchanged. `_registry_active` moved 932 → 929 (def line).

### Section 3 — Duplication survey beyond containers

| cite | claim | verdict | current |
|---|---|---|---|
| §3.1 `vault_repair.py:151/187/1507`; `scripts/vault_permission_cleanup_dryrun.py:148/198`; `scripts/migrate_vault.py:17`; `vault.py:56`; `vault_drift.py:101/129`; `vault_gc.py:757` | vault repair/drift/GC duplication | verified | same (all resolve; `migrate_vault.py:17` = shadowed `vault_root()`) |
| §3.2 `loader.py:87/278/287`; `config_manager.py:37`; `web_ui/backend/config_manager.py:161/520/536/803`; `security.py:209` | config-resolution resolver sprawl | verified | same |
| §3.3 `factory.py:11/51/119`; `base.py:50`; `llm_client.py:42` | provider construction (single-owner → no-op) | verified | same |
| §3.4 `git_info_tool.py:100/344/806`; `git_write_tool.py:19/143` | git tools single-owner | verified | same |
| §3.5 `bridge.py:971/976`; `event_forwarder.py:101/113` | WS-bridge broadcast dup | verified | same |
| §3.6 `security_gate.py:763/116/223/1001/1197`; `admission_gate.py:321/555`; `gate_helpers.py:90`; `security.py:105/209/329/1000`; `workspace_routes.py:786` | security/permission two-authority map | verified | same |
| §3.7 `tools/__init__.py:62/279`; `worker_thread.py:3238/3243`; `tool_executor.py:119/145/282` | three tool dispatch registries | verified | same |
| §3.8 `worker_manager.py:253/265/277/288,156/166/642,103`; `worker_registry.py:76/84/91/107`; `worker_lifecycle.py:130/143/148/199`; `worker_query.py:237`; `workspace_lifecycle_manager.py:201/385/582`; `worker_thread.py:569/807` | two registries / three staleness checks / two state machines | verified | same |
| §3.9 `start_thoughtmachine.sh:94/99`; `install.sh:26/31` | launcher duplication | verified | same |
| §3.10 `docker_executor.py:866`; `container_manager.py:1788`; `resource_container_manager.py:1504` | three exec paths | verified | same |

### Section 5 — Boundary map

| cite | claim | verdict | current |
|---|---|---|---|
| B1 `container_manager.py:_run_container:1456` (`volumes=None` L1466; tmpfs L1521-1527; `tm-packages` L1596) | create-time mount/tmpfs seam; isolation at CREATE only | verified | `:1456`; `volumes=None:1466`; tmpfs `:1522-1527`; `tm-packages:1596` |
| B2 `container_env.py:merge_container_identity_env:22`; consumed `container_manager.py:1481/1586/1793`, `resource_container_manager.py:1178/1509` | identity-env merge seam | verified | same |
| B3 `host_bash_tool.py:execute:214`, `shell=True:290` | host-shell surface (only `shell=True`) | verified | same |
| B4 `server.py:939`; `:521-533` | cross-thread event send onto loop | verified | same |
| B5 `llm_client.py:255-278` (`:268`) | sync `close()` → async `aclose()` | verified | same |
| B6 `server.py:511` `asyncio.to_thread` | loop → thread offload | verified | same |
| B7 `sandboxed_execution.py:SandboxedExecution.run:67` | subprocess sandbox chokepoint | drift | class `:48`; run `:74` (audit `:67`); `shell=False` |
| B8 `server.py:_confine_to_home:2706`, used `2749/2835/3062` | host-path confinement (HTTP) | verified | `:2706`; used `2749/2835/2843/3062` |
| B9 `gate_helpers.py:resolve_network_mode:90`; `security_gate.py:resolve_container_config:1001`; `docker_executor.py:176` | single network-mode mapping | verified | same |
| B10 `server.py:3940` bind; `uvicorn.run:3986`; CORS `869-871`; routers `2671-2683` | inbound posture (loopback + wildcard CORS, no `dependencies=`) | verified | `:3940`; `:3986`; `allow_origins:871` (import `:867`); routers `2671-2683` |
| B11 `injector.py:52/66/139/110` | credential injection seam | verified | same |
| B12 `models.py:AgentConfig` `model_dump(exclude={"api_key"})` | `api_key` hygiene | verified | same |
| B13 `llm_client.py:42/60/130` | provider construction/call | verified | same |
| B14 `mcp_server_connect.py:54`; `:98-105` | MCP connect via sandbox | verified | same |
| §5.8 host-shell `:290` / provider `:130` / CORS `:871` | three highest-blast ASSUMED boundaries | verified | same |

### Section 6 — Test suite shape

| cite | claim | verdict | current |
|---|---|---|---|
| `pyproject.toml:51` `testpaths=["tests"]`; `:54` markers | collection scope + markers | verified | `pyproject.toml:51`; markers `52-57` (audit `:54`) |
| 4 in-tree conftests + 1 out-of-tree | `tests/` (+`docker_integration`/`e2e`/`security`) + `web_ui/backend/tests/conftest.py` | verified | all five present |
| `3580 collected / 3576 selected / 4 deselected`; `docker=0 e2e=4 slow=0 integration=103` | measured suite shape | deferred | not re-measured this pass |
| `tests/test_container_machine_smoke.py` untracked `??`, +9, unmarked | one untracked test file, fully selected | verified | present (`21313 B`) |
| `tests/docker_integration/conftest.py` fakes docker | docker faked, never live | verified | same |

### Section 7 — Doc / code drift

| cite | claim | verdict | current |
|---|---|---|---|
| D1 `container_controls_findings.md:16` — router @ `server.py:2682` | audit concluded **WRONG** (off-by-one) | verified | `server.py:2682=vault_repair_router`; `container_record_router:2683` |
| D2 `container_controls_findings.md:46` — `workspace_container_delete` ends ≈3316; `# Container logs:` @3319 | audit concluded **IMPRECISE** end + **WRONG** fallback | verified | `def:3289`; `# Container logs:` @`3631` |
| D3 `target_architecture.md:181-192` §9 items 3-10 "planned" | audit concluded **STALE** (3-9 shipped) | verified | items 3-9 shipped |
| D4 `docs/security_layer.md` H1 obsolete | self-declared obsolete | verified | H1 `# ⚠️ OBSOLETE — pre-migration Qt architecture` |
| D5 Section 0b "~58/20" vs Section 0 | internal contradiction | verified | resolved (Section 0b reworded to 50/21) |
| D6 `docs/testing/test_inventory.md` 52 files / 752 fns | audit concluded **STALE** (paper 52/752 vs measured) | verified | paper claim present; code measured S6 |
| D7 `CAPABILITIES.md:126` `git_read`/`git_write`/`allow_host_resources` removed | audit concluded **CONTRADICTED** | verified | `schema_manifest.json:83/544/771/794/799` still declares; `session_config.py:105` `drop_legacy_allow_host_resources` |
| D8 `CAPABILITIES.md:49-50` single source enforced at create | audit concluded **IMPRECISE** (reuse not re-applied) | verified | true at create; reuse not re-applied (R1) |
| D9 `CAPABILITIES.md` cites `tools/docker_executor.py` | audit concluded **WRONG** (path) | verified | `docker_executor.py` at repo root; `tools/` absent |

### Section 8 — Risk register

| cite | claim | verdict | current |
|---|---|---|---|
| R1 `container_manager.py:_reuse_container:3272` | reuse returns a container without re-applying isolation; admission at create only (`admission_gate.py:321`) | verified | `container_manager.py:3272`; `admission_gate.py:321` |
| R2 `schema_manifest.json` 99 fields/33 entries; `models.py:141/137` undeclared; `vault_drift.py:344-362` WARNING-only | the two container-subsystem flags are unvalidated | verified | `vault_drift.py:344-362` (warning drift, never auto-fixed) |
| R3 `container_registry.py:156/239` "single path"; bypass @ `container_manager.py:1463`, `resource_container_manager.py:1213`, `docker_executor.py:790` | 3 raw `run()` sites bypass the documented single path | verified | same |
| R4 `resource_container_manager.py:932 _registry_active` fallback; `:954`; `docker_executor.py:292,374` | invisible legacy/new branch, same declared type | drift | `:929` (`_registry_active`); `:954`; `docker_executor.py:292/374` |
| R5 `host_bash_tool.py:290` `subprocess.run(cmd, shell=True, ...)` | only `shell=True` in product code | verified | same |
| R6 `server.py:871 allow_origins=["*"]` (import `:867`); routers `2671-2683` no `dependencies=` | wildcard CORS + dependency-free routers | verified | same |
| R7 `git_write_tool.py:590-597` single message; helper `:293-330` (`_unprotected_branch_agent_commit_allowed`) | one worktree-refusal message for ≥3 causes | verified | same |
| R8 `target_architecture.md:181-192` §9 stale | build-order doc understates shipped work | verified | (doc) |
| R9 `pyproject.toml:51 testpaths=["tests"]` excludes `web_ui/backend/tests/` | out-of-tree backend tests never collected | verified | `pyproject.toml:51` |
| R10 `worker_execution.py:338-366` | fail-open Site C — no ownership check (findings) | verified | `:338-366` (terminate/`_docker_exec_kill`; the container-exec branch guards on `_is_worker_owned_container`) |
| R11 four status surfaces (B-06) | container state from four disagreeing surfaces | deferred | conceptual; no single anchor |
| R12 `user=` missing @ `container_manager.py:1474` & `docker_executor.py:799`; present @ `registry:251` / `resource:1225` | Windows fallback missing at 2 of 4 sites | drift | all four now guarded (`1474` fixed `06a597e`; `799` fixed `0700c86`) |
| R13 `requirements.txt` 25 lower-bounds; `requirements-dev.txt:1` | no lockfile / upper bounds | drift | `requirements.txt` = 26 `>=` lines; `requirements-dev.txt:1` = `-r requirements.txt` |
| R14 `security/` vs `thoughtmachine/security.py`; `is_allowed:1000` vs `get_effective_permissions:763` | two permission authorities | verified | same |
| R15 resource UID/tmpfs/`.git` shadow (B-04) | resource-container UID/tmpfs/`.git` shadowing | deferred | findings-only (CONFIRMED per §0 item 2) |
| R16 `docker`=0 / `slow`=0 nodes; `tests/docker/` 143 unmarked; markers `:54` | dead markers while a by-name Docker suite runs | deferred | markers decl `pyproject.toml:52-57`; not re-measured |
| R17 `web_ui/frontend/src` JSX vs docs "TS" (§1d item 6) | docs frame JSX as TS | verified | (doc) |
| R18 `container_manager.py:857-868 _write_note` | note dropped when no record id | verified | `_write_note:848`; refusal block `:857-868` |
| R19 `server.py _BOOT_DRIFT_SCAN_LIMIT=100` | boot drift scan capped at 100 | verified | `server.py:523` (`=100`) |
| R20 `schema_manifest.json` `agent_soft_budget_seconds` orphan | declared-but-unused field | verified | `schema_manifest.json:252` (not on model) |

Note: R10's cited span is the terminate/`_docker_exec_kill` path; the container-exec branch within it is gated by `_is_worker_owned_container` — the findings-doc "no ownership check" claim was not independently re-derived this pass (anchor only).

### Section 9 — Migration plan

| cite | claim | verdict | current |
|---|---|---|---|
| M1 `container_registry.py:156/239`; callers `1463`/`1213`/`790`; exec `866`/`1788`/`1504`; delegation `container_manager.py:1580-1616`, `resource_container_manager.py:1174` | container create+exec unification | drift | callers `1463/1213/790`; exec `866/1788/1504`; delegation `1572-1616` & `1183` (call `:1190`) |
| M2 `security_gate.py:get_effective_permissions:763`; `is_allowed:1000`; resource-catalog dup; `workspace_routes.py:786` | fold to one permission authority | verified | same |
| M3 `defaults.py:132/226/227/161/256`; `loader.py:87/278/287`; `config_manager.py:37`; web_ui `config_manager.py:161/520/536/803`; `vault.py:347` | converge 8 config resolvers | verified | same |
| M4 `vault.py:56/126/157/347`; `vault_drift.py:101/129`; `vault_repair.py:151/187/1507`; `vault_gc.py:757`; `vault_repair_routes.py:54/96`; scripts `:148/198`; `migrate_vault.py:17` | vault lifecycle single-owner | verified | same |
| M5 `bridge.py:971/976`; `event_forwarder.py:101/113` | one broadcast helper | verified | same |
| M6 `tools/__init__.py:62/279`; `worker_thread.py:3238/3243`; `tool_executor.py:119/145/282` | three tool dispatch registries | verified | same |
| M7 `worker_manager.py:253…288,156/166/642,103`; `worker_registry.py:76…107`; `worker_lifecycle.py:130…199`; `worker_query.py:237`; `workspace_lifecycle_manager.py:201/385/582`; `worker_thread.py:569/807` | worker infra two mechanisms | verified | same |
| M8 `start_thoughtmachine.sh:94/99`; `install.sh:26/31` | launcher dup | verified | same |
| Preconditions O1-O7 (`resource_container_manager.py:932/:954`; `docker_executor.py:292,374`; `container_manager.py:3272`; `container_manager.py:1474`/`docker_executor.py:799`; `container_env.py:22`) | flag-removal open items | drift | O1 `:929/:954`; O2 `:292/374`; O3 `:3272`; O5 both now guarded; O6 `container_env.py:22` |

### Specific anchors

1. **Smoke-file quote.** `tests/test_container_machine_smoke.py:591` — the selected-suite smoke test `test_g9_real_container_hardening` (L545; self-skips without Docker; `host = host_user()`, L557, import L33) asserts **verbatim**: `assert user not in ("", "0", "0:0", "root")` (L591) and `assert user == host` (L592), with `user = attrs["Config"].get("User") or ""` (L590). This **marks against** the ratified Windows fallback `user=(host_user() or "0:0")` (§2a; the 2 R12 sites `infra/container_manager.py:1474` / `docker_executor.py:799`, item 3): the test **forbids** `"0:0"`, so the fallback value **would violate L591** — on Windows `host_user()` is `None`, so the L558 guard `if host is None or host.split(":")[0] == "0": pytest.skip(...)` fires *before* L591, i.e. the assertion is never reached on the very platform the fallback targets. This tension is **held by the old engineer** (resolution owner): recorded here, deliberately **not** resolved. (The out-of-product root harness `live_smoke_docker.py:3` — "`standalone, host-runnable live end-to-end smoke test for the ThoughtMachine Docker stack`", `.run()` sites L233/L244 hardcoding `user="1000:1000"` and `network_mode="none"` — is §2a's 5th/6th create sites.)
2. **`hook.py:150` one-line answer.** `thoughtmachine/container_record/hook.py:150` → `user = (attrs or {}).get("Config", {}).get("User") or host_user()`. This is a **bare** `host_user()` with **no** `or "0:0"` guard, but on the record **snapshot** path (deriving the recorded user on attach), **not** a container-create site — so it is outside R12's four create sites.
3. **The 2 R12 sites, recorded NOT-FIXED pre-W1 and fixed at HEAD.** When the audit was written, `infra/container_manager.py:1474` and `docker_executor.py:799` read `user=host_user()` (the two bare sites R12 named). At HEAD both read `user=(host_user() or "0:0")`: `1474` fixed by `06a597e` ("fall back to \"0:0\" when `host_user()` is unavailable") and `799` fixed by `0700c86` ("… unavailable in `DockerExecutor`"). Line numbers are unchanged (same-line edits).

### Scan notes

**(a) Import-time (module-level) constants.** Evaluated once at import; values are frozen at process start.

| constant | file:line | value |
|---|---|---|
| `HARDENED_CAP_DROP` | `agent/config/defaults.py:161` | `["ALL"]` |
| `HARDENED_SECURITY_OPT` | `agent/config/defaults.py:162` | `["no-new-privileges:true"]` |
| `HARDENED_READ_ONLY` | `agent/config/defaults.py:163` | `True` |
| `HARDENED_USER` | `agent/config/defaults.py:215` | `host_user()` (import-time eval) |
| `DEFAULT_USER_OOM_SCORE_ADJ` | `agent/config/defaults.py:144` | `1000` |
| `DEFAULT_RESOURCE_OOM_SCORE_ADJ` | `agent/config/defaults.py:145` | `500` |
| `DEFAULT_MEM_LIMIT` | `agent/config/defaults.py:226` | `"1g"` |
| `DEFAULT_CPU_QUOTA` | `agent/config/defaults.py:227` | `100000` |
| `RESOURCE_MEM_LIMIT` | `agent/config/defaults.py:132` | `"512m"` |
| `RESOURCE_CPU_QUOTA` | `agent/config/defaults.py:133` | `50000` |
| `DEFAULT_SESSION_PERMISSIONS` | `agent/config/defaults.py:256` | dict |
| `MAX_WORKERS_PER_SESSION` | `agent/config/defaults.py:39` | `3` |
| `HEARTBEAT_STALE_AFTER_S` | `agent/config/defaults.py:43` | `600` |
| `_BOOT_DRIFT_SCAN_LIMIT` | `web_ui/backend/server.py:523` | `100` |
| `_SHELL_METACHARS` | `security/sandboxed_execution.py:33` | `("&&","||",";","|","`","$(")` |
| `_STRIPPED_ENV` | `security/sandboxed_execution.py:36` | fixed PATH / `HOME=/dev/null` dict |

Note: `HARDENED_USER = host_user()` is bound at import, so a `None`/Windows `host_user()` is frozen for the process lifetime — relevant to the D1/R12 `user` policy.

**(b) `shell=True` / `os.system` sweep.** Scope: `agent/ tools/ security/ infra/ web_ui/ llm_providers/ session/` + root `docker_executor.py` (`--include=*.py`).

| pattern | file:line | note |
|---|---|---|
| `shell=True` (product) | `tools/host_bash_tool.py:290` | **only** product hit (`subprocess.run(cmd, shell=True, ...)`) |
| `shell=True` (comment) | `security/sandboxed_execution.py:15` | "`shell=True` is NEVER used" |
| `shell=True` (comment) | `security/sandboxed_execution.py:144` | "# … (never shell=True)" |
| `shell=True` (substring) | `security/sandboxed_execution.py:14` | `allow_shell=True` param (not a real `shell=True`) |
| `os.system` / `os.popen` | — | **zero** hits product-wide |
| `subprocess.call` | — | **zero** hits product-wide |
| `subprocess.Popen` | `tools/mcp_client.py:226`; `tools/mcp_client_new.py:219` | MCP stdio child launch (argv list, no shell) |

Note: confirms §5.8 #1 — the single `shell=True` in product code is `host_bash_tool.py:290`; the only other `subprocess` spawns are argv-list (no shell) MCP launches.

