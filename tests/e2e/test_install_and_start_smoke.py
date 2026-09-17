"""End-to-end smoke test: the *real* ``install.sh`` followed by the *real*
``start_thoughtmachine.sh``, driven on a live host.

This is an ``e2e``-marked test: ``tests/e2e/conftest.py`` registers the
``--run-e2e`` flag (``pytest_addoption``) and skips every ``e2e``-marked test
unless that flag is passed, so a default ``pytest`` run (and the CI collection
budget guarded by ``scripts/ci_assert_collected.py``) is unaffected.

What it exercises
-----------------
1. ``bash install.sh`` in an *isolated scratch repo* -> asserts ``rc == 0`` and
   that all five step banners were printed.
2. ``bash start_thoughtmachine.sh`` in the *same* scratch repo, in its **own
   process group**, with ``TM_BACKEND_PORT`` / ``TM_FRONTEND_PORT`` pointing at
   free ephemeral ports -> asserts the launcher actually serves
   ``GET /api/health`` on the requested backend port, then creates a session
   against a local deterministic OpenAI-compatible stub and asserts that at
   least one **non-container** tool call ran (the stub's ``DateTimeTool``), that
   no container tool ran, and that every non-container call succeeded.
3. A clean shutdown: ``SIGTERM`` to the launcher's process group, bounded wait,
   ``SIGKILL`` escalation, and an assertion that the launcher exited through
   its **own** exit path (a real exit code, not a raw signal death), with no
   ``Traceback`` in its output.

Isolation & disclosures (read before touching)
----------------------------------------------
* **Scratch dir, never ``/tmp``.** ``REPO_ROOT/.tmp-test/<uniq>`` is used
  because ``/tmp`` may be mounted ``noexec`` (mirrors
  ``tests/test_cross_platform_install.py``).  Everything is removed in
  ``finally`` via ``shutil.rmtree(..., ignore_errors=True)``.
* **``thoughtmachine/security.py`` L63 is NOT env-configurable.**  That module
  defines a module-level ``VAULT_ROOT`` from ``os.path.expanduser("~")``
  (i.e. derived from ``$HOME``, *not* from ``THOUGHTMACHINE_VAULT_ROOT``).  This
  test therefore redirects BOTH ``HOME`` (to a throwaway dir) *and*
  ``THOUGHTMACHINE_VAULT_ROOT`` (to ``<scratch_home>/.thoughtmachine``), and it
  snapshots the *real* ``~/.thoughtmachine`` before/after to prove it was not
  touched.
* **Install-time network is allowed.**  ``install.sh`` delegates to
  ``scripts/doctor_checks.py`` which may reach the network (``apt-get`` /
  ``pip`` / Docker).  The *runtime* stub below is strictly loopback and makes
  no external calls; the backend is pointed at it through the vault
  ``user/defaults.json`` provider recipe.
* **The scratch repo is a hybrid.**  ``install.sh``, ``start_thoughtmachine.sh``
  and ``scripts/doctor_checks.py`` are byte-for-byte *copies* of the real files
  (per the plan), while the importable packages (``web_ui``, ``thoughtmachine``,
  ``agent``, ``tools``, ... : every other top-level entry) and the existing
  ``.venv`` are *symlinked*.  The project is imported from the repo root (it is
  not pip-installed into ``.venv``), so the copied start script can only import
  ``web_ui.backend.server`` if cwd is (or mirrors) the repo root.  Symlinking
  keeps the script copies real while making the launcher actually boot the app
  and, crucially, keeps ``doctor --ensure-venv`` a no-op against the shared
  ``.venv`` instead of triggering a multi-minute pip install.

UNKNOWN / not asserted by design
--------------------------------
* Whether the provider config consumed by the subprocess factory after the
  workspace/profile layering is *exactly* the ``defaults.json`` recipe written
  here (``base_url`` could be overridden by a profile layer).

Observed wire shape (asserted)
------------------------------
The bridge never forwards a typed ``tool_result`` event to the frontend.
Instead it folds the agent's tool activity into a ``conversation_changed``
event whose ``messages`` is the **full cumulative** history snapshot
(``web_ui/backend/bridge.py`` L2455-2461).  Within that snapshot an assistant
tool call is rendered as ``{"role": "tool_call", "content": json.dumps({"name":
<tool>, "arguments": {...}})}`` (bridge.py L1769-1777) while the matching result
is ``{"role": "tool_result", "tool_call_id": ..., "content": ...}`` with **no**
name/success/error fields (bridge.py L1801-1812).  The extractor below therefore
walks only the last (most complete) snapshot and pairs each ``tool_result`` with
the preceding ``tool_call`` to recover the tool name.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BASH = shutil.which("bash")
_HOST_OK = (os.name == "posix") and (_BASH is not None)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _HOST_OK,
        reason="install/start smoke requires a POSIX host with bash on PATH",
    ),
]

# The five step banners install.sh must print (verbatim substrings).
_INSTALL_BANNERS = (
    "[1/5] Python >= 3.11",
    "[2/5] Docker daemon",
    "[3/5] Docker group",
    "[4/5] Python virtual environment",
    "[5/5] Node.js",
)

_HEALTH_TIMEOUT_S = 60.0
_HEALTH_POLL_S = 1.0
_START_LOG_TIMEOUT_S = 20.0
_TOOL_NAME = "DateTimeTool"
_CONTAINER_MARKERS = ("container", "docker")

# test-only fault-injection hook; unset in normal runs (sentinel "1" only)
_INDUCE_SIGTERM = os.environ.get("TM_SMOKE_INDUCE_SIGTERM_AFTER_S") == "1"


# ---------------------------------------------------------------------------
# scratch helpers
# ---------------------------------------------------------------------------


def _free_port(start: int = 18000, attempts: int = 200) -> int:
    """Return a currently-free TCP port on 127.0.0.1 at/after ``start``."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in [{start}, {start + attempts})")


