"""Schema + repair-engine guards for the engine-read workspace config keys.

``workspaces/<id>/config.json`` is read by several engine components, not just
by tools: ``root`` is the host_bash cwd-containment anchor
(``thoughtmachine/host_port.py::_workspace_root``) and ``git_identity`` is the
commit author pin (``tools/git_write_tool.py::_workspace_git_identity``).  Both
used to be *undeclared* in the schema manifest, which made the drift checker
report them as unknown keys and let ``vault_repair --apply`` quarantine them --
silently breaking ``host_bash`` and commits.

Two pins:

1. the manifest declares both keys on the ``workspaces/*/config.json`` entry;
2. ``thoughtmachine.vault_repair`` treats engine-read keys as manual-review at
   BOTH the reporting and the mutation layer, so ``--apply`` can never remove
   them even from a vault that is out of schema again.
"""

import json
from pathlib import Path

import pytest

from thoughtmachine import vault_repair as vr

REPO_ROOT = Path(__file__).resolve().parent.parent

_ENGINE_READ_CONFIG = {
    "root": "/srv/workspaces/ws1",
    "git_identity": {"name": "Ada Lovelace", "email": "ada@example.com"},
}


def _manifest():
    return json.loads(
        (REPO_ROOT / "agent/config/schema_manifest.json").read_text(encoding="utf-8")
    )


def test_workspace_config_manifest_declares_root_and_git_identity():
    """`root` and `git_identity` are declared, optional fields of the entry."""
    entry = _manifest()["files"]["workspaces/*/config.json"]
    assert entry["root_type"] == "dict"
    fields = entry["fields"]

    assert fields["root"]["type"] == "string"
    assert fields["root"]["required"] is False
    assert fields["git_identity"]["type"] == "dict"
    assert fields["git_identity"]["required"] is False


def test_engine_read_keys_never_quarantined_by_apply(tmp_path):
    """Undeclared engine-read keys are manual_review and refused by the fixer."""
    drift_report = {
        "files": {
            "workspaces/ws1/config.json": {
                "status": "drift",
                "drifts": [
                    {"issue": "undeclared field", "field": "root"},
                    {"issue": "undeclared field", "field": "git_identity"},
                ],
            }
        }
    }
    sink = vr._IssueSink()
    vr._collect_drift_issues(drift_report, {"files": {}}, sink, [])

    reported = {issue["path_in_file"]: issue for issue in sink.issues}
    assert set(reported) == {"root", "git_identity"}
    for issue in reported.values():
        assert issue["category"] == "unknown_top_key"
        assert issue["classification"] == "manual_review"

    # Mutation layer: the fixer refuses, leaving file bytes untouched.
    vault = tmp_path / "vault"
    config = vault / "workspaces/ws1/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(_ENGINE_READ_CONFIG), encoding="utf-8")
    before = config.read_bytes()

    for key in ("root", "git_identity"):
        with pytest.raises(ValueError, match="engine-read key"):
            vr._fix_issue(
                vault,
                vault / "logs/quarantine",
                {
                    "file": "workspaces/ws1/config.json",
                    "path_in_file": key,
                    "category": "unknown_top_key",
                },
                {},
            )

    assert config.read_bytes() == before
    assert not (vault / "logs/quarantine").exists()
