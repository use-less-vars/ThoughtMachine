"""
Pytest conftest for security tests.

Pytest inserts the ``tests/`` directory at the front of ``sys.path`` during
collection. When a test file does ``from security.security_gate import …``
Python finds ``tests/security/`` (which has ``__init__.py``) before the real
``<project_root>/security/`` — and since the test directory doesn't contain
``security_gate.py``, the import fails.

This conftest restores the correct sys.path order: stubs first (for the
sandbox agent package), then the project root.
"""

import sys

# ── 1. Remove the pytest-injected ``tests/`` directory ───────────────────
_tests_dir = "/workspace/tests"
while _tests_dir in sys.path:
    sys.path.remove(_tests_dir)

# ── 2. Ensure /tmp/stubs (fake agent package) is first ───────────────────
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
sys.path.insert(0, _stubs_path)

# ── 3. Ensure project root is present ────────────────────────────────────
if "/workspace" not in sys.path:
    sys.path.insert(1, "/workspace")
