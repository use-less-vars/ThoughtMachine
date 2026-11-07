# tests/test_tool_import_declaration_match.py
"""CI AST guard: ``subprocess`` may only be imported where the single-chokepoint
rule for host execution permits it.

``thoughtmachine/host_port.py`` is the ONE place allowed to launch a host
process.  Every caller is supposed to route through ``host_run()`` /
``host_run_async()`` so that the grain check, the audit record and the sandbox
all happen in a single funnel.  A stray ``import subprocess`` anywhere else is
the first half of a chokepoint bypass: the import is the declaration of intent,
and it lands long before the call that quietly stops being gated.

This is a static guard only -- it parses source with :mod:`ast` and never
imports the modules it scans, so it is fast, deterministic and free of side
effects (no process is spawned, no socket is opened, nothing heavy is imported
at module scope).

Rules pinned here
-----------------
1. :data:`CHOKEPOINT_MODULE` may import ``subprocess`` unconditionally.
2. Every other importer must be listed in :data:`ALLOWED_EXCEPTIONS` with its
   reason, and that list is pinned by exact membership so it cannot grow
   silently: a new importer fails with its own path in the message.
3. ``tools/mcp_client.py`` is allowed *only* as the documented deferral.  It
   must still carry its deferral markers (``host_run_async``,
   ``HostRunHandle``, ``NotImplementedError``).  If the markers disappear while
   the import stays, the exception is no longer self-documenting and this guard
   fails.
4. The modules migrated in this wave -- ``tools/host_bash_tool.py`` and
   ``tools/workspace/check_system.py`` -- must import no ``subprocess`` at all,
   while ``thoughtmachine/host_port.py`` stays the positive control proving the
   scan actually finds imports.
5. The exception list may contain neither duplicates nor entries that no longer
   exist on disk (a stale allowlist is how these guards rot).

All four import forms are detected -- ``import subprocess``,
``import subprocess as X``, ``from subprocess import ...`` and a
function-local import inside a function body -- because the scan walks the
whole AST instead of the module header.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Repository root, derived from this file (``<root>/tests/<this file>``) so the
#: guard does not depend on the working directory pytest happens to run from.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories scanned for ``subprocess`` imports (recursively; ``__pycache__``
#: is skipped).
SCAN_ROOTS = ("thoughtmachine", "tools")


# ---------------------------------------------------------------------------
# the chokepoint: permitted, always, with no condition attached
# ---------------------------------------------------------------------------

CHOKEPOINT_MODULE = "thoughtmachine/host_port.py"


# ---------------------------------------------------------------------------
# the allowlist: every module OTHER than the chokepoint that may still import
# subprocess.  One entry per file, each with the reason it is allowed to keep
# the import.  Order is irrelevant; membership is pinned in
# ``test_allowlist_has_not_grown``.
# ---------------------------------------------------------------------------

ALLOWED_EXCEPTIONS = (
    # -- DOCUMENTED DEFERRAL (conditional: see DEFERRAL_MARKERS) ------------
    # StdioMCPClient.start() still spawns the MCP server with
    # ``subprocess.Popen`` because the async port entry point does not exist
    # yet: host_run_async() and every HostRunHandle method are signature-only
    # stubs that raise NotImplementedError.  An MCP stdio child is long-lived
    # (stdin/stdout stay open for the whole session), so it needs the async
    # primitive, not host_run().  The deferral is recorded in a comment at the
    # Popen site (pinned by tests/test_mcp_client_uses_host_port.py).  When the
    # async primitive lands this file migrates to host_run_async(...) and this
    # entry must be pruned together with the comment.
    "tools/mcp_client.py",
    # -- PRE-EXISTING IMPORTERS, OUT OF SCOPE FOR THIS WAVE -----------------
    # `thoughtmachine doctor` self-check: its local _run() helper shells out to
    # python3/docker to report host health.  A dev/ops diagnostic, never
    # reachable from an agent tool call, so the tool-execution chokepoint does
    # not govern it.
    "thoughtmachine/doctor.py",
    # CI monitoring: shells out to the `gh` CLI (subprocess.run) to read
    # workflow runs.  Own grain (ci:read) and own gate; unrelated to host
    # execution tooling.
    "tools/ci_read_tool.py",
    # git read surface: drives the `git` CLI for status/diff/log/show/blame.
    # Out of scope for the host-execution wave.
    "tools/git_info_tool.py",
    # git write surface: drives the `git` CLI for commit/stage/checkout.  Out of
    # scope for the host-execution wave.
    "tools/git_write_tool.py",
    # Imports subprocess ONLY to catch the ``subprocess.TimeoutExpired``
    # exception type raised by SandboxedExecution.run(); it launches nothing
    # itself, so there is no spawn here to migrate.
    "tools/mcp_server_connect.py",
    # Worker/container lifecycle teardown: `docker exec <id> kill <pid>`
    # fallback used by terminate_all() when no container exec callable is
    # available.  Container plumbing, not host-execution tooling.
    "tools/workspace/worker_execution.py",
)


# ---------------------------------------------------------------------------
# conditions and controls
# ---------------------------------------------------------------------------

#: Exceptions whose permission is CONDITIONAL, not blanket: the module may keep
#: the import only while it still carries its deferral markers.
DEFERRED_EXCEPTIONS = ("tools/mcp_client.py",)

#: Tokens that make the tools/mcp_client.py deferral self-documenting.
DEFERRAL_MARKERS = ("host_run_async", "HostRunHandle", "NotImplementedError")

#: Modules migrated onto the chokepoint in this wave: they must import no
#: subprocess at all.  Registering one of these in ALLOWED_EXCEPTIONS instead of
#: fixing the code is a regression, not a fix.
MIGRATED_MODULES = (
    "tools/host_bash_tool.py",
    "tools/workspace/check_system.py",
)

#: The exact set of modules in SCAN_ROOTS that import subprocess, sorted.  This
#: is the anti-growth pin: it is written out literally (never derived from
#: ALLOWED_EXCEPTIONS) so that adding a file to the allowlist AND to the tree
#: still fails until a human updates this tuple deliberately.
PINNED_IMPORTERS = (
    "thoughtmachine/doctor.py",
    "thoughtmachine/host_port.py",
    "tools/ci_read_tool.py",
    "tools/git_info_tool.py",
    "tools/git_write_tool.py",
    "tools/mcp_client.py",
    "tools/mcp_server_connect.py",
    "tools/workspace/worker_execution.py",
)


# ---------------------------------------------------------------------------
# helpers: pure AST, no imports of the scanned modules
# ---------------------------------------------------------------------------


def _scanned_files() -> list[Path]:
    """Every ``*.py`` file under :data:`SCAN_ROOTS`, sorted and de-duplicated."""
    paths: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            paths.append(path)
    return paths


def _rel(path: Path) -> str:
    """POSIX path relative to the repository root (the allowlist key form)."""
    return path.relative_to(REPO_ROOT).as_posix()


def _import_sites(source: str) -> list[tuple[int, str, str]]:
    """Every ``subprocess`` import in *source* as ``(lineno, form, scope)``.

    ``form`` is the normalised import statement; ``scope`` names the enclosing
    function when the import is function-local (``""`` means module level).
    Comments and strings are ignored because this walks the AST, so prose about
    ``subprocess`` (such as the deferral note) is never mistaken for an import.
    """
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def _scope(node: ast.AST) -> str:
        names: list[str] = []
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(current.name)
            current = parents.get(current)
        if not names:
            return ""
        return "inside " + " -> ".join(reversed(names)) + "()"

    sites: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "subprocess":
                    form = f"import {alias.name}"
                    if alias.asname:
                        form += f" as {alias.asname}"
                    sites.append((node.lineno, form, _scope(node)))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                imported = ", ".join(
                    alias.name + (f" as {alias.asname}" if alias.asname else "")
                    for alias in node.names
                )
                form = f"from {node.module} import {imported}"
                sites.append((node.lineno, form, _scope(node)))
    return sites


def _discover_importers() -> dict[str, list[tuple[int, str, str]]]:
    """Map ``relative/path.py -> import sites`` for every discovered importer."""
    found: dict[str, list[tuple[int, str, str]]] = {}
    for path in _scanned_files():
        sites = _import_sites(path.read_text(encoding="utf-8"))
        if sites:
            found[_rel(path)] = sites
    return found


def _import_sites_for(rel_path: str) -> list[tuple[int, str, str]]:
    """Import sites of one module, addressed by its allowlist key."""
    path = REPO_ROOT / rel_path
    if not path.is_file():
        return []
    return _import_sites(path.read_text(encoding="utf-8"))


def _describe(sites: list[tuple[int, str, str]]) -> str:
    """Readable one-per-line rendering of import sites for failure messages."""
    return "\n".join(
        f"  {lineno}: {form}" + (f"  [{scope}]" if scope else "")
        for lineno, form, scope in sites
    )


def _source_for(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _missing_entries(discovered: list[str], expected: tuple[str, ...]) -> str:
    """Failure text naming what is new, what is stale, and what to do about it."""
    new = [item for item in discovered if item not in expected]
    gone = [item for item in expected if item not in discovered]
    lines: list[str] = []
    if new:
        lines.append(
            "NEW subprocess importer(s) not permitted by the single-chokepoint "
            "rule:\n"
            + "\n".join(f"  {item}" for item in new)
            + "\n  Fix: do not import subprocess here -- call host_run() / "
            "host_run_async() from thoughtmachine/host_port.py instead.\n"
            "  If it genuinely cannot be migrated in this wave, add it to "
            "ALLOWED_EXCEPTIONS in tests/test_tool_import_declaration_match.py "
            "with a one-line reason."
        )
    if gone:
        lines.append(
            "STALE allowlist/pin entry (the module no longer imports "
            "subprocess, or no longer exists):\n"
            + "\n".join(f"  {item}" for item in gone)
            + "\n  Fix: prune the entry from ALLOWED_EXCEPTIONS and/or "
            "PINNED_IMPORTERS in tests/test_tool_import_declaration_match.py "
            "-- a stale allowlist re-permits a bypass that has already been "
            "cleaned up."
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 1. only the chokepoint may import subprocess unconditionally
# ---------------------------------------------------------------------------


def test_only_the_chokepoint_imports_subprocess_unconditionally():
    """Every importer must be the chokepoint or a documented exception."""
    discovered = _discover_importers()

    # Scanner sanity: an empty scan would make every assertion below vacuous.
    assert CHOKEPOINT_MODULE in discovered, (
        f"the scanner did not find the known chokepoint {CHOKEPOINT_MODULE} at "
        f"all -- the AST scan in tests/test_tool_import_declaration_match.py is "
        f"broken (scanned roots: {', '.join(SCAN_ROOTS)}), so this guard would "
        f"pass vacuously. Fix the scan, not the allowlist."
    )

    allowed = set(ALLOWED_EXCEPTIONS) | {CHOKEPOINT_MODULE}
    offenders = sorted(item for item in discovered if item not in allowed)
    assert not offenders, (
        "these modules import subprocess but are neither the chokepoint "
        f"({CHOKEPOINT_MODULE}) nor a documented exception:\n"
        + "\n".join(
            f"  {item}\n" + _describe(discovered[item]) for item in offenders
        )
        + "\n  Fix: route the launch through host_run()/host_run_async() in "
        "thoughtmachine/host_port.py, or (only if it truly cannot be migrated "
        "now) register the file in ALLOWED_EXCEPTIONS with a one-line reason."
    )

    # The allowlist minus the chokepoint must be exactly the documented
    # exceptions: no importer may be permitted without being written down, and
    # no permission may exist for a file that does not import subprocess.
    exceptions_found = sorted(item for item in discovered if item != CHOKEPOINT_MODULE)
    assert exceptions_found == sorted(ALLOWED_EXCEPTIONS), _missing_entries(
        exceptions_found, ALLOWED_EXCEPTIONS
    )


# ---------------------------------------------------------------------------
# 2. the allowlist has not grown
# ---------------------------------------------------------------------------


def test_allowlist_has_not_grown():
    """Exact membership pin: neither the allowlist nor the tree may drift."""
    discovered = tuple(sorted(_discover_importers()))

    assert discovered == PINNED_IMPORTERS, _missing_entries(
        list(discovered), PINNED_IMPORTERS
    )

    # The allowlist itself is pinned too, so a file cannot be permissioned
    # "for later" before it has an import to justify it.
    expected_allowlist = tuple(
        item for item in PINNED_IMPORTERS if item != CHOKEPOINT_MODULE
    )
    assert tuple(sorted(ALLOWED_EXCEPTIONS)) == expected_allowlist, (
        "ALLOWED_EXCEPTIONS in tests/test_tool_import_declaration_match.py no "
        "longer matches the pinned set of permitted importers.\n"
        f"  allowlist: {list(sorted(ALLOWED_EXCEPTIONS))}\n"
        f"  expected : {list(expected_allowlist)}\n"
        "  Fix: the allowlist may only contain modules that (a) really do "
        "import subprocess today and (b) are recorded here with a reason. "
        "Remove the extra entry, or -- if the import is new -- migrate it onto "
        "host_run()/host_run_async() instead."
    )


# ---------------------------------------------------------------------------
# 3. the mcp_client exception stays self-documenting
# ---------------------------------------------------------------------------


def test_mcp_client_deferral_is_self_documenting():
    """The subprocess import in tools/mcp_client.py needs its deferral markers."""
    deferral_module = "tools/mcp_client.py"

    assert deferral_module in ALLOWED_EXCEPTIONS, (
        f"{deferral_module} lost its documented-deferral exception while the "
        "file still needs it -- restore the entry in ALLOWED_EXCEPTIONS of "
        "tests/test_tool_import_declaration_match.py."
    )

    sites = _import_sites_for(deferral_module)
    assert sites, (
        f"{deferral_module} no longer imports subprocess.\n"
        "  Fix: this is the happy ending of the deferral -- delete the DEFERRED "
        "HOST EXECUTION comment at the old Popen site, drop the entry from "
        "ALLOWED_EXCEPTIONS and DEFERRED_EXCEPTIONS, and update "
        "tests/test_mcp_client_uses_host_port.py."
    )

    source = _source_for(deferral_module)
    missing = [marker for marker in DEFERRAL_MARKERS if marker not in source]
    assert not missing, (
        f"{deferral_module} still imports subprocess ("
        + ", ".join(f"line {lineno}" for lineno, _f, _s in sites)
        + ") but has lost the deferral marker(s) "
        + ", ".join(repr(marker) for marker in missing)
        + ".\n  An unexplained subprocess import is a chokepoint bypass in "
        "waiting. Fix: restore the DEFERRED HOST EXECUTION comment (it must "
        "name host_run_async, HostRunHandle and NotImplementedError), or finish "
        "the migration to host_run_async() and remove the import."
    )


# ---------------------------------------------------------------------------
# 4. the migrated modules really are migrated
# ---------------------------------------------------------------------------


def test_migrated_modules_import_no_subprocess():
    """Modules migrated onto the chokepoint must not import subprocess at all."""
    offenders: list[str] = []
    for module in MIGRATED_MODULES:
        sites = _import_sites_for(module)
        if sites:
            offenders.append(f"  {module}\n" + _describe(sites))

    assert not offenders, (
        "these modules were migrated onto thoughtmachine/host_port.py in this "
        "wave but import subprocess again:\n"
        + "\n".join(offenders)
        + "\n  Fix: this is a migration regression -- call host_run() / "
        "host_run_async() from thoughtmachine/host_port.py. Do NOT add these "
        "files to ALLOWED_EXCEPTIONS; they are listed in MIGRATED_MODULES "
        "precisely so this cannot be papered over."
    )

    # Positive control: the chokepoint DOES import subprocess, so a green run
    # above cannot be the result of a scan that finds nothing at all.
    sites = _import_sites_for(CHOKEPOINT_MODULE)
    assert sites, (
        f"{CHOKEPOINT_MODULE} no longer imports subprocess.\n"
        "  If host execution moved to another module, that module becomes the "
        "new chokepoint: point CHOKEPOINT_MODULE at it and update "
        "PINNED_IMPORTERS. If the import simply disappeared, something else is "
        "spawning host processes unguarded."
    )
    assert any(
        form.split()[0] == "import" and "subprocess" in form for _l, form, _s in sites
    ), f"{CHOKEPOINT_MODULE} must import subprocess directly; found: {sites}"


# ---------------------------------------------------------------------------
# 5. allowlist hygiene: no duplicates, no stale entries
# ---------------------------------------------------------------------------


def test_allowlist_has_no_duplicates_and_no_stale_entries():
    """A duplicated or stale allowlist entry is how these guards rot."""
    duplicates = sorted(
        {
            item
            for item in ALLOWED_EXCEPTIONS
            if ALLOWED_EXCEPTIONS.count(item) > 1
        }
    )
    assert not duplicates, (
        "ALLOWED_EXCEPTIONS in tests/test_tool_import_declaration_match.py "
        "contains duplicate entries: " + ", ".join(duplicates) + ".\n"
        "  Fix: keep exactly one entry (with one reason) per file."
    )

    stale = sorted(
        item for item in ALLOWED_EXCEPTIONS if not (REPO_ROOT / item).is_file()
    )
    assert not stale, (
        "ALLOWED_EXCEPTIONS in tests/test_tool_import_declaration_match.py "
        "names files that do not exist: " + ", ".join(stale) + ".\n"
        "  Fix: prune the entry (renamed or deleted module) -- a stale entry "
        "silently re-permits a file path that a future module could reuse."
    )

    # The conditional bucket must be a subset of the allowlist, otherwise the
    # marker test above would guard a module the guard does not actually allow.
    undeclared = sorted(set(DEFERRED_EXCEPTIONS) - set(ALLOWED_EXCEPTIONS))
    assert not undeclared, (
        "DEFERRED_EXCEPTIONS names modules that are missing from "
        "ALLOWED_EXCEPTIONS: " + ", ".join(undeclared) + ".\n"
        "  Fix: add them to ALLOWED_EXCEPTIONS with their reason, or drop them "
        "from DEFERRED_EXCEPTIONS."
    )
