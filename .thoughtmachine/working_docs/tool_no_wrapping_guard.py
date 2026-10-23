# =============================================================================
# PARKED GUARD SOURCE -- hygiene/no-tool-wrapping
# =============================================================================
# STATUS: PARKED. This file is NOT active and a normal pytest run does NOT
# collect it:
#   * its path is outside tests/ (.thoughtmachine/working_docs/), and
#   * its filename does not match test_*.py (nor *_test.py), so default
#     collection ignores it. Passing this path to pytest explicitly WOULD
#     collect it -- don't; that mode is not how it is meant to be used.
# Do not import it, do not run it as a test suite from here.
#
# WHY PARKED
#   The rule this guard encodes has 13 open violations in the tree today
#   (5 x letter (a), 7 x letter (b), 1 x letter (c)), so 3 of its 4 tests fail
#   on an untouched checkout. Landing it as tests/test_tool_no_wrapping.py
#   right now would make CI permanently red, which trains reviewers to ignore
#   a red suite -- the opposite of what a structural guard is for. The rule is
#   real and stays; only its landing is deferred until the site count is zero.
#
# ACTIVATION (after the burn-down in
# .thoughtmachine/working_docs/ticket-hygiene-no-tool-wrapping.md):
#   1. move this file back to tests/test_tool_no_wrapping.py
#   2. drop this header block (everything above the original module docstring)
#   3. confirm it is green:  python -m pytest -q tests/test_tool_no_wrapping.py
#   Activating the guard and landing it green belong in the same PR.
#
# WHAT IT IS
#   Structural ban on tool wrapping: a tool is a leaf of the permission model,
#   not a node. No tool may invoke another tool, no wrapping, no tool symlinks.
#   If a tool needs another tool's capability it must use a shared internal
#   function, never the tool registry.
#
# THE THREE LETTERS
#   (a) IMPORT   no module under tools/ (other than the registry
#                tools/__init__.py) may import another tool module or tool
#                class.
#   (b) REGISTRY no code outside tools/__init__.py may reference TOOL_CLASSES
#                or SIMPLIFIED_TOOL_CLASSES.
#   (c) DYNAMIC  no dynamic import by string (importlib.import_module /
#                __import__(...)) inside tools/.
#
# ENFORCEMENT DESIGN (already implemented below, unchanged)
#   Pure ast static analysis: nothing here imports or executes the application.
#   No whitelist, no allow-list, no xfail, no skip -- a site is either clean or
#   it is a violation. Violations are collected as strings and reported by a
#   single `assert not violations` carrying the count and the joined list.
#   Non-vacuity self-check: a synthetic PaginateTool-shaped source is fed
#   through the same scanner and must trip letters (a), (b) and (c); that test
#   is the one currently passing and it is why the other three failures are
#   findings rather than scanner bugs.
#
# CENSUS AT PARK TIME
#   48 .py files scanned under tools/, 29 tool modules, 29 tool classes,
#   13 violations. Known detection defect (do NOT "fix" it by re-scoping the
#   rule): class GitWriteTool(GitReadTool) is neither a tool module nor a
#   direct ToolBase subclass, so any scoping keyed on "this file defines a
#   ToolBase subclass" silently exempts the second-worst site.
#
# The body below is byte-for-byte the file that was written to
# tests/test_tool_no_wrapping.py; it has not been weakened, edited or
# partially disabled for parking.
# =============================================================================

