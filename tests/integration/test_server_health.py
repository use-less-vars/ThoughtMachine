"""Server /health endpoint contract (hardening sprint, Step 2).

Proves the REAL FastAPI app (web_ui/backend/server.py) serves ``GET /health``
with the deployment-verification payload: status/service identity plus the git
revision the server was built from (``_SERVER_REVISION``, captured at import
time via ``git rev-parse HEAD``).

Hermetic harness mirrors tests/integration/test_apply_config_coverage.py: temp
HOME + patched ``Path.home()`` + purged/re-imported web_ui.backend modules so
module-level singletons are built against the temp HOME.  No network, no LLM,
no Docker daemon.  The lifespan runs inside ``TestClient(app)`` — safe here
(container scan is wrapped in try/except, and HOME is a throwaway temp dir).

Run (from repo root):
    python -m pytest tests/integration/test_server_health.py -v
"""

from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import subprocess
import sys as sys_mod
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.integration


# ════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness (EXACT mirror of test_apply_config_coverage.py)
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server."""
    tmp_home = tempfile.mkdtemp(prefix="test_server_health_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # Re-import server so module-level singletons (_session_store, registries,
    # _SERVER_REVISION) are built against the temp HOME, not the real one.
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile", "thoughtmachine.bootstrap", "session")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val
    shutil.rmtree(tmp_home, ignore_errors=True)


def _server_mod():
    """Return the (purged-then-imported) web_ui.backend.server module."""
    return importlib.import_module("web_ui.backend.server")


# ════════════════════════════════════════════════════════════════════════════
# Case 1 — /health identity payload
# ════════════════════════════════════════════════════════════════════════════

def test_health_endpoint_status_ok(contract_server):
    """GET /health → 200 with status 'ok' and service identity."""
    app, _ = contract_server
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["service"] == "thoughtmachine-web-ui"


def test_health_endpoint_reports_git_revision(contract_server):
    """GET /health → revision equals the repo's git HEAD at import time.

    The server computes _SERVER_REVISION from _project_root (server.py:165-189);
    this test re-derives the expected value the exact same way and also pins
    the broadcast payload against the module constant.
    """
    app, _ = contract_server
    server_mod = _server_mod()
    assert server_mod._SERVER_REVISION, "server must compute a revision"

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=server_mod._project_root,
        capture_output=True,
        text=True,
        timeout=5.0,
    ).stdout.strip()
    assert expected, "repo must have a git HEAD for the revision check"

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["revision"] == expected
        assert payload["revision"] == server_mod._SERVER_REVISION
