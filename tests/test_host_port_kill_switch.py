# tests/test_host_port_kill_switch.py
"""CI AST kill-switch: no module under ``tools/`` may reach the host directly.

Every host process is supposed to funnel through ``thoughtmachine/host_port.py``
(``host_run()`` / ``host_run_async()``), which is where the grain check, the
audit record and the resource policy live.  A stray ``import subprocess`` or a
bare ``os.system`` is the first half of a chokepoint bypass: the import (or the
call) is a declaration of intent, and it lands long before the call that quietly
stops being gated.

This guard parses source with :mod:`ast` -- never ``grep``, never an import of
the modules it scans -- so it is fast, deterministic and side-effect free, and
so that the word ``subprocess`` inside a docstring or a comment (every migrated
module explains *why* it has none) is not mistaken for the construct itself.

Rules pinned here
-----------------
1. Only :data:`SANCTIONED_MODULES` -- ``tools/host_resource_policy.py`` and
   ``tools/host_bash_tool.py`` -- may touch the host process surface inside
   ``tools/``.  They are the policy checker and the host-run bridge; everything
   else must call ``host_run()``.
2. Every other offender must appear in :data:`ALLOWED_OFFENDERS` by its exact
   repository-relative path with the reason it still needs the surface.  The
   allowlist is a path -> reason map matched by whole string: no prefixes, no
   globs, no directory entries (a prefix would silently cover files that do not
   exist yet).
3. Fail-closed.  A file that is not sanctioned and not allowlisted fails, and a
   file that cannot be parsed fails too (``ScanError``): an unparseable module is
   an unscanned module, and an unscanned module could hold anything.
4. The guard bites.  Synthetic fixtures prove that ``import subprocess``,
   ``subprocess.Popen(...)`` and ``os.system(...)`` are all flagged, and that the
   same words in a docstring or a comment are not.
5. The allowlist cannot rot: no duplicates, and every entry must exist on disk
   *and* still produce hits.  An entry that no longer bites is itself a failure.
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
from pathlib import Path
from typing import Iterable, NamedTuple

import pytest

#: Repository root, derived from this file (``<root>/tests/<this file>``).
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories scanned for a direct host-process surface (recursively).
#: ``thoughtmachine/host_port.py`` -- the chokepoint itself -- is deliberately
#: outside this root and is pinned by ``tests/test_tool_import_declaration_match.py``.
SCAN_ROOTS = ("tools",)


# ---------------------------------------------------------------------------
# the sanctioned surface: permitted by name, unconditionally
# ---------------------------------------------------------------------------

SANCTIONED_MODULES = (
    "tools/host_resource_policy.py",
    "tools/host_bash_tool.py",
)


# ---------------------------------------------------------------------------
# the allowlist: path -> reason, exact string match, one entry per file
# ---------------------------------------------------------------------------

ALLOWED_OFFENDERS: tuple[tuple[str, str], ...] = (
    (
        "tools/ci_read_tool.py",
        "spawns the `gh` CLI for CI monitoring (`subprocess.run` at line 260) "
        "under its own `ci:read` grain; not routed through host_run yet.",
    ),
    (
        "tools/git_info_tool.py",
        "imports `subprocess` ONLY for `subprocess.TimeoutExpired` (lines 731 "
        "and 861); the git spawn itself already goes through SandboxedExecution.",
    ),
    (
        "tools/git_write_tool.py",
        "imports `subprocess` ONLY for `subprocess.TimeoutExpired` (line 429); "
        "every git spawn goes through GitReadTool._run_git / SandboxedExecution.",
    ),
    (
        "tools/mcp_client.py",
        "documented async deferral: StdioMCPClient.start() needs a long-lived "
        "child (`subprocess.Popen` at line 239) and host_run_async() is still a "
        "signature-only stub raising NotImplementedError.",
    ),
    (
        "tools/mcp_server_connect.py",
        "imports `subprocess` ONLY to catch `subprocess.TimeoutExpired` "
        "(line 106); nothing is spawned here.",
    ),
    (
        "tools/workspace/worker_execution.py",
        "container teardown fallback: `docker exec <id> kill <pid>` via "
        "`subprocess.run` (line 384) in terminate_all().",
    ),
)


# ---------------------------------------------------------------------------
# the dangerous surface, as AST names
# ---------------------------------------------------------------------------

#: ``os`` entry points that start a process.  ``os`` is imported everywhere for
#: path work, so only these names -- and only these -- count as an offender.
DANGER_OS_NAMES = frozenset(
    {"system", "popen"}
    | {f"exec{v}" for v in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")}
    | {f"spawn{v}" for v in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")}
)


# ---------------------------------------------------------------------------
# the scanner: a plain function over a list of paths, so CI and tests share it
# ---------------------------------------------------------------------------


class Site(NamedTuple):
    """One offending construct: its line number and a readable description."""

    lineno: int
    construct: str


class ScanError(RuntimeError):
    """Raised when a scanned file cannot be parsed (fail-closed)."""


class _Bindings(NamedTuple):
    subprocess_names: set[str]
    os_module_names: set[str]
    os_function_names: set[str]


def _root(module_name: str) -> str:
    """Return the top-level package of a dotted module path."""
    return module_name.split(".")[0]


def _collect_import_bindings(tree: ast.AST, origin: str) -> tuple[list[Site], _Bindings]:
    """Walk every import in the module -- header, function body, ``try`` block."""
    sites: list[Site] = []
    bindings = _Bindings(set(), set(), set())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _root(alias.name)
                if top == "subprocess":
                    sites.append(Site(node.lineno, f"import {alias.name}"))
                    bindings.subprocess_names.add(alias.asname or "subprocess")
                elif top == "os":
                    bindings.os_module_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            top = _root(node.module)
            imported = ",".join(alias.name for alias in node.names)
            if top == "subprocess":
                sites.append(Site(node.lineno, f"from {node.module} import {imported}"))
                for alias in node.names:
                    bindings.subprocess_names.add(alias.asname or alias.name)
            elif top == "os":
                for alias in node.names:
                    if alias.name in DANGER_OS_NAMES or alias.name == "*":
                        sites.append(Site(node.lineno, f"from os import {alias.name}"))
                        bindings.os_function_names.add(alias.asname or alias.name)
                    else:
                        bindings.os_module_names.add(alias.asname or alias.name)
    return sites, bindings


def _collect_uses(tree: ast.AST, bindings: _Bindings) -> list[Site]:
    """Walk attribute/name references against the bindings found above."""
    sites: list[Site] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base in bindings.subprocess_names:
                sites.append(Site(node.lineno, f"subprocess.{node.attr}"))
            elif base in bindings.os_module_names and node.attr in DANGER_OS_NAMES:
                sites.append(Site(node.lineno, f"os.{node.attr}"))
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in bindings.os_function_names
        ):
            sites.append(Site(node.lineno, f"os-name `{node.id}`"))
    return sites


def scan_source(source: str, origin: str = "<string>") -> list[Site]:
    """Return every host-process site in ``source`` (empty list == clean).

    Raises :class:`ScanError` when the source does not parse: fail-closed.
    """
    try:
        tree = ast.parse(source, filename=origin)
    except SyntaxError as exc:  # pragma: no cover - message is the assertion
        raise ScanError(f"{origin}: cannot be parsed ({exc})") from exc

    sites, bindings = _collect_import_bindings(tree, origin)
    sites.extend(_collect_uses(tree, bindings))

    unique = sorted({(site.lineno, site.construct) for site in sites})
    return [Site(lineno, construct) for lineno, construct in unique]


def scan_paths(paths: Iterable[Path | str]) -> dict[str, list[Site]]:
    """Scan a list of files; return ``{relative path: sites}`` for the offenders.

    Only offending files appear in the result.  An unreadable or unparseable
    file raises -- a file this guard cannot read is not a clean file.
    """
    offenders: dict[str, list[Site]] = {}
    for raw in paths:
        path = Path(raw)
        sites = scan_source(path.read_text(encoding="utf-8"), origin=str(path))
        if sites:
            offenders[_rel(path)] = sites
    return offenders


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rel(path: Path) -> str:
    """Repository-relative POSIX path, or the raw path when outside the tree."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:  # pragma: no cover - only for fixtures outside the tree
        return path.as_posix()


