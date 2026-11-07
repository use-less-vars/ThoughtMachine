# Ticket: bug/forwarded-allow-ips-local-gate-bypass

**Filed:** 2026-10-31
**Source:** read of `web_ui/backend/local_gate.py` + `web_ui/backend/auth_boundary.py`
**Severity:** critical for any deployment behind a reverse proxy that runs with `FORWARDED_ALLOW_IPS=*`; **not** an exposure of the default (loopback-only) install.
**Status:** filed, not dispatched.

## Defect

The loopback gate decides "is this caller on this machine?" from one input only: the ASGI peer address.

- `web_ui/backend/local_gate.py:48` `localhost_only(request)`
- `:55-59` `client = getattr(request, "client", None)` / `host = getattr(client, "host", None)`
- `:37` `LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})`
- `:45` `TEST_HARNESS_CLIENT_HOSTS = frozenset({"testclient"})`

That peer address is **not** always the peer. uvicorn's `ProxyHeadersMiddleware` is on by default (`proxy_headers=True`) and rewrites `scope["client"]` from the `X-Forwarded-For` header for any peer listed in `FORWARDED_ALLOW_IPS`. With the documented reverse-proxy value `FORWARDED_ALLOW_IPS=*`, *every* peer is trusted to supply that header, so a remote caller chooses its own "peer address". The gate then reads a rewritten, attacker-supplied host and treats it as local.

The whole gate rests on an input the caller can set. A loopback check that consults a forwarded-header-derived address is not a loopback check.

## Symptom

A request from a non-local machine is served by endpoints that are documented and tested as loopback-only, and the server sees it as the local peer the attacker named. `GET /health`, `GET /api/health/containers` and `GET /api/system/health` are the surfaces that were gated; `/api/system/health` returns the running-worker registry and the open sessions, i.e. vault-global operator state. No error, no log line: the caller simply looks local.

## Why it matters

