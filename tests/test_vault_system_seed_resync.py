"""Tests for factory-owned seed re-sync at bootstrap.

``thoughtmachine.vault._sync_factory_owned_seeds()`` (called from
``ensure_vault_defaults()``) re-syncs exactly two factory-owned seed copies --
``system/checksystem_allowlist.json`` and ``system/factory_defaults.json`` --
from the shipped ``resources/`` files when their *content* differs.  Everything
else in the vault stays the operator's.

Fully hermetic: the vault root is redirected with
``THOUGHTMACHINE_VAULT_ROOT`` and the shipped seeds are a throw-away fake
``resources/`` tree, so the real ``~/.thoughtmachine`` is never touched.
"""

import hashlib
import json
import logging
import os

import pytest

import thoughtmachine.vault as vault

RESYNC_MARKER = "re-synced from shipped seed"
VAULT_LOGGER = "thoughtmachine.vault"

# Two allowlists whose only difference is an entry shipped by a later version --
# the upgrade scenario the re-sync exists for.
ALLOWLIST_OLD = ("vault_status", "runtime_state")
ALLOWLIST_NEW = ("vault_status", "runtime_state", "entry_added_in_upgrade")

FACTORY_DEFAULTS = {
    "version": "1",
    "config": {"max_turns": 50, "temperature": 0.7},
}

SYSTEM_FILES = {
    "factory_defaults.json",
    "checksystem_allowlist.json",
    "providers.json",
    "resource_catalog.json",
    "default_system_prompt.txt",
    "engineer_system_prompt.txt",
    ".vault_version",
}


def _allowlist_text(entries) -> str:
    """Serialise an allowlist whose ``sha256`` matches its entries."""
    payload = {
        "allowlist": list(entries),
        "sha256": hashlib.sha256(
            "\n".join(sorted(entries)).encode("utf-8")
        ).hexdigest(),
    }
    return json.dumps(payload, indent=2)


def _make_resources(tmp_path, *, allowlist=ALLOWLIST_OLD):
    """Build a fake shipped ``resources/`` tree holding every bootstrap seed."""
    res = tmp_path / "resources"
    # ``resources/`` and ``agent/`` are siblings under the repo root, and the
    # catalog seed is resolved as ``resources_dir.parent / agent/config/...``.
    res.mkdir(parents=True)
    (tmp_path / "agent" / "config").mkdir(parents=True)
    (res / "checksystem_allowlist.json").write_text(
        _allowlist_text(allowlist), encoding="utf-8"
    )
    (res / "factory_defaults.json").write_text(
        json.dumps(FACTORY_DEFAULTS, indent=2), encoding="utf-8"
    )
    (res / "default_providers.json").write_text('{"profiles": []}\n', encoding="utf-8")
    (res / "default_system_prompt.txt").write_text("shipped prompt\n", encoding="utf-8")
    (res / "engineer_system_prompt.txt").write_text(
        "shipped engineer prompt\n", encoding="utf-8"
    )
    (tmp_path / "agent" / "config" / "resource_catalog.json").write_text(
        '{"resources": []}\n', encoding="utf-8"
    )
    return res


