"""Focused tests for ``tools.host_resource_policy.workspace_allows_host_resources``.

The workspace operator gate is the single source of truth for the
``allow_host_resources`` top-level key in ``workspaces/<id>/config.json``.  The
gate is strict and fail-closed: only the JSON boolean ``true`` enables host
resources -- every other present value, an absent key, a missing file,
malformed JSON, a non-dict body and a missing vault root all report ``False``.

The vault root is resolved from ``THOUGHTMACHINE_VAULT_ROOT`` (see
``thoughtmachine.vault.vault_root``); each test points it at ``tmp_path``.
"""

import json

from tools.host_resource_policy import workspace_allows_host_resources


def _seed(root, monkeypatch, workspace_id, raw):
    """Point the vault root at *root* and write a raw workspace config."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    cfg = root / "workspaces" / workspace_id / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(raw, encoding="utf-8")
    return cfg


def test_literal_true_enables_host_resources(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, "ws1", json.dumps({"allow_host_resources": True}))
    assert workspace_allows_host_resources("ws1") is True


def test_only_literal_true_enables(tmp_path, monkeypatch):
    # Every other present value -- strings, numbers, null, containers -- fails
    # closed (the strictness is the whole point of the old bug this guards).
    for value in ("true", "yes", "false", "", 1, 0, None, [True], {"x": True}):
        _seed(tmp_path, monkeypatch, "ws1",
              json.dumps({"allow_host_resources": value}))
        assert workspace_allows_host_resources("ws1") is False, value


def test_absent_key_is_false(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, "ws1", json.dumps({"permissions": {"git": "read"}}))
    assert workspace_allows_host_resources("ws1") is False


def test_missing_or_empty_workspace_id_is_false(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, "ws1", json.dumps({"allow_host_resources": True}))
    assert workspace_allows_host_resources(None) is False
    assert workspace_allows_host_resources("") is False


def test_missing_file_is_false(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    assert workspace_allows_host_resources("no-such-ws") is False


def test_malformed_json_is_false(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, "ws1", "{ this is not json")
    assert workspace_allows_host_resources("ws1") is False


def test_non_dict_root_is_false(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, "ws1", json.dumps([1, 2, 3]))
    assert workspace_allows_host_resources("ws1") is False


def test_missing_vault_root_dir_is_false(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "absent-vault"))
    assert workspace_allows_host_resources("ws1") is False
