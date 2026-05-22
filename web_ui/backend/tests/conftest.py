"""
Minimal test configuration for web_ui backend tests.
"""
import pytest
import tempfile
import shutil
from pathlib import Path
from session.store import FileSystemSessionStore


@pytest.fixture
def temp_session_dir():
    """Create a temporary directory for session storage and clean up after test."""
    tmpdir = tempfile.mkdtemp(prefix="test_sessions_")
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def session_store(temp_session_dir):
    """Return a FileSystemSessionStore pointing at the temp directory."""
    return FileSystemSessionStore(sessions_dir=str(temp_session_dir), enable_session_history_pruning=False)
