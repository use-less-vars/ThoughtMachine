# Ticket: hygiene/venv-upgrade-disclosure

**Filed:** 2026-10-25 (Gen 50)
**Severity:** MEDIUM — a trust-surface gap, not a bug
**Status:** open; filed, not dispatched

## Finding

The host `.venv` can be rebuilt or repaired (by the installer's self-healing path, by a doctor step, or by a manual `pip install`) with no event emitted to the event log and no delta reported by `check_system`. The user cannot tell from the machine's own reports that the venv moved.

Surfaced during the Gen 50 hook-robustness wave. A `ModuleNotFoundError: pydantic` presented as an import-order bug for five commits before the interpreter mismatch was found. The root cause was environmental, but the reason it took five commits to find is that nothing in the machine said "the venv changed."

## The smallest first step

On venv creation, upgrade, or repair: append an event to the event log naming the reason and the pip-freeze delta. On `check_system`: report the venv's `pip freeze` hash so a moved venv is visible in the health surface.

Not a fix. A disclosure. The machine currently claims "pinned versions"; the pin can move without a report.

## What this is not

Not a proposal to lock the venv. The venv legitimately moves — installs, upgrades, repairs. The gap is that it moves silently.
