# bug/smoke-test-config-never-collected

**Filed:** 2026-10-22
**Severity:** LOW
**Status:** filed

## Finding

`tests/smoke_test_config.py` is a smoke test for the config pipeline
(load → resolve → validate → save against a temp `providers.json`) and a
silent coverage gap: its filename matches neither pytest default glob
(`test_*.py`, `*_test.py`), so pytest never collects it — and nothing else
invokes it either. The module's checks have never run anywhere.

The module is a **script, not a pytest module**: no `test_*` function, no
fixture, no `Test*` class — only `class SmokeResult` (line 35),
`def run_smoke_test()` (line 60) and `if __name__ == "__main__":` (line
333). Its own docstring says so (line 7): *"It is meant to be run
standalone (not via pytest)."*

## Evidence

```
$ python3 -m pytest tests/smoke_test_config.py --collect-only -q
no tests collected in 0.10s                     # exit status 5

$ python3 -m pytest --collect-only -q tests/ | grep -c smoke_test_config
0

$ python3 -m pytest --collect-only -q | tail -3
5306 tests collected in 3.93s

$ grep -rn smoke_test_config .github/ || echo NONE
NONE
```

Mechanism — `pyproject.toml` sets `testpaths` but **no `python_files` key**:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "web_ui/backend/tests"]
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "integration: marks tests as integration tests",
    "docker: marks tests that require a real Docker daemon",
    "e2e: marks Playwright end-to-end browser tests (run with '--run-e2e')",
]
```

so pytest's default `python_files = test_*.py *_test.py` applies and the
file is filtered out before collection. No import, script, `Makefile`
target or workflow references it: the only mentions in the tree are
`docs/testing/test_inventory.md:39` (which already records `0` tests for
it), `docs/xray/backend_runtime.md:293`, two `reports/*.md`, and
`.thoughtmachine/working_docs/guard-b-suite-gate.md:6`, which lists it
under "Exclusions everywhere". So the non-collection is *known* — but
recorded in prose, not in any config, and it means the checks are lost.

## Why it matters

21 named checks in `run_smoke_test()` have never executed:

- `Create providers.json`, `List profiles`, `Active profile loaded`
- `resolve_config:` × 5 — model_override wins, user model preserved,
  default model fallback, provider fields overwritten, provider switch
  clears stale values
- `validate_config:` × 2 — valid config returns `AgentConfig`, invalid
  config returns `None`
- `deep_merge:` × 2 — nested merge preserves unmentioned keys, `None`
  overlay removes key
- `save_config: file written`, `load_config:` × 3 — returns `AgentConfig`,
  model preserved, `session_permissions` preserved
- `resolve_from_profile:` × 2 — model preserved, model_override wins
- `providers.json: valid JSON and has profiles`

CI does **not** call it by path: the four workflows under
`.github/workflows/` (cross-platform-smoke, first_start_ci,
platform-matrix, python-version-matrix) run `python -m pytest -m "not
docker and not e2e"` (plus one explicit e2e module and a Windows thin
slice), none of which can reach a file outside the collection globs.
Concretely: the checks have never run in any run, local or CI — the config
pipeline has no smoke coverage at all, only the unit tests under
`tests/test_config_*.py`.

## Proposed fix (recommended: (i), wrapper flavour)

Two options:

1. **Make it collectable and recommended.** A bare rename to
   `tests/test_smoke_test_config.py` is *not* enough — the module defines
   no `test_*` callables, so pytest would still collect 0 items and exit 5.
   Keep the script as-is (its `__main__` / exit-code contract stays
   usable by hand) and add a thin collectable file:

   ```python
   # tests/test_smoke_test_config.py
   from tests.smoke_test_config import run_smoke_test

   def test_config_pipeline_smoke():
       assert run_smoke_test() is True
   ```

   Recommended because the checks already exist, need no rewrite, are
   hermetic (`HOME` is repointed at a `tempfile.mkdtemp()` and the keys
   are fakes like `sk-smoke-test-key`, so no network or real vault), and
   this is the only option that actually executes them in CI.

2. **Declare the exclusion deliberately.** If the file is meant to stay a
   hand-run script forever, make that mechanical and documented rather
   than accidental — e.g. a `collect_ignore` entry or a comment on
   `testpaths` in `pyproject.toml` plus a line in
   `docs/testing/test_inventory.md`. This costs nothing but leaves the 21
   checks permanently unrun; acceptable only as an explicit decision.

## Disposition

This ticket records the gap and the fix shape only. It does not rename,
wrap, exclude or execute anything, and it changes no config, workflow or
test file.
