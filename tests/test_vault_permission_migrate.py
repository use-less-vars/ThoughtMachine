"""Tests for scripts/vault_permission_migrate.py.

Every test drives the CLI scripts through subprocess (sys.executable, cwd=repo
root) against a tiny vault built under tmp_path, always passing --vault-root
explicitly so no real or hermetic HOME state is ever touched. The apply-mode
rewrite of edited files is canonical (indent=2, ensure_ascii=False + trailing
newline), so byte-level assertions are only made on .bak files and on files
that must NOT change.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
MIGRATE_SCRIPT = SCRIPTS_DIR / "vault_permission_migrate.py"
CLEANUP_SCRIPT = SCRIPTS_DIR / "vault_permission_cleanup_dryrun.py"

SUBPROCESS_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(script, vault_root, *extra_args):
    """Run one of the scripts against *vault_root* in a fresh subprocess."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(script), "--vault-root", str(vault_root), *extra_args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def run_migrate(vault_root, *extra_args):
    return _run(MIGRATE_SCRIPT, vault_root, *extra_args)


def run_cleanup(vault_root, *extra_args):
    return _run(CLEANUP_SCRIPT, vault_root, *extra_args)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def snapshot(root):
    """Return ({relpath: bytes for *.json}, {relpath: bytes for *.json.bak})."""
    files, baks = {}, {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root))
            (baks if rel.endswith(".json.bak") else files)[rel] = p.read_bytes()
    return files, baks


def assert_ok(result, *needles):
    """Assert rc == 0 and that every ASCII needle appears in stdout."""
    assert result.returncode == 0, (
        "exit %d\nstdout:\n%s\nstderr:\n%s"
        % (result.returncode, result.stdout, result.stderr)
    )
    for needle in needles:
        assert needle in result.stdout, (
            "missing %r in stdout:\n%s" % (needle, result.stdout)
        )


# Fixture builders -----------------------------------------------------------


def build_session_vault(root):
    """workspaces/<id>/sessions/<sid>/permissions.json with legacy + unknown keys."""
    write_json(
        root / "workspaces/w1/sessions/s1/permissions.json",
        {
            "git_read": "write",
            "git_write": "ask",
            "execution": "banned",
            "system": "read",
        },
    )


def build_ceiling_vault(root):
    """workspaces/<id>/config.json with a conflicting existing git key."""
    write_json(
        root / "workspaces/w1/config.json",
        {
            "permissions": {
                "git": "write",
                "git_read": "read",
                "git_write": "ask",
                "execution": "shell",
                "docker": "allow",
                "system": {"halt": True},
            }
        },
    )


def build_mixed_vault(root):
    """Representative vault: changeable session+ceiling, clean, invalid, other."""
    build_session_vault(root)
    build_ceiling_vault(root)
    # Clean modern session file: nothing to do.
    write_json(
        root / "workspaces/w1/sessions/s2/permissions.json",
        {"git": "read", "filesystem": "read"},
    )
    # Invalid JSON: skipped gracefully.
    write_bytes(root / "workspaces/w1/sessions/s3/permissions.json", b'{"broken": ')
    # Non-permission JSON file: skipped.
    write_json(root / "notes/scratch.json", {"foo": 1})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_01_dry_run_parity_identical_stdout(tmp_path):
    """Dry run (no --apply) must be byte-identical to the reference script."""
    vault = tmp_path / "vault"
    build_mixed_vault(vault)
    reference = run_cleanup(vault)
    migrated = run_migrate(vault)
    assert reference.returncode == 0, reference.stderr
    assert migrated.returncode == 0, migrated.stderr
    assert migrated.stdout == reference.stdout, (
        "migrate dry-run stdout differs from reference:\n"
        "--- reference ---\n%s\n--- migrate ---\n%s"
        % (reference.stdout, migrated.stdout)
    )
    assert "files that WOULD change: 2" in migrated.stdout


def test_02_apply_migrates_session_permissions_file(tmp_path):
    """--apply on a session permissions.json drops legacy+unknown, merges git."""
    vault = tmp_path / "vault"
    build_session_vault(vault)
    target = vault / "workspaces/w1/sessions/s1/permissions.json"
    before = target.read_bytes()

    result = run_migrate(vault, "--apply")
    assert_ok(
        result,
        "WARNING: --apply mode",
        "backup: ",
        "=> WOULD CHANGE (applied)",
        "1 file(s) migrated",
        "POST-APPLY VERIFICATION",
    )

    migrated_doc = json.loads(target.read_text(encoding="utf-8"))
    assert migrated_doc == {"git": "ask"}, migrated_doc  # legacy gone, unknown dropped

    bak = vault / "workspaces/w1/sessions/s1/permissions.json.bak"
    assert bak.is_file(), "expected .bak backup to exist"
    assert bak.read_bytes() == before, ".bak must be byte-identical to the original"


