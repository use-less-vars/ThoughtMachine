# Ticket: worktree-syspath-hardcode

**Filed:** 2026-02-14, while hardening `apply_workspace_ceiling` (worktree `harden-permissions`).
**Severity:** MEDIUM — silently wrong code under test, and one observed mode is a **phantom PASS** on a security suite.
**Status:** CLOSED (STALE — guard fires, mechanism does not reproduce). Closed Gen 66, at `544bd407`, by mutation proof: no-op'ing `security/security_gate.py:apply_workspace_ceiling` at line 285 (post-mutation SHA `d0fc6939fdf0d55bb7bd97d822082b5e46bc24da4092aa4c23deb41e424976a8`) drove 389 / 395 red across `test_security_gate_disk.py`, `test_permission_hierarchy.py`, `test_ceiling_prompt_gating.py`, `test_ceiling_denial_messages.py`, including 5 of the 6 modules named as phantom-pass in this ticket. Restore verified byte-identical (SHA `1a1d25ee8a4a6b1b1e98f709f971cd8d190bf86ab6ee30bc06b79f7be6a7ea12`). Evidence tree: `.worktrees/c81-provenance` at `544bd407`. The literal-removal fix (14d8fcfc) plus `test_import_provenance.py:39` is sufficient; the `sys.modules` caching mechanism this ticket feared does not survive the guard.

## Finding

Nine test modules hard-code the **parent** repository root:

```
sys.path.insert(1, "/workspace")
```

- `tests/test_server_import_no_signal_hijack.py:27`
- `tests/test_workspace_permissions.py:17`
- `tests/test_workspace_permission_vocab_unification.py:40`
- `tests/security/test_ceiling_denial_messages.py:43`
- `tests/security/test_null_event_bus.py:35`
- `tests/security/test_ceiling_resource_denial_messages.py:45`
- `tests/security/test_security_gate.py:29`
- `tests/security/test_ceiling_prompt_gating.py:50`
- `tests/security/test_denial_layer_attribution.py:37`

(plus `tests/test_per_worker_eventbus_bridge.py:3` — `sys.path.insert(0, '/workspace/pytest_install')`, the same defect in a different spelling: `grep -rn 'sys.path.insert' tests --include='*.py' | grep /workspace` finds 108 hard-coded `/workspace` strings in `tests/`.)

In a worktree, `/workspace` is the **parent** checkout. The hard-coded entry therefore lands the parent's `security/`, `tools/` and `agent/` packages on `sys.path` while the test thinks it is importing the worktree's.

## Evidence (measured, not inferred)

Ran each module from `/workspace/.worktrees/harden-permissions` with a `pytest_sessionfinish` plugin reporting which tree `tools` / `security` / `agent` actually came from:

| module | `tools`/`agent` imported from | result |
| --- | --- | --- |
| `tests/test_server_import_no_signal_hijack.py` (no hard-coded path) | **worktree** | 1 passed |
| `tests/test_workspace_permissions.py` | **PARENT** | 3 failed, 72 passed |
| `tests/test_workspace_permission_vocab_unification.py` | **PARENT** | 1 failed, 38 passed |
| `tests/security/test_security_gate.py` | **PARENT** | 43 passed |
| `tests/security/test_ceiling_prompt_gating.py` | **PARENT** | 9 passed |
| `tests/security/test_ceiling_denial_messages.py` | **PARENT** | 14 passed |
| `tests/security/test_ceiling_resource_denial_messages.py` | **PARENT** | 7 passed |
| `tests/security/test_denial_layer_attribution.py` | **PARENT** | 8 passed |
| `tests/security/test_null_event_bus.py` | **PARENT** | 12 passed |

Two distinct harms, both real:

1. **Noise.** `test_workspace_permissions.py` + `test_workspace_permission_vocab_unification.py` fail (4 failures) purely because they exercise the parent's pre-change `security` — which is why the same suite is green under a harness that puts the worktree back at the front of `sys.path`.
2. **Phantom PASS (the dangerous half).** The six `tests/security/*` modules *pass* while importing the **parent's** `security/security_gate.py`. A security-suite pass in a worktree is therefore evidence about `dev`, not about the branch. Verified directly: with `sys.path[0] == /workspace/.worktrees/harden-permissions`, `import tools` still resolved to `/workspace/tools/__init__.py`, i.e. the parent copy had already been imported and cached in `sys.modules` before the worktree root reached the front of `sys.path` — the first import wins for the whole run.

## Fix sketch

Resolve the repo root from the test file, never from a literal:

```python
import pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parents[1]   # tests/<file>.py -> repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
```

(`parents[2]` for the `tests/security/*` modules, or better: put one `_ROOT` helper in `tests/conftest.py` — it already computes exactly this at `tests/conftest.py:131-133` — and have the nine modules drop their own line entirely.) `tests/conftest.py`'s line is already correct; the defect is only in modules that re-add a literal on top of it.

## Why it is not fixed in this PR

The ceiling/trust-boundary change touches `security/security_gate.py` and `docs/CONTRACTS.md`; rewriting nine test headers in the same diff would bury the security review in unrelated churn, and the fix changes which code four currently-failing tests exercise (their failures must be re-read, not just made green). Two of the nine (`test_workspace_permissions.py`, `test_workspace_permission_vocab_unification.py`) are now **in** the contract gate, so the defect is visible in CI output — that is the right trigger for the follow-up, not a silent ride-along.

## Smallest first step

Add the `_ROOT` helper (or a `sys.path` assertion) to `tests/conftest.py`, then delete the nine literal inserts one file at a time, re-running each file from a worktree and confirming `tools.__file__` / `security.__file__` point at the worktree. A guard test asserting `pathlib.Path(tools.__file__).is_relative_to(_ROOT)` would make the whole class of defect fail loudly.
