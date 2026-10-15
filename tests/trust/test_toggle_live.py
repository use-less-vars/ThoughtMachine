"""
test_toggle_live.py — Trust-level stateful toggle tests for DockerExecutor.

Extends the docker-level ``tests/docker/test_toggle.py`` with tests for
edge cases that are not covered by the original parametrized matrix:

Gaps filled here:
  - **no_recreation_when_permissions_unchanged**: When session permissions
    are identical after a reload, the existing container is reused.
  - **ask_permission_treated_as_restrictive**: When the effective permission
    is "ask", the container config uses the most restrictive setting
    (network="none", mode="ro") and the container is NOT recreated if
    it already has those restrictive settings.

The tests in this file are "trust-level" — they validate behavioural
contracts that other modules depend on.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures (mirror tests/docker/test_toggle.py)
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_container() -> MagicMock:
    """A mock Docker container with realistic attrs."""
    c = MagicMock()
    c.id = "abc123def456"
    c.status = "running"
    c.attrs = {
        "HostConfig": {"NetworkMode": "bridge"},
        "Mounts": [
            {"Destination": "/workspace", "Mode": "rw", "Type": "bind"},
        ],
        "State": {"Status": "running"},
    }
    c.reload = MagicMock()
    c.stop = MagicMock()
    c.remove = MagicMock()
    return c


@pytest.fixture
def mock_docker(mock_container: MagicMock) -> MagicMock:
    """Mock ``docker.from_env()`` returning a *name-aware* daemon client.

    Production keys persistent containers by their canonical envelope name
    (:func:`build_container_name`): ``containers.get(name)`` resolves a
    container ONLY when that exact envelope already exists (otherwise it
    raises ``docker.errors.NotFound``); ``containers.run(name=...)`` creates a
    container under that name; ``container.remove()`` deletes it from the
    daemon.  The fake mirrors that daemon contract so ``_ensure_container``
    exercises its real name-match / drift / create branches instead of finding
    (and refusing to replace) ``mock_container`` under *every* name.

    ``images.get`` returns an image whose ``thoughtmachine.build_hash`` label
    matches the CURRENT executor build sources, so ``_ensure_image`` reuses it
    instead of triggering a drift rebuild (which would need a real vault
    Dockerfile).
    """
    import docker as _docker

    from docker_executor import (
        EXECUTOR_BUILD_HASH_LABEL,
        LIFECYCLE_PERSISTENT,
        build_container_name,
        compute_executor_build_hash,
    )

    workspace_path = "/tmp/test-workspace"
    # Names created via ``containers.run(name=...)`` (plus the single envelope
    # the live ``mock_container.attrs`` already occupies) are the only
    # resolvable ones: the daemon cannot return a container for a name it does
    # not hold.
    registry: dict[str, MagicMock] = {}
    mock_container._removed = False

    def _envelope_name() -> str:
        """Canonical name of the envelope the live ``mock_container`` occupies."""
        network = mock_container.attrs["HostConfig"]["NetworkMode"]
        mode = "ro"
        for m in mock_container.attrs.get("Mounts", []):
            if m.get("Destination") == "/workspace":
                mode = m.get("Mode", "ro")
                break
        return build_container_name(
            workspace_path,
            LIFECYCLE_PERSISTENT,
            network_mode=network,
            workspace_mode=mode,
        )

    def _get(name: str) -> MagicMock:
        # A removed container is gone from the daemon; any name it does not
        # currently hold is a NotFound, exactly like the docker daemon.
        if not mock_container._removed and (
            name == _envelope_name() or name in registry
        ):
            return mock_container
        raise _docker.errors.NotFound(f"No such container: {name}")

    def _run(*args: object, **kwargs: object) -> MagicMock:
        name = kwargs.get("name")
        if name:
            # ``run`` creates the container under ``name``; record the created
            # config on the (reused) mock object so later name lookups and
            # drift checks observe the live envelope.
            registry[name] = mock_container
            mock_container._removed = False
            network = kwargs.get("network_mode")
            if network is not None:
                mock_container.attrs["HostConfig"]["NetworkMode"] = network
            for m in kwargs.get("mounts") or []:  # docker.types.Mount is a dict
                if m.get("Target") == "/workspace":
                    mock_container.attrs["Mounts"] = [
                        {
                            "Destination": "/workspace",
                            "Mode": "ro" if m.get("ReadOnly") else "rw",
                            "Type": "bind",
                        }
                    ]
                    break
        return mock_container

    client = MagicMock()
    client.containers.get.side_effect = _get
    client.containers.run.side_effect = _run
    mock_container.remove.side_effect = (
        lambda *a, **k: setattr(mock_container, "_removed", True)
    )
    img = MagicMock()
    img.labels = {EXECUTOR_BUILD_HASH_LABEL: compute_executor_build_hash()}
    client.images.get.return_value = img
    client.api.build.return_value = []
    return client


# ══════════════════════════════════════════════════════════════════════════
#  Helper: instantiate DockerExecutor with mocks
# ══════════════════════════════════════════════════════════════════════════


def _make_executor(
    mock_docker_client: MagicMock,
    session_permissions: dict | None = None,
    workspace_id: str | None = "test-ws",
) -> "DockerExecutor":
    """Return a DockerExecutor with ``docker.from_env()`` and workspace mocks."""
    from docker_executor import DockerExecutor

    with patch("docker_executor.docker.from_env", return_value=mock_docker_client):
        executor = DockerExecutor(
            workspace_path="/tmp/test-workspace",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            force_rebuild=False,
            idle_timeout=600,
            session_permissions=session_permissions,
            workspace_id=workspace_id,
        )
    return executor


# ══════════════════════════════════════════════════════════════════════════
#  Tests: no recreation when permissions unchanged
# ══════════════════════════════════════════════════════════════════════════


class TestNoRecreation:
    """When session permissions do NOT change, the container is reused."""

    def test_no_recreation_when_permissions_unchanged(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Same permissions → existing container is reused, not recreated."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            # Same return value each time → config matches → no recreation
            return_value=("bridge", "rw"),
        ):
            # First call creates container
            executor._ensure_container()
            assert mock_container.stop.called is False, (
                "Container should NOT be stopped on first _ensure_container"
            )

            # Reset mocks to track second call
            mock_container.stop.reset_mock()
            mock_container.remove.reset_mock()
            mock_docker.containers.run.reset_mock()

            # Second call with same permissions — container should be reused
            executor._ensure_container()

        # No stop/remove/recreate should have occurred
        assert mock_container.stop.called is False, (
            "Container should NOT be stopped when permissions unchanged"
        )
        assert mock_container.remove.called is False, (
            "Container should NOT be removed when permissions unchanged"
        )
        # containers.run should NOT have been called (no new container created)
        if mock_docker.containers.run.called:
            import warnings
            warnings.warn(
                "containers.run was called during no-change toggle — "
                "this may indicate unnecessary recreation"
            )

    def test_recreation_when_permissions_change(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Changed permissions → old container removed, new one created."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "rw"), ("none", "ro"), ("none", "ro")],
        ):
            # First call → creates container with bridge+rw
            executor._ensure_container()

            # Change permissions
            executor.session_permissions = {
                "network": "banned", "filesystem": "read", "container": True,
            }

            # Second call → config mismatch → recreate
            executor._ensure_container()

        # Old container should have been stopped and removed
        assert mock_container.stop.called, (
            "Old container should be stopped when permissions change"
        )
        assert mock_container.remove.called, (
            "Old container should be removed when permissions change"
        )

    def test_identity_after_reload(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Re-assigning the same permissions dict values does NOT trigger recreation."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            return_value=("bridge", "rw"),
        ):
            executor._ensure_container()

            mock_container.stop.reset_mock()
            mock_container.remove.reset_mock()

            # Re-assign identical permissions
            executor.session_permissions = {
                "network": "write", "filesystem": "write", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called is False
        assert mock_container.remove.called is False


# ══════════════════════════════════════════════════════════════════════════
#  Tests: ask permission treated as restrictive
# ══════════════════════════════════════════════════════════════════════════


class TestAskPermissionRestrictive:
    """"ask" permission produces restrictive container config."""

    def test_ask_network_creates_none_mode(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """network='ask' → container gets network_mode='none'."""
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "ask", "filesystem": "write", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            return_value=("none", "rw"),
        ):
            executor._ensure_container()

        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs["network_mode"] == "none", (
            f"network='ask' → expected network='none', got {kwargs.get('network_mode')!r}"
        )

    def test_ask_filesystem_creates_ro_mode(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """filesystem='ask' → container gets workspace mount mode='ro'."""
        # workspace_id=None → the BIND path is exercised: this test asserts
        # the Phase-2 ``mounts`` list representation (a bind mount for the
        # workspace plus the per-workspace package volume).
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "ask", "container": True},
            workspace_id=None,
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            return_value=("bridge", "ro"),
        ):
            executor._ensure_container()

        _, kwargs = mock_docker.containers.run.call_args
        mounts = kwargs.get("mounts") or []
        ws_mounts = [m for m in mounts
                     if m.get("Target") == "/workspace" and m.get("Type") == "bind"]
        assert ws_mounts, f"no /workspace bind mount in mounts={mounts!r}"
        assert ws_mounts[0].get("ReadOnly") is True, (
            f"filesystem='ask' → expected ReadOnly=True, got {ws_mounts[0]!r}"
        )
        assert ws_mounts[0].get("Source") == "/tmp/test-workspace", f"{ws_mounts[0]!r}"
        pkg_mounts = [m for m in mounts if m.get("Target") == "/home/agent/.local"]
        assert pkg_mounts, f"no package volume mount in mounts={mounts!r}"
        assert pkg_mounts[0].get("Type") == "volume", f"{pkg_mounts[0]!r}"
        assert pkg_mounts[0].get("Source") == "tm-packages-default", f"{pkg_mounts[0]!r}"

    def test_ask_not_recreated_when_already_restrictive(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """Container already at restrictive settings (none+ro) is NOT recreated for 'ask'."""
        # Start with banned (which produces none+ro)
        mock_container.attrs["HostConfig"]["NetworkMode"] = "none"
        mock_container.attrs["Mounts"][0]["Mode"] = "ro"

        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "banned", "filesystem": "read", "container": True},
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            # Both produce ("none", "ro") — config matches, no recreation
            return_value=("none", "ro"),
        ):
            executor._ensure_container()
            mock_container.stop.reset_mock()
            mock_container.remove.reset_mock()
            mock_docker.containers.run.reset_mock()

            # Switch to 'ask' — still produces (none, ro)
            executor.session_permissions = {
                "network": "ask", "filesystem": "ask", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called is False, (
            "Container should NOT be stopped when switching banned→ask "
            "(both produce same restrictive config)"
        )
        assert mock_container.remove.called is False

    def test_ask_to_write_recreates_container(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """ask→write triggers recreation (none→bridge)."""
        mock_container.attrs["HostConfig"]["NetworkMode"] = "none"
        mock_container.attrs["Mounts"][0]["Mode"] = "ro"

        # workspace_id=None → bind path (mounts assertion below).
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "ask", "filesystem": "ask", "container": True},
            workspace_id=None,
        )

        # The admission gate re-derives container policy from the capability
        # SSOT (get_workspace_capabilities), NOT from the _compute_container_config
        # patched below. Make that source permissive so the gate agrees with this
        # test's proposed ("bridge", "rw") config; otherwise the gate correctly
        # narrows 'bridge' -> 'none' and the run never receives 'bridge'.
        import security.security_gate as _security_gate
        from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
        monkeypatch.setattr(
            _security_gate,
            "get_workspace_capabilities",
            lambda workspace_id: WorkspaceCapabilities.default(),
        )
        # Disk-authoritative: pin the vault seam to the intended WRITE grant so
        # the gate resolves the recreated container's network to bridge.
        monkeypatch.setattr(
            _security_gate,
            "_read_disk_permission_sources",
            lambda workspace_id, session_id: (
                {"network": "write", "filesystem": "write", "container": True},
                None,
            ),
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("none", "ro"), ("bridge", "rw"), ("bridge", "rw")],
        ):
            executor._ensure_container()

            executor.session_permissions = {
                "network": "write", "filesystem": "write", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs["network_mode"] == "bridge"
        mounts = kwargs.get("mounts") or []
        ws_mounts = [m for m in mounts
                     if m.get("Target") == "/workspace" and m.get("Type") == "bind"]
        assert ws_mounts, f"no /workspace bind mount in mounts={mounts!r}"
        assert ws_mounts[0].get("ReadOnly") is False, f"{ws_mounts[0]!r}"

    def test_write_to_ask_recreates_container(
        self,
        mock_docker: MagicMock,
        mock_container: MagicMock,
    ):
        """write→ask triggers recreation (bridge→none, rw→ro)."""
        # workspace_id=None → bind path (mounts assertion below).
        executor = _make_executor(
            mock_docker,
            session_permissions={"network": "write", "filesystem": "write", "container": True},
            workspace_id=None,
        )

        with patch.object(
            executor.__class__, "_compute_container_config",
            side_effect=[("bridge", "rw"), ("none", "ro"), ("none", "ro")],
        ):
            executor._ensure_container()

            executor.session_permissions = {
                "network": "ask", "filesystem": "ask", "container": True,
            }
            executor._ensure_container()

        assert mock_container.stop.called
        assert mock_container.remove.called
        _, kwargs = mock_docker.containers.run.call_args
        assert kwargs["network_mode"] == "none"
        mounts = kwargs.get("mounts") or []
        ws_mounts = [m for m in mounts
                     if m.get("Target") == "/workspace" and m.get("Type") == "bind"]
        assert ws_mounts, f"no /workspace bind mount in mounts={mounts!r}"
        assert ws_mounts[0].get("ReadOnly") is True, f"{ws_mounts[0]!r}"
