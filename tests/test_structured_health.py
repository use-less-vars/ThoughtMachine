"""Regression tests for the structured health endpoints.

Covers:
- GET /api/health/containers: structured docker availability payload
  (daemon-down degraded shape and healthy shape) with a faked `docker`
  SDK module — hermetic, never touches a real daemon.
- GET /api/health: structured alias of GET /health (status/service/revision).
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


def _fake_docker_daemon_down():
    """Fake `docker` SDK whose from_env() raises a daemon-connection error."""
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


def _fake_docker_available():
    """Fake `docker` SDK whose from_env() returns a healthy client."""
    fake = types.ModuleType("docker")
    fake.errors = types.ModuleType("docker.errors")

    class _Client:
        def ping(self):
            return True

        def version(self):
            return {"Version": "24.0.0"}

        def close(self):
            pass

    def from_env(*args, **kwargs):
        return _Client()

    fake.from_env = from_env
    return fake


def test_health_containers_structured_daemon_down(client, monkeypatch):
    """Daemon unreachable -> 200 degraded payload with daemon_down reason."""
    monkeypatch.setitem(sys.modules, "docker", _fake_docker_daemon_down())

    resp = client.get("/api/health/containers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    docker = body["docker"]
    assert docker["available"] is False
    assert docker["reason"] == "daemon_down"
    assert isinstance(docker["hint"], str) and docker["hint"]
    assert "checked_at" in body


def test_health_containers_structured_available(client, monkeypatch):
    """Docker reachable -> 200 healthy payload with version and no reason."""
    monkeypatch.setitem(sys.modules, "docker", _fake_docker_available())

    resp = client.get("/api/health/containers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    docker = body["docker"]
    assert docker["available"] is True
    assert docker["reason"] is None
    assert docker["version"] == "24.0.0"
    assert "checked_at" in body


def test_api_health_mirrors_health(client):
    """GET /api/health returns the same structured payload as /health."""
    resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "service" in body
    assert "revision" in body
    assert body["service"] == "thoughtmachine-web-ui"