- The gate exists precisely because "the server binds 127.0.0.1" is not a boundary: the bind is overridable (`--host` / `HOST`) and the module docstring (`local_gate.py:10-13`) says so — a reverse proxy "would forward a remote caller as a local one".
- `FORWARDED_ALLOW_IPS=*` is the value a reverse-proxy deployment reaches for, because a proxy's own source address is not fixed. Under that setting the bypass needs no misconfiguration beyond the setting itself.
- Impact is bounded by deployment: the default install binds loopback and runs without a proxy, so there is no remote caller to begin with. This is **not** a default-config exposure and must not be described as one. It is critical for exactly the deployments that use the setting.
- `TEST_HARNESS_CLIENT_HOSTS` matters here for the same reason: `"testclient"` is only unroutable while the peer address is real (see the constant's docstring at `:39-44`, which states the allowance is safe because "real traffic can never carry it"). Once `X-Forwarded-For` decides the host, real traffic *can* carry it — `X-Forwarded-For: testclient` satisfies the gate exactly as `X-Forwarded-For: 127.0.0.1` does.

## How to reproduce in principle

1. Run the app bound to a non-loopback address, or behind a reverse proxy, with uvicorn proxy headers on and `FORWARDED_ALLOW_IPS=*`.
2. From another machine, request a gated endpoint carrying `X-Forwarded-For: testclient` (or `: 127.0.0.1`, or `: ::1`).
3. The middleware rewrites `scope["client"]` to the supplied value; `localhost_only()` matches it against `LOCAL_CLIENT_HOSTS` / `TEST_HARNESS_CLIENT_HOSTS` and returns `None` (allow) instead of the 403 at `local_gate.py:61`.

Do not reproduce this on a shared host. A test-harness reproduction must inject the rewritten `scope["client"]` directly (that is the shape the current suite already uses for the harness peer), never a live proxy.

## Why it is currently unreachable — the mitigating fact, stated fully

The W4 auth boundary (`web_ui/backend/auth_boundary.py`, commit `f4c390a3`) refuses the whole proxy shape before anything else runs:

- `auth_boundary.py:26-36` — step 1 refuses any request carrying `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or `X-Real-IP` outright, and states that this converts the proxy discrepancy "from a silent bypass into a loud refusal".
- `auth_boundary.py:234` `FORWARDED_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")`.
- `auth_boundary.py:320` `forwarded_header(scope)`; applied at `:533-554`.

So **with the middleware installed and reached, the bypass is unreachable**: the request is refused at step 1, before the peer check is consulted, and the refusal names the header in the log. That is why this is filed as a latent defect and not as a live hole.

## Why it is still worth fixing — the aggravating fact, stated fully

W4 also **deleted the three per-route peer gates** (`/health`, `/api/health/containers`, `/api/system/health` no longer call `localhost_only()`). The middleware is now the *only* enforcement of that boundary:

- if `AuthBoundaryMiddleware` is not registered (a test app, a second ASGI factory, a future refactor), disabled, or bypassed for a path, nothing stands behind it — the old per-route gate is gone;
- the two mechanisms are not redundant any more, so a single point of failure now guards vault-global state;
- `local_gate.py` still ships, and still reads `request.client.host`. Any future re-wiring of it re-arms the bypass in full, because the middleware is the only thing making the forwarded case unreachable.

Defence in depth was traded for a single check. The single check is a good one; the trade should be recorded and the second layer repaired.

## Fix shape (not written)

The real fix is one rule: **a local-peer decision must never be influenced by a forwarded header.**

- Decide locality only from a peer address the ASGI server itself observed, i.e. the value before any proxy rewriting. Options, in preference order:
  1. Don't let the middleware rewrite the address the gate trusts: keep the raw peer (uvicorn supplies the un-rewritten value only before `ProxyHeadersMiddleware` runs; capture it at the outermost layer, e.g. a thin ASGI wrapper that stashes `scope["client"]` under a private key before the proxy middleware can touch it).
  2. Refuse the proxy shape at the gate itself — if `forwarded_header(scope)` is non-`None`, deny (fail closed) rather than resolve a host from it. This keeps the gate honest even when the middleware is absent.
  3. If a proxied deployment genuinely must be supported, require an operator-pinned proxy address (`FORWARDED_ALLOW_IPS` naming an explicit host) and still gate on the pre-rewrite peer; never on the header value.
- Do not "fix" this by adding `testclient`-only guards, by comparing the header to a fixed string, or by documenting the setting as unsafe. The defect is the trust relationship, not the spelling of the value.
- Restoring the per-route `localhost_only()` calls on the three routes is a separate, smaller decision (defence in depth) and may be ruled either way; it does not replace the rule above.

## Tests that pin the current behaviour

Local-peer decision, current semantics (all would stay green if the bypass were live, because none of them re-introduces a rewritten peer):

- `web_ui/backend/tests/test_local_gate_shared.py` — `test_loopback_and_harness_peers_are_allowed`, `test_only_the_defined_peers_are_local`, `test_no_peer_address_fails_closed`, `test_refusal_body_is_fixed_and_echoes_nothing`, `test_server_binds_the_shared_objects`, `test_module_declares_no_gate_of_its_own`, `test_module_restates_no_host_literals`, `test_health_route_allows_local_callers`, `test_health_route_refuses_a_remote_caller`.
- `tests/test_system_health_local_only.py` — `test_remote_client_is_refused`, `test_remote_client_cannot_reach_workers_or_sessions`, `test_missing_peer_address_is_refused`, `test_loopback_client_is_served`.

The step-1 refusal that currently makes the bypass unreachable:

- `web_ui/backend/tests/test_auth_boundary.py` — `test_a_forwarded_header_is_refused_even_with_a_valid_credential` (parametrised at `:626-634` over `("x-forwarded-for", "testclient")`, `("x-forwarded-for", "127.0.0.1")`, `("x-forwarded-host", "evil.example")`, `("forwarded", "for=127.0.0.1")` — the first two are exactly the spoofed-peer shape), `test_the_forwarded_cases_cover_every_header_the_boundary_refuses`, `test_the_proxy_refusal_applies_to_an_exempt_path` (drives `/health` with `x-forwarded-for: 1.2.3.4` at `:682`), `test_the_forwarded_refusal_names_the_header_in_the_log`, and `test_non_local_peer_is_refused_before_the_credential`, `test_the_harness_peer_is_allowed_when_the_flag_is_on`, `test_the_harness_peer_is_refused_when_the_flag_is_off`.

A regression test for the fix must assert that a request whose peer address was rewritten from `X-Forwarded-For` (scope injected directly, as the harness tests do) is **denied** by the gate, i.e. it must fail today.

## Bearing

Same family as `ticket-bug-unauthenticated-routes-beyond-health.md` and the `displayed ≠ enforced` cluster: the surface says "loopback only" while the enforcement reads an input the caller controls. Distinct mechanism — the enforcement is real, its input is not local.
