# Ticket: hygiene/inconsistent-localhost-check-pattern

**Filed:** 2026-10-21, from the F30(d) bind-host work (`--allow-remote-bind`).
**Severity:** medium — hygiene / "one fact, N sites". Not a live hole; both checks fail closed.
**Status:** open; NOT blocking, NOT to be fixed in this wave.

## Finding

The tree now answers "is this caller local?" two different ways, and the two answers are not
interchangeable.

1. **Peer-address check (new, F30).** `web_ui/backend/server.py` carries
   `_LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})`,
   `_TEST_HARNESS_CLIENT_HOSTS`, `_localhost_only(request)` (and, with F30(d),
   `_validate_bind_host`), gating `GET /health`, `GET /api/health`,
   `GET /api/health/containers` on `request.client.host`. Fail closed when
   `request.client is None`. Commit `ad1be717` (branch `fix/health-route-localhost-binding`).
2. **Origin/Referer check (house pattern).** `web_ui/backend/routes/vault_repair_routes.py:23-43`
   decides locality from the `Origin`/`Referer` headers instead. That is the pattern the rest of
   the FastAPI surface already uses for the same question.

`request.client` had **zero** uses anywhere in the tree's 627 `.py` files before commit
`ad1be717`; F30 introduced the first one. So the newer, better-informed check is also the one
nobody else shares.

## Why it matters

The two checks disagree in both directions:

- Behind a reverse proxy, `request.client` is the **proxy** (often `127.0.0.1`) → the peer check
  says local for a remote caller. The Origin check still sees the remote Origin.
- For a browser omnibox navigation, `Origin` is absent → the Origin check must fall back to
  permissive or deny indiscriminately; the peer check is unaffected.

Picking a definition per-route means the surface's blast radius depends on which helper the route
author happened to copy. `_validate_bind_host` inherits this by construction: it deliberately
reuses `_LOCAL_CLIENT_HOSTS` so the startup check and the health gate cannot drift, but that
constant is still a third notion of "local" relative to the Origin gate.

## Disposition options

**A (recommended, cheap)** — extract one helper (e.g. `web_ui/backend/local_request.py`) exposing
`is_local_request(request)` that consults **both** signals and states its policy (proxy-aware:
trust the peer when `request.client` is non-loopback, otherwise fall back to Origin/Referer), then
have `_localhost_only` and the vault-repair gate both call it. One definition, two call sites.

**B** — document `request.client` as the health-only mechanism and leave the two side by side. Cheap
but keeps the divergence.

Record both; the operator picks. Cross-reference: F30 (health gate + `--allow-remote-bind`),
`docs/` entry for the Web UI host-facing hardening.

## MUST NOT TOUCH

No behaviour change while this is merely filed: the refusal semantics of F30(d) (`SystemExit(2)`,
`--allow-remote-bind` opt-in) and the health 403 contract must not move. Do not fix this in the
F30 wave.
