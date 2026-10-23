# Ticket: bug/workspace-id-unvalidated-at-path-construction

**Filed:** 2026-10-23, from the vault-ownership review of workspace-id handling.
**Category:** bug
**Severity:** medium (conditional) - see below
**Status:** filed.

## Summary

`workspace_id` is minted by the server, but the value is treated as a trusted
filesystem path component with no validation anywhere between minting and disk.
The single function that turns an id into a path, `_workspace_dir`, joins it raw.
`RESERVED_WORKSPACE_IDS` exists for exactly this class of problem and is consulted
at exactly one place - the registry writer - so creation paths that bypass the
registry (`ensure_workspace_dirs`, 26 production call sites) can materialise
`<vault>/workspaces/global/` next to the real workspace tree on a typo alone.

## Severity

MEDIUM, conditional. Not remotely exploitable today: the id is server-minted and
`WorkspaceCreateBody` has no field for it, so the browser cannot choose it. Any
in-process caller can supply one, including by accident. This becomes HIGH the
moment a route accepts a `workspace_id` in a request body and that value reaches
`_workspace_dir` (or `ensure_workspace_dirs`), because at that point
user-controlled input arrives at path construction with no segment validation.

## Evidence

### The id is minted, not accepted

- `thoughtmachine/workspace_registry.py:370` - `ws_id = generate_human_id()`
- `:372-373` - dedupe loop: `while self.get_workspace(ws_id) is not None:` /
  `ws_id = generate_human_id()`
- `:346` - `def register_by_root(` calls into `:306` - `def register_workspace(`
- `:342` - `raw[workspace_id] = entry.to_dict()`, the sole registry write

`web_ui/backend/workspace_routes.py:1580-1585`:

```python
class WorkspaceCreateBody(BaseModel):
    """Body for POST /api/workspace (create workspace with a purpose preset)."""

    path: str
    purpose: Optional[str] = "general"
    settings: Optional[Dict[str, Any]] = {}
```

No `workspace_id` field. Verified.

### It becomes a path with no validation

`thoughtmachine/workspace_capabilities.py:176-178`:

```python
def _workspace_dir(workspace_id: str) -> Path:
    """Return the filesystem path for a given workspace ID."""
    return _user_dir() / "workspaces" / workspace_id
```

No segment check, no reserved-id check, no length check.

`thoughtmachine/workspace_registry.py:44-48` defines the guard that should have
been used here:

```python
#: Workspace ids the vault claims for its own namespaces.  ``global`` is the
#: vault-wide scope (``<vault_root>/credentials/global/``,
#: ``<vault_root>/global/``): a workspace with that id would resolve the global
#: store as if it were its own scope.  Same shape as ``HEAD`` in git.
RESERVED_WORKSPACE_IDS: frozenset = frozenset({"global"})
```

It has exactly one call site - `:319` `if workspace_id in RESERVED_WORKSPACE_IDS:`
- and 4 occurrences tree-wide (2 in `workspace_registry.py`, 2 in
`tests/test_credential_store_reconciliation.py`). It is enforced on registry
*write* only, which is precisely the path that `ensure_workspace_dirs` bypasses.

`thoughtmachine/workspace_capabilities.py:279` - `def ensure_workspace_dirs(workspace_id: str) -> List[str]:`
does not consult it either, and it does not merely fail on a bad id - it builds a
full workspace-shaped tree:

- `:312-313` `if not base.exists():` / `base.mkdir(parents=True, exist_ok=True)`
- `:317-322` seeds `capabilities.json` (`WorkspaceCapabilities.default()`)
- `:326` writes a `Dockerfile` from `resources/default_dockerfile.txt`

No workspace-id regex exists anywhere: `workspace_routes.py`,
`workspace_registry.py` and `workspace_capabilities.py` contain no
`re.match` / `re.compile` / `re.fullmatch` on an id.

### Call sites

Every one of the 24 listed sites was checked line by line against source. All are
correct, and the list is a **subset**: the production total is 26 (71 including
tests). Two are missing from it - `web_ui/backend/workspace_routes.py:983` and
`setup_workspace.py:11`.

- `web_ui/backend/workspace_routes.py` - 365, 379, 394, 444, 794, 819, 912, 922,
  951, **983**, 1226, 1297, 1339, 1447, 1622, 1663 all `ensure_workspace_dirs(ws_id)`;
  1739 `ensure_workspace_dirs(entry.id)`
- `web_ui/backend/server.py` - 836, 1655, 3129, 3331 `ensure_workspace_dirs(entry.id)`;
  3140, 3538 `ensure_workspace_dirs(workspace_id)`
- `web_ui/backend/session_routes.py:141` - `ensure_workspace_dirs(entry.id)`
- `setup_workspace.py:94` - `ensure_workspace_dirs(human_id)`; also **:11**

Note the three shapes: `ws_id` (route-local), `entry.id` (registry-sourced) and
`workspace_id` / `human_id` (parameter). Only the second is even incidentally
constrained, and only because the registry validated it on write.

