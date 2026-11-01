# ticket: feat/test-harness-production-safety

**Filed:** 2026-11-01
**Status:** **Open — hardening** (core default-deny requirement already satisfied on `dev`; see §Placement decision)
**Severity:** **Low** (defence-in-depth; no unauthenticated path to the flag was found)

## Symptom

The in-process ASGI test harness is trusted by a module-level boolean,
`TRUST_TEST_HARNESS`. A review question was raised: *can this flag ever be
true in a production process?* The concern is a "test-mode stays on in
production" class of bug — the flag disables the whole request gate, so if it
were reachable from configuration or environment it would be a one-switch
auth bypass.

## Verification (this pass, `dev` working tree)

Every claim below was re-checked by reading the file at the stated line.

| # | Check | Result |
|---|---|---|
| 1 | Default value at import | `web_ui/backend/auth_boundary.py:217` → `TRUST_TEST_HARNESS = False` |
| 2 | Read from environment anywhere? | No. `THOUGHTMACHINE_TEST_HARNESS` — repo-wide grep: **zero matches** |
| 3 | Any env-var name read into the flag | None; the assignment is a literal |
| 4 | Mutator | `:237-249` `def enable_test_harness_trust(enabled: bool = True) -> None:` (`global TRUST_TEST_HARNESS` / `TRUST_TEST_HARNESS = bool(enabled)`) — the only writer |
| 5 | Callers of the mutator (non-test) | **None.** Only `conftest.py`, `tests/integration/harness.py:316`, `web_ui/backend/tests/test_auth_boundary.py:168/172` |
| 6 | Route / CLI flag / config key that flips it | Does not exist |
| 7 | "Production mode" concept | **Does not exist.** grep `THOUGHTMACHINE_PRODUCTION\|IS_PRODUCTION\|PRODUCTION\b` over `*.py` → zero hits |
| 8 | Real mode signal | `server.py:6169` `--host` default `os.environ.get("HOST", "127.0.0.1")`; `:6172-6180` `--allow-remote-bind` ("INSECURE: accept a non-loopback `--host`/`HOST`"); `_validate_bind_host(args.host, allow_remote=args.allow_remote_bind)` at `:6219`; warning at `:6228-6230` |

Design statements on record (`auth_boundary.py:210-216`, `:90-104`, `:240-246`):

> The test suite flips this through `enable_test_harness_trust`; nothing else
> may, and nothing reads it from the environment.

> The allowance is therefore gated on `TRUST_TEST_HARNESS`, which is `False`
> at import time, is never read from the environment, and is flipped only by
> the test suite.

> Flipping it is a code change, visible in review, which is the point.

## Placement — what the flag actually skips

`auth_boundary.py:560` → `if TRUST_TEST_HARNESS and peer_host(scope) in TEST_HARNESS_CLIENT_HOSTS:`
body `await self.app(scope, receive, send); return`.

Ordering inside the gate matters, and it is favourable:

| Order | Check | Line |
|---|---|---|
| 1 | proxy-header refusal `_refuse(..., {"error": "proxied request"})` | `:549` |
| 2 | **test-harness short-circuit** | `:560` |
| 3 | host validation | `:565` |
| 4 | exemption set | `:570` |
| 5 | origin / cookie / session checks | after `:570` |

So the harness branch sits **behind** the proxy-header refusal, which is the
attack that motivated the guard: uvicorn's `ProxyHeadersMiddleware` rewrites
`scope["client"]` from `X-Forwarded-For`, i.e. `client=("testclient", 50000)`
was historically "one unauthenticated header away" (`auth_boundary.py:90-104`).
With the refusal first, an unauthenticated remote request cannot reach the
harness branch even if the flag were somehow true. Host, exemption, origin and
session-cookie checks are skipped when the flag fires — but only for peer
hosts in `local_gate.TEST_HARNESS_CLIENT_HOSTS = frozenset({"testclient"})`
(`local_gate.py:45`, used at `:58`, imported at `auth_boundary.py:137`), which
is not routable from off-host.

Test-side plumbing keeps the default honest: root `conftest.py` records the
suite value in `_suite_trust` (`:59-69`), wraps the mutator so every flip is
remembered (`:72-86`), installs a meta-path finder whose loader re-applies the
suite value after the module's real body runs (`:89-115`, `:137-141`,
`:147-148`), and a session-scoped autouse fixture sets it for the run and
restores both on teardown (`:151-169`). A pin test asserts that a freshly
imported boundary carries the suite value:
`web_ui/backend/tests/test_auth_boundary.py:782::test_a_freshly_imported_boundary_carries_the_suite_trust_value`
(`:797` true under the suite, `:812` false otherwise).

## Residual risk (what is genuinely still open)

1. **In-process flip, not enforcement.** `enable_test_harness_trust()` is a
   public module function. Any in-process code path that gains import access
   to `auth_boundary` can call it. There is no guard on *who* calls it — the
   protection is "nothing does", which is a grep-level invariant, not a
   runtime one. Nothing pins that invariant except this document and review.
2. **No production-mode assertion.** Because no production-mode concept
   exists, nothing fails loudly if the flag is true in a non-test process.
   A cheap hardening would be a process-start assertion (e.g. refuse to serve
   a non-loopback bind while `TRUST_TEST_HARNESS` is true), keyed off the
   existing `--allow-remote-bind` / `_validate_bind_host` machinery rather
   than a new "production" flag.
3. **The regression surface is the callers, not the flag.** A future caller
   added outside the test tree would be a real hole and would not trip any
   existing test.

## Placement decision (for the operator)

The original requirement was "the harness must default to deny and must not be
switchable from configuration". **That is satisfied on `dev` today** (rows 1-6
above). Two acceptable resolutions:

- **Close as already-satisfied** — record rows 1-6 as the acceptance evidence
  and drop items 1-3 above, or
- **Keep open as hardening** — adopt item 2 (a start-up assertion tying a
  non-loopback bind to a false `TRUST_TEST_HARNESS`), which is small, reuses
  the existing bind-validation path, and converts a grep-level invariant into
  a runtime one.

No code was changed under this ticket.

## Related

| Ticket / artefact | Where | Relationship |
|---|---|---|
| `ticket-ui-workspace-detail-no-scroll.md` | branch `w2/ui-workspace-panel-no-scrollbar` | unrelated UI work living on the branch of a similar name |
| Import-hook / module-reload work | branch `fix/harness-purge-modules-reload` | the `conftest.py` finder (`:95-115`, `:137-141`) that re-applies the suite value after reload |
| `w4/auth-boundary-design` | branch | design branch for the gate itself |

## UNKNOWN / not verifiable from this tree

- `ticket-feat-test-commit-policy.md` — **absent**: branch `feat/test-commit-policy`
  exists, but its `.thoughtmachine/working_docs` tree holds no ticket files
  (JSON docs only) and there is no such file on disk. Whether a ticket of that
  name ever existed cannot be established from the local tree.
- The historical commit that introduced `TRUST_TEST_HARNESS` (pre-dates the
  current line numbers; `:217` / `:560` are today's positions).
- Whether any deployment actually sets `HOST` to a non-loopback value — no
  deployment configuration lives in this repository.
