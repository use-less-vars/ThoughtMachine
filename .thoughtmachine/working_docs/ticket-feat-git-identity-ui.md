# feat/git-identity-ui

`git_write_tool.py` reads `git_identity: {name, email}` from workspace
`config.json` and pins it as `-c user.name/email` on every commit. Fix shipped
in #139. **There is no way to set it from the UI.**

Verified 2026-10-20: Windows user pulled the new build, git fix mechanism works
(explicit refusal: "Cannot commit: no git identity. Set git_identity in the
workspace config..."), but no UI field exists. Only path is hand-editing
`~/.thoughtmachine/workspaces/<ws>/config.json`.

**Amended Gen 54, 2026-10-28.** The backend half has since shipped; the ask below is
narrowed to the editing surface, and one line of the original copy was wrong.

## What changed since this ticket was filed

The two-field ask stands, but "no surface anywhere" no longer holds. Commit `4534b24e`
(`fix/workspace-creation-root-git-identity`) closed the creation half:

- `ensure_workspace_dirs()` (`thoughtmachine/workspace_capabilities.py`) now seeds every
  newly created workspace with `root` and a **blank** `git_identity: {"name": "", "email": ""}`,
  written only when `config.json` does not yet exist, so it is idempotent and never
  clobbers an existing value. Every creation path gets it, not only the HTTP one.
- `POST /api/workspace` (`web_ui/backend/workspace_routes.py`) accepts an optional
  `git_identity` nested in the request's `settings` object, and validates it through
  `_validated_git_identity()` **before** `register_by_root`, so malformed input returns
  422 with no half-created workspace. It writes `root` and always writes `git_identity`.
- `tools/git_write_tool.py`'s `_workspace_git_identity()` resolves each label three ways
  **by key membership, never by truthiness**: absent -> not configured; present
  non-string -> refuses with ``Cannot commit: workspace `git_identity.<label>` must be a
  string.``; present blank string -> not configured. Blank is inert, which is what makes
  the seed harmless.
- The key is a declared config key (`agent/config/schema_manifest.json`, `type: dict`,
  `required: false`) and is listed in `_ENGINE_READ_KEYS` in `thoughtmachine/vault_repair.py`,
  so vault repair will not remove it as unknown.

A *fresh* workspace therefore carries a blank pair and commits through git's own
repo-local / global / auto-detected identity. The original copy's claim — "If unset, the
agent cannot commit — it will refuse with an explicit error" — is **false** after D3 and
must not be reused anywhere: blank means "not configured", not "broken".

## What is still missing (the actual ask)

- **No PUT surface.** `WorkspacePermissionsBody` carries only `permissions` and
  `allow_host_resources`; `PUT /api/workspace/{ws_id}/permissions` cannot set or clear the
  pair. Creation is the only write path, so an existing workspace can be changed only by
  hand-editing the vault.
- **No UI.** Zero references to `git_identity` under `web_ui/frontend`.
- **No GET round-trip** for the pair on the payload the detail page already consumes, so a
  form could not display the current value even if one existed.

## Where the work lands

- `WorkspaceDetailPage.jsx` — two text fields ("Git user.name" / "Git user.email") in the
  existing *Permissions & Resources* tab, which is already the workspace-config editor.
- `WorkspacePermissionsBody` — add an optional, individually blankable
  `git_identity: {name?: str, email?: str}`. Reuse the validation contract from
  `_validated_git_identity()` rather than writing a second, divergent one.
- The PUT handler — merge the pair into `config.json` through the existing atomic writer,
  leaving every other key untouched.
- The GET path — return the stored pair so the form can populate.

Field copy must match post-D3 semantics: *"Used when this workspace commits. Leave blank
to use git's own configured identity."* — **not** "leave blank and the agent cannot
commit".

## Acceptance criteria

1. Setting name and email in the UI persists both under `git_identity`, and the page shows
   them after reload.
2. Clearing both fields returns the workspace to "not configured": no identity pinned,
   git's normal resolution applies, and a commit is **not** refused merely because the pair
   is blank (given a repo-local or global identity exists).
3. A workspace carrying the D3-seeded blank pair renders as two empty fields — not as an
   error, a validation warning, or a dirty form.
4. A value containing angle brackets (`<`, `>`) or a line break is rejected with a visible
   error — 422 from the API plus an inline field error in the UI — and is never silently
   stored, dropped, or truncated. The API's verdict must match what the commit path
   enforces; rejecting at the API is strictly better than storing a value the writer will
   refuse later.
5. Malformed input (non-object pair, non-string member) returns 422 through the existing
   error envelope, and no partial write reaches `config.json`.
6. The fields appear only on the workspace surface, never on anything implying a
   *session*-level identity — the commit writer reads the pair per workspace.

**Filed by:** Gen 46, 2026-10-20, from a Windows user support cycle. Amended Gen 54,
2026-10-28, to record the D3 backend contract and retire the pre-D3 refusal copy.