### Precedent for the fix (reuse, not a new regex)

`web_ui/backend/global_routes.py:339`:

```python
def _validate_credential_name(name: str) -> Optional[str]:
    """Return an error message for an invalid credential name, else None."""
    if not name:
        return "credential name must not be empty"
    if name == "." or ".." in name or "/" in name or "\\" in name:
        return "credential name must be a single path segment"
    if len(name) > _MAX_CREDENTIAL_NAME_LEN:
        return (
            f"credential name must be at most {_MAX_CREDENTIAL_NAME_LEN} characters"
        )
    return None
```

`_MAX_CREDENTIAL_NAME_LEN = 128` at `:336`. This is the same problem (a name
becoming a path segment under a vault directory) already solved in the credential
store; the fix here should call it or share it, not restate the rules.

### Drift found while verifying

- **`docs/vault_ownership_manifest.md` is not in this working tree.** It was
  committed as `f4e7909b` on branch `docs/gen48-vault-ownership-manifest`
  (pending merge), so a checkout based on `9ef9cfd0` does not contain it. Its
  §2.3 call-site list was therefore re-verified directly against source rather
  than accepted as documentation, and §3 is amended on that branch. Supporting
  artifacts on `dev`: `tests/test_backend_truth_vault_ownership.py` and
  `reports/2026-08-30_15-21-22_Backend_Truth___Vault_Ownership___Phase_1_2_Detail.md`.
- **`security/client-claim-surface-auth-model` is not a filename in this working
  tree.** The client-claim material that does exist: `redteam-client-claim-brief.md`
  and `ticket-security-workspace-vault-isolation.md`.

## Why it has not been exploited

Three reasons, of which only the first is load-bearing:

1. the id never comes from the client - `WorkspaceCreateBody` has no such field,
   so no request can set it;
2. the ids that do reach `_workspace_dir` today come from `generate_human_id()` or
   from `entry.id` read back out of the registry, both of which are minted;
3. traversal requires an in-process caller, which already holds the privilege the
   traversal would gain.

So the present bug is not "attacker walks the vault" but "a typo, or a future
route, silently creates `<vault>/workspaces/global/` beside the real workspace
tree (or escapes the workspaces directory entirely)". That is cheap to hit and
hard to notice; it is not remotely reachable.

## Fix shape

One choke point, no scatter:

- **Validate at `_workspace_dir`** (`workspace_capabilities.py:176-178`). It is the
  only function that converts id to path, so all 26 call sites and every future
  one inherit the check for free. Validating at each route guarantees the 27th
  caller forgets.
- **Reuse `_validate_credential_name`** (`global_routes.py:339`), or lift it into a
  shared helper (e.g. `thoughtmachine/path_segments.py`) that both call. Do not
  add a second regex: two implementations of "one safe path segment" will drift,
  and the weaker one will decide.
- **Refuse reserved ids at the choke point too.** `RESERVED_WORKSPACE_IDS`
  (`workspace_registry.py:48`) is currently enforced only on registry write
  (`:319`); the bypass exists because creation paths never consult it.
- **Fail closed.** Raise (e.g. `ValueError`) rather than substituting a sanitized
  value: a silently cleaned id addresses a *different* workspace than the caller
  asked for, which is worse than an error.
- Reject, at minimum: empty, `.`, `..`, anything containing `/`, `\` or NUL, ids
  longer than 128 characters, and any member of `RESERVED_WORKSPACE_IDS`.

## Acceptance criteria

- A test asserts the **choke point** (`_workspace_dir`, not a route) refuses
  `"global"`, `".."`, `"../x"`, `"a/b"`, `"a\\b"`, `""`, `"."` and a 129-character
  id - each raises, and none leaves a directory behind.
- A test asserts legitimate ids still resolve: a fresh `generate_human_id()` and an
  id read from a registered workspace both give `<vault>/workspaces/<id>`.
- A test asserts `ensure_workspace_dirs("global")` cannot create
  `<vault>/workspaces/global/` - i.e. the creation path is covered, not just the
  path builder.
- The check exists in exactly one function; a repo-level assertion that no
  route-side workspace-id validator has appeared (the fix did not get duplicated).
- If a route ever gains a `workspace_id` body field, it ships with a test that a
  traversal id is rejected. That field is the trigger for this ticket's severity
  moving to HIGH, so it must not be landable without the check.

## Related

- **Amends / blocks:** the scope claims in `docs/vault_ownership_manifest.md` §3
  (branch `docs/gen48-vault-ownership-manifest`, commit `f4e7909b`) and in the
  client-claim surface auth-model work. Those claims rest on "workspace scope is
  trustworthy by construction"; this ticket shows it is trustworthy by
  convention. Recorded here as a blocker/amendment to that work, not as a
  separate finding.
- Same defect class: `RESERVED_WORKSPACE_IDS` enforced on one path (registry write)
  and not the other (directory creation). Trusted by convention, not by code.
- Model to copy: `_validate_credential_name` (`web_ui/backend/global_routes.py:339`).
