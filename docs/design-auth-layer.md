# Design pass — the auth layer the Web UI does not have

Base: `dev = 6b02bcd1680637584fe0f50a0080ba3ea75dbe0a`. Design only; no code, no tokens, no re-finding.

## 1. The claim

The Web UI's security boundary is the **network boundary**, and the one decision this codebase must make explicitly — not a token scheme — is what an **unscoped connection** means: it is a legitimate first-class state that today silently disables every ownership comparison, so the design's single deliverable is to make "no workspace claimed yet" an explicit, typed, transition-bearing state rather than an absent field.

## 2. Qualifications — what a reader will overestimate

1. **"There are 5 CRITICAL findings" ⇒ "auth is 5 bugs away."** The red-team corpus says the opposite in its own frame: the five CRITICALs are *one missing abstraction*, and the abstraction is a decision (appliance vs authenticated), not a dependency. Every route-level fix landed before that decision is a fix whose correctness the decision may invalidate.
2. **"`--allow-remote-bind` is the hole."** It is already handled the way this design would handle it: refused by default (`web_ui/backend/server.py:6015-6023`, `_validate_bind_host` `:3864-3894`), opt-in only, announced on stderr (`:6024-6030`), and pinned off-by-default by test (`tests/integration/test_health_localhost_binding.py:275-280`). The remaining hole is not the flag — it is that **nothing else consults localness**: `_localhost_only` (`server.py:3897-3908`) is called from exactly two sites, `:3913` (`GET /health`) and `:5731` (container health). All 81 other routes and all 27 WS branches are unreachable-by-accident, not unreachable-by-construction.
3. **"`_ownership_refusal` is the enforcement point."** It has exactly **two** call sites, both inside the WS dispatch and both importing a private symbol across a module boundary (`server.py:1578-1580` apply_config branch; `server.py:3279-3281` set_project branch). There is no caller inside `web_ui/backend/bridge.py`, despite its docstring (`bridge.py:308-312`) naming `apply_config` and `set_project` as its callers. It is a *shared helper*, not an authority.

## 3. Decisions

### D1 — What "localhost only" means mechanically: **declared** (keep the hatch; it is already declared, make the declaration load-bearing)

Positions considered: **enforced** (delete `--allow-remote-bind`, bind loopback unconditionally), **declared** (keep the hatch as an explicit, logged, operator-only escape), **redefined** (the door is part of the product).

**Recommendation: declared**, because the code has already chosen it in prose and in argument structure: `server.py:33-38` states "with the flag off the only client is the operator's browser on loopback, and with it on every client that can route to the port is treated as the operator, which is false", and `:6005-6014` states why the hatch exists at all ("a blanket refusal with no exception gets deleted the first time someone needs a LAN demo"). Enforced would contradict a documented, tested decision (`tests/integration/test_health_localhost_binding.py:261-265` pins that `allow_remote=True` waives the loopback requirement) — that is a decision this design would be overruling on no new evidence.

What breaks under the other two:
- **Enforced** breaks the LAN-demo/test path the hatch was written for, and — more importantly — breaks the *honesty* of the current declaration: with the flag gone, `_localhost_only`'s two call sites still guard only health, so "enforced" would be a claim about 2 of 83 routes. Enforced also does not touch the actual residual exposure: `_localhost_only` guards two routes, and CORS — narrowed to two loopback origins at `server.py:1194-1200`, `allow_credentials=False` — is a browser-enforced control, not a server one, so a local process bypasses it entirely and a cross-origin simple request still executes server-side even where the browser withholds the response.
- **Redefined** breaks the one thing the codebase does correctly: it would legitimise the unscoped socket's current semantics (see D2) while the predicate that would have to change is the same one D3 is about — redefinition without D2/D3 done in the same wave leaves the corpus's 11 findings intact and now *intended*.

Consequence of choosing declared: the declaration must acquire a machine-readable form. The design's requirement is that **the boot-time decision is surfaced at request time** — not by adding a token, but by making the origin check that already exists (`web_ui/backend/origin_policy.py::_origin_allowed`, today called from exactly two sites in `vault_repair_routes.py` plus the WS handshake alias `websocket_origin_allowed`) the default for state-changing routes rather than the exception. That is a code-wave item; this design only names it.

### D2 — An unscoped socket is **must-bind** (a transition state, not a permanent state)

