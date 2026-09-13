"""Sticky notes are a RECORD field, never a writable labels/sidecar store.

Pins the ``notes-as-record-field`` contract of ``infra/container_manager``:

  1. ``_write_note(record_id, note)`` persists onto the container RECORD
     (``<vault_root>/workspaces/<ws>/containers/<rid>.json``), whose per-record
     ``flock`` means N concurrent note writes to N containers all survive --
     unlike the OLD read-modify-write of the shared ``container_notes.json``
     sidecar, which lost updates (test 2 is the control that proves the loss).
  2. The one-shot legacy adoption matrix (sidecar-only / record-only / both /
     neither) is idempotent and never overwrites a record that already has a
     note (the record WINS).
  3. Fail-closed refusal: a write with no record id persists NOTHING (a WARNING
     is logged once); ``set_note`` on a container that carries no record label
     returns ``{"success": False, ..., "error": "no container record"}``; and
     ``_read_note`` returns ``""`` when neither a record nor the read-only
     legacy sidecar can serve a note.

Docker labels are never touched (immutable after create on stock daemons).
"""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import infra.container_manager as _cm
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    RECORD_LABEL_KEY,
    create_record,
    load_record,
    update_record,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeContainers:
    """Minimal ``client.containers`` stand-in: ``.get(name) -> container``.

    Maps a container *name* to the record id carried by its
    ``thoughtmachine.container_id`` label.  A mapping value of ``None`` models
    a container that carries NO record label (its ``labels`` is empty).  A name
    that is absent raises, exactly like a real ``NotFound``.
    """

    def __init__(self, name_to_rid=None):
        self._name_to_rid = dict(name_to_rid or {})

    def get(self, name):
        if name not in self._name_to_rid:
            raise LookupError(name)
        rid = self._name_to_rid[name]
        labels = {} if rid is None else {RECORD_LABEL_KEY: str(rid)}
        ctr = SimpleNamespace(name=name, labels=labels)
        ctr.reload = lambda: None
        return ctr


def _client(name_to_rid=None):
    return SimpleNamespace(containers=_FakeContainers(name_to_rid))


def _make_manager(workspace_id, vault_root, client):
    """Build a ContainerManager without running its constructor (no Docker)."""
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_path = "/tmp/tm-notes-rec-ws"
    mgr.session_id = "sess-notes-rec"
    mgr.workspace_id = workspace_id
    mgr.vault_root = str(vault_root)
    mgr.session_permissions = None
    mgr.image = "agent-executor"
    mgr.mem_limit = "512m"
    mgr.cpu_quota = 50000
    mgr._containers = {}
    mgr.workspace_config = {}
    mgr.max_containers = 4
    mgr.client = client
    mgr._session_config = None
    return mgr


def _new_record(workspace_id, vault_root, rid, notes=None):
    """Mint a record with a fixed id, optionally seeded with *notes*."""
    rec = create_record(
        workspace_id,
        LIFECYCLE_PERSISTENT,
        OWNER_WORKSPACE,
        id=rid,
        vault_root=str(vault_root),
    )
    if notes is not None:
        update_record(workspace_id, rec.id, vault_root=str(vault_root), notes=notes)
    return rec.id


def _notes(workspace_id, rid, vault_root):
    rec = load_record(workspace_id, rid, vault_root=str(vault_root))
    assert rec is not None, f"record {rid!r} vanished"
    return str(rec.notes or "")


def _sidecar_path(vault_root, workspace_id):
    return (
        Path(vault_root)
        / "workspaces"
        / str(workspace_id)
        / "container_notes.json"
    )


