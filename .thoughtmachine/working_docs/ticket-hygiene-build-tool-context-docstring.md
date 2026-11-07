# Ticket: hygiene/build-tool-context-docstring

**Filed:** Gen 47, 2026-10-21 at 124477b7 (verified at 124477b7)
**Source:** gen-47 pen-test run — finding relayed by the operator (F12 remainder, after the wider claim was refuted).
**Severity:** low — docstring precision only, no behavioural defect.
**Status:** filed, not dispatched.
**Report cross-ref:** There is no report on this filesystem containing F11–F22. The finding was relayed by the operator from the gen-47 pen-test run; the cited report is not on this disk, so no report line numbers are citable.

## Defect

`tools/container_control.py:807` describes the build context as containing "ONLY that Dockerfile". It contains two files — the Dockerfile and the vault workspace-dir `requirements.txt` — staged into a tempdir, passed as the Docker context, with a hard fail on anything else (`infra/container_manager.py:4268, 4298, 4300, 4310`; `infra/container_manager.py:4172-4173` states it correctly).

The tool-surface docstring is the one people read.

## Refuted claim

The wider claim relayed to me — that the real build context includes siblings from the vault root, so `COPY requirements.txt .` resolves there and a `COPY .` would ship `.bak` files — does **not** hold at `124477b7`. Staging hard-fails on any file other than `Dockerfile` and `requirements.txt`: `infra/container_manager.py:4300` (`if not staged <= {"Dockerfile", "requirements.txt"}: raise RuntimeError("Unexpected files staged in build context: ...")`). The Docker context passed to the build is the private tempdir (`:4310`), never the vault directory. Recorded here so the refuted reading is not re-filed from the same relay.

## Fix shape (not written)

Correct the docstring to name both files and state that the context is a private tempdir, not the vault directory. No behavioural change.

## Bearing

This is the "words that span subsystems" pattern: a docstring that under-describes a security-relevant surface invites a wrong inference, and here the wrong inference was actually drawn — the tool text says one file, the manager docstring says two, and the gap produced a false exfiltration claim that cost a verification round to refute. The fix is one sentence; the cost of not fixing it recurs every time someone audits the build path.
