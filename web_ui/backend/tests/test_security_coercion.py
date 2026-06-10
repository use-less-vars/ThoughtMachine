"""
Tests for session permission coercion and file locking.

Covers:
- coerce_session_permissions() — valid, invalid, missing, non-dict inputs
- FileLock — acquire, release, timeout, context-manager, re-entrance
- Atomic save integration — lock acquired during save/load
"""
import os
import json
import time
import threading
import tempfile
import pytest
from pathlib import Path

from thoughtmachine.security import (
    coerce_session_permissions,
    PERMISSION_SCHEMA,
    SAFE_DEFAULTS,
)
from session.lock import FileLock, FileLockTimeoutError


# ══════════════════════════════════════════════════════════════════════════════
# Tests: coerce_session_permissions
# ══════════════════════════════════════════════════════════════════════════════

class TestCoerceSessionPermissions:
    """Tests for coerce_session_permissions()."""

    def test_valid_permissions_pass_through(self):
        """All valid values should pass through unchanged."""
        raw = {
            "network": "write",
            "filesystem": "read",
            "container": True,
            "execution": "banned",
            "git": "read",
            "system": "ask",
        }
        result = coerce_session_permissions(raw)
        assert result == raw, f"Expected pass-through, got {result}"

    def test_invalid_value_replaced_with_default(self):
        """An invalid value for a known key should be replaced by the safe default."""
        raw = {
            "network": "superadmin",   # invalid
            "filesystem": "full",       # not in VALID_PERMISSION_LEVELS
            "container": 42,            # not True/False
        }
        result = coerce_session_permissions(raw)
        assert result["network"] == SAFE_DEFAULTS["network"]  # "banned"
        assert result["filesystem"] == SAFE_DEFAULTS["filesystem"]  # "read"
        assert result["container"] == SAFE_DEFAULTS["container"]  # False

    def test_unknown_keys_are_omitted(self):
        """Keys not in PERMISSION_SCHEMA should be dropped."""
        raw = {
            "network": "write",
            "filesystem": "read",
            "invalid_extra": "something",
        }
        result = coerce_session_permissions(raw)
        assert "invalid_extra" not in result
        assert result["network"] == "write"

    def test_missing_key_gets_default(self):
        """A key that is in the schema but missing from raw should get the default."""
        raw = {"network": "write"}  # missing all others
        result = coerce_session_permissions(raw)
        for key, default in SAFE_DEFAULTS.items():
            if key != "network":
                assert result[key] == default, f"Expected {key}={default}, got {result[key]}"
        assert result["network"] == "write"

    def test_non_dict_input_returns_defaults(self):
        """If raw_perms is not a dict, return a copy of defaults."""
        for bad in (None, "string", 42, ["list"]):
            result = coerce_session_permissions(bad)
            assert result == SAFE_DEFAULTS, f"Expected defaults for {type(bad)}, got {result}"

    def test_empty_dict_returns_all_defaults(self):
        """An empty dict should produce a dict of all defaults."""
        result = coerce_session_permissions({})
        assert result == SAFE_DEFAULTS

    def test_custom_schema_and_defaults(self):
        """Custom schema/defaults parameters should be respected."""
        custom_schema = {"level": ("low", "high")}
        custom_defaults = {"level": "low"}
        result = coerce_session_permissions(
            {"level": "invalid"},
            schema=custom_schema,
            defaults=custom_defaults,
        )
        assert result["level"] == "low"

        result = coerce_session_permissions(
            {"level": "high"},
            schema=custom_schema,
            defaults=custom_defaults,
        )
        assert result["level"] == "high"

    def test_boolean_container_coercion(self):
        """container=True/False should pass through, invalid becomes default."""
        assert coerce_session_permissions({"container": True})["container"] is True
        assert coerce_session_permissions({"container": False})["container"] is False
        # "true" string is NOT in (True, False) — gets default
        result = coerce_session_permissions({"container": "true"})
        assert result["container"] is False  # default

    def test_none_value_gets_default(self):
        """None for a schema key should be replaced by the default."""
        result = coerce_session_permissions({"network": None, "filesystem": None})
        assert result["network"] == SAFE_DEFAULTS["network"]
        assert result["filesystem"] == SAFE_DEFAULTS["filesystem"]


# ══════════════════════════════════════════════════════════════════════════════
# Tests: FileLock
# ══════════════════════════════════════════════════════════════════════════════

