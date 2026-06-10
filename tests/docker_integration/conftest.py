"""
pytest conftest for ``tests/docker_integration/``.

Provides fixtures to mock the ``docker`` module so tests can run without a
real Docker daemon.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock docker client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_docker_client() -> MagicMock:
    """A bare mock Docker client with ``containers.get`` stubbed."""
    client = MagicMock()
    client.containers.get = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Patch docker.from_env()
# ---------------------------------------------------------------------------

@pytest.fixture
def patch_docker(mock_docker_client: MagicMock) -> None:
    """Patch ``docker.from_env()`` to return ``mock_docker_client``."""
    import docker as docker_module
    with patch.object(docker_module, "from_env", return_value=mock_docker_client):
        yield


# ---------------------------------------------------------------------------
# raise_on_docker_call — context manager
# ---------------------------------------------------------------------------

@pytest.fixture
def raise_on_docker_call() -> type:
    """Return a context-manager class that patches ``docker.from_env`` to raise.

    Usage::

        with raise_on_docker_call(RuntimeError("...")):
            ...
    """

    class _RaiseOnDockerCall:
        """Context manager that makes ``docker.from_env()`` raise *exc*."""

        def __init__(self, exc: BaseException):
            self._exc = exc

        def __enter__(self):
            import docker as docker_module
            self._patcher = patch.object(docker_module, "from_env", side_effect=self._exc)
            self._patcher.start()
            return self

        def __exit__(self, *args):
            self._patcher.stop()

    return _RaiseOnDockerCall


# ---------------------------------------------------------------------------
# make_container — factory for a mock container with realistic attrs
# ---------------------------------------------------------------------------

@pytest.fixture
def make_container(mock_docker_client: MagicMock) -> type:
    """Return a factory that creates a mock container for a workspace path.

    Usage::

        container = make_container(network_mode="bridge", mount_mode="rw")
        container = make_container()          # defaults: none / ro
    """

    class _MakeContainer:
        """Factory helper — call to create a pre-configured container mock."""

        @staticmethod
        def build(
            network_mode: str = "none",
            mount_mode: str = "ro",
            **kwargs: str,
        ) -> MagicMock:
            _container = MagicMock()
            _container.attrs = {
                "Mounts": [
                    {
                        "Destination": "/workspace",
                        "Mode": mount_mode,
                        "Type": "bind",
                    },
                ],
                "HostConfig": {
                    "NetworkMode": network_mode,
                },
            }
            # Accept extra kwargs (e.g. status) for test flexibility
            if "status" in kwargs:
                _container.attrs.setdefault("State", {})["Status"] = kwargs["status"]
            mock_docker_client.containers.get.return_value = _container
            return _container

    return _MakeContainer.build
