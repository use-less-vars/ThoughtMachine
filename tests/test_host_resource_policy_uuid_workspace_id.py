"""Regression tests: a ``uuid.UUID`` workspace id must reach the same disk
truth as its ``str`` form.

``tools.host_resource_policy.load_workspace_config`` promised "never raises"
but performed a raw
``Path(vault_root()) / "workspaces" / workspace_id / "config.json"`` join; a
non-``str`` id such as ``uuid.UUID`` made pathlib raise ``TypeError`` -- which
escaped the reader's fail-closed ``except (OSError, ValueError)`` and
propagated to the caller.  The reader now coerces the id via ``str`` before
the join (the ``thoughtmachine.permission_store._coerce_id`` idiom), so a UUID
workspace id resolves to exactly the same result as its string form.

In production the id reaching the seam is always ``str`` (or ``None``) at
every caller, so this is a robustness/contract fix with zero production
behaviour change -- the only changed outcome is a UUID id moving from a raised
``TypeError`` to the on-disk truth.
"""

import json
import uuid

from tools.host_resource_policy import (
    load_workspace_config,
    workspace_allows_host_resources,
)


def _seed(root, monkeypatch, workspace_id, raw):
    """Point the vault root at *root* and write a raw workspace config."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    cfg = root / "workspaces" / str(workspace_id) / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(raw, encoding="utf-8")
    return cfg


def test_uuid_workspace_id_does_not_raise(tmp_path, monkeypatch):
    # A UUID id with a malformed body must be fail-closed (False), never a
    # TypeError escaping the reader.
    ws_uuid = uuid.uuid4()
    _seed(tmp_path, monkeypatch, ws_uuid, "{ this is not json")
    assert workspace_allows_host_resources(ws_uuid) is False


def test_uuid_workspace_id_reads_disk_truth(tmp_path, monkeypatch):
    ws_uuid = uuid.uuid4()
    _seed(tmp_path, monkeypatch, ws_uuid, json.dumps({"allow_host_resources": True}))
    assert workspace_allows_host_resources(ws_uuid) is True


def test_uuid_absent_key_is_false(tmp_path, monkeypatch):
    ws_uuid = uuid.uuid4()
    _seed(tmp_path, monkeypatch, ws_uuid, json.dumps({"permissions": {"git": "read"}}))
    assert workspace_allows_host_resources(ws_uuid) is False


def test_uuid_missing_config_is_false(tmp_path, monkeypatch):
    ws_uuid = uuid.uuid4()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    assert workspace_allows_host_resources(ws_uuid) is False


def test_uuid_and_str_forms_agree(tmp_path, monkeypatch):
    # Enabled config: UUID and its str form both yield True.
    enabled = uuid.uuid4()
    _seed(tmp_path, monkeypatch, enabled, json.dumps({"allow_host_resources": True}))
    assert workspace_allows_host_resources(enabled) is True
    assert workspace_allows_host_resources(str(enabled)) is True

    # Absent key: UUID and its str form both yield False.
    disabled = uuid.uuid4()
    _seed(tmp_path, monkeypatch, disabled, json.dumps({}))
    assert workspace_allows_host_resources(disabled) is False
    assert workspace_allows_host_resources(str(disabled)) is False


def test_load_workspace_config_uuid_returns_dict(tmp_path, monkeypatch):
    ws_uuid = uuid.uuid4()
    _seed(tmp_path, monkeypatch, ws_uuid, json.dumps({"allow_host_resources": True}))
    assert load_workspace_config(ws_uuid) == {"allow_host_resources": True}
