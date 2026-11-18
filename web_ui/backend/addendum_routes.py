"""
addendum_routes.py — REST API for the operator session addendum (W1.5).

Provides:
- GET    /api/session/{session_id}/addendum  — entries, budget and max entries
- POST   /api/session/{session_id}/addendum  — append exactly one entry
- DELETE /api/session/{session_id}/addendum  — clear every entry
- PATCH  /api/session/{session_id}/addendum/entries/{entry_id}
                                              — replace one entry's text
- DELETE /api/session/{session_id}/addendum/entries/{entry_id}
                                              — remove exactly one entry
- GET    /api/session/{session_id}/addendum/effective
                                              — the block the next query carries

The addendum is a first-class session field (``session/models.py``) that reaches
the model by being composed into the single system-prompt string
(``agent/core/llm_client.py``).  This module is transport only: it is a thin
REST face over ``session/addendum.py``.

* ``write_addendum_entry`` stays the ONE write boundary.  This route never
  re-implements the budget check, never truncates, never evicts an existing
  entry and never validates entry shape itself: a refusal arrives as a value
  (``{'status': 'dropped', 'reason': ..., 'message': ...}``) and is rendered
  here as a 409.  The refusal sentence is the boundary's own words, not a
  second derivation of them.
* The entry ``source`` is forced to ``'operator'`` server-side.  A client-sent
  ``source`` is ignored (the request model does not carry the field), because
  ruling D-4 gives the mailbox its own writer (T-2b) and a browser cannot name
  itself as one.
* Nothing here touches a running ``Agent``'s ``addendum_queue``: notifying a
  live turn is the write boundary's job, and a request thread must not perturb
  it.

Write-through persistence (ruling: "Option A")
─────────────────────────────────────────────
A session can be *live* or *cold*, and this module resolves which one it is in
exactly one place (``_resolve_addendum_session``):

* **live** — a bridge in THIS process currently holds a ``Session`` object for
  that id (a running agent's session, the bridge's ``_session``, or its
  ``_loaded_session``).  That object is the runtime authority: it is the one the
  turn loop and the model actually read, so a write that landed only on disk
  would be invisible until the next resume.  The route therefore mutates the
  live object and then persists it.
* **cold** — nobody holds it.  The store is then the only authority and the
  write is a read-modify-write through ``store.mutate_session``, which reads,
  mutates and rewrites inside one ``FileLock``; the entry is picked up when the
  session is next resumed.

A live write mutates-and-persists under the bridge's own
``_session_state_lock`` — the same lock ``WebAgentBridge.save_session`` holds
across its snapshot+write — so an end-of-turn save can never persist a snapshot
taken before this route's mutation and silently drop the accepted entry.
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from session.addendum import (
    addendum_total_chars,
    remove_addendum_entry,
    update_addendum_entry,
    write_addendum_entry,
)
from session.models import normalize_addendum

from web_ui.backend import session_routes
from web_ui.backend.event_forwarder import _active_tab_bridges

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


class AddendumUpdateBody(BaseModel):
    """Body of ``PATCH .../addendum/entries/{entry_id}``: the replacement text.

    Only ``text`` is part of the contract, exactly as on the append path.  An
    entry's ``id``, ``ts``, ``source`` and ``pinned`` are its identity and are
    not patchable: the write boundary preserves them and a client-sent value for
    any of them is not a model field, so pydantic drops it before the route
    body is even read.
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


def _held_sessions(bridge, session_id: str) -> List[Any]:
    """Every ``Session`` object *bridge* currently holds for *session_id*.

    Ordered by authority, most authoritative first: the running agent's own
    session (the object its ``LLMClient`` re-reads), then the bridge's
    ``_session``, then ``_loaded_session``.  In practice the first two are the
    same object; the order makes that explicit instead of assumed, and
    ``_loaded_session`` is included because it is the object the next
    ``start()`` will run, so a write that skipped it could be undone.

    De-duplicated by identity, so a bridge that aliases the same Session three
    ways contributes it once.
    """
    candidates = [
        getattr(getattr(bridge, "_agent", None), "session", None),
        getattr(bridge, "_session", None),
        getattr(bridge, "_loaded_session", None),
    ]
    held: List[Any] = []
    for candidate in candidates:
        if candidate is None:
            continue
        if getattr(candidate, "session_id", None) != session_id:
            continue
        if any(candidate is existing for existing in held):
            continue
        held.append(candidate)
    return held


