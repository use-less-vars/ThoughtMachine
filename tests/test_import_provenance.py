"""Import-provenance guard: the suite must exercise THIS checkout's code.

Why this test exists
--------------------
Nine test modules used to prepare ``sys.path`` with a literal container path::

    sys.path.insert(1, "/workspace")

That literal is correct only when the checkout lives at ``/workspace``.  When
the suite runs from a git worktree (``/workspace/.worktrees/<name>``) the
literal resolves to the PARENT checkout, so those modules imported the parent
tree's ``security`` / ``tools`` / ``agent`` packages -- and still went green.

The phantom pass was measured, not assumed: with the worktree's
``security/security_gate.apply_workspace_ceiling`` mutated into a no-op
(``if True:  # MUTATION PROBE``), all 208 tests in those nine modules still
passed, because they were executing the parent checkout's unmutated module.
After the literals were removed the same mutation turns them red.

This module pins the property directly -- no subprocess, no ``sitecustomize``
plugin, no ``PYTHONPATH`` fixture:

1. it checks where the project packages actually resolve from, and
2. it statically rejects any ``sys.path`` mutation under ``tests/`` whose
   argument is a string literal containing ``/workspace``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

#: The project packages those modules imported; each one must come from
#: ``REPO_ROOT`` and not from some other checkout of the repository.
PROJECT_MODULES = ("security", "security.security_gate", "tools", "agent")

#: Container-only fake package directory used by the sandbox harness.  Nothing
#: outside ``REPO_ROOT`` other than this sandbox stub directory is an
#: acceptable import origin for a project module.
SANDBOX_STUBS_DIR = Path("/tmp/stubs")


def _origin_of(module_name: str) -> Path:
    """Return the file a bare ``import <module_name>`` would resolve to."""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, f"{module_name!r} is not importable at all"
    origin = spec.origin
    assert origin is not None and origin not in {"namespace", "built-in", "frozen"}, (
        f"{module_name!r} has no file origin (spec.origin={origin!r}); "
        "the provenance check cannot prove where it came from"
    )
    return Path(origin).resolve()


def test_project_modules_resolve_inside_this_checkout() -> None:
    """``security``/``tools``/``agent`` must come from this checkout."""
    offenders = []
    for module_name in PROJECT_MODULES:
        origin = _origin_of(module_name)
        if origin.is_relative_to(REPO_ROOT) or origin.is_relative_to(SANDBOX_STUBS_DIR):
            continue
        offenders.append(f"{module_name} -> {origin}")
    assert not offenders, (
        "these imports resolved outside this checkout "
        f"({REPO_ROOT}), so the tests would be exercising another tree's code: "
        + "; ".join(offenders)
    )


def _hardcoded_sys_path_mutation_lines(tree: ast.AST) -> list[int]:
    """Line numbers of ``sys.path`` mutations built from a ``/workspace`` literal."""
    bad_lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in {"insert", "append", "remove"}:
            continue
        target = func.value
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "path"
            and isinstance(target.value, ast.Name)
            and target.value.id == "sys"
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "/workspace" in arg.value:
                bad_lines.append(node.lineno)
                break
    return bad_lines


def test_no_hardcoded_container_paths_mutate_sys_path() -> None:
    """No test may hardcode a container path into ``sys.path``."""
    scanned = 0
    failures = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanned += 1
        for lineno in _hardcoded_sys_path_mutation_lines(tree):
            failures.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert scanned > 50, f"only {scanned} test files scanned -- the rglob is broken"
    assert not failures, (
        "sys.path is mutated with a hardcoded /workspace path in: "
        + ", ".join(failures)
        + ". Derive the repo root from __file__ instead (see tests/conftest.py "
        "and tests/security/conftest.py): a literal /workspace points at the "
        "PARENT checkout when the suite runs from a git worktree, so the module "
        "under test is not the one in this tree."
    )
