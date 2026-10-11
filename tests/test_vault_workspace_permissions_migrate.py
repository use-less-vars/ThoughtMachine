"""Tests for scripts/vault_workspace_permissions_migrate.py.

Every test drives the CLI script through subprocess (sys.executable, cwd=repo
root) against a tiny vault built under tmp_path, always passing --vault-root
explicitly so no real or hermetic HOME state is ever touched.  The script is
never imported; only its exit code, stdout and on-disk effects are asserted.

The only repo import here is the canonical purpose-preset resolver, used to
compute the EXPECTED permission map for the purpose path (so the test can never
drift from agent/config/workspace_purpose.py).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "vault_workspace_permissions_migrate.py"

SUBPROCESS_TIMEOUT = 120

# The canonical purpose preset resolver -- expected values only, never used to
# drive the script (the script resolves the preset itself).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.config.workspace_purpose import apply_purpose_preset  # noqa: E402

#: The all-banned block the script must write when no purpose preset applies.
ALL_BANNED = {
    "git": "banned",
    "filesystem": "banned",
    "container": False,
    "network": "banned",
    "mcp": "banned",
    "host_bash": "banned",
}

BACKUP_RE = re.compile(r"^config\.json\.bak-(\d{14})$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(vault_root, *extra_args):
    """Run the script against *vault_root* in a fresh subprocess."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--vault-root", str(vault_root), *extra_args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def snapshot(root):
    """Return {relpath: bytes} for every regular file under *root*."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def mtimes(root):
    """Return {relpath: st_mtime_ns} for every regular file under *root*."""
    return {
        str(p.relative_to(root)): p.stat().st_mtime_ns
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def assert_ok(result, *needles):
    assert result.returncode == 0, (
        "exit %d\nstdout:\n%s\nstderr:\n%s"
        % (result.returncode, result.stdout, result.stderr)
    )
    for needle in needles:
        assert needle in result.stdout, (
            "missing %r in stdout:\n%s" % (needle, result.stdout)
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_01_permissions_present_is_untouched(tmp_path):
    """A config.json that already has a `permissions` key is left byte-identical
    (the already-migrated bucket), with no backup written."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    write_json(cfg, {"permissions": {"git": "read"}, "root": "/home/x"})
    before = cfg.read_bytes()

    result = _run(vault, "--apply")
    assert_ok(result, "already-migrated: 1")

    assert cfg.read_bytes() == before, "already-migrated file must be untouched"
    assert not list(vault.rglob("*.bak-*")), "no backup for an untouched file"


def test_02_present_without_permissions_adds_block_preserving_keys(tmp_path):
    """A config.json present WITHOUT `permissions` gains the all-banned block
    while every other key/value is preserved (had-config-no-perms bucket)."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    write_json(
        cfg,
        {"root": "/home/x", "allow_host_resources": True, "capabilities": {"a": 1}},
    )

    result = _run(vault, "--apply")
    assert_ok(result, "had-config-no-perms: 1")

    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert doc["permissions"] == ALL_BANNED, doc
    assert doc["root"] == "/home/x"
    assert doc["allow_host_resources"] is True
    assert doc["capabilities"] == {"a": 1}


def test_03_absent_creates_file_with_block(tmp_path):
    """A workspace dir without a config.json gets one CREATED with the block
    (no-config bucket)."""
    vault = tmp_path / "vault"
    ws_dir = vault / "workspaces/w1"
    ws_dir.mkdir(parents=True)

    result = _run(vault, "--apply")
    assert_ok(result, "no-config: 1")

    cfg = ws_dir / "config.json"
    assert cfg.is_file(), "config.json must be created"
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert doc == {"permissions": ALL_BANNED}, doc


def test_04_purpose_coding_uses_preset_block(tmp_path):
    """A config.json carrying `purpose: "coding"` uses the purpose preset,
    WRAPPED under the `permissions` key."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    write_json(cfg, {"purpose": "coding"})

    result = _run(vault, "--apply")
    assert_ok(
        result,
        "had-config-no-perms: 1",
        "1 workspaces received a purpose preset; 0 received all-banned.",
    )

    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert doc["permissions"] == apply_purpose_preset("coding"), doc
    assert doc["purpose"] == "coding"