def _write_sidecar(vault_root, workspace_id, mapping):
    p = _sidecar_path(vault_root, workspace_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return p


def _record_dir(vault_root, workspace_id):
    return Path(vault_root) / "workspaces" / str(workspace_id) / "containers"


def _record_json_files(vault_root, workspace_id):
    d = _record_dir(vault_root, workspace_id)
    return list(d.glob("*.json")) if d.exists() else []


@pytest.fixture(autouse=True)
def _default_vault_is_tmp(tmp_path, monkeypatch):
    """Design B: the record helpers take NO manager ``vault_root``; they RESOLVE
    the DEFAULT vault at call time (explicit arg -> ``$THOUGHTMACHINE_VAULT_ROOT``
    -> ``~/.thoughtmachine``).  Point that default resolution at the SAME tmp
    vault the tests mint records in, so a note written through the manager
    lands on the record the test reads back.  (The legacy ``container_notes.json``
    sidecar is still keyed on ``manager.vault_root``, which these tests set to
    the same directory, so both live in one workspace tree.)"""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    return vault


@pytest.fixture(autouse=True)
def _reset_note_memos():
    """The warn/migrate memos are module-scoped; isolate every test."""
    _cm._NOTES_MIGRATED.clear()
    _cm._NOTES_WARNED.clear()
    yield
    _cm._NOTES_MIGRATED.clear()
    _cm._NOTES_WARNED.clear()


# ---------------------------------------------------------------------------
# Test 1 -- record-field writes are per-record; N concurrent writes survive
# ---------------------------------------------------------------------------


def test_concurrent_note_writes_all_survive(tmp_path):
    """N managers, N containers, N distinct notes -> all N survive.

    The record store locks per record file, so writes for *different*
    containers do not clobber each other (contrast with the control in test 2).
    """
    ws = "ws-notes-concurrent"
    vault = tmp_path / "vault"
    n = 4

    managers = []
    rids = []
    notes = []
    for i in range(n):
        rid = _new_record(ws, vault, f"rec-conc-{i}", notes="")
        rids.append(rid)
        notes.append(f"note-{i}")
        managers.append(_make_manager(ws, vault, _client({rid: rid})))

    barrier = threading.Barrier(n)
    errors = []

    def worker(idx):
        try:
            barrier.wait()
            managers[idx]._write_note(rids[idx], notes[idx])
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    survived = [_notes(ws, rid, vault) for rid in rids]
    print(f"RAW record-concurrent survived: {survived}")

    assert not errors, f"writer threads raised: {errors!r}"
    assert survived == notes, (
        "record-field writes lost an update -- survives=%r expected=%r"
        % (survived, notes)
    )


# ---------------------------------------------------------------------------
# Test 2 -- CONTROL: the pre-change shared sidecar DID lose updates
# ---------------------------------------------------------------------------


def _legacy_read_modify_write(path, name, note, delay=0.003):
    """Replicate the removed bulletin-board writer: whole-dict read-modify-write.

    Reads the WHOLE sidecar dict, sets one key, writes it back through a
    non-exclusive ``*.json.tmp`` + ``os.replace``.  ``delay`` (a sleep between
    the read and the write) merely widens the race window so the lost update is
    deterministically observable.
    """
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    if delay:
        time.sleep(delay)
    data[name] = {"note": note}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def test_control_legacy_sidecar_loses_updates(tmp_path):
    """Control: the shared-sidecar writer loses concurrent updates.

    Demonstrates WHY notes moved onto records: N distinct containers' notes all
    live in ONE ``container_notes.json``, so a concurrent read-modify-write race
    drops all but one writer's update.  At least one round must lose an update.
    """
    ws = "ws-notes-control"
    vault = tmp_path / "vault"
    path = _sidecar_path(vault, ws)
    path.parent.mkdir(parents=True, exist_ok=True)

    n = 4
    rounds = 40
    survivors_per_round = []
    oserror_count = []

    for rnd in range(rounds):
        if path.exists():
            path.unlink()
        barrier = threading.Barrier(n)

        def worker(idx, _barrier=barrier):
            _barrier.wait()
            try:
                _legacy_read_modify_write(path, f"ctr-{idx}", f"note-{idx}")
            except OSError:
                # All workers share ONE ``*.json.tmp`` name (the pre-change
                # behaviour).  Whichever thread loses the os.replace() race finds
                # the tmp already consumed -> FileNotFoundError (an OSError).
                # That IS the lost-write race this control demonstrates, so count
                # it instead of letting it escape the thread (which pytest would
                # surface as PytestUnhandledThreadExceptionWarning).
                oserror_count.append(idx)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        survivors_per_round.append(len(data))

    lost_rounds = sum(1 for s in survivors_per_round if s < n)
    print(f"RAW control survivors/round ({rounds} rounds, N={n}): {survivors_per_round}")
    print(f"RAW control lost rounds: {lost_rounds}/{rounds}")
    print(
        "RAW control expected OSError (shared tmp name, os.replace race): "
        f"{len(oserror_count)}"
    )

    assert lost_rounds >= 1, (
        "control did not reproduce a lost update -- the race window was too "
        f"narrow (survivors={survivors_per_round})"
    )


# ---------------------------------------------------------------------------
# Test 3 -- legacy adoption matrix (idempotent, record WINS)
# ---------------------------------------------------------------------------


def _rerun_migration(mgr, ws):
    """Re-run migration as a *fresh process* would (drop the once-memo)."""
    _cm._NOTES_MIGRATED.discard(ws)
    mgr._migrate_legacy_notes_once()


def test_migration_sidecar_only_seeds_record(tmp_path):
    """Sidecar note + empty record -> record adopts the note; sidecar removed."""
    ws = "ws-mig-sidecar-only"
    vault = tmp_path / "vault"
    rid = _new_record(ws, vault, "rec-m1", notes="")
    sidecar = _write_sidecar(vault, ws, {"ctr-m1": {"note": "hello"}})
    mgr = _make_manager(ws, vault, _client({"ctr-m1": rid}))

    mgr._migrate_legacy_notes_once()

    assert _notes(ws, rid, vault) == "hello"
    assert not sidecar.exists(), "adopted sidecar should be removed once empty"

    _rerun_migration(mgr, ws)
    assert _notes(ws, rid, vault) == "hello"
    assert not sidecar.exists()


def test_migration_record_only_is_noop(tmp_path):
    """A record note with no sidecar is untouched (and stays that way)."""
    ws = "ws-mig-record-only"
    vault = tmp_path / "vault"
    rid = _new_record(ws, vault, "rec-m2", notes="keep")
    mgr = _make_manager(ws, vault, _client({"ctr-m2": rid}))

    assert not _sidecar_path(vault, ws).exists()
    mgr._migrate_legacy_notes_once()
    assert _notes(ws, rid, vault) == "keep"

    _rerun_migration(mgr, ws)
    assert _notes(ws, rid, vault) == "keep"
    assert not _sidecar_path(vault, ws).exists()


def test_migration_both_nonempty_record_wins(tmp_path):
    """Record already has a note -> sidecar NEVER overwrites; sidecar removed."""
    ws = "ws-mig-both"
    vault = tmp_path / "vault"
    rid = _new_record(ws, vault, "rec-m3", notes="keep")
    sidecar = _write_sidecar(vault, ws, {"ctr-m3": {"note": "legacy-loser"}})
    mgr = _make_manager(ws, vault, _client({"ctr-m3": rid}))

    mgr._migrate_legacy_notes_once()

    assert _notes(ws, rid, vault) == "keep", "record note must win over legacy"
    assert not sidecar.exists()

    _rerun_migration(mgr, ws)
    assert _notes(ws, rid, vault) == "keep"
    assert not sidecar.exists()


def test_migration_neither_leaves_empty(tmp_path):
    """No sidecar + empty record -> nothing happens, nothing is created."""
    ws = "ws-mig-neither"
    vault = tmp_path / "vault"
    rid = _new_record(ws, vault, "rec-m4", notes="")
    mgr = _make_manager(ws, vault, _client({"ctr-m4": rid}))

    mgr._migrate_legacy_notes_once()

    assert _notes(ws, rid, vault) == ""
    assert not _sidecar_path(vault, ws).exists()

    _rerun_migration(mgr, ws)
    assert _notes(ws, rid, vault) == ""
    assert not _sidecar_path(vault, ws).exists()


# ---------------------------------------------------------------------------
# Test 4 -- fail-closed refusal / warn-once / empty read
# ---------------------------------------------------------------------------


def test_write_note_without_record_id_is_refused(tmp_path):
    """No record id -> the write is refused, nothing is persisted anywhere."""
    ws = "ws-refuse-write"
    vault = tmp_path / "vault"
    mgr = _make_manager(ws, vault, _client({}))

    mgr._write_note(None, "x")

    assert ("notes.no_record_write_refused", ws) in _cm._NOTES_WARNED
    # NOTHING persisted: no sidecar, no record file.
    assert not _sidecar_path(vault, ws).exists()
    assert _record_json_files(vault, ws) == []

    # An empty record id is refused identically.
    mgr._write_note("", "y")
    assert not _sidecar_path(vault, ws).exists()
    assert _record_json_files(vault, ws) == []


def test_set_note_fail_closed_without_record(tmp_path):
    """set_note on a container with no record label -> 'no container record'."""
    ws = "ws-set-note-failclosed"
    vault = tmp_path / "vault"
    mgr = _make_manager(ws, vault, _client({"ctr-nolabel": None}))

    res = mgr.set_note("ctr-nolabel", "x")

    assert res == {
        "success": False,
        "container_id": "ctr-nolabel",
        "error": "no container record",
    }
    assert ("notes.set_note_no_record", ws) in _cm._NOTES_WARNED
    # Fail-closed: nothing written to a record or the legacy sidecar.
    assert _record_json_files(vault, ws) == []
    assert not _sidecar_path(vault, ws).exists()


def test_read_note_empty_without_record_or_sidecar(tmp_path):
    """No record id resolvable and no sidecar entry -> '' (never raises)."""
    ws = "ws-read-empty"
    vault = tmp_path / "vault"
    mgr = _make_manager(ws, vault, _client({}))

    # Unknown container name: label lookup fails and there is no sidecar.
    assert mgr._read_note("missing-ctr") == ""

    # A container object that carries no record label and has no sidecar entry.
    bare = SimpleNamespace(name="bare-ctr", labels={})
    assert mgr._read_note(bare) == ""


# ---------------------------------------------------------------------------
# Test 5 -- DIVERGENT-VAULT pin (Design B)
# ---------------------------------------------------------------------------


def test_notes_resolve_default_vault_not_manager_vault(tmp_path, monkeypatch):
    """The NOTE helpers resolve the DEFAULT vault, NOT ``manager.vault_root``.

    Every other test in this file points ``$THOUGHTMACHINE_VAULT_ROOT`` at the
    SAME dir it hands the manager, so a regression that threaded
    ``manager.vault_root`` into the record store would pass silently.  Here the
    two vaults DIVERGE: a note write must land on the record in the DEFAULT
    vault while the manager carries a different workspace vault, and -- the
    intentional other half -- the LEGACY SIDECAR helper must keep resolving
    under that workspace vault.
    """
    ws = "ws-divergent"
    ws_vault = tmp_path / "ws_vault"
    default_vault = tmp_path / "default_vault"
    ws_vault.mkdir()
    default_vault.mkdir()

    # Override the autouse fixture's value (same function-scoped monkeypatch
    # instance): the DEFAULT vault resolution now points at default_vault, while
    # the manager still carries the DIVERGENT ws_vault.
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(default_vault))

    rec = create_record(
        ws,
        LIFECYCLE_PERSISTENT,
        OWNER_WORKSPACE,
        id="rec-divergent",
        vault_root=str(default_vault),
    )
    manager = _make_manager(ws, ws_vault, _client({rec.id: rec.id}))
    assert manager.vault_root == str(ws_vault)

    manager._write_note(rec.id, "divergent")

    # (a) The record now lives under the DEFAULT vault, at the REAL on-disk path
    #     <vault_root>/workspaces/<ws>/containers/<rid>.json.
    default_record_file = _record_dir(default_vault, ws) / f"{rec.id}.json"
    assert default_record_file.is_file(), (
        "note write did not land on the record in the DEFAULT vault: %s"
        % default_record_file
    )

    # (b) NOTHING was created under the (divergent) workspace vault.
    assert not (ws_vault / "workspaces").exists()

    # (c) Reading back from the DEFAULT vault shows the note.
    rec_back = load_record(ws, rec.id, vault_root=str(default_vault))
    assert rec_back is not None and rec_back.notes == "divergent"

    # (d) Reading from the WORKSPACE vault does NOT show it (record absent ->
    #     None, load_record's documented miss behaviour).
    assert load_record(ws, rec.id, vault_root=str(ws_vault)) is None

    # (e) The divergence: _read_note reads the DEFAULT vault even though the
    #     manager's own vault_root is the workspace vault.
    fake = SimpleNamespace(name="c-div", labels={RECORD_LABEL_KEY: rec.id})
    assert manager._read_note(fake) == "divergent"
    assert manager.vault_root == str(ws_vault)  # still the divergent vault

    # (f) The LEGACY SIDECAR helper, by contrast, deliberately stays under the
    #     WORKSPACE vault -- pin that, do not merely permit it.
    notes_path = manager._notes_path()
    assert notes_path.resolve().is_relative_to(ws_vault.resolve())
    assert not notes_path.resolve().is_relative_to(default_vault.resolve())
