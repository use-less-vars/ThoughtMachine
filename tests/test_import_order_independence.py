"""
test_import_order_independence.py — ``agent`` package import-order independence.

Regression guard for a CPython import-machinery interaction that made
``import agent.core.x as y`` order-dependent.

``agent/core/__init__.py`` is a barrel: it re-exports ``Agent``,
``ToolExecutor`` and friends, so importing it pulls in nearly the whole
``agent.core`` tree.  When ``agent.config.defaults`` (imported by
``security/security_gate.py`` at module import time) is the *first* thing to
trigger that barrel, the barrel can fail part-way through; CPython's
failed-import cleanup then evicts the partially-initialised ``agent.core``
package from ``sys.modules``, while the ``agent.core.*`` children it already
imported stay cached.  The ``agent`` package object itself survives but is
left without a ``core`` attribute, so a later

    import agent.core.tool_executor as t

— which is IMPORT_NAME followed by IMPORT_FROM, i.e. ``getattr(agent,
"core")`` — raised::

    ImportError: cannot import name 'core' from 'agent'

``agent/__init__.py``'s module ``__getattr__`` now re-imports the submodule on
demand, so the order in which ``agent.*`` and ``security.*`` modules are first
imported must no longer matter.

Every case below runs in a *fresh* interpreter: the poisoning is import-time
state, so an in-process assertion could never observe it.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TIMEOUT = 120

GATE_TEST_FILE = "tests/test_security_gate_disk.py"
EXECUTOR_TEST_FILE = "tests/test_tool_executor.py"


def _run(args, timeout=TIMEOUT):
    """Run *args* in a fresh interpreter rooted at the repository."""
    return subprocess.run(
        [sys.executable, "-W", "ignore", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _python(code):
    return _run(["-c", code])


def _assert_ok(result, label):
    assert result.returncode == 0, (
        f"{label} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "OK" in result.stdout, (
        f"{label} did not print OK\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_config_defaults_then_dotted_import_as():
    """agent.config.defaults first, then ``import agent.core.x as y``.

    This is the original failure: exit 1 with
    ``ImportError: cannot import name 'core' from 'agent'``.
    """
    _assert_ok(
        _python(
            "import agent.config.defaults\n"
            "import agent.core.tool_executor as t\n"
            "print('OK')\n"
        ),
        "config.defaults -> import agent.core.tool_executor as t",
    )


def test_security_gate_then_dotted_import_as():
    """security.security_gate first (it imports agent.config.defaults)."""
    _assert_ok(
        _python(
            "import security.security_gate\n"
            "import agent.core.tool_executor as t\n"
            "print('OK')\n"
        ),
        "security.security_gate -> import agent.core.tool_executor as t",
    )


def test_control_order_dotted_import_as_first():
    """Control: the order that always worked must keep working."""
    _assert_ok(
        _python(
            "import agent.core.tool_executor as t\n"
            "import agent.config.defaults\n"
            "print('OK')\n"
        ),
        "import agent.core.tool_executor as t -> config.defaults",
    )


def test_missing_attribute_still_raises():
    """The submodule fallback must not turn typos into silent successes."""
    result = _python(
        "import agent\n"
        "try:\n"
        "    agent.this_does_not_exist_xyz\n"
        "except AttributeError as exc:\n"
        "    print('ATTRIBUTE_ERROR', exc)\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError')\n"
        "try:\n"
        "    from agent import this_does_not_exist_xyz\n"
        "except ImportError as exc:\n"
        "    print('IMPORT_ERROR', exc)\n"
        "else:\n"
        "    raise SystemExit('expected ImportError')\n"
        "print('OK')\n"
    )
    _assert_ok(result, "missing attribute")
    assert "ATTRIBUTE_ERROR" in result.stdout, result.stdout + result.stderr
    assert "IMPORT_ERROR" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize(
    "test_files",
    [
        (GATE_TEST_FILE, EXECUTOR_TEST_FILE),
        (EXECUTOR_TEST_FILE, GATE_TEST_FILE),
    ],
    ids=["gate-first", "executor-first"],
)
def test_nested_pytest_suite_order_independent(test_files):
    """The two suites must pass in either collection order.

    ``test_security_gate_disk.py`` first used to poison ``sys.modules`` for
    ``tests/test_tool_executor.py`` (1 failed, 38 passed).
    """
    result = _run(
        [
            "-m", "pytest",
            *test_files,
            "-q",
            "-p", "no:cacheprovider",
            "-p", "no:warnings",
        ]
    )
    assert result.returncode == 0, (
        f"nested pytest {' '.join(test_files)} failed "
        f"(exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout[-4000:]}\n"
        f"--- stderr ---\n{result.stderr[-4000:]}"
    )
