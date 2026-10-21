# Ticket: feat/pre-commit-gate-hook

**Filed:** 2026-10-21, from the session-persistence pass at `9d5ed869` (the hygiene pass filed the gap and deferred the gate: `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §4, disposition D — "a commit-time gate is a separate ticket with its own risk surface". This is that ticket; the missing install target is `ticket-security-commit-hook-gate-not-installed.md`).
**Severity:** MEDIUM — nothing here is broken; the ask is a commit-time gate that runs the security/integration checks. The blocking number is the *budget*: the requested ≤2 s gate cannot exist, and even a 5 s ceiling cannot hold one interpreter, so the shape of the gate has to be decided before any hook is written.
**Status:** open; budget measured at `9d5ed869`, no hook written. NOT blocking, NOT to be fixed in this wave.

## Finding

The gate has a natural home — `tools/git_info_tool.py:989-996` already pins `core.hooksPath` to `<project>/.githooks` for container commits, and `:868-878` refuses host commits once `.githooks/pre-commit` exists — but that directory does not exist (see `ticket-security-commit-hook-gate-not-installed.md`). So this ticket owns one product decision (what the hook runs) and one install decision (who writes `<project>/.githooks`).

What exists and can be reused as-is:

- `tests/security/test_gate_import_order.py` (184 lines, 4 tests). Each test spawns a **fresh** interpreter via `sys.executable -c`, because the property under test is import order.
- `tools/__init__.py:25` declares `TOOL_CLASSES: List[Type[ToolBase]] = []` and `:64-65` appends only if the class is missing — so a dropped `GitReadTool` registration is assertable in a single interpreter.
- `tests/integration/test_session_creation_contract.py` (9968 B) is the integration file a "Tier 2" stage would want.
- Wiring points for a staged runner: `web_ui/backend/workspace_routes.py:1700` (`async def create_workspace`), `tests/integration/harness.py:296/308`.

What does not fit: the requested budget. Measured cold, in a container at `9d5ed869`:

```
$ python3 -c "import security.security_gate, tools"     # ×3
2.10s / 2.22s / 2.59s
```

That is one import of the two packages, nothing else. `tests/security/test_gate_import_order.py` pays it four times (`≈ 9 s`) because each test needs an unpolluted `sys.modules`. Consequences:

- A **≤2 s gate is impossible** with any stage that touches pytest or a fresh interpreter — the floor is one cold import (~2.2 s), and pytest startup is on top of it.
- A **5 s ceiling cannot hold even one** fresh-interpreter pytest run; it can hold exactly one bare `python -c "import ..."` with ~2 s of slack.

(Environment note: `.venv/bin/python` is not executable in the audit container, so the numbers are CPython 3 `python3` from PATH; treat them as a floor, not a promise — a warm page cache makes the third run the fastest, a cold one is worse.)

## Why it matters

The reason the hygiene pass deferred this is the same reason it is worth filing precisely: a gate whose stated budget cannot be met gets either disabled at the first slow machine or rewritten to `--no-verify` at the first emergency, and `--no-verify` is exactly the flag `tools/git_info_tool.py:902-903` adds on the host path. Getting the numbers on the record first keeps the hook from becoming a bypass.

It also fixes scope: this is not "run the test suite at commit time". The suite is unreachable in the broken-venv state the hygiene ticket documents; anything that shells out to pytest from a git hook inherits that breakage and turns a green commit into an opaque failure.

## Disposition options

- **A. Single-stage gate, hard-capped.** One `python -c` that imports `security.security_gate` and `tools`, then asserts the `TOOL_CLASSES` contents (i.e. one of the four existing import-order properties, inlined). ~2.2 s cold, no pytest, exit code is the gate. *This is the recommendation* for the hook body.
- **B. Staged runner, budget honoured per stage.** `--fast` (A, ~2.2 s), `--full` (`pytest -q tests/security/test_gate_import_order.py tests/integration/test_session_creation_contract.py`, tens of seconds) — hook calls `--fast`, CI calls `--full`. Strictly more work; only worth it if the fast stage is expected to grow.
- **C. Keep it out of git entirely.** A `make gate` / CI-only target, no hook. Cheapest, but loses the pre-commit property and leaves `ticket-security-commit-hook-gate-not-installed.md`'s reserved directory reserved forever.

Recommendation: **A** for the hook body (state the measured floor in the hook's header so nobody "optimizes" it below 2.2 s), with the install decision made explicitly: the hook lands in the user's project root (`<project>/.githooks/pre-commit`) because that is what `_resolved_workspace_path` resolves to (`tools/git_info_tool.py:1324-1348`); `resources/MANIFEST.json` deploys to `~/.thoughtmachine/` and is the wrong destination for it.

## MUST NOT TOUCH

- `tests/security/test_gate_import_order.py` — the 4 fresh-interpreter tests are the specification of the property; a hook that reimplements them must not weaken them, and they must not be rewritten to share an interpreter.
- `tools/__init__.py:25,64-65` — `TOOL_CLASSES` and the append-if-missing guard are what the gate asserts against.
- `tools/git_info_tool.py:884-899`, `:902-903` — host-mode hardening and `--no-verify`. A gate must not be made to "work" on the host by removing these.
- `tools/git_info_tool.py:989-996` — the container `core.hooksPath` pin. Installing a hook must not change the pin.
