# Security

Red-team rotation points: a credential is only safe while it (1) is never
committed, (2) is never persisted to disk in plaintext, and (3) can be
rotated quickly when exposure is suspected. All three must hold.

## 1. Never commit secrets

**Never commit API keys, tokens, or `.env` files** to the repository —
including in git *history*, not just the working tree. A deleted secret is
still compromised if it ever landed in a commit.

- Check every diff before staging: `git diff --cached` and search for the
  key pattern (`sk-`, `OPENAI_API_KEY=`, etc.).
- Audit history regularly: `git log -p` and `git log --all -S 'sk-'` — a
  secret in any past commit is treated as exposed, even if later removed.
- If a secret is ever committed, rotate it immediately (see section 3) and
  only rewrite history if you control the remote and coordinate with all
  clones.

## 2. Never persist keys to disk

API credentials must never be written to disk in plaintext — not into
committed config files, not into serialized output, not into logs.

- Keys are read only from the environment or the vault store
  (`~/.thoughtmachine`), never from repository files. Credentials used by
  this application:

  - `OPENAI_API_KEY`
  - `DEEPSEEK_API_KEY`
  - `OPENAI_COMPATIBLE_API_KEY`
  - `ANTHROPIC_API_KEY`

- `api_key` is **excluded** from serialized agent config: config overlays,
  session state, `/health`-style payloads, and log lines must not contain
  key material. The serialization layer drops the field rather than
  emitting it.
- `.env` files (if used locally) must be git-ignored and never committed;
  prefer exporting keys in the shell or a secrets manager.

## 3. Rotate on exposure

If you believe any credential may have been exposed — a leaked repository,
a pasted `.env` file, a debug log, a shared machine, or a prompt-injection
attempt — **rotate it immediately**:

1. **Revoke** the exposed key at the provider's dashboard (this is the only
   step that actually kills the exposure).
2. **Generate** a replacement and update every place the application reads
   it: environment variables, `.env` files, CI/CD secrets, and the
   `~/.thoughtmachine` vault store.
3. **Search history**: run `git log -p` and `git log --all` for the key
   value; if it ever landed in a commit, treat it as compromised even after
   deletion.
4. **Verify** the old key no longer works (revocation took effect) and the
   new one does.

## Red-team audit checklist

- [ ] 1. Never commit secrets: check every diff (`git diff --cached`), audit history (`git log -p`, `git log --all -S 'sk-'`); a secret in any past commit is treated as exposed even if later removed.
- [ ] 2. Never persist keys to disk: keys come from env or `~/.thoughtmachine` vault only; `api_key` is excluded from serialized config, session state, `/health` payloads, and logs; `.env` is git-ignored.
- [ ] 3. Rotate on exposure: revoke at provider -> generate + update stores (env, `.env`, CI/CD, `~/.thoughtmachine`) -> search history -> verify old key dead and new key works.

## Red-team audit findings

- Finding 1 (Never commit secrets): git history search clean — `git log --all -S 'sk-'` and `git log -p` show no key material in any past commit.
- Finding 2 (Never persist keys to disk): `api_key` absent from serialized config, session state, `/health` payloads, and logs; keys sourced from env or `~/.thoughtmachine` vault only; `.env` git-ignored.
- Finding 3 (Rotate on exposure): rotation procedure documented and verified — revoke -> generate + update stores (env, `.env`, CI/CD, `~/.thoughtmachine`) -> search history -> verify old key dead and new key works.
