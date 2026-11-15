# Contracts — the invariants this machine guarantees

This ledger names the invariants the machine actually guarantees, each with the exact code site that
enforces it and a test that would FAIL if that enforcement were removed. It is the input to the fast
contract gate (`.github/workflows/contract-gate.yml`), which runs only the tests named here.
It does not claim the wider suite is worthless — it claims that these three invariants are the
merge-blocking guarantees: a change that breaks one of them must not reach `dev`, and a change that
breaks anything else is still caught by the main matrix.

Verdict vocabulary: **ENFORCED** — the code site enforces it and a named test fails on removal.
**PARTIAL** — enforced, but at least one part of the invariant is not pinned by the tests named here.
**GAP** — a part of the invariant has no detecting test at all.

## Contract 1: The permission boundary — a caller-supplied session id is unrepresentable

**Invariant:** No tool's model-visible JSON schema advertises `session_id`, and a `session_id` supplied
by the caller (the model hallucinating one, or an attacker injecting one into the arguments) never
reaches the tool as an authority fact.

**Enforcement site:** `tools/base.py:85-88` — `ToolBase.session_id` (with the C68 comment at
`tools/base.py:77-84` and the consumer at `tools/base.py:302-307`):

```python
    session_id: SkipJsonSchema[Optional[str]] = Field(
        default=None,
        exclude=True,
        description="Session ID injected by ToolExecutor",
    )
```

