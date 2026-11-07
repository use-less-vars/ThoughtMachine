# Ticket: bug/checksystem-allowlist-fails-open-on-none

**Filed:** Gen 47, 2026-10-21, from the CheckSystem gate audit (this session).
**Severity:** HIGH — a `None` allowlist disables the path gate entirely, so every query is permitted (fail-open on a security control). Latent today; the shipped load path cannot produce `None`.
**Status:** OPEN — not dispatched. Verified latent; the claim as originally stated named the wrong method and the wrong call site (corrected below).

## Bearing

`CheckSystem` gates every query against the vault allowlist. Both the gate guard and the
gate body treat "no allowlist object" as "nothing to enforce", i.e. allow-all. The
allowlist is loaded once per `execute()` from the vault; that loader *can* return `None`.
So the whole control rests on one invariant — "the loader never returns `None`" — which is
true today only because the vault seam happens to be fail-closed. The consumer is written
as if it were fail-open. One `except` away from a full bypass.

Correction to the filed claim: the gated method named in the claim
(`_query_running_workers`, `tools/workspace/check_system.py:805`) does **not** contain the
`if self.allowlist is not None:` guard — it only scopes the process-global worker registry
to `self.session_id` (`:809-810`). The guard lives in `execute()`. The claim's *conclusion*
holds; its *location* did not.

## Evidence — verified at this checkout

### Fail-open site 1 — the gate guard in `execute()`

- `tools/workspace/check_system.py:338` — `if self.allowlist is not None:` wraps the gate
  call at `:343` (`allowed = self._check_path_allowed(check_query)`). When the loaded
  allowlist is `None`, `_check_path_allowed` is never called at all.
- `:340-342` — inside that block, the `worker/<name>` → `workers` rewrite runs.
- `:344-348` — the denial branch (`allowed` False ⇒ permission-denied response). Unreachable
  when the guard is False.

### Fail-open site 2 — the gate body's "degraded mode"

- `tools/workspace/check_system.py:268` — `def _check_path_allowed(self, query: str) -> bool:`.
- `:269-274` — docstring states verbatim: "Returns True if allowed or if vault is
  unavailable (degraded mode)."
- `:277-278` — `if self.allowlist is None: return True  # degraded mode: vault not
  available, assume allowed`.
- This is an **independent** second fail-open: hardening site 1 alone (e.g. always calling
  the gate) still allows everything, because the gate body itself returns True on `None`.

### Where the `None` can come from

- `:261` — `allowlist: Optional[list] = Field(default=None, ...)`.
- `:334` — the only assignment: `self.allowlist = self._load_allowlist_from_vault()`.
- `_load_allowlist_from_vault()` `:285-303` returns `None` on two paths:
  - `:291-292` — `if get_checksystem_allowlist is None: return None`. That name is `None`
    exactly when the module-level import at `:108-113` raised:
    `try: from thoughtmachine.vault import get_checksystem_allowlist` /
    `except ImportError: get_checksystem_allowlist = None`.
  - `:300-302` — `except Exception: ... return None`.

### Why it is latent, not exploitable today

- `thoughtmachine/vault.py:411` — `def get_checksystem_allowlist() -> list[str]` **never
  returns `None`**. It returns `[]` (hard deny) on a missing/unstat-able file (`:426-430`),
  on integrity failure (`:441-444`), on wildcard rejection (`:457-460`), and on any
  exception (`:463-466`). Mtime-cached at `:407-409` (`_checksystem_allowlist_cache`).
- Callers of that seam: `tools/workspace/check_system.py:294` and `thoughtmachine/vault.py:477`
  (`is_path_allowed`).
- Net: with the shipped code the load yields `[]` ⇒ deny-all. The only live routes to `None`
  are (a) `thoughtmachine.vault` failing to import, and (b) an unexpected exception inside
  `get_checksystem_allowlist()` itself (its own `except Exception` covers most, not all —
  e.g. a failure before the `try` body engages).

### Why the degraded branch looked survivable

- `:46` — `SAFE_QUERIES = frozenset({"version", "containers"})`.
- `:274-276` — safe queries short-circuit to True *before* the allowlist is consulted. So
  with no allowlist the tool still answers `version`/`containers`, which is exactly the
  observable behaviour that makes "degraded mode" feel reasonable and hid the defect.
  Those two queries are unaffected by any fix here.

### Blast radius / deny-by-default counterpart

- Inside `CheckSystem` the `is not None` gate pattern is exactly these two sites (`:338`,
  `:277`) plus one cosmetic read.
