"""Isolation-boundary e2e tests — slice 1 (WebSocket cross-workspace attacks).

A WebSocket connection bound to workspace **A** must not be able to pivot onto
— or destroy — a session belonging to workspace **B**.

Two tests only:

  * ``test_load_session_cannot_pivot_to_other_workspace``
  * ``test_delete_session_cannot_remove_other_workspace_session``

Both drive the real server (REST create + WebSocket commands) through the
shared hermetic harness in ``tests/integration/harness.py`` (temp HOME, patched
``pathlib.Path.home``, purged/re-imported ``web_ui.backend`` modules), and both
``print()`` the verbatim request/response payloads so ``pytest -s`` shows the
raw wire traffic.

Guards under test (source references):

  * ``web_ui/backend/bridge.py:1927-1932`` — ``load_session`` refuses when the
    requested ``workspace_id`` differs from the bridge's binding:
    "load_session refused: workspace {requested!r} requested but bridge is
    scoped to {bound!r} (session {sid})" → returns ``False``, bridge untouched.
  * ``web_ui/backend/server.py:2563-2585`` — ``delete_session`` takes
    ``workspace_id`` straight from the client message, ignores the return value
    of ``session_store.delete_session`` and performs no caller-workspace check
    at all.  The behaviour asserted here is refusal.

If an attack succeeds, that is a reported finding — the assertions below are
NOT weakened to accommodate it.
"""

from __future__ import annotations

import glob
import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest

_HERE = Path(__file__).resolve().parent
_INTEGRATION_DIR = (
    _HERE / "integration"
    if (_HERE / "integration" / "harness.py").is_file()
    else _HERE
)
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

import harness as H  # noqa: E402

pytestmark = pytest.mark.integration


