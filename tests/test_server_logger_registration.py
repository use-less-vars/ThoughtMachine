"""Regression tests for server-process agent-logger registration.

The agent process (``agent/core/agent.py``) creates an ``_AgentLogger`` and
registers it with BOTH facades:

- ``agent.logging.unified.set_logger``  (read back via ``_get_logger()``)
- ``thoughtmachine.security.set_logger`` (read back via ``security._logger``)

The server process (``web_ui.backend.server``) historically never did this, so
``agent.logging.unified._get_logger()`` and ``thoughtmachine.security._logger``
stayed ``None`` for the whole server lifetime -- the "no agent logger is
attached (server.py never calls set_logger)" gap named in
``infra/container_manager.py``.

These tests drive the real FastAPI lifespan and then assert both module-level
registrations are live.  Lifespan is driven with the Starlette ``TestClient``
context-manager form, mirroring ``tests/test_structured_health.py`` (fresh
import of ``web_ui.backend.server`` under a temp HOME + prefix purge).
"""

import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def server_module():
    """Fresh import of web_ui.backend.server (temp HOME + prefix purge).

    Mirrors ``tests/test_structured_health.py::server_module``.
    """
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_home:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp_home
        prefixes = (
            "web_ui.backend",
            "agent.config.provider_profile",
            "thoughtmachine.bootstrap",
            "session",
        )
        try:
            from tests.integration.harness import purged_sys_modules

            with purged_sys_modules(prefixes):
                import web_ui.backend.server as server_mod

                yield server_mod
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


@pytest.fixture(scope="module")
def started_client(server_module):
    """Enter the TestClient context manager so the real lifespan runs once."""
    with TestClient(server_module.app) as test_client:
        yield test_client


def test_unified_logger_registered_after_lifespan(started_client):
    """After startup ``agent.logging.unified._get_logger()`` is non-None."""
    from agent.logging import unified

    assert unified._get_logger() is not None


def test_security_logger_registered_after_lifespan(started_client):
    """After startup ``thoughtmachine.security._logger`` is non-None."""
    from thoughtmachine import security

    assert security._logger is not None
