"""
Integration tests for ``GET /api/workspace/templates``.

Tests the endpoint's file-reading, validation, and fallback logic
using a minimal FastAPI app that only registers the workspace router.
"""

from __future__ import annotations

import json
import logging
import pathlib
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from agent.models.worker_definition import WorkerDefinition

# ---------------------------------------------------------------------------
# Minimal test app (router only — no full server bootstrap needed)
# ---------------------------------------------------------------------------

app = FastAPI()

# Import the router late so the app is defined first (no circular issues)
from web_ui.backend.workspace_routes import router as workspace_router  # noqa: E402
app.include_router(workspace_router)


@pytest.fixture
def client() -> TestClient:
    """Yield a TestClient bound to the minimal app."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REPO_TEMPLATES = _PROJECT_ROOT / "resources" / "worker_templates"

assert _REPO_TEMPLATES.is_dir(), (
    f"Expected repo templates at {_REPO_TEMPLATES}"
)


def _valid_default_entry() -> Dict[str, Any]:
    """Return a minimal valid WorkerDefinition dict (mimics default.json)."""
    return {
        "name": "default",
        "description": "Default general-purpose worker with no restrictions.",
        "system_prompt": "You are a capable autonomous sub-agent.\n",
        "tools": [],
        "worker_permissions": {},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetTemplates:
    """Tests for ``GET /api/workspace/templates``."""

    def test_returns_200_with_fallback_templates(self, client):
        """When the user directory is absent, the endpoint falls back to
        the repo templates and returns 200 with a non-empty list."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = Path("/nonexistent")
            resp = client.get("/api/workspace/templates")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert isinstance(data, list), "Response should be a list"
        assert len(data) > 0, "Should return at least one template"

    def test_each_item_parses_as_worker_definition(self, client):
        """Every item in the response can be validated as WorkerDefinition."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = Path("/nonexistent")
            resp = client.get("/api/workspace/templates")

        data = resp.json()
        for item in data:
            wd = WorkerDefinition.model_validate(item)
            assert wd.name is not None
            assert wd.description is not None
            assert wd.system_prompt is not None
            assert wd.tools is not None
            assert wd.worker_permissions is not None

    def test_returns_valid_jsons_only_skips_invalid(self, client, tmp_path):
        """When the user directory contains a mix of valid and invalid JSON,
        only valid documents are returned."""
        templates_dir = tmp_path / ".thoughtmachine" / "worker_templates"
        templates_dir.mkdir(parents=True)

        # Write one valid template
        valid = _valid_default_entry()
        (templates_dir / "default.json").write_text(json.dumps(valid))

        # Write an invalid JSON file (malformed)
        (templates_dir / "garbage.json").write_text("{not valid json}")

        # Write a valid JSON that fails WorkerDefinition validation
        bad_schema = {"name": "no-tools"}
        (templates_dir / "bad_schema.json").write_text(json.dumps(bad_schema))

        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            resp = client.get("/api/workspace/templates")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = [item["name"] for item in data]
        assert "default" in names, "Valid template should be returned"
        assert "bad_schema" not in names, "Invalid schema should be skipped"
        assert len(data) == 1, "Only 1 valid template expected"

    def test_empty_user_dir_falls_back_to_repo(self, client, tmp_path):
        """An empty user template directory triggers the repo fallback."""
        templates_dir = tmp_path / ".thoughtmachine" / "worker_templates"
        templates_dir.mkdir(parents=True)

        # Empty directory — no .json files
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            resp = client.get("/api/workspace/templates")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0, "Should fall back to repo templates"
        names = {item["name"] for item in data}
        expected = {"default"}
        assert names == expected, f"Expected {expected}, got {names}"

    def test_default_template_name_present(self, client):
        """The fallback returns the default template."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = Path("/nonexistent")
            resp = client.get("/api/workspace/templates")

        assert resp.status_code == 200
        names = {item["name"] for item in resp.json()}
        assert names == {"default"}, (
            f"Expected default, got {names}"
        )

    def test_logs_warning_for_invalid_files(self, client, tmp_path, caplog):
        """Invalid template files produce a warning log message."""
        templates_dir = tmp_path / ".thoughtmachine" / "worker_templates"
        templates_dir.mkdir(parents=True)

        valid = _valid_default_entry()
        (templates_dir / "default.json").write_text(json.dumps(valid))
        (templates_dir / "garbage.json").write_text("{not valid json}")

        caplog.set_level(logging.WARNING)

        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            client.get("/api/workspace/templates")

        assert any(
            "garbage.json" in record.message
            for record in caplog.records
        ), "Expected a warning about garbage.json"
