# Ticket: bug/macos-tmp-path-tests-assume-symlink-free

**Filed:** Gen 47, after wave-4 PR 4 (#162) merged; `dev` = `d60ffc92`.
**Verified at:** CI run 39640002594, PR 3's "macOS - full test suite (advisory)" job — five failures, all in `tests/integration/test_apply_config_coverage.py`. Not reproducible on Linux: the same file is green in the sandbox. The failing set is macOS-specific by construction.
**Source:** operator relay of the PR 3 CI log. Corroborated by the PR 4 run on `5e5c8f11`, which shows the same five failures — that is what dates the defect to PR 2 (`0d9a7764`) and clears both PR 3 and PR 4 of introducing it.
**Severity:** MEDIUM — no security consequence. Matters because a green-on-Linux / red-on-macOS split is how a team learns to stop reading macOS (see `ticket-hygiene-macos-advisory-hides-real-failures.md`).
**Status:** filed, not dispatched. Fix shape is written below; the fix is a test-level normalization of about five assertion lines.

## Defect

On macOS `/tmp` is a symlink to `/private/tmp`. PR 2 (`0d9a7764`) added realpath ingress via `_confine_workspace_path`, so `workspace_path` in the emitted `config_changed` event is the **resolved** form `/private/tmp/...`. `tests/integration/test_apply_config_coverage.py` asserts the **as-entered** form `/tmp/...`. Five assertions compare those two strings and fail on every macOS run.

The production behaviour is correct and must not change: resolving before confinement is what stops `/tmp/../<vault>` and symlink-swap tricks from smuggling a mount source outside the workspace. The test asserts string identity where it means **directory identity**.

## Why this is a behaviour change, not a flake

Before PR 2 nothing resolved the path, so as-entered and emitted agreed on every platform. After PR 2 they diverge exactly where the filesystem has a symlinked temp directory. macOS is the only platform in the matrix with that property, which is why the failure reads as machine-specific rather than code-specific. It is deterministic on macOS and not timing-dependent — distinct from `test_soft_timeout_warning_emitted_once`, and conflating the two is the subject of `ticket-hygiene-macos-advisory-hides-real-failures.md`.

## Fix shape

Test-level only; production code does not move.

```
assert cfg["workspace_path"] == "/tmp/x"
# becomes
assert Path(cfg["workspace_path"]).resolve() == Path("/tmp/x").resolve()
```

Comparing resolved forms asserts what the test means — same directory — and keeps passing on Linux, where resolve is a no-op on an already-resolved path. Apply to all five assertions and **name them in the PR body**; a count hides which one was missed.

Out of scope, filed as a separate design note rather than actioned here: should `config_changed` display the path as the user entered it instead of the resolved form? A user who typed `/tmp/x` and reads back `/private/tmp/x` may not recognise their own workspace. That is a display question, not this defect.

## Bearing

MEDIUM. No security consequence — the strict behaviour is the secure one. It is recorded because it is the concrete failure that `ticket-hygiene-macos-advisory-hides-real-failures.md` shows was absorbed by the word "advisory" across three merges.