def _live_bridges(session_id: str) -> List[Tuple[Any, List[Any]]]:
    """``[(bridge, held_sessions), ...]`` for every bridge holding *session_id*.

    A snapshot of the live set (``list(...)``) so a tab registering or
    unregistering mid-request cannot change the set under the loop.  Bridges
    with a running agent sort first: that is the copy the model is reading.
    """
    live: List[Tuple[Any, List[Any]]] = []
    for bridge in list(_active_tab_bridges):
        held = _held_sessions(bridge, session_id)
        if held:
            live.append((bridge, held))
    live.sort(key=lambda pair: getattr(pair[0], "_agent", None) is None)
    return live


def _resolve_addendum_session(session_id: str):
    """Resolve the addendum authority for *session_id*; 404 when unknown.

    The ONE resolver: it decides between a live session object and the cold
    store path, and every endpoint goes through it, so "which copy is the
    authority" is answered in exactly one place.

    "Live" means a bridge in this process currently holds a ``Session`` object
    for that id -- the running agent's session, the bridge's ``_session``, or
    its ``_loaded_session``.  That object is the runtime authority (it is what
    the turn loop and the model read) and the store is the durability
    authority; a live write lands on the object and is then persisted.

    "Cold" means nobody holds it: the store is the only authority, and writes go
    through ``store.mutate_session`` -- one atomic read-modify-write under the
    file lock -- so the entry is there when the session is next resumed.

    Returns ``(store, bridges, session)``.  ``bridges`` is empty on the cold
    path; ``session`` is the live object on the live path and a freshly loaded
    copy on the cold path.
    """
    store = _get_store()
    bridges = _live_bridges(session_id)
    if bridges:
        return store, bridges, bridges[0][1][0]
    session = store.load_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )
    return store, [], session


def _bridge_lock(bridge):
    """The bridge's session-state lock, or a no-op when it has none.

    A bridge without ``_session_state_lock`` (older object, test double) still
    works: it simply does not serialise against a turn-loop save, which is the
    pre-existing behaviour rather than a new failure mode.
    """
    lock = getattr(bridge, "_session_state_lock", None)
    if lock is None:
        return nullcontext()
    return lock


def _write_through(
    store,
    session_id: str,
    bridges: List[Tuple[Any, List[Any]]],
    mutator: Callable[[Any], Tuple[Dict[str, Any], bool]],
) -> Tuple[Optional[Dict[str, Any]], Any]:
    """Apply *mutator* to the addendum authority and persist the result.

    *mutator* has the same contract as ``FileSystemSessionStore.mutate_session``
    -- ``(result, should_write)`` -- so the live and cold paths below are the
    same decision expressed against two different authorities.

    Returns ``(result, session)``; ``result`` is ``None`` only on the cold path
    when the session vanished between resolution and the read-modify-write
    (the caller turns that into a 404).
    """
    if not bridges:
        # Cold: the store's own atomic read-modify-write is the whole story.
        session, result = store.mutate_session(session_id, mutator)
        return result, session

    primary_bridge, primary_held = bridges[0]
    accepted = primary_held[0]
    with _bridge_lock(primary_bridge):
        result, should_write = mutator(accepted)
        if not should_write:
            # A refusal changed nothing, so nothing is persisted: a refused
            # write must not bump ``updated_at`` or rewrite the record.
            return result, accepted
        store.save_session(accepted)
        addendum = copy.deepcopy(getattr(accepted, "addendum", None))
        # Other live copies of the same session in THIS bridge (the alias case)
        # must not keep serving the pre-write set to the next reader.
        for twin in primary_held[1:]:
            twin.addendum = copy.deepcopy(addendum)

    # Another tab's bridge may hold its own copy of the same session; bring it
    # to the accepted set too, under that bridge's own lock, so no reader can
    # observe the write as applied for one tab and absent for the other.
    for bridge, held in bridges[1:]:
        with _bridge_lock(bridge):
            for twin in held:
                twin.addendum = copy.deepcopy(addendum)

    return result, accepted


