# Ticket: docs/worker-lifecycle-citation-drift

**Filed:** 2026-11-02, from the W8 P3-S0 backlog pass, while auditing every `<path>.py:<line>` citation in `docs/design-worker-lifecycle.md` against the tree.
**Severity:** LOW — the doc is not wrong in bulk; three citations in its load-bearing state-model paragraph point at lines that do not contain the quoted code, plus one off-by-one range.
**Verified at:** HEAD `73b1d325` — the full 316-citation audit below was run at that SHA. Not exercised at runtime.
**Status:** open, report-only. NOT blocking, NOT to be fixed in this wave.

## Finding

**The bulk-drift hypothesis is REFUTED — record this so nobody repeats the audit.** The file is
1,590 lines (`wc -l`) and carries **316** `<path>.py:<line>` or `<path>.py:<lo>-<hi>` citations. A
mechanical resolver pass (every cited path, plus basename fallback against the tree) produced:

| Check | Result |
|---|---|
| citations found | 316 |
| resolve to exactly one existing file | **316** |
| unresolved or ambiguous | **0** |
| out-of-range (cited line > file length) | **1** |

The single out-of-range citation is
`tools/workspace/worker_execution.py:427-459` against a 458-line file — an off-by-one on the closing
bound, not a moved target.

A sample of the most-cited lines was then read for content, and these are **correct**:

| Citation | Actual line |
|---|---|
| `agent/config/defaults.py:61` | `WORKER_TIMEOUT_SECONDS = 600` |
| `agent/config/defaults.py:64` | `WORKER_MAX_RETRIES = 3` |
| `tools/base.py:312` | `def _truncate_output(self, output: str, limit: Optional[int] = None) -> str:` |
| `agent/core/ir.py:57` | `class ImageBlock:` |
| `tools/workspace/worker_manager.py:107` | `class PeriodicStaleCheck(threading.Thread):` |
| `agent/core/state.py:231` | `self.restriction_reason = 'timeout'` |

**What survives is narrow and concentrated.** The three wrong citations sit in the §3 state-model
paragraph — the passage the rest of the document reasons from.

Doc line 105, describing the detach primitive:

```
  with status written as `tools/workspace/worker_thread.py:2402` — `self.status = "paused"`
```

Line 2402 of `tools/workspace/worker_thread.py` is `self._containers_cleaned = True`. The statement
the doc quotes is at **`:2363`**.

Doc lines 151-152, in the state-name paragraph:

```
  `detaching` / `detached`; a new terminal `killed` is added. `paused` (`:1915`,
  `:2402`, `:2615`) is retained as the internal *mechanism* name and is not exposed as
```

None of the three closes over a `paused` assignment. `:1915` is `"worker_name": self.worker_name,`;
`:2615` is blank; `:2402` is the `_containers_cleaned` assignment above. The real `paused` sites are
`:2363`, `:2855` and `:3097` — the latter two being
`self.status = "detached" if self._detach_requested else "paused"`.

For the repair, the complete `self.status = ` census in `tools/workspace/worker_thread.py` at
`73b1d325` is:

```
1511 detaching      1537 detached       1557 killed         1730 ready
2021 detached       2080 ready          2363 paused         2696 ready
2855 paused|detached                    2910 ready          3024 busy
3097 paused|detached                    3184 ready          3240 ready
3373 error          3413 final_status   3522 data.get("status", "ready")
```

Note the shape the second and third drifts share: the doc writes a `paused` assignment, but two of the
three real sites are *conditional* (`"detached" if self._detach_requested else "paused"`). A corrected
citation must not repeat the doc's implication that `paused` is written unconditionally at those lines.

## Why it matters

The drift is small in count and large in position. §3 is the paragraph that defines `detaching`,
`detached`, `killed` and `paused` and states which of them is externally visible; doc line 153
concludes "the externally visible resting state after a Stop is `detached`". A reader who follows the
citations to check that claim lands on a container-cleanup flag, a dict literal and a blank line.

Three of three wrong in one paragraph is also the pattern most likely to recur: the citation style
(`:NNNN` with no requoted line) invites renumbering drift, while the style used elsewhere in the same
file (citation followed by the quoted source line — as at `:100-104`) does not. The two stylistic
halves of the document are the explanation for why most of it is accurate and this paragraph is not.

## Disposition options

- **A. Repair the four citations and leave the audit trail.** Change doc `:105` to `:2363`, doc `:151-152`
  to `:2363, :2855, :3097`, and the off-by-one to `worker_execution.py:427-458`; add one sentence to
  §3 noting that `paused` is written conditionally at `:2855` / `:3097`. *This is the recommendation* —
  four-line diff, no restructuring, and it fixes exactly the lines a reader is most likely to follow.
- **B. Convert §3 to the quoted-line citation style used at `:100-104`.** Every citation in the paragraph
  carries the source line it points at, so the next renumber is self-detecting. Larger diff but it
  removes the recurrence mechanism rather than this instance.
- **C. Add a mechanical citation check** (resolve every `<path>.py:<line>`; fail if the file is missing
  or the line is beyond EOF). Catches option-A-style rot in the 316-citation population — but note it
  would **not** have caught any of these three, since all three are in-range. Only the quoted-line
  style catches semantic drift. Worth doing, but as the cheap half of B, not instead of it.

Recommendation: **A** now, and record the 316/0/1 audit numbers in the doc or the ticket so the next
reader does not re-run the bulk pass looking for a problem that is not there.

## Acceptance

- Doc `:105`, `:151-152` and the `worker_execution.py` range each resolve to a line that still contains
  what the doc claims.
- The §3 statement that `paused` is written at those lines does not contradict the conditional form at
  `:2855` / `:3097`.
- No citation in the file resolves out-of-range.
- The ticket (or the doc) continues to record that the bulk audit found 316 citations with 0 unresolved
  and 1 range error — so "citation drift in worker-lifecycle.md" is not re-filed as a large problem.

## MUST NOT TOUCH

- The §3 state *model* itself — `detaching` / `detached` / `killed` / `paused`-as-mechanism and the
  "externally visible resting state after a Stop is `detached`" conclusion. This ticket corrects where
  the doc points, not what it decides.
- `docs/design-worker-lifecycle.md` §5 and later: their citations passed the audit. A fix here must not
  renumber or rewrite them.
- The quoted-line citation style at `:100-104` — it is the model answer and the reason option B exists.
