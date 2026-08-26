"""Regression tests: GET /api/health/containers structured response.

These tests are hermetic: they fake the `docker` SDK module in sys.modules
and never touch a real docker daemon. They pin the structured degraded
shape ({status, docker:{available, reason, hint, ...}}) for the daemon-down
and tool-import-failure cases.
"""

import sys
import types

import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def server_module():
    """Fresh import of web_ui.backend.server (temp HOME + prefix purge)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_home:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp_home
        try:
            for prefix in (
                "web_ui.backend",
                "agent.config.provider_profile",
                "thoughtmachine.bootstrap",
                "session",
            ):
                for mod in list(sys.modules):
                    if mod == prefix or mod.startswith(prefix + "."):
                        del sys.modules[mod]
            import web_ui.backend.server as server_mod

            yield server_mod
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


@pytest.fixture(scope="module")
def client(server_module):
    with TestClient(server_module.app) as test_client:
        yield test_client


def _fake_docker_module():
    """A fake `docker` SDK that raises a daemon-connection error on from_env()."""
    fake = types.ModuleType("docker")

    class DockerException(Exception):
        pass

    errors = types.ModuleType("docker.errors")
    errors.DockerException = DockerException

    def from_env(*args, **kwargs):
        raise DockerException(
            "Cannot connect to the Docker daemon at "
            "unix:///var/run/docker.sock. Is the docker daemon running?"
        )

    fake.errors = errors
    fake.from_env = from_env
    return fake


def test_health_endpoint_returns_structured_docker_unavailable_reason(
    client, monkeypatch
):
    monkeypatch.setitem(sys.modules, "docker", _fake_docker_module())

    resp = client.get("/api/health/containers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    docker = body["docker"]
    assert docker["available"] is False
    assert docker["reason"] == "daemon_down"
    assert isinstance(docker["hint"], str) and docker["hint"]
    assert docker["version"] is None
    assert isinstance(docker["error"], str) and docker["error"]
    assert "checked_at" in body


def test_health_endpoint_import_failure_reason_is_queryable(client, monkeypatch):
    import tools

    monkeypatch.setattr(
        tools,
        "IMPORT_FAILURES",
        [{"tool": "DockerCodeRunner", "error": "boom: docker SDK import failed"}],
    )
    # `import docker` then raises ImportError (None in sys.modules).
    monkeypatch.setitem(sys.modules, "docker", None)

    resp = client.get("/api/health/containers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    docker = body["docker"]
    assert docker["available"] is False
    assert docker["reason"] == "import_failed"
    assert isinstance(docker["hint"], str) and docker["hint"]
    assert (
        "failed to load" in docker["hint"] or "DockerCodeRunner" in docker["hint"]
    )
    assert "checked_at" in body
