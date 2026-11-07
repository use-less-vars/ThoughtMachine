# Ticket: bug/ws-handshake-bypasses-cors-origin-check

**Filed:** 2026-10-21, from the unauthenticated-surface pass at `9d5ed869`, while checking whether the CORS configuration bounds the WebSocket as well as the HTTP routes.
**Severity:** HIGH — the only origin policy on the process is an HTTP middleware; it does not apply to `ws://`, and the WebSocket is accepted before any header is read.
**Status:** open; verified by read at `9d5ed869` (grep + read, not exercised at runtime). NOT blocking, NOT to be fixed in this wave.

## Finding

The process states an origin policy exactly once, and states it as an HTTP middleware (`web_ui/backend/server.py:1103-1112`):

```python
# ── CORS (allow frontend dev server on any port) ────────────────────────────
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

The WebSocket endpoint has no counterpart (`web_ui/backend/server.py:1125-1127`, `:1131`):

```python
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, project: Optional[str] = None):
    await ws.accept()
    ...
    _project_path = project or _project_root
```

`accept()` at `:1127` happens before anything is read off `ws` — no `origin`, no `referer`, no token. I grepped `web_ui/backend/*.py` for `origin` casing-insensitively; every hit is one of: (i) the CORS middleware block above; (ii) `vault_repair_routes.py:22-23` `_ORIGIN_RE` and `:30-44` `_origin_allowed`, used at `:58` and `:100` for two REST endpoints only; (iii) unrelated identifiers (`_original_created_by`, "the origin of a container", `_make_bus_handler(original_type=...)`). Nothing in `websocket_endpoint` — or anywhere on the WS dispatch path — reads `Origin` or `Referer`.

That matters because browser CORS does not apply to `ws://`. A page served from any origin can open `ws://127.0.0.1:<port>/ws` and speak the command protocol; the `Access-Control-Allow-Origin` grant that the middleware manages is never consulted for the upgrade. So the server's most powerful transport is the one with no origin policy at all, and `allow_origins=["*"]` (`:1108`) gives a false impression of a browser boundary that only exists on the other transport.

The origin of the connection is also where the project root is set: `:1131` takes `?project=` verbatim (`_project_path = project or _project_root`), before any handshake decision. So an accepted socket both carries an unvalidated workspace root *and* is the surface all subsequent commands run on (`delete_session` `:2378`, `security_response` `:2887`, `apply_config` `:1397`+, `load_session` `:2102`, …).

Two things this is not, stated to keep the finding precise:

- It is not "there is no way to know how to do this here". The check exists, one directory away, in the shape this needs: `vault_repair_routes.py:22-23` compiles `_ORIGIN_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$", re.I)` and `:30-44` accepts an `Origin` header with a `Referer` fallback and **fails closed on a missing header** (`if not raw: return False`). That is a ready-made policy for loopback-only serving; it is simply not applied to the socket.
- It is not authentication. An origin check stops a *browser page* from opening the socket; it does not stop a local process, and it is trivially absent from curl. It closes the CORS-shaped hole, not the F30 hole.

## Why it matters

The threat model behind the CORS ticket is "the user's own browser becomes the transport": uvicorn listens on loopback, the user visits some other page, that page's JavaScript drives the API. The HTTP half of that story is at least *argued about* in `server.py:1103-1112` (badly, with `*`, but visibly). The WebSocket half has no policy to argue about — and the WS surface is strictly larger than any single REST route, since it exposes the whole command roster, including the permission-prompt answer (`:2887`) and session deletion (`:2378`).

Accept-before-check also means the failure mode is silent: a rejected socket would be indistinguishable from a dropped one at the client, so any future check has to decide deliberately whether to close before or after `accept()` (Starlette delivers a pre-`accept()` close as a failed handshake, which is the correct signal for a browser that should not be talking to you).

## Disposition options

- **A. Check `Origin`/`Referer` before `accept()`.** Read `ws.headers.get("origin")` / `("referer")`, apply the loopback regex from `vault_repair_routes.py:22-23`, and close (or `accept()` then `close(code=1008)`, whichever the handshake contract wants) on a non-loopback or missing origin. *This is the recommendation* — it is ~10 lines, it matches a policy the process already publishes for its REST peers, and it closes the browser-as-transport path.
- **B. Require a process-minted token.** Generate a random token at startup, embed it in the served page, require it as a query parameter or `Sec-WebSocket-Protocol` value before `accept()`. Stronger (survives non-browser clients and origin-header-free agents), but it needs the frontend to carry it, and it is really the first inch of the F30 auth story.
- **C. Declare the WS out of scope and say so.** Extend the CORS comment (`:1103`) to state that the socket is origin-unchecked by design and that loopback binding is the only control. Honest, zero-risk, and leaves the browser path open.

Recommendation: **A** now, **B** when F30 is decided. Do not let A be described as a fix for the unauthenticated-command class — it bounds one client population, nothing more.

## MUST NOT TOUCH

- The order of `await ws.accept()` and the surrounding setup (`server.py:1125-1135`): whatever is added must not move `accept()` without checking how the existing client reacts to a pre-accept rejection, and the `_project_path` initialisation at `:1131` must keep running for accepted sockets.
- `vault_repair_routes._origin_allowed` / `_ORIGIN_RE` — they take a `Request`; the WS gives you `ws.headers`. Share the regex or the policy, not the function signature, and do not change the REST behaviour (it is referenced by its own tests).
- The CORS middleware block (`:1103-1112`) — it is the subject of `ticket-bug-cors-wildcard-exposes-api-to-browser-origin.md` and must be decided there, not here.
- The `?project=` parameter's *meaning* (`:1131`) — that is `ticket-bug-ws-set-project-bypasses-vault-confinement.md`'s confinement question; this ticket is only about who may open the socket.
