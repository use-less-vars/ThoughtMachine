# Ticket: `/api/auth/session` was exempt at the boundary but had no handler

Status: fixed in the W5 working tree (uncommitted at time of writing)

## Problem

`/api/auth/session` was listed in the auth boundary's exemption set from the moment the
boundary landed, but no route ever answered it, so `GET /api/auth/session` returned 404.
The exemption is a statement about an endpoint that answers "do you hold a credential?" —
instead the path was reserved-but-unimplemented, and the reservation was invisible in
review: an exemption reads like a deliberate hole for something that exists, not like a
promise about something that does not.

The consequence is sharper than a missing endpoint. Because the path is exempt, the
middleware cannot be the component that answers the question — it never sees the
credential on an exempt path. So the route itself has to perform the credential check,
and until it existed the frontend had no way to distinguish "log in" from "server down":
`/health` says the process is up, and that is all it says.

## Evidence

- `web_ui/backend/auth_boundary.py:171` — the comment describing the path's intended role
  ("must be able to answer *do you have a credential?* *before* the caller has one").
- `web_ui/backend/auth_boundary.py:186` — the `EXEMPT_PATHS` entry itself
  (`EXEMPT_PATHS` is declared at `:181`).
- `web_ui/backend/auth_boundary.py:147` and `:484` — two further references to the route
  (the credential-kind constant and `credential_kind`) that describe an endpoint which,
  before this wave, did not exist to serve them.
- No handler in `web_ui/backend/server.py` before this wave: `grep -n "api/auth/session"`
  there returned only prose comments, no decorator. (Git history was not consulted —
  see "What was not verified".)
- No frontend reference: `grep -rn "api/auth/session" web_ui/frontend/src` returned
  nothing before this wave.
- The only test references asserted the exemption itself, not a response:
  `web_ui/backend/tests/test_auth_boundary.py:176-187`
  (`test_exempt_set_is_the_documented_list`).

## Suggested fix / status

**Fixed in W5.** A real handler now exists:

- `web_ui/backend/server.py:4112` — `@app.get("/api/auth/session")`, handler
  `auth_session` at `:4113`.
- `200 {"authenticated": true, "credential": "cookie"|"bearer"}` at `server.py:4145`,
  with the kind derived from `auth_boundary.credential_kind` (one policy, not a second
  sniffing path); `401 {"authenticated": false}` at `:4138` and `:4143` otherwise.
- It performs its own credential check precisely because the path is exempt from the
  middleware (`auth_boundary.py:171`).
- The body never echoes the token, a token prefix, a length, a hash, or a filesystem
  path — see the docstring at `server.py:4116-4128`.
- Coverage: `web_ui/backend/tests/test_auth_session_route.py` (306 lines; 14 test
  functions, 17 collected cases) — both channels accepted, garbage refused on both,
  a rotated cookie refused, no token material in any body, plus the `/api/health`
  narrowing and the `/api/system/health` gating pinned alongside.

## Residual risk (flag, do not ignore)

The endpoint is reachable **unauthenticated** and answers a yes/no about a presented
guess, so it is a cheap "does this guess verify?" oracle for anyone who can reach the
port. Mitigations in place: it verifies but never echoes (no prefix, length or hash, so
a wrong answer leaks nothing but "no"), and the secret is high-entropy random bytes
(`web_ui/backend/auth_token.py`). If this ever matters, the next step is a rate limit or
a short failure delay on this path only — not un-exempting it, which would put the
"log in vs. server down" question back where it started.

## What was not verified

Git history was not consulted (the tree is a working copy with no commits from this
wave), so the "no handler existed before" claim rests on reading the current tree's
comments, not on a diff against the pre-wave commit.