class TestFileLock:
    """Tests for FileLock context manager."""

    def test_acquire_and_release(self, tmp_path: Path):
        """Lock can be acquired and released."""
        target = tmp_path / "test.json"
        lock = FileLock(str(target))
        assert lock.is_locked is False
        lock.acquire()
        assert lock.is_locked is True
        lock.release()
        assert lock.is_locked is False
        # Lock file should be cleaned up
        assert not lock._lock_path.exists()

    def test_context_manager(self, tmp_path: Path):
        """FileLock works as a context manager."""
        target = tmp_path / "ctx.json"
        with FileLock(str(target)) as lock:
            assert lock.is_locked is True
        assert lock.is_locked is False

    def test_exclusive_locking(self, tmp_path: Path):
        """Two locks on the same path should be exclusive."""
        target = tmp_path / "exclusive.json"
        lock1 = FileLock(str(target), timeout=0.5)
        lock2 = FileLock(str(target), timeout=0.5)

        lock1.acquire()
        assert lock1.is_locked is True
        with pytest.raises(FileLockTimeoutError):
            lock2.acquire()
        lock1.release()
        # Now lock2 should succeed
        lock2.acquire()
        assert lock2.is_locked is True
        lock2.release()

    def test_timeout_raises(self, tmp_path: Path):
        """Acquiring a held lock beyond timeout raises FileLockTimeoutError."""
        target = tmp_path / "timeout.json"
        lock1 = FileLock(str(target), timeout=0.3)
        lock2 = FileLock(str(target), timeout=0.3)

        lock1.acquire()
        with pytest.raises(FileLockTimeoutError):
            lock2.acquire()
        lock1.release()

    def test_reentrant_acquire(self, tmp_path: Path):
        """Acquiring the same lock instance twice is a no-op (re-entrant)."""
        target = tmp_path / "reentrant.json"
        lock = FileLock(str(target), timeout=1.0)
        lock.acquire()
        lock.acquire()  # second acquire should be a no-op
        assert lock.is_locked is True
        lock.release()
        assert lock.is_locked is False

    def test_double_release_no_error(self, tmp_path: Path):
        """Releasing an already-released lock should not raise."""
        target = tmp_path / "double_release.json"
        lock = FileLock(str(target))
        lock.acquire()
        lock.release()
        lock.release()  # should be a no-op
        assert lock.is_locked is False

    def test_lock_file_cleaned_up(self, tmp_path: Path):
        """The .lock file should be removed after release."""
        target = tmp_path / "cleanup.json"
        lock_path = target.with_suffix(target.suffix + ".lock")
        lock = FileLock(str(target))
        lock.acquire()
        assert lock_path.exists()
        lock.release()
        assert not lock_path.exists()

    def test_concurrent_threads_exclusive(self, tmp_path: Path):
        """Two threads should not hold the same lock simultaneously."""
        target = tmp_path / "concurrent.json"
        results = []
        errors = []

        def worker(lock_obj):
            try:
                with lock_obj:
                    results.append(threading.current_thread().name)
                    time.sleep(0.05)  # hold lock briefly
            except Exception as e:
                errors.append(str(e))

        lock = FileLock(str(target), timeout=2.0)
        t1 = threading.Thread(target=worker, args=(lock,), name="t1")
        t2 = threading.Thread(target=worker, args=(lock,), name="t2")
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # Both should have succeeded sequentially (no timeout)
        assert len(results) == 2, f"Expected 2 results, got {results}"
        assert len(errors) == 0, f"Unexpected errors: {errors}"

    def test_separate_lock_instances_exclusive(self, tmp_path: Path):
        """Two separate FileLock instances on the same path should block."""
        target = tmp_path / "separate.json"
        lock_a = FileLock(str(target), timeout=0.5)
        lock_b = FileLock(str(target), timeout=0.5)

        lock_a.acquire()
        with pytest.raises(FileLockTimeoutError):
            lock_b.acquire()
        lock_a.release()
        # Now lock_b can acquire
        lock_b.acquire()
        assert lock_b.is_locked is True
        lock_b.release()

    def test_custom_timeout(self):
        """A custom timeout is respected."""
        lock = FileLock("/nonexistent/forced_timeout.json", timeout=0.01)
        assert lock._timeout == 0.01
        # No real file needed — just check the attribute


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Integration — atomic save with locking
# ══════════════════════════════════════════════════════════════════════════════

class TestAtomicSaveWithLock:
    """Verify that FileSystemSessionStore uses locking correctly."""

    def test_save_creates_and_removes_lock(self, session_store, temp_session_dir):
        """After save_session() completes, the .lock file should be gone."""
        from session.models import Session
        session = Session(
            session_id="lock-test-001",
            metadata={"name": "Lock Test Session"},
        )
        session_store.save_session(session)

        # Check that no .lock files remain in the sessions dir
        lock_files = list(Path(temp_session_dir).glob("*.lock"))
        assert len(lock_files) == 0, f"Lock files left behind: {lock_files}"

    def test_load_with_locking(self, session_store, temp_session_dir):
        """Loading a saved session should not produce leftover lock files."""
        from session.models import Session
        session = Session(
            session_id="lock-test-002",
            metadata={"name": "Lock Test Load"},
        )
        session_store.save_session(session)
        loaded = session_store.load_session("lock-test-002")
        assert loaded is not None
        assert loaded.metadata.get("name") == "Lock Test Load"

        # No .lock files should remain
        lock_files = list(Path(temp_session_dir).glob("*.lock"))
        assert len(lock_files) == 0, f"Lock files left behind: {lock_files}"
