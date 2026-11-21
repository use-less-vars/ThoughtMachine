"""Drift display parity -- the container-record ``drift`` field must resolve its
policy with the SAME (fail-closed) capabilities the gate enforces.

The enforcement path resolves workspace capabilities through
``security.security_gate.get_workspace_capabilities``, which FAILS CLOSED to the
deny-all sentinel (``filesystem_write`` / ``allow_docker`` / ``allow_network`` /
``git_available`` all ``False``) when ``workspaces/<id>/capabilities.json`` is
missing or unparsable.

The drift display used to bypass that resolver: it read
``load_workspace_capabilities(ws)`` and fell back to
``WorkspaceCapabilities.default()`` -- the fully permissive profile -- so for a
workspace the gate denies, the record panel evaluated the *policy* axis against
a profile the workspace does not have (drift reported as "clean" relative to a
policy nobody enforces).  These tests pin the route to the gate's own answer for
a missing file, keep the opposite direction covered (a SAVED capabilities file
is honoured, so the fail-closed value is not hardcoded), and pin the honesty
field that says WHAT the finding compares: the record's own RECORDED intent
(``drift_subject == "record_intent"``), never a comparison against the calling
session's live grants.

Fixture style follows ``tests/test_container_record_routes.py``: a bare FastAPI
app with only that router mounted, and a hermetic vault under ``tmp_path``
(``THOUGHTMACHINE_VAULT_ROOT``) so the real vault is never touched.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.security_gate import get_workspace_capabilities  # noqa: E402
from thoughtmachine.container_record import RECORD_LABEL_KEY, api  # noqa: E402
from web_ui.backend import container_record_routes  # noqa: E402

WS_ID = "ws-drift-parity"

# The gate's fail-closed sentinel, spelled out: the four enforcing booleans are
# the exact negation of ``WorkspaceCapabilities.default()``.
FAIL_CLOSED_CAPS = {
    "allowed_tools": [],
    "blocked_tools": [],
    "allowed_providers": [],
    "max_context_length": 0,
    "max_conversation_turns": 0,
    "allowed_file_extensions": [],
    "max_file_size_bytes": 0,
    "allow_network": False,
    "allow_docker": False,
    "filesystem_write": False,
    "git_available": False,
    "allowed_workspace_dirs": ["."],
    "extra": {},
}

# A saved capabilities file that GRANTS everything: the drift display must
# honour it (the fail-closed value above must not be hardcoded).
PERMISSIVE_CAPS_FILE = {
    "allow_network": True,
    "allow_docker": True,
    "filesystem_write": True,
    "git_available": True,
}


# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeContainer:
    """Minimal stand-in for a docker-py ``Container`` (read-only surface)."""

    def __init__(self, container_id, labels=None, attrs=None):
        self.id = container_id
        self.labels = dict(labels or {})
        self.attrs = dict(attrs or {})


class _FakeContainerCollection:
    def __init__(self, containers):
        self._containers = list(containers)

    def list(self, all=True, filters=None):  # noqa: A002 - mirrors docker-py
        return list(self._containers)


class _FakeDockerClient:
    def __init__(self, containers):
        self.containers = _FakeContainerCollection(containers)


def _fake_docker(monkeypatch, containers):
    fake = _FakeDockerClient(containers)
    monkeypatch.setattr("docker.from_env", lambda: fake)
    return fake


def _spy_detect(monkeypatch):
    """Patch the detector the route calls lazily; return the captured kwargs."""
    from thoughtmachine.container_record import drift as drift_mod

    captured = []

    def _spy(record, containers, **kwargs):
        captured.append({"record_id": record.id, **kwargs})
        return []

    monkeypatch.setattr(drift_mod, "detect_record_drift", _spy)
    return captured


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A hermetic vault whose root is what ``vault_root()`` returns."""
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def client(vault):
    app = FastAPI()
    app.include_router(container_record_routes.router)
    return TestClient(app)