def _snapshot(root):
    """Content + mtime of every file under *root* (proves zero writes)."""
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def seeded_root(tmp_path, monkeypatch):
    """Redirect the vault root to a throw-away directory."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    # The allowlist cache is module-global; isolate it per test.
    monkeypatch.setattr(vault, "_checksystem_allowlist_cache", None)
    return root


# ── (a) fresh vault ─────────────────────────────────────────────────────────


def test_fresh_vault_deploys_both_seeds_without_warning(
    tmp_path, seeded_root, caplog
):
    res = _make_resources(tmp_path)

    with caplog.at_level(logging.WARNING, logger=VAULT_LOGGER):
        created = vault.ensure_vault_defaults(res)

    allowlist = seeded_root / "system" / "checksystem_allowlist.json"
    defaults = seeded_root / "system" / "factory_defaults.json"
    assert allowlist.is_file()
    assert defaults.is_file()
    assert allowlist.read_bytes() == (res / "checksystem_allowlist.json").read_bytes()
    assert defaults.read_bytes() == (res / "factory_defaults.json").read_bytes()
    assert (allowlist.stat().st_mode & 0o777) == 0o644
    # The helper writes them, so they are reported exactly once (the following
    # _copy_resource() calls find the destination present and skip).
    assert [p for p in created if p.endswith("checksystem_allowlist.json")] == [
        str(allowlist)
    ]
    assert [p for p in created if p.endswith("factory_defaults.json")] == [
        str(defaults)
    ]
    # Nothing was overwritten, so nothing is logged.
    assert RESYNC_MARKER not in caplog.text
    # ... and no extra bookkeeping file was introduced into the vault.
    assert {p.name for p in (seeded_root / "system").iterdir()} == SYSTEM_FILES


# ── (b) idempotency ─────────────────────────────────────────────────────────


def test_second_call_writes_nothing(tmp_path, seeded_root, caplog):
    res = _make_resources(tmp_path)
    vault.ensure_vault_defaults(res)
    before = _snapshot(seeded_root)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=VAULT_LOGGER):
        second = vault.ensure_vault_defaults(res)

    assert second == []
    # Byte-for-byte AND mtime-for-mtime: nothing was rewritten (a rewrite would
    # bust get_checksystem_allowlist()'s mtime-keyed cache).
    assert _snapshot(seeded_root) == before
    assert RESYNC_MARKER not in caplog.text


# ── (c) drifted allowlist is re-synced and reported ─────────────────────────


def test_upgraded_shipped_allowlist_reaches_the_vault(tmp_path, seeded_root, caplog):
    res = _make_resources(tmp_path, allowlist=ALLOWLIST_OLD)
    vault.ensure_vault_defaults(res)

    live = seeded_root / "system" / "checksystem_allowlist.json"
    assert json.loads(live.read_text(encoding="utf-8"))["allowlist"] == list(
        ALLOWLIST_OLD
    )
    # Warm the cache with the old entries (the real-world stale state).
    vault._checksystem_allowlist_cache = None
    assert set(vault.get_checksystem_allowlist()) == set(ALLOWLIST_OLD)

    # A later version ships an allowlist with one more entry; the vault copy
    # still holds the old content, so it must be re-synced.
    (res / "checksystem_allowlist.json").write_text(
        _allowlist_text(ALLOWLIST_NEW), encoding="utf-8"
    )
    # Make the seed's mtime strictly newer so the mtime-keyed cache cannot
    # legitimately serve the stale entries (deterministic on coarse filesystems).
    future = live.stat().st_mtime + 10
    os.utime(res / "checksystem_allowlist.json", (future, future))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=VAULT_LOGGER):
        created = vault.ensure_vault_defaults(res)

    assert str(live) in created
    assert live.read_bytes() == (res / "checksystem_allowlist.json").read_bytes()
    # Exactly ONE warning, naming the file.
    warnings = [r for r in caplog.records if RESYNC_MARKER in r.getMessage()]
    assert len(warnings) == 1
    assert "system/checksystem_allowlist.json" in warnings[0].getMessage()
    # The runtime now sees the shipped entries (fresh copy => new mtime).
    assert set(vault.get_checksystem_allowlist()) == set(ALLOWLIST_NEW)


def test_drifted_allowlist_is_overwritten_without_double_write(
    tmp_path, seeded_root, caplog
):
    res = _make_resources(tmp_path)
    vault.ensure_vault_defaults(res)

    live = seeded_root / "system" / "checksystem_allowlist.json"
    live.write_text(_allowlist_text(("only_vault_status",)), encoding="utf-8")

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=VAULT_LOGGER):
        created = vault.ensure_vault_defaults(res)

    # Written once (by the helper) -- not twice in the same boot.
    assert created.count(str(live)) == 1
    assert json.loads(live.read_text(encoding="utf-8"))["allowlist"] == list(
        ALLOWLIST_OLD
    )


# ── (d) formatting-only difference is a match ───────────────────────────────


def test_formatting_only_difference_is_not_resynced(
    tmp_path, seeded_root, caplog
):
    res = _make_resources(tmp_path)
    vault.ensure_vault_defaults(res)

    live = seeded_root / "system" / "checksystem_allowlist.json"
    reformatted = json.dumps(
        json.loads(live.read_text(encoding="utf-8")), indent=8, sort_keys=True
    )
    live.write_text(reformatted, encoding="utf-8")
    before = (live.read_bytes(), live.stat().st_mtime_ns)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=VAULT_LOGGER):
        assert vault.ensure_vault_defaults(res) == []

    assert (live.read_bytes(), live.stat().st_mtime_ns) == before
    assert RESYNC_MARKER not in caplog.text


# ── missing shipped seed ────────────────────────────────────────────────────


def test_missing_shipped_seed_is_skipped_silently(tmp_path, seeded_root, caplog):
    res = _make_resources(tmp_path)
    (res / "checksystem_allowlist.json").unlink()
    (res / "factory_defaults.json").unlink()

    with caplog.at_level(logging.WARNING, logger=VAULT_LOGGER):
        assert vault._sync_factory_owned_seeds(seeded_root, res) == []

    assert not (seeded_root / "system" / "checksystem_allowlist.json").exists()
    assert not (seeded_root / "system" / "factory_defaults.json").exists()
    assert RESYNC_MARKER not in caplog.text


# ── (e) operator-owned files are never re-synced ────────────────────────────


def test_operator_owned_files_are_never_resynced(tmp_path, seeded_root):
    res = _make_resources(tmp_path)
    vault.ensure_vault_defaults(res)

    providers = seeded_root / "system" / "providers.json"
    prompt = seeded_root / "system" / "default_system_prompt.txt"
    engineer_prompt = seeded_root / "system" / "engineer_system_prompt.txt"
    catalog = seeded_root / "system" / "resource_catalog.json"
    providers.write_text('{"profiles": [{"id": "operator"}]}', encoding="utf-8")
    prompt.write_text("operator customised prompt\n", encoding="utf-8")
    engineer_prompt.write_text("operator engineer prompt\n", encoding="utf-8")
    catalog.write_text('{"resources": [{"id": "own"}]}', encoding="utf-8")
    before = _snapshot(seeded_root)

    assert vault.ensure_vault_defaults(res) == []

    assert _snapshot(seeded_root) == before
    assert "operator" in providers.read_text(encoding="utf-8")
    assert prompt.read_text(encoding="utf-8") == "operator customised prompt\n"
    assert engineer_prompt.read_text(encoding="utf-8") == "operator engineer prompt\n"
    assert "own" in catalog.read_text(encoding="utf-8")


# ── (f) trust anchors and worker templates untouched ────────────────────────


def test_trust_anchors_and_worker_templates_untouched(tmp_path, seeded_root):
    res = _make_resources(tmp_path)
    (res / "default_dockerfile.txt").write_text("# shipped runtime\n", encoding="utf-8")
    (res / "git_resource_overlay_dockerfile.txt").write_text(
        "# shipped overlay\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("shipped==1.0\n", encoding="utf-8")

    build_dir = seeded_root / "docker" / "resource"
    build_dir.mkdir(parents=True)
    (build_dir / "default_runtime.Dockerfile").write_text(
        "# operator tampered runtime\n", encoding="utf-8"
    )
    (build_dir / "git_overlay.Dockerfile").write_text(
        "# operator tampered overlay\n", encoding="utf-8"
    )
    (build_dir / "requirements.txt").write_text("operator-pinned==9.9\n", encoding="utf-8")

    templates = seeded_root / "worker_templates"
    templates.mkdir()
    (templates / "custom.md").write_text("operator template\n", encoding="utf-8")

    build_before = _snapshot(build_dir)
    vault.ensure_vault_defaults(res)
    vault.ensure_resource_build_files(res, tmp_path)

    assert _snapshot(build_dir) == build_before
    assert (build_dir / "default_runtime.Dockerfile").read_text(
        encoding="utf-8"
    ) == "# operator tampered runtime\n"
    assert [p.name for p in templates.iterdir()] == ["custom.md"]
