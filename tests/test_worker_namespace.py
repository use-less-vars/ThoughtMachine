"""Worker namespace isolation: on-disk worker dirs and bridge event routing.

Defect under test
─────────────────
A worker instance label (``<name>`` or ``<name>#<N>``) is only unique *within*
one session.  Both resources that used the label as their whole identity were
therefore shared by every session:

* the on-disk worker directory (``workers/<label>``), which holds the worker's
  ``context.json`` transcript, its journal and its attachments directory;
* the bridge's per-worker EventBus subscription map (``_worker_bus_subs``),
  keyed by the bare label, so one session's worker completion could tear down —
  or shadow — another session's identically named worker.

This module pins both halves of the fix:

* ``WorkerRegistry.worker_dir`` builds ``workers/<session_id>/<label>`` and
  degrades to the legacy un-namespaced path (never raising, never escaping
  ``workers/``) when the session id is unusable as a single path segment;
* ``WebAgentBridge`` keys per-worker subscriptions by
  ``"<session_id>:<label>"`` and drops any event that does not provably carry
  its own session id.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.workspace.worker_registry import WORKERS_DIRNAME, WorkerRegistry
from web_ui.backend.bridge import WebAgentBridge


# ── On-disk worker directory namespace ────────────────────────────────────────


def test_same_worker_name_in_two_sessions_gets_distinct_dirs(tmp_path):
    """Two sessions running a worker called ``default`` must not share a dir."""
    dir_a = WorkerRegistry.worker_dir(tmp_path, "session-a", "default")
    dir_b = WorkerRegistry.worker_dir(tmp_path, "session-b", "default")

    assert dir_a != dir_b, (
        "two sessions' 'default' workers resolve to the same directory — their "
        f"transcripts would collide: {dir_a}"
    )
    assert dir_a == Path(tmp_path) / WORKERS_DIRNAME / "session-a" / "default"
    assert dir_b == Path(tmp_path) / WORKERS_DIRNAME / "session-b" / "default"


def test_worker_instances_in_one_session_get_distinct_dirs(tmp_path):
    """Instance 1 stays the bare name; instance N>1 gets the ``#N`` suffix."""
    dir_1 = WorkerRegistry.worker_dir(tmp_path, "session-a", "default", 1)
    dir_2 = WorkerRegistry.worker_dir(tmp_path, "session-a", "default", 2)

    assert dir_1 == Path(tmp_path) / WORKERS_DIRNAME / "session-a" / "default"
    assert dir_2 == Path(tmp_path) / WORKERS_DIRNAME / "session-a" / "default#2"
    assert dir_1 != dir_2


@pytest.mark.parametrize("missing_session", [None, ""])
def test_missing_session_id_falls_back_to_legacy_layout(tmp_path, missing_session):
    """A sessionless (operator/test) spawn keeps the directory it always used
    and never raises."""
    path = WorkerRegistry.worker_dir(tmp_path, missing_session, "default")

    assert path == Path(tmp_path) / WORKERS_DIRNAME / "default"


@pytest.mark.parametrize(
    "hostile",
    ["../etc", "..", ".", "a/b", "a\\b", "/", "\\", "x\x00y", "../../root"],
)
def test_hostile_session_ids_stay_inside_workers_dir(tmp_path, hostile):
    """A hostile session id can never widen the path beyond ``workers/``."""
    workers_root = Path(tmp_path) / WORKERS_DIRNAME

    path = WorkerRegistry.worker_dir(tmp_path, hostile, "default")

    assert workers_root in path.parents, (
        f"session id {hostile!r} produced a path outside workers/: {path}"
    )
    assert path == workers_root / "default"


@pytest.mark.parametrize(
    "hostile_name,expected_label,expected_label_2",
    [
        ("a/b", "a_b", "a_b#2"),
        ("../x", "_x", "_x#2"),
        # A '.'/'' name keeps the '#N' suffix: only LEADING dots are stripped.
        (".", "worker", "#2"),
        ("", "worker", "#2"),
    ],
)
def test_hostile_worker_names_are_sanitised_not_used(tmp_path, hostile_name,
                                                     expected_label,
                                                     expected_label_2):
    """An unsafe worker NAME can never widen the path either.

    Both branches of ``worker_dir`` put the label in the path, so a name such as
    ``a/b`` or ``../x`` used verbatim would escape ``workers/``.  The label is
    sanitised instead: rewritten, never rejected (the no-raise contract), and
    always exactly one segment directly under the expected parent.
    """
    workers_root = Path(tmp_path) / WORKERS_DIRNAME

    # A usable session still namespaces the (sanitised) label: an unsafe NAME
    # must not change the SHAPE of the path, only its last segment.
    sessioned = WorkerRegistry.worker_dir(tmp_path, "session-a", hostile_name)
    assert sessioned == workers_root / "session-a" / expected_label, sessioned

    # Higher instances keep the ``#N`` suffix on top of the sanitised name.
    second = WorkerRegistry.worker_dir(tmp_path, "session-a", hostile_name, 2)
    assert second == workers_root / "session-a" / expected_label_2, second

    # Sessionless (operator/test) spawn: the sanitised label sits under workers/.
    legacy = WorkerRegistry.worker_dir(tmp_path, None, hostile_name)
    assert legacy == workers_root / expected_label, legacy

    for path in (sessioned, second, legacy):
        assert workers_root in path.parents, (
            f"worker name {hostile_name!r} produced a path outside workers/: {path}"
        )