# Top-level entries that must NOT be symlinked verbatim: the three recipe files
# are copied, and build/test/log cruft is omitted entirely.
_SCRATCH_SKIP = {
    ".git",
    ".tmp-test",
    "tests",
    "logs",
    "node_modules",
    "scripts",
    "install.sh",
    "start_thoughtmachine.sh",
    "requirements.txt",
}


def _mirror_repo_into(scratch_repo: Path) -> None:
    """Populate the scratch repo with copies + symlinks of the real repo.

    The real ``install.sh`` / ``start_thoughtmachine.sh`` / ``doctor_checks.py``
    / ``requirements.txt`` are *copied* (they are the objects under test); every
    other top-level entry (importable packages, ``resources``, ``.venv``, ...)
    is symlinked back into ``REPO_ROOT`` so the copied start script can boot the
    real application from ``cwd == scratch_repo`` without a pip install.
    """
    scratch_repo.mkdir(parents=True, exist_ok=True)
    for entry in sorted(REPO_ROOT.iterdir()):
        if entry.name in _SCRATCH_SKIP:
            continue
        if entry.name == "web_ui":
            # web_ui must be a REAL directory (not a symlink to REPO_ROOT) so the
            # frontend dependencies installed by install.sh land inside the
            # scratch repo instead of mutating the real tree.
            web_ui_dest = scratch_repo / "web_ui"
            web_ui_dest.mkdir(parents=True, exist_ok=True)
            for child in sorted(entry.iterdir()):
                if child.name == "frontend":
                    continue
                (web_ui_dest / child.name).symlink_to(child, target_is_directory=child.is_dir())
            frontend_src = entry / "frontend"
            if frontend_src.is_dir():
                shutil.copytree(
                    frontend_src,
                    web_ui_dest / "frontend",
                    symlinks=True,
                    ignore=shutil.ignore_patterns("node_modules", ".vite", "dist"),
                )
            continue
        (scratch_repo / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())

    (scratch_repo / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "doctor_checks.py", scratch_repo / "scripts" / "doctor_checks.py")
    shutil.copy2(REPO_ROOT / "install.sh", scratch_repo / "install.sh")
    shutil.copy2(REPO_ROOT / "start_thoughtmachine.sh", scratch_repo / "start_thoughtmachine.sh")
    shutil.copy2(REPO_ROOT / "requirements.txt", scratch_repo / "requirements.txt")


