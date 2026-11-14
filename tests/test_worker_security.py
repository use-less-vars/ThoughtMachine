"""C68 — the authenticated session is an AUTHORITY FACT, never an argument.

The three properties pinned here:

(i)   A ``session_id`` supplied by the CALLER (the model hallucinating one, or
      any other caller-influenced value) must be dropped BEFORE the tool's
      arguments are validated, so it can never reach the args model at all.
      The executor's own authenticated id is what the tool receives.
(ii)  With NO authenticated id the call is REFUSED.  It must never proceed
      with ``''`` (which the worker layer degrades to the shared
      ``'<no-session>'`` scope), and it must never fall back to a
      caller-supplied value.
(iii) The model-visible schema does not advertise ``session_id`` at all.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.core.tool_executor import ToolExecutor
from tools.workspace.worker_thread import Worker

#: What the executor authenticates.  Authoritative: this always wins.
AUTHENTICATED = "sess-authenticated-owner"

#: What a hallucinating model might send.  Never authoritative.
CALLER = "sess-hallucinated-by-the-model"


def _executor(tmp_path) -> ToolExecutor:
    config = SimpleNamespace(
        workspace_path=str(tmp_path),
        worktree_root=None,
        tool_output_token_limit=None,
        session_permissions=None,
    )
    return ToolExecutor(
        tool_classes=[Worker], config=config, state=SimpleNamespace()
    )


def _run(executor: ToolExecutor, arguments: dict, session_id: str) -> dict:
    """Drive one Worker call through the real executor injection path."""
    return executor._execute_single_tool(
        Worker,
        dict(arguments),
        "Worker",
        0,
        lambda: False,
        lambda: "",
        lambda: 0,
        session_id=session_id,
    )


@pytest.fixture()
def recorded(monkeypatch):
    """Record what the TOOL actually received, without spawning anything."""
    seen: dict = {}

    def fake_execute(self):
        seen["session_id"] = self.session_id
        return json.dumps({"session_id": self.session_id})

    monkeypatch.setattr(Worker, "execute", fake_execute)
    return seen


def test_caller_supplied_session_id_never_reaches_the_args_model(
    tmp_path, recorded
):
    """(i) A wrongly-typed caller id is IGNORED, not rejected; the real id wins.

    Pre-C68 the caller's value was validated first, so an int here produced
    ``Invalid arguments`` while a well-formed string was silently overwritten
    only afterwards.  Popping it before validation makes the caller's value
    inert either way.
    """
    executor = _executor(tmp_path)
    result = _run(executor, {"action": "list", "session_id": 12345}, AUTHENTICATED)
    assert "Invalid arguments" not in str(result.get("result")), result
    assert recorded["session_id"] == AUTHENTICATED, recorded


def test_hallucinated_session_id_does_not_win(tmp_path, recorded):
    """(i) A well-formed hallucinated id still loses to the authenticated one."""
    executor = _executor(tmp_path)
    _run(executor, {"action": "list", "session_id": CALLER}, AUTHENTICATED)
    assert recorded["session_id"] == AUTHENTICATED, recorded
    assert recorded["session_id"] != CALLER, recorded


def test_empty_authenticated_session_fails_closed(tmp_path, recorded):
    """(ii) No authenticated session -> refuse; never run unscoped."""
    executor = _executor(tmp_path)
    result = _run(executor, {"action": "spawn", "query": "do work"}, "")
    assert result.get("success") is False, result
    assert "session" in str(result.get("result", "")).lower(), result
    assert recorded == {}, "the spawn must never reach Worker.execute"
    assert "'<no-session>'" not in str(result), result


def test_empty_authenticated_session_never_falls_back_to_the_caller(
    tmp_path, recorded
):
    """(ii) The refusal path must not resurrect the caller's value either."""
    executor = _executor(tmp_path)
    result = _run(
        executor, {"action": "spawn", "query": "x", "session_id": CALLER}, ""
    )
    assert result.get("success") is False, result
    assert recorded == {}, recorded


def test_model_visible_schema_does_not_advertise_session_id():
    """(iii) The model is no longer told it may supply a session id."""
    properties = Worker.model_json_schema().get("properties", {})
    assert "session_id" not in properties, sorted(properties)
    # Regression guard: the operational fields must still be advertised.
    assert "action" in properties, sorted(properties)
