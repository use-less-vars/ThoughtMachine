# Ticket: hygiene/knowledge-base-get-global-kb-root-not-dead-code

**Filed:** Gen 47, 2026-10-21, from the F24 global-KB write-gate audit (this session).
**Severity:** None for the product — this is a **claim refutation**. Filed so the "zero callers / dead code, delete it" disposition does not get executed later against a wrong reading of the tree.
**Status:** CLOSED — claim refuted at this checkout; disposition is **KEEP**. Nothing to fix beyond an optional one-line comment.

## Bearing

The claim under audit: `_get_global_kb_root` in `tools/knowledge_base.py` has zero callers,
is dead code, and is safe to delete (or inline).

That is **false**. The wrapper has exactly one production caller and one test seam that
depends on its module attribute name. Deleting it breaks the `scope=global` write path and
the entire global-KB write-gate test suite. It is also a deliberate lazy-import seam, not an
accident — inlining it would re-introduce the circular import that its own comment exists to
avoid.

A "dead code" finding that is wrong in the delete direction is the expensive kind: it reads
as a free cleanup and is acted on without re-grepping.

## Evidence — refutation (verified by full-repo grep, excluding `.git`/`node_modules`/`.venv`/`__pycache__`)

### It is defined here

- `tools/knowledge_base.py:43` — `def _get_global_kb_root():`
  - `:44` — `from agent.knowledge.global_kb import get_global_kb_root`
  - `:45` — `return get_global_kb_root()`

### It has a real production caller

- `tools/knowledge_base.py:241` — `KnowledgeBaseTool.execute()` begins.
- `:250-252` — inside the `if self.scope == "global":` branch, write modes only:
  `if self.mode in write_modes: kb_root = _get_global_kb_root()`. This is a live call on the
  global-scope write path, not a reference.
- `:253-257` — read modes deliberately use `GLOBAL_KB_DIR` instead, with the rationale stated
  in the source: "so no directories or files are created on a read-only operation". The
  asymmetry is intentional; the wrapper is the write-mode seam precisely because a write is
  allowed to create the tree.

### A test depends on the module attribute name

- `tests/test_global_kb_write_gate.py:20` — the module docstring references the name.
- `tests/test_global_kb_write_gate.py:57` — `monkeypatch.setattr(kb_module,
  "_get_global_kb_root", lambda: root)`. This is the fixture's write-mode seam. The whole
  write-gate suite is hermetic *because* it patches this exact module-level attribute; renaming
  or removing it makes the suite either fail or stop isolating the filesystem.

### Why it exists (not an oversight)

- `tools/knowledge_base.py:41` — an adjacent comment explains the pattern; the sibling
  `_get_ensure_global_kb()` is defined immediately above using the identical lazy-import
  shape over `agent.knowledge.global_kb.ensure_global_kb`. Two wrappers, one convention:
  import the heavy `agent.knowledge` module inside the function body so the module-level
  import graph stays acyclic.

### Everything else that greps is a reference, not a caller

- `reports/2026-07-30_03-52-16_Architecture_Trace_Report_All_8_Questions_md.md:62` — a
  historical quote.
- `reports/2026-07-30_03-53-41_architecture-trace-report_md.md:17`, `:45` — historical.
- `.thoughtmachine/working_docs/ticket-hygiene-prompt-asserted-guards-audit.md:23` — a
  reference.
- `.thoughtmachine/working_docs/design-intent-state-divergence-recon-W4-result.md:25` — a
  reference.
- These are prose mentions. None of them is affected by the wrapper, and none of them is
  evidence of dead code — a grep that stops at "no `()` in the call position" would have
  missed `:252` only if it excluded `tools/`.

## Why it matters

- Acting on the claim deletes a live call site's target: `tools/knowledge_base.py:252` would
  raise `NameError` on every global-scope write.
- It would also silently de-hermeticise `tests/test_global_kb_write_gate.py` (the
  monkeypatch at `:57` targets a name that would no longer exist), so the write-gate coverage
  that this session's work rests on would break or need rewriting.
- Inlining it at `:252` re-introduces the module-level circular import that the comment at
  `:41` documents as the reason for the wrapper — a regression that will not show up as a
  failing test, only as an import-order crash in some other entry point.
- Precedent: "this looks unused, delete it" is the disposition that costs the most when it is
  wrong, and costs nothing to check. This ticket records the check.

## Fix sketch

**No code change required. Disposition: KEEP.**

Optional, one line: add a comment at `tools/knowledge_base.py:43` marking the wrapper as the
write-mode lazy-import seam — noting that it is monkeypatched by
`tests/test_global_kb_write_gate.py:57` and that the indirection exists to keep
`agent.knowledge.global_kb` out of the module import graph. That comment is what stops this
claim from being re-filed.

Do **not**: delete it; rename it; inline the body at `:252`; or convert it to a module-level
import.

## Verification

1. `grep -rn '_get_global_kb_root' --include='*.py' .` (excluding `.git`/`node_modules`/`.venv`/`__pycache__`)
   — expect exactly three hits: the definition `tools/knowledge_base.py:43`, the caller
   `tools/knowledge_base.py:252`, and the monkeypatch `tests/test_global_kb_write_gate.py:57`.
   Any additional or missing hit means the disposition must be re-derived, not assumed.
2. `pytest tests/test_global_kb_write_gate.py` — green, unchanged, after any comment-only edit.
3. Confirm the sibling `_get_ensure_global_kb()` still uses the same lazy-import shape
   (same pattern, immediately above `:43`) — if the convention is ever unified, both wrappers
   move together or the circular import returns for one path only.

**Not established:** whether any caller imports `tools/knowledge_base` in a way that would
let a module-level import of `agent.knowledge.global_kb` succeed safely (which would make
inlining viable). Not investigated, because the disposition is KEEP and the test seam at
`tests/test_global_kb_write_gate.py:57` forbids removal regardless.

## MUST NOT TOUCH
Same list as the container tickets.