def test_03_apply_ceiling_conflict_keeps_existing_git(tmp_path):
    """Ceiling config.json: execution dropped, git conflict keeps existing git."""
    vault = tmp_path / "vault"
    build_ceiling_vault(vault)
    target = vault / "workspaces/w1/config.json"
    before = target.read_bytes()

    result = run_migrate(vault, "--apply")
    assert_ok(result, "CONFLICT", "KEEPING existing git", "1 file(s) migrated")

    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["permissions"] == {
        "git": "write",  # existing value kept over merged 'ask'
        "docker": "allow",  # ceiling key untouched
        "system": {"halt": True},  # ceiling key untouched (kept, review later)
    }, doc
    bak = vault / "workspaces/w1/config.json.bak"
    assert bak.is_file()
    assert bak.read_bytes() == before


def test_04_apply_creates_byte_identical_baks(tmp_path):
    """Every changed file gets a .bak that is a byte-identical copy."""
    vault = tmp_path / "vault"
    build_session_vault(vault)
    build_ceiling_vault(vault)
    originals, _ = snapshot(vault)
    assert set(originals) == {
        "workspaces/w1/sessions/s1/permissions.json",
        "workspaces/w1/config.json",
    }

    result = run_migrate(vault, "--apply")
    assert_ok(result, "2 file(s) migrated")

    files, baks = snapshot(vault)
    assert sorted(baks) == [
        "workspaces/w1/config.json.bak",
        "workspaces/w1/sessions/s1/permissions.json.bak",
    ]
    for rel, bak_rel in (
        ("workspaces/w1/config.json", "workspaces/w1/config.json.bak"),
        (
            "workspaces/w1/sessions/s1/permissions.json",
            "workspaces/w1/sessions/s1/permissions.json.bak",
        ),
    ):
        assert files[rel] != originals[rel], "edited file should have changed"
        assert baks[bak_rel] == originals[rel], ".bak must equal pre-apply bytes"


def test_05_post_apply_dry_run_reports_zero_changes(tmp_path):
    """After --apply, a fresh dry run reports 0 files that WOULD change."""
    vault = tmp_path / "vault"
    build_session_vault(vault)
    assert_ok(run_migrate(vault, "--apply"))
    rerun = run_migrate(vault)
    assert rerun.returncode == 0, rerun.stderr
    assert "files that WOULD change: 0" in rerun.stdout, rerun.stdout


def test_06_second_apply_is_noop(tmp_path):
    """A second --apply is a clean no-op (rc 0, no new edits or bak conflicts)."""
    vault = tmp_path / "vault"
    build_session_vault(vault)
    target = vault / "workspaces/w1/sessions/s1/permissions.json"

    first = run_migrate(vault, "--apply")
    assert_ok(first, "1 file(s) migrated")
    after_first = target.read_bytes()

    second = run_migrate(vault, "--apply")
    assert_ok(second, "0 file(s) migrated")
    assert "backup: " not in second.stdout, "second apply must not re-backup"
    assert target.read_bytes() == after_first, "second apply must not rewrite bytes"


def test_07_untouched_files_stay_byte_identical_after_apply(tmp_path):
    """Clean / invalid / non-permission json files survive --apply untouched."""
    vault = tmp_path / "vault"
    build_session_vault(vault)  # the one file that WILL change
    write_json(
        vault / "workspaces/w1/sessions/s2/permissions.json",
        {"git": "read", "filesystem": "read"},  # clean modern: no change
    )
    invalid = vault / "workspaces/w1/sessions/s3/permissions.json"
    write_bytes(invalid, b'{"broken": ')  # invalid JSON: skipped
    scratch = vault / "notes/scratch.json"
    write_json(scratch, {"foo": 1})  # non-permission location: skipped

    originals, _ = snapshot(vault)
    result = run_migrate(vault, "--apply")
    assert_ok(result, "1 file(s) migrated")

    files, baks = snapshot(vault)
    assert sorted(baks) == ["workspaces/w1/sessions/s1/permissions.json.bak"]
    for rel in (
        "workspaces/w1/sessions/s2/permissions.json",
        "workspaces/w1/sessions/s3/permissions.json",
        "notes/scratch.json",
    ):
        assert files[rel] == originals[rel], (
            "%s must remain byte-identical after --apply" % rel
        )
    # And the changed file really did migrate (git_write 'ask' -> merged 'ask').
    changed = json.loads(
        (vault / "workspaces/w1/sessions/s1/permissions.json").read_text(
            encoding="utf-8"
        )
    )
    assert changed == {"git": "ask"}, changed


def test_08_dry_run_leaves_vault_untouched(tmp_path):
    """Dry runs (reference and migrate) create no .bak and change no bytes."""
    vault = tmp_path / "vault"
    build_mixed_vault(vault)
    originals, _ = snapshot(vault)

    assert run_cleanup(vault).returncode == 0
    assert run_migrate(vault).returncode == 0

    files, baks = snapshot(vault)
    assert baks == {}, "dry run must not create .bak files"
    assert files == originals, "dry run must leave every file byte-identical"


def test_09_nonexistent_vault_root_exits_2(tmp_path):
    """--vault-root pointing at a nonexistent dir aborts with exit code 2."""
    result = run_migrate(tmp_path / "no-such-vault")
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "VAULT ROOT ERROR" in result.stdout + result.stderr
