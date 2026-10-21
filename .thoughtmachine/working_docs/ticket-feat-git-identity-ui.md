# feat/git-identity-ui

`git_write_tool.py` reads `git_identity: {name, email}` from workspace
`config.json` and pins it as `-c user.name/email` on every commit. Fix shipped
in #139. **There is no way to set it from the UI.**

Verified 2026-10-20: Windows user pulled the new build, git fix mechanism works
(explicit refusal: "Cannot commit: no git identity. Set git_identity in the
workspace config..."), but no UI field exists. Only path is hand-editing
`~/.thoughtmachine/workspaces/<ws>/config.json`.

**Fix:** Workspace detail page, two text fields ("Git user.name" / "Git
user.email") under a "Git" section. Save writes `git_identity` into workspace
config. Copy: *"Used when this workspace commits. If unset, the agent cannot
commit — it will refuse with an explicit error."*

**Filed by:** Gen 46, 2026-10-20, from Windows user support cycle.
