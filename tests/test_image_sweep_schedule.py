"""Tests for the periodic pasted-image orphan sweep (image GC scheduling).

``web_ui.backend.server`` already re-runs the container sweeps on an interval
(``_periodic_container_sweep_loop`` -> ``_run_container_sweeps``).  The durable
pasted-image orphan sweep rides that SAME tick -- as
``_run_image_sweep`` -- so it needs no loop, thread or cancellation of its own.

What has to hold for that step:

* a file dies only when BOTH the workspace's persisted histories stop naming it
  AND it is older than ``ORPHAN_TTL_SECONDS``;
* a workspace whose session files cannot all be read is ``complete=False`` from
  ``scan_workspace_references``: its id set may be an UNDER-count, so NOTHING in
  that workspace is deleted (the one mistake that silently destroys bytes a live
  session still references) and a WARNING names it;
* every run logs exactly ONE INFO line with the totals, zero-removed runs
  included -- a silent sweep and a broken sweep must not look alike;
* a fault in the image step is contained: the container sweeps and the loop's
  next tick are unaffected.

Everything runs against a throw-away vault root (``THOUGHTMACHINE_VAULT_ROOT``
pointed at ``tmp_path``), so no test reads or writes the real user vault.  No
Docker daemon is needed: the three container sweeps are replaced by counters.
Ages are set with ``os.utime`` -- never by sleeping.

Log assertions go through the module-level ``server.log`` seam: that facade
prints to stderr and forwards to a JSONL ``AgentLogger``, emitting NO stdlib
``logging`` records, so pytest's ``caplog`` cannot see it (the sibling sweep
tests record the call the same way -- see
``tests/test_periodic_sweep_registry_recheck.py``).
"""

import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_files
from agent.core.image_files import ORPHAN_TTL_SECONDS, mint_image_id
from web_ui.backend import server

PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload" * 4

INFO_PREFIX = "Periodic image sweep:"


# ── Fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point the vault at a throw-away directory for the duration of a test."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


class _FakeEntry:
    def __init__(self, workspace_id):
        self.id = workspace_id


def _register(monkeypatch, *workspace_ids):
    """Make the sweep enumerate exactly these workspaces.

    ``_run_image_sweep`` resolves workspaces through the registry (the same
    source every other sweep in ``server`` uses), so this is the seam.
    """
    registry = server.WorkspaceRegistry.__new__(server.WorkspaceRegistry)
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
    )
    monkeypatch.setattr(
        server.WorkspaceRegistry,
        "list_workspaces",
        staticmethod(lambda: [_FakeEntry(wid) for wid in workspace_ids]),
    )
    return registry


def _record_log(monkeypatch):
    """Capture ``server.log(level, category, message)`` calls as tuples."""
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(server, "log", recorder)
    return events


def _write_image(vault: Path, workspace_id: str, image_id: str, data: bytes = PNG) -> Path:
    """Store one image file the way ``save_image`` lays it out on disk."""
    images_dir = vault / "workspaces" / workspace_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    path = images_dir / f"{image_id}.png"
    path.write_bytes(data)
    return path


def _write_session(vault: Path, workspace_id: str, session_id: str, history: list) -> Path:
    """Write one session record the way the store lays it out on disk."""
    sessions_dir = vault / "workspaces" / workspace_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.json"
    path.write_text(
        json.dumps({"session_id": session_id, "user_history": history}),
        encoding="utf-8",
    )
    return path


def _age(path: Path, seconds: int) -> None:
    """Backdate *path*'s mtime by *seconds*, so a TTL sweep can see it."""
    old = time.time() - seconds
    os.utime(path, (old, old))


# ── 1. TTL and persisted references decide what dies ─────────────────────────


def test_expired_orphan_is_removed_and_its_bytes_are_reported(vault, monkeypatch):
    """An unreferenced file past the TTL is freed and counted in the totals."""
    _register(monkeypatch, "ws-a")
    orphan = _write_image(vault, "ws-a", mint_image_id())
    _age(orphan, ORPHAN_TTL_SECONDS + 60)

    summary = server._run_image_sweep()

    assert not orphan.exists()
    assert summary["workspaces_scanned"] == 1
    assert summary["files_removed"] == 1
    assert summary["bytes_freed"] == len(PNG)
    assert summary["workspaces_skipped_incomplete"] == 0