"""Structural ban on tool wrapping -- ``hygiene/no-tool-wrapping``.

A tool is a leaf: nothing under ``tools/`` may treat another tool as a
callable / addressable node.  Three letters are pinned, all by static analysis
(``ast``), so nothing here imports or executes the application:

(a) IMPORT   Files under ``tools/`` other than the registry ``tools/__init__.py``
             must not import another tool MODULE or tool CLASS -- neither
             ``from .x import Y``, ``from tools.x import Y``, ``import tools.x``
             nor ``from tools import X``.  Importing ``tools/base`` (ToolBase),
             ``tools/__init__`` itself, or a non-tool helper module is allowed.
             "Tool module" is derived from the tree itself: a module under
             ``tools/`` that defines a class whose base name ends with
             ``ToolBase`` -- the set is never hard-coded.
(b) REGISTRY ``TOOL_CLASSES`` / ``SIMPLIFIED_TOOL_CLASSES`` may be referenced
             only from ``tools/__init__.py``: no plain reads, ``.append(...)``,
             subscripts, ``.get(...)`` or iteration elsewhere under ``tools/``.
(c) DYNAMIC  No dynamic module resolution by string inside ``tools/``:
             ``importlib.import_module(...)`` / ``__import__(...)`` outside
             ``tools/__init__.py``.

There is no whitelist, no allow-list of names, no ``xfail`` and no ``skip``:
any hit under any letter is RED.  The scanner is exercised against a synthetic
snippet (``test_scanner_flags_a_wrapping_tool``) so it can never become
vacuously green if the real tree changes shape.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
REGISTRY_FILE = TOOLS_DIR / "__init__.py"
REGISTRY_NAMES = frozenset({"TOOL_CLASSES", "SIMPLIFIED_TOOL_CLASSES"})
BASE_SUFFIX = "ToolBase"
RULE = "hygiene/no-tool-wrapping"

LETTER_WHAT = {
    "a": "a tool module/class is imported by another file under tools/",
    "b": "the tool registry is referenced outside tools/__init__.py",
    "c": "a module is resolved dynamically by string inside tools/",
}


# ── tree discovery ────────────────────────────────────────────────────────────
def _iter_tool_files():
    """Every ``*.py`` under ``tools/``, recursively, excluding caches."""
    return sorted(p for p in TOOLS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path):
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        return source, ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # unparsable file = hole in the pin, never a pass
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)}:{exc.lineno}: cannot parse for {RULE}: {exc.msg}"
        )


def _module_name(path):
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _tool_modules_and_classes():
    """Derive (tool module names, tool class names) from the tree itself."""
    tool_modules, tool_classes = set(), set()
    for path in _iter_tool_files():
        _, tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                (_base_name(base) or "").endswith(BASE_SUFFIX) for base in node.bases
            ):
                tool_modules.add(_module_name(path))
                tool_classes.add(node.name)
    return tool_modules, tool_classes


def _resolve_from(node, module):
    """Absolute module path an ``ImportFrom`` refers to, or "" when relative-past-root."""
    if not node.level:
        return node.module or ""
    parts = module.split(".")[:-1]
    up = node.level - 1
    if up:
        parts = parts[:-up] if up <= len(parts) else []
    base = ".".join(parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


# ── scanning ──────────────────────────────────────────────────────────────────
def _violations(source, module, tool_modules, tool_classes):
    """All (lineno, letter, detail) hits in *source*, one per offending node."""
    tree = ast.parse(source)
    lines = source.splitlines()
    found = []

    def text(lineno):
        return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""

    def add(lineno, letter, detail):
        found.append((lineno, letter, f"{detail} || {text(lineno)}"))

    for node in ast.walk(tree):
        # (a) imports of a tool module / tool class
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in tool_modules:
                    add(node.lineno, "a", f"imports tool module '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from(node, module)
            names = [alias.name for alias in node.names]
            parts = []
            if target in tool_modules:
                parts.append(
                    f"imports from tool module '{target}' (names: {', '.join(names)})"
                )
            for name in names:
                if name in tool_classes:
                    parts.append(f"imports tool class '{name}' (from '{target or '.'}')")
            if parts:
                add(node.lineno, "a", "; ".join(parts))
            # The registry imported BY NAME is a registry reference in its own
            # right -- and the only way an alias could otherwise hide from (b).
            for name in names:
                if name in REGISTRY_NAMES:
                    add(node.lineno, "b", f"imports registry name '{name}' (from '{target or '.'}')")
        # (b) registry referenced by name
        elif isinstance(node, ast.Name) and node.id in REGISTRY_NAMES:
            add(node.lineno, "b", f"references registry name '{node.id}'")
        elif isinstance(node, ast.Attribute) and node.attr in REGISTRY_NAMES:
            add(node.lineno, "b", f"references registry name '(...).{node.attr}'")
        # (c) dynamic module resolution by string
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"__import__", "import_module"}:
                add(node.lineno, "c", f"dynamic import via '{func.id}(...)'")
            elif isinstance(func, ast.Attribute) and func.attr == "__import__":
                add(node.lineno, "c", "dynamic import via '(...).__import__(...)'")
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
            ):
                add(node.lineno, "c", "dynamic import via 'importlib.import_module(...)'")

    return found


def _collect(letter):
    """Every violation of *letter* across ``tools/`` (registry exempted)."""
    tool_modules, tool_classes = _tool_modules_and_classes()
    violations, scanned = [], 0
    for path in _iter_tool_files():
        module = _module_name(path)
        source, _ = _parse(path)
        scanned += 1
        if path == REGISTRY_FILE:
            # The registry legitimately imports every tool to build TOOL_CLASSES.
            continue
        for lineno, hit_letter, detail in _violations(
            source, module, tool_modules, tool_classes
        ):
            if hit_letter == letter:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: [{letter}] {detail}"
                )
    return sorted(violations), scanned, tool_modules, tool_classes


def _assert_clean(letter, violations, scanned, tool_modules):
    assert scanned > 0, f"scanned no files under tools/ -- {RULE} pin is vacuous"
    assert tool_modules, "detected no tool modules -- the detector is broken, not the tree"
    assert not violations, (
        f"{len(violations)} violation(s) of {RULE} "
        f"({letter}: {LETTER_WHAT[letter]}) "
        f"-- scanned {scanned} file(s) under tools/, {len(tool_modules)} tool module(s):\n  "
        + "\n  ".join(violations)
    )


# ── the rule ──────────────────────────────────────────────────────────────────
def test_rule_a_no_tool_imports_another_tool():
    """(a) No file under ``tools/`` except the registry imports a tool module/class."""
    violations, scanned, tool_modules, _ = _collect("a")
    _assert_clean("a", violations, scanned, tool_modules)


def test_rule_b_registry_referenced_only_from_registry():
    """(b) ``TOOL_CLASSES`` / ``SIMPLIFIED_TOOL_CLASSES`` live only in ``tools/__init__.py``."""
    violations, scanned, tool_modules, _ = _collect("b")
    _assert_clean("b", violations, scanned, tool_modules)


def test_rule_c_no_dynamic_module_resolution_by_string():
    """(c) No ``importlib.import_module`` / ``__import__`` call inside ``tools/``."""
    violations, scanned, tool_modules, _ = _collect("c")
    _assert_clean("c", violations, scanned, tool_modules)


def test_scanner_flags_a_wrapping_tool():
    """Self-check: the scanner is NOT vacuous -- it catches a PaginateTool-shaped file.

    The deleted ``tools/paginate_tool.py`` resolved a target tool through the
    registry and instantiated it by name; the synthetic snippet below reproduces
    exactly that shape and must trip (a), (b) and (c).
    """
    source = (
        "from .glob_tool import GlobTool\n"
        "from tools import TOOL_CLASSES\n"
        "import importlib\n"
        "\n"
        "class PaginateTool(ToolBase):\n"
        "    def execute(self):\n"
        "        target = TOOL_CLASSES[0]\n"
        "        TOOL_CLASSES.append(target)\n"
        "        other = importlib.import_module('tools.glob_tool')\n"
        "        return target, other, GlobTool, __import__('tools.thought')\n"
    )
    hits = _violations(source, "tools.paginate_tool", {"tools.glob_tool"}, {"GlobTool"})
    letters = {letter for _, letter, _ in hits}
    assert letters == {"a", "b", "c"}, f"scanner missed a letter: {sorted(hits)}"
