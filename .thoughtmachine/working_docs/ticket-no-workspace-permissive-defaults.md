# Ticket: bug/no-workspace-session-permissive-defaults-security-review

- **Title:** bug/no-workspace-session-permissive-defaults-security-review
- **Finding:** A no-workspace session resolves to `{container: True, network: True, filesystem: write}` (maximum permissive).
- **Frame:** Designed default for a valid transitional state, not a fail-open, but deserves review — is a truly id-less session reachable in production with no ceiling applied?
- **Tag:** pass-2 evidence / security review, not hygiene.