def test_orphan_younger_than_the_ttl_survives(vault, monkeypatch):
    """Age matters: an unreferenced file still inside the TTL is left alone."""
    _register(monkeypatch, "ws-a")
    young = _write_image(vault, "ws-a", mint_image_id())

    summary = server._run_image_sweep()

    assert young.exists()
    assert summary["files_removed"] == 0
    assert summary["bytes_freed"] == 0


def test_a_file_a_persisted_session_references_survives_any_age(vault, monkeypatch):
    """A referenced id is never swept, however old the file is."""
    _register(monkeypatch, "ws-a")
    referenced_id = mint_image_id()
    _write_session(vault, "ws-a", "s1", [
        {"role": "user", "content": "look", "images": [{"id": referenced_id}]},
    ])
    referenced = _write_image(vault, "ws-a", referenced_id)
    _age(referenced, ORPHAN_TTL_SECONDS * 10)
    orphan = _write_image(vault, "ws-a", mint_image_id())
    _age(orphan, ORPHAN_TTL_SECONDS + 60)

    summary = server._run_image_sweep()

    assert referenced.exists()
    assert not orphan.exists()
    assert summary["files_removed"] == 1


# ── 2. An incomplete scan deletes nothing for that workspace ─────────────────


def test_an_unparseable_session_file_skips_its_workspace_only(vault, monkeypatch):
    """Incomplete scan -> nothing deleted there; a complete sibling still sweeps.

    The escaping mistake is to feed a partial reference set to a function that
    deletes unreferenced files: every id the unreadable file held would look
    dead.  So the partial workspace is skipped WHOLE and named in a WARNING,
    while the healthy workspace on the same tick is swept as usual.
    """
    events = _record_log(monkeypatch)
    _register(monkeypatch, "ws-partial", "ws-complete")

    partial_orphan = _write_image(vault, "ws-partial", mint_image_id())
    _age(partial_orphan, ORPHAN_TTL_SECONDS + 60)
    _write_session(vault, "ws-partial", "s1", [{"role": "user", "content": "hi"}])
    broken = vault / "workspaces" / "ws-partial" / "sessions" / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")

    complete_orphan = _write_image(vault, "ws-complete", mint_image_id())
    _age(complete_orphan, ORPHAN_TTL_SECONDS + 60)

    summary = server._run_image_sweep()

    assert partial_orphan.exists(), "an incomplete scan must not delete anything"
    assert not complete_orphan.exists(), "the complete workspace is still swept"
    assert summary["workspaces_skipped_incomplete"] == 1
    assert summary["skipped_incomplete"] == ["ws-partial"]
    assert summary["workspaces_scanned"] == 1
    assert summary["files_removed"] == 1

    warnings = [message for level, _category, message in events if level == "WARNING"]
    assert any("ws-partial" in message and "could not be read completely" in message
               for message in warnings), warnings


# ── 3. One INFO line per run, and never a raise into the loop ────────────────


def test_exactly_one_info_line_per_run_even_when_nothing_is_removed(vault, monkeypatch):
    """A zero-removed sweep still reports its totals, exactly once."""
    events = _record_log(monkeypatch)
    _register(monkeypatch, "ws-empty")

    summary = server._run_image_sweep()

    lines = [message for level, _category, message in events
             if level == "INFO" and message.startswith(INFO_PREFIX)]
    assert len(lines) == 1, lines
    assert summary["files_removed"] == 0
    assert "scanned 1 workspace(s)" in lines[0]
    assert "removed 0 file(s)" in lines[0]
    assert "freed 0 byte(s)" in lines[0]
    assert "skipped 0 workspace(s)" in lines[0]


def test_a_raising_image_sweep_does_not_touch_the_container_sweeps(monkeypatch):
    """A fault in the image step is logged, contained, and nothing else."""
    events = _record_log(monkeypatch)
    _register(monkeypatch, "ws-a")

    ran = []
    monkeypatch.setattr(
        server, "_sweep_exited_workspace_containers", lambda: ran.append("exited"))
    monkeypatch.setattr(
        server, "_sweep_orphan_resource_containers", lambda: ran.append("orphan"))
    monkeypatch.setattr(
        server, "_sweep_orphan_container_records", lambda: ran.append("records"))

    def boom():
        raise RuntimeError("image sweep blew up")

    monkeypatch.setattr(server, "_run_image_sweep", boom)

    # The routine itself must not raise: the loop calls it unguarded per step.
    server._run_container_sweeps()

    assert ran == ["exited", "orphan", "records"]
    warnings = [message for level, _category, message in events if level == "WARNING"]
    assert any("Periodic image sweep failed" in message for message in warnings), warnings