# ────────────────────────────────────────────────────────────────────────────
# Hermetic full-server fixture (shape copied from
# tests/integration/test_ws_event_contracts.py::contract_server, backed by the
# shared harness instead of an inline copy).
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def iso_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server."""
    app, tmp_home, stop_fn = H.start_backend()
    try:
        import web_ui.backend.server as server_mod

        H.register_mock_provider(server_mod)
        yield app, tmp_home, server_mod
    finally:
        stop_fn(rmtree=True, restore_env=True)


# ────────────────────────────────────────────────────────────────────────────
# Small WebSocket helper: one reader thread per connection, so a timed-out read
# can never steal the *next* event (the failure mode of a per-call
# ``receive_json`` wrapper).  Every send and every received event is printed
# verbatim.
# ────────────────────────────────────────────────────────────────────────────

class WsPump:
    """Continuous reader for one ``/ws`` connection."""

    def __init__(self, ws):
        self.ws = ws
        self.q: "queue.Queue" = queue.Queue()
        self.closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                self.q.put(self.ws.receive_json())
            except Exception:  # noqa: BLE001 - socket closed / portal gone
                self.q.put(None)
                return

    def send(self, payload):
        self.ws.send_json(payload)

    def wait_for(self, stop_types, timeout=10.0, max_events=60):
        events = []
        deadline = time.monotonic() + timeout
        while len(events) < max_events:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                evt = self.q.get(timeout=remaining)
            except queue.Empty:
                break
            if evt is None:
                self.closed = True
                break
            events.append(evt)
            if evt.get("type") in stop_types:
                break
        return events

    def drain(self, timeout=1.0, max_events=40):
        return self.wait_for(set(), timeout=timeout, max_events=max_events)


_MAX_TEXT = 900


def _brief(obj, limit=_MAX_TEXT):
    """json-dump obj, eliding/truncating oversized values so logs stay readable."""
    if isinstance(obj, dict):
        slim = {}
        elided = False
        for key, val in obj.items():
            text = json.dumps(val, sort_keys=True, default=str)
            if len(text) > 200:
                slim[key] = "<%d chars elided>" % len(text)
                elided = True
            else:
                slim[key] = val
        dumped = json.dumps(slim, sort_keys=True, default=str)
        if len(dumped) <= limit:
            return dumped + ("  (large values elided)" if elided else "")
        obj = slim
    text = json.dumps(obj, sort_keys=True, default=str)
    if len(text) > limit:
        text = text[:limit] + "...[truncated %d chars]" % (len(text) - limit)
    return text


def _show(payload, events):
    """Print the request dict and every response event (bounded for readability)."""
    print("\n[REQUEST]  " + _brief(payload))
    if not events:
        print("[RESPONSE] <no events>")
    for evt in events:
        print("[RESPONSE] " + _brief(evt))


def _command(pump, payload, stop_types, timeout=10.0, max_events=60):
    pump.send(payload)
    return pump.wait_for(stop_types, timeout=timeout, max_events=max_events)


def _open_ws(client):
    cm = H.ws_connect(client)
    ws = cm.__enter__()          # required: without __enter__ there is no portal
    return cm, WsPump(ws)


def _close_ws(cm):
    try:
        cm.__exit__(None, None, None)
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def _open_bound_ws(client, session_id, workspace_id):
    """Open a WS connection and bind it to ``workspace_id`` via load_session."""
    cm, pump = _open_ws(client)
    payload = {
        "command": "load_session",
        "session_id": session_id,
        "workspace_id": workspace_id,
    }
    events = _command(pump, payload, {"session_loaded", "status_message"}, timeout=20.0)
    _show(payload, events)
    bound = [
        e
        for e in events
        if e.get("type") == "session_loaded"
        and e.get("workspace_id") == workspace_id
        and not e.get("load_error")
    ]
    assert bound, (
        f"could not bind WS to workspace {workspace_id} with session "
        f"{session_id}: events={events!r}"
    )
    pump.drain(timeout=1.0)
    return cm, pump


def _two_workspaces(client, tmp_home, tag):
    info_a = H.create_workspace(client, tmp_home, name=f"iso_{tag}_a")
    info_b = H.create_workspace(client, tmp_home, name=f"iso_{tag}_b")
    print("[SETUP] workspace A payload: " + json.dumps(info_a, sort_keys=True, default=str))
    print("[SETUP] workspace B payload: " + json.dumps(info_b, sort_keys=True, default=str))
    id_a = info_a.get("id") or info_a.get("workspace_id")
    id_b = info_b.get("id") or info_b.get("workspace_id")
    assert id_a and id_b, f"workspace ids missing: A={info_a!r} B={info_b!r}"
    return id_a, id_b


def _list_sessions(pump):
    payload = {"command": "list_sessions"}
    events = _command(pump, payload, {"sessions_list", "status_message"}, timeout=20.0)
    _show(payload, events)
    return events


def _session_ids(events):
    ids = set()
    for evt in events:
        if evt.get("type") != "sessions_list":
            continue
        for entry in evt.get("sessions") or []:
            if isinstance(entry, dict):
                for key in ("id", "session_id"):
                    if entry.get(key):
                        ids.add(entry[key])
            elif isinstance(entry, str):
                ids.add(entry)
    return ids


def _disk_hits(tmp_home, session_id):
    """Session files on disk belonging to ``session_id`` (layout-agnostic)."""
    pattern = os.path.join(tmp_home, ".thoughtmachine", "**", f"*{session_id}*.json")
    return sorted(glob.glob(pattern, recursive=True))


def _print_server_refusal_lines(captured):
    """Echo the server-side refusal/denial lines produced during the attack."""
    seen = False
    for stream, text in (("stderr", captured.err), ("stdout", captured.out)):
        for line in text.splitlines():
            low = line.lower()
            if "refus" in low or "\u26a0" in line or "denied" in low or "forbidden" in low:
                print(f"[SERVER:{stream}] {line.strip()}")
                seen = True
    if not seen:
        print("[SERVER] no refusal/denial line observed in captured server output")


# ---------------------------------------------------------------------------
# helpers for the controlled case-by-case matrix
# ---------------------------------------------------------------------------

def _print_events(events):
    if not events:
        print("[RESPONSE] <no events>")
    for evt in events:
        print("[RESPONSE] " + _brief(evt))


def _count_json(root):
    """Every .json file under a workspace root (recursive), sorted."""
    if not root or not os.path.isdir(root):
        return []
    return sorted(glob.glob(os.path.join(root, "**", "*.json"), recursive=True))


def _verdict(events, captured, ws_b_id, root_b):
    """Return (loaded_events, adopted_b_events, refused_bool)."""
    loaded = [e for e in events if e.get("type") == "session_loaded"]
    base_b = os.path.basename(str(root_b).rstrip("/")) if root_b else None
    adopted = [
        e
        for e in loaded
        if e.get("workspace_id") == ws_b_id
        or (base_b and os.path.basename(str(e.get("workspace_path") or "").rstrip("/")) == base_b)
    ]
    blob = (captured.out or "") + (captured.err or "")
    refused = (
        "load_session refused" in blob
        or any(e.get("load_error") for e in loaded)
        or any("could not be loaded" in (e.get("text") or "") for e in events)
    )
    return loaded, adopted, refused


def _two_workspaces_full(client, tmp_home, tag):
    """Like _two_workspaces but also returns the two workspace roots."""
    info_a = H.create_workspace(client, tmp_home, name=f"iso_{tag}_a")
    info_b = H.create_workspace(client, tmp_home, name=f"iso_{tag}_b")
    print("[SETUP] workspace A payload: " + json.dumps(info_a, sort_keys=True, default=str))
    print("[SETUP] workspace B payload: " + json.dumps(info_b, sort_keys=True, default=str))
    id_a = info_a.get("id") or info_a.get("workspace_id")
    id_b = info_b.get("id") or info_b.get("workspace_id")
    root_a = info_a.get("root") or os.path.join(tmp_home, f"iso_{tag}_a")
    root_b = info_b.get("root") or os.path.join(tmp_home, f"iso_{tag}_b")
    assert id_a and id_b, f"workspace ids missing: A={info_a!r} B={info_b!r}"
    print(f"[SETUP] A id={id_a} root={root_a}")
    print(f"[SETUP] B id={id_b} root={root_b}")
    return id_a, root_a, id_b, root_b


def _cmd_captured(pump, payload, stop_types, capfd, timeout=20.0):
    """Send a command with fd-level capture around it; print request+events."""
    with capfd.disabled():
        print("\n[REQUEST]  " + _brief(payload))
    capfd.readouterr()                                  # clear
    events = _command(pump, payload, stop_types, timeout=timeout)
    captured = capfd.readouterr()
    with capfd.disabled():
        _print_events(events)
        _print_server_refusal_lines(captured)
    return events, captured


# ---------------------------------------------------------------------------
# T1 / CONTROL — is socket A really scoped to A, and can it pivot to B?
# ---------------------------------------------------------------------------

def test_load_session_cannot_pivot_to_other_workspace(iso_server, capfd):
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    with capfd.disabled():
        ws_a_id, root_a, ws_b_id, root_b = _two_workspaces_full(client, tmp_home, "load")
        sid_a = H.create_session(client, workspace_id=ws_a_id)
        sid_b = H.create_session(client, workspace_id=ws_b_id)
        print(f"[SETUP] A session_a={sid_a}  B session_b={sid_b}")
        assert sid_a != sid_b

    # case (c) runs FIRST and last on its own fresh socket; (a) and (b) each get
    # a *fresh* A-bound socket so the previous case cannot contaminate them.
    cases = (
        ("(c) CONTROL load A's OWN session from A-bound socket", "control",
         lambda: {"command": "load_session", "session_id": sid_a, "workspace_id": ws_a_id}),
        ("(a) load B's session, NO workspace_id field", "attack",
         lambda: {"command": "load_session", "session_id": sid_b}),
        ("(b) load B's session WITH workspace_id=B", "attack",
         lambda: {"command": "load_session", "session_id": sid_b, "workspace_id": ws_b_id}),
    )

    results = []
    for label, kind, mk in cases:
        with capfd.disabled():
            print("\n" + "=" * 78)
            print(f"[CASE] {label}")
            print("[SETUP] opening a FRESH socket and binding it to workspace A "
                  f"({ws_a_id}) with A's own session {sid_a}")
        cm, pump = _open_bound_ws(client, sid_a, ws_a_id)   # prints bind request+response
        try:
            payload = mk()
            events, captured = _cmd_captured(
                pump, payload, {"session_loaded", "status_message"}, capfd
            )
        finally:
            _close_ws(cm)
        loaded, adopted, refused = _verdict(events, captured, ws_b_id, root_b)
        with capfd.disabled():
            print(f"[VERDICT] case={label} loaded={len(loaded)} adopted_B={len(adopted)} "
                  f"refused={refused} ws_id={[e.get('workspace_id') for e in loaded]} "
                  f"ws_path={[e.get('workspace_path') for e in loaded]}")
        results.append({
            "label": label, "kind": kind, "payload": payload, "events": events,
            "loaded": loaded, "adopted": adopted, "refused": refused,
            "captured": captured,
        })

    control = results[0]
    attacks = [r for r in results if r["kind"] == "attack"]

    # CONTROL: a socket bound to A must report A's workspace for A's own session.
    ctrl_ok = bool(control["loaded"]) and control["loaded"][0].get("workspace_id") == ws_a_id
    ctrl_path = control["loaded"][0].get("workspace_path") if control["loaded"] else None
    assert ctrl_ok, (
        "CONTROL FAILED: after binding to workspace A, load_session of A's OWN "
        f"session {sid_a} did not return workspace_id={ws_a_id!r} "
        f"(events={control['events']!r}) -> the socket is NOT scoped to A, so any "
        "cross-workspace result below means 'unscoped socket', not 'scoped pivot'."
    )
    print(f"[CONTROL] socket A scoped to A: YES (workspace_id={ws_a_id}, "
          f"workspace_path={ctrl_path})")

    # ATTACKS: no adoption of B, and a refusal reason must be observable.
    for r in attacks:
        assert not r["adopted"], (
            f"BREACH: A-bound socket pivoted onto workspace B ({ws_b_id}) "
            f"case={r['label']} payload={r['payload']!r} "
            f"loaded={r['loaded']!r}"
        )
    for r in attacks:
        assert r["refused"], (
            f"load_session across workspaces had no observable refusal reason "
            f"case={r['label']} events={r['events']!r} "
            f"server_stderr={r['captured'].err!r}"
        )

    # B's session must still exist, proven from B's own side.
    cm_b, pump_b = _open_bound_ws(client, sid_b, ws_b_id)
    try:
        listed = _list_sessions(pump_b)
    finally:
        _close_ws(cm_b)
    listed_ids = _session_ids(listed)
    disk = _disk_hits(tmp_home, sid_b)
    print(f"[CHECK] B-side ids={sorted(listed_ids)} disk={disk}")
    assert sid_b in listed_ids or disk, (
        f"workspace B's session {sid_b} is gone after A's load_session attempts; "
        f"listed={sorted(listed_ids)} disk={disk}"
    )


# ---------------------------------------------------------------------------
# T2 / CONTROL — delete_session matrix (a, b) + A deleting its OWN session (c)
# ---------------------------------------------------------------------------

def test_delete_session_cannot_remove_other_workspace_session(iso_server, capfd):
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    with capfd.disabled():
        ws_a_id, root_a, ws_b_id, root_b = _two_workspaces_full(client, tmp_home, "delete")
        sid_a = H.create_session(client, workspace_id=ws_a_id)      # A anchor + case (c) target
        sid_b_anchor = H.create_session(client, workspace_id=ws_b_id)
        print(f"[SETUP] A anchor session={sid_a}  B anchor session={sid_b_anchor}")
        assert sid_a != sid_b_anchor

    cases = (
        ("(a) delete B's session, NO workspace_id", dict()),
        (f"(b) delete B's session WITH workspace_id={ws_b_id}", {"workspace_id": ws_b_id}),
        ("(c) CONTROL delete A's OWN session from A-bound socket", "control"),
    )

    results = []
    cm_anchor, pump_anchor = _open_bound_ws(client, sid_b_anchor, ws_b_id)
    try:
        for n, (label, extra) in enumerate(cases):
            is_control = extra == "control"
            with capfd.disabled():
                print("\n" + "=" * 78)
                print(f"[CASE] {label}")

            # fresh target session per case (a/b: in B; control: A's own sid)
            if is_control:
                target = sid_a
                owner_ws = ws_a_id
                owner_root = root_a
            else:
                target = H.create_session(client, workspace_id=ws_b_id)
                owner_ws = ws_b_id
                owner_root = root_b
                cm_s, pump_s = _open_bound_ws(client, target, ws_b_id)
                try:
                    _cmd_captured(
                        pump_s,
                        {"command": "save_session", "name": f"iso-del-{n}"},
                        {"session_saved", "status_message"},
                        capfd,
                    )
                finally:
                    _close_ws(cm_s)

            before_hit = _disk_hits(tmp_home, target)
            before_own = _count_json(owner_root)
            before_other = _count_json(root_a if owner_root is root_b else root_b)
            before_ids = _session_ids(_list_sessions(pump_anchor))
            with capfd.disabled():
                print(f"[COUNT] target={target} owner_root={owner_root}")
                print(f"[COUNT] before: owner_files={len(before_own)} other_files={len(before_other)} "
                      f"target_hits={before_hit} in_owner_list={target in before_ids}")

            payload = {"command": "delete_session", "session_id": target}
            if isinstance(extra, dict):
                payload.update(extra)

            cm_a, pump_a = _open_bound_ws(client, sid_a, ws_a_id) if not is_control else _open_bound_ws(client, sid_a, ws_a_id)
            try:
                events, captured = _cmd_captured(
                    pump_a, payload, {"session_deleted", "status_message"}, capfd
                )
            finally:
                _close_ws(cm_a)

            deleted = [
                e for e in events
                if e.get("type") == "session_deleted" and e.get("session_id") == target
            ]
            after_hit = _disk_hits(tmp_home, target)
            after_own = _count_json(owner_root)
            after_other = _count_json(root_a if owner_root is root_b else root_b)
            after_ids = _session_ids(_list_sessions(pump_anchor))
            with capfd.disabled():
                print(f"[COUNT] after : owner_files={len(after_own)} other_files={len(after_other)} "
                      f"target_hits={after_hit} in_owner_list={target in after_ids}")
                print(f"[COUNT] owner file diff: {sorted(set(before_own) - set(after_own))}")
                print(f"[VERDICT] case={label} deleted_event={bool(deleted)} "
                      f"survived_disk={bool(after_hit)} survived_list={target in after_ids}")
            results.append({
                "label": label, "control": is_control, "target": target, "payload": payload,
                "deleted": deleted, "events": events, "after_hit": after_hit,
                "after_ids": after_ids, "after_own": after_own, "before_own": before_own,
            })
    finally:
        _close_ws(cm_anchor)

    attacks = [r for r in results if not r["control"]]
    control = [r for r in results if r["control"]][0]

    # CONTROL: A must be able to delete its OWN session (proves delete works at all).
    assert control["deleted"], (
        "CONTROL FAILED: delete_session could not delete A's OWN session "
        f"{control['target']} from an A-bound socket; events={control['events']!r}"
    )
    assert not control["after_hit"] and not (control["target"] in control["after_ids"]), (
        f"CONTROL FAILED: A's own session {control['target']} still present after delete; "
        f"disk={control['after_hit']} listed={control['target'] in control['after_ids']}"
    )
    print("[CONTROL] delete_session works for A's own session (deleted, gone from disk+list)")

    # ATTACKS: foreign delete must be refused and B's session must survive.
    for r in attacks:
        assert not r["deleted"], (
            f"BREACH: delete_session from an A-bound socket was acknowledged for "
            f"workspace B's session case={r['label']} target={r['target']} "
            f"payload={r['payload']!r} events={r['deleted']!r}"
        )
    for r in attacks:
        assert r["after_hit"] or (r["target"] in r["after_ids"]), (
            f"BREACH: delete_session from an A-bound socket destroyed workspace B's "
            f"session case={r['label']} target={r['target']} "
            f"payload={r['payload']!r} disk={r['after_hit']} "
            f"listed={r['target'] in r['after_ids']}"
        )


# ---------------------------------------------------------------------------
# T3 — worker stop/reset: is there a WS path, and does it stay in-workspace?
# ---------------------------------------------------------------------------

class _FakeLiveWorker:
    """Minimal stand-in for a live WorkerThread registry entry (B-owned)."""

    def __init__(self, name, session_id):
        self.worker_name = name
        self.session_id = session_id
        self.status = "ready"
        self._timeout_seconds = 2
        self._worker_ctx = None
        self._worker_dir = None
        self.stop_calls = 0

    def is_alive(self):
        return self.stop_calls == 0

    def stop(self):
        self.stop_calls += 1

    def join(self, timeout=None):
        return None

    def _save_context(self):
        pass

    def _cleanup_worker_containers(self):
        pass

    def _write_status_file(self):
        pass


def test_stop_worker_cannot_touch_other_workspace_worker(iso_server, capfd):
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    from tools.workspace import worker as worker_module

    ws_a_id, _root_a, ws_b_id, _root_b = _two_workspaces_full(client, tmp_home, "worker")
    sid_a = H.create_session(client, workspace_id=ws_a_id)
    sid_b = H.create_session(client, workspace_id=ws_b_id)
    name = "iso-shared-worker"
    fake = _FakeLiveWorker(name, sid_b)
    with worker_module._registry_lock:
        worker_module._worker_registry[(sid_b, name, 1)] = fake
    try:
        # (1) WS dispatch: no worker stop/reset command exists -> unknown cmd.
        cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
        try:
            for command in ("stop_worker", "reset_worker"):
                events, _cap = _cmd_captured(
                    pump, {"command": command, "worker_name": name, "session_id": sid_b},
                    {"status_message"}, capfd,
                )
                print(f"[VERDICT] ws {command} -> events={events!r}")
        finally:
            _close_ws(cm)

        # (2) Tool path: Worker.execute() with A's session must not stop B's worker.
        from tools.workspace.worker import Worker

        for action in ("stop", "reset"):
            with capfd.disabled():
                print(f"\n[REQUEST]  Worker(action={action!r}, worker_name={name!r}, "
                      f"session_id={sid_a!r}  # A-scoped)")
            result = Worker(action=action, worker_name=name, session_id=sid_a).execute()
            with capfd.disabled():
                print("[RESPONSE] " + result)
            assert "stopped successfully" not in result, (
                f"BREACH: A-scoped Worker({action}) claimed a live kill: {result}"
            )
            assert fake.stop_calls == 0, (
                f"BREACH: A-scoped Worker({action}) stopped workspace B's worker "
                f"(stop_calls={fake.stop_calls}) result={result}"
            )
            with worker_module._registry_lock:
                assert (sid_b, name, 1) in worker_module._worker_registry, (
                    f"BREACH: workspace B's worker entry was removed by A's {action}"
                )

        # (3) CONTROL: the same tool scoped to B CAN stop it (refusal is real).
        result_b = Worker(action="stop", worker_name=name, session_id=sid_b).execute()
        with capfd.disabled():
            print("[CONTROL] " + result_b)
        assert fake.stop_calls == 1, (
            "CONTROL FAILED: the Worker tool could not stop B's own worker, so the "
            f"A-scoped refusal above proves nothing: {result_b}"
        )
    finally:
        with worker_module._registry_lock:
            worker_module._worker_registry.pop((sid_b, name, 1), None)


# ---------------------------------------------------------------------------
# T4 — close_session: a socket bound to A must not close B's session.
# ---------------------------------------------------------------------------

def test_close_session_cannot_kill_other_workspace_workers(iso_server, capfd):
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    from tools.workspace import worker as worker_module

    ws_a_id, _root_a, ws_b_id, _root_b = _two_workspaces_full(client, tmp_home, "close")
    sid_a = H.create_session(client, workspace_id=ws_a_id)
    sid_b = H.create_session(client, workspace_id=ws_b_id)
    name = "iso-close-worker"
    fake = _FakeLiveWorker(name, sid_b)
    with worker_module._registry_lock:
        worker_module._worker_registry[(sid_b, name, 1)] = fake
    try:
        # (1) ATTACK: an A-bound socket names B's session in close_session.
        cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
        try:
            events, _cap = _cmd_captured(
                pump, {"command": "close_session", "session_id": sid_b},
                {"session_closed", "status_message"},
                capfd,
            )
            with capfd.disabled():
                print(f"[VERDICT] close_session(session_id=B) -> events={events!r}")
                print(f"[VERDICT] B worker stop_calls={fake.stop_calls}")
        finally:
            _close_ws(cm)

        assert fake.stop_calls == 0, (
            f"BREACH: an A-bound socket closed B's session and killed B's worker "
            f"(stop_calls={fake.stop_calls}, events={events!r})"
        )
        with worker_module._registry_lock:
            assert (sid_b, name, 1) in worker_module._worker_registry, (
                "BREACH: workspace B's worker entry was removed by A's close_session"
            )

        # (2) CONTROL: a B-bound socket CAN close B's session (refusal is real).
        fake_ctl = _FakeLiveWorker(name, sid_b)
        with worker_module._registry_lock:
            worker_module._worker_registry[(sid_b, name, 1)] = fake_ctl
        cm_b, pump_b = _open_bound_ws(client, sid_b, ws_b_id)
        try:
            events_b, _cap_b = _cmd_captured(
                pump_b, {"command": "close_session", "session_id": sid_b},
                {"session_closed", "status_message"},
                capfd,
            )
            with capfd.disabled():
                print(f"[CONTROL] close_session(session_id=own) -> events={events_b!r}")
                print(f"[CONTROL] B worker stop_calls={fake_ctl.stop_calls}")
        finally:
            _close_ws(cm_b)
        assert fake_ctl.stop_calls == 1, (
            "CONTROL FAILED: a B-bound socket could not close B's own session, so "
            f"the A-scoped refusal above proves nothing: events={events_b!r}"
        )
    finally:
        with worker_module._registry_lock:
            worker_module._worker_registry.pop((sid_b, name, 1), None)


def test_a_legit_close_does_not_stop_b_workers(iso_server, capfd):
    """A legit close of A's OWN session must succeed and spare B's workers.

    Modelled on ``test_close_session_cannot_kill_other_workspace_workers``
    (line ~644), but inverted: there the A-bound socket names B's session and
    must be refused, so a refusing-but-also-sweeping implementation could still
    pass. Here A closes its own session legitimately, which MUST succeed and
    stop A's own worker — the only shape that proves the sweep is session
    scoped rather than merely gated.
    """
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    from tools.workspace import worker as worker_module

    ws_a_id, _root_a, ws_b_id, _root_b = _two_workspaces_full(
        client, tmp_home, "legitclose"
    )
    sid_a = H.create_session(client, workspace_id=ws_a_id)
    sid_b = H.create_session(client, workspace_id=ws_b_id)
    name = "iso-legit-close-worker"
    fake_a = _FakeLiveWorker(name, sid_a)
    fake_b = _FakeLiveWorker(name, sid_b)
    with worker_module._registry_lock:
        worker_module._worker_registry[(sid_a, name, 1)] = fake_a
        worker_module._worker_registry[(sid_b, name, 1)] = fake_b
    try:
        # (1) LEGITIMATE close: the A-bound socket closes A's OWN session.
        cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
        try:
            events, captured = _cmd_captured(
                pump, {"command": "close_session", "session_id": sid_a},
                {"session_closed", "status_message"},
                capfd,
            )
            with capfd.disabled():
                print(f"[VERDICT] legit close_session(own) -> events={events!r}")
                print(
                    f"[VERDICT] A stop_calls={fake_a.stop_calls} "
                    f"B stop_calls={fake_b.stop_calls}"
                )
        finally:
            _close_ws(cm)

        # (2) The legitimate close SUCCEEDED: yes-verdict, no refusal anywhere.
        assert any(e.get("type") == "session_closed" for e in events), (
            "a socket could not close its own session: "
            f"events={events!r}"
        )
        refusals = [
            e.get("text") for e in events
            if e.get("type") == "status_message"
            and "refused" in str(e.get("text", "")).lower()
        ]
        assert not refusals, f"LEGIT close was refused: {refusals!r}"
        assert "Close refused" not in captured.out, (
            "LEGIT close was refused server-side: "
            f"captured={captured.out!r}"
        )

        # (3) CONTROL: the close really swept A's own worker.
        assert fake_a.stop_calls == 1, (
            "CONTROL FAILED: closing A's own session did not stop A's own worker, "
            f"so 'B was spared' proves nothing: events={events!r}"
        )

        # (4) DISCRIMINATOR: B's worker was never touched and is still registered.
        assert fake_b.stop_calls == 0, (
            "BREACH: A's own close_session stopped workspace B's worker "
            f"(stop_calls={fake_b.stop_calls}, events={events!r})"
        )
        with worker_module._registry_lock:
            assert (sid_b, name, 1) in worker_module._worker_registry, (
                "BREACH: A's own close_session removed workspace B's worker entry"
            )
    finally:
        with worker_module._registry_lock:
            worker_module._worker_registry.pop((sid_a, name, 1), None)
            worker_module._worker_registry.pop((sid_b, name, 1), None)


# ---------------------------------------------------------------------------
# T5 / ATTACK 6 — re-scoping: can a bound socket switch its own workspace?
# ---------------------------------------------------------------------------

def test_new_session_cannot_rescope_bound_socket(iso_server, capfd):
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    ws_a_id, root_a, ws_b_id, root_b = _two_workspaces_full(client, tmp_home, "rescope")
    sid_a = H.create_session(client, workspace_id=ws_a_id)
    sid_b = H.create_session(client, workspace_id=ws_b_id)

    cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
    try:
        # ATTACK: ask for a new session in B, naming B both ways the frontend
        # could (workspace_id is read; workspace_path is included to prove
        # whether it is read too).
        ns_payload = {
            "command": "new_session",
            "workspace_id": ws_b_id,
            "workspace_path": root_b,
            "mode": "custom",
        }
        ns_events, ns_cap = _cmd_captured(
            pump, ns_payload, {"session_loaded", "status_message"}, capfd,
        )
        _, ns_out = _brief(ns_events), ns_cap.out or ""
        rescoped = [
            e for e in ns_events
            if e.get("type") == "session_loaded" and e.get("workspace_id") == ws_b_id
        ]
        with capfd.disabled():
            print(f"[VERDICT] new_session(workspace_id=B) -> rescoped_events={rescoped!r}")
        assert not rescoped, (
            "RESCOPE: an A-bound socket re-scoped itself to workspace B via "
            f"new_session: {rescoped!r}"
        )

        # DISCRIMINATOR: whatever the re-scope attempt did, the pivot onto B's
        # session must still be refused.
        pivot_payload = {
            "command": "load_session",
            "session_id": sid_b,
            "workspace_id": ws_b_id,
        }
        events, captured = _cmd_captured(
            pump, pivot_payload, {"session_loaded", "status_message"}, capfd,
        )
        pivoted = [
            e for e in events
            if e.get("type") == "session_loaded"
            and e.get("workspace_id") == ws_b_id
            and not e.get("load_error")
        ]
        blob = (captured.out or "") + (captured.err or "") + (ns_out or "")
        refused = (
            "load_session refused" in blob
            or "Close refused" in blob
            or "New session refused" in blob
            or any("refused" in (e.get("text") or "") for e in events)
            or any(e.get("load_error") for e in events)
        )
        with capfd.disabled():
            print(f"[VERDICT] post-rescope pivot -> pivoted={pivoted!r} refused={refused}")
        assert not pivoted and refused, (
            "BREACH (attack 6): an A-bound socket re-scoped itself and then "
            f"pivoted onto workspace B's session {sid_b}: pivoted={pivoted!r} "
            f"refused={refused} new_session_events={ns_events!r} events={events!r}"
        )
    finally:
        _close_ws(cm)


# ---------------------------------------------------------------------------
# T6 / ATTACK 6 residuals — the two other ways a bound socket can re-scope
# ---------------------------------------------------------------------------
#
# Residual (a): ``set_project`` re-scopes the connection from a client-named
#   path.  server.py:3128 takes ``msg["project"]``, confines it (existence +
#   outside-vault only), assigns ``_project_path = new_project``, throws the
#   bridge away and builds a fresh one, then resolves/auto-registers a
#   workspace for that path and assigns ``bridge._workspace_id``.  Nothing
#   compares the requested path against the workspace the socket is already
#   bound to.
#
# Residual (b): ``new_session`` with ``workspace_id`` OMITTED.  The ownership
#   gate added for attack 6 only fires when both ids are present, so the
#   ``_resolve_workspace_id(_project_path)`` fallback (server.py:3015) and the
#   auto-register block (server.py:3032) still re-derive the scope from the
#   connection-local ``_project_path`` — which the client can steer at connect
#   time with the ``?project=`` query parameter or by moving it with
#   ``set_project``.



def _open_ws_with_project(client, project_path):
    """Open a WS connection whose connection-local ``_project_path`` is set.

    ``?project=`` is client-supplied and only confined for existence/outside-
    vault (server.py:1244-1247); it does NOT bind a workspace.  A socket opened
    this way is still *scopeless*, so it is a legitimate bootstrap - and it is
    exactly how an attacker parks a foreign ``_project_path`` on a socket that
    later binds itself to workspace A.
    """
    cm = client.websocket_connect("/ws?project=" + quote(str(project_path), safe=""))
    ws = cm.__enter__()
    return cm, WsPump(ws)


def _slim(events):
    """Scope-bearing fields of session_loaded events (keeps failure output readable)."""
    return [
        {
            "type": e.get("type"),
            "workspace_id": e.get("workspace_id"),
            "workspace_path": e.get("workspace_path"),
            "load_error": e.get("load_error"),
            "text": (e.get("text") or "")[:120],
        }
        for e in events
        if e.get("type") in ("session_loaded", "status_message")
    ]


def _adopted_b(events, ws_b_id, root_b):
    """session_loaded events that adopted workspace B (by id or by root name)."""
    base_b = os.path.basename(str(root_b).rstrip("/")) if root_b else None
    out = []
    for evt in events:
        if evt.get("type") != "session_loaded":
            continue
        path = str(evt.get("workspace_path") or "").rstrip("/")
        if evt.get("workspace_id") == ws_b_id or (
            base_b and path and os.path.basename(path) == base_b
        ):
            out.append(evt)
    return out


def _is_load_reply(evt):
    """True for the terminal ``session_loaded`` of load_session/new_session.

    Every server reply is a multi-message burst, so "the first message whose
    type we care about" is not necessarily the answer to *this* request.  The
    terminal message of a bind attempt is unambiguous: a refusal carries
    ``load_error`` (server.py:2351/2371/2568), a success carries the bound
    ``workspace_id`` and no ``load_error``.
    """
    if evt.get("type") != "session_loaded":
        return False
    return "load_error" in evt or bool(evt.get("workspace_id"))


def _is_project_reply(evt):
    """True for the terminal reply of a ``set_project`` request.

    Refused -> a single ``status_message`` containing "refused"
    (server.py:3249-3256); allowed -> the ``session_loaded`` of the fresh
    session (server.py:3195).  The idle "Ready. Type a query to start."
    status_message that closes a previous burst matches neither.
    """
    if evt.get("type") == "session_loaded":
        return True
    return (
        evt.get("type") == "status_message"
        and "refused" in (evt.get("text") or "").lower()
    )


def _await_terminal(pump, is_terminal, timeout=20.0, max_events=60):
    """Collect events up to the terminal reply; return ``(events, reply)``.

    ``WsPump.wait_for`` breaks on the FIRST event whose *type* is in the stop
    set, so a request issued straight after another one samples that previous
    command's tail: every ``new_session`` reply ends with
    ``status_message "Ready. Type a query to start."`` (server.py:3187), so the
    sample stops there and the real reply is never read (measured: a pivot
    sampled exactly tokens_updated, context_updated, config_changed,
    status_message(Ready) and missed its own ``session_loaded``).  This helper
    keeps reading past every non-terminal message instead, so the verdict is
    computed from the message that actually answers the request.  ``reply`` is
    that terminal event (``None`` if it never arrived).
    """
    events = []
    reply = None
    deadline = time.monotonic() + timeout
    while len(events) < max_events:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            evt = pump.q.get(timeout=remaining)
        except queue.Empty:
            break
        if evt is None:
            pump.closed = True
            break
        events.append(evt)
        if is_terminal(evt):
            reply = evt
            break
    return events, reply


def _cmd_terminal(pump, payload, is_terminal, capfd, timeout=20.0):
    """``_cmd_captured`` whose collection stops at the terminal reply."""
    with capfd.disabled():
        print("\n[REQUEST]  " + _brief(payload))
    capfd.readouterr()                                  # clear
    pump.send(payload)
    events, reply = _await_terminal(pump, is_terminal, timeout=timeout)
    captured = capfd.readouterr()
    with capfd.disabled():
        _print_events(events)
        _print_server_refusal_lines(captured)
    return events, reply, captured


def test_set_project_cannot_rescope_bound_socket(iso_server, capfd):
    """ATTACK 6 residual (a): set_project must not move a bound socket to B."""
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    ws_a_id, root_a, ws_b_id, root_b = _two_workspaces_full(client, tmp_home, "setproj")
    sid_a = H.create_session(client, workspace_id=ws_a_id)
    sid_b = H.create_session(client, workspace_id=ws_b_id)

    cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
    try:
        sp_payload = {"command": "set_project", "project": root_b}
        sp_events, sp_reply, sp_cap = _cmd_terminal(
            pump, sp_payload, _is_project_reply, capfd,
        )
        pump.drain(timeout=1.0)     # flush this reply's burst before the pivot
        adopted = _adopted_b(sp_events, ws_b_id, root_b)
        with capfd.disabled():
            print(f"[VERDICT] set_project(project=B) -> adopted_b_events={adopted!r}")
        assert not adopted, (
            "RESCOPE: an A-bound socket re-scoped itself to workspace B via "
            f"set_project({root_b!r}): {_slim(adopted)!r}"
        )

        # DISCRIMINATOR: the pivot onto B's session must still be refused.
        pivot_payload = {
            "command": "load_session",
            "session_id": sid_b,
            "workspace_id": ws_b_id,
        }
        events, reply, captured = _cmd_terminal(
            pump, pivot_payload, _is_load_reply, capfd,
        )
        pivot_reply = [reply] if reply is not None else []
        pivoted = [
            e for e in pivot_reply
            if e.get("type") == "session_loaded"
            and e.get("workspace_id") == ws_b_id
            and not e.get("load_error")
        ]
        verdict_events = pivot_reply + ([sp_reply] if sp_reply is not None else [])
        blob = (
            (captured.out or "") + (captured.err or "")
            + (sp_cap.out or "") + (sp_cap.err or "")
        )
        refused = (
            # verdict from the refusal MESSAGES the requests really produced ...
            any("refused" in (e.get("text") or "").lower() for e in verdict_events)
            or any(e.get("load_error") for e in verdict_events)
            # ... with the fd-captured server-side log as redundant belt-and-braces
            or "load_session refused" in blob
            or "set_project refused" in blob
            or "Project switch refused" in blob
        )
        with capfd.disabled():
            print(f"[VERDICT] post-set_project pivot -> pivoted={pivoted!r} refused={refused}")
        assert not pivoted and refused, (
            "BREACH (attack 6a): an A-bound socket re-scoped itself via "
            f"set_project and then pivoted onto workspace B's session {sid_b}: "
            f"pivoted={pivoted!r} refused={refused} pivot_reply={_slim(pivot_reply)!r} "
            f"set_project_events={_slim(sp_events)!r} events={_slim(events)!r}"
        )
    finally:
        _close_ws(cm)


def test_new_session_without_workspace_id_cannot_rescope(iso_server, capfd):
    """ATTACK 6 residual (b): the _project_path fallback must not re-scope.

    Two phases, both asserting the same invariant:

      A. isolated fallback: a socket bootstraps with ``?project=<B root>``
         (scopeless, legitimate), binds itself to A with an explicit
         ``workspace_id``, then sends ``new_session`` with the field OMITTED.
         Pre-fix the fallback resolves ``_project_path`` -> B and re-scopes.
      B. the sequence from the brief: bound socket, ``set_project`` to B's
         path (mover), then ``new_session`` with the field omitted.
    """
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    ws_a_id, root_a, ws_b_id, root_b = _two_workspaces_full(client, tmp_home, "nofallback")
    sid_a = H.create_session(client, workspace_id=ws_a_id)
    sid_b = H.create_session(client, workspace_id=ws_b_id)

    # ── Phase A: isolated _project_path fallback ──────────────────────────
    cm, pump = _open_ws_with_project(client, root_b)
    try:
        bind_payload = {"command": "new_session", "workspace_id": ws_a_id, "mode": "custom"}
        bind_events, _ = _cmd_captured(
            pump, bind_payload, {"session_loaded", "status_message"}, capfd,
        )
        bound_a = [
            e for e in bind_events
            if e.get("type") == "session_loaded"
            and e.get("workspace_id") == ws_a_id
            and not e.get("load_error")
        ]
        with capfd.disabled():
            print(f"[SETUP] ?project=B + new_session(A) -> bound_to_A={len(bound_a)}")
        assert bound_a, (
            "could not set up phase A: explicit new_session(workspace_id=A) on a "
            f"?project=B socket did not bind to A: {bind_events!r}"
        )
        pump.drain(timeout=1.0)

        # ATTACK: omit workspace_id so the fallback re-derives from _project_path
        bare_payload = {"command": "new_session", "mode": "custom"}
        bare_events, bare_cap = _cmd_captured(
            pump, bare_payload, {"session_loaded", "status_message"}, capfd,
        )
        pump.drain(timeout=1.0)     # flush this reply's burst before the next request
        adopted = _adopted_b(bare_events, ws_b_id, root_b)
        with capfd.disabled():
            print(f"[VERDICT] new_session(<omitted>) on ?project=B socket -> adopted_b={adopted!r}")
        assert not adopted, (
            "RESCOPE: an A-bound socket re-scoped itself to workspace B by "
            "omitting workspace_id and letting the _project_path fallback "
            f"re-derive the scope ({root_b!r}): {_slim(adopted)!r}"
        )

        # ── Phase B: same thing after a set_project attempt ───────────────
        sp_payload = {"command": "set_project", "project": root_b}
        sp_events, _ = _cmd_captured(
            pump, sp_payload, {"session_loaded", "status_message"}, capfd,
        )
        pump.drain(timeout=1.0)
        bare2_payload = {"command": "new_session", "mode": "custom"}
        bare2_events, captured2 = _cmd_captured(
            pump, bare2_payload, {"session_loaded", "status_message"}, capfd,
        )
        pump.drain(timeout=1.0)     # flush this reply's burst before the pivot
        adopted2 = _adopted_b(bare2_events, ws_b_id, root_b)
        with capfd.disabled():
            print(f"[VERDICT] new_session(<omitted>) after set_project -> adopted_b={adopted2!r}")
        assert not adopted2, (
            "RESCOPE: an A-bound socket re-scoped itself to workspace B via "
            "set_project(_project_path) + new_session(<omitted>): "
            f"{_slim(adopted2)!r} (set_project_events={_slim(sp_events)!r})"
        )

        # DISCRIMINATOR: still cannot pivot onto B's session.
        pivot_payload = {
            "command": "load_session",
            "session_id": sid_b,
            "workspace_id": ws_b_id,
        }
        events, reply, captured = _cmd_terminal(
            pump, pivot_payload, _is_load_reply, capfd,
        )
        pivot_reply = [reply] if reply is not None else []
        pivoted = [
            e for e in pivot_reply
            if e.get("type") == "session_loaded"
            and e.get("workspace_id") == ws_b_id
            and not e.get("load_error")
        ]
        blob = (
            (captured.out or "") + (captured.err or "")
            + (bare_cap.out or "") + (bare_cap.err or "")
            + (captured2.out or "") + (captured2.err or "")
        )
        refused = (
            # verdict from the refusal MESSAGE the pivot really produced ...
            any("refused" in (e.get("text") or "").lower() for e in pivot_reply)
            or any(e.get("load_error") for e in pivot_reply)
            # ... with the fd-captured server-side log as redundant belt-and-braces
            or "load_session refused" in blob
        )
        with capfd.disabled():
            print(f"[VERDICT] post-fallback pivot -> pivoted={pivoted!r} refused={refused}")
        assert not pivoted and refused, (
            "BREACH (attack 6b): a socket re-scoped via the _project_path "
            f"fallback and then pivoted onto workspace B's session {sid_b}: "
            f"pivoted={pivoted!r} refused={refused} pivot_reply={_slim(pivot_reply)!r} "
            f"events={_slim(events)!r}"
        )
    finally:
        _close_ws(cm)


# ---------------------------------------------------------------------------
# T8 - list_sessions must not enumerate other workspaces' sessions
# ---------------------------------------------------------------------------

def test_list_sessions_does_not_enumerate_other_workspace_sessions(iso_server, capfd):
    app, tmp_home, _server_mod = iso_server
    client = H.make_client(app)

    with capfd.disabled():
        ws_a_id, root_a, ws_b_id, root_b = _two_workspaces_full(
            client, tmp_home, "listsess")
        sid_a = H.create_session(client, workspace_id=ws_a_id)
        sid_b = H.create_session(client, workspace_id=ws_b_id)
        print(f"[SETUP] A={ws_a_id} sid_a={sid_a}  B={ws_b_id} sid_b={sid_b}")
        assert sid_a != sid_b

    cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
    try:
        listed = _list_sessions(pump)
    finally:
        _close_ws(cm)
    a_ids = _session_ids(listed)
    print(f"[CHECK] A-bound socket listed ids={sorted(a_ids)}")

    # CONTROL: the handler must actually work, else "no leak" is vacuous.
    assert sid_a in a_ids, (
        f"CONTROL FAILED: an A-bound socket did not list A's OWN session {sid_a}; "
        f"listed={sorted(a_ids)}"
    )
    assert sid_b not in a_ids, (
        f"ENUMERATION LEAK: an A-bound socket (workspace {ws_a_id}) enumerated "
        f"workspace B's session {sid_b} via list_sessions; listed={sorted(a_ids)}"
    )
    blob = json.dumps(listed, default=str)
    assert ws_b_id not in blob, (
        f"ENUMERATION LEAK: workspace B's id {ws_b_id!r} appears in the sessions_list "
        f"payload sent to an A-bound socket: {blob[:_MAX_TEXT]}"
    )


# ---------------------------------------------------------------------------
# Ruling 3 — a security_response for a request_id no socket was asked must be
# refused (default-deny), never quietly resolved.
# ---------------------------------------------------------------------------

def _seed_pending_prompt(request_id):
    """Register a response queue exactly as security.py does before a prompt."""
    from thoughtmachine.security import _pending_requests_lock, _pending_security_requests

    q = queue.Queue(maxsize=1)
    with _pending_requests_lock:
        _pending_security_requests[request_id] = q
    return q


def _drop_pending_prompt(request_id):
    from thoughtmachine.security import _pending_requests_lock, _pending_security_requests

    with _pending_requests_lock:
        _pending_security_requests.pop(request_id, None)


def _is_security_refusal(events):
    """True if the server answered with a security_response refusal status."""
    return any(
        e.get("type") == "status_message"
        and "security_response" in (e.get("text") or "")
        and "cannot be" in (e.get("text") or "")
        for e in events
    )


def test_unknown_security_response_request_id_refused(iso_server, capfd):
    """A security_response no socket is entitled to answer must be refused.

    Modelled on ``test_close_session_cannot_kill_other_workspace_workers``:
    attack legs first, then a CONTROL proving the refusal is real (an entitled
    socket's answer IS applied) — otherwise "refused" would be vacuous.
    """
    app, tmp_home, server_mod = iso_server
    client = H.make_client(app)

    with capfd.disabled():
        ws_a_id, _root_a, ws_b_id, _root_b = _two_workspaces_full(
            client, tmp_home, "secresp")
        sid_a = H.create_session(client, workspace_id=ws_a_id)
        sid_b = H.create_session(client, workspace_id=ws_b_id)

    cm, pump = _open_bound_ws(client, sid_a, ws_a_id)
    cm_b, pump_b = _open_bound_ws(client, sid_b, ws_b_id)
    rid_unknown = str(uuid.uuid4())
    rid_other = str(uuid.uuid4())
    rid_ctl = str(uuid.uuid4())
    q_unknown = _seed_pending_prompt(rid_unknown)
    q_other = _seed_pending_prompt(rid_other)
    q_ctl = _seed_pending_prompt(rid_ctl)
    try:
        # (1) ATTACK: a request_id that was never issued to any connection.
        events, _cap = _cmd_captured(
            pump,
            {"command": "security_response", "request_id": rid_unknown,
             "approved": True},
            {"status_message"},
            capfd,
        )
        with capfd.disabled():
            print(f"[VERDICT] security_response(unknown rid) -> events={events!r}")
            print(f"[VERDICT] unknown-rid queue still pending={not q_unknown.empty()}")
        assert _is_security_refusal(events), (
            "BREACH: the server accepted a security_response for a request_id it "
            f"never issued to this socket (no refusal status_message); events={events!r}"
        )
        assert q_unknown.empty(), (
            "BREACH: an unissued request_id was resolved — the decision reached "
            "the pending queue, so an attacker can approve an unasked prompt"
        )
        from thoughtmachine.security import _pending_security_requests
        assert rid_unknown in _pending_security_requests, (
            "BREACH: the unissued request_id was popped from the pending registry"
        )

        # (2) SECOND ATTACK SHAPE: the same id sent on a SECOND bound socket
        # (workspace B) is refused just the same — the refusal is not an
        # artefact of one connection.
        events_b, _cap_b = _cmd_captured(
            pump_b,
            {"command": "security_response", "request_id": rid_unknown,
             "approved": True},
            {"status_message"},
            capfd,
        )
        with capfd.disabled():
            print(f"[VERDICT] security_response(unknown rid) on B socket -> "
                  f"events={events_b!r}")
        assert _is_security_refusal(events_b), (
            "BREACH: a B-bound socket resolved a request_id no socket was asked; "
            f"events={events_b!r}"
        )
        assert q_unknown.empty(), (
            "BREACH: the unissued request_id was resolved by a second socket"
        )

        # (3) CONTROL: the handler itself is live — the pre-existing empty-id
        # refusal still fires, so the refusal above is a real branch and not a
        # dropped/ignored command.
        events_ctl, _cap_ctl = _cmd_captured(
            pump,
            {"command": "security_response", "request_id": "", "approved": True},
            {"status_message"},
            capfd,
        )
        with capfd.disabled():
            print(f"[CONTROL] security_response(empty rid) -> events={events_ctl!r}")
        assert any(
            e.get("type") == "status_message"
            and "request_id is required" in (e.get("text") or "")
            for e in events_ctl
        ), (
            "CONTROL FAILED: the security_response handler did not answer at all, "
            f"so the refusal above proves nothing; events={events_ctl!r}"
        )
        assert server_mod._escalation_asker(rid_unknown) is None, (
            "BREACH: an unissued request_id ended up in the asker registry"
        )
    finally:
        _drop_pending_prompt(rid_unknown)
        _drop_pending_prompt(rid_other)
        _drop_pending_prompt(rid_ctl)
        _close_ws(cm_b)
        _close_ws(cm)

