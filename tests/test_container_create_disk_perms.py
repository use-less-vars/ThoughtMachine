"""RED-first test: the container-create path must resolve its network mode from
the DISK permission store (session grants + workspace ceiling), not from the
legacy in-memory permission mirror handed in by the caller.

Bug: `bug/container-create-uses-legacy-permission-mirror`.

The legacy mirror must remain byte-for-byte for callers that carry no
session_id/workspace_id (see ``test_legacy_mirror_unchanged_without_ids``).
"""

import json
import os
import sys
from unittest import mock

import pytest

# Make the repository root importable when running this file directly.
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from infra.container_registry import (  # noqa: E402
    ContainerRegistry,
    _resolve_network_mode_via_gate,
)


# ---------------------------------------------------------------------------
# Minimal fake docker surface (mirrors tests/test_container_registry.py)
# ---------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, name):
        self.name = name
        self.id = f"id-{name}"
        self.stopped = False
        self.removed = False

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class FakeClient:
    def __init__(self):
        self.containers = mock.Mock()
        self.images = mock.Mock()
        self.containers.run = mock.Mock(
            side_effect=lambda image, command=None, **kwargs: FakeContainer(
                kwargs["name"]
            )
        )
        self.containers.get = mock.Mock(side_effect=lambda name: FakeContainer(name))
        self.containers.list = mock.Mock(return_value=[])

    def ping(self):
        return True


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def registry(fake_client):
    return ContainerRegistry(docker_client=fake_client, feature_flag_check=lambda: True)


@pytest.fixture
def permissive_caps(monkeypatch):
    """Permissive workspace capabilities so the ONLY variable under test is the
    session-grant source (disk vs legacy mirror).  This patches the capability
    loader, NOT the permission store -- it deliberately cannot rescue the disk
    session/ceiling read."""
    from security.security_gate import WorkspaceCapabilities

    monkeypatch.setattr(
        "security.security_gate.get_workspace_capabilities",
        lambda workspace_id=None: WorkspaceCapabilities.default(),
    )


def _run_kwargs(fake_client):
    call = fake_client.containers.run.call_args
    merged = {}
    if call.args:
        merged["image"] = call.args[0]
    if len(call.args) > 1:
        merged["command"] = call.args[1]
    merged.update(call.kwargs)
    return merged


def _write_disk_grants(vault, workspace_id, session_id, grants):
    """Write the session sidecar + the workspace ceiling config into the vault."""
    from thoughtmachine.permission_store import write_session_permissions

    write_session_permissions(vault, workspace_id, session_id, grants)
    cfg_dir = vault / "workspaces" / workspace_id
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({"permissions": dict(grants)}))


# ---------------------------------------------------------------------------
# RED: create-path must read the disk, not the mirror
# ---------------------------------------------------------------------------


def test_request_container_reads_disk_not_mirror(
    registry, fake_client, permissive_caps, hermetic_vault
):
    # Disk grants network write; the legacy mirror says the OPPOSITE.
    _write_disk_grants(
        hermetic_vault,
        "ws-disk",
        "sess-disk",
        {"network": "write", "filesystem": "write"},
    )
    registry.request_container(
        "w",
        "sess-disk",
        {"network": "banned", "filesystem": "read"},
        workspace_id="ws-disk",
    )
    # Disk is the SSoT -> "bridge"; pre-fix the stale mirror yields "none".
    assert _run_kwargs(fake_client)["network_mode"] == "bridge"


def test_request_container_disk_denies_even_when_mirror_permits(
    registry, fake_client, permissive_caps, hermetic_vault
):
    # Disk grants NOTHING (empty sidecar); the legacy mirror claims network write.
    _write_disk_grants(hermetic_vault, "ws-lock", "sess-lock", {})
    registry.request_container(
        "w",
        "sess-lock",
        {"network": "write", "filesystem": "write"},
        workspace_id="ws-lock",
    )
    # Disk fail-closed -> "none"; pre-fix the mirror naively permits "bridge".
    assert _run_kwargs(fake_client)["network_mode"] == "none"


def test_legacy_mirror_unchanged_without_ids(permissive_caps):
    # No session_id/workspace_id threaded -> the 2-arg legacy mirror is used
    # byte-for-byte; "network": "write" still resolves to "bridge".
    assert _resolve_network_mode_via_gate("ws-legacy", {"network": "write"}) == "bridge"