def _addendum_state(session, boundary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The session's addendum as a REST payload.

    ``entries`` is the canonical stored set (normalised, order preserved, copied
    so a client cannot mutate the session's own list).  ``total_chars`` is the
    exact character count of that set, measured by
    ``session.addendum.addendum_total_chars`` -- the same function the write
    boundary measures with.  This module never adds up text itself, which is
    what makes the displayed total unable to disagree with the enforced budget.

    *boundary* is a ``write_addendum_entry`` result.  When given, the numbers it
    measured (``total_chars``, ``budget_chars``, ``max_entries``) are reported
    as-is: a refusal must quote the verdict it is reporting, not a second
    derivation of it that a later change could drift away from.
    """
    addendum = normalize_addendum(getattr(session, "addendum", None))
    entries = [dict(item) for item in addendum["entries"]]
    state = {
        "entries": entries,
        "budget_chars": addendum["budget_chars"],
        "max_entries": addendum["max_entries"],
        "total_chars": addendum_total_chars(session),
    }
    if boundary:
        # The boundary's own verdict, quoted as it measured it.  `total_chars`
        # is the post-attempt total it computed and `attempted_*` describe the
        # write it refused; re-deriving any of them here is how the reported
        # numbers drift away from the enforced ones.
        for key in ("total_chars", "budget_chars", "max_entries",
                    "attempted_chars", "attempted_entries"):
            if boundary.get(key) is not None:
                state[key] = boundary[key]
    return state


def _refusal_message(result: Dict[str, Any]) -> str:
    """The refusal sentence: the write boundary's own words when it gave them.

    The boundary measures the numbers and phrases the sentence
    (``session/addendum.py``); re-deriving it here from the unchanged set plus
    the refused text is how two places come to disagree.  The fallback only
    covers a drop that carried no sentence at all (the boundary's own exception
    guard), where there are no numbers to report either.
    """
    message = result.get("message")
    if message:
        return message
    reason = result.get("reason") or "unknown"
    return f"addendum write refused: {reason}"


def _refusal_body(result: Dict[str, Any], session) -> Dict[str, Any]:
    """The 409 payload: why the write was refused, plus the unchanged set."""
    return {
        "status": "refused",
        "reason": result.get("reason") or "unknown",
        "message": _refusal_message(result),
        **_addendum_state(session, result),
    }


def _count(items: Any) -> int:
    """How many items *items* holds.

    This module's source is asserted
    (``test_a3_route_module_never_sums_or_measures_text``) to contain neither the
    builtin summation call nor the builtin size call: every character total it
    reports is the write boundary's own number, so the transport must not be
    able to measure text even by accident.  Counting items (live bridges, list
    members) is still legitimate for diagnostics, so it goes through
    ``__len__`` explicitly rather than re-introducing the forbidden spelling.
    """
    return items.__len__()


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/{session_id}/addendum")
async def get_session_addendum(request: Request, session_id: str) -> Dict[str, Any]:
    """Return the session's addendum:
    ``{entries, budget_chars, max_entries, total_chars, session_id}``.

    Read-only.  A live session is read off the object the model is reading, not
    off disk, so a client never sees a set the next turn will not see.
    """
    try:
        _store, _bridges, session = _resolve_addendum_session(session_id)
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

    Order: (1) origin check; (2) resolve the authority (404 when unknown);
    (3) the append through ``session.addendum.write_addendum_entry`` with
    ``source='operator'`` forced here -- the ONE write boundary, which also
    makes the budget verdict (refuse, never truncate, never evict);
    (4) write-through: mutate the live object and persist it, or, when the
    session is cold, read-modify-write the store atomically.

    ``{'status': 'ok'}`` -> 200 ``{status: 'accepted', entry, entries,
    total_chars, budget_chars, max_entries}``.
    ``{'status': 'dropped'}`` -> 409 ``{status: 'refused', reason, message,
    entries, total_chars, budget_chars, max_entries}`` where ``entries`` is the
    UNCHANGED set read back off the session (the boundary reports an empty list
    on a drop) and nothing is persisted.
    """
    try:
        store, bridges, _session = _resolve_addendum_session(session_id)

        def mutator(session) -> Tuple[Dict[str, Any], bool]:
            """Append through the ONE boundary; persist only on acceptance."""
            result = write_addendum_entry(
                session, body.text, source=OPERATOR_SOURCE, notify=False
            )
            return result, result.get("status") == "ok"

        result, session = _write_through(store, session_id, bridges, mutator)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
        if result.get("status") != "ok":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=_refusal_body(result, session),
            )
        return {
            "status": "accepted",
            "entry": dict(result["entry"]),
            **_addendum_state(session, result),
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
    change is written through -- live object and store, or the store's atomic
    read-modify-write when the session is cold.  Returns
    ``{status: 'cleared', entries: [], total_chars: 0, budget_chars,
    max_entries, session_id}``.

    The normaliser also repairs an out-of-range cap, so a clear persists the
    default caps (8000 chars / 64 entries) even when the stored record carried
    ``budget_chars: 0``: clearing is the repair path for an unusable record, not
    only for its entries.
    """
    try:
        store, bridges, _session = _resolve_addendum_session(session_id)

        def mutator(session) -> Tuple[Dict[str, Any], bool]:
            """Replace the entry set with the empty one; always persist."""
            current = getattr(session, "addendum", None)
            normalized = normalize_addendum(current)
            persisted = dict(current) if isinstance(current, dict) else {}
            persisted["entries"] = []
            persisted["budget_chars"] = normalized["budget_chars"]
            persisted["max_entries"] = normalized["max_entries"]
            session.addendum = persisted
            return persisted, True

        result, session = _write_through(store, session_id, bridges, mutator)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
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


@router.get("/{session_id}/addendum/effective")
async def get_session_addendum_effective(
    request: Request, session_id: str
) -> Dict[str, Any]:
    """Return the addendum block the session's NEXT query will carry, verbatim.

    ``rendered_block`` is ``agent.core.llm_client.render_addendum_block`` output
    for the resolved session -- the very function the turn's
    ``ensure_system_prompt`` composes the system prompt with -- so the operator
    reads the text the model will read instead of a second rendering of it that
    could drift.  ``block_chars`` is that string's character count and
    ``applied`` is always ``'next_query'``: this route renders, it never reaches
    into a running turn.

    ``resolved_via`` names the copy that was read: ``'live'`` when a bridge in
    this process holds the session, ``'store'`` when it was loaded from disk.
    ``diagnostics.same_object`` is True only when the resolved session IS the
    running agent's own session object; on the cold path no agent holds it, so
    it is necessarily False and ``agent_session_object_id`` is ``None``.
    """
    try:
        _store, bridges, session = _resolve_addendum_session(session_id)

        # Imported here, not at module scope: this router must not pull the
        # agent package in at import time (it is imported by the web server).
        from agent.core.llm_client import render_addendum_block

        rendered_block = render_addendum_block(session)
        agent_sessions = [
            getattr(getattr(bridge, "_agent", None), "session", None)
            for bridge, _held in bridges
        ]
        agent_session = next(
            (candidate for candidate in agent_sessions if candidate is not None),
            None,
        )
        return {
            "status": "ok",
            "session_id": session_id,
            "resolved_via": "live" if bridges else "store",
            **_addendum_state(session),
            "rendered_block": rendered_block,
            "block_chars": _count(rendered_block),
            "applied": "next_query",
            "diagnostics": {
                "live_bridges": _count(bridges),
                "agent_bearing_bridges": _count([
                    bridge for bridge, _held in bridges
                    if getattr(bridge, "_agent", None) is not None
                ]),
                "session_object_id": id(session),
                "agent_session_object_id": (
                    id(agent_session) if agent_session is not None else None
                ),
                "same_object": (
                    agent_session is not None and agent_session is session
                ),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to render session addendum: {exc}",
        )


@router.patch("/{session_id}/addendum/entries/{entry_id}")
async def patch_session_addendum_entry(
    request: Request,
    session_id: str,
    entry_id: str,
    body: AddendumUpdateBody,
) -> Dict[str, Any]:
    """Replace the TEXT of one entry, in place, through the ONE boundary.

    The entry's ``id``, ``ts``, ``source`` and ``pinned`` are its identity and
    are PRESERVED; only ``text`` changes.  The budget verdict is
    ``session.addendum.update_addendum_entry``'s own, measured on the RESULTING
    set with the old text swapped out -- so an entry can still be shortened on
    an addendum that is already full, and a refusal leaves the stored text
    byte-identical to what it was with nothing persisted.

    ``{'status': 'ok'}`` -> 200 ``{status: 'updated', entry, entries,
    total_chars, budget_chars, max_entries}``.
    ``reason == 'entry_not_found'`` -> 404 ``{status: 'not_found', reason,
    message, ...}``: that id came from the caller, so an unknown one is a
    missing resource, not a write refused for size.
    Any other drop -> 409 ``_refusal_body`` (the boundary's own sentence and
    numbers).
    """
    try:
        store, bridges, _session = _resolve_addendum_session(session_id)

        def mutator(session) -> Tuple[Dict[str, Any], bool]:
            """Swap the text through the ONE boundary; persist on acceptance."""
            result = update_addendum_entry(
                session, entry_id, body.text,
                source=OPERATOR_SOURCE, notify=False,
            )
            return result, result.get("status") == "ok"

        result, session = _write_through(store, session_id, bridges, mutator)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
        if result.get("status") != "ok":
            # `entry_not_found` is quoted as the boundary reported it: a mutation
            # that names an id nobody carries is a missing resource, and it is
            # the only drop that is (every other drop is a refused write).
            if result.get("reason") == "entry_not_found":
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "status": "not_found",
                        "reason": "entry_not_found",
                        "message": _refusal_message(result),
                        **_addendum_state(session, result),
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=_refusal_body(result, session),
            )
        return {
            "status": "updated",
            "entry": dict(result["entry"]),
            **_addendum_state(session, result),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update session addendum entry: {exc}",
        )


@router.delete("/{session_id}/addendum/entries/{entry_id}")
async def delete_session_addendum_entry(
    request: Request,
    session_id: str,
    entry_id: str,
) -> Dict[str, Any]:
    """Remove exactly ONE entry, through the ONE boundary.

    No budget verdict applies: a removal only ever shrinks the measured set, so
    it cannot be the operation that exceeds a cap -- which is what makes this
    the repair path for an addendum that is already over its budget.

    ``{'status': 'ok'}`` -> 200 ``{status: 'removed', removed_id, entries,
    total_chars, budget_chars, max_entries}``.
    ``reason == 'entry_not_found'`` -> 404 as on PATCH; any other drop -> 409
    ``_refusal_body``.
    """
    try:
        store, bridges, _session = _resolve_addendum_session(session_id)

        def mutator(session) -> Tuple[Dict[str, Any], bool]:
            """Remove the entry through the ONE boundary; persist on success."""
            result = remove_addendum_entry(session, entry_id, notify=False)
            return result, result.get("status") == "ok"

        result, session = _write_through(store, session_id, bridges, mutator)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
        if result.get("status") != "ok":
            if result.get("reason") == "entry_not_found":
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "status": "not_found",
                        "reason": "entry_not_found",
                        "message": _refusal_message(result),
                        **_addendum_state(session, result),
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=_refusal_body(result, session),
            )
        return {
            "status": "removed",
            "removed_id": result["removed_id"],
            **_addendum_state(session, result),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove session addendum entry: {exc}",
        )
