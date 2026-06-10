"""Pytest conftest for tests/docker/ — sets up sys.path and stubs."""

import sys

# Remove pytest-injected test dirs from sys.path to avoid shadowing real packages
for _dir in ("/workspace/tests/docker", "/workspace/tests"):
    while _dir in sys.path:
        sys.path.remove(_dir)

# Ensure /tmp/stubs (fake agent package) is first
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
sys.path.insert(0, _stubs_path)

# Ensure project root is present
if "/workspace" not in sys.path:
    sys.path.insert(1, "/workspace")