def _snapshot_tree(root: Path):
    """A stable (relpath, size, mtime_ns) listing of ``root`` (or None)."""
    if not root.exists():
        return None
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return out


def _read_tail(path: Path, lines: int = 50) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return f"<could not read {path}>"
    return "\n".join(text.splitlines()[-lines:])


def _terminate_group(proc: "subprocess.Popen", timeout: float = _START_LOG_TIMEOUT_S) -> int:
    """SIGTERM the launcher's whole process group, escalate to SIGKILL.

    Returns the launcher's exit code (never None).
    """
    if proc.poll() is not None:
        return proc.returncode
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
    return proc.returncode


# ---------------------------------------------------------------------------
# deterministic OpenAI-compatible loopback stub (stdlib only)
# ---------------------------------------------------------------------------


class _StubLLMHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/chat/completions`` + ``/v1/models`` stub.

    First completion returns a single ``tool_calls`` entry for ``DateTimeTool``;
    every later completion returns a plain final message, so a session that
    starts against this stub performs exactly one non-container tool call and
    then terminates the turn deterministically.
    """

    server_version = "StubLLM/1.0"

    def log_message(self, *args):  # noqa: D401 - silence stderr access logs
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(
                {"object": "list", "data": [{"id": "stub-model", "object": "model", "owned_by": "stub"}]}
            )
            return
        self._send_json({"error": {"message": "not found"}}, status=404)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json({"error": {"message": "not found"}}, status=404)
            return
        # Ignore request body content; the stub is deliberately deterministic.
        _ = raw
        call_index = getattr(self.server, "call_count", 0)
        self.server.call_count = call_index + 1

        # test-only non-vacuity hook; see TM_SMOKE_STUB_FAIL_FIRST; unset in normal runs
        if getattr(self.server, "fail_first", False) and call_index == 0:
            self._send_json({"error": "induced failure for non-vacuity proof"}, status=500)
            return

        if call_index == 0:
            arguments = json.dumps({"operation": "current_datetime"})
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stub_1",
                        "type": "function",
                        "function": {"name": _TOOL_NAME, "arguments": arguments},
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": "The current date and time has been retrieved.",
            }
            finish_reason = "stop"

        self._send_json(
            {
                "id": f"chatcmpl-stub-{call_index + 1}",
                "object": "chat.completion",
                "created": 0,
                "model": "stub-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


class _StubLLM:
    """A ``ThreadingHTTPServer`` stub on an ephemeral loopback port."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubLLMHandler)
        self._server.call_count = 0
        self._server.fail_first = os.environ.get("TM_SMOKE_STUB_FAIL_FIRST") == "1"
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            self._server.server_close()
        except Exception:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# http / ws client helpers
# ---------------------------------------------------------------------------


def _http_get_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - refused while booting
        return False


