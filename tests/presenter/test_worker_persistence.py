"""
Tests for worker persistence — bridge._load_worker_contexts(),
bridge.resume_worker(), and workspace routes scanning logic.

We mock `tiktoken` and `libcst` at the module level so that
``WebAgentBridge`` can be imported in the test container without
requiring those heavy dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── Module-level mocks for dependencies the bridge pulls in ──────────────────

class _MockEncoding:
    """Stand-in for tiktoken.Encoding."""
    def encode(self, text: str) -> list[int]:
        return [0] * len(text)
    def decode(self, tokens: list[int]) -> str:
        return ""

_mock_tiktoken = types.ModuleType("tiktoken")
_mock_tiktoken.Encoding = _MockEncoding
_mock_tiktoken.get_encoding = staticmethod(lambda name: _MockEncoding())
_mock_tiktoken.model_name_to_encoding = staticmethod(lambda name: _MockEncoding())
sys.modules.setdefault("tiktoken", _mock_tiktoken)

for _mod_name in ("libcst", "tree_sitter", "tree_sitter_python"):
    sys.modules.setdefault(_mod_name, types.ModuleType(_mod_name))

# Now safe to import the bridge
from web_ui.backend.bridge import WebAgentBridge


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ws_dir(tmp_path: Path) -> Path:
    """Create a temporary workspace directory with workers sub-structure."""
    d = tmp_path / "workspaces" / "ws_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def bridge(ws_dir: Path) -> WebAgentBridge:
    """Return a minimal WebAgentBridge instance with heavy internals mocked."""
    inst = WebAgentBridge.__new__(WebAgentBridge)
    # Minimal attribute setup that _load_worker_contexts relies on
    inst._workspace_id = "ws_test"
    inst._persisted_workers = {}
    return inst


@pytest.fixture(autouse=True)
def _patch_workspace_dir(ws_dir: Path):
    """Route ``_workspace_dir(ws_id)`` to our temp directory.

    We patch at both the source module AND ``workspace_routes`` because
    ``workspace_routes`` imports ``_workspace_dir`` with a direct
    ``from ... import`` (creating its own module-level reference).
    """
    from unittest.mock import patch as _patch

    patcher1 = _patch(
        "thoughtmachine.workspace_capabilities._workspace_dir",
        return_value=ws_dir,
    )
    patcher2 = _patch(
        "web_ui.backend.workspace_routes._workspace_dir",
        return_value=ws_dir,
    )
    patcher1.start()
    patcher2.start()
    try:
        yield
    finally:
        patcher2.stop()
        patcher1.stop()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_context(workers_dir: Path, name: str, data: dict | None = None) -> Path:
    """Create ``workers/<name>/context.json`` and return its path."""
    worker_dir = workers_dir / name
    worker_dir.mkdir(parents=True, exist_ok=True)
    ctx = data if data is not None else {"messages": [{"role": "user", "content": "hello"}]}
    p = worker_dir / "context.json"
    p.write_text(json.dumps(ctx), encoding="utf-8")
    return p


# ══════════════════════════════════════════════════════════════════════════════
#  _load_worker_contexts
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadWorkerContexts:
    """Covers WebAgentBridge._load_worker_contexts()."""

    def test_no_workspace_id(self, bridge: WebAgentBridge):
        """If _workspace_id is falsy, the method returns without error."""
        bridge._workspace_id = None
        bridge._load_worker_contexts()  # should not raise
        assert bridge._persisted_workers == {}

    def test_empty_workers_dir(self, ws_dir: Path, bridge: WebAgentBridge):
        """A workers/ directory with no sub-directories produces nothing."""
        (ws_dir / "workers").mkdir(parents=True)
        bridge._load_worker_contexts()
        assert bridge._persisted_workers == {}

    def test_no_workers_dir(self, ws_dir: Path, bridge: WebAgentBridge):
        """No workers/ directory at all → empty dict, no error."""
        bridge._load_worker_contexts()
        assert bridge._persisted_workers == {}

    def test_loads_single_worker(self, ws_dir: Path, bridge: WebAgentBridge):
        """A single worker with a valid context.json is loaded."""
        _make_context(ws_dir / "workers", "alpha", {"key": "value"})
        bridge._load_worker_contexts()
        assert "alpha" in bridge._persisted_workers
        assert bridge._persisted_workers["alpha"]["name"] == "alpha"
        assert bridge._persisted_workers["alpha"]["context"] == {"key": "value"}

    def test_loads_multiple_workers(self, ws_dir: Path, bridge: WebAgentBridge):
        """Multiple workers are loaded independently."""
        _make_context(ws_dir / "workers", "alpha", {"role": "alpha"})
        _make_context(ws_dir / "workers", "beta", {"role": "beta"})
        bridge._load_worker_contexts()
        assert set(bridge._persisted_workers) == {"alpha", "beta"}

    def test_skips_empty_subdir(self, ws_dir: Path, bridge: WebAgentBridge):
        """A sub-directory without context.json is silently skipped."""
        (ws_dir / "workers" / "empty_worker").mkdir(parents=True)
        _make_context(ws_dir / "workers", "real", {"x": 1})
        bridge._load_worker_contexts()
        assert "empty_worker" not in bridge._persisted_workers
        assert "real" in bridge._persisted_workers

    def test_skips_bad_json(self, ws_dir: Path, bridge: WebAgentBridge):
        """A context.json with invalid JSON is skipped (no crash)."""
        worker_dir = ws_dir / "workers" / "broken"
        worker_dir.mkdir(parents=True)
        (worker_dir / "context.json").write_text("{bad json}", encoding="utf-8")
        _make_context(ws_dir / "workers", "good", {"ok": True})
        bridge._load_worker_contexts()
        assert "broken" not in bridge._persisted_workers
        assert "good" in bridge._persisted_workers

    def test_skips_files_not_dirs(self, ws_dir: Path, bridge: WebAgentBridge):
        """Regular files inside workers/ are ignored (only dirs are scanned)."""
        workers = ws_dir / "workers"
        workers.mkdir(parents=True)
        (workers / "not_a_dir.txt").write_text("ignored", encoding="utf-8")
        bridge._load_worker_contexts()
        assert bridge._persisted_workers == {}

    def test_loads_from_real_directory(self, ws_dir: Path, bridge: WebAgentBridge):
        """Integration check: real directory on disk loads correctly."""
        ctx = {"messages": [{"role": "assistant", "content": "I am ready"}]}
        _make_context(ws_dir / "workers", "worker_a", ctx)
        bridge._load_worker_contexts()
        assert "worker_a" in bridge._persisted_workers
        loaded = bridge._persisted_workers["worker_a"]["context"]
        assert loaded["messages"][0]["content"] == "I am ready"


# ══════════════════════════════════════════════════════════════════════════════
#  resume_worker
# ══════════════════════════════════════════════════════════════════════════════


class TestResumeWorker:
    """Covers WebAgentBridge.resume_worker()."""

    def test_returns_context_for_known_worker(self, bridge: WebAgentBridge):
        """Calling resume_worker with a loaded worker name returns its context dict."""
        bridge._persisted_workers = {
            "alice": {"name": "alice", "context": {"msg": "hello"}},
        }
        result = bridge.resume_worker("alice")
        assert result == {"msg": "hello"}

    def test_returns_none_for_unknown(self, bridge: WebAgentBridge):
        """Calling resume_worker with a name not in _persisted_workers returns None."""
        bridge._persisted_workers = {}
        assert bridge.resume_worker("nonexistent") is None

    def test_returns_none_after_clear(self, bridge: WebAgentBridge):
        """After clearing _persisted_workers, resume_worker returns None."""
        bridge._persisted_workers = {
            "bob": {"name": "bob", "context": {"msg": "bye"}},
        }
        bridge._persisted_workers.clear()
        assert bridge.resume_worker("bob") is None

    def test_persisted_workers_maintained_after_load(self, ws_dir: Path, bridge: WebAgentBridge):
        """End-to-end: load contexts, then resume each one."""
        _make_context(ws_dir / "workers", "x", {"data": 1})
        _make_context(ws_dir / "workers", "y", {"data": 2})
        bridge._load_worker_contexts()

        assert bridge.resume_worker("x") == {"data": 1}
        assert bridge.resume_worker("y") == {"data": 2}
        assert bridge.resume_worker("z") is None


# ══════════════════════════════════════════════════════════════════════════════
#  Workspace routes — get_workers (persisted context flag)
# ══════════════════════════════════════════════════════════════════════════════


class TestGetWorkersPersistedContextFlag:
    """The ``get_workers`` endpoint detects persisted contexts on disk."""

    def _make_workers_json(self, ws_dir: Path, configs: list[dict]) -> None:
        """Write a workers.json config file."""
        (ws_dir / "workers.json").write_text(json.dumps(configs), encoding="utf-8")

    @patch("tools.workspace.worker._worker_registry", {})
    @patch("tools.workspace.worker._registry_lock", MagicMock())
    def test_has_persisted_context_flag(self, ws_dir: Path):
        """Workers with context.json on disk get has_persisted_context=True."""
        from web_ui.backend.workspace_routes import get_workers

        self._make_workers_json(ws_dir, [
            {"name": "alpha", "system_prompt": "you are alpha"},
            {"name": "beta", "system_prompt": "you are beta"},
        ])
        # Create persisted context for alpha only
        _make_context(ws_dir / "workers", "alpha", {"msg": "persisted"})

        import asyncio
        result = asyncio.run(get_workers("ws_test"))

        alpha_entry = next(e for e in result if e["name"] == "alpha")
        beta_entry = next(e for e in result if e["name"] == "beta")
        assert alpha_entry["has_persisted_context"] is True
        assert beta_entry["has_persisted_context"] is False

    @patch("tools.workspace.worker._worker_registry", {})
    @patch("tools.workspace.worker._registry_lock", MagicMock())
    def test_all_false_when_no_workers_dir(self, ws_dir: Path):
        """When no workers/ directory exists, all workers have has_persisted_context=False."""
        from web_ui.backend.workspace_routes import get_workers

        self._make_workers_json(ws_dir, [
            {"name": "gamma"},
        ])
        import asyncio
        result = asyncio.run(get_workers("ws_test"))
        assert result[0]["has_persisted_context"] is False

    @patch("tools.workspace.worker._worker_registry", {})
    @patch("tools.workspace.worker._registry_lock", MagicMock())
    def test_all_false_when_empty_workers_dir(self, ws_dir: Path):
        """When workers/ dir exists but empty, has_persisted_context is False."""
        from web_ui.backend.workspace_routes import get_workers

        self._make_workers_json(ws_dir, [{"name": "delta"}])
        (ws_dir / "workers").mkdir(parents=True)

        import asyncio
        result = asyncio.run(get_workers("ws_test"))
        assert result[0]["has_persisted_context"] is False

    @patch("tools.workspace.worker._worker_registry", {})
    @patch("tools.workspace.worker._registry_lock", MagicMock())
    def test_true_for_multiple_persisted_workers(self, ws_dir: Path):
        """Multiple workers with context.json all get has_persisted_context=True."""
        from web_ui.backend.workspace_routes import get_workers

        self._make_workers_json(ws_dir, [
            {"name": "one"},
            {"name": "two"},
        ])
        _make_context(ws_dir / "workers", "one", {})
        _make_context(ws_dir / "workers", "two", {})

        import asyncio
        result = asyncio.run(get_workers("ws_test"))
        flags = {e["name"]: e["has_persisted_context"] for e in result}
        assert flags == {"one": True, "two": True}

    @patch("tools.workspace.worker._worker_registry", {})
    @patch("tools.workspace.worker._registry_lock", MagicMock())
    def test_ignores_workers_not_in_config(self, ws_dir: Path):
        """Persisted workers not in workers.json are ignored (no entry created)."""
        from web_ui.backend.workspace_routes import get_workers

        self._make_workers_json(ws_dir, [{"name": "configured"}])
        _make_context(ws_dir / "workers", "configured", {})
        _make_context(ws_dir / "workers", "orphan", {})  # not in config

        import asyncio
        result = asyncio.run(get_workers("ws_test"))
        names = [e["name"] for e in result]
        assert "configured" in names
        assert "orphan" not in names


# ══════════════════════════════════════════════════════════════════════════════
#  Workspace routes — get_worker_events
# ══════════════════════════════════════════════════════════════════════════════


class TestGetWorkerEvents:
    """Covers the ``get_worker_events`` endpoint."""

    def _make_events_file(self, ws_dir: Path, lines: list[str]) -> None:
        """Write a workers/events.jsonl file."""
        p = ws_dir / "workers" / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_returns_empty_when_no_events_file(self, ws_dir: Path):
        """No events.jsonl → empty list."""
        from web_ui.backend.workspace_routes import get_worker_events

        import asyncio
        result = asyncio.run(get_worker_events("ws_test", "any"))
        assert result == []

    def test_returns_only_matching_worker_events(self, ws_dir: Path):
        """Only events with matching worker_name are returned."""
        from web_ui.backend.workspace_routes import get_worker_events

        self._make_events_file(ws_dir, [
            json.dumps({"worker_name": "alice", "timestamp": "2025-01-01T00:00:00Z", "type": "msg"}),
            json.dumps({"worker_name": "bob", "timestamp": "2025-01-01T00:00:01Z", "type": "msg"}),
            json.dumps({"worker_name": "alice", "timestamp": "2025-01-01T00:00:02Z", "type": "done"}),
        ])

        import asyncio
        result = asyncio.run(get_worker_events("ws_test", "alice", limit=None, since=None))
        assert len(result) == 2
        assert all(e["worker_name"] == "alice" for e in result)

    def test_limit(self, ws_dir: Path):
        """?limit=N returns only the N most recent events."""
        from web_ui.backend.workspace_routes import get_worker_events

        lines = [
            json.dumps({"worker_name": "alice", "timestamp": f"2025-01-01T00:00:0{i}Z", "type": "msg"})
            for i in range(5)
        ]
        self._make_events_file(ws_dir, lines)

        import asyncio
        result = asyncio.run(get_worker_events("ws_test", "alice", limit=2, since=None))
        assert len(result) == 2
        # Should be the 2 most recent
        assert result[0]["timestamp"] == "2025-01-01T00:00:03Z"
        assert result[1]["timestamp"] == "2025-01-01T00:00:04Z"

    def test_since_filter(self, ws_dir: Path):
        """?since=<timestamp> returns only events after that time."""
        from web_ui.backend.workspace_routes import get_worker_events

        self._make_events_file(ws_dir, [
            json.dumps({"worker_name": "alice", "timestamp": "2025-01-01T00:00:01Z", "type": "old"}),
            json.dumps({"worker_name": "alice", "timestamp": "2025-01-01T00:00:05Z", "type": "mid"}),
            json.dumps({"worker_name": "alice", "timestamp": "2025-01-01T00:00:10Z", "type": "new"}),
        ])

        import asyncio
        result = asyncio.run(get_worker_events("ws_test", "alice", since="2025-01-01T00:00:04Z", limit=None))
        assert len(result) == 2
        assert result[0]["type"] == "mid"
        assert result[1]["type"] == "new"

    def test_limit_with_since(self, ws_dir: Path):
        """?limit and ?since work together."""
        from web_ui.backend.workspace_routes import get_worker_events

        self._make_events_file(ws_dir, [
            json.dumps({"worker_name": "x", "timestamp": f"2025-01-01T00:00:0{i}Z", "type": "t"})
            for i in range(10)
        ])

        import asyncio
        result = asyncio.run(get_worker_events("ws_test", "x", since="2025-01-01T00:00:05Z", limit=2))
        assert len(result) == 2
        # After timestamp 5, we have events 6,7,8,9 — limit to 2 most recent = 8,9
        assert result[0]["timestamp"] == "2025-01-01T00:00:08Z"
        assert result[1]["timestamp"] == "2025-01-01T00:00:09Z"

    def test_ignores_bad_json_lines(self, ws_dir: Path):
        """Malformed JSON lines are silently skipped."""
        from web_ui.backend.workspace_routes import get_worker_events

        self._make_events_file(ws_dir, [
            json.dumps({"worker_name": "alice", "timestamp": "t1", "type": "good"}),
            "{bad json}",
            json.dumps({"worker_name": "alice", "timestamp": "t2", "type": "also_good"}),
        ])

        import asyncio
        result = asyncio.run(get_worker_events("ws_test", "alice", limit=None, since=None))
        assert len(result) == 2
