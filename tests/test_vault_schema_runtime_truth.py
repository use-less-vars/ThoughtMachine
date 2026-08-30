"""Truth tests: repo schema manifest declares the runtime-truth vault layout.

Pins the manifest <-> resources agreement for the checksystem allowlist
(including the new ``runtime_state`` query) and the presence of the
runtime-state / session / prompt entries so the manifest stays in sync
with the actual vault layout the runtime reads and writes.
"""

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWLIST_SHA256 = "201f3e9839ceaca5e3241bd914d2343c0059c6efbe35f49594650686b6ec335f"

_REQUIRED_ENTRIES = (
    "vault_version.json",
    "system/providers.json",
    "system/factory_defaults.json",
    "system/checksystem_allowlist.json",
    "state/session_registry.json",
    "state/workspace_registry.json",
    "user/defaults.json",
)

_KNOWN_PATTERNS = (
    "sessions/*/config.json",
    "workspaces/*/config.json",
    "workspaces/*/workers.json",
    "workspaces/*/capabilities.json",
    "workspaces/*/mcp_servers.json",
    "workspaces/*/defaults.json",
    "logs/*",
)

_RUNTIME_TRUTH_ENTRIES = (
    "state/open_sessions.json",
    "state/.current_session",
    "logs/event_log.jsonl",
    "workspaces/*/Dockerfile",
    "workspaces/*/domain_allowlist.json",
    "workspaces/*/sessions/*.json",
    "global/.version",
    "global/system/*.md",
    "global/user/my_notes.md",
    "default_system_prompt.txt",
    "engineer_system_prompt.txt",
    "system/default_system_prompt.txt",
    "system/engineer_system_prompt.txt",
)


def _manifest():
    return json.loads(
        (REPO_ROOT / "agent/config/schema_manifest.json").read_text(encoding="utf-8")
    )


def _resource_allowlist():
    return json.loads(
        (REPO_ROOT / "resources/checksystem_allowlist.json").read_text(encoding="utf-8")
    )


def test_schema_manifest_parses_and_required_entries_present():
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    assert isinstance(manifest["files"], dict)
    files = manifest["files"]
    for relpath in _REQUIRED_ENTRIES:
        assert relpath in files, relpath
        assert files[relpath]["required"] is True


def test_manifest_allowlist_safe_default_matches_resource():
    manifest = _manifest()
    resource = _resource_allowlist()
    safe = manifest["files"]["system/checksystem_allowlist.json"]["safe_default"]

    assert safe == resource
    assert resource["allowlist"] == sorted(resource["allowlist"])
    assert "runtime_state" in resource["allowlist"]
    assert "vault_status" in resource["allowlist"]
    assert resource["sha256"] == hashlib.sha256(
        "\n".join(sorted(str(e) for e in resource["allowlist"])).encode()
    ).hexdigest()
    assert resource["sha256"] == _ALLOWLIST_SHA256


def test_schema_manifest_declares_known_patterns():
    files = _manifest()["files"]
    for pattern in _KNOWN_PATTERNS:
        assert pattern in files, pattern
        assert files[pattern].get("pattern") is True


def test_schema_manifest_declares_runtime_truth_entries():
    files = _manifest()["files"]
    for relpath in _RUNTIME_TRUTH_ENTRIES:
        assert relpath in files, relpath
