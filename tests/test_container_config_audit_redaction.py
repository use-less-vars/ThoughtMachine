"""SECURITY: the ``CONTAINER_CONFIG`` audit line must never leak permission values.

``ContainerManager.start`` emits ``_audit("CONTAINER_CONFIG", ...)`` into the
plaintext audit log (``<vault>/container_audit.log``, i.e. plaintext on disk).
That line used to embed the raw ``session_permissions`` dict — real permission
values — so every start() copied the session's grants into the log.

The event must instead carry a SHAPE-ONLY view (a redaction marker, the number
of permission categories and their sorted names) — enough for an operator to
correlate a config change, and nothing that discloses a permission value.

Style mirrors the audit assertions in ``tests/test_host_bash_tool.py``: drive
the real emitter and assert on what was emitted.
"""

from unittest.mock import MagicMock

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager, _redact_permission_shape

SENTINEL = "SENTINEL_SECRET_1234"
FS_SENTINEL = "SENTINEL_FS_5678"


@pytest.fixture
def audit(monkeypatch):
    """Capture ``(event, data)`` pairs emitted through the module audit hook."""
    captured = []

    def _fake_audit_event(event, data):
        captured.append((event, data))

    # ``_audit`` is a lambda resolving ``audit_event`` from the module globals.
    monkeypatch.setattr(container_manager, "audit_event", _fake_audit_event,
                        raising=True)
    return captured


def _make_cm(monkeypatch):
    """A manager whose identity ladder is stubbed out (no Docker, no vault)."""
    cm = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
    cm._containers = {}
    cm.client = MagicMock()
    cm.workspace_path = "/tmp/ws-audit"
    cm.workspace_id = "ws-audit"
    cm.session_id = "sess-audit"
    cm.image = "img"
    cm.workspace_config = {"disk_quota_mb": 0}
    cm.max_containers = 5
    cm.session_permissions = {
        "container": SENTINEL,
        "filesystem": FS_SENTINEL,
        "network": "write",
    }
    # Isolation is resolved before the audit; keep it deterministic.
    cm._compute_config = lambda *a, **k: ("none", "ro")
    # Identity ladder: no record, no existing container -> reaches the
    # isolation/CONTAINER_CONFIG block and then the (stubbed) fresh create.
    cm._migrate_legacy_notes_once = lambda: None
    cm._ensure_name_index = lambda: None
    cm._migrate_records_v3_once = lambda: None
    cm._name_collision = lambda name: False
    cm._record_for_name = lambda name: None
    cm.list_containers = lambda *a, **k: []
    cm._find_by_labels = lambda name: None
    cm._fresh_start = (
        lambda **k: {"id": "c-new", "name": k.get("name"), "status": "created"}
    )
    # Never let a root host short-circuit start() before the audit.
    monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000),
                        raising=True)
    return cm


@pytest.mark.parametrize("permissions", [None, {}, {"container": SENTINEL}])
def test_redact_permission_shape_never_returns_values(permissions):
    shape = _redact_permission_shape(permissions)
    assert SENTINEL not in shape
    assert "count=" in shape


def test_container_config_audit_redacts_permission_values(audit, monkeypatch):
    """The event fires, but no permission VALUE reaches the audit line."""
    cm = _make_cm(monkeypatch)
    cm.start(name="tm-audit", image="img")

    events = [e for e, _ in audit]
    assert "CONTAINER_CONFIG" in events, "CONTAINER_CONFIG audit no longer fires"

    line = next(d for e, d in audit if e == "CONTAINER_CONFIG")
    assert SENTINEL not in line
    assert FS_SENTINEL not in line
    assert "REDACTED" in line
    # Correlation detail survives: counts + category names, no values.
    assert "count=3" in line
    assert "categories=container,filesystem,network" in line
    # The rest of the line is untouched.
    assert "name=tm-audit" in line
    assert "network=none" in line
    assert "workspace=ro" in line
    assert "workspace_id=ws-audit" in line


def test_container_config_audit_handles_empty_permissions(audit, monkeypatch):
    """An empty/absent permission dict still emits a redacted, count-only line."""
    cm = _make_cm(monkeypatch)
    cm.session_permissions = {}
    cm.start(name="tm-audit-empty", image="img")

    line = next(d for e, d in audit if e == "CONTAINER_CONFIG")
    assert "REDACTED" in line
    assert "count=0" in line
