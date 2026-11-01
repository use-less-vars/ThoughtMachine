# Ticket: the test harness can disable the auth boundary, and nothing stops it reaching production

Status: open

## Problem

Two mechanisms landed with the auth work, both of which make a green test run say less
than it appears to:

1. **A session-wide trust override.** The root `conftest.py` flips
   `auth_boundary.TRUST_TEST_HARNESS` on for the whole test session, and while it is on
   the middleware admits any caller whose peer name is in `TEST_HARNESS_CLIENT_HOSTS`
   without a credential.
2. **An inert e2e suite by default.** `tests/e2e/conftest.py` skips every `e2e`-marked
   test unless `--run-e2e` is passed, so a normal local run never exercises the boundary
   end-to-end and still goes green.

Nothing structural prevents (1) from being active in a production-shaped process: it is
process-global state, set by a function, in the same module as the gate. The gate's own
design is careful — it fails closed when the secret is missing — but the override sits
*in front* of that care.

The asymmetry is not obvious from reading either side. In this wave,
`tests/test_system_health_local_only.py` needed an explicit injected token for its
loopback-peer test, because `127.0.0.1` is **not** in `TEST_HARNESS_CLIENT_HOSTS`
(only `testclient` is) — i.e. the override is keyed on a peer *name*, and its blast
radius is a property of Starlette's `TestClient`, not of the flag's name.

## Evidence

- `web_ui/backend/auth_boundary.py:229` — `TRUST_TEST_HARNESS = False` at import;
  flipped only through `enable_test_harness_trust` (`:249`, assignment at `:260-261`).
- `web_ui/backend/auth_boundary.py:625` — the admission branch:
  `if TRUST_TEST_HARNESS and peer_host(scope) in TEST_HARNESS_CLIENT_HOSTS:` → serve
  without a credential. `auth_boundary.py:100-102` describes the same branch.
- `web_ui/backend/local_gate.py:45` — `TEST_HARNESS_CLIENT_HOSTS = frozenset({"testclient"})`,
  consumed at `local_gate.py:58` and imported into the boundary at
  `auth_boundary.py:137`.
- `conftest.py:151-175` — `_trust_the_in_process_asgi_harness`, `scope="session"`,
  `autouse=True`: no test has to ask for it, and it is on for every test in the tree.
- `tests/e2e/conftest.py:60-77` — `--run-e2e` (`:60`) and
  `pytest_collection_modifyitems` (`:69`) which marks every `e2e` test skip when the
  flag is absent. Each e2e module also `pytest.importorskip("playwright.sync_api")`
  (`tests/e2e/test_permission_persistence.py:15`,
  `test_permission_mutation_ui_persistence.py:46`,
  `test_tab_switch_permission_state.py:52`), so in an environment without Playwright
  they collect zero tests — a green suite that ran none of them.
- `tests/e2e/conftest.py:279` — the e2e backend gets a throwaway vault
  (`tempfile.mkdtemp(prefix="e2e_vault_")`), which is what makes the e2e path safe; the
  conftest override has no equivalent containment.

## Suggested fix

Make the override require *positive opt-in* rather than a default-on flip:

1. Require both the environment marker (`THOUGHTMACHINE_TEST_HARNESS=1`) **and** the
   conftest hook, so flipping the module function alone is not enough.
2. Refuse to activate when a real credential file or a real vault already exists, or when
   the workspace looks like user data (`<vault>/workers.json`, an existing
   `<vault>/state/web_ui_token`) — the e2e harness already builds an isolated vault, so
   nothing legitimate needs the override on a live one.
3. Emit a loud one-line startup warning whenever it is active, naming the peer names it
   trusts, so "auth is off in this process" is visible in a log rather than inferred.
4. Add a test proving the override cannot be enabled after the process has served a
   non-test request (i.e. it is a boot-time decision, not a runtime switch).
5. Document the flag next to the boundary's threat model in
   `docs/design-auth-boundary.md`, in the same place the exemption table is described.

For (2): do not rely on the flag's name to communicate scope — `127.0.0.1` is not in the
trusted set, and a future addition to `TEST_HARNESS_CLIENT_HOSTS` would widen a
production-visible hole without any diff in the boundary itself.

## Why it matters

The threat model this boundary was built for is a malicious page driving the operator's
own browser on loopback. A trust override that can be switched on ambiently, in the same
process, reopens exactly that hole. And because the e2e suite skips itself by default,
"all tests green" has never meant "the boundary works" — which is precisely the gap the
new path-filtered `linux-e2e-auth` job in `.github/workflows/platform-matrix.yml` exists
to close; that job is the mitigation for (2) today.

## What was not verified

Whether any production entry point actually imports `conftest.py` (it should not — but
this ticket does not prove the absence, it argues the override should not depend on that
absence).
