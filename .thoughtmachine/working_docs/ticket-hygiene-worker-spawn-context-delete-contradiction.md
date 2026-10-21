# Ticket: hygiene/worker-spawn-context-delete-contradiction

**Filed:** 2026-10-21, from the worker-command pass at `9d5ed869`, while reading the spawn path and then the test that claims to cover it.
**Severity:** LOW — documentation/test-fidelity debt. The shipped behaviour is one thing; the test and its docstring assert the opposite, and the test cannot fail either way.
**Status:** open; verified by read at `9d5ed869`. Deliberately **not** resolved here — one further read is named below. NOT blocking, NOT to be fixed in this wave.

## Finding

Two artefacts in the same tree state opposite rules about `context.json` at spawn.

The source says context is never deleted (`tools/workspace/worker_thread.py:4391-4395`):

```python
# Context is always preserved — context.json is never deleted on spawn.
# If context.json exists from a previous run, it is loaded in run()
# and _initial_context is merged into the loaded context rather than
# replacing it. This ensures worker conversation state persists across
# spawn/query boundaries.
```

and the merge rule is restated where it is implemented, in `run()` (`tools/workspace/worker_thread.py:2336-2339`):

```python
# ── Context loaded from disk: merge initial_context if provided ──
# When context.json exists, we preserve the loaded conversation and
# merge _initial_context into it rather than replacing it.
if self._initial_context:
```

The test asserts the reverse (`tests/test_worker_agent_transplant.py:1596-1659`, class `TestSpawnContextFixes`). Its docstring (`:1600-1603`):

```
**Fix 1 (Persistence shadowing):** When spawn provides fresh context,
any stale ``context.json`` on disk is deleted first so that
``_load_context()`` returns ``None`` and the ``_initial_context``
branch is entered in ``run()``.
```

and the body performs exactly that deletion (`:1649-1659`):

```python
# ── Act: simulate _action_spawn's Fix 1 logic ──
thread._initial_context = {"query": "hello"}
if thread._initial_context is not None:
    if ctx_path.exists():
        ctx_path.unlink(missing_ok=True)

# ── Assert: stale context.json is deleted ──
assert not ctx_path.exists(), (
    "Fix 1 FAILED: stale context.json should have been deleted "
    "when _initial_context is set"
)
```

The hygiene defect is the last piece: the test never calls `_action_spawn`. It says so (`:1649`, "simulate _action_spawn's Fix 1 logic"), constructs a `WorkerThread` directly (`:1621-1637`, `from tools.workspace.worker import WorkerThread`), re-implements the behaviour it expects inline (`:1651-1653`), and asserts on its own re-implementation. So it passes whether the product deletes `context.json`, merges it, or ignores it entirely — it is a test of the ticket author's memory of the code, not of the code.

What actually *does* delete files at spawn is a different file (`tools/workspace/worker_thread.py:4397-4403`):

```python
# ── Fix 1.1A: Clean up stale command.json before starting ──
# TODO(ticket): command.json carries no instance/id fence — ...
cmd_path = thread._worker_dir / "command.json"
if cmd_path.exists():
    cmd_path.unlink(missing_ok=True)
```

`command.json`, not `context.json` — and that block carries its own TODO ticket. The only place `context.json` is unlinked in this module is `_action_reset`'s `_drop_persisted_files` (`:5403-5411`, loop at `:5407-5409`), which is a different action with a docstring that is explicit about it (`:5393-5399`).

## Why it matters

`context.json` is the worker's entire persisted conversation (`:905`, `:2967-2968`), and the generation guard exists specifically to stop stale threads clobbering it (`:2950-2952`, `:3193-3197`). So "is it wiped on spawn?" is not a cosmetic question — it is the difference between a worker that remembers across restarts and one that does not. A future reader has two authoritative-looking artefacts pointing opposite ways, and the *test* is the one that looks authoritative (it has an assert). The likely outcome is a "fix" that either deletes a user's worker history to satisfy a test that never tested anything, or deletes the test as "stale" without recording that the merge behaviour was intentional.

It is filed LOW rather than higher because nothing here changes runtime behaviour today: it is a false green, not a false claim about a live path.

## Disposition options

- **A. Re-point the test at the product.** Have it call `_action_spawn` (or the `run()` load branch) and assert on the resulting on-disk/loaded state instead of on a local re-implementation. Catches the next flip in either direction. *This is the recommendation* regardless of which rule wins.
- **B. If merge-and-preserve is the rule** (two source comments say so, `:4391-4395` and `:2336-2339`), rewrite the test docstring and body to assert that: pre-seed `context.json`, spawn with `_initial_context`, assert the loaded conversation survives and the initial context was merged in. Deletes the "Fix 1" framing, which no longer describes anything in the code.
- **C. Leave both and add a cross-reference.** Cheapest; keeps a test that documents an approach that the source says was abandoned, with a comment explaining the divergence. Acceptable only if nobody will ever read the test as spec.

Recommendation: **A**, landed with **B** (source comments already agree with each other; the test is the outlier).

**The one further read that settles it, not performed here:** the body of `run()`'s loaded-context branch immediately after `tools/workspace/worker_thread.py:2339` (the statements under `if self._initial_context:` and the `_load_context()` call ahead of them). That read decides which artefact is wrong — merge-and-preserve (source) or replaced-on-spawn (test) — and therefore whether this closes as B or as an outright correction of the source comments.

## MUST NOT TOUCH

- `_action_reset`'s deletion semantics and its docstring (`:5393-5399`, `:5403-5411`) — that behaviour is intended, documented, and a different action.
- The `command.json` cleanup at spawn (`:4397-4403`) and its TODO — separate issue, already ticketed in place.
- The F1/F2 markers persisted in `context.json` (`:1086-1099`) and the generation guard (`:2950-2952`, `:2970-2980`, `:3193-3197`) — the `context.json` file is load-bearing for stale-thread detection, so nothing here may change its lifecycle.
- The test's `WorkerThread` construction helper (`tests/test_worker_agent_transplant.py:1621-1637`): the fixture setup is fine; it is the "simulate" step that is the problem.
