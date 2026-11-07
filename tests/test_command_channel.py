"""Unit tests for the fenced command channel (``tools/workspace/command_channel``).

Pure filesystem tests against ``tmp_path`` -- no worker thread, no second
process. The module's entire contract is the atomics of ONE file plus the
strictness of ONE fence parse, so a real directory is the whole fixture;
what needs pinning is that the live ``command.json`` is never read and
never unlinked by the consumer (the read/unlink race), that a rejected
command is preserved rather than deleted, and that a fence with a single
wrong axis is refused.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import tools.workspace.command_channel as channel
from tools.workspace.command_channel import (
    COMMAND_FILENAME,
    REJECTED_MARKER,
    CommandFence,
    claim_command,
    discard,
    fence_rejection_reason,
    quarantine,
    write_command,
)

FENCE = CommandFence(session_id="sess-a", worker_name="w1", instance_id=1, epoch=3)

AXES = [
    ("session_id", "sess-b"),
    ("worker_name", "w2"),
    ("instance_id", 2),
    ("epoch", 4),
]


def _live(tmp_path: Path) -> Path:
    return tmp_path / COMMAND_FILENAME


def _names(tmp_path: Path) -> list:
    """Every entry in the worker dir -- temp files and claims included."""
    return sorted(p.name for p in tmp_path.iterdir())


def _claim_one(tmp_path: Path, action: str = "kill") -> Path:
    assert write_command(tmp_path, action, FENCE) is True
    claimed = claim_command(tmp_path)
    assert claimed is not None
    return claimed[0]


# ── write_command / claim_command: the roundtrip ────────────────────────────


def test_write_then_claim_preserves_every_fence_axis(tmp_path):
    assert write_command(tmp_path, "kill", FENCE) is True
    assert _live(tmp_path).is_file()

    claimed, text = claim_command(tmp_path)
    assert claimed is not None
    assert claimed != _live(tmp_path)
    assert claimed.is_file()
    assert _live(tmp_path).exists() is False, "the claim must consume the live file"

    payload = json.loads(text)
    assert payload["action"] == "kill"
    assert payload["session_id"] == "sess-a"
    assert payload["worker_name"] == "w1"
    assert payload["instance_id"] == 1
    assert payload["epoch"] == 3
    assert CommandFence.from_payload(payload) == FENCE


def test_reason_is_written_only_when_given(tmp_path):
    assert write_command(tmp_path, "kill", FENCE, reason="operator asked") is True
    claimed = claim_command(tmp_path)
    assert claimed is not None
    path, text = claimed
    assert json.loads(text)["reason"] == "operator asked"
    discard(path)

    assert write_command(tmp_path, "kill", FENCE) is True
    claimed = claim_command(tmp_path)
    assert claimed is not None
    path, text = claimed
    assert "reason" not in json.loads(text)
    discard(path)


def test_a_claim_consumes_atomically_and_only_once(tmp_path):
    _claim_one(tmp_path, "stop")
    assert claim_command(tmp_path) is None, "a claimed command must not be claimable twice"


def test_write_leaves_no_temp_file_behind(tmp_path):
    assert write_command(tmp_path, "kill", FENCE) is True
    assert _names(tmp_path) == [COMMAND_FILENAME]


@pytest.mark.parametrize("target", ["replace", "fsync"])
def test_write_failure_returns_false_leaves_no_debris_and_never_raises(
    tmp_path, monkeypatch, target
):
    def _boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(channel.os, target, _boom)

    assert write_command(tmp_path, "kill", FENCE) is False
    assert _names(tmp_path) == [], "a failed write must clean up its temp file"
    assert claim_command(tmp_path) is None


def test_write_into_a_missing_directory_returns_false(tmp_path):
    assert write_command(tmp_path / "not-there", "kill", FENCE) is False


def test_a_command_written_after_a_claim_survives_for_the_next_claim(tmp_path):
    """The race this module exists to kill.

    The consumer claims (live file renamed away), then a NEW command lands
    on the live path, then the consumer discards its claim. The new command
    must still be there -- the old read-then-unlink loop destroyed exactly
    this command.
    """
    first = _claim_one(tmp_path, "pause")
    assert json.loads(first.read_text(encoding="utf-8"))["action"] == "pause"

    assert write_command(tmp_path, "kill", FENCE) is True
    discard(first)

    second = claim_command(tmp_path)
    assert second is not None
    assert second[0] != first
    assert json.loads(second[1])["action"] == "kill"


# ── CommandFence: matching and strict parsing ───────────────────────────────


def test_an_exact_fence_matches_and_yields_no_reason(tmp_path):
    assert FENCE.matches(FENCE) is True
    assert FENCE.matches(replace(FENCE)) is True
    assert fence_rejection_reason(FENCE, FENCE.to_payload()) is None
    assert write_command(tmp_path, "kill", FENCE) is True
    payload = json.loads(_live(tmp_path).read_text(encoding="utf-8"))
    assert fence_rejection_reason(FENCE, payload) is None


@pytest.mark.parametrize("axis,value", AXES)
def test_a_single_axis_mismatch_rejects_the_fence(axis, value):
    other = replace(FENCE, **{axis: value})
    assert FENCE.matches(other) is False
    assert other.matches(FENCE) is False
    assert CommandFence.from_payload(other.to_payload()) == other

    reason = fence_rejection_reason(FENCE, other.to_payload())
    assert reason is not None and axis in reason, reason


def test_only_the_disagreeing_axes_are_named():
    other = CommandFence(session_id="sess-a", worker_name="w1", instance_id=2, epoch=9)
    reason = fence_rejection_reason(FENCE, other.to_payload())
    assert reason == "instance_id 2 != 1, epoch 9 != 3"


def test_from_payload_accepts_zero_epoch():
    zero = CommandFence(session_id="s", worker_name="w", instance_id=1, epoch=0)
    assert CommandFence.from_payload(zero.to_payload()) == zero


@pytest.mark.parametrize("payload", [[], "str", None, 42, ()])
def test_from_payload_rejects_non_dict_without_raising(payload):
    assert CommandFence.from_payload(payload) is None
    assert fence_rejection_reason(FENCE, payload) == "no fence in payload"


@pytest.mark.parametrize("axis,_", AXES)
def test_from_payload_rejects_a_missing_key(axis, _):
    payload = FENCE.to_payload()
    del payload[axis]
    assert CommandFence.from_payload(payload) is None
    assert fence_rejection_reason(FENCE, payload) == "no fence in payload"


def test_from_payload_rejects_an_empty_dict():
    assert CommandFence.from_payload({}) is None
    assert fence_rejection_reason(FENCE, {}) == "no fence in payload"


_BAD_TYPES = [
    pytest.param({"session_id": None}, id="none-session"),
    pytest.param({"worker_name": None}, id="none-worker"),
    pytest.param({"instance_id": None}, id="none-instance"),
    pytest.param({"epoch": None}, id="none-epoch"),
    pytest.param({"instance_id": True}, id="bool-instance-true"),
    pytest.param({"epoch": True}, id="bool-epoch-true"),
    pytest.param({"instance_id": False}, id="bool-instance-false"),
    pytest.param({"instance_id": "1"}, id="numeric-string-instance"),
    pytest.param({"epoch": "3"}, id="numeric-string-epoch"),
    pytest.param({"session_id": 7}, id="int-session"),
    pytest.param({"worker_name": ["w1"]}, id="list-worker"),
    pytest.param({"instance_id": 1.0}, id="float-instance"),
]


@pytest.mark.parametrize("override", _BAD_TYPES)
def test_from_payload_rejects_wrong_types(override):
    payload = FENCE.to_payload()
    payload.update(override)
    assert CommandFence.from_payload(payload) is None
    assert fence_rejection_reason(FENCE, payload) == "malformed fence in payload"


# ── quarantine: keep the evidence, never delete, never escape the dir ───────


def test_quarantine_moves_the_claim_and_never_recreates_the_live_name(tmp_path):
    claimed = _claim_one(tmp_path)
    dest = quarantine(claimed, "sess-b/w1/1/3")

    assert dest is not None
    assert dest.is_file()
    assert claimed.exists() is False
    assert dest.name.startswith(COMMAND_FILENAME + REJECTED_MARKER)
    assert dest.parent == tmp_path
    assert _live(tmp_path).exists() is False, "quarantine must not touch the live name"


def test_quarantine_never_overwrites_a_previous_rejection(tmp_path):
    first = quarantine(_claim_one(tmp_path, "stop"), "same-fence")
    second = quarantine(_claim_one(tmp_path, "kill"), "same-fence")

    assert first is not None and second is not None
    assert first != second
    assert first.is_file() and second.is_file()
    assert json.loads(first.read_text(encoding="utf-8"))["action"] == "stop"
    assert json.loads(second.read_text(encoding="utf-8"))["action"] == "kill"


def test_quarantine_sanitises_a_hostile_fence_repr(tmp_path):
    claimed = _claim_one(tmp_path)
    hostile = "../../etc/passwd ../.. a b\tc\x00" * 20
    dest = quarantine(claimed, hostile)

    assert dest is not None
    assert dest.is_file()
    assert dest.parent == tmp_path, "the rejection must stay inside the worker dir"
    assert "/" not in dest.name
    assert "\\" not in dest.name
    assert "\x00" not in dest.name
    assert ".." not in dest.name
    assert " " not in dest.name
    assert len(dest.name) < 160


def test_quarantine_of_a_missing_path_returns_none(tmp_path):
    assert quarantine(tmp_path / "gone.json", "whatever") is None


# ── discard: idempotent, never raises ──────────────────────────────────────


def test_discard_on_a_missing_path_does_not_raise(tmp_path):
    discard(tmp_path / "never-existed.json")
    claimed = _claim_one(tmp_path)
    discard(claimed)
    discard(claimed)
    assert claimed.exists() is False
