# Ticket — fresh install on a clean machine: make it work, or make it explain itself

**Status:** open · **Type:** feat (developer experience / installer) · **Priority:** high — it blocks a new contributor on day one.
**Provenance:** **operator-reported, not independently reproduced.** This comes from a colleague's attempt to install on a clean machine. The reported symptom was not observed by the author of this ticket and the reporter's exact error text was not captured. Treat the symptom as a lead, not as a fact; everything marked *verified* below comes from reading the tree, not from the report.

---

## 1. Reported symptom

A clean clone on a machine that had never run this project failed to come up, and at least part of the failure surfaced as a missing-requirements error.

## 2. What the repository actually contains *(verified)*

Checking the report split it in half immediately:

- **The requirements files are present at the repo root** — `requirements.txt`, `requirements-dev.txt`, `requirements-rag.txt`, plus `pyproject.toml`. So "requirements are missing" is not a missing-file problem.
- **There is no `Dockerfile` anywhere in the repository** — not at the root, not within three levels. The repo looks complete and still cannot build its own executor image from what it contains.
- A comment at `.gitignore:160` names `~/.thoughtmachine/docker/resource/Dockerfile` as the location of the container definition. That is **outside the repository**, in the vault.

So if the missing-requirements error is real, its cause is one of: wrong working directory, wrong interpreter or venv, or the file never being copied into the build context — **not** an absent file. Those three produce nearly identical error text, which is exactly why this ticket is about diagnostics rather than about adding a file.

## 3. Leading root-cause candidate: the container definition lives in the vault

The repo cannot build its own image. The Dockerfile is materialised into `~/.thoughtmachine/docker/resource/` by a bootstrap step that lives outside the repo. On a machine where that step is skipped, partially run, or fails silently, the first build dies against a repo that looks complete — and the error names a path the user has never heard of.

This is the strongest hypothesis and it predicts the observed shape of the failure. It is not proven, and this ticket does not assume it.

## 4. Why this is a diagnostic ticket, not a "fix the installer" ticket

Three plausible causes (working directory, interpreter, unseeded build context) plus one strong candidate (the vault-materialised Dockerfile) produce overlapping symptoms. Writing a fix before telling them apart risks papering over the real one. So the deliverable is: **the failure becomes self-explaining**, and then the specific cause gets fixed.

## 5. Acceptance criteria

1. **Single-command bootstrap from a clean clone** on a machine with no prior state: one documented command, no manual file copying, no undocumented environment setup. Any prerequisite that genuinely cannot be automated (Docker running, network) is checked and named *before* anything else happens.
2. **Preflight, before the build.** The bootstrap verifies, in order: Python version; requirements files exist *relative to the location the installer uses, not the caller's CWD*; the vault resource directory exists and contains the Dockerfile; Docker is reachable. Every failing check produces one message naming the check, the path it looked at, and the command that fixes it.
3. **An actionable error when the vault resource directory or its Dockerfile is missing** — naming the directory, the materialise command, and the fact that the file is *expected to live outside the repository*. A bare `No such file: Dockerfile` in a repo that contains no Dockerfile is the current failure mode and is not acceptable output. The message must not require the reader to understand the container subsystem.
4. **The reported symptom is either reproduced or explicitly retired.** Either reproduce the missing-requirements error on a clean machine and record the exact command and text here, or record the attempts and close the "missing requirements" half as unreproducible. Do not leave it dangling.
5. **Regression test** extending `tests/test_rc11_fresh_machine_bootstrap.py` which asserts the preflight fails *fast and with the named message* when the vault Dockerfile is absent — the absence case, not just the happy path.
6. **No drift:** `tests/test_fresh_install_no_drift.py` stays green, or is updated with a stated reason.

## 6. Where to start — run these first, they may already be half-built

The intent behind a fresh-machine bootstrap may already be encoded in these tests:

- `tests/test_rc11_fresh_machine_bootstrap.py` — the obvious anchor. Read before designing anything.
- `tests/test_fresh_install_no_drift.py` — the drift contract.
- `tests/integration/test_first_query_fresh_vault.py` — first query against a *fresh vault*, which is close to the reported scenario.

Run them first. If one already specifies the bootstrap, this ticket is largely "make the runtime match the existing test" rather than new design — and criterion 3 becomes an assertion that the existing test does not yet make, which is the cheapest version of this work.

## 7. Explicitly out of scope

- **Vendoring the Dockerfile into the repo.** That reverses an ownership decision — the container definition belongs to the vault — and is a separate architectural call, not a DX fix.
- **Rewriting the installer.** Add preflight and honest errors; do not restructure.
- **Cross-platform path normalisation** beyond what criterion 2 requires.

## 8. Related

- No earlier ticket covers this: ~186 files match `ticket-*.md` under `.thoughtmachine/working_docs/`, and none of them concern fresh install or bootstrap diagnostics.
- Nearest neighbour, unrelated: `ticket-bug-onboarding-ssrf-ambient-credentials.md` — onboarding security, not bootstrap.
