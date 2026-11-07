"""Characterisation guard for ``web_ui.backend.server._map_container_view_state``.

This pins the *user-visible* entry ``state`` produced from every (raw status,
record state) input.  It is a characterisation guard: it MUST pass both on the
pre-routing code (an inline record-state lookup) and after the display consumer
is routed through the pure ``state.resolve_container_state`` resolver --
byte-identical output for every input.

The resolver is now fed a ``live_state`` keyword: ``None`` when no live status
dict is present, the raw Docker ``status`` string when it is a non-empty
string, else the unknown sentinel ``"unknown"``.  This is a DISPLAY path that
reads only the resolved state and ignores the freshness flag, so the rendered
values are unchanged by that routing -- the rows below pin them.
"""

from __future__ import annotations

import pytest

import web_ui.backend.server as server_module

_MAP = server_module._map_container_view_state

# (raw status dict-or-None, record.state, expected entry state)
_RECORD_STATE_CASES = [
    # (a) no usable status -> record-state fallback
    (None, "running", "running"),
    (None, "creating", "creating"),
    (None, "", "stopped"),
    (None, "stopped", "stopped"),
    (None, "bogus", "stopped"),
    # (b) present-but-unknown raw status -> record-state fallback
    ({"status": "bogus"}, "running", "running"),
    ({"status": "bogus"}, "creating", "creating"),
    ({"status": "bogus"}, "", "stopped"),
    ({"status": "bogus"}, "bogus", "stopped"),
    # (c) OOM override wins over record state
    ({"status": "running", "oom_killed": True}, "creating", "oom"),
    # (d) known raw status wins over record state
    ({"status": "exited"}, "creating", "exited"),
    ({"status": "running"}, "stopped", "running"),
    # (e) present-but-empty raw status -> unknown sentinel (still a live
    # container); display still renders the record's own state vocabulary
    ({"status": ""}, "creating", "creating"),
    ({"status": ""}, "running", "running"),
    ({"status": ""}, "", "stopped"),
]

# Full raw-state table, with no record state.
_RAW_CASES = [
    ({"status": "running"}, "running"),
    ({"status": "restarting"}, "running"),
    ({"status": "paused"}, "paused"),
    ({"status": "exited"}, "exited"),
    ({"status": "dead"}, "exited"),
    ({"status": "created"}, "stopped"),
    ({"status": "stopped"}, "stopped"),
    ({"status": "removing"}, "stopped"),
    ({"status": "missing"}, "stopped"),
    ({"status": "error"}, "stopped"),
]


@pytest.mark.parametrize("status,record_state,expected", _RECORD_STATE_CASES)
def test_record_state_fallback_characterisation(status, record_state, expected):
    assert _MAP(status, record_state) == expected


@pytest.mark.parametrize("status,expected", _RAW_CASES)
def test_raw_state_table_characterisation(status, expected):
    assert _MAP(status, "") == expected
