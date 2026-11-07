"""
Pytest conftest for security tests.

Pytest inserts the ``tests/`` directory at the front of ``sys.path`` during
collection. When a test file does ``from security.security_gate import …``
Python could otherwise find ``tests/security/`` (a plain directory with no
``__init__.py``) before the real ``<project_root>/security/`` — and since
the test directory doesn't contain ``security_gate.py``, the import fails.

This conftest derives the repo root from ``__file__`` (this file is
``tests/security/conftest.py``, so the repo root is its third parent), drops
the pytest-injected ``tests/`` directory from ``sys.path``, and restores the
correct sys.path order: the sandbox-only ``/tmp/stubs`` fake agent package
first when that directory exists, then the project root.
"""

import os
import sys
from pathlib import Path

# ── 1. Derive the repo root from this file's location ─────────────
_REPO_ROOT = str(Path(__file__).resolve().parents[2])

# ── 2. Remove the pytest-injected ``tests/`` directory ─────────────
_tests_dir = os.path.join(_REPO_ROOT, "tests")
while _tests_dir in sys.path:
    sys.path.remove(_tests_dir)

# ── 3. Ensure /tmp/stubs (fake agent package) is first, if it exists ──
_stubs_path = "/tmp/stubs"
if os.path.isdir(_stubs_path):
    if _stubs_path in sys.path:
        sys.path.remove(_stubs_path)
    sys.path.insert(0, _stubs_path)
    _repo_insert_at = 1
else:
    # Not the sandbox: no stub package, so the repo root goes first.
    _repo_insert_at = 0

# ── 4. Ensure project root is present ──────────────────────────────
if _REPO_ROOT not in sys.path:
    sys.path.insert(_repo_insert_at, _REPO_ROOT)
