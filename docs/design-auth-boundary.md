# Design pass — the authentication boundary the Web UI does not have

Base: `dev = 6e01a004` (merge of #268, `w3/residuals`). Design only: no code, no tests, no token, no commits.

Read first, and unchanged by this pass: `docs/design-auth-layer.md` (D1–D4). That document decided *what localness means* and produced the rules that later passes can be small: keep `--allow-remote-bind` as a declared hatch (D1), make the origin check the default rather than the exception (D1), make an unscoped socket a transition state (D2), give the ownership predicate one authority (D3). It also named a chain it deliberately did not walk — *"Sequence. credential-delivery → credential-resolution → D4 design pass"* (§D4). **This document is that first link: credential-delivery.** It adds no fifth decision, overturns none of D1–D4, and takes their line citations as *stale* (see §10).

## 1. The claim

The Web UI's boundary is **the listen address plus a habit**. The address is checked once at boot (`_validate_bind_host`, `web_ui/backend/server.py:4036`, called from `main()` at `:6224`); the habit is a per-route origin call that 39 routes remember and 45 others do not need to. Nobody is authenticated, and the reason no one noticed is that a loopback peer is *almost* the right answer on a single-user appliance — it fails only for the four callers in §3 that arrive as loopback without being the operator.

The decision this pass has to make is therefore narrow and concrete: **what credential a caller must present, how a browser carries it without an operator ever typing anything, and the one place it is checked so that a route filed next week is protected by construction rather than by review.**

## 2. What is actually in the tree

Method: decorator grep over `web_ui/backend/*.py` for `^\s*@<name>\.(get|post|put|delete|patch|websocket)\(`, then each decorator's body scanned up to the next decorator. Cross-checked three ways: no `add_api_route` anywhere, no `add_websocket_route` anywhere, 12 `APIRouter(...)` definitions all included at `server.py:3802-3815`. So *decorator count = route count* here, and one `app.mount(` at `server.py:6149` (the built frontend) is the only non-decorator surface.

| Module | Router prefix (line) | Routes |
|---|---|---|
| `workspace_routes.py` | `/api/workspace` (`:63`) | 26 |
| `server.py` (direct `@app`) | — | 25 (24 HTTP + `WS /ws`) |
| `session_routes.py` | `/api/session` (`:52`) | 6 |
| `global_routes.py` | `/api` (`:56`) | 4 |
| `prompt_routes.py` | `/api/prompts` (`:17`) | 4 |
| `addendum_routes.py` | `/api/session` (`:72`) | 3 |
| `config_routes.py` | `/api/config` (`:25`) | 3 |
| `container_record_routes.py` | `/api/container-records` (`:29`) | 3 |
| `onboarding_routes.py` | `/api/onboarding` (`:33`) | 3 |
| `provider_routes.py` | `/api` (`:36`) | 3 |
| `logging_routes.py` | `/api/logging` (`:25`) | 2 |
| `vault_repair_routes.py` | `/api` (`:27`) | 2 |
| `health_routes.py` | `/api/system` (`:20`) | 1 |
| **Total** | | **85** |

85 decorators = **84 HTTP** (`GET` 45, `POST` 24, `DELETE` 7, `PUT` 6, `PATCH` 2) + **1 WebSocket** (`server.py:1260`). The briefing's "85 routes" is right and its "3 gated" is right; the arithmetic that follows from it is not, see §10.

**Peer-gated (3).** All three call the same predicate, and the third copy is frozen rather than removed:

| Route | Decorator | Gate call |
|---|---|---|
| `GET /health` | `server.py:4071` | `server.py:4073` (`local_gate.localhost_only`, imported `:227`) |
| `GET /api/health/containers` | `server.py:5896` | `server.py:5910` |
| `GET /api/system/health` | `health_routes.py:215` | `health_routes.py:234` (module-private copy at `:185-196`; FROZEN for W3) |

**Origin-guarded (42 sites).** `origin_policy.py:86` `_origin_check_request` (and `origin_policy.py:69` `_origin_allowed`, used by `vault_repair_routes.py:45`, `:87`) is called from **39 of 39 mutating routes** — full coverage, verified route by route — plus 3 GETs (`addendum_routes.py:310`, `server.py:3893` `/api/browse`, `vault_repair_routes.py:40`). The guard arrived as `f00aebe9 feat(security): refuse foreign-origin calls on all 37 non-GET routes`; the tree now has 39, i.e. the habit reproduced itself twice without a test noticing. Two predicates with **opposite semantics for an absent `Origin`** are live at once: `_origin_check_request` allows it (`origin_policy.py:107-113`, deliberately, for the CLI), `_origin_allowed` refuses it (`:75-77`).

**Ungated reads that matter.** 42 of 45 GETs have neither gate. The ones whose payload is machine-wide or vault-wide, not workspace-scoped:

- `GET /api/global/summary` (`global_routes.py:326`), `GET /api/credentials` (`:406`) — file *names* only, values never returned (`:407-419`), from `<vault>/credentials/global` (`:349-351`).
- `GET /api/providers` (`provider_routes.py:72`) — the plaintext `api_key` contract is asserted by `tests/test_providers_crud.py:79`.
- `GET /api/session/list` (`session_routes.py:209`), `GET /api/session/{id}` (`:263`), `GET /api/session/{id}/permissions` (`:435`) — every session on the machine, by id.
- `GET /api/workspace/list` (`workspace_routes.py:197`), `/{ws_id}/summary` (`:1972`), `/{ws_id}/permissions` (`:1683`), `/{ws_id}/workers/active` (`:621`).
- `GET /api/vault/status` (`server.py:4089`), `GET /api/tools` (`:3932`), `GET /api/browse` (`:3893`), `GET /api/resource-catalog` (`:4147`), `GET /api/container-records` (`container_record_routes.py:130`).

**No `Depends(`, no `Security(`, no cookie, no `Authorization`, no `Host` check.** All four are zero hits across `web_ui/backend/` (`Depends(`/`Security(`: 0; `set_cookie`/`SameSite`/`HttpOnly`: 0; `authorization`/`bearer`: 0 outside two prose lines at `server.py:34` and `:1210`; any `request.headers` read of `host`, and `Sec-Fetch-*` anywhere: 0). The prose is the current design of record: *"This app authenticates with nothing (no cookie, no Authorization header), so `allow_credentials` is off"* (`server.py:1210-1217`).

## 3. Threat model — before the mechanism

Adversaries, in the order they are cheap to reach:

| # | Caller | Reaches today because | Stopped today by |
|---|---|---|---|
| **A** | Remote host, `--allow-remote-bind` set | `_validate_bind_host(..., allow_remote=True)` waives loopback (`server.py:4036`, `:6224`); the opt-in is `store_true` (`:6177-6185`) and announces itself (`:6233-6240`) | **nothing** — no credential exists to be wrong |
| **B** | Anything that re-originates a loopback connection: reverse proxy, `ssh -L`, port-forward, container gateway | `_localhost_only` reads `request.client.host` (`local_gate.py:55-60`), i.e. the *last hop*; a proxy is invisible to it | the 3 peer gates, i.e. nothing that matters; all 82 remaining surfaces are open |
| **C1** | Browser tab on `evil.com` → CSRF against a mutating route | nothing, but… | **origin guard**: 39/39 non-GET routes refuse a foreign `Origin` (`origin_policy.py:107-129`); CORS narrows `allow_origins` to two loopback spellings (`server.py:1214-1220`) |
| **C2** | Browser tab on `evil.com` → **DNS rebinding** | the attacker's hostname resolves to `127.0.0.1`, so the page becomes same-origin with the UI: `Origin` is now `http://evil.com:PORT`… and the peer is genuinely loopback | **nothing**. No `Host` validation exists anywhere (§2), so a page served from `evil.com:PORT` reads `GET /api/session/list` and `GET /api/providers` as same-origin. This is the one adversary the origin guard cannot see and the peer gates cannot see either |
| **C3** | Browser tab on `evil.com` → cross-site WebSocket hijack | `ws://` handshakes are exempt from CORS, so a page on any origin can open `/ws` | **the handshake origin check** (`server.py:1266-1269`, `_reject_websocket` `:1247`) — added after `ticket-bug-ws-commands-unauthenticated.md` was filed (that ticket's `:1125-1127` "accepts first and asks nothing" is stale). 27 commands, 16 mutation-capable, still behind **no credential** |
| **D** | Another local OS user, or any local process (`curl`, a script, a rogue app) | loopback peer = operator, by assumption | **nothing** except the 3 peer gates. CORS is a browser-enforced control and does not apply |
| **E** | A worker container holding the agent's `host_bash`/network reach | containers default to `network_mode="none"` (`infra/container_create.py:285`, decided `:398-411`); with a network granted, its peer address is the docker gateway, not loopback | refused by the 3 peer gates; **allowed by everything else** — and a prompt-injected worker is precisely the caller that knows the API shape |

**Non-goals, explicitly.** (1) Multi-user identity, RBAC, or "who may do what": §D4 of `design-auth-layer.md` owns that and is not decided here. (2) Wire encryption: TLS on loopback is not a control this design adds, and a remote bind stays plain HTTP. (3) Defending against a compromised *operator account* or a hostile process running as the same uid — it can read the credential file by construction (§4). (4) The security gate's per-tool approval semantics; the container/workspace isolation boundary (`docs/workspace_isolation_matrix.md`); the frozen `health_routes.py` copy. (5) Making `--allow-remote-bind` *safe*: this design makes it **not anonymous** (§6), which is a smaller claim.

## 4. Mechanisms

### (i) Start-up bearer token in the vault, auto-consumed by the frontend

Server mints a random token at boot, writes `<vault>/state/web_ui_token` (0600; `VAULT_SUBDIRS` at `thoughtmachine/vault.py:45-53`, `0o700` dirs `:76-85`, `state/session_registry.json` as the existing precedent at `:349`), and the frontend injects it into every request and into the WS URL.

Cost: a minting step, a storage decision, a frontend change in `apiBase.js:13-21`, and a `?token=` query parameter on `/ws` — the one place query strings are already read (`?project=`, `server.py:1274`) and therefore already logged.

**The bootstrap circularity.** "Auto-consumed by the frontend" cannot mean "read the file": a browser cannot. It means the frontend fetches the token from an endpoint that, by definition, must work *before* the credential exists. That endpoint can only be gated by peer-loopback and `Host` — and adversary **C2** (rebinding) satisfies both, because after rebinding the browser really is talking to `127.0.0.1`. So (i) does not authenticate the browser. What it does authenticate is a *non-browser* caller (D, and A once the operator copies the file), because `curl` has no way to obtain the token without filesystem access.

### (ii) HttpOnly signed session cookie, minted at a loopback bootstrap

The server sets an `HttpOnly; SameSite=Strict; Path=/` cookie, signed with a secret persisted in the vault, on the first same-origin document request (`GET /`, `server.py:6015`) or on an explicit `GET /api/auth/session`. No operator action: opening `http://127.0.0.1:8000/` is the login. The browser then attaches it to every `fetch` and — the decisive property — to the **`/ws` handshake**, with zero frontend change (`apiBase.js:18-21` builds `ws://<page host>/ws`, so the cookie is same-origin and is sent).

Why this survives C2 where (i) does not: `SameSite=Strict` is evaluated on the *page's* site, and a rebound page's site stays `evil.com`. The browser will not attach a `127.0.0.1` cookie to a document whose origin is `http://evil.com:PORT`, so rebinding loses the credential even though it wins the peer and `Host` arguments. It also survives C1 for free, and D completely (no cookie jar to forge). It needs `allow_credentials=False` (`server.py:1217`) left exactly as is: same-origin through the vite proxy (`vite.config.js:13-22`) and through the bundled static mount (`server.py:6149`) means no CORS path is involved; re-enabling credentialed cross-origin would turn the two loopback origins into a new hole.

Weakness, stated plainly: a cookie is ambient. Anything that can make the operator's browser issue a same-site request (a same-site `GET`, a link) can drive the API — which is why §5 still pins `Host` and keeps the origin guard, and why the cookie must carry a `SameSite=Strict` and not `Lax`.

### (iii) OS peer credentials, or a proxy-trust shared-secret header

`SO_PEERCRED` (`struct ucred` via `getsockopt` on `AF_UNIX`) yields the caller's **uid**, which is the only mechanism here that actually distinguishes "the operator's browser" from "another local user's script" (adversary D) rather than merely bounding it. It is also undeliverable as the primary control: the API is TCP (`uvicorn.run`, `server.py:6254`) and a browser cannot speak `AF_UNIX`, so applying it would mean a second listener plus a proxy — new surface, new failure modes, and a Windows port problem.

The proxy-trust variant (trust `X-Forwarded-*`/`X-TM-Client` from a "trusted" reverse proxy, shared secret in a header) is worse than useless here: nothing in the tree can verify the proxy, so any local process **forges** the header and is authenticated by it. Rejected.

### Recommendation — **(ii), with (i)'s file as the root secret**

- **Credential**: a signed cookie carrying `{issued_at, secret_id}` HMAC'd with a 32-byte secret at `<vault>/state/web_ui_token`, mode 0600, created on first boot if absent, never logged, never returned by any route.
- **Delivery**: `Set-Cookie` on `GET /` when the request is (peer loopback) ∧ (`Host` ∈ pinned set) ∧ (`Sec-Fetch-Site: same-origin` or absent-with-loopback-peer for non-browser); otherwise 403 with the reason in the body, so an operator can see *which* of the three failed.
- **Non-browser callers** (CLI, integration harness, `ssh -L` user) present the same secret as `Authorization: Bearer <file content>`; that is the documented, one-line path, and it is why the secret is a file and not memory-only.
- **Rotation** is `rm <vault>/state/web_ui_token` (next request mints a new one, all cookies die). **Loss** is the same action — the appliance self-heals, and nobody is ever locked out, only re-issued.
- **`--allow-remote-bind`** stops being an authentication-free hatch: with it set the credential is mandatory (it already would be), and the warning text (`server.py:6233-6240`) changes from "unauthenticated" to "authenticated by a token file the operator must distribute".

**What this makes worse, honestly.** (1) Every existing test that drives the API through `TestClient` acquires a credential or an exemption — the exemption is unavoidable and is the same shape as `local_gate.py:45`'s `TEST_HARNESS_CLIENT_HOSTS`, which means it is an in-process bypass by construction. (2) `curl` one-liners, `docs/` examples, and any operator script stop working until they read the file: the appliance's "just works" becomes "just works in a browser". (3) A browser-dependence on `Sec-Fetch-Site` and `SameSite=Strict` puts a floor under the supported browser versions, and non-browser bootstrap clients must be handled by the "absent" branch — a branch that a hand-written attacker can also satisfy, so it must not be the branch that *mints* cookies for a browser-shaped request. (4) Two uvicorn processes (two workspaces) sharing one `HOME` would share one secret and therefore one credential realm — see §6. (5) The cookie adds a stateful failure mode (stale cookie after rotation) that a bearer token does not have; the frontend must recover by reloading `GET /`, not by prompting.

## 5. Enforcement — fail closed, structurally

**Mechanism: one ASGI middleware, one enumerated exemption table, both in a new module `web_ui/backend/auth_boundary.py`, mounted at `server.py:1222`** — immediately after the CORS block (`server.py:1214-1220`) and before the first route declaration (`server.py:1260`). Middleware added at that point is the outermost wrapper, so it sees `scope["type"]` in `{"http", "websocket"}` for every surface in §2 **plus** the static mount (`server.py:6149`) that no router owns.

Why not the two alternatives:

| Option | Verdict |
|---|---|
| App-level `dependencies=[Depends(...)]` on `FastAPI(...)` (`server.py:1164-1168`) | Applies to every route for free — but it cannot express the exemption. Exempting the 3 health routes and `GET /` would mean mounting them on a second app or hand-mutating `route.dependant` at import. Also there are currently **0** `Depends(` in the package, so the mechanism would arrive with no precedent to copy |
| Subclassed `APIRouter` (+ subclassed `FastAPI`) overriding `add_route`/`add_api_route` | Covers both router-declared and `@app.get`-declared routes, and makes the *author* unable to forget. Rejected as the primary because `include_router` re-registers each route on the app (`server.py:3802-3815`), so the dependency is applied twice unless carefully made idempotent; and because the exemption becomes a per-route argument scattered across 13 modules instead of one diffable table. It is the right *second* belt if the middleware ever needs a per-route opinion |
| ASGI middleware + exemption table | One place, covers HTTP, WS and the static mount uniformly, refuses by default for a path nobody thought about, and the exemption list is a single visible literal. Cannot prove a route was *intended* to be gated — which is exactly why §6 exists |

Decision order inside the middleware (each branch returns; there is no fallthrough that allows):

```
1. scope["type"] not in {"http","websocket"}          -> pass through (lifespan)
2. path in BOUNDARY_EXEMPT                            -> annotate scope["state"]["auth"]="exempt:<reason>"; continue
3. Host header not in PINNED_HOSTS                    -> refuse 403 "host"        # adversary C2
4. credential absent                                  -> refuse 403 "no-credential"
5. credential signature invalid / unknown secret_id   -> refuse 403 "bad-credential"
6. credential valid                                   -> annotate scope["state"]["auth"]="operator"; continue
WS refusals close before accept with 1008 via the existing
`_reject_websocket` helper (`server.py:1247`); never accept-then-close.
```

`BOUNDARY_EXEMPT` is a `Final[dict[str, str]]` literal — path → one-sentence reason — because a dict edit is a reviewable diff and a regex is not. Contents: `/health` (`server.py:4071`) and `/api/health/containers` (`:5896`) — peer gates that stay, they simply stop being the only gate; `/api/health`, the liveness alias the launcher and the CI smoke jobs poll over loopback with no credential; `/api/auth/session` (the cookie mint, §4-ii); `/` (the index document, `server.py:6015`) — the login page cannot itself require a login — plus the `/assets/` prefix (the vite bundle's asset directory, served by the `app.mount("/", StaticFiles(...))` at `server.py:6149-6153`), which lives in `EXEMPT_PREFIXES` rather than the exact-match set. **Five paths plus one prefix.** `GET /api/system/health` (`health_routes.py:215`) is deliberately *not* exempt: it reports live worker and session counts, i.e. deployment state rather than a liveness bit, so it now requires the credential (its peer gate and its payload are unchanged).

Two consequences to state rather than discover. (1) The mount is at `/`, not at a prefix (`server.py:6149`), so the middleware cannot tell a static asset from a mistyped API path; an unknown path therefore gets **403, not 404**. That is the correct trade (fail closed) but it *is* a behaviour change, and it only reaches `--serve-frontend`/prod — in dev the vite server serves the assets and proxies only `/api` and `/ws` (`vite.config.js:13-22`). (2) If the bundle's asset directory is ever renamed, the UI breaks with 403s in the console and the fix is one line in the same table — the S5 checklist owns that check. Every other path, including `/ws` (`server.py:1260`) and all 42 origin-guarded mutating routes, requires a credential. The origin guard is **not** replaced: it is a different question (which *browser context*) and it keeps working, now in front of an authenticated caller rather than instead of one.

Pinned hosts: `localhost:<port>`, `127.0.0.1:<port>`, `[::1]:<port>` for both the backend port and `TM_FRONTEND_PORT` (`origin_policy.py:26-48` already owns the frontend port). Both spellings are needed because the vite proxy sets `changeOrigin: true` on `/api` only (`vite.config.js:20`), so the backend sees `127.0.0.1:8000` for API calls and `localhost:5173` for the WS upgrade (`:14-17`).

**How a browser WS carries the credential: the cookie.** Not a query token — `/ws` already reads `?project=` (`server.py:1274`), so a secret in the query string lands in the uvicorn access log, in any proxy log, and in shell history. Not a subprotocol — `Sec-WebSocket-Protocol` requires a JS change at every `new WebSocket(...)` call site to carry a *non-secret* echo value, and buys nothing the cookie does not. The cookie is attached to the handshake by the browser itself, is `HttpOnly` (so the token never exists in JS-visible memory), and the frontend's `wsUrl()` (`apiBase.js:18-21`) needs no edit at all.

## 6. Coverage proof — mechanical, not reviewed

New test: `web_ui/backend/tests/test_auth_boundary_coverage.py`, modelled on `web_ui/backend/tests/test_local_gate_shared.py` (which is the precedent for *pinning a boundary by enumerating implementations rather than by trusting them* — and for recording its own residual in the module docstring).

```python
# shape, not final code
def _all_paths(app):
    http = {r.path for r in app.routes if hasattr(r, "path")}          # 84 APIRoutes
    ws   = {r.path for r in app.router.routes if r.__class__.__name__ == "APIWebSocketRoute"}
    return http | ws

def test_every_route_refuses_without_credential(client):
    for path in _all_paths(app) - set(BOUNDARY_EXEMPT):
        concrete = path.replace("{", "x").replace("}", "")            # any match is enough
        r = client.get(concrete)                                       # and .post/.delete/...
        assert r.status_code == 403 and r.json()["reason"] == "no-credential"

def test_exemptions_are_real_routes():
    assert set(BOUNDARY_EXEMPT) <= _all_paths(app) | {"/", "/assets/"}

def test_exemptions_carry_a_reason():
    assert all(isinstance(v, str) and len(v) > 20 for v in BOUNDARY_EXEMPT.values())

def test_no_route_is_gated_only_by_origin():
    # a route that calls _origin_check_request is *not* thereby authenticated
    ...

def test_middleware_is_mounted(app):
    assert any(m.cls is AuthBoundaryMiddleware for m in app.user_middleware)
```

The load-bearing assertion is the first one: add a route tomorrow and it fails until either the credential check applies (it does, automatically) or somebody writes its name into `BOUNDARY_EXEMPT` — which is the visible diff the design wants. The count itself becomes an assertion (`len(_all_paths(app)) == 85`) so that a route deleted by accident is also a red test.

Behaviour matrix the same file must pin:

| Case | Expected |
|---|---|
| valid credential, loopback peer | 200 |
| absent credential | 403 `no-credential` |
| malformed / non-HMAC credential | 403 `bad-credential` |
| credential signed with a *different* secret (second vault) | 403 `bad-credential` |
| credential valid, `Host: evil.com:PORT` (rebinding) | 403 `host` |
| cookie present but `Sec-Fetch-Site: cross-site` on the mint endpoint | 403, no `Set-Cookie` |
| `WS /ws` with cookie | accepted, existing handshake guards still run (`server.py:1266-1279`) |
| `WS /ws` without cookie | closed 1008 **before** accept (`server.py:1247`) |
| `WS /ws` with cookie but foreign `Origin` | still closed 1008 (origin check is not replaced) |
| credential store unreadable (permissions, missing dir) | 503 `credential-store` — never "allow because unknown", never a 500 |
| `TEST_HARNESS_CLIENT_HOSTS` peer (`local_gate.py:45`) | exempt, and the exemption is asserted to be *only* that constant |
| the 3 peer-gated routes, remote peer | still 403 `localhost only` (their own gate, unchanged) |

## 7. Rollout

| Slice | Content | Operator action | Green means |
|---|---|---|---|
| **S1** | `auth_boundary.py`: secret file, HMAC, `BOUNDARY_EXEMPT`, middleware mounted with **every path exempt** (log-only `auth=<absent>` line, never refuses) | none | 400-test suite unchanged; `grep auth= ` shows every request annotated |
| **S2** | Coverage test lands asserting the *exempt* set equals all 85 — a deliberate, one-line red-to-green ratchet | none | the ratchet is the file we edit, not the middleware |
| **S3** | Enforcement on for `/ws` + the 39 mutating routes; exemptions shrink to the five paths plus the `/assets/` prefix in §5 | none (browser login is a redirect to `/`) | WS ticket's 16 mutation-capable commands now behind a credential |
| **S4** | Enforcement on for the 42 GETs; CLI/harness gain the `Authorization: Bearer <file>` path; `--allow-remote-bind` warning text updated (`server.py:6233-6240`) | re-run any script with the header | adversary D closed; `docs/` examples updated |
| **S5** | Host pinning turned on (the one branch that can break a working proxy); `_validate_bind_host` (`server.py:4036`) unchanged | none | C2 closed |

Kept working throughout: the 3 peer gates and `test_local_gate_shared.py` (they are now redundant, not replaced — deleting them is a separate decision, and the frozen `health_routes.py` copy stays frozen); loopback probes (`GET /health` from a monitoring script) because `/health` is exempt *and* peer-gated; `--allow-remote-bind` still boots, now into an authenticated surface.

Rotation & loss: `rm <vault>/state/web_ui_token` → next request mints; every cookie dies at the next request (signature check fails → 403 → the frontend reloads `GET /` once and gets a fresh cookie). A missing vault dir is a **503**, not an allow.

Failure modes:

| Failure | Behaviour | Why that is the right answer |
|---|---|---|
| Secret file deleted while running | 503 `credential-store` until next request mints it (S1 behaviour decides: mint-on-read, not mint-once) | an appliance must not need a restart to heal |
| Stale cookie after restart with a *reused* secret | still valid — a restart is not a logout | the operator did not ask to be logged out |
| Stale cookie after rotation | 403 once, then `GET /` re-mints | one extra navigation, no prompt |
| Two workspaces, one `HOME` → one secret | one credential realm, two servers: a cookie minted by either is accepted by both | acceptable *only* if `Host` pinning includes both ports; otherwise the two servers must own `<vault>/state/web_ui_token.<port>` — open question §8-2 |
| Second tab | same cookie jar, works | zero-login preserved |
| Frontend hard-coded port | not an issue today: `apiUrl()` is a relative path and `wsUrl()` uses `window.location.host` (`apiBase.js:13-21`) | the cookie follows the page origin, so this stays true |
| A local process as the same uid | reads the secret file, is the operator | out of scope by §3 non-goal 3 — say so, do not pretend otherwise |

## 8. Open questions

1. **Does the cookie realm follow the vault or the port?** (§7 table, row 4.) Owner: whoever dispatches the multi-workspace-per-`HOME` question; blocks S4.
2. **`Sec-Fetch-Site` floor.** Which browsers are supported? If the answer includes anything below Safari 16.4, the mint endpoint needs a second signal and the design loses its cheapest rebinding defence. Owner: product/UI lead. Blocks S5.
3. **Does the CLI/harness bearer path count as a supported interface** (i.e. documented in `docs/`, with a `--token-file` flag on any launcher), or is it an internal test affordance? Owner: Main. Blocks S4's docs slice.
4. **Do the 3 peer gates and the frozen `health_routes.py` copy get deleted after S5**, or do they stay as defence in depth? `design-auth-layer.md` D1 wants the origin check to be the default, which argues for keeping them; the "one definition" work (W3-R2, `local_gate.py`) argues for deleting the route-level calls once the boundary covers them. Owner: W4 dispatcher. Blocks nothing; changes the S5 diff.
5. **Is `--allow-remote-bind` still wanted at all** once a credential exists? `design-auth-layer.md` D1 says keep it and this pass honours that, but the flag's *justification text* ("unauthenticated") is what made it acceptable. Owner: operator. Blocks the wording change only.

## 9. Evidence notes

- Inventory method and counts: §2; 85 decorators across 13 modules, `server.py:3802-3815` includes, `server.py:6149` mount, 0 `add_api_route`.
- Peer gates: `local_gate.py:37` (`LOCAL_CLIENT_HOSTS`), `:45` (`TEST_HARNESS_CLIENT_HOSTS`), `:48` (`localhost_only`); call sites `server.py:4073`, `:5910`, `health_routes.py:234`; frozen copy `health_routes.py:185-196`.
- Origin: `origin_policy.py:86` (`_origin_check_request`), `:69` (`_origin_allowed`), `:107-113` (absent allowed), `:75-77` (absent refused), 39/39 mutating coverage, commit `f00aebe9` ("all 37 non-GET routes" — the tree has 39).
- WS: `server.py:1260` decorator, `:1266-1269` origin guard, `:1247` `_reject_websocket`, `:1274-1279` `?project=` confinement, `:1285-1290` workspace pinning at handshake.
- Bind: `server.py:4036` `_validate_bind_host`, `:6177-6185` argparse (`store_true`, "INSECURE"), `:6224` call, `:6225-6232` refusal, `:6233-6240` warning.
- CORS: `server.py:1214-1220`, rationale `:1205-1211`.
- Absent mechanisms (0 hits): `Depends(`/`Security(`, `set_cookie`/`SameSite`/`HttpOnly`, `authorization`/`bearer`, any `Host` header read, `Sec-Fetch-*`.
- Frontend: `web_ui/frontend/src/apiBase.js:13-21`; `vite.config.js:13-22` (`changeOrigin: true` on `/api` only).
- Vault: `thoughtmachine/vault.py:29-39` (`vault_root`, honours `THOUGHTMACHINE_VAULT_ROOT`), `:45-53` (`VAULT_SUBDIRS`), `:76-85` (`0o700`), `:349` (`state/session_registry.json`).
- Containers: `infra/container_create.py:285` (`network_mode="none"`), `:398-411` (decision funnel).
- Prior art: `docs/design-auth-layer.md` D1–D4; `.thoughtmachine/working_docs/ticket-design-auth-layer.md` (the two ruled options — this pass answers (a) vs (b) as *both, in that order*: (b)'s peer/origin checks as the substrate, (a)'s credential as the identity); `ticket-bug-unauthenticated-routes-beyond-health.md` (R3 sweep update, "82 of the 85 route decorators"); `ticket-bug-ws-commands-unauthenticated.md` (roster; its `:1125-1127` citation predates the handshake origin guard).

## 10. Corrections

1. **The briefing's "nothing but `/health` checks anything" is wrong in the tree's favour.** 39 of 39 mutating routes call `_origin_check_request` (`origin_policy.py:86`) and the WS handshake validates `Origin` (`server.py:1266-1269`). The gap is not "no checks"; it is that those checks cannot see adversaries **C2**, **D**, **A** or **B**, and that they are maintained by hand — a commit titled "37 non-GET routes" now covers 39.
2. **"82 of 85 HTTP routes" (F17, `docs/workspace_isolation_matrix.md:340`, and the R3 sweep update) is a category error, not an arithmetic one.** 85 is the decorator count *including* `WS /ws`; HTTP is 84. Ungated HTTP is **81**, ungated surfaces are **82** (81 + `/ws`). Quoted as "82 of 85 routes" it survives; quoted as "82 of 85 *HTTP* routes" it does not.
3. **`design-auth-layer.md`'s line citations are stale at `6e01a004`** and this document re-anchors the ones it shares: `_validate_bind_host` `:3864-3894` → `:4036`; `_localhost_only` `:3897-3908` → `local_gate.py:48` (imported `server.py:227`, called `:4073`, `:5910`); CORS `:1194-1200` → `:1214-1220`; `/health` `:3913` → `:4071`; argparse `:5964-5975` → `:6177-6185`; boot `:6015-6030` → `:6225-6240`. Its "all 81 other routes and all 27 WS branches" is superseded by §2's inventory (81 ungated HTTP is right; 27 is the command count, not a route count).
4. **`ticket-bug-ws-commands-unauthenticated.md`'s finding is half-fixed, not fixed.** The handshake now refuses a foreign `Origin` (`server.py:1266-1269`) — its quoted `:1125-1127` "accepts first and asks nothing" no longer exists — but no credential is required, so adversary D and C2 still reach all 27 commands. Its own Disposition ("an authenticated identity, checked once at accept and carried into the dispatch loop, with `Origin` validation as the cheap first step") describes exactly §5's S3 slice.
5. **`_origin_allowed` (`origin_policy.py:69`) and `_origin_check_request` (`:86`) disagree about a missing `Origin`** — refuse vs allow — and both are live. Not corrected here (one is the `vault_repair` surface's, the other is the default's); named because a single enforcement point is what makes "which predicate am I behind" stop being a per-route question.
