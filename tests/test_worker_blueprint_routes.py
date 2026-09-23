"""
RED tests for feature C.2 — worker-blueprint read/patch routes (backend).

Contract under test (NOT yet implemented).  Per-workspace worker *blueprints*
(a.k.a. reusable worker definitions) are exposed through the existing
``/api/workspace`` router::

    GET   /api/workspace/{ws_id}/workers/blueprints          -> list[blueprint]
    GET   /api/workspace/{ws_id}/workers/blueprints/{name}   -> blueprint | 404
    PATCH /api/workspace/{ws_id}/workers/blueprints/{name}   -> blueprint | 404 | 422

The fetch + filesystem boundary IS the boundary: we drive the real FastAPI
router against a real on-disk vault (``THOUGHTMACHINE_VAULT_ROOT`` -> tmp_path)
and never mock the route, the filesystem, or the loader.

These tests are RED-first: the blueprint routes do not exist yet, so every
request 404s and each test fails on its status assertion (never on an import or
fixture error).
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.models.worker_definition import WorkerDefinition
from web_ui.backend.workspace_routes import router


WS_ID = "ws-test"
BASE = f"/api/workspace/{WS_ID}/workers/blueprints"

# Minimal-valid blueprints.  Only the fields the definition schema requires plus
# the two token thresholds; every other WorkerDefinition field is optional.
ALPHA = {
    "name": "alpha",
    "description": "Alpha worker blueprint",
    "system_prompt": "You are the alpha worker.",
    "tools": [],
    "permission_footprint": {},
    "warning_threshold_tokens": 65000,
    "critical_threshold_tokens": 80000,
}
BETA = {
    "name": "beta",
    "description": "Beta worker blueprint",
    "system_prompt": "You are the beta worker.",
    "tools": [],
    "permission_footprint": {},
    "warning_threshold_tokens": 11111,
    "critical_threshold_tokens": 22222,
}


def _seed_workers(ws_dir):
    """Write the seeded blueprints to ``<ws_dir>/workers.json``.

    Serialised with ``indent=2`` — the exact form ``_atomic_write_json`` uses —
    so a rewrite that leaves ``beta`` untouched is byte-identical to the seed.
    """
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workers.json").write_text(
        json.dumps([ALPHA, BETA], indent=2), encoding="utf-8"
    )


@contextmanager
def _client(monkeypatch, tmp_path):
    """Real router over a real tmp vault with a seeded workers.json."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    ws_dir = tmp_path / "workspaces" / WS_ID
    _seed_workers(ws_dir)

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, ws_dir


def _indent_block(block, spaces=2):
    """Indent a JSON block so it matches its in-array serialisation."""
    pad = " " * spaces
    return "\n".join(pad + line for line in block.splitlines())


# ── T1 ──────────────────────────────────────────────────────────────────────
def test_get_blueprint_list_returns_all_blueprints(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as (client, _ws_dir):
        resp = client.get(BASE)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list), f"expected a JSON list, got {type(data)!r}"

    by_name = {b["name"]: b for b in data}
    assert set(by_name) == {"alpha", "beta"}

    alpha = by_name["alpha"]
    assert alpha["description"] == ALPHA["description"]
    assert alpha["warning_threshold_tokens"] == 65000
    assert alpha["critical_threshold_tokens"] == 80000

    # The list endpoint returns fully-normalised blueprint objects: every field
    # of the WorkerDefinition schema is present, not just the seeded subset.
    assert set(alpha.keys()) == set(WorkerDefinition.model_fields)


