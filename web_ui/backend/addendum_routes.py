"""
addendum_routes.py — REST API for the operator session addendum (W1.5).

Provides:
- GET    /api/session/{session_id}/addendum  — entries, budget and max entries
- POST   /api/session/{session_id}/addendum  — append exactly one entry
- DELETE /api/session/{session_id}/addendum  — clear every entry

The addendum is a first-class session field (``session/models.py``) that reaches
the model by being composed into the single system-prompt string
(``agent/core/llm_client.py``).  This module is transport only: it is a thin
REST face over ``session/addendum.py``.

* ``write_addendum_entry`` stays the ONE write boundary.  This route never
  re-implements the budget check, never truncates, never evicts an existing
  entry and never validates entry shape itself: a refusal arrives as a value
  (``{'status': 'dropped', 'reason': ...}``) and is rendered here as a 409.
* The entry ``source`` is forced to ``'operator'`` server-side.  A client-sent
  ``source`` is ignored (the request model does not carry the field), because
  ruling D-4 gives the mailbox its own writer (T-2b) and a browser cannot name
  itself as one.
* Nothing here touches a running ``Agent``'s ``addendum_queue``: notifying a
  live turn is the write boundary's job, and a request thread must not perturb
  it.

Persistence uses the house call the other session routes use,
``FileSystemSessionStore.save_session``.  That call stamps
``session.updated_at = datetime.now()`` before serialising
(``session/store.py``), so an accepted write or a clear does bump the session's
``updated_at``; a refused write persists nothing and therefore does not.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from session.addendum import (
    DROP_BUDGET_EXCEEDED,
    DROP_EMPTY_TEXT,
    DROP_MAX_ENTRIES_EXCEEDED,
    write_addendum_entry,
)
from session.models import normalize_addendum

from web_ui.backend import session_routes
from web_ui.backend.origin_policy import _origin_check_request

# ── Router ───────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/session")

#: The only source this REST surface may write.  The mailbox writer (T-2b) is a
#: different caller of the same write boundary; a client cannot name itself.
OPERATOR_SOURCE = "operator"


# ── Pydantic models ──────────────────────────────────────────────────────────


class AddendumWriteBody(BaseModel):
    """Body of ``POST /api/session/{session_id}/addendum``: the entry text.

    Only ``text`` is part of the contract.  Any other key a client sends --
    ``source`` in particular -- is not a model field and is dropped by pydantic,
    so it can never reach the write boundary.
    """

    text: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_store():
    """The shared session store the session routes read.

    Delegated rather than re-bound on purpose: the addendum surface must read
    and write exactly the store ``session_routes`` reads, so replacing that
    module's ``_get_store`` (as the route tests do) reaches this router too.
    """
    return session_routes._get_store()


def _load_addendum_session(session_id: str):
    """Load a session for the addendum endpoints; 404 when unknown.

    Mirrors ``session_routes._load_permission_session`` / the GET
    ``/{session_id}`` detail handler, including the detail wording.
    """
    store = _get_store()
    session = store.load_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )
    return store, session


def _addendum_state(session) -> Dict[str, Any]:
    """The session's addendum as a REST payload.

    ``entries`` is the canonical stored set (normalised, order preserved, copied
    so a client cannot mutate the session's own list).  ``total_chars`` is the
    exact character count of that set, reported for display only -- the budget
    verdict is made at the write boundary, never here.
    """
    addendum = normalize_addendum(getattr(session, "addendum", None))
    entries = [dict(item) for item in addendum["entries"]]
    return {
        "entries": entries,
        "budget_chars": addendum["budget_chars"],
        "max_entries": addendum["max_entries"],
        "total_chars": sum(len(item["text"]) for item in entries),
    }


def _refusal_message(reason: str, session, text: str) -> str:
    """Human sentence for a refused write, naming the numbers when it can.

    The write boundary reports a refusal as a CODE and logs the sentence; it
    does not return the sentence (``session/addendum.py``).  The numbers are
    therefore recomputed here from the UNCHANGED set plus the refused text --
    the same formula the boundary measures, an exact character count of the
    resulting set (existing entry texts + the candidate's text) and the
    resulting entry count.  The wording matches the boundary's own notice.
    """
    addendum = normalize_addendum(getattr(session, "addendum", None))
    entries = addendum["entries"]
    if reason == DROP_BUDGET_EXCEEDED:
        attempted = sum(len(item["text"]) for item in entries) + len(text)
        return (
            f"addendum budget exceeded: {attempted} > "
            f"{addendum['budget_chars']} chars"
        )
    if reason == DROP_MAX_ENTRIES_EXCEEDED:
        return (
            f"addendum entry limit exceeded: {len(entries) + 1} > "
            f"{addendum['max_entries']} entries"
        )
    if reason == DROP_EMPTY_TEXT:
        return "addendum text is empty"
    return f"addendum write refused: {reason}"


def _refusal_body(reason: str, message: str, session) -> Dict[str, Any]:
    """The 409 payload: why the write was refused, plus the unchanged set."""
    return {
        "status": "refused",
        "reason": reason,
        "message": message,
        **_addendum_state(session),
    }


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/{session_id}/addendum")
async def get_session_addendum(request: Request, session_id: str) -> Dict[str, Any]:
    """Return the session's addendum:
    ``{entries, budget_chars, max_entries, total_chars, session_id}``.

    Read-only; the budget shown is the stored one, and ``total_chars`` is the
    exact character count of the current entries (display only).
    """
    if not _origin_check_request(request, route_name="get_session_addendum"):
        raise HTTPException(status_code=403, detail="origin not permitted")
    try:
        _store, session = _load_addendum_session(session_id)
        return {**_addendum_state(session), "session_id": session_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session addendum: {exc}",
        )


@router.post("/{session_id}/addendum")
async def post_session_addendum(
    request: Request,
    session_id: str,
    body: AddendumWriteBody,
) -> Dict[str, Any]:
    """Append exactly one operator entry to the session's addendum.

    Order: (1) origin check; (2) session load (404 when unknown); (3) the
    append through ``session.addendum.write_addendum_entry`` with
    ``source='operator'`` forced here -- the ONE write boundary, which also
    makes the budget verdict (refuse, never truncate, never evict);
    (4) persist via ``store.save_session``.

    ``{'status': 'ok'}`` -> 200 ``{status: 'accepted', entry, entries,
    total_chars, budget_chars, max_entries}``.
    ``{'status': 'dropped'}`` -> 409 ``{status: 'refused', reason, message,
    entries, total_chars, budget_chars, max_entries}`` where ``entries`` is the
    UNCHANGED set read back off the session (the boundary reports an empty list
    on a drop) and nothing is persisted.
    """
    if not _origin_check_request(request, route_name="post_session_addendum"):
        raise HTTPException(status_code=403, detail="origin not permitted")
    try:
        store, session = _load_addendum_session(session_id)
        result = write_addendum_entry(session, body.text, source=OPERATOR_SOURCE)
        if result["status"] != "ok":
            reason = result["reason"] or "unknown"
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=_refusal_body(
                    reason, _refusal_message(reason, session, body.text), session
                ),
            )
        store.save_session(session)
        return {
            "status": "accepted",
            "entry": dict(result["entry"]),
            **_addendum_state(session),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write session addendum: {exc}",
        )


@router.delete("/{session_id}/addendum")
async def delete_session_addendum(
    request: Request, session_id: str
) -> Dict[str, Any]:
    """Clear every entry from the session's addendum.

    ``entries`` becomes ``[]`` while ``budget_chars``, ``max_entries`` and any
    key this module does not know about are preserved (normalised through
    ``normalize_addendum``, the same idiom the write boundary uses), then the
    session is persisted.  Returns ``{status: 'cleared', entries: [],
    total_chars: 0, budget_chars, max_entries, session_id}``.
    """
    if not _origin_check_request(request, route_name="delete_session_addendum"):
        raise HTTPException(status_code=403, detail="origin not permitted")
    try:
        store, session = _load_addendum_session(session_id)
        current = getattr(session, "addendum", None)
        normalized = normalize_addendum(current)
        persisted = dict(current) if isinstance(current, dict) else {}
        persisted["entries"] = []
        persisted["budget_chars"] = normalized["budget_chars"]
        persisted["max_entries"] = normalized["max_entries"]
        session.addendum = persisted
        store.save_session(session)
        return {
            "status": "cleared",
            **_addendum_state(session),
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear session addendum: {exc}",
        )
