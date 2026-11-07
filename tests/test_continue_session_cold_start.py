"""continue_session on a cold start must not silently drop the query.

Bug: when the WebSocket opens, the frontend drains its queued commands
BEFORE it sends ``load_session``.  A ``continue_session`` therefore reaches
the server while ``bridge is None`` (no session loaded yet).  The handler's
CASE 3 branch logs a WARNING and replies with a generic console-only
``status_message`` — the operator's ``query`` is never referenced, so the
text is silently lost (the input box was already cleared on send).

This test drives the *real* ``server.websocket_endpoint`` handler with
exactly one ``continue_session`` frame on a cold start (no prior session)
and asserts the query is preserved in an emitted frame, i.e. the operator
is told what was dropped rather than losing it silently.

RED on the pre-fix tree (CASE 3 text is "No active session — start a new
one." and contains no reference to the query).  GREEN once CASE 3 echoes
the query back to the client.
"""

import json


def _run_continue_session(monkeypatch, tmp_path, query):
    """Drive the real ``server.websocket_endpoint`` handler for one message.

    Feeds exactly one ``continue_session`` frame into the endpoint *before
    any session has been created* (so ``bridge`` stays ``None``), then lets
    the next ``receive_text`` raise ``WebSocketDisconnect`` so the endpoint
    exits cleanly.  Returns the list of payloads emitted via ``ws.send_json``.
    """
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    import web_ui.backend.server as server_mod
    from fastapi import WebSocketDisconnect

    inbox = [json.dumps({"command": "continue_session", "query": query})]
    sent = []

    class FakeWS:
        _closed = False
        client = "fake-client"

        async def accept(self):
            return None

        async def receive_text(self):
            if inbox:
                return inbox.pop(0)
            raise WebSocketDisconnect(1000)

        async def send_json(self, data):
            sent.append(data)

    asyncio.run(server_mod.websocket_endpoint(FakeWS()))
    return sent


def test_continue_session_cold_start_preserves_query(monkeypatch, tmp_path):
    """The operator's query must not be silently dropped on a cold start.

    At least one emitted ``status_message`` must carry the query text so the
    operator learns their input was not actioned and can resend it.
    """
    query = "hello from the cold start"

    sent = _run_continue_session(monkeypatch, tmp_path, query)

    texts = [
        m.get("text", "")
        for m in sent
        if isinstance(m, dict) and m.get("type") == "status_message"
    ]
    assert texts, f"no status_message emitted; got {sent!r}"
    assert any(query in t for t in texts), (
        "the operator's query was silently dropped — no emitted frame "
        f"preserves it.  status_message texts={texts!r}"
    )
