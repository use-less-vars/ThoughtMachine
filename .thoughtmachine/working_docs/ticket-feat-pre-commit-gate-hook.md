# Ticket: feat/pre-commit-gate-hook

**Filed:** 2026-10-21, from the session-persistence pass at `9d5ed869` (the hygiene pass filed the gap and deferred the gate: `ticket-hygiene-venv-broken-and-pytest-unreachable.md` §4, disposition D — "a commit-time gate is a separate ticket with its own risk surface". This is that ticket; the missing install target is `ticket-security-commit-hook-gate-not-installed.md`).
**Severity:** MEDIUM — nothing here is broken; the ask is a commit-time gate that runs the security/integration checks. The budget objection raised at `9d5ed869` is settled rather than blocking: a ≤2 s gate cannot exist (one cold import of `security.security_gate` + `tools` is ~2.2 s), but a **≤3 s target with a 5 s hard ceiling** holds one `python -c` gate with slack to spare, and that is the budget specified in `## Decision A`.
**Status:** open; both open decisions are taken — the hook body and budget (`## Decision A`) and the destination (`## Destination (decided)`). No hook written yet. NOT blocking, NOT to be fixed in this wave.

## Finding

The gate has a natural home — `tools/git_info_tool.py:989-996` already pins `core.hooksPath` for container commits, and `:868-878` refuses host commits once `.githooks/pre-commit` exists — but neither path resolves that directory where a policy-owned payload could live, and the directory exists nowhere in the tree (see `ticket-security-commit-hook-gate-not-installed.md`). Both halves of that sentence are now decided: what the hook runs is `## Decision A`, and where it is installed from is `## Destination (decided)`.

What exists and can be reused as-is:

- `tests/security/test_gate_import_order.py` (184 lines, 4 tests). Each test spawns a **fresh** interpreter via `sys.executable -c`, because the property under test is import order.
- `tools/__init__.py:25` declares `TOOL_CLASSES: List[Type[ToolBase]] = []` — a list of **classes**, not instances — and `:62-67` (`register_tool`: `if cls not in TOOL_CLASSES` / `TOOL_CLASSES.append(cls)`), plus the module-level `TOOL_CLASSES.append` calls behind `try/except ImportError` (e.g. `:76-90`), append only when the class is missing. So a dropped `GitReadTool`/`GitWriteTool` registration is assertable in a single interpreter.
  - The name to assert on is the **class** attribute: `tools/base.py:93` `name: ClassVar[str] = ""`, `tools/git_info_tool.py:200` `name: ClassVar[str] = "git_read"`, `tools/git_write_tool.py:107` `name: ClassVar[str] = "git_write"`. So `{'git_read', 'git_write'} <= {c.name for c in tools.TOOL_CLASSES}` is correct **as written**. (`.tool` is the per-instance field used for logging, `tools/base.py:141+` — not what the gate should look at.)
  - Second, independent signal if wanted: `tools/__init__.py:11` `IMPORT_FAILURES: List[Dict[str, str]]`, fed by `_record_import_failure` (`:14-17`), readable via `get_import_failures()` (`:20-22`). An empty list is a stronger statement than "the two git classes are registered" — it means no tool module failed to import at all.
- `tests/integration/test_session_creation_contract.py` (9968 B) is the integration file a "Tier 2" stage would want — not used by this decision, see `## Considered and rejected`.
- Wiring points for a staged runner: `web_ui/backend/workspace_routes.py:1700` (`async def create_workspace`), `tests/integration/harness.py:296/308`.

What sets the budget: one cold import. Measured cold, in a container at `9d5ed869`:

```
$ python3 -c "import security.security_gate, tools"     # ×3
2.10s / 2.22s / 2.59s
```

That is one import of the two packages, nothing else. `tests/security/test_gate_import_order.py` pays it four times (`≈ 9 s`) because each test needs an unpolluted `sys.modules`.

Consequences, kept from the `9d5ed869` measurement and now read against the revised budget:

- A **≤2 s gate is impossible** with any stage that touches pytest or a fresh interpreter — the floor is one cold import (~2.2 s), and pytest startup is on top of it.
- A **5 s ceiling cannot hold even one** fresh-interpreter pytest run; it can hold exactly one bare `python -c "import ..."` with ~2 s of slack. That single-`python -c` shape is precisely what `## Decision A` specifies.