**Recommendation: must-bind.** Today unscoped is permanent and silently permissive: `_ownership_refusal` returns `None` immediately when `bound_workspace_id is None` (`bridge.py:333-336`, with the docstring at `:314` calling unscoped "nothing to violate"), the server's load gate computes `_expected_ws = _socket_ws` which is `None` (`server.py:2365-2393`), and the bridge's own guard is `_scope = self._workspace_id or workspace_id` (`bridge.py:2251`), which then admits *any* record. `new_session` repeats the same shape at `server.py:3057` (`if _ns_cur_ws and _ns_req_ws and ...` — vacuous when either side is absent). Absent currently means **unchecked**; that is the whole finding.

Where the binding step goes: not in the route layer. It goes at the two places that already have a workspace in hand and today throw the fact away —
1. **WS handshake** (`server.py:1231-1245`): `?project=` is confined to "an existing dir outside the vault" and never resolved to a workspace id. Resolution there makes the socket born bound (`bridge._workspace_id` set at construction, `server.py:2474-2477` currently does this only on the cold-load path).
2. **`bootstrap_workspace`** (`server.py:3529-3546`) and `POST /api/workspace` (`workspace_routes.py:1699`): the bootstrap that creates the workspace is the event that binds the connection.

Transition shape (three states, in the protocol, not inferred): `unbound` → `bound(workspace_id)` → never back. `unbound` is a *capability class of exactly one*: create/bootstrap and nothing else. If the wave cannot afford the full transition, the cheapest honest version is to keep `unbound` permanent but make it **explicit in the protocol** — the server emits its scope state at handshake and the guards refuse rather than pass when the state is unknown. What must not survive is the current spelling, in which `None` is indistinguishable from "no check ran".