# ── Bridge per-worker subscription namespace ─────────────────────────────────


def _worker_event(session_id, worker_name="w1"):
    """A minimal worker event as the bridge's global-bus handlers see it."""
    data = {"worker_name": worker_name, "instance_id": 1}
    if session_id is not None:
        data["session_id"] = session_id
    return SimpleNamespace(
        type=SimpleNamespace(value="worker_completed"),
        data=data,
        metadata=SimpleNamespace(timestamp=datetime.now(timezone.utc)),
    )


def test_bridge_forwards_only_its_own_session_events():
    """Own-session events are forwarded; foreign and session-less ones dropped."""
    received = []
    bridge = WebAgentBridge(event_callback=lambda ev: received.append(ev))
    bridge._session_id = "sess-own"
    try:
        bridge._forward_worker_event(_worker_event("sess-own"))
        assert [e.get("type") for e in received] == ["worker:worker_completed"], received
        assert received[0].get("session_id") == "sess-own", received[0]

        received.clear()
        bridge._forward_worker_event(_worker_event("sess-other"))
        assert received == [], (
            "an event from another session was forwarded into this session's tab"
        )

        # Deny-by-default: an event that cannot be attributed to a session is
        # dropped rather than guessed at.
        for unattributable in (None, ""):
            received.clear()
            bridge._forward_worker_event(_worker_event(unattributable))
            assert received == [], (
                f"an event with session_id={unattributable!r} was forwarded"
            )
    finally:
        try:
            bridge._unsubscribe_worker_events()
        except Exception:
            pass


# -- Session attribution on the event path (per-worker bus + global bus) -----


def test_per_worker_bus_adapter_stamps_session_id():
    """Events published through WorkerBusAdapter carry the owning session.

    Both the payload AND the metadata must carry it: the per-worker bus is
    consumed by a bridge that denies by default on ``data['session_id']``, and
    the legacy-dict conversion moves a payload session_id into the metadata, so
    metadata alone is not enough to attribute the event.
    """
    from agent.events import EventBus, EventType
    from tools.workspace.worker_thread import WorkerBusAdapter

    bus = EventBus()
    seen = []
    bus.subscribe(EventType.WORKER_MESSAGE, lambda ev: seen.append(ev))
    adapter = WorkerBusAdapter(bus, "w1", session_id="sess-own")

    adapter._publish("worker_message", {"content": "hi"})

    assert len(seen) == 1, seen
    event = seen[0]
    assert event.data.get("session_id") == "sess-own", event.data
    assert event.metadata.session_id == "sess-own", event.metadata
    assert event.data.get("worker_name") == "w1", event.data


def test_bridge_forwards_own_session_token_warning_only():
    """`_on_worker_token_warning` forwards only provably own-session warnings.

    The main agent publishes token warnings through
    ``convert_from_legacy_format``, which moves ``data['session_id']`` into the
    metadata.  A guard that reads only the payload is dead, and every tab then
    sees every session's warning -- the regression this pins.
    """
    from agent.events import EventMetadata, TokenWarningEvent

    def _warning(payload_session, metadata_session, source="agent"):
        data = {
            "old_state": "LOW",
            "new_state": "WARNING",
            "token_count": 68_000,
            "warning_message": "context nearly full",
        }
        if payload_session is not None:
            data["session_id"] = payload_session
        return TokenWarningEvent(
            type="token_warning",
            metadata=EventMetadata(source=source, session_id=metadata_session),
            data=data,
        )

    received = []
    bridge = WebAgentBridge(event_callback=lambda ev: received.append(ev))
    bridge._session_id = "sess-own"
    try:
        # Own session, metadata-only (the shape the main agent actually emits).
        bridge._on_worker_token_warning(_warning(None, "sess-own"))
        assert [e.get("type") for e in received] == ["worker:system_notification"], received
        assert received[0].get("session_id") == "sess-own", received[0]

        # Own session carried in the payload instead of the metadata.
        received.clear()
        bridge._on_worker_token_warning(_warning("sess-own", ""))
        assert len(received) == 1, received

        # Another session's warning, in either location, must not be forwarded.
        for foreign in (
            ("sess-other", "sess-other"),
            (None, "sess-other"),
            ("sess-other", ""),
        ):
            received.clear()
            bridge._on_worker_token_warning(_warning(*foreign))
            assert received == [], (
                f"a token warning for another session was forwarded: {foreign!r}"
            )

        # Deny-by-default: no session anywhere -> dropped, never guessed at.
        received.clear()
        bridge._on_worker_token_warning(_warning(None, ""))
        assert received == [], (
            "an unattributed token warning was forwarded into every tab"
        )

        # Worker-sourced warnings stay on the per-worker path only.
        received.clear()
        bridge._on_worker_token_warning(_warning("sess-own", "sess-own", source="worker"))
        assert received == [], "a worker-sourced warning was forwarded twice"
    finally:
        try:
            bridge._unsubscribe_worker_events()
        except Exception:
            pass