(Environment note: `.venv/bin/python` is not executable in the audit container, so the numbers are CPython 3 `python3` from PATH; treat them as a floor, not a promise — a warm page cache makes the third run the fastest, a cold one is worse.)

## Why it matters

The reason the hygiene pass deferred this is the same reason it is worth filing precisely: a gate whose stated budget cannot be met gets either disabled at the first slow machine or rewritten to `--no-verify` at the first emergency. Getting the numbers on the record first keeps the hook from becoming a bypass — which is why the budget below is stated as a floor *and* a ceiling, and why the one remaining bypass route is made an event rather than a flag.

It also fixes scope: this is not "run the test suite at commit time". The suite is unreachable in the broken-venv state the hygiene ticket documents; anything that shells out to pytest from a git hook inherits that breakage and turns a green commit into an opaque failure. The decision below keeps pytest out of the hook entirely.

## Decision A — hook body and budget (decided)

`resources/githooks/pre-commit` does exactly two things and nothing else:

1. **`py_compile` the staged `.py` files** (`git diff --cached --name-only --diff-filter=ACM` → `*.py` → `python -m py_compile`). A syntax error in a file nothing else imports is caught here and nowhere else.
2. **One `python -c` smoke test, one fresh interpreter:** `import security.security_gate`, `import tools`, then assert the git tools are registered — `assert {'git_read', 'git_write'} <= {c.name for c in tools.TOOL_CLASSES}` (correct as written; `## Finding` explains why it is the class attribute and not the instance field). Optionally add `assert not tools.get_import_failures()` for the broader "no tool module silently failed to import" check.

Budget:

- **Target ≤3 s, hard ceiling 5 s.** The ~2.2 s floor above is one cold import and is **not** to be optimized away — the hook's header must state it, so that nobody later "fixes" the hook by dropping the import it exists to perform. Against the 5 s ceiling this leaves ~2.8 s of slack warm, ~2.4 s cold. If the payload ever pushes past the ceiling, the answer is to remove work from the hook, not to raise the ceiling.
- Measured reference, unchanged and still the basis for the numbers: `2.10 s / 2.22 s / 2.59 s` for the bare import; `≈ 9 s` for the four fresh-interpreter tests.

Division of labour — the hook catches the **class**, CI keeps the **property**:

- The hook asserts the class of failure that is cheap to detect and disastrous to ship: the import is broken, or a tool registration was dropped. ~2.5 s, one interpreter, exit code is the gate.
- `tests/security/test_gate_import_order.py` keeps asserting the **property** (import order is correct, under four unpolluted `sys.modules`), in CI, where `≈ 9 s` is free. The hook must **not** reproduce it: four fresh interpreters blow the ceiling four times over, and a one-interpreter approximation is a weaker, silently different test wearing the same name.
- **No pytest in the hook** — for the budget reason above, and because the hook must not inherit the broken-venv breakage documented in `ticket-hygiene-venv-broken-and-pytest-unreachable.md`.

## Destination (decided)

The payload ships tracked, and the vault workspace provisions it.

- **Payload:** `resources/githooks/pre-commit`, tracked in the repo like any other resource.
- **Provisioning:** `ensure_workspace_dirs` (`thoughtmachine/workspace_capabilities.py:279`) copies the payload to `<vault>/workspaces/<id>/.githooks/pre-commit` and `chmod +x` it. The vault workspace is ours; the user's project root is not.

**The actual bug — this is the part that makes the install work.** `_resolve_registry_workspace_info` (`tools/git_info_tool.py:1324-1348`) stores `entry.root_path` — the **user's project root** — as `_resolved_workspace_path` (`:565-574`), and both execution modes build `core.hooksPath` from it (`:989-996` container, `:868-878` host). So `core.hooksPath` resolves against the user's project, not the vault workspace. **Installing the payload alone changes nothing:** the pinned path would still point into someone else's repository. The resolver must be changed to yield the vault workspace path (or to expose it as a separate, explicitly named attribute) before the payload is worth shipping. Note the knock-on: `_to_container_path` (`:1302-1312`) maps only paths **under** `_resolved_workspace_path` and raises `ValueError` otherwise (`:1308`), so a vault-workspace `.githooks` would fall straight into the `except ValueError` fallback at `:993-995` and resolve as the bare relative `".githooks"` — i.e. against the container's cwd, the project root. The resolver change must therefore also give the tool a way to map the vault directory, or that fallback silently reintroduces the bug.

How each mode pins it once the resolver is fixed:

- **Container mode:** `core.hooksPath` = the absolute **in-container** path of `<vault>/workspaces/<id>/.githooks`. Read-only is acceptable — a hook needs read and execute, not write.
- **Host mode:** same absolute vault path. This **drops** `core.hooksPath=/dev/null` (`:885`) and the `--no-verify` insertion (`:902-903`) — which is why both come off the `## MUST NOT TOUCH` list below, and why the existing argv-shape tests must be updated in the same change.

Deliberate override instead of a silent one: an explicit `--no-verify` stays possible, but must emit an event — `hook_bypass`, carrying actor and reason — so a bypass is recorded rather than invisible. Rationale: agents genuinely need a WIP exit route, and a route that is denied outright gets found anyway (patched hook, deleted file); a route that is recorded is auditable. Silence is the property being removed here, not the escape hatch. Implementation note: the event bus is typed — `global_event_bus.publish(<BaseEvent>)` (`agent/events.py:447`, `:645`) — so this needs an `EventType` member **and** an event class, not a `publish("hook_bypass", ...)` string.

**Unresolved prerequisite, not verified in this pass:** whether the resource container mounts `<vault>/workspaces/<id>` at all. The rationale comment above the container pin (`tools/git_info_tool.py:981-988`) documents only `/workspace`, and the container workspace path defaults to `/workspace` (`:516`). If the vault workspace is not mounted into the resource container, the container half of this decision needs that mount added first — the hook cannot be read from a path the container cannot see.

## Considered and rejected

- **Staged runner (`--fast` / `--full`).** Strictly more work, and only worth it if the fast stage is expected to grow. The single-stage gate above is the whole of what fits in 5 s.
- **Keep it out of git entirely (CI-only `make gate`).** Cheapest, but loses the pre-commit property and leaves `ticket-security-commit-hook-gate-not-installed.md`'s reserved directory reserved forever.
- **Shipping the hook through `resources/MANIFEST.json`.** Wrong destination: the manifest deploys to `~/.thoughtmachine/`, not to a workspace's `.githooks`, and its shape is asserted by `tests/workspace/test_vault_seeding.py:83-93`.

## MUST NOT TOUCH

- `tests/security/test_gate_import_order.py` — the 4 fresh-interpreter tests are the specification of the property and stay the CI half of the division of labour. They must not be rewritten to share an interpreter, and the hook must not reimplement them.
- `tools/__init__.py:25,62-67` — `TOOL_CLASSES` and the append-if-missing guard are what the gate asserts against. Do not turn `TOOL_CLASSES` into instances, and do not rename the class attribute `name`, to make a hook expression shorter.
- `tools/git_info_tool.py:884-899` — the host-mode hardening pins (`diff.external=`, `filter.*`, `credential.helper=`, `core.autocrlf`/`core.eol`, and the rest of the list). **Exception, decided:** the one `"-c", "core.hooksPath=/dev/null"` pair at `:885` is removed by `## Destination (decided)`. Every other pin in the block stays; removing them to make the gate "work" is not an option.
- `tools/git_info_tool.py:902-903` — the `--no-verify` insertion. **Exception, decided:** this pair is removed by `## Destination (decided)` and replaced by the `hook_bypass` event. The hardening purpose it served must not be lost silently.
- `tools/git_info_tool.py:989-996` — the container `core.hooksPath` pin **changes**: its base moves from `_resolved_workspace_path` (project root) to the vault workspace. What must survive is the property, not the current text: it stays an explicit `core.hooksPath` override carrying an **absolute, container-mapped** path, it is never a repo-local `.git/hooks`, and the `except ValueError` fallback must not become the normal path — a relative `".githooks"` resolves against the container's cwd (the project root), the wrong directory.
- `tools/git_info_tool.py:1324-1348`, `:565-574` — the resolver. Changing what it returns is the point of `## Destination (decided)`; it must not be done by making `_resolved_workspace_path` ambiguous for every other caller.
- `thoughtmachine/workspace_capabilities.py:508-520` — the allowed-item set for the workspace config dir (`:524-528` warns on anything else). `.githooks` must be added there in the same change that starts creating it, or every provision logs an unexpected-item warning.
- `tests/security/test_git_execution_mode.py:118-127`, `tests/security/test_git_container_hooks.py:118,187,209` — these assert today's argv shape (host `--no-verify` present, host `core.hooksPath=/dev/null`). They must be **updated** in the same change as `## Destination (decided)`, not deleted.
