"""
Tests for worker template merging during workspace bootstrap.

Verifies that ``ensure_workspace_dirs()`` correctly merges echo + template workers
into ``workers.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from thoughtmachine.workspace_capabilities import (
    _build_default_workers,
    _default_echo_worker,
    _load_template_workers,
    _user_dir,
    ensure_workspace_dirs,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_user_dir():
    """Temporarily redirect ``~/.thoughtmachine`` to a temp directory."""
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(Path, "home", return_value=Path(tmp)):
            yield Path(tmp)


@pytest.fixture
def with_template_dir(temp_user_dir):
    """Set up a fake worker_templates directory with a default template JSON file."""
    template_dir = _user_dir() / "worker_templates"
    template_dir.mkdir(parents=True, exist_ok=True)

    templates = {
        "default.json": {
            "name": "default",
            "description": "Default general-purpose worker",
            "system_prompt": "You are a capable autonomous sub-agent.",
            "tools": [],
            "worker_permissions": {},
            "timeout_seconds": 120,
            "temperature": 0.2,
        },
    }

    for filename, content in templates.items():
        (template_dir / filename).write_text(
            json.dumps(content, indent=2), encoding="utf-8"
        )

    return template_dir


# ── Template loading tests ────────────────────────────────────────────────────


class TestLoadTemplateWorkers:
    """Tests for _load_template_workers()."""

    def test_uses_user_template_dir_first(self, with_template_dir):
        """When user templates dir exists and has files, it takes priority."""
        workers = _load_template_workers()
        names = {w["name"] for w in workers}
        assert names == {"default"}

    def test_invalid_template_is_skipped(self, with_template_dir, caplog):
        """A template with missing required fields logs a warning and is skipped."""
        # Write an invalid template
        bad_template = with_template_dir / "bad.json"
        bad_template.write_text('{"name": "bad"}', encoding="utf-8")

        workers = _load_template_workers()
        names = {w["name"] for w in workers}
        assert "bad" not in names  # missing system_prompt, tools, etc.

        # Check warning was logged
        assert any("bad.json" in rec.message for rec in caplog.records)

    def test_invalid_json_is_skipped(self, with_template_dir, caplog):
        """A file with invalid JSON logs a warning and is skipped."""
        bad_template = with_template_dir / "garbage.json"
        bad_template.write_text("not valid json", encoding="utf-8")

        workers = _load_template_workers()
        names = {w["name"] for w in workers}
        assert len(names) == 1  # only the valid default

        assert any("garbage.json" in rec.message for rec in caplog.records)

    def test_falls_back_to_resources(self, temp_user_dir):
        """When user template dir is missing, falls back to resources/worker_templates/."""
        workers = _load_template_workers()
        # The real resources/worker_templates/ should have at least default.json
        assert len(workers) > 0
        names = {w["name"] for w in workers}
        assert "default" in names

    def test_empty_user_dir_uses_fallback(self, temp_user_dir):
        """An empty user template directory triggers fallback."""
        empty_dir = _user_dir() / "worker_templates"
        empty_dir.mkdir(parents=True, exist_ok=True)
        # Directory exists but is empty -> fallback
        workers = _load_template_workers()
        assert len(workers) > 0
        assert "default" in workers[0] or any("name" in w for w in workers)


class TestBuildDefaultWorkers:
    """Tests for _build_default_workers()."""

    def test_template_workers_present(self, with_template_dir):
        """Default template worker is present."""
        workers = _build_default_workers()
        names = {w["name"] for w in workers}
        assert names == {"default"}
        assert "echo" not in names

    def test_default_worker_present(self, with_template_dir):
        """Default worker is present (no echo)."""
        workers = _build_default_workers()
        names = {w["name"] for w in workers}
        assert names == {"default"}
        assert len(workers) == 1

    def test_no_duplicate_names(self, with_template_dir):
        """No two workers share the same name."""
        workers = _build_default_workers()
        names = [w["name"] for w in workers]
        assert len(names) == len(set(names))

    def test_each_worker_has_required_fields(self, with_template_dir):
        """Every worker dict has all required fields."""
        from thoughtmachine.workspace_capabilities import _validate_worker_dict

        workers = _build_default_workers()
        for w in workers:
            assert _validate_worker_dict(w) is not None, f"Worker {w.get('name')} missing required fields"


class TestEnsureWorkspaceDirsMerged:
    """Integration tests for ensure_workspace_dirs with template merging."""

    def test_creates_workers_with_templates(self, with_template_dir):
        """workers.json contains the default template on first bootstrap."""
        ensure_workspace_dirs("test-ws-merged")
        path = _user_dir() / "workspaces" / "test-ws-merged" / "workers.json"
        assert path.exists()

        workers = json.loads(path.read_text(encoding="utf-8"))
        names = {w["name"] for w in workers}
        assert names == {"default"}
        assert "echo" not in names
        assert len(workers) == 1

    def test_idempotent_does_not_overwrite_existing(self, with_template_dir):
        """Existing workers are preserved; the default template is merged in if missing."""
        ensure_workspace_dirs("test-ws-merged-2")
        path = _user_dir() / "workspaces" / "test-ws-merged-2" / "workers.json"

        # Modify the file to only have a custom worker
        path.write_text(
            json.dumps([{"name": "custom", "system_prompt": "custom", "description": "custom", "tools": [], "worker_permissions": {}}], indent=2),
            encoding="utf-8",
        )

        # Call again — should NOT overwrite existing, but should merge in missing default template
        ensure_workspace_dirs("test-ws-merged-2")

        workers = json.loads(path.read_text(encoding="utf-8"))
        assert len(workers) == 2  # custom preserved + default merged in
        names = {w["name"] for w in workers}
        assert "custom" in names  # existing worker untouched
        assert "default" in names  # default template merged in
        custom = [w for w in workers if w["name"] == "custom"][0]
        assert custom["system_prompt"] == "custom"  # untouched

    def test_atomic_write_leaves_no_tmp_file(self, with_template_dir):
        """The .tmp file is cleaned up after writing workers.json."""
        ensure_workspace_dirs("test-ws-atomic")
        ws_dir = _user_dir() / "workspaces" / "test-ws-atomic"
        tmp_files = list(ws_dir.glob("*.tmp"))
        assert len(tmp_files) == 0
        assert (ws_dir / "workers.json").exists()