def test_05_no_purpose_falls_back_to_all_banned(tmp_path):
    """Without a purpose every key is 'banned' except container which is False."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    write_json(cfg, {"root": "/home/x"})

    result = _run(vault, "--apply")
    assert_ok(
        result,
        "had-config-no-perms: 1",
        "0 workspaces received a purpose preset; 1 received all-banned.",
    )

    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert doc["permissions"] == ALL_BANNED, doc
    assert doc["permissions"]["container"] is False
    assert doc["permissions"]["git"] == "banned"


def test_06_dry_run_writes_nothing(tmp_path):
    """Without --apply no file is created, no backup written and no mtime moves
    (covers both the had-config-no-perms and no-config buckets)."""
    vault = tmp_path / "vault"
    write_json(vault / "workspaces/w1/config.json", {"root": "/home/x"})
    (vault / "workspaces/w2").mkdir(parents=True)  # no config.json -> no-config

    before_files = snapshot(vault)
    before_mtimes = mtimes(vault)

    result = _run(vault)  # dry-run
    assert result.returncode == 0, result.stderr

    assert snapshot(vault) == before_files, "dry run must not create/change files"
    assert mtimes(vault) == before_mtimes, "dry run must not touch mtimes"
    assert not (vault / "workspaces/w2/config.json").exists(), "no new config.json"
    assert not list(vault.rglob("*.bak-*")), "dry run must not write backups"


def test_07_backup_suffix_and_byte_identity(tmp_path):
    """A rewritten config.json gets a sibling .bak-<14 digits> that is a
    byte-exact copy of the pre-write file."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    write_json(cfg, {"root": "/home/x"})
    before = cfg.read_bytes()

    result = _run(vault, "--apply")
    assert_ok(result, "had-config-no-perms: 1")

    baks = list((vault / "workspaces/w1").glob("config.json.bak-*"))
    assert len(baks) == 1, "expected exactly one timestamped backup"
    assert BACKUP_RE.match(baks[0].name), "bad backup name: %r" % baks[0].name
    assert baks[0].read_bytes() == before, ".bak must equal the pre-write bytes"


def test_08_purpose_lookup_failure_logs_and_falls_back(tmp_path):
    """A `purpose` value that makes the preset lookup raise (a JSON list) is
    logged per-workspace, the all-banned block is still written, and exit is 0."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    write_json(cfg, {"purpose": ["coding"]})  # unhashable -> TypeError in lookup

    result = _run(vault, "--apply")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)

    # Per-workspace failure is logged, naming the path and the exception.
    assert str(cfg) in result.stdout, result.stdout
    assert "TypeError" in result.stdout, result.stdout
    assert "0 workspaces received a purpose preset; 1 received all-banned." in result.stdout, (
        result.stdout
    )

    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert doc["permissions"] == ALL_BANNED, doc


def test_09_unreadable_config_reported(tmp_path):
    """A config.json that EXISTS but is not parseable is promoted to the
    `unreadable-config` bucket: it is reported with its own count line, the
    per-workspace `! skipped` diagnostic is preserved, it leaks into no other
    bucket, and a dry run leaves the tree byte-identical and mtime-stable."""
    vault = tmp_path / "vault"
    cfg = vault / "workspaces/w1/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(b'{"broken": ')  # exists, but invalid JSON

    before_files = snapshot(vault)
    before_mtimes = mtimes(vault)

    result = _run(vault)  # dry-run
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)

    # (i) reported under the new bucket ...
    assert "unreadable-config: 1" in result.stdout, result.stdout
    assert str(cfg) in result.stdout, result.stdout
    # ... and the pre-existing per-workspace diagnostic is NOT swallowed.
    assert "! skipped unreadable config" in result.stdout, result.stdout
    # ... and it counts in no other bucket.
    assert "already-migrated: 0" in result.stdout, result.stdout
    assert "had-config-no-perms: 0" in result.stdout, result.stdout
    assert "no-config: 0" in result.stdout, result.stdout

    # (iii) nothing on disk changed.
    assert snapshot(vault) == before_files, "dry run must not create/change files"
    assert mtimes(vault) == before_mtimes, "dry run must not touch mtimes"
    assert not list(vault.rglob("*.bak-*")), "dry run must not write backups"