What breaks: every unscoped call path in the test suite (see D3's list) — that is the point, and it is a floor, not a ceiling.

### D3 — Single authority: **`_ownership_refusal` wins**; `load_session`'s binding becomes a caller

Module and predicate: `web_ui/backend/bridge.py:308` `_ownership_refusal(target_path, bound_workspace_id) -> Optional[str]`, with its primitives `_resolve_workspace_id` (`:228`), `_registered_workspace_roots` (`:269`), `_normalise_workspace_root` (`:182`), `_path_is_same_or_inside` (`:289`).

Current callers: **two**, both in `web_ui/backend/server.py`, both importing the private symbol inside the branch — `:1578-1580` (apply_config / workspace switch) and `:3279-3281` (set_project). Zero callers inside `bridge.py` itself.

Current re-spellings of the same rule that must be collapsed into it: `bridge.load_session` (`bridge.py:2251-2278`: `_scope = self._workspace_id or workspace_id`, then two independent `!=` refusals), the server's load gate (`server.py:2377-2393`: `if _socket_ws and _msg_ws and _msg_ws != _socket_ws`), and `new_session` (`server.py:3057`).

Path for `load_session` to become a caller — two honest options, and the choice is forced by a shape mismatch the design must state plainly:
- `_ownership_refusal` is **path-shaped** (it resolves a path to an id and then refuses by id); `load_session` is **id-shaped** (it has a record's `workspace_id` and a bridge's `_workspace_id`, no path in hand). So either (a) `load_session` backfills the record's workspace root from the registry (`bridge.py:2276-2290` already does exactly this backfill: `registry.get_workspace(self._workspace_id)` → `entry.root_path`) and calls `_ownership_refusal(root_path, self._workspace_id)`, or (b) the predicate grows an id-shaped entry point that the path-shaped one is defined in terms of. (a) is the smaller move and uses machinery that already exists; (b) is the one that actually delivers a single authority. **Recommendation: (b) long-term, (a) as the code-wave step**, with the explicit note that (a) alone leaves two shapes and therefore two places for the rule to drift.

**Tests that pin the current behaviour and must be inverted** (floor of three; each is intent, not accident):

| Test | File | Behaviour it pins today |
|---|---|---|
| `test_unscoped_bridge_still_loads_any_workspace` | `tests/test_bridge_load_session_workspace_scope.py` | An *unscoped* bridge may load a record from any workspace — i.e. "absent means unchecked" is asserted as correct. |
| `test_new_session_without_workspace_id_cannot_rescope` | `tests/test_isolation_boundary_e2e.py` | A *bound* socket cannot be re-scoped by a client-supplied id — the half of the rule that already holds; it stays true, but its fixture asserts an unscoped socket is the valid baseline, so it inverts with D2. |
| `test_providers_crud.py` line 79 | `tests/test_providers_crud.py:79` (`assert listing[0]["api_key"] == "sk-test-key"`) | Plaintext `api_key` in `GET /api/providers` is the contract — the test asserts the leak, so any redaction is a test inversion, not an addition. |
| `test_browse_root_allowed` | tests/test_host_api_hardening.py:188 | `/api/browse` at the filesystem root is allowed — the unscoped-filesystem posture the design's "no workspace identity on HTTP" frame depends on. |
| `test_parser_allow_remote_bind_is_off_by_default` | `tests/integration/test_health_localhost_binding.py:275-280` | D1's declared position is already pinned: the hatch exists and defaults off. Not inverted by this design — it is the evidence for D1. |
| `test_validate_bind_host_allow_remote_waives_loopback_requirement` | `tests/integration/test_health_localhost_binding.py:261-265` | `allow_remote=True` waives the loopback requirement — pinned, and the reason "enforced" is a decision reversal rather than a patch. |

### D4 — What changes the day a second client arrives: **caller identity becomes the ownership predicate's first operand**

**What changes.** The rule's arity, not the rule. `_ownership_refusal_ids(target_workspace_id, bound_workspace_id)` (`bridge.py:308`) compares two workspace ids — the one a call names and the one the connection was bound to. Neither is *who called*. With one client that is exact rather than accidental: the connection and the caller are the same principal, so the binding *is* the identity and a third operand would only restate it. A second client, arriving with its own workspace provenance, its own credential and its own binding, breaks that coincidence and forces the operand the predicate has never needed: `(caller_id, target_id, bound_id)` — `caller_id` names the calling principal, `bound_id` keeps deciding what that principal may reach. No caller-identity type, field or parameter exists anywhere in `web_ui/backend` today: `caller_id` is 0 hits, and all 62 `caller` hits are prose.

**One home for the change.** D3 collapsed the rule into an ids-shaped function with two resolving adapters — `_ownership_refusal_by_id` (`bridge.py:349`, session id in) and `_ownership_refusal` (`bridge.py:372`, path in) both resolve and delegate. That is what keeps D4 small: the operand is added once, in the primitive, and both adapters inherit it. The one outcome to avoid is a partial arity change — the 7 call sites today (`server.py:2424`, `:3185`; `bridge.py:369`, `:403`, `:617`, `:2370`, `:2389`) either all pass a caller or none do, because a predicate that sometimes trusts the connection and sometimes demands a named principal is weaker than either spelling alone.

**What D1/D2/D3 leave in place.** D4 composes on top; it replaces nothing.

| Primitive | What it decides today | Under D4 |
|---|---|---|
| `_ownership_refusal_ids(target, bound)` — `bridge.py:308` | whether two ids name the same workspace, `None` failing closed | gains `caller_id` as first operand; the rule itself is unchanged |
| `_bind_workspace(id, mode)` — `bridge.py:575` | the only way a bridge acquires a scope (`bootstrap` / `switch`) | unchanged — identity changes *who may ask*, never *how a scope is acquired* |
| `_origin_check_request(request, route)` — `origin_policy.py:86` | whether the asking *browser context* is a trusted local one | unchanged — a different question, and it composes first: a credential-less cross-site call is caught here and is invisible to a caller identity |

**Positions considered.** (a) App-level `dependencies=[...]` on each `include_router` — one line per router, but it makes identity all-or-nothing per router, and several routers mix routes that need different scopes. (b) A per-route `Depends(...)` — explicit, greppable, and the same shape D1 has just taken for origin: one predicate, judged route by route, pinnable by the same AST test. (c) Middleware stamping `request.state` — invisible at the route, and it cannot refuse without duplicating the route table. (d) An explicit per-route call — no new surface, so no enforcement either. **Recommendation: (b)**, the only position that both creates the surface and leaves the judge where D1 put it.

**What a second client forces structurally.** There is no injection surface to hang identity on: `Depends(` and `Security(` appear zero times across `web_ui/backend/`, and the app-level mounts carry no `dependencies=` (`server.py:3784-3796`, of the form `app.include_router(workspace_router)`). So every route must acquire a caller-identity dependency or make the per-route call — a wave, not a doc-level change, and one that cannot begin before **credential-delivery**: a second client must be able to present a credential before anything can name it. Naming a caller is the last link of a chain, not the first.

**What it is not.** Not a token scheme, not cookies, not OAuth, not a session store. Credential issuance, transport, rotation and storage are credential-delivery's design; D4 decides only what the ownership predicate does once a caller *can* be named. Out of scope for D1—D4.

**Sequence.** credential-delivery → credential-resolution → **D4 design pass** → D4 code. This note prepares the reader for that pass; it does not authorise the wave. Until credential-delivery lands, the predicate keeps two operands, and every route keeps its connection-shaped answer to *who is asking*.

## 4. What this design does not cover

- **Tokens, sessions, cookies, OAuth.** Explicitly out: the decision in D1 is that there is no auth layer, and building one is a different brief.
- **Fixing `--allow-remote-bind`** beyond the declared position in D1.
- **Deciding the second-client question.** D4 names what changes; it does not answer it.
- **The per-route ownership gaps themselves** (F1–F11, the seven filed tickets). This design supplies the abstraction those fixes compose against; it does not re-find or re-adjudicate them.
- **`PaginateTool`'s category bypass** (corpus P-1) — a gate-stepping hole, orthogonal to identity, and fixing routes without it does not compose (corpus, "Consequence for the A findings").
- **`?project=` handshake policy** — corpus lists it as DELIBERATELY NOT FILED pending the second-client ruling; D2 names it as a binding site, nothing more.

## 5. Migration path — what this design does to the seven tickets

- **Superseded by this design (the fix shape is now a decision, not a patch):**
  - `ticket-bug-prompt-injection-unauthenticated.md` — its resolution is D1's request-time origin default; fixing `prompt_routes.py` in isolation encodes "some routes check origin" as the norm.
  - `ticket-bug-provider-apikey-read-unauthenticated.md` — redaction is safe to land now, but the *authorisation* half waits on D1; the pinned test (`:79`) must invert either way.
  - `ticket-bug-workspace-dockerfile-write-unauthenticated.md` — its own file defers the fix shape to this design.
  - `ticket-bug-worker-blueprint-prompt-write-unauthenticated.md` — same shape: ownership predicate absent, so it waits on D3.
- **Leaves for the code wave (mechanism chosen here, work not done here):** D2's transition (handshake resolution + bootstrap binding) and D3's predicate consolidation, including every test inversion in the D3 table.
- **Closes by reframing (the finding stops being a bug once the decision is made):** `ticket-bug-domain-allowlist-inert-control.md` and `ticket-bug-container-status-fail-open.md` are *fail-open defaults*, not ownership gaps — they belong to a "defaults are capabilities" wave and are not fixed by D1–D4; the design's contribution is to say so, so they stop being queued behind auth. `ticket-bug-integrity-status-docstring-drift.md` closes as documentation: the funnel already refuses (`infra/container_lifecycle.py:298-304`), pinned by `tests/test_container_lifecycle_funnel.py:374`.

## 6. Evidence notes

- `--allow-remote-bind`: `server.py:29-38` (docstring position), `:3869-3894` (`_validate_bind_host`), `:5964-5975` (argparse, `store_true`, help says INSECURE), `:6015-6030` (boot refusal + stderr warning). Tests: `tests/integration/test_health_localhost_binding.py:261`, `:270`, `:275-280`, `:283`.
- `_localhost_only` (`server.py:3897-3908`) call sites: `:3913`, `:5731`. Two, both health.
- No `Depends(` in `web_ui/backend` (0 matches, 31 files).
- `_ownership_refusal`: definition `web_ui/backend/bridge.py:308`; call sites `server.py:1578`, `server.py:3279`.
- `load_session` predicate: `web_ui/backend/bridge.py:2222` (def), `:2251-2278` (rule). Server gate: `server.py:2343-2400`, `:2474-2477`, `:2493`.
- `new_session` guard: `server.py:3055-3066`, `:3108-3110`.
- CORS: read directly, and the corpus citation is stale. `add_middleware(CORSMiddleware, allow_origins=_cors_allowed_origins(), allow_credentials=False, allow_methods=["*"], allow_headers=["*"])` sits at `server.py:1194-1200`, with the alias at `:206-209` resolving to `origin_policy.py:42-45` — two loopback origins, explicitly not a wildcard. The corpus's `:1183-1185` is `_SERVE_FRONTEND = False` and a blank line. Trust the direct read.

## Corrections

The wildcard-CORS premise was carried in two documents before it was disproved at `origin_policy.py:42-45` (narrowed to `http://localhost:<port>` and `http://127.0.0.1:<port>`, `allow_credentials=False`):
- This design doc, lines 24 and 94 — corrected above.
- The frozen brief `brief-auth-layer.md` (sha `e5e6907b…`) — carries the claim at the D1 section. **Not edited.** It is the dispatch record of what the pass was given; editing it would rewrite what the pass read.
- `design-input-auth-layer-prerequisite.md` — carries the same claim. Amended separately, with a dated correction at the top in the same pattern as the corpus U-2 correction.

The stale line citation `server.py:1183-1185` (the corpus's) is the instance of `cite a symbol, not a line` already named in `working_agreements.md` §2026-10-17. Those lines are `_SERVE_FRONTEND = False` and a blank; the actual `add_middleware` is at `:1194-1200`. Corpus citation to be amended in the same follow-up dispatch.

No wave-2 ticket depends on the false premise — none was filed CORS-shaped.