def test_load_worker_contexts_never_loads_another_session(tmp_path, monkeypatch):
    """``_load_worker_contexts`` must not import another session's transcript.

    ``_persisted_workers`` is keyed by BARE worker label, so a loader that
    walked every entry under ``workers/`` would key another session's
    ``workers/<other-session>/<label>/`` under ``<label>`` and hand it to this
    session via ``resume_worker(<label>)``.  The loader must read only its own
    session directory plus the legacy sessionless layout.
    """
    import json

    monkeypatch.setattr(
        "thoughtmachine.workspace_capabilities._workspace_dir",
        lambda workspace_id: tmp_path,
    )

    workers = tmp_path / "workers"
    own = workers / "sess-own" / "default"
    own.mkdir(parents=True)
    (own / "context.json").write_text(json.dumps({"who": "own"}), encoding="utf-8")
    # Another session: one worker sharing our label, one this session never had.
    for label in ("default", "ghost"):
        foreign = workers / "sess-other" / label
        foreign.mkdir(parents=True)
        (foreign / "context.json").write_text(
            json.dumps({"who": "other-" + label}), encoding="utf-8"
        )
    legacy = workers / "legacy1"
    legacy.mkdir()
    (legacy / "context.json").write_text(json.dumps({"who": "legacy"}), encoding="utf-8")

    bridge = WebAgentBridge(event_callback=lambda ev: None)
    bridge._workspace_id = "ws-under-test"
    bridge._session_id = "sess-own"
    try:
        bridge._load_worker_contexts()

        # Only our own session's worker plus the sessionless legacy one.
        assert set(bridge._persisted_workers) == {"default", "legacy1"}
        assert bridge.resume_worker("default") == {"who": "own"}
        assert bridge.resume_worker("legacy1") == {"who": "legacy"}
        # A label this session never had must not resolve to foreign history.
        assert bridge.resume_worker("ghost") is None
        assert "other" not in json.dumps(bridge._persisted_workers)
    finally:
        try:
            bridge._unsubscribe_worker_events()
        except Exception:
            pass


def test_load_worker_contexts_own_session_wins_over_legacy(tmp_path, monkeypatch):
    """On a label collision the CURRENT session's transcript must win.

    An in-place upgrade leaves the pre-ticket legacy ``workers/<label>/`` next
    to the new ``workers/<session_id>/<label>/``; both map to the same bare
    cache key, so the loader must resolve the collision by construction (legacy
    pass first, own-session pass second), never by directory iteration order.
    """
    import json

    monkeypatch.setattr(
        "thoughtmachine.workspace_capabilities._workspace_dir",
        lambda workspace_id: tmp_path,
    )

    workers = tmp_path / "workers"
    legacy_dup = workers / "legacydup"
    legacy_dup.mkdir(parents=True)
    (legacy_dup / "context.json").write_text(
        json.dumps({"who": "legacy-stale"}), encoding="utf-8"
    )
    own_dup = workers / "sess-own" / "legacydup"
    own_dup.mkdir(parents=True)
    (own_dup / "context.json").write_text(
        json.dumps({"who": "own-current"}), encoding="utf-8"
    )

    bridge = WebAgentBridge(event_callback=lambda ev: None)
    bridge._workspace_id = "ws-under-test"
    bridge._session_id = "sess-own"
    try:
        bridge._load_worker_contexts()

        assert bridge.resume_worker("legacydup") == {"who": "own-current"}
        assert bridge._persisted_workers["legacydup"]["name"] == "legacydup"
        assert "legacy-stale" not in json.dumps(bridge._persisted_workers)
    finally:
        try:
            bridge._unsubscribe_worker_events()
        except Exception:
            pass
