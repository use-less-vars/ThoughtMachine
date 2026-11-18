"""``agent.core.tool_executor`` must import even without ``fast_json_repair``.

``fast_json_repair`` is an optional convenience parser used to repair malformed
model JSON.  A CI leg where the wheel failed to install therefore must not fail
at *import* time: the guarded import in ``tool_executor`` degrades
``repair_loads`` to ``json.loads``.

These tests exercise the fallback without leaving broken module state behind:
the absence is simulated by putting ``None`` in ``sys.modules`` (which makes
``from fast_json_repair import ...`` raise ImportError) and every patch is
undone in a ``finally``.
"""

import importlib
import importlib.util
import json
import sys

import pytest

import agent.core.tool_executor as tool_executor

_MISSING = object()


def _load_fresh_module(name):
    """Load an independent instance of tool_executor.py under *name*.

    A fresh module object is used where possible so the shared, already-imported
    module is never mutated by a test.
    """
    spec = importlib.util.spec_from_file_location(name, tool_executor.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _absent_fast_json_repair:
    """Simulate the package being uninstalled for the duration of a block."""

    def __enter__(self):
        self._saved = sys.modules.get("fast_json_repair", _MISSING)
        # ``None`` in sys.modules makes the import machinery raise ImportError,
        # which is exactly what a missing wheel looks like to the guarded import.
        sys.modules["fast_json_repair"] = None
        return self

    def __exit__(self, *exc_info):
        if self._saved is _MISSING:
            sys.modules.pop("fast_json_repair", None)
        else:
            sys.modules["fast_json_repair"] = self._saved
        return False


def test_repair_loads_is_callable_and_parses_valid_json():
    """The normal path still exposes a working ``repair_loads``."""
    assert callable(tool_executor.repair_loads)
    assert tool_executor.repair_loads('{"a": 1}') == {"a": 1}
    assert tool_executor.repair_loads('{"a": [1, 2], "b": null}') == {
        "a": [1, 2],
        "b": None,
    }


def test_module_imports_and_falls_back_when_package_is_absent():
    """A fresh module instance imports with no package and parses plain JSON."""
    with _absent_fast_json_repair():
        try:
            module = _load_fresh_module("_tool_executor_without_fast_json_repair")
        except ImportError as exc:  # pragma: no cover - the regression
            pytest.fail(f"tool_executor failed to import without fast_json_repair: {exc}")

    try:
        assert callable(module.repair_loads)
        assert module.repair_loads('{"a": 1}') == {"a": 1}
        # The fallback is plain json.loads, so malformed JSON is not repaired.
        with pytest.raises(json.JSONDecodeError):
            module.repair_loads('{"a": ')
    finally:
        sys.modules.pop("_tool_executor_without_fast_json_repair", None)


def test_real_module_reload_survives_absent_package_and_is_restored():
    """Reloading the shared module under the absence keeps it importable.

    Guards against the import machinery caching a half-initialised module: the
    reloaded module must be usable, and the real one must be restored so later
    tests in the same process see the normal module again.
    """
    with _absent_fast_json_repair():
        reloaded = importlib.reload(tool_executor)
        assert callable(reloaded.repair_loads)
        assert reloaded.repair_loads('{"a": 1}') == {"a": 1}

    # Restored: the shared module is reloaded once more with the real import
    # path available, so this file cannot leak the fallback into other tests.
    restore = importlib.reload(tool_executor)
    assert callable(restore.repair_loads)
    assert restore.repair_loads('{"b": 2}') == {"b": 2}
