# Ticket: `_canon_path("root")` returns the empty string, which collides with the locate-root label

Status: open

## Problem

In `thoughtmachine/vault_repair.py`, `_canon_path("root") == ""`. That empty string is also the
label the locate-root path uses, so `_get_node(doc, "")` raises `KeyError("empty path")`. The
engine-read guard now short-circuits that call for the engine-read keys, but the ambiguity itself
remains: any *non*-engine-read key named `root` still walks into the same collision and can be
reported as a removal candidate or crash the traversal.

Second, related defect: `_is_engine_read_target` matches on the **last path segment**. A nested key
that merely ends in `root` (e.g. `something.root`) is therefore treated as engine-read and
over-blocked — it can never be auto-removed even though it is not an engine-read key.

## Evidence

- `_canon_path("root") == ""` — the canonicalisation strips the segment and returns the empty
  path.
- `_get_node(doc, "")` raises `KeyError("empty path")`.
- `_ENGINE_READ_KEYS = frozenset({_GAW_KEY, "root", "git_identity"})` — `root` is a real top-level
  key, so the collision is reachable.
- `_is_engine_read_target` compares the last path segment, so `a.b.root` matches.

## Suggested fix

Canonicalise the root key to a non-empty sentinel (or special-case it before `_canon_path`), so a
key named `root` cannot produce the empty path and `_get_node` is never called with `""`. Change
`_is_engine_read_target` to compare the *full* path (top-level key only) rather than the last
segment, so nested keys ending in `root` are not over-blocked. Add a test for a nested
`…root` key that is still removable, and one for a top-level `root` that is never removable.