- `:1270` — `allowlist_count = len(self.allowlist) if self.allowlist is not None else 0`,
  surfaced at `:1290`. Cosmetic, but note: a reported `allowlist_count` of `0` cannot
  distinguish "empty allowlist = deny-all" from "load failed = allow-all". Two opposite
  states, one number.
- Deny-by-default counterpart that must not be weakened: `:280-282` (`return False` when the
  query is not in the allowlist) and vault's `[]`-on-every-failure above.
- Other `is not None` occurrences in the file (`:181`, `:637`, `:714`, `:756`, `:811`,
  `:827`, `:884`, `:936`, `:943`, `:1076`) are unrelated `None`-checks on optional
  dependencies, **not** security gates. Do not sweep them into this fix.

## Why it matters

- A security control whose failure mode is "permit everything" is one refactor away from
  being no control at all. The failure is silent: no log, no warning, and the response
  format is unchanged — the agent simply starts getting answers it should have been denied.
- The two safe queries keep working in the degraded state, so a smoke test passes and the
  defect reads as healthy.
- `allowlist_count: 0` in the tool output actively misleads an operator debugging "why is
  everything denied" versus "why is everything allowed".
- It is explicitly scheduled to ship with **F30**. Shipping it alongside anything that can
  make the load path return `None` would convert a latent defect into a live bypass.

## Fix sketch

One function, normalise at load — the narrowest change that makes both sites unreachable:

- In `_load_allowlist_from_vault()` (`:285-303`), replace both `return None` with
  `return []` (`:291-292` and `:300-302`), and change the return annotation to `list`.
  Then `self.allowlist` is never `None`, `:338`'s guard is always true, and `:277`'s
  branch is dead code. (Optional follow-up: delete `:277-278` and amend the `:269-274`
  docstring, which currently documents the fail-open as intended behaviour.)
- Acceptable alternative, one line, no signature change: change `:277-278` to `return False`.
  This closes the gate body but leaves `:338`'s guard able to skip the call entirely — so
  the load-normalisation is the preferred fix and the `return False` is the belt to its braces.
- Whichever is chosen, two tests currently **encode the fail-open as intended behaviour** and
  must be deleted or rewritten, not merely left to fail:
  - `tests/security/test_checksystem_allowlist.py:81-87` —
    `test_allowlist_none_degraded_allows_all`, docstring "When allowlist is None (vault
    unavailable), every query is allowed"; asserts True for `"anything"`, `"../../../etc/passwd"`,
    `""`, `"workers/foo"`.
  - `tests/test_check_system_safe_queries_always_allowed.py:44-48` —
    `test_no_allowlist_still_allows_safe_queries`, docstring "the degraded path is not
    replaced, only bypassed for the two safe names".
  - Keep as-is: `tests/security/test_checksystem_allowlist.py:88-93`
    (`test_empty_allowlist_blocks_all`) and the monkeypatch sites at `:262` and `:272`.
- Guard rail for the commit message / review: **this must not ship in the same change as
  anything that lets the load path return `None`** (e.g. a broader `except` in
  `_load_allowlist_from_vault`, or a loosened `get_checksystem_allowlist` contract).

## Verification

1. `grep -n 'return None' tools/workspace/check_system.py` — no hit inside
   `_load_allowlist_from_vault` (`:285-303`).
2. Unit-level: construct `CheckSystem` with the vault seam monkeypatched to raise, then
   assert an allowlisted query is permitted and a non-allowlisted query is **denied** —
   i.e. the ImportError route (`:291-292`) no longer yields allow-all.
3. Same for the exception route (`:300-302`): monkeypatch `get_checksystem_allowlist` to
   raise and assert deny for a non-allowlisted query.
4. Confirm `SAFE_QUERIES` still answers: `version` and `containers` succeed under both
   monkeypatched failures above.
5. `pytest tests/security/test_checksystem_allowlist.py tests/test_check_system_safe_queries_always_allowed.py`
   — green **after** the two fail-open tests are removed/rewritten; if either still passes
   unchanged, the fix did not land.
6. Optional: make `allowlist_count` at `:1270` distinguish empty from unavailable (e.g.
   report the loader outcome, not just the length) so the printed number at `:1290` stops
   conflating the two states.

**Not established:** whether any other tool or entry point loads the CheckSystem allowlist
through a path that can still produce `None` (this audit covered
`tools/workspace/check_system.py` and `thoughtmachine/vault.py` only); and what F30 actually
changes at the seam — if F30 widens the loader's exception handling, the guard rail above
becomes blocking rather than advisory.

## MUST NOT TOUCH
Same list as the container tickets.
