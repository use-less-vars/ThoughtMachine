"""Authoritative harness for ``scripts/ci_assert_collected.py`` (anti-vacuity gate).

The gate asserts that pytest collected *at least* ``--expected`` tests.  These
tests pin the three load-bearing behaviours of that gate:

  1. growth      (actual > expected) -> PASSES  (the lower-bound behaviour)
  2. shrinkage   (actual < expected) -> FAILS
  3. exact match (actual == expected) -> PASSES

A full-suite collection (the 3827-test default selection) takes minutes, so the
tests NEVER run a real collection.  Instead the gate module is imported and its
pytest *collection subprocess* is stubbed with a controlled summary line, so a
millisecond replaces the multi-minute collect while still exercising the real
parse -> compare -> return-code path.

Failure is signalled by the process exit status, i.e. ``raise SystemExit(main())``
in the script.  We therefore assert on ``main()``'s integer return value, which
IS that exit status.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_assert_collected.py"


def _load_gate_module():
    """Import ``scripts/ci_assert_collected.py`` without running ``main()``."""
    spec = importlib.util.spec_from_file_location("ci_assert_collected", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gate_module():
    return _load_gate_module()


def _stub_collection(module, monkeypatch, collected):
    """Replace the gate's pytest collection with a controlled test count.

    This is the crux of the fast harness: ``main()`` calls ``subprocess.run`` to
    invoke pytest, so stubbing *that* call means no collection is ever spawned.
    """

    class _Completed:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def _fake_run(cmd, capture_output=False, text=False, **kwargs):
        # A valid pytest summary; parse_collected() reads the leading integer.
        return _Completed(f"{collected} tests collected in 0.10s\n")

    monkeypatch.setattr(module.subprocess, "run", _fake_run)


def test_growth_over_expected_passes(gate_module, monkeypatch):
    """actual > expected must PASS: growth must not fail CI."""
    _stub_collection(gate_module, monkeypatch, 4000)
    assert gate_module.main(["--expected", "3827"]) == 0


def test_shrinkage_below_expected_fails(gate_module, monkeypatch):
    """actual < expected must FAIL (non-zero exit)."""
    _stub_collection(gate_module, monkeypatch, 3600)
    exit_code = gate_module.main(["--expected", "3827"])
    assert exit_code != 0


def test_exact_match_passes(gate_module, monkeypatch):
    """actual == expected must PASS."""
    _stub_collection(gate_module, monkeypatch, 3827)
    assert gate_module.main(["--expected", "3827"]) == 0