def _wait_health(url: str, timeout: float = _HEALTH_TIMEOUT_S) -> bool:
    """Bounded poll of ``url`` until it answers 200 or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_get_ok(url):
            return True
        time.sleep(_HEALTH_POLL_S)
    return False


def _http_post_json(url: str, payload: dict, timeout: float = 30.0):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _collect_events(ws, seconds: float) -> list:
    """Read WS messages for up to ``seconds`` and return the parsed dicts.

    A closed WebSocket is **not** silently swallowed: the old blanket
    ``except Exception: break`` hid an aborted turn as "no events".  Only a
    ``TimeoutError`` (the read window elapsed) is retried; a
    ``ConnectionClosed`` is surfaced as an ``AssertionError`` so the caller's
    diagnostics can dump the observed wire shape, and every other exception
    propagates untouched.
    """
    from websockets.exceptions import ConnectionClosed  # noqa: PLC0415 - optional dep, lazy import

    events = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            raw = ws.recv(timeout=1.0)
        except TimeoutError:
            continue
        except ConnectionClosed as exc:
            raise AssertionError(f"WebSocket closed during event collection: {exc}") from exc
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(message, dict):
            events.append(message)
    return events


def _wait_for_session_loaded(ws, seconds: float = 30.0, interval: float = 0.25) -> list:
    """Drain WS frames until the ``session_loaded`` frame arrives (bounded).

    ``new_session`` is asynchronous: the backend replies with a
    ``session_loaded`` frame carrying the **WS-minted** session id only once the
    session is fully built, and the frames before it are neither fixed in count
    nor guaranteed to arrive within a fixed window.  A fixed-time drain (the
    previous ``_collect_events(ws, seconds=5.0)``) therefore either races a
    half-built session or wastes wall-clock.  This helper blocks on the single
    frame that actually signals readiness, **returns every frame it drained**
    (so the caller's ``events`` stays complete) and fails loudly -- with the
    last five frame types -- if the frame never arrives.  The ``session_id`` it
    matches is the *WS* one, not the REST ``/api/session/create`` id.
    """
    from websockets.exceptions import ConnectionClosed  # noqa: PLC0415 - optional dep, lazy import

    events = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            raw = ws.recv(timeout=interval)
        except TimeoutError:
            continue
        except ConnectionClosed as exc:
            raise AssertionError(f"WebSocket closed before session_loaded: {exc}") from exc
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(frame, dict):
            continue
        events.append(frame)
        if frame.get("type") == "session_loaded" and "session_id" in frame:
            return events
    tail = [f"{e.get('type')!r} keys={sorted(e.keys())}" for e in events[-5:]]
    pytest.fail(
        f"session_loaded frame never arrived within {seconds:.0f}s "
        f"(drained {len(events)} frame(s)); last 5: " + "; ".join(tail)
    )


# Raw events from the most recent tool turn, captured by _run_single_tool_turn
# so a failed assertion can dump the observed wire shape.
_LAST_TURN_EVENTS: list = []


def _tool_call_name(msg: dict) -> str:
    """Recover a tool name from a normalised ``tool_call`` message.

    ``bridge._normalize_for_frontend`` renders each assistant ``tool_calls``
    entry as ``{"role": "tool_call", "content": json.dumps({"name": ...,
    "arguments": ...})}`` (bridge.py L1769-1777).  The name lives *only* there —
    the matching ``tool_result`` message is ``dict(raw_tool_message)`` and carries
    just ``role`` / ``tool_call_id`` / ``content`` (bridge.py L1801-1812).
    """
    content = msg.get("content")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            for key in ("name", "tool_name"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    return value
    for key in ("name", "tool_name"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _events_diagnostic(events: list, limit: int = 3, max_chars: int = 2000) -> str:
    """Bounded description of the raw WS event stream, for failure messages."""
    lines = [f"collected {len(events)} event(s)"]
    for index, event in enumerate(events[:limit]):
        if isinstance(event, dict):
            lines.append(f"  [{index}] keys={sorted(event.keys())}")
        else:
            lines.append(f"  [{index}] {type(event).__name__}")
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_result":
            lines.append("first typed tool_result event: " + json.dumps(event, default=str))
            break
        if event.get("type") == "conversation_changed":
            messages = event.get("messages")
            if isinstance(messages, list) and any(
                isinstance(m, dict) and m.get("role") == "tool_result" for m in messages
            ):
                lines.append(
                    "first conversation_changed event carrying a tool_result: "
                    + json.dumps(event, default=str)
                )
                break
    return "\n".join(lines)[:max_chars]


def _extract_tool_results(events: list) -> list:
    """Extract per-tool-call observations from the WebSocket event stream.

    The bridge does **not** forward a typed ``tool_result`` event to the
    frontend; it folds the agent's ``tool_result`` into a ``conversation_changed``
    event whose ``messages`` is the **full cumulative** history snapshot
    (bridge.py:2455-2461).  Two consequences drive this implementation:

    * every snapshot repeats the earlier messages, so iterating all snapshots
      double-counts a single tool call — we therefore walk only the last
      (most complete) snapshot;
    * the ``tool_result`` message carries no name, so each result is paired with
      the **preceding** ``tool_call`` message (see :func:`_tool_call_name`).

    A typed ``tool_result`` branch is kept as a defensive fallback for a future
    bridge that emits the event directly.
    """
    snapshots = []
    typed = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "tool_result":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            typed.append(
                {
                    "name": data.get("tool_name") or event.get("tool_name") or "",
                    "success": bool(data.get("success", event.get("success", False))),
                    "error": data.get("error") or event.get("error"),
                }
            )
        elif event_type == "conversation_changed":
            messages = event.get("messages") or event.get("conversation") or []
            if isinstance(messages, dict):
                messages = list(messages.values())
            if isinstance(messages, list):
                snapshots.append(messages)

    if not snapshots:
        return typed

    # Snapshots are cumulative: keep the results of the last snapshot that
    # yielded any observation (the most complete history seen).
    best = []
    for snapshot in snapshots:
        results = []
        pending_name = ""
        for msg in snapshot:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "tool_call":
                name = _tool_call_name(msg)
                if name:
                    pending_name = name
            elif role == "tool_result":
                results.append(
                    {
                        "name": msg.get("name") or msg.get("tool_name") or pending_name or "",
                        "success": bool(msg.get("success", True)),
                        "error": msg.get("error"),
                    }
                )
        if results:
            best = results
    return best or typed


def _run_single_tool_turn(
    backend_port: int, vault: Path, launcher_proc: "subprocess.Popen | None" = None
) -> list:
    """Create a session, send one query, and return the observed tool results."""
    import websockets.sync.client as ws_client  # noqa: PLC0415 - optional dep, lazy import

    base_url = f"http://127.0.0.1:{backend_port}"
    status, body = _http_post_json(f"{base_url}/api/session/create", {"mode": "custom"})
    assert status in (200, 201), f"session creation failed: {status} {body}"
    session_id = body.get("session_id")
    assert session_id, f"no session_id in create response: {body}"

    events = []
    with ws_client.connect(f"ws://127.0.0.1:{backend_port}/ws", open_timeout=15, close_timeout=5) as ws:
        ws.send(json.dumps({"command": "new_session"}))
        events += _wait_for_session_loaded(ws)
        # test-only fault-injection hook; see _INDUCE_SIGTERM; unset in normal runs
        if _INDUCE_SIGTERM and launcher_proc is not None:
            print(
                f"TM_SMOKE_INDUCE_SIGTERM_AFTER_S=1: SIGTERM to launcher pgid "
                f"(pid={launcher_proc.pid}) before continue_session send",
                flush=True,
            )
            try:
                _launcher_pgid = os.getpgid(launcher_proc.pid)
            except ProcessLookupError:
                _launcher_pgid = None
            if _launcher_pgid is not None:
                try:
                    os.killpg(_launcher_pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # Drain whatever the dying launcher flushes (the close frame in
            # particular) before the send below, so the send races the real
            # close and reproduces the CI condition. The drained frames are
            # discarded; a missed close falls through to the send.
            _collect_events(ws, seconds=5.0)
        ws.send(json.dumps({"command": "continue_session", "query": "What is the current date and time?"}))
        # Collect until the stub's final message arrives (or a bounded timeout).
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            batch = _collect_events(ws, seconds=2.0)
            events += batch
            if _extract_tool_results(events) and any(
                e.get("type") in ("final", "agent_responded") for e in batch
            ):
                break
            if any(e.get("type") == "status_message" and e.get("text", "").startswith("\u274c") for e in batch):
                break
    _LAST_TURN_EVENTS[:] = list(events)
    return _extract_tool_results(events)


# ---------------------------------------------------------------------------
# the smoke test
# ---------------------------------------------------------------------------


def test_install_and_start_smoke(monkeypatch):
    """Real installer + real launcher, end to end, on free ports."""
    real_home = Path(os.path.expanduser("~"))
    real_vault = real_home / ".thoughtmachine"
    before = _snapshot_tree(real_vault)

    scratch = REPO_ROOT / ".tmp-test" / f"install_start_{uuid.uuid4().hex[:12]}"
    scratch_repo = scratch / "repo"
    scratch_home = scratch / "home"
    vault = scratch_home / ".thoughtmachine"
    start_log = scratch / "start_thoughtmachine.log"
    backend_log = scratch_repo / "logs" / "backend_startup.log"

    backend_port = _free_port(18000)
    frontend_port = _free_port(backend_port + 1)

    stub = None
    proc = None
    log_handle = None
    body_error = None
    completed_ok = False

    scratch_home.mkdir(parents=True, exist_ok=True)
    (vault / "user").mkdir(parents=True, exist_ok=True)

    base_env = {**os.environ, "HOME": str(scratch_home), "THOUGHTMACHINE_VAULT_ROOT": str(vault), "CI": ""}
    base_env.pop("OPENAI_API_KEY", None)
    base_env.pop("DEEPSEEK_API_KEY", None)
    base_env.pop("OPENAI_COMPATIBLE_API_KEY", None)

    try:
        # ---- 1. real install.sh in an isolated scratch repo -----------------
        _mirror_repo_into(scratch_repo)
        install = subprocess.run(
            [_BASH, str(scratch_repo / "install.sh")],
            cwd=str(scratch_repo),
            env=base_env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        combined = install.stdout + install.stderr
        assert install.returncode == 0, (
            "install.sh exited non-zero "
            f"({install.returncode})\n--- stdout ---\n{install.stdout}\n--- stderr ---\n{install.stderr}"
        )
        for banner in _INSTALL_BANNERS:
            assert banner in combined, f"install.sh banner missing: {banner!r}\n--- output ---\n{combined}"

        # ---- 1b. frontend deps: install.sh must be load-bearing ------------
        frontend_dir = scratch_repo / "web_ui" / "frontend"
        vite_bin = frontend_dir / "node_modules" / ".bin" / "vite"
        assert vite_bin.exists(), (
            "install.sh is expected to be load-bearing for frontend deps, but "
            f"{vite_bin} does not exist after install.sh ran"
        )
        # Hazard regression guard: the frontend tree must be a REAL directory
        # inside the scratch repo, never a symlink back into the real repo
        # (otherwise install.sh would mutate REPO_ROOT/web_ui/frontend).
        assert not frontend_dir.is_symlink(), (
            f"{frontend_dir} must be a real directory, not a symlink"
        )
        resolved_node_modules = (frontend_dir / "node_modules").resolve()
        assert scratch_repo.resolve() in resolved_node_modules.parents, (
            f"{resolved_node_modules} must resolve inside {scratch_repo}, "
            "not back into the real repository"
        )

        # ---- 2. provider recipe + local deterministic stub ------------------
        stub = _StubLLM()
        recipe = {
            "provider_id": "openai_compatible",
            "model": "stub-model",
            "base_url": stub.base_url,
            "temperature": 0.0,
            "max_turns": 3,
            "system_prompt": "You are a ThoughtMachine smoke-test agent.",
        }
        (vault / "user" / "defaults.json").write_text(json.dumps(recipe), encoding="utf-8")

        # ---- 3. real start_thoughtmachine.sh on the requested ports ---------
        start_env = {
            **base_env,
            "OPENAI_API_KEY": "dummy",
            "TM_BACKEND_PORT": str(backend_port),
            "TM_FRONTEND_PORT": str(frontend_port),
        }
        log_handle = open(start_log, "wb", buffering=0)
        proc = subprocess.Popen(
            [_BASH, str(scratch_repo / "start_thoughtmachine.sh")],
            cwd=str(scratch_repo),
            env=start_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        health_url = f"http://127.0.0.1:{backend_port}/api/health"
        if not _wait_health(health_url, timeout=_HEALTH_TIMEOUT_S):
            tail = _read_tail(backend_log, 50)
            launcher_tail = _read_tail(start_log, 50)
            pytest.fail(
                f"backend never answered {health_url} within {_HEALTH_TIMEOUT_S:.0f}s "
                f"(launcher pid={proc.pid}, requested backend port={backend_port}).\n"
                f"--- backend_startup.log (last 50 lines) ---\n{tail}\n"
                f"--- start_thoughtmachine.sh output (last 50 lines) ---\n{launcher_tail}"
            )

        # ---- 4. non-container tool call(s) ran and succeeded ----------------
        # Everything from here on runs *after* the health check, so these two
        # tails are the only backend/launcher evidence a failure in this region
        # would otherwise lack. The wrap re-raises, so nothing is swallowed.
        try:
            tool_results = _run_single_tool_turn(backend_port, vault, proc)
            diagnostic = _events_diagnostic(_LAST_TURN_EVENTS)
            container = [r for r in tool_results if any(m in r["name"].lower() for m in _CONTAINER_MARKERS)]
            non_container = [r for r in tool_results if r not in container]
            assert non_container, (
                f"expected at least one non-container tool result, got: {tool_results}\n{diagnostic}"
            )
            assert not container, f"expected no container tool results, got: {container}\n{diagnostic}"
            assert any(r["success"] is True for r in non_container), (
                f"expected a successful non-container tool call, got: {non_container}\n{diagnostic}"
            )
            failed = [r for r in non_container if r["success"] is False and r["error"] is not None]
            assert not failed, f"non-container tool call(s) failed with an error: {failed}\n{diagnostic}"
            names = [r["name"] for r in non_container]
            assert _TOOL_NAME in names, (
                f"expected {_TOOL_NAME!r} among the executed tools, got: {names}\n{diagnostic}"
            )
        except BaseException:
            # Post-health failure (barrier timeout, WS-closed AssertionError, or
            # an assertion mismatch): dump both tails to stdout, then re-raise
            # the original exception unchanged.
            print("--- BEGIN post-health diagnostics ---", flush=True)
            print("--- backend_startup.log (last 100 lines) ---", flush=True)
            print(_read_tail(backend_log, 100), flush=True)
            print("--- start_thoughtmachine.sh output (last 100 lines) ---", flush=True)
            print(_read_tail(start_log, 100), flush=True)
            print("--- END post-health diagnostics ---", flush=True)
            raise

        # Reached only after every post-health assertion passed; on any failure
        # above the flag stays False so the scratch is preserved in `finally`.
        completed_ok = True

    except BaseException as exc:  # noqa: BLE001 - re-raised after cleanup
        body_error = exc
        raise
    finally:
        if proc is not None:
            rc = _terminate_group(proc)
            # The launcher must exit through its OWN path (documented INT/TERM
            # trap -> exit 130, or `wait "$BACKEND_PID"; exit "$BACKEND_RC"`),
            # never as a raw SIGTERM death (Python reports that as -15).
            if rc is not None:
                assert rc != -signal.SIGTERM, (
                    "start_thoughtmachine.sh died from the raw SIGTERM instead of running "
                    f"its own exit path (returncode={rc})"
                )
            output = _read_tail(start_log, 200)
            assert "Traceback" not in output, f"launcher emitted a Traceback:\n{output}"
        if log_handle is not None:
            log_handle.close()
        if stub is not None:
            stub.stop()
        if completed_ok:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            # On failure the scratch holds the only backend/launcher evidence, so
            # it is deliberately left in place for CI artifact collection; on
            # success it is removed exactly as before.
            print(f"preserving scratch for diagnostics: {scratch}", flush=True)
            print(f"  launcher log: {start_log}", flush=True)
            print(f"  backend log:  {backend_log}", flush=True)

        after = _snapshot_tree(real_vault)
        if after != before:
            message = f"the real vault {real_vault} was modified during the run"
            if body_error is None:
                raise AssertionError(message)
            print(f"WARNING: {message}", file=sys.stderr)

    # Silence "unused" for monkeypatch if future hardening needs env patching.
    _ = monkeypatch