`SkipJsonSchema` removes the field from `model_json_schema()` and therefore from
`model_to_openai_tool`; the value is an authority fact injected by `ToolExecutor` (popped out of the
caller's arguments before validation), never an argument.

**Mutation-proof test:** `tests/test_worker_security.py` — `120 assert "session_id" not in properties,
sorted(properties)` (replacing `SkipJsonSchema` with plain `Optional[str]` makes the field reappear in
the schema and this assertion fail), plus `84 assert recorded["session_id"] == AUTHENTICATED, recorded`
and `92 assert recorded["session_id"] != CALLER, recorded` (injection precedence). Also
`tests/tools/test_tool_schemas.py` — `74 assert worker.session_id == "sess-injected"` and
`80 assert "session_id" not in dumped` (the `exclude=True` half).

**Verdict:** ENFORCED — both halves are asserted: the field cannot be advertised, and a caller value
cannot win.

Note: the design brief's tests/test_identity_injection.py does not exist in this tree, so no such file
is cited here; the property is covered by the two real files above.

## Contract 2: The trust boundary fails closed

**Invariant:** When the permission store cannot be read (missing, corrupt, or raising), the effective
permission profile is the deny-all shape rather than a permissive default; and every allow/deny
decision denies on an unknown category, on a value the session does not grant, and on ASK with no
interactive approver.

**Enforcement site:** `security/security_gate.py:991` — `get_effective_permissions`, whose disk-mode
exception handlers force the deny-all sentinels (`1097-1099`, and the unreadable-ceiling handler at
`1180`):

```python
            fail_closed_reason = f"{type(exc).__name__}: {exc}"
            session = _DISK_FAIL_CLOSED_SESSION
            workspace_permissions = _DISK_FAIL_CLOSED_CEILING
```

plus the allow/deny deciders `check_required_categories` (`security/security_gate.py:1600-1781`),
`check_atomic_operation` (`security/security_gate.py:1492-1527`) and `check_requires_resource`
(`security/security_gate.py:1530-1592`):

```python
        allowed = effective.get(category)

        if allowed is None:
            return False, f"Permission denied: Unknown category '{category}' required by tool."

        result = _value_satisfies(required_value, allowed)

        if result is False:
```

`_denial_layer_label` (`security/security_gate.py:610-622`) is NOT an enforcement point: it returns
denial-message text and is called only from `_denial_clause` (`security/security_gate.py:649`). It can
neither allow nor deny a call and must not be cited as the trust boundary.

**Mutation-proof test:** `tests/test_security_gate_disk.py` for the fail-closed source —
`139`, `156`, `174`, `189`, `228`, `341` and `399` each assert `eff == ALL_BANNED` for missing session
record, corrupt sidecar, missing config, corrupt config, present-config-without-`permissions`,
schema-invalid ceiling and the unknown-I/O policy; `85 assert eff["filesystem"] == "read"  # capped by
ceiling` dies if the ceiling cap is dropped; `258 assert disk == legacy` pins disk/legacy parity;
`404 assert getattr(eff, "_fail_closed_reason", "") != ""` dies if the reason is not recorded. For the
deciders, `tests/trust/test_gate_contract.py` — `601 assert ok is True, ...`, `613 assert ok2 is False,
msg2` and `614 assert "denied" in msg2.lower()` (a worker footprint narrowing write→read must deny the
write; returning `True` or a non-denial message fails).

**Verdict:** ENFORCED for `get_effective_permissions` and for `check_required_categories` as exercised
by those two files (verified in this tree: 11 passed and 43 passed respectively). `check_atomic_operation`
and `check_requires_resource` are not imported by either cited file — see Known gaps.

## Contract 3: Worker process isolation

**Invariant:** With `WORKER_RUNTIME_MODE` absent, the resolved worker runtime mode is
`DEFAULT_RUNTIME_MODE = MODE_PROCESS`, and process mode is answered by a real OS child process, not by
the calling interpreter.

**Enforcement site:** `tools/workspace/worker_runtime.py:83` (`DEFAULT_RUNTIME_MODE = MODE_PROCESS`,
resolver at `tools/workspace/worker_runtime.py:317-331`) and the substrate in
`tools/workspace/worker_runtime_process.py:486-495`:

```python
        process = context.Process(
            target=worker_runtime_main,
            args=(child_sock, self._spec),
            kwargs={"worker_factory": self._worker_factory},
            name=(
                f"worker-runtime-{self._spec['worker']}-{self._spec['instance']}"
            ),
            daemon=False,
        )
        process.start()
```

**Mutation-proof test:** `tests/test_worker_is_a_real_process.py` — `105 assert reply_pid != parent_pid`
and `111 assert child_process_exists` for the honest handle, mirrored by `119 assert reply_pid ==
parent_pid` / `124 assert not child_process_exists` so the branch cannot pick the vacuous side;
`254 assert not _owns_a_real_child_process(handle)` and `265 assert "process" in str(excinfo.value)`
reject a handle that declares `process` but answers in-process; `165 assert result.get("spawned") is
True` dies if the production spawn path stops building a handle; `153` asserts the env flag is really
absent, so the default is the thing under test.

**Verdict:** PARTIAL — the substrate law ("the mode is a fact about where the code runs") is
mutation-proven, but the test does NOT pin the literal default: `293 assert handle.mode ==
DEFAULT_RUNTIME_MODE` and `300 assert resolve_runtime_mode({}) == DEFAULT_RUNTIME_MODE` compare against
the constant itself, so flipping `DEFAULT_RUNTIME_MODE` to `MODE_THREAD` keeps every assertion in this
file green. The literal value `MODE_PROCESS` is not asserted anywhere — see Known gaps.

## Known gaps

1. **Contract 3 — the literal default is unproven (GAP).** No test in the tree asserts
   `DEFAULT_RUNTIME_MODE == "process"`. Every reference compares against the constant: in the cited
   file at `293`/`300`, and in tests/test_worker_runtime_protocol.py at `840-842`, `864`, `1007-1008`
   and tests/test_worker_runtime_seam.py at `242`. Flipping the constant to `MODE_THREAD` is a
   one-token change that the entire named set would still pass. Contract 3 is cited as PARTIAL for
   exactly this reason.
2. **Contract 2 — two of the three deciders are unproven by anything named here (GAP).**
   `tests/trust/test_gate_contract.py` imports only `check_required_categories` (its line 25); neither
   `check_atomic_operation` (`security/security_gate.py:1492-1527`) nor `check_requires_resource`
   (`security/security_gate.py:1530-1592`) is imported or called by `tests/test_security_gate_disk.py`
   or `tests/trust/test_gate_contract.py`. Their `ASK`-is-denied, unknown-resource and
   malformed-`operation_required` branches have no detecting test in this ledger.
3. **Contract 2 — `_denial_layer_label` is untested (GAP, cosmetic).** Correctly not cited as
   enforcement; it has no test at all, so a regression there is a wrong *message*, never a wrong
   decision.
4. **Contract 2 — a documented fail-OPEN asymmetry is only half covered (GAP).**
   An unknown workspace-ceiling *value* is fail-open in `apply_workspace_ceiling`
   (`security/security_gate.py:234`), while unknown *I/O* fails closed. The tests named here cover the
   I/O side (`341`, `399`) and the ceiling-value side only through the specific schema-invalid case,
   not through every value the ceiling schema might later admit.
5. **Contract 1 — coverage is split across two files, and the cited name from the brief is absent.**
   Schema advertising is asserted in `tests/test_worker_security.py`, `model_dump` exclusion in
   `tests/tools/test_tool_schemas.py`. A future refactor that moves the field to a new base class could
   drop one half without the other noticing.

## How this document stays honest

Every test path in this document is written inside backticks and is parsed by
`tests/test_contracts_ledger_parity.py`, which fails if a named path does not exist on disk, and fails
if a named path is not present in the pytest invocation of `.github/workflows/contract-gate.yml`. That
is what stops this ledger from rotting into a list of files that moved or that the gate no longer runs.
Deliberately, a path mentioned as *non-existent* (see the note under Contract 1) is written without
backticks, so the parser cannot mistake a negative mention for a citation.