def _seed_record(vault):
    api.create_record(
        WS_ID, "ephemeral", "workspace-owned", id="rec-a", vault_root=vault
    )
    api.attach_container(WS_ID, "rec-a", "docker-aaa", vault_root=vault)


def _capabilities_path(vault: Path) -> Path:
    return Path(vault) / "workspaces" / WS_ID / "capabilities.json"


def _write_capabilities(vault: Path, payload) -> Path:
    path = _capabilities_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── the policy axis must be evaluated with the GATE's capabilities ───────────


def test_drift_uses_gate_fail_closed_caps_when_file_missing(
    vault, client, monkeypatch
):
    """No capabilities.json -> the gate's deny-all sentinel, never the default."""
    _seed_record(vault)
    _fake_docker(
        monkeypatch,
        [_FakeContainer("docker-aaa", labels={RECORD_LABEL_KEY: "rec-a"}, attrs={})],
    )
    captured = _spy_detect(monkeypatch)

    resp = client.get(
        "/api/container-records/rec-a", params={"workspace_id": WS_ID}
    )
    assert resp.status_code == 200
    assert not _capabilities_path(vault).exists()

    assert len(captured) == 1
    caps = captured[0]["capabilities"]
    assert caps is not None
    # The gate's own answer for this workspace...
    assert caps.to_dict() == get_workspace_capabilities(WS_ID).to_dict()
    # ...which for a missing file is the RESTRICTIVE profile, not the
    # fully-permissive ``WorkspaceCapabilities.default()``.
    assert caps.to_dict() == FAIL_CLOSED_CAPS
    assert caps.allow_docker is False
    assert caps.allow_network is False
    assert caps.filesystem_write is False
    assert caps.git_available is False


def test_drift_honours_a_saved_capabilities_file(vault, client, monkeypatch):
    """A saved capabilities file is what the policy axis is evaluated against."""
    _seed_record(vault)
    _write_capabilities(vault, PERMISSIVE_CAPS_FILE)
    _fake_docker(
        monkeypatch,
        [_FakeContainer("docker-aaa", labels={RECORD_LABEL_KEY: "rec-a"}, attrs={})],
    )
    captured = _spy_detect(monkeypatch)

    resp = client.get(
        "/api/container-records/rec-a", params={"workspace_id": WS_ID}
    )
    assert resp.status_code == 200
    assert len(captured) == 1
    caps = captured[0]["capabilities"]
    assert caps.to_dict() == get_workspace_capabilities(WS_ID).to_dict()
    assert caps.to_dict()["allow_network"] is True
    assert caps.to_dict()["filesystem_write"] is True


# ── honesty: the response says WHAT the finding compares ────────────────────


def test_response_states_the_drift_subject(vault, client, monkeypatch):
    """``drift_subject`` names the recorded-intent basis of the findings."""
    _seed_record(vault)
    _fake_docker(
        monkeypatch,
        [_FakeContainer("docker-aaa", labels={RECORD_LABEL_KEY: "rec-a"}, attrs={})],
    )
    _spy_detect(monkeypatch)

    body = client.get(
        "/api/container-records/rec-a", params={"workspace_id": WS_ID}
    ).json()
    assert body["drift_subject"] == container_record_routes.DRIFT_SUBJECT
    assert body["drift_subject"] == "record_intent"

    # the list route carries the same statement
    listed = client.get(
        "/api/container-records", params={"workspace_id": WS_ID}
    ).json()
    assert listed["records"][0]["drift_subject"] == "record_intent"


def test_drift_subject_is_stated_even_when_drift_is_unresolved(
    vault, client, monkeypatch
):
    """An unresolved ``drift`` (docker down) still names what WOULD be compared."""
    _seed_record(vault)

    def _no_docker():
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr("docker.from_env", _no_docker)

    body = client.get(
        "/api/container-records/rec-a", params={"workspace_id": WS_ID}
    ).json()
    assert body["drift"] is None
    assert body["drift_subject"] == "record_intent"