# ── T2 ──────────────────────────────────────────────────────────────────────
def test_get_single_blueprint_and_404(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as (client, _ws_dir):
        ok = client.get(f"{BASE}/alpha")
        missing = client.get(f"{BASE}/missing")

    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "alpha"
    assert missing.status_code == 404, missing.text


# ── T3 ──────────────────────────────────────────────────────────────────────
def test_patch_blueprint_persists_and_round_trips(monkeypatch, tmp_path):
    edited = "edited by C.2"
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        resp = client.patch(f"{BASE}/alpha", json={"description": edited})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == edited

        # Round-trips through the single-blueprint GET.
        got = client.get(f"{BASE}/alpha")
        assert got.status_code == 200, got.text
        assert got.json()["description"] == edited

        post_text = (ws_dir / "workers.json").read_text(encoding="utf-8")

    on_disk = json.loads(post_text)
    by_name = {w["name"]: w for w in on_disk}
    assert by_name["alpha"]["description"] == edited
    # beta must be left byte-identical to the seed (only alpha was patched).
    seeded_beta_block = _indent_block(json.dumps(BETA, indent=2))
    assert seeded_beta_block in post_text


# ── T4 ──────────────────────────────────────────────────────────────────────
def test_patch_rejects_invalid_required_fields(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        before = (ws_dir / "workers.json").read_text(encoding="utf-8")

        r1 = client.patch(f"{BASE}/alpha", json={"system_prompt": None})
        assert r1.status_code == 422, r1.text

        r2 = client.patch(f"{BASE}/alpha", json={"tools": "not-a-list"})
        assert r2.status_code == 422, r2.text

        after = (ws_dir / "workers.json").read_text(encoding="utf-8")

    assert after == before


# ── T4a ─────────────────────────────────────────────────────────────────────
def test_validate_worker_dict_not_on_blueprint_write_path(monkeypatch, tmp_path):
    """WHY: the blueprint PATCH must gate writes through
    ``WorkerDefinition.model_validate`` (the Pydantic V2 schema the create/update
    worker routes already use), NOT through the legacy template-worker helper
    ``thoughtmachine.workspace_capabilities._validate_worker_dict``.

    The legacy helper only checks a 5-field subset and coerces nothing, so
    routing blueprint writes through it would silently accept invalid
    thresholds/tool shapes.  This test pins that the Pydantic validator runs and
    the legacy helper is bypassed.
    """
    import thoughtmachine.workspace_capabilities as caps
    import web_ui.backend.workspace_routes as routes

    hits = {"legacy_caps": 0, "legacy_routes": 0, "model_validate": 0}

    real_legacy_caps = getattr(caps, "_validate_worker_dict", None)
    if real_legacy_caps is not None:
        def _spy_legacy_caps(*a, **k):
            hits["legacy_caps"] += 1
            return real_legacy_caps(*a, **k)

        monkeypatch.setattr(
            caps, "_validate_worker_dict", _spy_legacy_caps, raising=False
        )

    if hasattr(routes, "_validate_worker_dict"):
        real_legacy_routes = routes._validate_worker_dict

        def _spy_legacy_routes(*a, **k):
            hits["legacy_routes"] += 1
            return real_legacy_routes(*a, **k)

        monkeypatch.setattr(
            routes, "_validate_worker_dict", _spy_legacy_routes, raising=False
        )

    real_model_validate = WorkerDefinition.model_validate

    @classmethod
    def _recording_model_validate(cls, obj, *a, **k):
        hits["model_validate"] += 1
        return real_model_validate(obj, *a, **k)

    monkeypatch.setattr(
        WorkerDefinition, "model_validate", _recording_model_validate
    )

    with _client(monkeypatch, tmp_path) as (client, _ws_dir):
        resp = client.patch(f"{BASE}/alpha", json={"description": "spy probe"})
        assert resp.status_code == 200, resp.text

    assert hits["legacy_caps"] == 0
    assert hits["legacy_routes"] == 0
    assert hits["model_validate"] >= 1


# ── T5 ──────────────────────────────────────────────────────────────────────
def test_patch_threshold_reaches_fresh_worker_config(monkeypatch, tmp_path):
    """WHY: a blueprint's token thresholds must reach the REAL consumer that
    builds a freshly spawned worker's agent config, not just the persisted file.

    Consumer symbol (quoted):
        ``tools.workspace.worker_thread.WorkerThread._build_agent_config``
    Call form (quoted):
        ``WorkerThread(name="alpha", definition=<workers.json entry>,
                       agent_config={"provider": ..., "model": ...},
                       workspace_dir=<ws_dir>)._build_agent_config()``

    ``_build_agent_config`` maps ``warning_threshold_tokens`` ->
    ``token_monitor_warning_threshold`` and ``critical_threshold_tokens`` ->
    ``token_monitor_critical_threshold`` on the resulting ``AgentConfig``.  We
    patch the blueprint, then drive that exact method (no stub of the mapping,
    no hand-parse of the plumbing, no mocked loader) and assert the fresh config
    carries the persisted values.
    """
    from tools.workspace.worker_thread import WorkerThread

    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        resp = client.patch(
            f"{BASE}/alpha",
            json={
                "warning_threshold_tokens": 12345,
                "critical_threshold_tokens": 55555,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["warning_threshold_tokens"] == 12345
        assert resp.json()["critical_threshold_tokens"] == 55555

        # The persisted list is exactly what the spawn path's loader
        # (Worker._load_workers) reads back into its definition dict.
        persisted = json.loads((ws_dir / "workers.json").read_text(encoding="utf-8"))

    definition = next(w for w in persisted if w["name"] == "alpha")

    worker = WorkerThread(
        name="alpha",
        definition=definition,
        agent_config={"provider": "openai_compatible", "model": "deepseek-reasoner"},
        workspace_dir=ws_dir,
        session_permissions={},
    )
    cfg = worker._build_agent_config()

    assert cfg is not None, "WorkerThread._build_agent_config returned None"
    assert cfg.token_monitor_warning_threshold == 12345
    assert cfg.token_monitor_critical_threshold == 55555
