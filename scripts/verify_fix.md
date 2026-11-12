# The red-before gate (`scripts/verify_fix.py`)

**Defect #22.** A regression test is only worth writing if it *would have
failed before the fix*. A test that passes both at `<base>` and in the current
tree documents the current behaviour and protects nothing — it is a
tautology with a passing checkmark next to it. This gate mechanises the
check that a human reviewer would otherwise have to do by hand: run the same
selector twice, once against `<base>` (must be **RED**) and once against the
current tree (must be **GREEN**), and refuse to report success for any other
pattern.

```bash
python scripts/verify_fix.py --test tests/test_x.py::test_y --base HEAD~1
```

## What the gate proves

* The selector **fails** at `<base>` and **passes** in the current tree.
* Therefore the selector *discriminates* this change: it distinguishes the
  pre-fix source from the post-fix source. That is exactly the property that
  makes it a regression test rather than a description.
* That the run is reproducible from a clean checkout of `<base>` — the base
  run happens in a detached `git worktree` of the resolved commit, not in
  the developer's working tree, so local state cannot mask (or manufacture)
  the RED.

## What the gate does NOT prove

Be honest about the boundary; the gate is one narrow mechanical check.

* **It cannot prove the test asserts the right thing.** A tautology that
  fails at base for an unrelated reason and passes now still satisfies the
  gate. Passing this gate is a necessary condition, never a sufficient one.
* **It cannot prove the fix is correct.** A wrong fix that happens to flip
  the assertion passes. The gate proves discrimination, not correctness.
* **It cannot prove the RED was for the right reason.** `<base>` may fail
  with an `ImportError` or a collection error because the test file itself
  does not import cleanly against old code, rather than because the assertion
  under test fired. **Read the JSON**: `counts.base.errors` versus
  `counts.base.failed`, and the raw pytest output echoed to stderr,
  distinguish "the assertion failed" from "the run blew up". A base failure
  that is really an import error is a weak RED and deserves a second look.
* **It does not review the test's scope.** One trivial assertion satisfies it.
  Coverage and intent stay human judgements.

## CI invocation

```bash
python scripts/verify_fix.py \
    --test "$SELECTOR" \
    --base "${{ github.event.pull_request.base.sha }}" \
    --timeout-seconds 600 \
    > verify_fix.json
```

* stdout is **always exactly one JSON object** (schema `schema_version: 1`);
  all human-readable output — including the captured pytest output of both
  runs — goes to stderr. So `> verify_fix.json` is safe, and `| jq -e
  '.verdict == "PASS"'` works too.
* `--test` is **mandatory and has no default**. The full suite is not a
  usable selector here: it is far too slow and hangs at one node, so
  defaulting to it would turn every invocation into a timeout. The script
  refuses to run without an explicit selector (`argparse` exit 2).
* `--base` is mandatory: `HEAD~1`, a SHA, a tag — any rev `git rev-parse
  --verify <rev>^{commit}` accepts.
* `--json` is accepted for symmetry only; it changes nothing.

## Verdicts and exit codes

| Verdict   | Exit | Meaning |
|-----------|:----:|---------|
| `PASS`    | 0    | RED at base (or `absent_at_base`, see below) **and** GREEN in the current tree. |
| `FAIL`    | 1    | The pattern did not hold: already green at base (`base_not_red`), red now (`head_not_green`), nothing collected (`head_no_tests_collected` / `base_no_tests_collected`), zero passed (`zero_passed`), exit-code/verdict disagreement, or a run error. |
| `BLOCKED` | 2    | The gate could not run at all: `git-unavailable`, `repo_root_unresolved` (not inside a work tree), `bad_selector`, `base_rev_unknown`, `worktree_create_failed`, `worktree_leftover`, `gate_incomplete`, `gate_crashed`. |
| `TIMEOUT` | 3    | One of the two pytest runs exceeded `--timeout-seconds` (`base_timeout` / `head_timeout`). |

Only `PASS` is `ok` and only `PASS` exits 0. There is no "warn" verdict and
no soft-failure mode: an unparseable summary line, a missing counter, or an
exit code that contradicts the printed summary is a hard failure, because the
exit code is what CI actually trusts.

### `absent_at_base`

A test file **added** by the fix commit does not exist at `<base>`. That is a
legitimate RED by construction, so it is reported as the distinct outcome
`absent_at_base` (not as `failed`) and the current tree must still be GREEN
for `PASS`. Note the deliberate consequence: the gate runs the selector *as
it exists at `<base>`*. It never copies today's test file into the old tree —
that would measure "does my new test fail against my new source", which is
not the question.

## Timeout policy

`--timeout-seconds` (default `600`) is applied to **each** run separately, not
to the pair. A timeout is `TIMEOUT` (exit 3) and is never a pass and never a
RED: a hung run has proved nothing in either direction. Because the base run
happens in a fresh worktree with no warm caches, the base run is usually the
slower of the two — give it headroom rather than trimming the box.

## Cleanup and idempotency

The throwaway worktree lives under `tempfile.mkdtemp(prefix="verify-fix-")`
and is removed in a `finally:` block, so it is torn down on success, on
failure, on timeout and on crash. Teardown is `git worktree unlock` (only if
the plain remove failed) → `git worktree remove --force` → `shutil.rmtree`
fallback → `git worktree prune`. The caller's checkout is never mutated: no
`git stash`, no checkout, no `git reset`. `git worktree add/remove` does
write metadata under `.git/worktrees/`, which is inherent to worktrees and is
pruned in the same block.

Cleanup outranks the verdict: if a worktree survives teardown the result is
`BLOCKED` / `worktree_leftover` even when both runs looked perfect. A gate
that claims PASS and then leaves a locked tree behind has still broken the
repository. `cleanup.removed` in the JSON is the machine-readable form.

## Known limitation of this change

This ships as the **script plus the documented CI step above**, not as a live
workflow entry: the delivery environment has no network and no push access,
so a `.github/workflows/*.yml` edit could not be exercised. Wiring the step
in is a one-line addition to the existing workflow once it can be pushed.

## Tests

```bash
python -m pytest tests/test_verify_fix.py -q
```

The decision table (`parse_counts`, `classify_run`, `decide`) is pure, so it
is tested directly with hand-built outcomes — no git, no subprocesses. The
end-to-end test (`test_gate_end_to_end_red_then_green`) builds a throwaway git
repository and is skipped when `git` is not on `PATH`; it runs in CI.
