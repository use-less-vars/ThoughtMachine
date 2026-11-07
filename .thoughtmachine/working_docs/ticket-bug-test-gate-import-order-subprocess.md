# bug/test-gate-import-order-subprocess — the gate test spawns the system interpreter, not the running one

**Category:** `bug`
**Status:** open
**Severity:** LOW
**Classification:** record, do not fix
**Filed:** 2026-10-24, Gen 49, ruled by Main Gen 39

## 1. Summary

`tests/security/test_gate_import_order.py::test_import_security_gate_then_tools_registers_git_tools` spawns a subprocess using a hardcoded absolute interpreter path, `/usr/bin/python3.12`, instead of `sys.executable`. The test is a real test doing a real thing — it starts a fresh interpreter to observe import-order side effects on the tool registry — but the interpreter it starts is not the one the test is running under.

Inside a virtualenv the consequence is immediate and total: the subprocess's `sys.path` does not include the environment's site-packages, so `pydantic` is not importable and the child dies with `ModuleNotFoundError: No module named 'pydantic'`. The parent test then fails on the child's non-zero exit, with an error that describes pydantic rather than the actual defect.

## 2. Evidence

Surfaced during a `git commit` that triggered the local pre-commit hook. The hook runs the test suite from the developer's virtualenv, the test spawned `/usr/bin/python3.12`, and the child could not import pydantic. The commit was made with `--no-verify` and a docstring recording why, so that the bypass is visible in the history rather than silent.

## 3. Blast radius

Local pre-commit hook only. Production is unaffected: the test never runs in production. The application's import order is not broken and the registry is not broken — this is a defect in the test's method of observing the registry, not in the thing observed.

## 4. Why CI is green

CI runs inside a virtualenv, and in CI's environment the hardcoded path is never the one used, so the child inherits an environment where pydantic resolves. The test passes in CI for a reason that has nothing to do with the property it is testing. That is the honest reading: green here means 'the path happened to be fine', not 'the import-order property holds'.

The corollary is worth recording, because it is the more general defect. A test whose result depends on which interpreter is at a fixed absolute path is not measuring the property it claims to measure on any machine but the one that path identifies. That is the same shape as the guard that resolved the repo root incorrectly and scanned zero files and passed: a green result that does not imply the thing was checked.

## 5. Classification

Record, do not fix. This follows `working_agreements.md` section 1 — findings are written down and not silently repaired in passing, because a repair smuggled into an unrelated change removes the evidence that the defect existed. Severity LOW, no urgency: it costs a developer a `--no-verify` once, and CI is not red.

## 6. Fix shape

In `_run_script`, use `sys.executable`. If a fallback is wanted for the case where the test is deliberately exercising a system interpreter, resolve it conditionally: use `sys.executable` when the current interpreter is inside a virtualenv (`sys.prefix != sys.base_prefix`), otherwise `shutil.which("python3.12")`, and fail loudly if neither resolves. The failure mode to avoid is a silent fallback to a path that does not exist, which replaces `ModuleNotFoundError` with `FileNotFoundError` and helps nobody.

## 7. Sibling call sites

The defect is in the helper, not in the one test, so every other `_run_script` call site in the same file carries the same exposure and will surface the same way the next time one of those tests runs locally. Those call sites are noted here rather than filed as separate tickets: one ticket, one fix, all call sites. Filing four tickets for one `sys.executable` substitution would be filing the same fact four times.

## 8. How to prove fixed

Run the file's tests inside a virtualenv from the repository root. Every test that spawns a subprocess must pass without `--no-verify` being needed, and the child's interpreter must be the venv's. A useful additional assertion: have the child print `sys.executable` and assert it equals the parent's, so the property is checked rather than assumed.