def _scanned_files() -> list[Path]:
    """Every ``*.py`` file under :data:`SCAN_ROOTS` (``__pycache__`` skipped)."""
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _allowlisted() -> dict[str, str]:
    return dict(ALLOWED_OFFENDERS)


def _describe(offenders: dict[str, list[Site]]) -> str:
    lines = []
    for rel_path in sorted(offenders):
        sites = ", ".join(f"line {s.lineno}: {s.construct}" for s in offenders[rel_path])
        lines.append(f"  {rel_path} -> {sites}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the real tree
# ---------------------------------------------------------------------------


def test_tools_tree_has_no_unsanctioned_host_process_surface():
    offenders = scan_paths(_scanned_files())
    sanctioned = set(SANCTIONED_MODULES)
    allowed = _allowlisted()

    unlisted = {
        rel_path: sites
        for rel_path, sites in offenders.items()
        if rel_path not in sanctioned and rel_path not in allowed
    }
    assert not unlisted, (
        "Host-process kill-switch tripped: module(s) under tools/ reach the host "
        "directly instead of going through thoughtmachine.host_port.host_run().\n"
        f"{_describe(unlisted)}\n"
        "Fix the module by calling host_run()/host_run_async(), or -- if the "
        "surface genuinely has to stay -- add an exact-path entry with its reason "
        "to ALLOWED_OFFENDERS in tests/test_host_port_kill_switch.py. "
        f"Sanctioned modules (no entry needed): {sorted(sanctioned)}"
    )


def test_scan_finds_the_known_offenders():
    """Positive control: the real-tree scan is not silently returning nothing."""
    offenders = scan_paths(_scanned_files())
    assert offenders, (
        "The host-process scan found nothing anywhere under tools/. Either the "
        "scan is broken (it must at least see the known offenders) or the "
        "allowlist has gone stale."
    )
    missing = sorted(set(_allowlisted()) - set(offenders))
    assert not missing, (
        "These ALLOWED_OFFENDERS entries no longer produce any hit, so the "
        "allowlist has rotted -- remove them:\n  " + "\n  ".join(missing)
    )


def test_sanctioned_modules_exist_and_are_scanned():
    for rel_path in SANCTIONED_MODULES:
        path = REPO_ROOT / rel_path
        assert path.is_file(), f"sanctioned module {rel_path} does not exist"
        assert path in _scanned_files(), (
            f"{rel_path} is not picked up by the scan roots {SCAN_ROOTS}, so its "
            "sanction is decorative"
        )


# ---------------------------------------------------------------------------
# the allowlist itself
# ---------------------------------------------------------------------------


def test_allowlist_is_exact_paths_with_reasons():
    allowed = _allowlisted()
    assert len(allowed) == len(ALLOWED_OFFENDERS), (
        "ALLOWED_OFFENDERS contains duplicate paths: "
        f"{sorted(p for p in allowed)}"
    )
    for rel_path, reason in ALLOWED_OFFENDERS:
        assert "*" not in rel_path and "?" not in rel_path, (
            f"allowlist entry {rel_path!r} is a glob; entries must be exact paths"
        )
        assert not rel_path.endswith("/"), (
            f"allowlist entry {rel_path!r} is a directory prefix; entries must "
            "name a single file"
        )
        assert rel_path.endswith(".py"), f"allowlist entry {rel_path!r} is not a .py file"
        assert not rel_path.startswith("/"), f"allowlist entry {rel_path!r} must be repo-relative"
        assert (REPO_ROOT / rel_path).is_file(), (
            f"stale allowlist entry {rel_path!r}: the file does not exist"
        )
        assert len(reason.strip()) >= 20, (
            f"allowlist entry {rel_path!r} needs a real reason, got {reason!r}"
        )
        assert rel_path not in SANCTIONED_MODULES, (
            f"{rel_path!r} is already sanctioned and must not also be allowlisted"
        )


def test_allowlist_has_not_grown():
    """New offenders fail here even if someone quietly appends to the allowlist."""
    discovered = set(scan_paths(_scanned_files())) - set(SANCTIONED_MODULES)
    allowlisted = set(_allowlisted())

    added = sorted(discovered - allowlisted)
    removed = sorted(allowlisted - discovered)
    assert not added, (
        "New unsanctioned host-process offender(s) appeared under tools/:\n  "
        + "\n  ".join(added)
        + "\nMigrate them to host_run() or add an exact-path reason to ALLOWED_OFFENDERS."
    )
    assert not removed, (
        "ALLOWED_OFFENDERS lists file(s) that produce no hit any more (the "
        "allowlist must shrink when the last offender is migrated):\n  "
        + "\n  ".join(removed)
    )


def test_allowed_offender_reasons_bite_on_the_recorded_constructs():
    """Each allowlist reason names the construct that justifies it."""
    offenders = scan_paths(_scanned_files())
    for rel_path, reason in ALLOWED_OFFENDERS:
        constructs = " ".join(site.construct for site in offenders[rel_path])
        assert constructs, f"no hits recorded for allowlisted {rel_path}"
        if "ONLY" in reason:
            assert "TimeoutExpired" in constructs, (
                f"{rel_path} is allowlisted as exception-type-only but now uses: "
                f"{constructs}"
            )


# ---------------------------------------------------------------------------
# the guard bites (synthetic fixtures; nothing under the real tree is touched)
# ---------------------------------------------------------------------------


def test_scanner_flags_import_subprocess():
    sites = scan_source("import subprocess\n")
    assert [(s.lineno, s.construct) for s in sites] == [(1, "import subprocess")], sites


def test_scanner_flags_subprocess_call():
    source = textwrap.dedent(
        """
        import subprocess


        def run_it():
            return subprocess.Popen(["ls"], stdout=subprocess.PIPE)
        """
    )
    sites = scan_source(source)
    constructs = [s.construct for s in sites]
    assert "subprocess.Popen" in constructs, sites
    assert "subprocess.PIPE" in constructs, sites


def test_scanner_flags_os_system():
    sites = scan_source('import os\n\nos.system("ls")\n')
    constructs = [s.construct for s in sites]
    assert "os.system" in constructs, sites


def test_scanner_flags_from_imports():
    assert [s.construct for s in scan_source("from subprocess import Popen\n")] == [
        "from subprocess import Popen"
    ]
    assert "from os import execvp" in [
        s.construct for s in scan_source("from os import execvp\n")
    ]
    sites = scan_source("from os import system as sh\n\n\nsh('ls')\n")
    assert sorted(s.construct for s in sites) == [
        "from os import system",
        "os-name `sh`",
    ], sites


def test_scanner_ignores_docstrings_and_comments():
    source = textwrap.dedent(
        '''
        """This module never imports subprocess and never calls os.system()."""

        # subprocess.Popen would be a bypass; os.system likewise. import subprocess
        HOST = "subprocess.Popen"


        def helper():
            """os.system is deliberately absent here."""
            return 1  # subprocess.run is not used
        '''
    )
    assert scan_source(source) == [], scan_source(source)


def test_scanner_flags_local_import_inside_function():
    source = ("def f():\n    import subprocess\n    return subprocess.run(['ls'])\n")
    lines = sorted({s.lineno for s in scan_source(source)})
    assert lines == [2, 3], scan_source(source)


def test_scan_paths_flags_synthetic_tree():
    """The path-list entry point -- the one CI uses -- bites on real files."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "clean.py").write_text('"""mentions subprocess and os.system"""\n', encoding="utf-8")
        (root / "bad_import.py").write_text("import subprocess\n", encoding="utf-8")
        (root / "bad_call.py").write_text("import os\n\nos.system('ls')\n", encoding="utf-8")

        offenders = scan_paths(sorted(root.glob("*.py")))

        assert sorted(offenders) == [str(root / "bad_call.py"), str(root / "bad_import.py")], (
            offenders
        )
        assert [s.construct for s in offenders[str(root / "bad_import.py")]] == [
            "import subprocess"
        ]
        assert "os.system" in [s.construct for s in offenders[str(root / "bad_call.py")]]


def test_scan_is_fail_closed_on_unparseable_source():
    with pytest.raises(ScanError):
        scan_source("def broken(:\n", origin="broken.py")


def test_scan_paths_is_fail_closed_on_unparseable_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "broken.py"
        path.write_text("def broken(:\n", encoding="utf-8")
        with pytest.raises(ScanError):
            scan_paths([path])
