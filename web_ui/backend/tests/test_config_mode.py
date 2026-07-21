"""
Tests for the system prompt mode switching endpoints
(POST /api/config/mode and GET /api/config/mode).

These tests use a temporary directory in place of ``~/.thoughtmachine/``
so that the real user config is never touched.
"""

from __future__ import annotations

import tempfile
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_ui.backend.server import app

# Shared client for all tests in this module
client = TestClient(app)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_thoughtmachine(monkeypatch):
    """Replace ``Path.home()`` with a temporary directory containing a
    throwaway ``.thoughtmachine/`` directory.

    The fixture creates an ``engineer_system_prompt.txt`` inside so that
    the POST /api/config/mode endpoint can copy it.  After the test the
    entire temporary tree is removed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="test_tm_"))
    tm_dir = tmp / ".thoughtmachine"
    tm_dir.mkdir(parents=True, exist_ok=True)

    # Create the engineer prompt source file
    engineer_content = (
        "You are an AI engineer. Write code. Do not philosophise."
    )
    (tm_dir / "engineer_system_prompt.txt").write_text(
        engineer_content, encoding="utf-8"
    )

    # Patch Path.home() to point at our temporary directory
    monkeypatch.setattr(Path, "home", lambda: tmp)

    yield tm_dir

    shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/config/mode
# ══════════════════════════════════════════════════════════════════════════════


class TestSetMode:
    """Tests for the POST /api/config/mode endpoint."""

    def test_set_engineer_mode(self, mock_thoughtmachine):
        """Switching to engineer mode copies the engineer prompt to
        ``custom_system_prompt.txt`` and returns the expected response."""
        response = client.post("/api/config/mode", json={"mode": "engineer"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data == {"status": "ok", "mode": "engineer"}

        custom_prompt = mock_thoughtmachine / "custom_system_prompt.txt"
        assert custom_prompt.exists(), "custom_system_prompt.txt should exist"
        content = custom_prompt.read_text(encoding="utf-8").strip()
        assert "Write code" in content
        assert "not philosophise" in content

    def test_set_agent_mode_clears_custom_prompt(self, mock_thoughtmachine):
        """Switching to agent mode removes ``custom_system_prompt.txt``."""
        # First set engineer mode so the file exists
        client.post("/api/config/mode", json={"mode": "engineer"})
        assert (
            mock_thoughtmachine / "custom_system_prompt.txt"
        ).exists(), "precondition: custom prompt should exist"

        # Now switch to agent
        response = client.post("/api/config/mode", json={"mode": "agent"})
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok", "mode": "agent"}

        custom_prompt = mock_thoughtmachine / "custom_system_prompt.txt"
        assert not custom_prompt.exists(), (
            "custom_system_prompt.txt should be removed in agent mode"
        )

    def test_invalid_mode_returns_400(self, mock_thoughtmachine):
        """An unrecognised mode value should result in a 400 error."""
        response = client.post(
            "/api/config/mode", json={"mode": "invalid"}
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"].lower()
        assert "invalid" in detail
        assert "engineer" in detail
        assert "custom" in detail

    def test_empty_mode_returns_400(self, mock_thoughtmachine):
        """An empty mode string should result in a 400 error."""
        response = client.post("/api/config/mode", json={"mode": ""})
        assert response.status_code == 400, response.text

    def test_mode_is_case_insensitive(self, mock_thoughtmachine):
        """Mode values should be accepted case-insensitively."""
        response = client.post(
            "/api/config/mode", json={"mode": "ENGINEER"}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok", "mode": "engineer"}

        custom_prompt = mock_thoughtmachine / "custom_system_prompt.txt"
        assert custom_prompt.exists()

    def test_legacy_full_maps_to_agent(self, mock_thoughtmachine):
        """Legacy "full" mode value should be accepted and mapped to
        "agent"."""
        # First set engineer mode so custom_system_prompt.txt exists
        client.post("/api/config/mode", json={"mode": "engineer"})
        assert (
            mock_thoughtmachine / "custom_system_prompt.txt"
        ).exists(), "precondition: custom prompt should exist"

        # Send legacy "full" — should be treated as "agent"
        response = client.post("/api/config/mode", json={"mode": "full"})
        assert response.status_code == 200, response.text
        # Response reports the canonical mode name
        assert response.json() == {"status": "ok", "mode": "agent"}

        custom_prompt = mock_thoughtmachine / "custom_system_prompt.txt"
        assert not custom_prompt.exists(), (
            "custom_system_prompt.txt should be removed when mapping full -> agent"
        )

    def test_set_agent_mode_when_no_custom_prompt(self, mock_thoughtmachine):
        """Switching to agent mode when no custom prompt exists should
        still succeed (no-op)."""
        custom_prompt = mock_thoughtmachine / "custom_system_prompt.txt"
        assert not custom_prompt.exists(), "precondition: no custom prompt"

        response = client.post("/api/config/mode", json={"mode": "agent"})
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok", "mode": "agent"}


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/config/mode
# ══════════════════════════════════════════════════════════════════════════════


class TestGetMode:
    """Tests for the GET /api/config/mode endpoint."""

    def test_get_mode_returns_agent_by_default(self, mock_thoughtmachine):
        """When no custom prompt exists, GET should return agent mode."""
        response = client.get("/api/config/mode")
        assert response.status_code == 200, response.text
        assert response.json() == {"mode": "agent"}

    def test_get_mode_returns_engineer_after_set(self, mock_thoughtmachine):
        """After switching to engineer mode, GET should return engineer."""
        client.post("/api/config/mode", json={"mode": "engineer"})
        response = client.get("/api/config/mode")
        assert response.status_code == 200, response.text
        assert response.json() == {"mode": "engineer"}

    def test_get_mode_returns_agent_after_clearing(self, mock_thoughtmachine):
        """After switching back to agent mode, GET should return agent."""
        client.post("/api/config/mode", json={"mode": "engineer"})
        client.post("/api/config/mode", json={"mode": "agent"})
        response = client.get("/api/config/mode")
        assert response.status_code == 200, response.text
        assert response.json() == {"mode": "agent"}

    def test_get_mode_with_empty_custom_prompt(self, mock_thoughtmachine):
        """An empty ``custom_system_prompt.txt`` should be treated as
        agent mode."""
        custom_prompt = mock_thoughtmachine / "custom_system_prompt.txt"
        custom_prompt.write_text("   \n  \n", encoding="utf-8")
        response = client.get("/api/config/mode")
        assert response.status_code == 200, response.text
        assert response.json() == {"mode": "agent"}
