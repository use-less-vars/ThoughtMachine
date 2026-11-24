"""Schema v6 -- the record *retention charter*.

``retention_mode`` is the only machine-readable retention authority on a
record.  These tests pin the four properties that make that statement true:

* the 5 -> 6 migration stamps a charter from the *machine* fields alone --
  free-text prose (a note, a purpose, an event payload) is never parsed, so a
  record whose note says "do not remove" and one whose note says "temporary"
  come out of the migration with an identical charter;
* the migration is idempotent and leaves an already-current record byte
  identical (it is never rewritten just to re-stamp what it already has);
* an unknown mode is rejected fail-closed on construction, on the API and on
  the lifecycle-class default path;
* the charter round-trips through ``to_dict`` / ``from_dict`` and an absent
  mode reads back as ``pinned``.

The final test is a source tripwire: neither ``migration`` nor
``lifecycle_policy`` may ever grow code that *parses* the prose ``notes``
field to decide a retention.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from thoughtmachine.container_record import (
    RETENTION_EPHEMERAL_UNTIL_EXIT,
    RETENTION_MODES,
    RETENTION_PINNED,
    RETENTION_RETAINED_UNTIL,
    api,
    migration,
    models,
    storage,
)
from thoughtmachine.container_record.api import (
    RETENTION_SOURCE_CLASS_DEFAULT,
    RETENTION_SOURCE_EXPLICIT,
)

WS = "ws-retention"
DAY_SECONDS = 86400.0


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def _legacy_v5_record(record_id: str, notes: str) -> dict:
    """Return a v5 on-disk record dict (18 keys, no retention charter)."""
    return {
        "id": record_id,
        "docker_id": f"d-{record_id}",
        "lifecycle_class": "persistent",
        "owner": "workspace-owned",
        "purpose": "",
        "intent_snapshot": {},
        "notes": notes,
        "event_log": [],
        "schema_version": 5,
        "inferred": False,
        "state": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "restart_policy": None,
        "name": "",
        "retention_days": None,
        "user": "",
        "permissions": None,
    }


def _seed(vault, record_id: str, notes: str) -> Path:
    path = storage.record_path(WS, record_id, vault)
    storage.write_record_file(path, _legacy_v5_record(record_id, notes))
    return path


# ── (a) prose never decides the charter ─────────────────────────────────────


def test_prose_notes_never_influence_the_migrated_charter(vault):
    keep = _seed(vault, "rec-keep", "do not remove this container")
    drop = _seed(vault, "rec-drop", "Temporary; remove when done")

    summary = migration.migrate_retention_charter(WS, vault_root=vault)

    assert summary["workspace_id"] == WS
    assert summary["scanned"] == 2
    assert summary["upgraded"] == 2
    assert summary["failed"] == 0
    assert summary["errors"] == []

    charters = {}
    for path in (keep, drop):
        data = storage.read_record_file(path)
        charters[path.name] = (
            data["retention_mode"],
            data["retention_until"],
            data["retention_source"],
            data["schema_version"],
        )

    # Diametrically opposed prose, byte-identical charter: prose has no authority.
    assert len(set(charters.values())) == 1
    for charter in charters.values():
        assert charter == (
            RETENTION_PINNED,
            None,
            migration.RETENTION_MIGRATION_SOURCE,
            models.SCHEMA_VERSION_CURRENT,
        )
    # The prose itself is preserved verbatim (additive migration).
    assert storage.read_record_file(keep)["notes"] == "do not remove this container"
    assert storage.read_record_file(drop)["notes"] == "Temporary; remove when done"


def test_retention_migration_is_summarised_in_the_migrations_log(vault):
    _seed(vault, "rec-log", "")
    migration.migrate_retention_charter(WS, vault_root=vault)

    lines = (
        storage.migrations_log_path(WS, vault)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    entries = [
        json.loads(line)
        for line in lines
        if line.strip() and json.loads(line).get("event") == "retention_charter"
    ]
    assert len(entries) == 1
    assert entries[0]["scanned"] == 1
    assert entries[0]["upgraded"] == 1
    assert entries[0]["failed"] == 0
    assert entries[0]["timestamp"]


# ── (b) idempotency + no gratuitous writes ──────────────────────────────────


def test_migration_is_idempotent_and_leaves_current_records_byte_identical(vault):
    _seed(vault, "rec-a", "do not remove")
    _seed(vault, "rec-b", "")

    first = migration.migrate_retention_charter(WS, vault_root=vault)
    assert first["upgraded"] == 2
    assert first["skipped"] == 0

    # A natively created record already carries a valid current charter.
    native = api.create_record(
        WS, "persistent", "workspace-owned", id="rec-native", vault_root=vault
    )
    assert native.retention_mode == RETENTION_RETAINED_UNTIL
    native_path = storage.record_path(WS, "rec-native", vault)
    before = native_path.read_bytes()

    second = migration.migrate_retention_charter(WS, vault_root=vault)

    assert second["scanned"] == 3
    assert second["upgraded"] == 0
    assert second["skipped"] == 3
    assert second["failed"] == 0
    # Not rewritten at all -- the bytes (and every pre-existing field) survive.
    assert native_path.read_bytes() == before
    data = storage.read_record_file(native_path)
    assert list(data.keys()) == list(models.SCHEMA_FIELD_NAMES)


def test_valid_mode_at_an_old_schema_version_is_only_version_stamped(vault):
    path = _seed(vault, "rec-old", "whatever")
    data = storage.read_record_file(path)
    data["retention_mode"] = RETENTION_EPHEMERAL_UNTIL_EXIT
    data["retention_source"] = RETENTION_SOURCE_EXPLICIT
    storage.write_record_file(path, data)

    summary = migration.migrate_retention_charter(WS, vault_root=vault)

    assert summary["upgraded"] == 1
    migrated = storage.read_record_file(path)
    # An explicit, valid charter is never overwritten by the migration default.
    assert migrated["retention_mode"] == RETENTION_EPHEMERAL_UNTIL_EXIT
    assert migrated["retention_source"] == RETENTION_SOURCE_EXPLICIT
    assert migrated["schema_version"] == models.SCHEMA_VERSION_CURRENT


# ── (c) fail-closed rejection of an unknown mode ────────────────────────────


def test_unknown_retention_mode_is_rejected_everywhere(vault):
    with pytest.raises(ValueError):
        models.Record(
            id="r1",
            lifecycle_class="ephemeral",
            owner="workspace-owned",
            retention_mode="forever",
        )
    with pytest.raises(ValueError):
        models.validate_retention_mode("")
    # ``retained_until`` with no deadline is a dangling charter.
    with pytest.raises(ValueError):
        models.Record(
            id="r1",
            lifecycle_class="ephemeral",
            owner="workspace-owned",
            retention_mode=RETENTION_RETAINED_UNTIL,
        )
    with pytest.raises(ValueError):
        api.begin_record(
            WS, "ephemeral", "workspace-owned", retention_mode="nope", vault_root=vault
        )
    with pytest.raises(ValueError):
        api.begin_record(
            WS,
            "ephemeral",
            "workspace-owned",
            retention_mode=RETENTION_RETAINED_UNTIL,
            vault_root=vault,
        )
    # Nothing rejected was persisted.
    assert storage.iter_record_files(storage.containers_dir(WS, vault)) == []


# ── (d) round-trip / absent-mode default ────────────────────────────────────


def test_charter_round_trips_and_absent_mode_reads_as_pinned():
    until = 1_800_000_000.0
    rec = models.Record(
        id="r1",
        lifecycle_class="persistent",
        owner="workspace-owned",
        retention_mode=RETENTION_RETAINED_UNTIL,
        retention_until=until,
        retention_source=RETENTION_SOURCE_EXPLICIT,
    )
    data = rec.to_dict()
    assert data["retention_mode"] == RETENTION_RETAINED_UNTIL
    assert data["retention_until"] == until
    assert data["retention_source"] == RETENTION_SOURCE_EXPLICIT
    assert list(data.keys())[-3:] == list(models.SCHEMA_FIELD_NAMES[-3:])

    back = models.Record.from_dict(data)
    assert back.retention_mode == RETENTION_RETAINED_UNTIL
    assert back.retention_until == until
    assert isinstance(back.retention_until, float)
    assert back.retention_source == RETENTION_SOURCE_EXPLICIT

    # A record that predates the field defaults to the conservative mode.
    plain = models.Record(
        id="r2", lifecycle_class="ephemeral", owner="workspace-owned"
    )
    assert plain.retention_mode == RETENTION_PINNED
    assert plain.retention_until is None
    assert plain.retention_source == ""

    legacy = models.Record.from_dict(
        {"id": "r3", "lifecycle_class": "ephemeral", "owner": "workspace-owned"}
    )
    assert legacy.retention_mode == RETENTION_PINNED
    assert legacy.retention_until is None

    # Reading can never widen a guarantee: an unknown on-disk mode is pinned.
    unknown = models.Record.from_dict(
        {
            "id": "r4",
            "lifecycle_class": "ephemeral",
            "owner": "workspace-owned",
            "retention_mode": "forever",
            "retention_until": until,
        }
    )
    assert unknown.retention_mode == RETENTION_PINNED
    assert unknown.retention_until is None
    assert set(RETENTION_MODES) == {
        RETENTION_PINNED,
        RETENTION_RETAINED_UNTIL,
        RETENTION_EPHEMERAL_UNTIL_EXIT,
    }


# ── (e) lifecycle-class defaults at create time ─────────────────────────────


def test_begin_record_stamps_the_lifecycle_class_default(vault):
    persistent = api.begin_record(
        WS, "persistent", "workspace-owned", id="rec-p", vault_root=vault
    )
    assert persistent.retention_mode == RETENTION_RETAINED_UNTIL
    anchor = datetime.fromisoformat(persistent.created_at).timestamp()
    assert persistent.retention_until == pytest.approx(
        anchor + DAY_SECONDS, abs=1.0
    )
    assert persistent.retention_source == RETENTION_SOURCE_CLASS_DEFAULT

    ephemeral = api.begin_record(
        WS, "ephemeral", "workspace-owned", id="rec-e", vault_root=vault
    )
    assert ephemeral.retention_mode == RETENTION_EPHEMERAL_UNTIL_EXIT
    assert ephemeral.retention_until is None
    assert ephemeral.retention_source == RETENTION_SOURCE_CLASS_DEFAULT

    pinned = api.begin_record(
        WS,
        "persistent",
        "workspace-owned",
        id="rec-x",
        retention_mode=RETENTION_PINNED,
        vault_root=vault,
    )
    assert pinned.retention_mode == RETENTION_PINNED
    assert pinned.retention_until is None
    assert pinned.retention_source == RETENTION_SOURCE_EXPLICIT

    # The charter is on disk, in field order.
    data = storage.read_record_file(storage.record_path(WS, "rec-p", vault))
    assert data["retention_mode"] == RETENTION_RETAINED_UNTIL
    assert data["retention_until"] == pytest.approx(anchor + DAY_SECONDS, abs=1.0)
    assert data["retention_source"] == RETENTION_SOURCE_CLASS_DEFAULT

    # An explicit deadline is honoured verbatim.
    deadline = 1_900_000_000.0
    explicit = api.begin_record(
        WS,
        "ephemeral",
        "workspace-owned",
        id="rec-d",
        retention_mode=RETENTION_RETAINED_UNTIL,
        retention_until=deadline,
        vault_root=vault,
    )
    assert explicit.retention_mode == RETENTION_RETAINED_UNTIL
    assert explicit.retention_until == deadline
    assert explicit.retention_source == RETENTION_SOURCE_EXPLICIT


# ── (f) source tripwire: prose is never parsed ──────────────────────────────

_PARSE_PATTERNS = (
    r"notes\s*\.\s*(startswith|endswith|lower|strip|split|find|replace|count|match|search)",
    r"\.get\(\s*['\"]notes['\"]",
    r"\[\s*['\"]notes['\"]\s*\]",
    r"['\"]notes['\"]\s*(in|not in)",
)

_PROSE_LITERALS = ("do not remove", "temporar")


def _module_source(module_name: str) -> str:
    module = __import__(
        f"thoughtmachine.container_record.{module_name}", fromlist=["_"]
    )
    return Path(module.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("module_name", ["migration", "lifecycle_policy"])
def test_retention_code_never_parses_the_prose_notes_field(module_name):
    source = _module_source(module_name)
    for pattern in _PARSE_PATTERNS:
        assert not re.search(pattern, source), (
            f"{module_name}.py parses the prose notes field ({pattern}); retention "
            "must be decided by retention_mode alone"
        )


def test_migration_contains_no_prose_retention_heuristics():
    source = _module_source("migration").lower()
    for literal in _PROSE_LITERALS:
        assert literal not in source, (
            f"migration.py matches on the prose phrase {literal!r}; a note's "
            "wording must never influence a retention decision"
        )
